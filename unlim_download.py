"""Сборка списка файлов безлимитного фото-хранилища (photoslice) через web API.

Использует cookies.json (экспортированные из браузера расширением Cookie-Editor).
Запоминает все файлы в unlim_state.db со статусом 'pending'. Само скачивание —
в unlim_worker.py (можно запускать несколько параллельно).

CLI:
  python unlim_download.py --collect       # собрать список из photoslice
  python unlim_download.py --status        # показать статус БД
  python unlim_download.py --retry-failed  # вернуть упавшие и зависшие в очередь
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import threading
import time

from tqdm import tqdm

from unlim_common import (
    LOG_DIR,
    UNLIM_DB_FILE as DB_FILE,
    UNLIM_SCHEMA as SCHEMA,
    AuthExpiredError,
    api_call,
    ensure_attempts_column,
    get_sk_uid,
    load_cookies,
    make_session,
    reset_failed_files,
)
from utils import ensure_utf8_stdio, sqlite_tx

LOG_FILE = os.path.join(LOG_DIR, "unlim.log")
# Сколько кластеров (дней) запрашиваем одним вызовом API
CLUSTER_BATCH = 15

log = logging.getLogger("unlim")


def _setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s | %(levelname)s | %(message)s")


# ── DB ──────────────────────────────────────────────────────────────────────

_tls = threading.local()


def db() -> sqlite3.Connection:
    c = getattr(_tls, "c", None)
    if c is None:
        c = sqlite3.connect(DB_FILE, timeout=30, isolation_level=None,
                            check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.row_factory = sqlite3.Row
        _tls.c = c
    return c


def db_init():
    c = db()
    c.executescript(SCHEMA)
    ensure_attempts_column(DB_FILE)


def status_counts() -> dict[str, int]:
    return {r["status"]: r["n"] for r in db().execute(
        "SELECT status, COUNT(*) AS n FROM unlim_files GROUP BY status"
    )}


# ── Логика сбора photoslice ─────────────────────────────────────────────────


def init_snapshot(s, sk, uid) -> str:
    d = api_call(s, sk, uid, "intapi/photo-init-snapshot")
    psid = d.get("photoslice_id")
    if not psid:
        raise RuntimeError(f"no photoslice_id in {d}")
    return psid


def get_all_clusters(s, sk, uid, psid, batch_size: int = 5000) -> list[dict]:
    """Получаем плоский список всех кластеров (дни) с поддержкой пагинации."""
    all_items: list[dict] = []
    offset = 0
    while True:
        snap = api_call(
            s, sk, uid, "intapi/photo-get-snapshot",
            {"id": psid, "locale": "ru", "amount": batch_size, "offset": offset},
        )
        items = snap.get("items", []) if isinstance(snap, dict) else []
        if not items:
            break
        all_items.extend(items)
        if len(items) < batch_size:
            break
        offset += len(items)
    return all_items


def save_clusters(items: list[dict]):
    """Сохраняет кластеры (дни). Если в известном дне стало больше файлов —
    он перезапрашивается: иначе новые фото в старых днях никогда не попадут в бэкап.
    """
    with sqlite_tx(db()) as c:
        c.executemany(
            """INSERT INTO unlim_clusters (cluster_id, size) VALUES (?, ?)
               ON CONFLICT(cluster_id) DO UPDATE SET
                   fetched = CASE WHEN excluded.size <> unlim_clusters.size THEN 0 ELSE fetched END,
                   size = excluded.size""",
            [(it.get("id"), int(it.get("size", 0) or 0)) for it in items],
        )


def fetch_cluster_resources(s, sk, uid, psid, cluster_rows: list[dict]) -> int:
    """Запрашивает файлы для пачки кластеров."""
    clusters_map = {}
    for row in cluster_rows:
        size = max(int(row["size"] or 0), 1)
        clusters_map[row["cluster_id"]] = {"range": [0, max(size - 1, 0)]}
    res = api_call(s, sk, uid, "intapi/photo-get-clusters-with-resources", {
        "photosliceId": psid,
        "clusters": clusters_map,
        "hideScreenshots": False,
    })
    fetched = (res.get("clusters") or {}).get("fetched") or []
    added = 0
    with sqlite_tx(db()) as c:
        for cl in fetched:
            cid = cl.get("id")
            for f in cl.get("items", []) or []:
                fid = f.get("id") or f.get("resource_id")
                if not fid:
                    continue
                name = f.get("name") or fid.rsplit("/", 1)[-1]
                size = int(f.get("size", 0) or 0)
                md5 = f.get("md5") or f.get("etag")
                mime = f.get("mimetype") or f.get("type")
                added += c.execute(
                    """INSERT INTO unlim_files
                       (file_id, cluster_id, name, size, md5, mime, status)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending')
                       ON CONFLICT(file_id) DO NOTHING""",
                    (fid, cid, name, size, md5, mime),
                ).rowcount
            c.execute("UPDATE unlim_clusters SET fetched=1 WHERE cluster_id=?",
                      (cid,))
    return added


# ── CLI ─────────────────────────────────────────────────────────────────────


def cmd_collect():
    db_init()
    s = make_session(load_cookies())
    try:
        try:
            sk, uid = get_sk_uid(s)
        except AuthExpiredError as e:
            print(f"❌ Ошибка авторизации: {e}")
            print("   Обновите cookies.json в Web UI или браузере.")
            return 2
        print(f"  ✓ uid={uid}, sk OK")

        psid = init_snapshot(s, sk, uid)
        print("  ✓ photoslice_id получен")

        clusters = get_all_clusters(s, sk, uid, psid)
        print(f"  ✓ кластеров (дней с фото): {len(clusters)}")
        save_clusters(clusters)

        todo = [dict(r) for r in db().execute(
            "SELECT cluster_id, size FROM unlim_clusters WHERE fetched=0"
        )]
        print(f"\nЗапрашиваю файлы для {len(todo)} кластеров...")
        bar = tqdm(total=len(todo), unit="cluster", disable=not sys.stderr.isatty())
        added_total = 0
        try:
            for i in range(0, len(todo), CLUSTER_BATCH):
                batch = todo[i:i + CLUSTER_BATCH]
                try:
                    added_total += fetch_cluster_resources(s, sk, uid, psid, batch)
                except AuthExpiredError as e:
                    # Куки протухли посреди сбора — дальше бессмысленно, и это не «успех»
                    print(f"\n❌ auth_expired | Ошибка авторизации: {e}")
                    print("   Обновите cookies.json в Web UI или браузере и запустите сбор заново.")
                    return 2
                except Exception as e:
                    log.error(f"batch {i}: {e}")
                    time.sleep(2)
                bar.update(len(batch))
                bar.set_postfix(files=added_total)
        finally:
            bar.close()

        stats = status_counts()
        print(f"\n  ✓ Файлов в БД: {sum(stats.values())}")
        for k, v in stats.items():
            print(f"     {k}: {v}")
    finally:
        try:
            s.close()
        except Exception:
            pass


def cmd_status():
    if not os.path.exists(DB_FILE):
        print("БД пуста — запусти --collect")
        return
    db_init()
    stats = status_counts()
    print(f"Всего файлов в безлимите: {sum(stats.values())}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


def cmd_retry_failed():
    """Возвращает failed-файлы и зависшие in_progress в очередь (сбрасывает счётчик попыток)."""
    if not os.path.exists(DB_FILE):
        print("БД пуста — запусти --collect")
        return
    db_init()
    n = reset_failed_files(DB_FILE, include_in_progress=True)
    print(f"Возвращено в очередь: {n}")


def main():
    ensure_utf8_stdio()

    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true",
                    help="Собрать список файлов из photoslice")
    ap.add_argument("--status", action="store_true",
                    help="Показать статус БД")
    ap.add_argument("--retry-failed", action="store_true",
                    help="Вернуть упавшие и зависшие файлы в очередь")
    args = ap.parse_args()
    _setup_logging()
    if args.collect:
        return cmd_collect()
    elif args.status:
        cmd_status()
    elif args.retry_failed:
        cmd_retry_failed()
    else:
        ap.print_help()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
