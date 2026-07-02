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
import json
import logging
import os
import random
import re
import sqlite3
import sys
import threading
import time

import requests
from dotenv import load_dotenv
from tqdm import tqdm

sys.stdout.reconfigure(encoding="utf-8")

PROJECT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT, ".env"))

COOKIES_FILE = os.path.join(PROJECT, "cookies.json")
DB_FILE = os.path.join(PROJECT, "unlim_state.db")
LOG_FILE = os.path.join(PROJECT, "logs", "unlim.log")

BASE = "https://disk.yandex.ru"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("unlim")


# ── DB ──────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS unlim_files (
    file_id TEXT PRIMARY KEY,
    cluster_id TEXT,
    name TEXT,
    size INTEGER,
    md5 TEXT,
    mime TEXT,
    status TEXT DEFAULT 'pending',
    local_path TEXT,
    error TEXT,
    worker_id INTEGER,
    assigned_at TIMESTAMP,
    downloaded_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_unlim_status ON unlim_files(status);
CREATE TABLE IF NOT EXISTS unlim_clusters (
    cluster_id TEXT PRIMARY KEY,
    size INTEGER,
    fetched INTEGER DEFAULT 0
);
"""

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
    db().executescript(SCHEMA)


# ── HTTP ────────────────────────────────────────────────────────────────────


def load_cookies() -> dict[str, str]:
    if not os.path.exists(COOKIES_FILE):
        sys.exit("Нет cookies.json — сначала экспортируй куки из браузера. "
                 "См. README раздел про куки.")
    raw = json.loads(open(COOKIES_FILE, encoding="utf-8").read())
    if not isinstance(raw, list):
        sys.exit("cookies.json должен быть массивом (формат Cookie-Editor)")
    return {c["name"]: c["value"] for c in raw
            if c.get("name") and c.get("value") is not None}


def make_session(cookies: dict[str, str]) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "ru,en;q=0.9",
        "Origin": BASE, "Referer": f"{BASE}/client/photo",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
    })
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".yandex.ru")
    return s


def get_sk_uid(s: requests.Session) -> tuple[str, str]:
    r = s.get(f"{BASE}/client/disk", timeout=30)
    if "passport.yandex" in r.url or r.status_code != 200:
        sys.exit("Куки невалидны — нужно обновить cookies.json")
    sk = re.search(r'"sk"\s*:\s*"([^"]+)"', r.text)
    uid = re.search(r'"uid"\s*:\s*"?(\d+)"?', r.text)
    if not sk or not uid:
        sys.exit("Не нашёл sk/uid в HTML — Яндекс изменил формат")
    return sk.group(1), uid.group(1)


def make_cid(uid: str) -> str:
    return f"{uid}{int(time.time() * 1000)}{random.randint(100, 999)}"


def api_call(s: requests.Session, sk: str, uid: str, method: str,
             params=None, retries: int = 3):
    last_err = None
    for att in range(retries):
        try:
            body = {"sk": sk, "connection_id": make_cid(uid), "apiMethod": method}
            if params is not None:
                body["requestParams"] = params
            r = s.post(f"{BASE}/models-v2", params={"m": method},
                       json=body, timeout=120)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return None
            if r.status_code in (401, 403):
                sys.exit(f"Сессия истекла (HTTP {r.status_code})")
            if r.status_code == 429:
                wait = 30 + att * 30
                log.warning(f"{method} HTTP 429 → sleep {wait}")
                time.sleep(wait)
                continue
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** att)
    raise RuntimeError(f"{method}: {last_err}")


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
        try: c.execute("ROLLBACK")
        except sqlite3.Error: pass
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
                # photoslice отдаёт и удалённые фото (корзина) — их bulk-download
                # НЕ качает (HTTP 409 BulkDownloadNoFilesToDownload), а один
                # trash-путь роняет весь батч. Не пишем их в БД вообще.
                if fid.startswith("/trash"):
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
        try: c.execute("ROLLBACK")
        except sqlite3.Error: pass
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
        print(f"  ✓ photoslice_id получен")

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
        try: s.close()
        except Exception: pass


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true",
                    help="Собрать список файлов из photoslice")
    ap.add_argument("--status", action="store_true",
                    help="Показать статус БД")
    args = ap.parse_args()
    if args.collect:
        cmd_collect()
    elif args.status:
        cmd_status()
    else:
        ap.print_help()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
