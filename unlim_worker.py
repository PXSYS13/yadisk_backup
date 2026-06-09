"""Параллельный воркер скачивания безлимита (photoslice).

Берёт pending файлы пачками, для каждой просит Яндекс собрать ZIP,
скачивает, распаковывает в DOWNLOAD_DIR/_unlim/, удаляет ZIP.

Можно запускать несколько процессов параллельно — каждый атомарно
забирает свою пачку из БД и не мешает другим.

Запуск:
  python unlim_worker.py --id 1
  python unlim_worker.py --id 2
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
import time
import zipfile

import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")

PROJECT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT, ".env"))

COOKIES_FILE = os.path.join(PROJECT, "cookies.json")
DB_FILE = os.path.join(PROJECT, "unlim_state.db")
LOG_FILE = os.path.join(PROJECT, "logs", "unlim_worker.log")

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(PROJECT, "downloads")).rstrip("/\\")
EXTRACT_DIR = os.path.join(DOWNLOAD_DIR, "_unlim")
ZIP_TMP_DIR = os.path.join(DOWNLOAD_DIR, "_unlim_tmp_zips")
BATCH_SIZE = 250

BASE = "https://disk.yandex.ru"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")


def take_batch(worker_id: int, size: int) -> list[str]:
    """Атомарно забирает пачку pending под себя (status='in_progress')."""
    c = sqlite3.connect(DB_FILE, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    try:
        c.execute("BEGIN IMMEDIATE")
        rows = list(c.execute(
            "SELECT file_id FROM unlim_files WHERE status='pending' LIMIT ?",
            (size,),
        ))
        if not rows:
            c.execute("COMMIT")
            return []
        ids = [r["file_id"] for r in rows]
        c.executemany(
            "UPDATE unlim_files SET status='in_progress', worker_id=?, "
            "assigned_at=CURRENT_TIMESTAMP WHERE file_id=?",
            [(worker_id, i) for i in ids],
        )
        c.execute("COMMIT")
        return ids
    except Exception:
        try: c.execute("ROLLBACK")
        except sqlite3.Error: pass
        raise
    finally:
        c.close()


def mark_done(file_ids: list[str]):
    c = sqlite3.connect(DB_FILE, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        c.execute("BEGIN IMMEDIATE")
        c.executemany(
            "UPDATE unlim_files SET status='downloaded', downloaded_at=CURRENT_TIMESTAMP, "
            "worker_id=NULL, error=NULL WHERE file_id=?",
            [(i,) for i in file_ids],
        )
        c.execute("COMMIT")
    finally:
        c.close()


def mark_pending(file_ids: list[str], error: str):
    c = sqlite3.connect(DB_FILE, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        c.execute("BEGIN IMMEDIATE")
        c.executemany(
            "UPDATE unlim_files SET status='pending', worker_id=NULL, error=? WHERE file_id=?",
            [(error[:500], i) for i in file_ids],
        )
        c.execute("COMMIT")
    finally:
        c.close()


def recover_stuck(timeout_sec: int = 1800) -> int:
    """Возвращает в pending файлы которые в in_progress дольше timeout."""
    c = sqlite3.connect(DB_FILE, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        c.execute("BEGIN IMMEDIATE")
        n = c.execute(
            "UPDATE unlim_files SET status='pending', worker_id=NULL "
            "WHERE status='in_progress' AND "
            "(julianday(CURRENT_TIMESTAMP) - julianday(assigned_at)) * 86400 > ?",
            (timeout_sec,),
        ).rowcount
        c.execute("COMMIT")
        return n
    finally:
        c.close()


# ── HTTP ────────────────────────────────────────────────────────────────────


def make_session():
    raw = json.loads(open(COOKIES_FILE, encoding="utf-8").read())
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "ru,en;q=0.9",
        "Origin": BASE, "Referer": f"{BASE}/client/photo",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
    })
    for c in raw:
        if c.get("name") and c.get("value"):
            s.cookies.set(c["name"], c["value"], domain=".yandex.ru")
    return s


def get_sk_uid(s):
    r = s.get(f"{BASE}/client/disk", timeout=30)
    if "passport.yandex" in r.url or r.status_code != 200:
        sys.exit("Куки невалидны — обнови cookies.json")
    return (re.search(r'"sk"\s*:\s*"([^"]+)"', r.text).group(1),
            re.search(r'"uid"\s*:\s*"?(\d+)"?', r.text).group(1))


def prepare_zip(s, sk, uid, paths: list[str]) -> str | None:
    body = {"sk": sk,
            "connection_id": f"{uid}{int(time.time()*1000)}{random.randint(100,999)}",
            "apiMethod": "mpfs/bulk-download-prepare",
            "requestParams": {"items": paths}}
    try:
        r = s.post(f"{BASE}/models-v2",
                   params={"m": "mpfs/bulk-download-prepare"},
                   json=body, timeout=60)
        if r.status_code != 200:
            logging.warning(f"prepare HTTP {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        url = data.get("download_url")
        if url and url.startswith("//"):
            url = "https:" + url
        return url
    except Exception as e:
        logging.warning(f"prepare exc: {e}")
        return None


def download_and_extract(s, url: str, worker_id: int, batch_idx: int) -> tuple[int, int]:
    os.makedirs(ZIP_TMP_DIR, exist_ok=True)
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    zip_path = os.path.join(ZIP_TMP_DIR, f"w{worker_id}_b{batch_idx}.zip")
    try:
        with s.get(url, stream=True, timeout=900) as r:
            r.raise_for_status()
            tmp = zip_path + ".part"
            try:
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(2 * 1024 * 1024):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp, zip_path)
            except OSError as e:
                # disk full / permission denied
                logging.error(f"w{worker_id} b{batch_idx} write: {e}")
                try: os.remove(tmp)
                except OSError: pass
                return 0, 0
    except Exception as e:
        logging.error(f"w{worker_id} b{batch_idx} download: {e}")
        return 0, 0

    extracted = 0
    total_bytes = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for m in zf.infolist():
                if m.is_dir():
                    continue
                safe = m.filename.replace("\\", "/").lstrip("/")
                if ".." in safe.split("/"):
                    continue
                dst = os.path.join(EXTRACT_DIR, safe)
                if os.path.exists(dst) and os.path.getsize(dst) == m.file_size:
                    extracted += 1
                    total_bytes += m.file_size
                    continue
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                try:
                    with zf.open(m) as src, open(dst, "wb") as f:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                    extracted += 1
                    total_bytes += m.file_size
                except Exception as e:
                    logging.warning(f"w{worker_id} extract {safe}: {e}")
    except zipfile.BadZipFile as e:
        logging.error(f"w{worker_id} b{batch_idx} bad zip: {e}")
        return 0, 0
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass

    return extracted, total_bytes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, required=True, help="ID воркера (1, 2, ...)")
    ap.add_argument("--batch", type=int, default=BATCH_SIZE)
    args = ap.parse_args()

    worker_id = args.id
    print(f"[w{worker_id}] старт. batch={args.batch}")

    if not os.path.exists(DB_FILE):
        sys.exit("Нет unlim_state.db — сначала запусти unlim_download.py --collect")

    recover_stuck()

    s = make_session()
    sk, uid = get_sk_uid(s)
    print(f"[w{worker_id}] авторизован uid={uid}")

    batch_idx = 0
    total_files = 0
    total_bytes = 0
    empty_streak = 0

    while True:
        paths = take_batch(worker_id, args.batch)
        if not paths:
            empty_streak += 1
            if empty_streak >= 3:
                print(f"[w{worker_id}] нет работы, выход")
                break
            time.sleep(5)
            continue
        empty_streak = 0
        batch_idx += 1

        url = prepare_zip(s, sk, uid, paths)
        if not url:
            print(f"[w{worker_id}] batch {batch_idx}: prepare FAIL → pending")
            mark_pending(paths, "prepare failed")
            time.sleep(10)
            continue

        start = time.monotonic()
        extracted, bytes_ = download_and_extract(s, url, worker_id, batch_idx)
        dur = time.monotonic() - start

        if extracted > 0:
            mark_done(paths)
            total_files += extracted
            total_bytes += bytes_
            mbps = (bytes_ / 1e6) / max(0.1, dur)
            print(f"[w{worker_id}] batch {batch_idx}: ✓ {extracted} files / "
                  f"{bytes_/1e9:.2f} GB ({mbps:.1f} MB/s, {int(dur)}s)")
        else:
            print(f"[w{worker_id}] batch {batch_idx}: ✗ FAIL → pending")
            mark_pending(paths, "download/extract failed")

    # Закрываем HTTP-сессию (освобождаем sockets)
    try:
        s.close()
    except Exception:
        pass

    # Чистим пустую папку для ZIP'ов
    try:
        if os.path.isdir(ZIP_TMP_DIR) and not os.listdir(ZIP_TMP_DIR):
            os.rmdir(ZIP_TMP_DIR)
    except OSError:
        pass

    print(f"[w{worker_id}] ИТОГ: {total_files} files / {total_bytes/1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
