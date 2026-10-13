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
import logging
import os
import re
import sqlite3
import sys
import time
import zipfile
from collections import defaultdict

import requests
from dotenv import load_dotenv

from unlim_common import (
    BASE,
    COOKIES_FILE,
    ensure_attempts_column as common_ensure_attempts,
    get_sk_uid,
    make_cid,
    make_session as common_make_session,
)
from utils import TELEGRAM_CHANNEL, human_bytes

PROJECT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT, ".env"))

DB_FILE = os.path.join(PROJECT, "unlim_state.db")
LOG_FILE = os.path.join(PROJECT, "logs", "unlim_worker.log")

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(PROJECT, "downloads")).rstrip("/\\")
EXTRACT_DIR = os.path.join(DOWNLOAD_DIR, "_unlim")
ZIP_TMP_DIR = os.path.join(DOWNLOAD_DIR, "_unlim_tmp_zips")
BATCH_SIZE = 80
MAX_BATCH_BYTES = 800 * 1024 * 1024  # 800 MB (предотвращает зависания CDN Яндекса на гигантских архивах)
LARGE_FILE_THRESHOLD = 400 * 1024 * 1024  # 400 MB (тяжёлые видео качаем соло-пачкой)

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")
log = logging.getLogger("unlim_worker")

MAX_ATTEMPTS = 3  # после стольких неудач файл уходит в 'failed', чтобы не крутиться вечно


def ensure_attempts_column() -> None:
    """Миграция для старых БД: колонка attempts могла отсутствовать."""
    common_ensure_attempts(DB_FILE)


def take_batch(worker_id: int, size: int = BATCH_SIZE, max_bytes: int = MAX_BATCH_BYTES) -> list[dict]:
    """Атомарно забирает пачку pending с контролем как количества, так и суммарного веса.

    Если первый файл уже тяжёлый (>= LARGE_FILE_THRESHOLD) или имел сбои (attempts > 0) —
    забирает ровно 1 файл, изолируя проблемные файлы в индивидуальные загрузки.
    """
    c = sqlite3.connect(DB_FILE, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    try:
        c.execute("BEGIN IMMEDIATE")
        fetch_limit = max(size * 2, 40)
        cols = {r[1] for r in c.execute("PRAGMA table_info(unlim_files)")}
        has_attempts = "attempts" in cols
        has_assigned = "assigned_at" in cols

        att_col = "COALESCE(attempts, 0) AS attempts" if has_attempts else "0 AS attempts"
        order_parts = []
        if has_attempts:
            order_parts.append("attempts ASC")
        if has_assigned:
            order_parts.append("assigned_at ASC NULLS FIRST")
        order_parts.append("rowid ASC")
        order_clause = f"ORDER BY {', '.join(order_parts)}"

        rows = list(c.execute(
            f"SELECT file_id, name, size, {att_col} "
            f"FROM unlim_files WHERE status='pending' "
            f"{order_clause} "
            f"LIMIT ?",
            (fetch_limit,),
        ))
        if not rows:
            c.execute("COMMIT")
            return []

        items: list[dict] = []
        total_size = 0
        for r in rows:
            file_size = int(r["size"] or 0)
            attempts = int(r["attempts"] or 0)
            # Если первый же файл тяжёлый или уже имел сбои — качаем соло (1 файл)
            if not items and (file_size >= LARGE_FILE_THRESHOLD or attempts > 0):
                items.append({"file_id": r["file_id"], "name": r["name"], "size": file_size, "attempts": attempts})
                break
            # Если добавление превысит лимит размера или наткнулись на проблемный файл — останавливаемся
            if items and (total_size + file_size > max_bytes or len(items) >= size or attempts > 0):
                break
            items.append({"file_id": r["file_id"], "name": r["name"], "size": file_size, "attempts": attempts})
            total_size += file_size

        if not items:
            c.execute("COMMIT")
            return []

        c.executemany(
            "UPDATE unlim_files SET status='in_progress', worker_id=?, "
            "assigned_at=CURRENT_TIMESTAMP WHERE file_id=?",
            [(worker_id, it["file_id"]) for it in items],
        )
        c.execute("COMMIT")
        return items
    except Exception:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
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
            "  worker_id = NULL, error = ?, "
            "  assigned_at = CURRENT_TIMESTAMP "
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


def make_session(cookies: dict[str, str] | None = None) -> requests.Session:
    return common_make_session(cookies=cookies, cookies_file=COOKIES_FILE)


def prepare_zip(s, sk, uid, paths: list[str]) -> str | None:
    body = {"sk": sk,
            "connection_id": make_cid(uid),
            "apiMethod": "mpfs/bulk-download-prepare",
            "requestParams": {"items": paths}}
    try:
        r = s.post(f"{BASE}/models-v2",
                   params={"m": "mpfs/bulk-download-prepare"},
                   json=body, timeout=60)
        if r.status_code != 200:
            if r.status_code == 429:
                log.warning(f"prepare HTTP 429 (Rate limit Яндекса). Пауза... Новости: {TELEGRAM_CHANNEL}")
            elif r.status_code >= 500:
                log.warning(f"prepare HTTP {r.status_code} (Сбой серверов Яндекса / перегрузка): {r.text[:200]}")
            else:
                log.warning(f"prepare HTTP {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        url = data.get("download_url")
        if url and url.startswith("//"):
            url = "https:" + url
        return url
    except Exception as e:
        log.warning(f"prepare exc (сбой связи с сервером Яндекса): {e}")
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


def _non_colliding_path(path: str) -> str:
    """Генерирует уникальный путь с суффиксом _1, _2 если файл уже существует."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{base}_{i}{ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def _clean_stem(name: str) -> tuple[str, str]:
    base = os.path.basename(name).lower()
    stem, ext = os.path.splitext(base)
    clean = re.sub(r"[\s_]*\(\d+\)$|_+\d+$", "", stem).strip()
    return clean, ext


def download_and_extract(s, url: str, worker_id: int, batch_idx: int) -> tuple[list[tuple[str, int]], int]:
    """Качает ZIP пачки и распаковывает.

    Возвращает (список успешно распакованных (имя_файла, размер), суммарные байты).
    Имена нужны наверху, чтобы пометить downloaded ТОЛЬКО реально приехавшие файлы.
    """
    os.makedirs(ZIP_TMP_DIR, exist_ok=True)
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    zip_path = os.path.join(ZIP_TMP_DIR, f"w{worker_id}_b{batch_idx}.zip")
    tmp = zip_path + ".part"
    try:
        with s.get(url, stream=True, timeout=(30, 300)) as r:
            r.raise_for_status()
            headers = getattr(r, "headers", {}) or {}
            content_len = int(headers.get("Content-Length", 0) or 0)
            written = 0
            last_log = time.monotonic()
            try:
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                            now = time.monotonic()
                            if now - last_log >= 6.0:
                                last_log = now
                                total_str = f" / {human_bytes(content_len)}" if content_len > 0 else ""
                                print(f"[w{worker_id}] b{batch_idx}: качаю архив {human_bytes(written)}{total_str}...", flush=True)
                os.replace(tmp, zip_path)
            except OSError as e:
                # disk full / permission denied
                log.error(f"w{worker_id} b{batch_idx} write: {e}")
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return [], 0
    except Exception as e:
        log.error(f"w{worker_id} b{batch_idx} download: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
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
                    log.warning(f"w{worker_id} пропускаю опасное имя в архиве: {m.filename!r}")
                    continue
                if os.path.exists(dst):
                    if os.path.getsize(dst) == m.file_size:
                        extracted.append((os.path.basename(m.filename), m.file_size))
                        total_bytes += m.file_size
                        continue
                    # Другой файл с тем же именем — сохраняем с уникальным суффиксом, не перезаписывая
                    dst = _non_colliding_path(dst)
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                try:
                    with zf.open(m) as src, open(dst, "wb") as f:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                    extracted.append((os.path.basename(m.filename), m.file_size))
                    total_bytes += m.file_size
                except Exception as e:
                    log.warning(f"w{worker_id} extract {m.filename}: {e}")
    except zipfile.BadZipFile as e:
        log.error(f"w{worker_id} b{batch_idx} bad zip: {e}")
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

    1. Точное совпадение по (имя файла, размер).
    2. Совпадение по базовому имени (вариации вроде 'photo (1).jpg') + расширению + размеру.
    3. Фоллбэк только для одинакового расширения и размера (не путаем разные форматы).
    """
    exact_pool: dict[tuple[str, int], int] = defaultdict(int)
    for name, size in extracted:
        key = (os.path.basename(name).lower(), int(size or 0))
        exact_pool[key] += 1

    done: list[str] = []
    unmatched: list[dict] = []

    # 1. Точное совпадение
    for item in batch:
        key = (os.path.basename(item.get("name") or "").lower(),
               int(item.get("size") or 0))
        if exact_pool[key] > 0:
            exact_pool[key] -= 1
            done.append(item["file_id"])
        else:
            unmatched.append(item)

    if not unmatched:
        return done, []

    # Остаток записей архива
    leftover_items: list[tuple[str, int]] = []
    for (name, size), count in exact_pool.items():
        for _ in range(count):
            leftover_items.append((name, size))

    still_unmatched: list[dict] = []

    # 2. Совпадение по нормализованному стэму + расширению + размеру (a (1).jpg -> a.jpg)
    leftover_stems: dict[tuple[str, str, int], int] = defaultdict(int)
    for name, size in leftover_items:
        clean, ext = _clean_stem(name)
        leftover_stems[(clean, ext, size)] += 1

    for item in unmatched:
        clean, ext = _clean_stem(item.get("name") or "")
        size = int(item.get("size") or 0)
        key = (clean, ext, size)
        if leftover_stems[key] > 0:
            leftover_stems[key] -= 1
            for idx, (lname, lsize) in enumerate(leftover_items):
                if lsize == size and _clean_stem(lname) == (clean, ext):
                    leftover_items.pop(idx)
                    break
            done.append(item["file_id"])
        else:
            still_unmatched.append(item)

    if not still_unmatched:
        return done, []

    # 3. Фоллбэк: только если расширение и размер совпадают
    leftover_exts: dict[tuple[str, int], int] = defaultdict(int)
    for name, size in leftover_items:
        ext = os.path.splitext(name.lower())[1]
        leftover_exts[(ext, size)] += 1

    missing: list[str] = []
    for item in still_unmatched:
        ext = os.path.splitext((item.get("name") or "").lower())[1]
        size = int(item.get("size") or 0)
        key = (ext, size)
        if leftover_exts[key] > 0:
            leftover_exts[key] -= 1
            done.append(item["file_id"])
        else:
            missing.append(item["file_id"])

    return done, missing


def main():
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, required=True, help="ID воркера (1, 2, ...)")
    ap.add_argument("--batch", type=int, default=BATCH_SIZE)
    args = ap.parse_args()

    worker_id = args.id
    print("=" * 65)
    print(f"  Yandex.Disk Unlim Worker #{worker_id} — by Davron (PXSYS)")
    print(f"  Telegram: {TELEGRAM_CHANNEL} (новости о статусе серверов)")
    print(f"  batch={args.batch}")
    print("=" * 65)

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
            print(f"[w{worker_id}] batch {batch_idx}: ⚠️ Сервер Яндекса не собрал архив (перегрузка очереди Яндекса) → возвращаю в очередь")
            print(f"             (Скрипт в порядке. Статус серверов Яндекса: {TELEGRAM_CHANNEL})")
            try:
                sk, uid = get_sk_uid(s)
            except Exception:
                pass
            if len(batch) > 1:
                mark_pending(batch_ids, "сервер Яндекса не успел подготовить архив (пачка)")
            else:
                mark_pending(batch_ids, "сервер Яндекса не подготовил архив")
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
            mbps = (bytes_ / 1e6) / max(0.1, dur)
            tail = f", не приехало {len(missing_ids)}" if missing_ids else ""
            print(f"[w{worker_id}] batch {batch_idx}: ✓ {len(done_ids)}/{len(batch)} files / "
                  f"{bytes_/1e9:.2f} GB ({mbps:.1f} MB/s, {int(dur)}s{tail})")
        else:
            # Ни один файл не приехал (сбой скачивания/распаковки архива)
            if len(batch) > 1:
                print(f"[w{worker_id}] batch {batch_idx}: ⚠️ Сбой отдачи архива узлом Яндекса ({len(batch)} файлов) → возвращаю в очередь для разделения")
                mark_pending(batch_ids, "сбой серверов Яндекса при отдаче архива (пачка)")
            else:
                # Одиночный файл реально не качается — инкрементируем attempts
                att = (batch[0].get("attempts", 0) or 0) + 1
                print(f"[w{worker_id}] batch {batch_idx}: ⚠️ Файл не отдаётся сервером Яндекса → pending (попытка {att}/{MAX_ATTEMPTS})")
                print(f"             (Если сервер Яндекса штормит — статус в канале: {TELEGRAM_CHANNEL})")
                mark_pending(batch_ids, "файл не отдаётся сервером Яндекса")
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
