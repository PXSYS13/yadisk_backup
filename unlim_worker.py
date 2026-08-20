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


MAX_ATTEMPTS = 3  # после стольких неудач файл уходит в 'failed', чтобы не крутиться вечно


def ensure_attempts_column() -> None:
    """Миграция для старых БД: колонка attempts могла отсутствовать."""
    c = sqlite3.connect(DB_FILE, timeout=60)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(unlim_files)")}
        if "attempts" not in cols:
            c.execute("ALTER TABLE unlim_files ADD COLUMN attempts INTEGER DEFAULT 0")
            c.commit()
    finally:
        c.close()


def take_batch(worker_id: int, size: int) -> list[dict]:
    """Атомарно забирает пачку pending под себя (status='in_progress').

    Возвращает список словарей {file_id, name, size} — имя и размер нужны,
    чтобы после распаковки понять, какие именно файлы реально приехали.
    """
    c = sqlite3.connect(DB_FILE, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    try:
        c.execute("BEGIN IMMEDIATE")
        rows = list(c.execute(
            "SELECT file_id, name, size FROM unlim_files WHERE status='pending' LIMIT ?",
            (size,),
        ))
        if not rows:
            c.execute("COMMIT")
            return []
        items = [{"file_id": r["file_id"], "name": r["name"],
                  "size": int(r["size"] or 0)} for r in rows]
        c.executemany(
            "UPDATE unlim_files SET status='in_progress', worker_id=?, "
            "assigned_at=CURRENT_TIMESTAMP WHERE file_id=?",
            [(worker_id, it["file_id"]) for it in items],
        )
        c.execute("COMMIT")
        return items
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
    """Возвращает файлы в очередь. После MAX_ATTEMPTS попыток — в 'failed',
    иначе воркер будет вечно крутить одну и ту же битую пачку.
    """
    if not file_ids:
        return
    c = sqlite3.connect(DB_FILE, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        c.execute("BEGIN IMMEDIATE")
        c.executemany(
            "UPDATE unlim_files SET "
            "  attempts = COALESCE(attempts, 0) + 1, "
            "  status = CASE WHEN COALESCE(attempts, 0) + 1 >= ? THEN 'failed' ELSE 'pending' END, "
            "  worker_id = NULL, error = ? "
            "WHERE file_id = ?",
            [(MAX_ATTEMPTS, error[:500], i) for i in file_ids],
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
    sk = re.search(r'"sk"\s*:\s*"([^"]+)"', r.text)
    uid = re.search(r'"uid"\s*:\s*"?(\d+)"?', r.text)
    if not sk or not uid:
        sys.exit("Не нашёл sk/uid в HTML — Яндекс изменил формат "
                 "или сессия протухла. Обнови cookies.json.")
    return sk.group(1), uid.group(1)


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


def safe_extract_dst(base_dir: str, member_name: str) -> str | None:
    """Куда безопасно распаковать запись архива.

    Отбрасывает всё, что уводит за пределы base_dir: '..', ведущие слэши,
    букву диска ('C:/evil.txt' — os.path.join такой путь просто заменил бы базу).
    Возвращает None, если запись выглядит враждебно или пустой.
    """
    raw = member_name.replace("\\", "/")
    parts = []
    for part in raw.split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        if ":" in part:  # 'C:', 'C:evil.txt' — не даём подменить диск
            continue
        parts.append(part)
    if not parts:
        return None
    dst = os.path.join(base_dir, *parts)
    # Финальная проверка: результат обязан лежать внутри base_dir
    base_real = os.path.realpath(base_dir)
    dst_real = os.path.realpath(dst)
    if os.path.commonpath([base_real, dst_real]) != base_real:
        return None
    return dst


def download_and_extract(s, url: str, worker_id: int, batch_idx: int) -> tuple[list[tuple[str, int]], int]:
    """Качает ZIP пачки и распаковывает.

    Возвращает (список успешно распакованных (имя_файла, размер), суммарные байты).
    Имена нужны наверху, чтобы пометить downloaded ТОЛЬКО реально приехавшие файлы.
    """
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
                return [], 0
    except Exception as e:
        logging.error(f"w{worker_id} b{batch_idx} download: {e}")
        return [], 0

    extracted: list[tuple[str, int]] = []
    total_bytes = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for m in zf.infolist():
                if m.is_dir():
                    continue
                dst = safe_extract_dst(EXTRACT_DIR, m.filename)
                if dst is None:
                    logging.warning(f"w{worker_id} пропускаю опасное имя в архиве: {m.filename!r}")
                    continue
                if os.path.exists(dst) and os.path.getsize(dst) == m.file_size:
                    extracted.append((os.path.basename(dst), m.file_size))
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
                    extracted.append((os.path.basename(dst), m.file_size))
                    total_bytes += m.file_size
                except Exception as e:
                    logging.warning(f"w{worker_id} extract {m.filename}: {e}")
    except zipfile.BadZipFile as e:
        logging.error(f"w{worker_id} b{batch_idx} bad zip: {e}")
        return [], 0
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass

    return extracted, total_bytes


def split_by_extracted(
    batch: list[dict],
    extracted: list[tuple[str, int]],
) -> tuple[list[str], list[str]]:
    """Делит пачку на реально приехавшие и не приехавшие файлы.

    Сопоставляем по (имя файла, размер) — то, что есть и в БД, и в архиве.
    Каждая запись архива «закрывает» ровно один файл пачки, поэтому считаем
    кратности: две одинаковые записи в БД требуют двух записей в архиве.
    """
    pool: dict[tuple[str, int], int] = {}
    for name, size in extracted:
        key = (os.path.basename(name).lower(), int(size or 0))
        pool[key] = pool.get(key, 0) + 1

    done: list[str] = []
    unmatched: list[dict] = []
    for item in batch:
        key = (os.path.basename(item.get("name") or "").lower(),
               int(item.get("size") or 0))
        if pool.get(key, 0) > 0:
            pool[key] -= 1
            done.append(item["file_id"])
        else:
            unmatched.append(item)

    # Второй проход — по одному размеру. Яндекс иногда переименовывает файлы
    # в архиве (коллизии имён внутри дня), и строгое сравнение зря отправило бы
    # их на перекачку. Остаток архива — это почти наверняка файлы этой же пачки.
    leftovers: dict[int, int] = {}
    for key, count in pool.items():
        if count > 0:
            leftovers[key[1]] = leftovers.get(key[1], 0) + count

    missing: list[str] = []
    for item in unmatched:
        size = int(item.get("size") or 0)
        if leftovers.get(size, 0) > 0:
            leftovers[size] -= 1
            done.append(item["file_id"])
        else:
            missing.append(item["file_id"])
    return done, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, required=True, help="ID воркера (1, 2, ...)")
    ap.add_argument("--batch", type=int, default=BATCH_SIZE)
    args = ap.parse_args()

    worker_id = args.id
    print(f"[w{worker_id}] старт. batch={args.batch}")

    if not os.path.exists(DB_FILE):
        sys.exit("Нет unlim_state.db — сначала запусти unlim_download.py --collect")

    ensure_attempts_column()
    recover_stuck()

    s = make_session()
    sk, uid = get_sk_uid(s)
    print(f"[w{worker_id}] авторизован uid={uid}")

    batch_idx = 0
    total_files = 0
    total_bytes = 0
    empty_streak = 0

    while True:
        batch = take_batch(worker_id, args.batch)
        if not batch:
            empty_streak += 1
            if empty_streak >= 3:
                print(f"[w{worker_id}] нет работы, выход")
                break
            time.sleep(5)
            continue
        empty_streak = 0
        batch_idx += 1
        batch_ids = [it["file_id"] for it in batch]

        url = prepare_zip(s, sk, uid, batch_ids)
        if not url:
            print(f"[w{worker_id}] batch {batch_idx}: prepare FAIL → pending")
            mark_pending(batch_ids, "prepare failed")
            time.sleep(10)
            continue

        start = time.monotonic()
        extracted, bytes_ = download_and_extract(s, url, worker_id, batch_idx)
        dur = time.monotonic() - start

        # Помечаем downloaded ТОЛЬКО те файлы, которые реально нашлись в архиве.
        # Остальные возвращаем в очередь — иначе бэкап «зелёный», а файлов нет.
        done_ids, missing_ids = split_by_extracted(batch, extracted)
        if done_ids:
            mark_done(done_ids)
            total_files += len(done_ids)
            total_bytes += bytes_
        if missing_ids:
            mark_pending(missing_ids, "не найден в распакованном архиве")

        if done_ids:
            mbps = (bytes_ / 1e6) / max(0.1, dur)
            tail = f", не приехало {len(missing_ids)}" if missing_ids else ""
            print(f"[w{worker_id}] batch {batch_idx}: ✓ {len(done_ids)}/{len(batch)} files / "
                  f"{bytes_/1e9:.2f} GB ({mbps:.1f} MB/s, {int(dur)}s{tail})")
        else:
            print(f"[w{worker_id}] batch {batch_idx}: ✗ FAIL ({len(batch)} файлов → pending)")
            time.sleep(5)

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
