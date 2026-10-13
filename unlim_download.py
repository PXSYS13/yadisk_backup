"""Сборка списка файлов безлимитного фото-хранилища (photoslice) через web API.

Использует cookies.json (экспортированные из браузера расширением Cookie-Editor).
Запоминает все файлы в unlim_state.db со статусом 'pending'. Само скачивание —
в unlim_worker.py (можно запускать несколько параллельно).

CLI:
  python unlim_download.py --collect    # собрать список из photoslice
  python unlim_download.py --status     # показать статус БД
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
    UNLIM_DB_FILE as DB_FILE,
    UNLIM_SCHEMA as SCHEMA,
    api_call,
    ensure_attempts_column,
    get_sk_uid,
    load_cookies,
    make_session,
)

PROJECT = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(PROJECT, "logs", "unlim.log")

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("unlim")


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


# ── Логика сбора photoslice ─────────────────────────────────────────────────


def init_snapshot(s, sk, uid) -> str:
    d = api_call(s, sk, uid, "intapi/photo-init-snapshot")
    psid = d.get("photoslice_id")
    if not psid:
        raise RuntimeError(f"no photoslice_id in {d}")
    return psid


def get_all_clusters(s, sk, uid, psid) -> list[dict]:
    """Получаем плоский список всех кластеров (дни)."""
    snap = api_call(s, sk, uid, "intapi/photo-get-snapshot",
                    {"id": psid, "locale": "ru",
                     "amount": 100000, "offset": 0})
    return snap.get("items", [])


def save_clusters(items: list[dict]):
    c = db()
    c.execute("BEGIN IMMEDIATE")
    try:
        for it in items:
            c.execute(
                "INSERT OR IGNORE INTO unlim_clusters (cluster_id, size) VALUES (?, ?)",
                (it.get("id"), int(it.get("size", 0) or 0)),
            )
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


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
    c = db()
    c.execute("BEGIN IMMEDIATE")
    try:
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
                c.execute(
                    """INSERT INTO unlim_files
                       (file_id, cluster_id, name, size, md5, mime, status)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending')
                       ON CONFLICT(file_id) DO NOTHING""",
                    (fid, cid, name, size, md5, mime),
                )
                added += 1
            c.execute("UPDATE unlim_clusters SET fetched=1 WHERE cluster_id=?",
                      (cid,))
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return added


# ── CLI ─────────────────────────────────────────────────────────────────────


def cmd_collect():
    db_init()
    s = make_session(load_cookies())
    try:
        sk, uid = get_sk_uid(s)
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
        BATCH = 15
        bar = tqdm(total=len(todo), unit="cluster")
        added_total = 0
        try:
            for i in range(0, len(todo), BATCH):
                batch = todo[i:i + BATCH]
                try:
                    n = fetch_cluster_resources(s, sk, uid, psid, batch)
                    added_total += n
                except Exception as e:
                    log.error(f"batch {i}: {e}")
                    time.sleep(2)
                bar.update(len(batch))
                bar.set_postfix(files=added_total)
        finally:
            bar.close()

        stats = {r["status"]: r["n"] for r in db().execute(
            "SELECT status, COUNT(*) AS n FROM unlim_files GROUP BY status"
        )}
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
    stats = {r["status"]: r["n"] for r in db().execute(
        "SELECT status, COUNT(*) AS n FROM unlim_files GROUP BY status"
    )}
    total = sum(stats.values())
    print(f"Всего файлов в безлимите: {total}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


def cmd_retry_failed():
    """Возвращает failed-файлы в очередь (сбрасывает счётчик попыток)."""
    if not os.path.exists(DB_FILE):
        print("БД пуста — запусти --collect")
        return
    db_init()
    c = db()
    c.execute("BEGIN IMMEDIATE")
    try:
        n = c.execute(
            "UPDATE unlim_files SET status='pending', attempts=0, error=NULL, "
            "worker_id=NULL WHERE status IN ('failed', 'in_progress')"
        ).rowcount
        c.execute("COMMIT")
    except Exception:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    print(f"Возвращено в очередь: {n}")


def main():
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true",
                    help="Собрать список файлов из photoslice")
    ap.add_argument("--status", action="store_true",
                    help="Показать статус БД")
    ap.add_argument("--retry-failed", action="store_true",
                    help="Вернуть упавшие и зависшие файлы в очередь")
    args = ap.parse_args()
    if args.collect:
        cmd_collect()
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
