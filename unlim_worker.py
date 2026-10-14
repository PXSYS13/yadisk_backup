"""Параллельный воркер скачивания безлимита (photoslice).

Берёт pending файлы пачками и складывает их в DOWNLOAD_DIR/_unlim/. Два режима:
  direct (по умолчанию) — каждый файл пачки по прямой ссылке на оригинал
           (mpfs/url), как кнопка «Скачать» в веб-версии. Работает, пока Яндекс
           держит отключённым массовое скачивание (с октября 2026).
  zip    — Яндекс собирает ZIP из всей пачки (mpfs/bulk-download-prepare),
           воркер скачивает и распаковывает его. Быстрее на мелких файлах,
           но работает, только когда Яндекс отдаёт архивы.

Можно запускать несколько процессов параллельно — каждый атомарно
забирает свою пачку из БД и не мешает другим.

Запуск:
  python unlim_worker.py --id 1
  python unlim_worker.py --id 2 --mode zip

Коды выхода:
  0 — очередь пуста, работа закончена
  2 — куки протухли (auth_expired), нужен новый cookies.json
  3 — Яндекс больше часа не отдаёт файлы (bulk_unavailable и др.), повторить позже
  4 — на локальном диске нет места
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import NamedTuple

import requests

from storage_fetch import FetchFailure, fetch_to_file

from unlim_common import (
    BASE,
    COOKIES_FILE,
    DOWNLOAD_DIR,
    LOG_DIR,
    UNLIM_DB_FILE,
    AuthExpiredError,
    connect_unlim_db,
    ensure_attempts_column as common_ensure_attempts,
    get_sk_uid,
    make_cid,
    make_session as common_make_session,
    reset_failed_files,
    response_trace,
    unlim_tx,
)
from utils import (
    APP_VERSION,
    DEFAULT_UNLIM_BATCH,
    TELEGRAM_CHANNEL,
    ensure_utf8_stdio,
    human_bytes,
    is_local_disk_error,
    remove_quiet,
    sanitize_component,
)

# Отдельное имя модуля — тесты подменяют его через monkeypatch
DB_FILE = UNLIM_DB_FILE
LOG_FILE = os.path.join(LOG_DIR, "unlim_worker.log")

EXTRACT_DIR = os.path.join(DOWNLOAD_DIR, "_unlim")
# Временные файлы: ZIP пачек и недокачанные .part режима direct
ZIP_TMP_DIR = os.path.join(DOWNLOAD_DIR, "_unlim_tmp_zips")
MODES = ("direct", "zip")
DEFAULT_MODE = "direct"
BATCH_SIZE = DEFAULT_UNLIM_BATCH
MAX_BATCH_BYTES = 250 * 1024 * 1024  # 250 MB (быстрая подготовка архива без зависаний CDN)
LARGE_FILE_THRESHOLD = 150 * 1024 * 1024  # 150 MB (тяжёлые файлы качаем соло)
MIN_FREE_BYTES = 500 * 1024 * 1024

MAX_ATTEMPTS = 3  # после стольких неудач файл уходит в 'failed', чтобы не крутиться вечно
# Одиночный файл, который сервер раз за разом не отдаёт «по своей вине» (50x/таймаут),
# попытку не сжигает. Но после стольких подряд таких сбоев — сжигаем, иначе последний
# битый файл крутится в очереди бесконечно.
SOLO_SERVER_FAIL_STREAK = 5
# Глобальный предохранитель: столько пачек подряд без единого файла (любая причина) —
# значит, Яндекс не отдаёт архивы вообще. Дальше попытки файлов не сжигаем, ждём
# растущими паузами и через OUTAGE_GIVE_UP_SEC без успеха выходим с кодом 3.
OUTAGE_STREAK_THRESHOLD = 5
# ...и при этом сбои должны затрагивать разные файлы: один и тот же битый файл —
# это не отказ Яндекса, а битый файл (им занимается SOLO_SERVER_FAIL_STREAK)
OUTAGE_MIN_DISTINCT_FILES = 3
OUTAGE_PAUSES_SEC = (30, 60, 120, 300, 600, 900)
OUTAGE_GIVE_UP_SEC = 3600
# 429 от Яндекса: пауза растёт 45 → 90 → 180 … до 10 минут и действует на ВСЕ воркеры
RATE_LIMIT_BASE_PAUSE_SEC = 45
RATE_LIMIT_MAX_PAUSE_SEC = 600
# Сервер хранения, не ответивший за STORAGE_READ_TIMEOUT_SEC, считается недоступным
# столько секунд (для всех воркеров); потом его файлы пробуются снова.
# Живые серверы отвечают меньше чем за секунду, молчащие — не отвечают никогда
STORAGE_READ_TIMEOUT_SEC = 15
DEAD_HOST_TTL_SEC = 1800
# Пачка, где все файлы отложены из-за недоступных серверов, — пауза растёт до этой
DEFERRED_MAX_PAUSE_SEC = 300
# Столько подряд проверок «нет места на диске» — выходим с кодом 4
DISK_FULL_GIVE_UP = 10
# Сколько подряд пустых выборок из очереди — и воркер завершает работу
EMPTY_POLLS_BEFORE_EXIT = 3
# Живой воркер раз в минуту продлевает assigned_at своей пачки (heartbeat),
# поэтому зависшей считается пачка без признаков жизни дольше STUCK_TIMEOUT_SEC
HEARTBEAT_EVERY_SEC = 60
STUCK_TIMEOUT_SEC = 900

EXIT_AUTH = 2
EXIT_OUTAGE = 3
EXIT_DISK_FULL = 4

log = logging.getLogger("unlim_worker")


def _setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")


# ── БД ──────────────────────────────────────────────────────────────────────


def _tx():
    """Транзакция над текущей DB_FILE (модульная переменная — её подменяют тесты)."""
    return unlim_tx(DB_FILE)


def ensure_attempts_column() -> None:
    """Миграция для старых БД: колонка attempts могла отсутствовать."""
    common_ensure_attempts(DB_FILE)


def check_disk_space(target_dir: str, min_bytes: int = MIN_FREE_BYTES) -> tuple[bool, int]:
    """Проверяет наличие свободного места на целевом диске."""
    try:
        usage = shutil.disk_usage(target_dir)
        return usage.free >= min_bytes, usage.free
    except OSError:
        return True, -1


def take_batch(worker_id: int, size: int = BATCH_SIZE, max_bytes: int = MAX_BATCH_BYTES) -> list[dict]:
    """Атомарно забирает пачку pending с контролем как количества, так и суммарного веса.

    Если первый файл уже тяжёлый (>= LARGE_FILE_THRESHOLD) или имел сбои (attempts > 0) —
    забирает ровно 1 файл, изолируя проблемные файлы в индивидуальные загрузки.
    """
    with _tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(unlim_files)")}
        has_attempts = "attempts" in cols
        has_assigned = "assigned_at" in cols

        att_col = "COALESCE(attempts, 0) AS attempts" if has_attempts else "0 AS attempts"
        md5_col = "md5" if "md5" in cols else "NULL AS md5"
        order_parts = []
        if has_attempts:
            order_parts.append("attempts ASC")
        if has_assigned:
            order_parts.append("assigned_at ASC NULLS FIRST")
        order_parts.append("rowid ASC")

        rows = list(c.execute(
            f"SELECT file_id, name, size, {md5_col}, {att_col} "
            f"FROM unlim_files WHERE status='pending' "
            f"ORDER BY {', '.join(order_parts)} "
            f"LIMIT ?",
            (max(size * 2, 40),),
        ))

        items: list[dict] = []
        total_size = 0
        for r in rows:
            file_size = int(r["size"] or 0)
            attempts = int(r["attempts"] or 0)
            item = {"file_id": r["file_id"], "name": r["name"], "size": file_size,
                    "md5": r["md5"], "attempts": attempts}
            # Если первый же файл тяжёлый или уже имел сбои — качаем соло (1 файл)
            if not items and (file_size >= LARGE_FILE_THRESHOLD or attempts > 0):
                items.append(item)
                break
            # Если добавление превысит лимит размера или наткнулись на проблемный файл — останавливаемся
            if items and (total_size + file_size > max_bytes or len(items) >= size or attempts > 0):
                break
            items.append(item)
            total_size += file_size

        if items:
            c.executemany(
                "UPDATE unlim_files SET status='in_progress', worker_id=?, "
                "assigned_at=CURRENT_TIMESTAMP WHERE file_id=?",
                [(worker_id, it["file_id"]) for it in items],
            )
        return items


def _owner_clause(worker_id: int | None) -> tuple[str, tuple]:
    """Ограничение «только своя пачка»: чужой или уже сброшенный recover_stuck файл не трогаем."""
    if worker_id is None:
        return "", ()
    return " AND worker_id=?", (worker_id,)


def _release_to_pending(file_ids: list[str], worker_id: int | None = None) -> None:
    """Возвращает файлы в очередь без сжигания попытки — пачка не собралась, файлы не виноваты.

    Трогает только in_progress: скачанный другим воркером файл никогда не станет pending.
    """
    if not file_ids:
        return
    clause, extra = _owner_clause(worker_id)
    with _tx() as c:
        c.executemany(
            "UPDATE unlim_files SET status='pending', worker_id=NULL "
            f"WHERE file_id=? AND status='in_progress'{clause}",
            [(i, *extra) for i in file_ids],
        )


def mark_done(file_ids: list[str], paths: dict[str, str] | None = None) -> None:
    """Помечает файлы скачанными; paths — file_id → локальный путь (если известен)."""
    if not file_ids:
        return
    paths = paths or {}
    with _tx() as c:
        c.executemany(
            "UPDATE unlim_files SET status='downloaded', local_path=COALESCE(?, local_path), "
            "downloaded_at=CURRENT_TIMESTAMP, worker_id=NULL, error=NULL WHERE file_id=?",
            [(paths.get(i), i) for i in file_ids],
        )


def mark_pending(file_ids: list[str], error: str, worker_id: int | None = None) -> None:
    """Возвращает файлы в очередь, сжигая попытку. После MAX_ATTEMPTS — в 'failed',
    иначе воркер будет вечно крутить одну и ту же битую пачку.
    """
    if not file_ids:
        return
    clause, extra = _owner_clause(worker_id)
    with _tx() as c:
        c.executemany(
            "UPDATE unlim_files SET "
            "  attempts = COALESCE(attempts, 0) + 1, "
            "  status = CASE WHEN COALESCE(attempts, 0) + 1 >= ? THEN 'failed' ELSE 'pending' END, "
            "  worker_id = NULL, error = ?, "
            "  assigned_at = CURRENT_TIMESTAMP "
            f"WHERE file_id = ?{clause}",
            [(MAX_ATTEMPTS, error[:500], i, *extra) for i in file_ids],
        )


def heartbeat(file_ids: list[str], worker_id: int) -> None:
    """Продлевает assigned_at своей пачки — признак, что воркер жив и качает."""
    if not file_ids:
        return
    with _tx() as c:
        c.executemany(
            "UPDATE unlim_files SET assigned_at=CURRENT_TIMESTAMP "
            "WHERE file_id=? AND worker_id=? AND status='in_progress'",
            [(i, worker_id) for i in file_ids],
        )


def recover_stuck(worker_id: int | None = None, timeout_sec: int = STUCK_TIMEOUT_SEC) -> int:
    """Возвращает в pending файлы которые в in_progress.
    Если указан worker_id — сразу сбрасывает файлы этого воркера (от прошлого запуска).
    Также сбрасывает любые зависшие в in_progress без heartbeat дольше timeout_sec.
    """
    with _tx() as c:
        recovered = 0
        if worker_id is not None:
            recovered += c.execute(
                "UPDATE unlim_files SET status='pending', worker_id=NULL "
                "WHERE status='in_progress' AND worker_id=?",
                (worker_id,),
            ).rowcount
        recovered += c.execute(
            "UPDATE unlim_files SET status='pending', worker_id=NULL "
            "WHERE status='in_progress' AND "
            "(julianday(CURRENT_TIMESTAMP) - julianday(assigned_at)) * 86400 > ?",
            (timeout_sec,),
        ).rowcount
        return recovered


def reset_failed() -> int:
    """Сбрасывает все 'failed' файлы и попытки обратно в 'pending'."""
    return reset_failed_files(DB_FILE)


# ── Общая пауза по лимиту запросов (429) ────────────────────────────────────
# Воркеры — отдельные процессы. Если пауза только у того, кто получил 429,
# остальные продолжают долбить Яндекс и тоже получают 429. Поэтому момент
# окончания паузы хранится в БД и его соблюдают все.


def _ensure_meta(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS unlim_meta (key TEXT PRIMARY KEY, value TEXT)")


def set_shared_cooldown(seconds: float) -> float:
    """Продлевает общую паузу минимум на seconds. Возвращает момент её окончания (epoch)."""
    until = time.time() + seconds
    with _tx() as c:
        _ensure_meta(c)
        row = c.execute("SELECT value FROM unlim_meta WHERE key='cooldown_until'").fetchone()
        current = float(row[0]) if row else 0.0
        if until > current:
            c.execute(
                "INSERT INTO unlim_meta (key, value) VALUES ('cooldown_until', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (repr(until),),
            )
        return max(until, current)


def shared_cooldown_remaining() -> float:
    """Сколько секунд ещё действует общая пауза (0 — паузы нет)."""
    c = connect_unlim_db(DB_FILE)
    try:
        row = c.execute("SELECT value FROM unlim_meta WHERE key='cooldown_until'").fetchone()
    except sqlite3.OperationalError:  # старая БД без таблицы — паузы нет
        return 0.0
    finally:
        c.close()
    return max(0.0, float(row[0]) - time.time()) if row else 0.0


# ── Недоступные серверы хранения ────────────────────────────────────────────
# Файл лежит на конкретном сервере хранения (s123klg.storage.yandex.net). После атак
# на дата-центры часть таких серверов молчит: и REST API, и веб-версия ждут ответа
# вечно. Сервер, который однажды не ответил, на DEAD_HOST_TTL_SEC считается
# недоступным для всех воркеров: его файлы откладываются сразу, без ожидания таймаута.


def mark_host_dead(host: str, ttl: float = DEAD_HOST_TTL_SEC) -> None:
    with _tx() as c:
        _ensure_meta(c)
        c.execute(
            "INSERT INTO unlim_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"dead_host:{host}", repr(time.time() + ttl)),
        )


def is_host_dead(host: str) -> bool:
    c = connect_unlim_db(DB_FILE)
    try:
        row = c.execute("SELECT value FROM unlim_meta WHERE key=?", (f"dead_host:{host}",)).fetchone()
    except sqlite3.OperationalError:  # старая БД без таблицы
        return False
    finally:
        c.close()
    return bool(row) and float(row[0]) > time.time()


def queue_summary() -> str:
    """Состояние очереди одной строкой — для старта воркера и логов."""
    try:
        with _tx() as c:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM unlim_files GROUP BY status"
            ).fetchall()
    except Exception as e:  # noqa: BLE001 — сводка не должна мешать работе
        return f"очередь: нет данных ({e})"
    parts = [f"{r['status']}={r['n']} ({human_bytes(r['b'])})" for r in rows]
    return "очередь: " + (", ".join(parts) if parts else "пусто")


# ── HTTP: подготовка ZIP ────────────────────────────────────────────────────


def make_session(cookies: dict[str, str] | None = None) -> requests.Session:
    return common_make_session(cookies=cookies, cookies_file=COOKIES_FILE)


@dataclass(frozen=True)
class PrepareError:
    """Почему Яндекс не собрал ZIP.

    kind — машинная категория (auth_expired | rate_limit | server_50x | timeout |
    connection | invalid_json | prepare_failed | refused), text — объяснение для человека.
    str() даёт 'категория | текст': по этому префиксу диагностика Web UI
    распознаёт проблему в логах воркера.
    """

    kind: str
    text: str
    status: int | None = None
    retry_after: float | None = None  # заголовок Retry-After, если Яндекс его прислал

    def __str__(self) -> str:
        return f"{self.kind} | {self.text}"

    @property
    def is_auth(self) -> bool:
        return self.kind == "auth_expired"

    @property
    def is_server_issue(self) -> bool:
        """Сбой на стороне Яндекса или сети — файл не виноват, попытку не сжигаем."""
        return self.kind in ("rate_limit", "server_50x", "timeout", "connection", "refused")

    @property
    def retry_delay(self) -> float:
        """Пауза перед следующей пачкой, сек."""
        if self.kind == "rate_limit":
            return max(RATE_LIMIT_BASE_PAUSE_SEC, self.retry_after or 0)
        if self.kind == "timeout" or self.status == 504:
            return 15
        if self.kind in ("server_50x", "refused"):
            return 10
        return 5


def _retry_after_sec(r) -> float | None:
    """Retry-After в секундах, если Яндекс его прислал числом."""
    headers = getattr(r, "headers", None) or {}
    try:
        value = float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _prepare_http_error(r, op: str = "prepare") -> PrepareError:
    """Не-200 ответ models-v2 → PrepareError; op — что просили (для лога)."""
    code = r.status_code
    if code in (401, 403):
        err = PrepareError(
            "auth_expired",
            f"HTTP {code} Unauthorized/Forbidden: авторизация отклонена Яндексом (куки устарели)",
            code,
        )
        log.error(f"{op} {err}")
        return err
    trace = response_trace(r)
    if code == 429:
        retry_after = _retry_after_sec(r)
        hint = f", просит подождать {int(retry_after)} с" if retry_after else ""
        err = PrepareError(
            "rate_limit",
            f"HTTP 429 Too Many Requests: Яндекс просит реже обращаться{hint}{trace}. Ответ: {r.text[:200]}",
            code, retry_after,
        )
    elif code == 504:
        err = PrepareError("server_50x", f"HTTP 504 Gateway Timeout (сервер Яндекса не успел ответить){trace}", code)
    elif code in (500, 502, 503):
        err = PrepareError("server_50x", f"HTTP {code} (сбой или перегрузка серверов Яндекса): {r.text[:200]}{trace}", code)
    else:
        err = PrepareError("prepare_failed", f"HTTP {code}: {r.text[:200]}{trace}", code)
    log.warning(f"{op} {err}")
    return err


def get_file_url(s, sk, uid, path: str) -> tuple[str | None, PrepareError | None]:
    """Прямая ссылка на оригинал одного файла (mpfs/url) — так качает кнопка «Скачать»
    в веб-версии. Массовое скачивание Яндекс отключил, одиночное — нет.

    Возвращает (ссылка, None) или (None, ошибка). Ссылка живёт недолго — качать сразу.
    """
    body = {"sk": sk,
            "connection_id": make_cid(uid),
            "apiMethod": "mpfs/url",
            "requestParams": {"path": path}}
    try:
        r = s.post(f"{BASE}/models-v2", params={"m": "mpfs/url"}, json=body, timeout=(30, 60))
    except requests.exceptions.Timeout:
        err = PrepareError("timeout", "Яндекс не выдал ссылку на файл за 60 с (ReadTimeout)")
        log.warning(f"url exc: {err}")
        return None, err
    except requests.exceptions.ConnectionError:
        err = PrepareError("connection", "Разрыв соединения со стороны серверов Яндекса (ConnectionError)")
        log.warning(f"url exc: {err}")
        return None, err
    except Exception as e:
        err = PrepareError("connection", f"Сбой связи с сервером Яндекса: {e}")
        log.warning(f"url exc: {err}")
        return None, err

    trace = response_trace(r)
    # На частые запросы Яндекс отвечает капчей — это лимит, а не протухшие куки
    if "captcha" in str(getattr(r, "text", "") or "").lower()[:2000]:
        err = PrepareError("rate_limit", f"Яндекс показал капчу: слишком частые запросы (HTTP {r.status_code}){trace}",
                           r.status_code, _retry_after_sec(r))
        log.warning(f"url {err}")
        return None, err
    if r.status_code != 200:
        return None, _prepare_http_error(r, "url")

    try:
        data = r.json()
    except Exception:
        data = None
    if data is None:
        err = PrepareError("invalid_json", f"Ответ Яндекса не JSON ({r.text[:200]}){trace}")
    elif not isinstance(data, dict):
        err = PrepareError("prepare_failed", f"Неожиданный формат ответа Яндекса: {type(data).__name__}{trace}")
    elif "error" in data:
        e = data.get("error")
        e_str = (e.get("title") or e.get("id") or str(e)) if isinstance(e, dict) else str(e)
        err = PrepareError("prepare_failed", f"Яндекс не выдал ссылку на файл: {e_str} | ответ: {str(e)[:200]}{trace}")
    elif not isinstance(data.get("file"), str) or not data["file"]:
        err = PrepareError("prepare_failed", f"В ответе Яндекса нет ссылки на файл | ответ: {str(data)[:200]}{trace}")
    else:
        url = data["file"]
        if url.startswith("//"):
            url = "https:" + url
        return url, None
    log.warning(f"url {err}")
    return None, err


def prepare_zip(s, sk, uid, paths: list[str]) -> tuple[str | None, PrepareError | None]:
    """Просит Яндекс собрать ZIP из файлов. Возвращает (download_url, None) или (None, ошибка)."""
    body = {"sk": sk,
            "connection_id": make_cid(uid),
            "apiMethod": "mpfs/bulk-download-prepare",
            "requestParams": {"items": paths}}
    try:
        r = s.post(f"{BASE}/models-v2",
                   params={"m": "mpfs/bulk-download-prepare"},
                   json=body, timeout=(30, 90))
    except requests.exceptions.Timeout:
        err = PrepareError("timeout", "Превышено время ожидания ответа от сервера Яндекса (ReadTimeout > 90с)")
        log.warning(f"prepare exc: {err}")
        return None, err
    except requests.exceptions.ConnectionError:
        err = PrepareError("connection", "Разрыв соединения со стороны серверов Яндекса (ConnectionError)")
        log.warning(f"prepare exc: {err}")
        return None, err
    except Exception as e:
        err = PrepareError("connection", f"Сбой связи с сервером Яндекса: {e}")
        log.warning(f"prepare exc: {err}")
        return None, err

    if r.status_code != 200:
        return None, _prepare_http_error(r)

    try:
        data = r.json()
    except Exception:
        data = None
    trace = response_trace(r)
    if data is None:
        err = PrepareError("invalid_json", f"Ответ Яндекса не JSON ({r.text[:200]}){trace}")
    elif not isinstance(data, dict):
        err = PrepareError("prepare_failed", f"Неожиданный формат ответа Яндекса: {type(data).__name__}{trace}")
    elif "error" in data:
        e = data.get("error")
        e_str = (e.get("title") or e.get("id") or str(e)) if isinstance(e, dict) else str(e)
        err = PrepareError("prepare_failed", f"Ошибка сервиса Яндекса: {e_str} | ответ: {str(e)[:200]}{trace}")
    elif not data.get("download_url"):
        err = PrepareError("prepare_failed", f"В ответе Яндекса отсутствует download_url | ответ: {str(data)[:200]}{trace}")
    else:
        url = data["download_url"]
        if url.startswith("//"):
            url = "https:" + url
        log.info("prepare OK → %s%s", url.split("?")[0][:120], trace)
        return url, None
    log.warning(str(err))
    return None, err


# ── Скачивание и распаковка ─────────────────────────────────────────────────


def safe_extract_dst(base_dir: str, member_name: str) -> str | None:
    """Куда безопасно распаковать запись архива.

    Отбрасывает всё, что уводит за пределы base_dir: '..', ведущие слэши,
    букву диска ('C:/evil.txt' — os.path.join такой путь просто заменил бы базу).
    Каждый компонент чистится от символов, запрещённых на Windows (':' в именах
    вроде '2024-05-01 12:30:00.jpg' — иначе файл не распакуется никогда).
    Возвращает None, если запись выглядит враждебно или пустой.
    """
    raw = member_name.replace("\\", "/")
    parts = []
    for part in raw.split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        if re.fullmatch(r"[A-Za-z]:", part):  # 'C:' — буква диска, не имя
            continue
        parts.append(sanitize_component(part))
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
    """'photo (1).jpg' → ('photo', '.jpg'): Яндекс дописывает суффиксы при коллизиях имён в ZIP."""
    base = os.path.basename(name).lower()
    stem, ext = os.path.splitext(base)
    clean = re.sub(r"[\s_]*\(\d+\)$|_+\d+$", "", stem).strip()
    return clean, ext


class ExtractedItem(NamedTuple):
    """Запись архива, которую удалось распаковать."""

    name: str
    size: int
    dst: str = ""


class FetchResult(NamedTuple):
    extracted: list[ExtractedItem]
    total_bytes: int
    failure: FetchFailure | None = None


_NOT_ZIP_CONTENT_TYPES = ("text/html", "application/json", "text/plain")


def _download_zip(
    s, url: str, zip_path: str, worker_id: int, batch_idx: int,
    on_progress: Callable[[], None] | None = None,
) -> FetchFailure | None:
    """Стримит ZIP во временный .part и атомарно переименовывает. None — успех."""
    tmp = zip_path + ".part"
    t0 = time.monotonic()
    try:
        with s.get(url, stream=True, timeout=(30, 300)) as r:
            r.raise_for_status()
            headers = getattr(r, "headers", {}) or {}
            trace = response_trace(r)
            ctype = (headers.get("Content-Type") or "").lower()
            if any(t in ctype for t in _NOT_ZIP_CONTENT_TYPES):
                # Яндекс отдал страницу/JSON вместо архива: так выглядит «функция отключена»
                snippet = next(iter(r.iter_content(512)), b"")[:200]
                return FetchFailure(
                    "not_zip",
                    f"вместо ZIP пришёл {ctype.split(';')[0]}: {snippet.decode('utf-8', 'replace')!r}{trace}",
                )
            content_len = int(headers.get("Content-Length", 0) or 0)
            log.info("w%d b%d: архив %s%s, качаю%s", worker_id, batch_idx,
                     ctype.split(";")[0] or "без типа",
                     f" {human_bytes(content_len)}" if content_len else "", trace)
            written = 0
            last_log = time.monotonic()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if now - last_log >= 6.0:
                        last_log = now
                        total_str = f" / {human_bytes(content_len)}" if content_len > 0 else ""
                        print(f"[w{worker_id}] b{batch_idx}: качаю архив {human_bytes(written)}{total_str}...", flush=True)
                        if on_progress:
                            on_progress()
        os.replace(tmp, zip_path)
        if not zipfile.is_zipfile(zip_path):
            with open(zip_path, "rb") as f:
                head = f.read(200)
            remove_quiet(zip_path)
            return FetchFailure("not_zip", f"скачанный файл не ZIP (первые байты: {head!r})")
        dur = max(0.001, time.monotonic() - t0)
        log.info("w%d b%d: архив скачан %s за %.1f с (%s/с)", worker_id, batch_idx,
                 human_bytes(written), dur, human_bytes(written / dur))
        return None
    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        code = getattr(resp, "status_code", None)
        trace = response_trace(resp) if resp is not None else ""
        if code == 429:
            failure = FetchFailure("rate_limit", f"HTTP 429 при скачивании архива: {e}{trace}")
        elif code is not None and code >= 500:
            failure = FetchFailure("server_50x", f"HTTP {code} при скачивании архива: {e}{trace}")
        else:
            failure = FetchFailure("refused", f"HTTP {code} при скачивании архива: {e}{trace}")
    except requests.exceptions.Timeout as e:
        failure = FetchFailure("timeout", f"таймаут при скачивании архива: {e}")
    except requests.exceptions.ConnectionError as e:
        failure = FetchFailure("connection", f"обрыв соединения при скачивании архива: {e}")
    except OSError as e:
        kind = "local_disk" if is_local_disk_error(e) else "connection"
        failure = FetchFailure(kind, f"ошибка записи архива: {e}")
    except Exception as e:
        failure = FetchFailure("connection", f"сбой скачивания архива: {e}")
    log.error(f"w{worker_id} b{batch_idx} download: {failure}")
    remove_quiet(tmp)
    return failure


def _extract_zip(zip_path: str, worker_id: int, batch_idx: int) -> FetchResult:
    """Распаковывает архив в EXTRACT_DIR."""
    extracted: list[ExtractedItem] = []
    total_bytes = 0
    reused = 0
    errors = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            for m in members:
                dst = safe_extract_dst(EXTRACT_DIR, m.filename)
                if dst is None:
                    log.warning(f"w{worker_id} пропускаю опасное имя в архиве: {m.filename!r}")
                    errors += 1
                    continue
                try:
                    if os.path.exists(dst):
                        if os.path.getsize(dst) == m.file_size:
                            extracted.append(ExtractedItem(os.path.basename(m.filename), m.file_size, dst))
                            total_bytes += m.file_size
                            reused += 1
                            continue
                        # Другой файл с тем же именем — сохраняем с уникальным суффиксом, не перезаписывая
                        dst = _non_colliding_path(dst)
                    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                    with zf.open(m) as src, open(dst, "wb") as f:
                        shutil.copyfileobj(src, f, 1024 * 1024)
                    extracted.append(ExtractedItem(os.path.basename(m.filename), m.file_size, dst))
                    total_bytes += m.file_size
                except OSError as e:
                    if is_local_disk_error(e):
                        # Место/права — дальше распаковывать бессмысленно, и файлы не виноваты
                        log.error(f"w{worker_id} b{batch_idx} extract {m.filename}: {e}")
                        return FetchResult(extracted, total_bytes,
                                           FetchFailure("local_disk", f"ошибка записи при распаковке: {e}"))
                    errors += 1
                    log.warning(f"w{worker_id} extract {m.filename}: {e}")
                except Exception as e:
                    errors += 1
                    log.warning(f"w{worker_id} extract {m.filename}: {e}")
    except zipfile.BadZipFile as e:
        log.error(f"w{worker_id} b{batch_idx} bad zip: {e}")
        return FetchResult([], 0, FetchFailure("bad_zip", f"архив повреждён: {e}"))
    log.info("w%d b%d: в архиве %d файлов, распаковано %d (%s), уже были %d, ошибок %d",
             worker_id, batch_idx, len(members), len(extracted) - reused,
             human_bytes(total_bytes), reused, errors)
    return FetchResult(extracted, total_bytes, None)


def download_and_extract(
    s, url: str, worker_id: int, batch_idx: int,
    on_progress: Callable[[], None] | None = None,
) -> FetchResult:
    """Качает ZIP пачки и распаковывает.

    Возвращает FetchResult(распакованные ExtractedItem(имя, размер, dst), суммарные байты, причина сбоя).
    """
    os.makedirs(ZIP_TMP_DIR, exist_ok=True)
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    zip_path = os.path.join(ZIP_TMP_DIR, f"w{worker_id}_b{batch_idx}.zip")
    failure = _download_zip(s, url, zip_path, worker_id, batch_idx, on_progress)
    if failure is not None:
        return FetchResult([], 0, failure)
    try:
        return _extract_zip(zip_path, worker_id, batch_idx)
    finally:
        remove_quiet(zip_path)


def download_file(
    s, url: str, dst: str, expected_size: int = 0, worker_id: int = 0,
    on_progress: Callable[[], None] | None = None,
    expected_md5: str | None = None,
    host_is_dead: Callable[[str], bool] | None = None,
) -> tuple[str, FetchFailure | None]:
    """Стримит один файл во временный .part и переносит в dst (режим direct).

    Возвращает (итоговый путь, None) или ("", причина сбоя). Итоговый путь получает
    суффикс _1, если файл с таким именем уже лежит в папке: чужой файл не затираем.
    Редирект на сервер хранения и молчащие серверы разбирает storage_fetch.fetch_to_file.
    """
    os.makedirs(ZIP_TMP_DIR, exist_ok=True)
    tmp = os.path.join(ZIP_TMP_DIR, f"w{worker_id}_file.part")
    name = os.path.basename(dst)
    t0 = time.monotonic()
    last_log = time.monotonic()

    def progress(written: int, total: int) -> None:
        nonlocal last_log
        now = time.monotonic()
        if now - last_log >= 6.0:
            last_log = now
            total_str = f" / {human_bytes(total)}" if total > 0 else ""
            print(f"[w{worker_id}] качаю {name}: {human_bytes(written)}{total_str}...", flush=True)
            if on_progress:
                on_progress()

    try:
        written, _host, failure = fetch_to_file(
            s, url, tmp,
            expected_size=expected_size or None,
            expected_md5=expected_md5,
            host_is_dead=host_is_dead,
            storage_timeout=STORAGE_READ_TIMEOUT_SEC,
            on_progress=progress,
        )
        if failure is None:
            try:
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                final = _non_colliding_path(dst)
                shutil.move(tmp, final)
            except OSError as e:
                kind = "local_disk" if is_local_disk_error(e) else "connection"
                failure = FetchFailure(kind, f"ошибка записи файла: {e}")
            else:
                dur = max(0.001, time.monotonic() - t0)
                log.info("w%d: %s скачан %s за %.1f с (%s/с)", worker_id, name,
                         human_bytes(written), dur, human_bytes(written / dur))
                return final, None
    finally:
        remove_quiet(tmp)  # после успешного переноса его уже нет
    (log.info if failure.is_storage else log.error)(f"w{worker_id} {name}: {failure}")
    return "", failure


# ── Сопоставление «что просили» ↔ «что приехало» ────────────────────────────


class SplitResult(NamedTuple):
    done: list[str]        # file_id, которые реально приехали
    missing: list[str]     # file_id, которых в архиве не оказалось
    paths: dict[str, str]  # file_id → локальный путь распакованного файла


def _batch_name(item: dict) -> str:
    return os.path.basename(item.get("name") or "").lower()


def _batch_size(item: dict) -> int:
    return int(item.get("size") or 0)


def split_by_extracted(batch: list[dict], extracted: Iterable[tuple]) -> SplitResult:
    """Делит пачку на реально приехавшие и не приехавшие файлы.

    1. Точное совпадение по (имя файла, размер).
    2. Совпадение по базовому имени (вариации вроде 'photo (1).jpg') + расширению + размеру.
    3. Фоллбэк только для одинакового расширения и размера (не путаем разные форматы).

    extracted принимает кортежи (имя, размер) или (имя, размер, путь).
    """
    unused: list[ExtractedItem] = [ExtractedItem(*it) for it in extracted]
    done: list[str] = []
    paths: dict[str, str] = {}

    def stage(
        pending: list[dict],
        key_batch: Callable[[dict], tuple],
        key_item: Callable[[ExtractedItem], tuple],
    ) -> list[dict]:
        """Сопоставляет pending с ещё не использованными записями архива по ключу,
        с учётом кратности. Возвращает несопоставленные."""
        nonlocal unused
        buckets: dict[tuple, list[int]] = defaultdict(list)
        for idx, it in enumerate(unused):
            buckets[key_item(it)].append(idx)
        taken: set[int] = set()
        rest: list[dict] = []
        for item in pending:
            idxs = buckets.get(key_batch(item))
            if not idxs:
                rest.append(item)
                continue
            idx = idxs.pop(0)
            taken.add(idx)
            fid = str(item["file_id"])
            done.append(fid)
            if unused[idx].dst:
                paths[fid] = unused[idx].dst
        unused = [it for idx, it in enumerate(unused) if idx not in taken]
        return rest

    # Архив приезжает с санитизированными именами (см. safe_extract_dst) —
    # сравниваем в том же виде
    def norm_name(name: str) -> str:
        return sanitize_component(os.path.basename(name)).lower()

    # 1. Точное совпадение (имя, размер)
    pending = stage(
        batch,
        lambda b: (norm_name(b.get("name") or ""), _batch_size(b)),
        lambda it: (norm_name(it.name), int(it.size or 0)),
    )
    # 2. Нормализованный стэм: 'a (1).jpg' ↔ 'a.jpg'
    if pending:
        pending = stage(
            pending,
            lambda b: (*_clean_stem(norm_name(b.get("name") or "")), _batch_size(b)),
            lambda it: (*_clean_stem(norm_name(it.name)), int(it.size or 0)),
        )
    # 3. Фоллбэк: только расширение + размер
    if pending:
        pending = stage(
            pending,
            lambda b: (os.path.splitext(_batch_name(b))[1], _batch_size(b)),
            lambda it: (os.path.splitext(it.name.lower())[1], int(it.size or 0)),
        )

    return SplitResult(done, [str(b["file_id"]) for b in pending], paths)


# ── Цикл воркера ────────────────────────────────────────────────────────────


# Категория сбоя → как объяснить человеку серию одинаковых отказов
_OUTAGE_TEXT = {
    "rate_limit": ("rate_limit",
                   "Яндекс ограничивает частоту запросов (HTTP 429). Функция не отключена, просто нужно "
                   "обращаться реже. Если повторяется, оставьте 1–2 воркера."),
    "server": ("server_50x",
               "Серверы Яндекса сбоят или перегружены (ошибки 50x, таймауты, обрывы связи)."),
    "refused": ("bulk_unavailable",
                "Похоже, Яндекс ограничил скачивание на своей стороне."),
}


def _failure_cause(kind: str) -> str:
    if kind == "rate_limit":
        return "rate_limit"
    if kind in ("server_50x", "timeout", "connection"):
        return "server"
    return "refused"  # not_zip, refused, prepare_failed, invalid_json, bad_zip, missing


class Worker:
    """Один процесс-воркер: берёт пачки из очереди, пока очередь не опустеет."""

    def __init__(self, worker_id: int, batch_size: int = BATCH_SIZE, mode: str = DEFAULT_MODE):
        if mode not in MODES:
            raise ValueError(f"неизвестный режим {mode!r}, допустимы: {', '.join(MODES)}")
        self.id = worker_id
        self.mode = mode
        self.target_batch = max(1, batch_size)
        self.batch_size = self.target_batch  # адаптивно уменьшается при сбоях
        self.session: requests.Session | None = None
        self.sk = ""
        self.uid = ""
        self.batch_idx = 0
        self.total_files = 0
        self.total_bytes = 0
        # file_id → сколько раз подряд сервер не отдал этот одиночный файл «по своей вине»
        self._solo_streak: dict[str, int] = {}
        # Подряд идущие пачки без единого файла (любая причина) — предохранитель
        self._fail_streak = 0
        self._streak_files: set[str] = set()
        self._streak_causes: Counter[str] = Counter()
        self._rate_limit_streak = 0
        self._cooldown_said_at = 0.0
        self._last_success_at = time.monotonic()
        self._disk_full_streak = 0
        self._deferred_streak = 0  # пачки подряд, целиком отложенные (direct)
        self._last_heartbeat = 0.0
        self._current_ids: list[str] = []

    def say(self, msg: str) -> None:
        print(f"[w{self.id}] {msg}", flush=True)

    # ── авторизация ──

    def authorize(self) -> None:
        self.session = make_session()
        try:
            self.sk, self.uid = get_sk_uid(self.session)
        except AuthExpiredError as e:
            self.say(f"auth_expired | КРИТИЧЕСКАЯ ОШИБКА: {e}")
            self.say("Куки устарели. Обновите cookies.json в Web UI или браузере.")
            sys.exit(EXIT_AUTH)
        self.say(f"авторизован uid={self.uid}")

    def _refresh_auth(self) -> bool:
        """sk живёт недолго — после сбоя обновляем. True, если сессия по кукам жива."""
        try:
            self.sk, self.uid = get_sk_uid(self.session)
            return True
        except AuthExpiredError:
            return False
        except Exception:
            return True  # сеть/формат — не доказательство протухших кук

    # ── основной цикл ──

    def run(self) -> int:
        empty_polls = 0
        while True:
            wait = shared_cooldown_remaining()
            if wait > 0:
                if time.monotonic() - self._cooldown_said_at > 60:
                    self._cooldown_said_at = time.monotonic()
                    self.say(f"rate_limit | общая пауза по лимиту Яндекса (429): жду ещё {int(wait)} с")
                time.sleep(min(wait, 30))
                continue
            batch = take_batch(self.id, self.batch_size)
            if not batch:
                empty_polls += 1
                if empty_polls >= EMPTY_POLLS_BEFORE_EXIT:
                    self.say("нет работы, выход")
                    break
                time.sleep(5)
                continue
            empty_polls = 0
            self.batch_idx += 1
            self._process(batch)

        self._cleanup()
        self.say(f"ИТОГ: {self.total_files} files / {self.total_bytes / 1e9:.2f} GB")
        return 0

    def _process(self, batch: list[dict]) -> None:
        ids = [it["file_id"] for it in batch]
        self._current_ids = ids

        # Место проверяем ДО запроса к Яндексу — иначе просим собрать архив впустую
        if not self._has_disk_space(batch):
            _release_to_pending(ids, self.id)
            self._disk_full_streak += 1
            if self._disk_full_streak >= DISK_FULL_GIVE_UP:
                self.say(f"local_disk | Выход: на диске нет места ({DISK_FULL_GIVE_UP} проверок подряд). "
                         f"Освободите место и запустите снова.")
                self._exit(EXIT_DISK_FULL)
            time.sleep(30)
            return
        self._disk_full_streak = 0

        total = sum(_batch_size(it) for it in batch)
        max_att = max((int(it.get("attempts") or 0) for it in batch), default=0)
        log.info("w%d b%d: пачка %d файлов, %s, попыток у худшего %d", self.id, self.batch_idx,
                 len(batch), human_bytes(total), max_att)

        if self.mode == "direct":
            self._process_direct(batch)
        else:
            self._process_zip(batch)

    def _process_zip(self, batch: list[dict]) -> None:
        ids = [it["file_id"] for it in batch]
        t_prep = time.monotonic()
        url, err = prepare_zip(self.session, self.sk, self.uid, ids)
        if err is not None:
            self._on_prepare_failed(batch, err)
            return
        log.info("w%d b%d: Яндекс собрал архив за %.1f с", self.id, self.batch_idx, time.monotonic() - t_prep)

        start = time.monotonic()
        result = download_and_extract(self.session, url, self.id, self.batch_idx, on_progress=self._heartbeat)
        self._on_downloaded(batch, result, time.monotonic() - start)

    def _process_direct(self, batch: list[dict]) -> None:
        """Качает пачку по одному файлу через прямые ссылки (mpfs/url), без ZIP.

        Каждый файл помечается скачанным сразу — прерванная пачка не теряет готовое.
        После первого сбоя остаток пачки возвращается в очередь без сжигания попыток.
        """
        ids = [str(it["file_id"]) for it in batch]
        start = time.monotonic()
        files_before, bytes_before = self.total_files, self.total_bytes
        deferred = 0
        failed = False
        for idx, item in enumerate(batch):
            if idx and shared_cooldown_remaining() > 0:
                # Другой воркер поймал 429 — остаток вернём, общий цикл выждет паузу
                _release_to_pending(ids[idx:], self.id)
                break
            outcome = self._fetch_one(item, ids[idx + 1:])
            self._heartbeat()  # мелкие файлы качаются быстрее, чем срабатывает on_progress
            if outcome == "failed":
                failed = True
                break
            if outcome == "deferred":
                deferred += 1
        done = self.total_files - files_before
        done_bytes = self.total_bytes - bytes_before
        tail = f", отложено {deferred}: их серверы хранения не отвечают" if deferred else ""
        if done:
            self._deferred_streak = 0
            dur = time.monotonic() - start
            mbps = (done_bytes / 1e6) / max(0.1, dur)
            self.say(f"batch {self.batch_idx}: ✓ {done}/{len(batch)} files / "
                     f"{done_bytes / 1e9:.2f} GB ({mbps:.1f} MB/s, {int(dur)}s{tail})")
        elif deferred and not failed:
            self._on_all_deferred(deferred)

    def _fetch_one(self, item: dict, rest: list[str]) -> str:
        """Один файл режима direct: 'done' | 'deferred' | 'failed'.

        deferred — сервер хранения файла недоступен: файл вернулся в очередь без
        сжигания попытки, пачка продолжается. failed — сбой уже обработан, а rest
        (не начатый остаток пачки) возвращён в очередь до обработки: она может
        завершить процесс.
        """
        fid = str(item["file_id"])
        name = sanitize_component(os.path.basename(str(item.get("name") or fid)))
        dst = os.path.join(EXTRACT_DIR, name)
        expected = _batch_size(item)
        if expected > 0 and os.path.isfile(dst) and os.path.getsize(dst) == expected:
            # Уже лежит в папке (прошлый запуск или архив) — второй раз не качаем
            mark_done([fid], {fid: dst})
            self._on_file_done(fid, expected)
            return "done"

        url, err = get_file_url(self.session, self.sk, self.uid, fid)
        if err is not None:
            _release_to_pending(rest, self.id)
            self._on_prepare_failed([item], err, f"batch {self.batch_idx}: ⚠️ Яндекс не выдал ссылку на файл {name}")
            return "failed"

        path, failure = download_file(self.session, url, dst, expected, self.id, on_progress=self._heartbeat,
                                      expected_md5=item.get("md5"), host_is_dead=is_host_dead)
        if failure is not None and failure.kind in ("storage_down", "storage_skip"):
            _release_to_pending([fid], self.id)
            if failure.kind == "storage_down":
                mark_host_dead(failure.host)
                self.say(f"storage_down | Сервер хранения Яндекса {failure.host} не отвечает (файл {name}). "
                         f"Его файлы откладываю на {DEAD_HOST_TTL_SEC // 60} мин, попытки не сжигаю.")
            return "deferred"
        if failure is not None:
            _release_to_pending(rest, self.id)
            if failure.is_local:
                self._on_local_disk_failure([fid], failure)
            else:
                self.say(f"batch {self.batch_idx}: ⚠️ Файл {name} не скачался: {failure}")
                self._on_batch_failed([item], str(failure), failure.is_server_issue, 5, failure.kind)
            return "failed"

        size = os.path.getsize(path)
        mark_done([fid], {fid: path})
        self._on_file_done(fid, size)
        return "done"

    def _on_all_deferred(self, deferred: int) -> None:
        """Вся пачка — файлы на недоступных серверах хранения. Скорее всего, доступные
        файлы кончились: ждём растущими паузами, через час без успеха выходим с кодом 3."""
        self._deferred_streak += 1
        pause = min(DEFERRED_MAX_PAUSE_SEC, 30 * self._deferred_streak)
        without_success = time.monotonic() - self._last_success_at
        if without_success >= OUTAGE_GIVE_UP_SEC:
            self.say("storage_down | Выход: больше часа доступных файлов нет — оставшиеся лежат на серверах "
                     "хранения Яндекса, которые не отвечают (пострадавшие дата-центры). Попытки не сожжены: "
                     f"запустите скачивание позже. Новости: {TELEGRAM_CHANNEL}")
            self._exit(EXIT_OUTAGE)
        self.say(f"batch {self.batch_idx}: все {deferred} файлов пачки на серверах хранения, которые не отвечают. "
                 f"Пауза {pause} с (без успеха {int(without_success // 60)} мин)")
        time.sleep(pause)

    def _heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat >= HEARTBEAT_EVERY_SEC:
            self._last_heartbeat = now
            try:
                heartbeat(self._current_ids, self.id)
            except Exception as e:
                log.warning(f"w{self.id} heartbeat: {e}")

    # ── обработка исходов ──

    def _on_prepare_failed(self, batch: list[dict], err: PrepareError, headline: str = "") -> None:
        """Яндекс не собрал архив (zip) или не выдал ссылку на файл (direct)."""
        self.say(headline or f"batch {self.batch_idx}: ⚠️ Сбой подготовки архива Яндексом ({len(batch)} файлов)")
        print(f"             Причина: {err}")
        print(f"             (Статус серверов Яндекса: {TELEGRAM_CHANNEL})")

        if err.is_auth:
            # 401/403 бывает и при живых куках — когда Яндекс отключил функцию
            # (так было с массовым скачиванием). Проверяем сессию отдельно.
            if self._refresh_auth():
                err = PrepareError("refused", f"Яндекс отклонил запрос (HTTP {err.status}), "
                                              f"хотя сессия жива: скачивание ограничено на стороне Яндекса", err.status)
                self.say(f"             Уточнение: {err}")
            else:
                _release_to_pending([it["file_id"] for it in batch], self.id)
                self.say("auth_expired | КРИТИЧЕСКАЯ ОШИБКА: Авторизация отклонена Яндексом (401/403).")
                self.say("Куки устарели. Обновите cookies.json в Web UI или браузере и перезапустите процесс.")
                self._exit(EXIT_AUTH)
        else:
            self._refresh_auth()

        self._on_batch_failed(batch, str(err), err.is_server_issue, err.retry_delay, err.kind)

    def _on_downloaded(self, batch: list[dict], result: FetchResult, dur: float) -> None:
        failure = result.failure
        if failure is not None and failure.is_local:
            self._on_local_disk_failure([it["file_id"] for it in batch], failure)
            return

        res = split_by_extracted(batch, result.extracted)
        if res.done:
            self._on_batch_success(batch, res, dur)
            return

        # Ни один файл не приехал (сбой скачивания/распаковки архива)
        reason = str(failure) if failure else "архив пришёл без нужных файлов"
        self.say(f"batch {self.batch_idx}: ⚠️ Сбой отдачи архива узлом Яндекса ({len(batch)} файлов): {reason}")
        server_issue = failure.is_server_issue if failure else False
        self._on_batch_failed(batch, reason, server_issue, 5, failure.kind if failure else "missing")

    def _on_local_disk_failure(self, ids: list[str], failure: FetchFailure) -> None:
        """Локальный диск (место/права) — Яндекс и файлы ни при чём."""
        self.say(f"batch {self.batch_idx}: ⚠️ {failure}")
        _release_to_pending(ids, self.id)
        self._disk_full_streak += 1
        if self._disk_full_streak >= DISK_FULL_GIVE_UP:
            self.say(f"local_disk | Выход: локальный диск не принимает файлы ({DISK_FULL_GIVE_UP} раз подряд).")
            self._exit(EXIT_DISK_FULL)
        time.sleep(30)

    def _note_success(self) -> None:
        """Яндекс отдал хоть что-то — серия сбоев прервана."""
        self._fail_streak = 0
        self._streak_files.clear()
        self._streak_causes.clear()
        self._rate_limit_streak = 0
        self._last_success_at = time.monotonic()

    def _on_file_done(self, fid: str, size: int) -> None:
        self._note_success()
        self._solo_streak.pop(fid, None)
        self.total_files += 1
        self.total_bytes += size

    def _on_batch_success(self, batch: list[dict], res: SplitResult, dur: float) -> None:
        self._note_success()
        # Пачка прошла — постепенно восстанавливаем размер к целевому
        if self.batch_size < self.target_batch:
            self.batch_size = min(self.target_batch, self.batch_size + 5)

        # Помечаем downloaded ТОЛЬКО те файлы, которые реально нашлись в архиве.
        # Остальные возвращаем в очередь — иначе бэкап «зелёный», а файлов нет.
        mark_done(res.done, res.paths)
        done_set = set(res.done)
        done_bytes = sum(_batch_size(it) for it in batch if str(it.get("file_id")) in done_set)
        self.total_files += len(res.done)
        self.total_bytes += done_bytes
        for fid in res.done:
            self._solo_streak.pop(fid, None)
        if res.missing:
            mark_pending(res.missing, "не найден в распакованном архиве", self.id)
        mbps = (done_bytes / 1e6) / max(0.1, dur)
        tail = f", не приехало {len(res.missing)}" if res.missing else ""
        self.say(f"batch {self.batch_idx}: ✓ {len(res.done)}/{len(batch)} files / "
                 f"{done_bytes / 1e9:.2f} GB ({mbps:.1f} MB/s, {int(dur)}s{tail})")

    def _on_batch_failed(self, batch: list[dict], reason: str, server_issue: bool,
                         delay: float, kind: str = "other") -> None:
        """Пачка не дала ни одного файла. Решает, кто виноват, и что делать дальше."""
        self._fail_streak += 1
        ids = [it["file_id"] for it in batch]
        self._streak_files.update(str(i) for i in ids)
        cause = _failure_cause(kind)
        self._streak_causes[cause] += 1

        if cause == "rate_limit":
            # 429: пауза растёт с каждым ответом подряд и действует на все воркеры
            self._rate_limit_streak += 1
            grown = RATE_LIMIT_BASE_PAUSE_SEC * 2 ** (self._rate_limit_streak - 1)
            delay = max(delay, min(RATE_LIMIT_MAX_PAUSE_SEC, grown))
            set_shared_cooldown(delay)
            self.say(f"rate_limit | Яндекс просит реже обращаться (HTTP 429). "
                     f"Общая пауза для всех воркеров: {int(delay)} с")

        outage = (
            self._fail_streak >= OUTAGE_STREAK_THRESHOLD
            and len(self._streak_files) >= OUTAGE_MIN_DISTINCT_FILES
        )
        if outage:
            # Системный отказ: Яндекс не отдаёт архивы по одной причине раз за разом.
            # Попытки файлов не сжигаем, пауза растёт, диагноз — по реальной причине.
            _release_to_pending(ids, self.id)
            self._shrink_batch()
            idx = min(self._fail_streak - OUTAGE_STREAK_THRESHOLD, len(OUTAGE_PAUSES_SEC) - 1)
            pause = max(OUTAGE_PAUSES_SEC[idx], delay)
            if cause == "rate_limit":
                set_shared_cooldown(pause)
            without_success = time.monotonic() - self._last_success_at
            prefix, explain = _OUTAGE_TEXT[self._dominant_cause(cause)]
            streak, what = ("раз подряд не отдал файл", "файлы") if self.mode == "direct" \
                else ("пачек подряд не отдал архив", "архивы")
            self.say(f"{prefix} | Яндекс {self._fail_streak} {streak}. Последняя причина: {reason}")
            self.say(f"{explain} Попытки файлов не сжигаю, пауза {int(pause)} с "
                     f"(без успеха уже {int(without_success // 60)} мин).")
            if without_success >= OUTAGE_GIVE_UP_SEC:
                self.say(f"{prefix} | Выход: Яндекс больше часа не отдаёт {what}. {explain} "
                         f"Запустите скачивание позже. Новости: {TELEGRAM_CHANNEL}")
                self._exit(EXIT_OUTAGE)
            time.sleep(pause)
            return

        if len(batch) > 1:
            # Пачка не собралась — возвращаем в очередь без сжигания попыток
            _release_to_pending(ids, self.id)
            self._shrink_batch()
        elif server_issue:
            self._solo_server_failure(batch[0], reason)
        else:
            # Постоянная ошибка (400, 404 и т.п.) — файл не отдаётся
            self._burn_attempt(batch[0], reason)
        time.sleep(delay)

    # ── вспомогательное ──

    def _dominant_cause(self, last: str) -> str:
        """Чаще всего встречавшаяся причина в текущей серии; при равенстве — последняя."""
        top = max(self._streak_causes.values(), default=0)
        if self._streak_causes.get(last) == top:
            return last
        return self._streak_causes.most_common(1)[0][0] if self._streak_causes else last

    def _shrink_batch(self) -> None:
        """Адаптивно уменьшает пачку при сбоях (25 → 12 → 6 → 1), снижая нагрузку на Яндекс."""
        if self.mode != "zip":
            return  # в direct файлы и так идут по одному: размер пачки нагрузку не меняет
        old = self.batch_size
        self.batch_size = max(1, old // 2)
        if self.batch_size < old:
            self.say(f"снижаю размер пачки: {old} → {self.batch_size} для снижения нагрузки на Яндекс")

    def _burn_attempt(self, item: dict, reason: str) -> None:
        att = int(item.get("attempts") or 0) + 1
        self.say(f"batch {self.batch_idx}: ⚠️ Файл не отдаётся сервером Яндекса → pending (попытка {att}/{MAX_ATTEMPTS})")
        mark_pending([str(item["file_id"])], reason, self.id)

    def _solo_server_failure(self, item: dict, reason: str) -> None:
        """Сервер не отдал одиночный файл по своей вине. Попытку не сжигаем,
        но считаем серию: бесконечно крутить один файл тоже нельзя."""
        fid = str(item["file_id"])
        streak = self._solo_streak.get(fid, 0) + 1
        if streak >= SOLO_SERVER_FAIL_STREAK:
            self._solo_streak.pop(fid, None)
            self._burn_attempt(item, reason)
        else:
            self._solo_streak[fid] = streak
            _release_to_pending([fid], self.id)

    def _has_disk_space(self, batch: list[dict]) -> bool:
        total = sum(_batch_size(it) for it in batch)
        # zip: архив + распакованное + запас; direct: файлы + запас
        need = max(MIN_FREE_BYTES, int(total * (2.5 if self.mode == "zip" else 1.2)))
        ok, free = check_disk_space(DOWNLOAD_DIR, need)
        if ok:
            return True
        free_str = human_bytes(free) if free >= 0 else "неизвестно"
        self.say(f"local_disk | ⚠️ Недостаточно места на диске ({free_str} свободно, нужно ~{human_bytes(need)}). "
                 f"Возвращаю файлы в очередь.")
        return False

    def _exit(self, code: int) -> None:
        self._cleanup()
        sys.exit(code)

    def _cleanup(self) -> None:
        if self.session is not None:
            try:
                self.session.close()  # освобождаем sockets
            except Exception:
                pass
        # Чистим пустую папку для ZIP'ов
        try:
            if os.path.isdir(ZIP_TMP_DIR) and not os.listdir(ZIP_TMP_DIR):
                os.rmdir(ZIP_TMP_DIR)
        except OSError:
            pass


def main() -> int:
    ensure_utf8_stdio()

    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, required=True, help="ID воркера (1, 2, ...)")
    ap.add_argument("--batch", type=int, default=BATCH_SIZE)
    ap.add_argument("--mode", choices=MODES, default=DEFAULT_MODE,
                    help="direct — по одному файлу (работает при отключённых архивах), "
                         "zip — архивами пачек")
    ap.add_argument("--retry-failed", action="store_true", help="Сбросить failed-файлы обратно в pending")
    args = ap.parse_args()

    _setup_logging()
    worker = Worker(args.id, args.batch, args.mode)
    mode_text = "по одному файлу" if worker.mode == "direct" else "ZIP-архивами"
    print("=" * 65)
    print(f"  Yandex.Disk Unlim Worker #{worker.id} — by Davron (PXSYS) · v{APP_VERSION}")
    print(f"  Telegram: {TELEGRAM_CHANNEL} (новости о статусе серверов)")
    print(f"  batch={worker.target_batch} · режим: {mode_text} · папка: {EXTRACT_DIR}")
    print("=" * 65)
    log.info("worker %d старт: v%s, batch=%d, режим=%s, БД=%s, папка=%s", worker.id, APP_VERSION,
             worker.target_batch, worker.mode, DB_FILE, EXTRACT_DIR)

    if not os.path.exists(DB_FILE):
        sys.exit("Нет unlim_state.db — сначала запусти unlim_download.py --collect")

    ensure_attempts_column()
    if args.retry_failed:
        n_res = reset_failed()
        if n_res:
            worker.say(f"сброшено {n_res} файлов с ошибками обратно в очередь")

    n_rec = recover_stuck(worker_id=worker.id)
    if n_rec:
        worker.say(f"возвращено {n_rec} зависших файлов в очередь")

    summary = queue_summary()
    ok_space, free = check_disk_space(DOWNLOAD_DIR, 0)
    free_str = human_bytes(free) if free >= 0 else "неизвестно"
    worker.say(f"{summary}; свободно на диске {free_str}")
    log.info("worker %d: %s; свободно %s", worker.id, summary, free_str)

    worker.authorize()
    code = worker.run()
    log.info("worker %d завершён: код %s, %d файлов, %s", worker.id, code,
             worker.total_files, human_bytes(worker.total_bytes))
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
