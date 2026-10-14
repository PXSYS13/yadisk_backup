"""Сборка списка файлов безлимитного фото-хранилища через web API Диска.

Использует cookies.json (экспортированные из браузера расширением Cookie-Editor).
Запоминает все файлы в unlim_state.db со статусом 'pending'. Само скачивание —
в unlim_worker.py (можно запускать несколько параллельно).

Список берётся постранично из папки /photounlim (mpfs/list) — с размерами и MD5.
Фотосрез (photoslice) — запасной путь: с октября 2026 Яндекс отдаёт его пустым.

CLI:
  python unlim_download.py --collect       # собрать список безлимита
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
from utils import ensure_utf8_stdio, human_bytes, sqlite_tx

LOG_FILE = os.path.join(LOG_DIR, "unlim.log")
# Сколько кластеров (дней) запрашиваем одним вызовом API
CLUSTER_BATCH = 15
# Безлимит во внутреннем API Диска (mpfs); фотосрез называет те же файлы «photounlim:/имя»
UNLIM_ROOT = "/photounlim"
LIST_PAGE = 1000
LIST_META = "size,md5,mimetype,mediatype"

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


# ── Сбор через mpfs/list ────────────────────────────────────────────────────


def list_unlim_page(s, sk, uid, offset: int, amount: int = LIST_PAGE) -> list[dict]:
    """Одна страница /photounlim. Первый элемент ответа — сама папка."""
    data = api_call(s, sk, uid, "mpfs/list",
                    {"path": UNLIM_ROOT, "amount": amount, "offset": offset, "meta": LIST_META})
    if not isinstance(data, list):
        raise RuntimeError(f"mpfs/list: ожидался список, пришёл {type(data).__name__}: {str(data)[:200]}")
    return data


def save_listed_files(items: list[dict]) -> int:
    """Добавляет файлы из mpfs/list, возвращает число новых.

    Уже известный файл (в том числе собранный фотосрезом как 'photounlim:/имя') не
    дублируется — ему только записываются размер и MD5 из mpfs, они точнее.
    """
    added = 0
    with sqlite_tx(db()) as c:
        for f in items:
            path = f.get("path") or ""
            if not path.startswith(UNLIM_ROOT + "/"):
                continue
            meta = f.get("meta") or {}
            size = int(meta.get("size") or 0)
            md5 = meta.get("md5") or None
            mime = meta.get("mimetype") or meta.get("mediatype")
            alt = "photounlim:" + path[len(UNLIM_ROOT):]
            known = c.execute(
                "UPDATE unlim_files SET size = COALESCE(NULLIF(?, 0), size), md5 = COALESCE(?, md5), "
                "mime = COALESCE(mime, ?) WHERE file_id IN (?, ?)",
                (size, md5, mime, path, alt),
            ).rowcount
            if known:
                continue
            c.execute(
                "INSERT INTO unlim_files (file_id, name, size, md5, mime, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (path, f.get("name") or path.rsplit("/", 1)[-1], size, md5, mime),
            )
            added += 1
    return added


def collect_via_list(s, sk, uid) -> int:
    """Весь безлимит постранично. Возвращает, сколько файлов увидели."""
    print("\nЧитаю список безлимита (mpfs/list)...", flush=True)
    seen = added = 0
    total_bytes = 0
    dirs = 0
    offset = 0
    while True:
        page = list_unlim_page(s, sk, uid, offset)
        children = [x for x in page if (x.get("path") or "").rstrip("/") != UNLIM_ROOT]
        files = [x for x in children if x.get("type") == "file"]
        dirs += len(children) - len(files)
        added += save_listed_files(files)
        seen += len(files)
        total_bytes += sum(int((x.get("meta") or {}).get("size") or 0) for x in files)
        print(f"  Найдено: {seen} файлов ({human_bytes(total_bytes)}), новых {added}", flush=True)
        if len(children) < LIST_PAGE:
            break
        offset += len(children)
    if dirs:
        log.warning("mpfs/list: в %s есть вложенные папки (%d) — их содержимое не собрано", UNLIM_ROOT, dirs)
    log.info("mpfs/list: %d файлов, %s, новых %d", seen, human_bytes(total_bytes), added)
    return seen


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


def collect_via_photoslice(s, sk, uid) -> int | None:
    """Запасной сбор через фотосрез. Возвращает 2, если куки протухли."""
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
    return None


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

        try:
            listed = collect_via_list(s, sk, uid)
        except AuthExpiredError as e:
            print(f"\n❌ auth_expired | Ошибка авторизации: {e}")
            print("   Обновите cookies.json в Web UI или браузере и запустите сбор заново.")
            return 2
        except Exception as e:
            log.error(f"mpfs/list: {e}")
            print(f"  ⚠️ Список через mpfs/list не получен ({e}) — пробую фотосрез")
            listed = 0
        if not listed:
            code = collect_via_photoslice(s, sk, uid)
            if code:
                return code

        stats = status_counts()
        print(f"\n  ✓ Файлов в БД: {sum(stats.values())}")
        for k, v in stats.items():
            print(f"     {k}: {v}")
        if not stats:
            print("  ⚠️ Яндекс не вернул ни одного файла безлимита. Если в веб-версии Диска фото есть — "
                  "обновите cookies.json и запустите сбор ещё раз.")
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
                    help="Собрать список файлов безлимита")
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
