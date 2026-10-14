"""Общий код для модулей безлимитного фото-хранилища (unlim_download / unlim_worker / webui).

Вынесены повторяющиеся: константы, загрузка кук, создание сессии, получение sk/uid,
миграция БД, соединения и транзакции над unlim_state.db.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

import requests
from dotenv import load_dotenv

from storage_fetch import response_trace  # noqa: F401 — заодно реэкспорт для воркера и тестов
from utils import sqlite_tx

log = logging.getLogger("unlim")

PROJECT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT, ".env"))

COOKIES_FILE = os.path.join(PROJECT, "cookies.json")
UNLIM_DB_FILE = os.path.join(PROJECT, "unlim_state.db")
LOG_DIR = os.path.join(PROJECT, "logs")

BASE = "https://disk.yandex.ru"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(PROJECT, "downloads")).rstrip("/\\")

UNLIM_SCHEMA = """
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
    attempts INTEGER DEFAULT 0,
    worker_id INTEGER,
    assigned_at TIMESTAMP,
    downloaded_at TIMESTAMP,
    storage_host TEXT,
    preview_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_unlim_status ON unlim_files(status);
CREATE TABLE IF NOT EXISTS unlim_clusters (
    cluster_id TEXT PRIMARY KEY,
    size INTEGER,
    fetched INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS unlim_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class UnlimError(Exception):
    """Базовая ошибка подсистемы безлимитного хранилища."""


class AuthExpiredError(UnlimError):
    """Срок действия сессии или кук истёк (401/403/passport redirect)."""


class YandexUnavailableError(UnlimError):
    """Яндекс не ответил или ответил ошибкой сервера — куки при этом могут быть живы."""


class CookiesFormatError(SystemExit, UnlimError):
    """Неверный формат или отсутствие файла cookies.json."""


# ── Куки и HTTP-сессия ──────────────────────────────────────────────────────

# Без этих кук web-API Диска не авторизует запрос.
REQUIRED_COOKIES = ("Session_id", "yandexuid", "sessionid2")


def missing_required_cookies(cookies: list) -> list[str]:
    """Какие из обязательных кук отсутствуют в массиве формата Cookie-Editor."""
    names = {c.get("name") for c in cookies if isinstance(c, dict)}
    return [n for n in REQUIRED_COOKIES if n not in names]


def load_cookies(cookies_file: str | None = None) -> dict[str, str]:
    """Загружает куки из cookies.json (формат Cookie-Editor)."""
    path = cookies_file or COOKIES_FILE
    if not os.path.exists(path):
        raise CookiesFormatError(
            "Нет cookies.json — сначала экспортируй куки из браузера. "
            "См. README раздел про куки."
        )
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise CookiesFormatError("cookies.json должен быть массивом (формат Cookie-Editor)")
    res: dict[str, str] = {}
    for c in raw:
        if isinstance(c, dict):
            name = c.get("name")
            value = c.get("value")
            if isinstance(name, str) and isinstance(value, str) and name and value:
                res[name] = value
    return res


def make_session(cookies: dict[str, str] | None = None,
                 cookies_file: str | None = None) -> requests.Session:
    """Создаёт requests.Session с нужными заголовками и куками.

    Если cookies не переданы — загружает из cookies.json.
    """
    if cookies is None:
        cookies = load_cookies(cookies_file)
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
    """Извлекает sk и uid из HTML-страницы диска.

    AuthExpiredError — только когда Яндекс явно не пускает (редирект на вход, 401/403).
    Сеть, 50x и 429 дают YandexUnavailableError: во время сбоев Яндекса куки живы,
    и просить человека обновить их — значит гонять его по кругу.
    """
    try:
        r = s.get(f"{BASE}/client/disk", timeout=30)
    except requests.exceptions.RequestException as e:
        raise YandexUnavailableError(f"Яндекс не ответил ({type(e).__name__})") from e
    if "passport.yandex" in r.url or r.status_code in (401, 403):
        raise AuthExpiredError("Куки невалидны — нужно обновить cookies.json")
    if r.status_code != 200:
        raise YandexUnavailableError(f"Яндекс ответил HTTP {r.status_code} — сбой на его стороне")
    sk = re.search(r'"sk"\s*:\s*"([^"]+)"', r.text)
    uid = re.search(r'"uid"\s*:\s*"?(\d+)"?', r.text)
    if not sk or not uid:
        raise UnlimError("Не нашёл sk/uid в HTML — Яндекс изменил формат")
    return sk.group(1), uid.group(1)


def make_cid(uid: str) -> str:
    """Генерирует connection_id для API-запроса."""
    return f"{uid}{int(time.time() * 1000)}{random.randint(100, 999)}"


def api_call(s: requests.Session, sk: str, uid: str, method: str,
             params=None, retries: int = 3):
    """Вызов web-API Яндекс.Диска с retry и обработкой rate limit."""
    last_err = None
    for att in range(retries):
        try:
            body = {"sk": sk, "connection_id": make_cid(uid), "apiMethod": method}
            if params is not None:
                body["requestParams"] = params
            t0 = time.monotonic()
            r = s.post(f"{BASE}/models-v2", params={"m": method},
                       json=body, timeout=120)
            log.info("%s: HTTP %s за %d мс%s", method, r.status_code,
                     int((time.monotonic() - t0) * 1000), response_trace(r))
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    raise RuntimeError(f"{method}: ответ не JSON: {r.text[:200]}")
                # mpfs/list отвечает списком, остальные методы — объектом
                if not isinstance(data, (dict, list)):
                    raise RuntimeError(f"{method}: неожиданный формат ответа: {type(data).__name__}")
                return data
            if r.status_code in (401, 403):
                raise AuthExpiredError(f"Сессия истекла (HTTP {r.status_code})")
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code == 429:
                wait = 30 + att * 30
                log.warning(f"{method} HTTP 429 (Лимит запросов Яндекса) → sleep {wait}с")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                log.warning(f"{method} HTTP {r.status_code} (Сбой узлов Яндекса / дата-центра) → повтор через {2 ** att}с")
            time.sleep(2 ** att)
        except AuthExpiredError:
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** att)
    raise RuntimeError(f"{method}: {last_err} (Проблема на стороне серверов Яндекса. Новости и статус: https://t.me/davronsv)")


# ── БД безлимита: соединения, транзакции, миграция ──────────────────────────


def connect_unlim_db(db_path: str | None = None, timeout: float = 60) -> sqlite3.Connection:
    """Соединение с unlim_state.db в WAL-режиме, строки — sqlite3.Row."""
    c = sqlite3.connect(db_path or UNLIM_DB_FILE, timeout=timeout)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def unlim_tx(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Короткая транзакция над unlim_state.db: открыть → BEGIN IMMEDIATE → COMMIT → закрыть.

    Несколько воркеров пишут в одну БД параллельно, поэтому каждая операция —
    отдельное короткое соединение и явная транзакция с блокировкой на запись.
    """
    c = connect_unlim_db(db_path)
    try:
        with sqlite_tx(c):
            yield c
    finally:
        c.close()


RESET_FAILED_SQL = (
    "UPDATE unlim_files SET status='pending', attempts=0, error=NULL "
    "WHERE status='failed' OR (status='pending' AND attempts > 0)"
)
RELEASE_IN_PROGRESS_SQL = (
    "UPDATE unlim_files SET status='pending', worker_id=NULL WHERE status='in_progress'"
)


def reset_failed_files(db_path: str | None = None, *, include_in_progress: bool = False) -> int:
    """Возвращает упавшие файлы в очередь (attempts=0). Возвращает число затронутых строк.

    include_in_progress=True — также сбрасывает зависшие in_progress (когда
    точно известно, что воркеры не работают).
    """
    with unlim_tx(db_path) as c:
        n = c.execute(RESET_FAILED_SQL).rowcount
        if include_in_progress:
            n += c.execute(RELEASE_IN_PROGRESS_SQL).rowcount
        return n


# Маркер отложенного файла в колонке error (его же считает db_stats в webui.py)
DEFERRED_MARKER = "storage_down | "


def _add_column(c: sqlite3.Connection, ddl: str) -> None:
    """ALTER TABLE, который не падает, если колонку только что добавил соседний воркер."""
    try:
        c.execute(ddl)
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def ensure_unlim_schema(db_path: str | None = None) -> None:
    """Миграция старых БД: колонки attempts и storage_host, индекс, таблица unlim_meta.

    Воркеры стартуют одновременно и зовут её параллельно — это безопасно.
    """
    db_path = db_path or UNLIM_DB_FILE
    c = sqlite3.connect(db_path, timeout=60)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(unlim_files)")}
        if cols and "attempts" not in cols:
            _add_column(c, "ALTER TABLE unlim_files ADD COLUMN attempts INTEGER DEFAULT 0")
            cols.add("attempts")
        if cols and "storage_host" not in cols:
            _add_column(c, "ALTER TABLE unlim_files ADD COLUMN storage_host TEXT")
        if cols and "preview_path" not in cols:
            _add_column(c, "ALTER TABLE unlim_files ADD COLUMN preview_path TEXT")
        if {"status", "attempts", "assigned_at"}.issubset(cols):
            c.execute("CREATE INDEX IF NOT EXISTS idx_unlim_pending ON unlim_files(status, attempts, assigned_at)")
        # Общие для всех воркеров значения: пауза по 429, молчащие серверы хранения
        c.execute("CREATE TABLE IF NOT EXISTS unlim_meta (key TEXT PRIMARY KEY, value TEXT)")
        c.commit()
    finally:
        c.close()


# Старое имя — им пользуются CLI и тесты
ensure_attempts_column = ensure_unlim_schema


def clear_dead_hosts(db_path: str | None = None) -> int:
    """Забывает молчащие серверы хранения: в новом раунде каждый пробуется снова."""
    db_path = db_path or UNLIM_DB_FILE
    if not os.path.exists(db_path):
        return 0
    with unlim_tx(db_path) as c:
        try:
            return c.execute("DELETE FROM unlim_meta WHERE key LIKE 'dead_host:%'").rowcount
        except sqlite3.OperationalError:  # старая БД без unlim_meta
            return 0


def count_deferred(db_path: str | None = None) -> int:
    """Сколько файлов ждут, когда их серверы хранения оживут."""
    db_path = db_path or UNLIM_DB_FILE
    if not os.path.exists(db_path):
        return 0
    c = connect_unlim_db(db_path)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM unlim_files WHERE status='pending' AND error LIKE ?",
            (DEFERRED_MARKER + "%",),
        ).fetchone()[0]
    finally:
        c.close()
