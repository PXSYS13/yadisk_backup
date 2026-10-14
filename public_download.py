"""Скачивание папки или файла по публичной ссылке Яндекс Диска (disk.yandex.ru/d/…).

Ключ доступа не нужен: публичный API отдаёт список файлов и ссылки на них без входа.
Файлы качаются по одному — «Скачать всё» (ZIP) Яндекс отключил в октябре 2026, — с
той же защитой, что и Диск: молчащий сервер хранения откладывает файл, размер и MD5
проверяются. Раздел «По ссылке» в Web UI запускает `public_download.py --wait`;
ссылки добавляет сам webui (add_link), поэтому адреса не попадают ни в аргументы,
ни в журналы — там только «#id «имя»».

CLI:
    uv run public_download.py --add https://disk.yandex.ru/d/XXXX   # добавить ссылку
    uv run public_download.py            # собрать списки и скачать один раунд
    uv run public_download.py --wait     # повторять отложенное, пока не скачается всё
    uv run public_download.py --status   # что в списке
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from dotenv import load_dotenv

from state import DEFERRED_MARKER
from storage_fetch import DeadHosts, FetchFailure, fetch_preview, fetch_to_file
from utils import (
    APP_VERSION,
    EXIT_DEFERRED,
    EXIT_DISK_FULL,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_OUTAGE,
    EXIT_PARTIAL,
    LINKS_DIRNAME,
    MAX_DISK_WORKERS,
    avoid_case_collision,
    categorize,
    ensure_dir,
    ensure_utf8_stdio,
    human_bytes,
    keep_awake,
    local_copy_matches,
    preview_path_for,
    previews_enabled,
    remove_quiet,
    resolve_local_path,
    safe_int,
    sanitize_component,
    sqlite_tx,
    to_long_path,
)

PROJECT = Path(__file__).resolve().parent
DB_FILE = PROJECT / "links_state.db"
ENV_FILE = PROJECT / ".env"

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
API_TIMEOUT = (10, 30)
# Сколько элементов папки просим за один запрос списка
LIST_LIMIT = 500
DEFAULT_WORKERS = 3
# Столько ответов «лимит скачиваний» подряд по ссылке — ставим её на паузу до полуночи
LIMIT_STREAK = 3
# Столько ошибок локального диска подряд — дальше качать бессмысленно (код 4)
LOCAL_DISK_GIVE_UP = 10
# Сколько раз берём свежую ссылку на файл при обрыве или 50x
FILE_TRIES = 3
# Список ссылки старше этого собирается заново при новом запуске
RELIST_SEC = 24 * 3600
# Как часто ожидание раунда проверяет «Попробовать сейчас» и как часто печатается прогресс
WAKE_POLL_SEC = 2.0
PROGRESS_EVERY_SEC = 2.0
# Хост ссылки на файл: только серверы Яндекса
_HREF_HOSTS = (".yandex.net", ".yandex.ru")
# Лимит скачиваний по публичной ссылке Яндекс снимает в полночь по Москве
MSK = timezone(timedelta(hours=3))

LINK_HOSTS = ("disk.yandex.ru", "disk.yandex.com", "disk.yandex.kz", "disk.yandex.by", "disk.yandex.uz",
              "disk.yandex.com.tr", "disk.360.yandex.ru", "yadi.sk")
# Ошибки, после которых ссылку бессмысленно пробовать, пока человек не нажмёт «Попробовать снова»
LINK_LEVEL_ERRORS = ("not_found", "password", "forbidden")

log = logging.getLogger("public_download")

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    public_url TEXT NOT NULL,
    root_path TEXT NOT NULL DEFAULT '/',
    name TEXT,
    kind TEXT,
    local_dir TEXT,
    added_at REAL,
    listed_at REAL,
    gen INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'active',
    error_kind TEXT,
    error TEXT,
    limit_until REAL
);
CREATE TABLE IF NOT EXISTS link_files (
    link_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    size INTEGER,
    md5 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    local_path TEXT,
    error TEXT,
    storage_host TEXT,
    preview_path TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    gen INTEGER NOT NULL DEFAULT 0,
    downloaded_at TEXT,
    PRIMARY KEY (link_id, path)
);
CREATE INDEX IF NOT EXISTS idx_link_files_status ON link_files(link_id, status);
CREATE TABLE IF NOT EXISTS links_meta (key TEXT PRIMARY KEY, value TEXT);
"""


# ── Ссылка из письма или мессенджера ────────────────────────────────────────


def normalize_public_url(text: str) -> tuple[str, str]:
    """Ссылка → (адрес для публичного API, подпапка '/…' или '/').

    Принимает disk.yandex.ru/d/КЛЮЧ, /i/ (файл), yadi.sk, другие домены Яндекса и
    ссылку на подпапку (…/d/КЛЮЧ/Подпапка — тогда качается только она). Ошибка —
    ValueError с текстом для человека.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Вставьте ссылку на папку или файл Яндекс Диска")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        u = urlsplit(raw)
        host = (u.hostname or "").lower()
    except ValueError:
        raise ValueError("Это не похоже на ссылку") from None
    host = host.removeprefix("www.")
    if host not in LINK_HOSTS:
        raise ValueError("Это не ссылка Яндекс Диска — такие начинаются с disk.yandex.ru/d/ или yadi.sk/d/")
    parts = [unquote(p) for p in u.path.split("/") if p]
    kind = parts[0] if parts else ""
    if kind == "client":
        raise ValueError("Это адрес вашего собственного Диска, а не публичная ссылка. "
                         "Свой Диск программа качает на «Главной»")
    if kind == "a":
        raise ValueError("Это ссылка на альбом — альбомы программа пока не скачивает. Попросите ссылку на папку")
    if kind == "public" and parse_qs(u.query).get("hash"):
        # Старый вид ссылки: ключ — весь адрес
        return f"https://{host}/public/?hash={parse_qs(u.query)['hash'][0]}", "/"
    if kind not in ("d", "i") or len(parts) < 2:
        raise ValueError("Не похоже на публичную ссылку: в ней должно быть /d/ или /i/")
    public_url = f"https://{host}/{kind}/{parts[1]}"
    sub = "/" + "/".join(parts[2:]) if kind == "d" and len(parts) > 2 else "/"
    return public_url, sub


def link_key(public_url: str, sub_path: str) -> str:
    """Уникальный адрес ссылки в базе: публичная ссылка плюс подпапка."""
    return public_url + (sub_path if sub_path not in ("", "/") else "")


# ── Публичный API Яндекса ───────────────────────────────────────────────────


class LinkError(Exception):
    """Публичный API не дал того, что нужно. kind — для логики, text — для человека."""

    def __init__(self, kind: str, text: str):
        super().__init__(text)
        self.kind = kind
        self.text = text

    @property
    def link_level(self) -> bool:
        return self.kind in LINK_LEVEL_ERRORS


def _error_for(resp) -> LinkError:
    try:
        body = resp.json()
        name = str(body.get("error") or "") if isinstance(body, dict) else ""
    except ValueError:
        name = ""
    code = resp.status_code
    if code in (404, 410):
        return LinkError("not_found", "Ссылка больше не работает: её удалили или закрыли")
    if code == 403 and "Password" in name:
        return LinkError("password", "Ссылка с паролем — такие программа пока не скачивает")
    if code in (401, 403):
        return LinkError("forbidden", "Владелец закрыл скачивание по этой ссылке")
    if code == 429 and name == "DiskResourceDownloadLimitExceededError":
        return LinkError("limit", "Яндекс ограничил скачивание по этой ссылке до конца суток")
    if code == 429:
        return LinkError("rate_limit", "Яндекс просит обращаться реже")
    if code >= 500:
        return LinkError("server_50x", f"Сбой серверов Яндекса ({code})")
    return LinkError("refused", f"Яндекс отказал ({code})")


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = f"yadisk-backup/{APP_VERSION}"
    return s


class PublicApi:
    """Публичный API без ключа: заголовка Authorization нет вовсе."""

    def __init__(self, session=None, timeout=API_TIMEOUT):
        self.session = session or _new_session()
        self.timeout = timeout

    def _get(self, url: str, params: dict) -> dict:
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout:
            raise LinkError("timeout", "Яндекс отвечал слишком долго") from None
        except requests.exceptions.RequestException:
            raise LinkError("network", "Нет связи с Яндексом — проверьте интернет") from None
        if resp.status_code != 200:
            raise _error_for(resp)
        try:
            data = resp.json()
        except ValueError:
            raise LinkError("server_50x", "Яндекс ответил непонятно") from None
        if not isinstance(data, dict):
            raise LinkError("server_50x", "Яндекс ответил непонятно")
        return data

    def meta(self, public_url: str, path: str = "/", limit: int | None = None, offset: int = 0,
             preview_size: str | None = None) -> dict:
        params = {"public_key": public_url, "limit": LIST_LIMIT if limit is None else limit, "offset": offset}
        if path and path != "/":
            params["path"] = path
        if preview_size:
            params.update(preview_size=preview_size, preview_crop="false")
        return self._get(API, params)

    def download_href(self, public_url: str, path: str = "/") -> str:
        params = {"public_key": public_url}
        if path and path != "/":
            params["path"] = path
        href = self._get(API + "/download", params).get("href")
        if not href:
            raise LinkError("server_50x", "Яндекс не выдал ссылку на файл")
        return href


_thread_local = threading.local()


def _thread_api() -> PublicApi:
    """Свой клиент API на поток: сессия requests не рассчитана на общий доступ."""
    api = getattr(_thread_local, "api", None)
    if api is None:
        api = _thread_local.api = PublicApi()
    return api


def _thread_http() -> requests.Session:
    """Сессия потока для самих файлов: ссылки подписаны, ключ не нужен."""
    http = getattr(_thread_local, "http", None)
    if http is None:
        http = _thread_local.http = _new_session()
    return http


# ── База ссылок ─────────────────────────────────────────────────────────────


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def connect_db(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_FILE), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM links_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM links_meta WHERE key=?", (key,))
    else:
        conn.execute("INSERT INTO links_meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def poke(conn: sqlite3.Connection, wake: bool = False) -> None:
    """Сообщить идущему скачиванию: список ссылок изменился (и, если wake, — не ждать раунда)."""
    meta_set(conn, "changed", str(time.time()))
    if wake:
        meta_set(conn, "wake", str(time.time()))


def link_folder_name(conn: sqlite3.Connection, name: str) -> str:
    """Папка ссылки внутри папки скачивания: '_ссылки/<имя>', уникальная без учёта регистра."""
    text = (name or "").strip()
    base = (sanitize_component(text)[:80].rstrip(" .") if text else "") or "Ссылка"
    taken = {r[0].casefold() for r in conn.execute("SELECT local_dir FROM links WHERE local_dir IS NOT NULL")}
    candidate, n = base, 2
    while f"{LINKS_DIRNAME}/{candidate}".casefold() in taken:
        candidate = f"{base} ({n})"
        n += 1
    return f"{LINKS_DIRNAME}/{candidate}"


def _fallback_name(public_url: str, sub_path: str) -> str:
    tail = [p for p in sub_path.split("/") if p]
    return tail[-1] if tail else public_url.rstrip("/").rsplit("/", 1)[-1]


def add_link(conn: sqlite3.Connection, public_url: str, sub_path: str = "/",
             meta: dict | None = None) -> tuple[int, bool]:
    """Добавляет ссылку или возвращает убранную из списка. (id, появилась ли она в списке)."""
    url = link_key(public_url, sub_path)
    name = (meta or {}).get("name") or _fallback_name(public_url, sub_path)
    kind = (meta or {}).get("type")
    with sqlite_tx(conn):
        row = conn.execute("SELECT id, state FROM links WHERE url=?", (url,)).fetchone()
        if row:
            if row["state"] != "removed":
                return row["id"], False
            conn.execute("UPDATE links SET state='active', error_kind=NULL, error=NULL, limit_until=NULL, "
                         "listed_at=NULL WHERE id=?", (row["id"],))
            return row["id"], True
        cur = conn.execute(
            "INSERT INTO links (url, public_url, root_path, name, kind, local_dir, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, public_url, sub_path or "/", name, kind, link_folder_name(conn, name), time.time()),
        )
    return int(cur.lastrowid), True


def remove_link(conn: sqlite3.Connection, link_id: int, purge: bool) -> bool:
    """Убирает ссылку из списка. Файлы на диске остаются. purge — сразу забыть её файлы."""
    with sqlite_tx(conn):
        n = conn.execute("UPDATE links SET state='removed' WHERE id=? AND state<>'removed'", (link_id,)).rowcount
        if purge:
            conn.execute("DELETE FROM link_files WHERE link_id=?", (link_id,))
    return bool(n)


def purge_removed(conn: sqlite3.Connection) -> None:
    with sqlite_tx(conn):
        conn.execute("DELETE FROM link_files WHERE link_id IN (SELECT id FROM links WHERE state='removed')")


def label(link) -> str:
    """Как ссылка выглядит в журнале: без адреса."""
    return f"#{link['id']} «{link['name'] or 'ссылка'}»"


# ── Список файлов ссылки ────────────────────────────────────────────────────


def _upsert_files(conn: sqlite3.Connection, link_id: int, gen: int, items: list[tuple]) -> None:
    if not items:
        return
    with sqlite_tx(conn):
        conn.executemany(
            """
            INSERT INTO link_files (link_id, path, size, md5, status, gen) VALUES (?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(link_id, path) DO UPDATE SET
                size = excluded.size,
                md5 = COALESCE(excluded.md5, link_files.md5),
                gen = excluded.gen,
                status = CASE
                    WHEN link_files.status = 'gone' THEN 'pending'
                    WHEN link_files.status = 'downloaded'
                         AND excluded.md5 IS NOT NULL AND link_files.md5 IS NOT NULL
                         AND excluded.md5 <> link_files.md5
                    THEN 'pending'
                    ELSE link_files.status
                END
            """,
            [(link_id, path, size, md5, gen) for path, size, md5 in items],
        )


def list_link(api: PublicApi, conn: sqlite3.Connection, link, on_found=None) -> int:
    """Собирает список файлов ссылки обходом папок в ширину. Возвращает число файлов.

    Файлы, которых больше нет, помечаются 'gone' — только после полного обхода.
    LinkError решает вызывающий.
    """
    gen = int(link["gen"] or 0) + 1
    root = link["root_path"] or "/"
    found = 0
    batch: list[tuple] = []
    first = api.meta(link["public_url"], root)
    kind = first.get("type") or "dir"
    name = first.get("name") or link["name"]
    if kind == "file":
        batch.append(("/", first.get("size"), first.get("md5")))
        found = 1
    else:
        queue: deque[str] = deque([root])
        while queue:
            path = queue.popleft()
            offset = 0
            while True:
                data = first if path == root and offset == 0 else api.meta(link["public_url"], path, offset=offset)
                emb = data.get("_embedded") or {}
                items = emb.get("items") or []
                for it in items:
                    if it.get("type") == "dir" and it.get("path"):
                        queue.append(it["path"])
                    elif it.get("type") == "file" and it.get("path"):
                        batch.append((it["path"], it.get("size"), it.get("md5")))
                        found += 1
                if len(batch) >= 200:
                    _upsert_files(conn, link["id"], gen, batch)
                    batch.clear()
                if on_found:
                    on_found(found)
                offset += len(items)
                total = emb.get("total")
                if not items or (total is not None and offset >= int(total)) or (total is None and len(items) < LIST_LIMIT):
                    break
    _upsert_files(conn, link["id"], gen, batch)
    with sqlite_tx(conn):
        conn.execute("UPDATE link_files SET status='gone' WHERE link_id=? AND gen<>? AND status IN ('pending','failed')",
                     (link["id"], gen))
        # Пока ничего не скачано, папку можно назвать по-настоящему (при добавлении Яндекс мог не ответить)
        nothing_yet = not conn.execute("SELECT 1 FROM link_files WHERE link_id=? AND status='downloaded' LIMIT 1",
                                       (link["id"],)).fetchone()
        rename = nothing_yet and name and name != link["name"]
        conn.execute("UPDATE links SET gen=?, listed_at=?, name=?, kind=?, error_kind=NULL, error=NULL WHERE id=?",
                     (gen, time.time(), name, kind, link["id"]))
        if rename:
            conn.execute("UPDATE links SET local_dir=NULL WHERE id=?", (link["id"],))
            conn.execute("UPDATE links SET local_dir=? WHERE id=?", (link_folder_name(conn, name), link["id"]))
    return found


# ── Скачивание ──────────────────────────────────────────────────────────────


@dataclass
class Job:
    link_id: int
    path: str
    size: int | None
    md5: str | None
    error: str | None
    storage_host: str | None
    preview_path: str | None
    public_url: str
    local_dir: str
    kind: str | None


@dataclass
class Result:
    job: Job
    status: str  # downloaded | deferred | failed | limited | gone | link_error
    local_path: str | None = None
    bytes_written: int = 0
    error: str = ""
    storage_host: str = ""
    preview_path: str = ""
    error_kind: str = ""
    api_ok: bool = False      # API ответил — Яндекс не «лежит»
    api_failed: bool = False  # API так и не ответил: сбой Яндекса, а не файла


def local_target(download_dir: str, job: Job) -> str:
    """<папка скачивания>/_ссылки/<имя ссылки>/<путь внутри папки>; у ссылки на файл — сам файл."""
    base = os.path.join(download_dir, *job.local_dir.split("/"))
    if job.kind == "file" or job.path in ("", "/"):
        return base
    return resolve_local_path(base, job.path)


def _href_allowed(href: str) -> bool:
    host = (urlsplit(href).hostname or "").lower()
    return urlsplit(href).scheme == "https" and any(host.endswith(h) for h in _HREF_HOSTS)


def _save_preview(api: PublicApi, job: Job, local: str, download_dir: str) -> str:
    """Страховочное превью отложенного фото. Путь или "" — никогда не бросает."""
    if not previews_enabled() or categorize(os.path.basename(local)) != "photos":
        return ""
    if job.preview_path and os.path.exists(to_long_path(job.preview_path)):
        return ""
    dst = preview_path_for(download_dir, local)
    try:
        url = api.meta(job.public_url, job.path, limit=1, preview_size="XXXL").get("preview")
        if not url or not _href_allowed(url):
            return ""
        failure = fetch_preview(_thread_http(), url, to_long_path(dst))
    except Exception as e:  # noqa: BLE001 — превью — бонус
        log.warning("превью #%s: %s", job.link_id, type(e).__name__)  # текст ошибки мог бы унести адрес
        return ""
    return "" if failure is not None else dst


def _download_file(job: Job, download_dir: str, dead: DeadHosts) -> Result:
    """Скачивает один файл ссылки. Никогда не бросает: любой исход — Result."""
    api = _thread_api()
    local = ""
    part = ""
    try:
        local = avoid_case_collision(local_target(download_dir, job))
        parent = os.path.dirname(local)
        if parent:
            ensure_dir(to_long_path(parent))
        if local_copy_matches(local, job.size, job.md5):
            return Result(job, "downloaded", local)
        if job.storage_host and dead.is_dead(job.storage_host):
            # Сервер этого файла недавно молчал — не беспокоим Яндекс до следующего раунда
            return Result(job, "deferred", error=f"{DEFERRED_MARKER}сервер хранения {job.storage_host} недавно не отвечал",
                          storage_host=job.storage_host)
        part = to_long_path(local + ".part")
        remove_quiet(part)
        was_deferred = (job.error or "").startswith(DEFERRED_MARKER)
        tries = 1 if was_deferred else FILE_TRIES
        failure: FetchFailure | None = None
        api_error: LinkError | None = None
        api_ok = False
        for attempt in range(tries):
            try:
                href = api.download_href(job.public_url, job.path)
            except LinkError as e:
                if e.kind in ("limit", "not_found") or e.link_level:
                    raise
                api_error, failure = e, None
                if attempt + 1 < tries:
                    time.sleep(10 if e.kind == "rate_limit" else 2 * (attempt + 1))
                continue
            api_ok, api_error = True, None
            if not _href_allowed(href):
                return Result(job, "failed", error="refused | Яндекс прислал ссылку не на свой сервер — файл пропущен",
                              api_ok=True)
            written, _host, failure = fetch_to_file(
                _thread_http(), href, part, expected_size=job.size, expected_md5=job.md5,
                host_is_dead=dead.is_dead, reject_pages=False,
            )
            if failure is None:
                os.replace(part, to_long_path(local))
                return Result(job, "downloaded", local, written, api_ok=True)
            if failure.is_storage:
                if failure.kind == "storage_down":
                    dead.mark(failure.host)
                remove_quiet(part)
                res = Result(job, "deferred", error=str(failure), storage_host=failure.host, api_ok=True)
                res.preview_path = _save_preview(api, job, local, download_dir)
                return res
            if failure.is_local or attempt + 1 >= tries:
                break
            time.sleep(10 if failure.kind == "rate_limit" else 2 * (attempt + 1))
        remove_quiet(part)
        if api_error is not None:
            # Яндекс не выдал ссылку на файл — это сбой Яндекса, а не файла: ждём раунда
            return Result(job, "deferred", error=f"{DEFERRED_MARKER}{api_error.text}",
                          api_ok=api_ok, api_failed=not api_ok)
        if failure is not None and failure.is_local:
            return Result(job, "failed", error=f"local_disk | {failure.text}", api_ok=api_ok)
        if failure is not None and failure.kind in ("server_50x", "timeout", "connection", "rate_limit"):
            return Result(job, "deferred", error=f"{DEFERRED_MARKER}Яндекс не отдал файл после {tries} попыток: {failure.text}",
                          storage_host=failure.host, api_ok=api_ok)
        kind = {"refused": "http_error", "bad_file": "bad_file"}.get(failure.kind if failure else "", "http_error")
        return Result(job, "failed", error=f"{kind} | {failure.text if failure else 'неизвестный сбой'}", api_ok=api_ok)
    except LinkError as e:
        if part:
            remove_quiet(part)
        if e.kind == "limit":
            return Result(job, "limited", error=f"link_limit | {e.text}", api_ok=True)
        if e.kind == "not_found":
            return Result(job, "gone", error=f"not_found | {e.text}", api_ok=True)
        return Result(job, "link_error", error=f"{e.kind} | {e.text}", error_kind=e.kind, api_ok=True)
    except OSError as e:
        if part:
            remove_quiet(part)
        return Result(job, "failed", error=f"local_disk | {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001 — один файл не должен ронять весь раунд
        if part:
            remove_quiet(part)
        log.exception("файл #%s", job.link_id)
        return Result(job, "failed", error=f"{type(e).__name__}: {e}")


def next_limit_reset(now: float) -> float:
    """Ближайшие 00:10 по Москве: лимит скачиваний по ссылке Яндекс снимает в полночь."""
    dt = datetime.fromtimestamp(now, MSK)
    reset = dt.replace(hour=0, minute=10, second=0, microsecond=0)
    if reset <= dt:
        reset += timedelta(days=1)
    return reset.timestamp()


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _files(n: int) -> str:
    n = int(n)
    tail = "файл" if n % 10 == 1 and n % 100 != 11 else (
        "файла" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "файлов")
    return f"{n} {tail}"


def _take_jobs(conn: sqlite3.Connection, now: float) -> list[Job]:
    rows = conn.execute(
        """
        SELECT f.link_id, f.path, f.size, f.md5, f.error, f.storage_host, f.preview_path,
               l.public_url, l.local_dir, l.kind
          FROM link_files f JOIN links l ON l.id = f.link_id
         WHERE l.state='active' AND l.error_kind IS NULL AND l.listed_at IS NOT NULL
           AND (l.limit_until IS NULL OR l.limit_until <= ?) AND f.status='pending'
         ORDER BY f.link_id, COALESCE(f.size, 0)
        """,
        (now,),
    ).fetchall()
    return [Job(r["link_id"], r["path"], r["size"], r["md5"], r["error"], r["storage_host"], r["preview_path"],
                r["public_url"], r["local_dir"], r["kind"]) for r in rows]


class Round:
    """Один раунд: собрать списки новых ссылок и пройти очередь один раз."""

    def __init__(self, conn: sqlite3.Connection, download_dir: str, workers: int, dead: DeadHosts):
        self.conn = conn
        self.download_dir = download_dir
        self.workers = max(1, min(workers, MAX_DISK_WORKERS))
        self.dead = dead
        self.counters = {"downloaded": 0, "failed": 0, "deferred": 0, "limited": 0, "gone": 0,
                         "link_errors": 0, "bytes": 0, "api_ok": 0, "api_fail": 0}
        self.aborted: str | None = None
        self.tried: set[tuple[int, str]] = set()
        self.limit_streak: dict[int, int] = {}
        self.local_streak = 0
        self._last_progress = 0.0
        self.total = 0

    # ── списки ──

    def list_links(self, relist: bool) -> None:
        now = time.time()
        rows = self.conn.execute(
            "SELECT * FROM links WHERE state='active' AND error_kind IS NULL "
            "AND (listed_at IS NULL OR listed_at < ? OR ?) ORDER BY id",
            (now - RELIST_SEC, 1 if relist else 0),
        ).fetchall()
        for link in rows:
            print(f"Собираю список {label(link)}...", flush=True)
            last = [0.0]

            def on_found(n: int, link=link) -> None:
                if time.monotonic() - last[0] >= PROGRESS_EVERY_SEC:
                    last[0] = time.monotonic()
                    print(f"Найдено: {n} ({label(link)})", flush=True)

            try:
                n = list_link(_thread_api(), self.conn, link, on_found)
            except LinkError as e:
                if e.link_level:
                    self._link_error(link["id"], e.kind, e.text)
                else:
                    self.counters["api_fail"] += 1
                    print(f"{e.kind} | {label(link)}: {e.text} — список соберу в следующий раз", flush=True)
                continue
            self.counters["api_ok"] += 1
            print(f"Найдено: {n} ({label(link)})", flush=True)

    def _link_error(self, link_id: int, kind: str, text: str) -> None:
        with sqlite_tx(self.conn):
            self.conn.execute("UPDATE links SET error_kind=?, error=? WHERE id=?", (kind, text, link_id))
        self.counters["link_errors"] += 1
        row = self.conn.execute("SELECT id, name FROM links WHERE id=?", (link_id,)).fetchone()
        print(f"link_error | {label(row) if row else f'#{link_id}'}: {text}", flush=True)

    # ── скачивание ──

    def _active_ok(self) -> set[int]:
        now = time.time()
        return {r[0] for r in self.conn.execute(
            "SELECT id FROM links WHERE state='active' AND error_kind IS NULL "
            "AND (limit_until IS NULL OR limit_until <= ?)", (now,))}

    def _apply(self, res: Result) -> None:
        job, c = res.job, self.counters
        key = (job.link_id, job.path)
        c["api_ok"] += res.api_ok
        c["api_fail"] += res.api_failed
        if res.status == "downloaded":
            c["downloaded"] += 1
            c["bytes"] += res.bytes_written
            self.limit_streak[job.link_id] = 0
            self.local_streak = 0
            self.conn.execute(
                "UPDATE link_files SET status='downloaded', local_path=?, error=NULL, storage_host=NULL, "
                "downloaded_at=? WHERE link_id=? AND path=?",
                (res.local_path, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), *key))
        elif res.status == "deferred":
            c["deferred"] += 1
            self.conn.execute(
                "UPDATE link_files SET status='pending', error=?, storage_host=?, "
                "preview_path=COALESCE(NULLIF(?, ''), preview_path) WHERE link_id=? AND path=?",
                (res.error, res.storage_host or None, res.preview_path, *key))
        elif res.status == "limited":
            c["limited"] += 1
            self.conn.execute("UPDATE link_files SET error=? WHERE link_id=? AND path=?", (res.error, *key))
            streak = self.limit_streak.get(job.link_id, 0) + 1
            self.limit_streak[job.link_id] = streak
            if streak >= LIMIT_STREAK:
                until = next_limit_reset(time.time())
                self.conn.execute("UPDATE links SET limit_until=? WHERE id=?", (until, job.link_id))
                row = self.conn.execute("SELECT id, name FROM links WHERE id=?", (job.link_id,)).fetchone()
                print(f"link_limit | {label(row)}: Яндекс ограничил скачивание по этой ссылке — "
                      f"продолжу в {_hhmm(until)}", flush=True)
        elif res.status == "gone":
            self._file_gone(job, res)
        elif res.status == "link_error":
            self._link_error(job.link_id, res.error_kind, res.error.split(" | ", 1)[-1])
        else:
            c["failed"] += 1
            local = res.error.startswith("local_disk |")
            self.local_streak = self.local_streak + 1 if local else 0
            self.conn.execute("UPDATE link_files SET status='failed', error=?, attempts=attempts+1 "
                              "WHERE link_id=? AND path=?", (res.error, *key))
            if self.local_streak >= LOCAL_DISK_GIVE_UP:
                self.aborted = "local_disk"

    def _file_gone(self, job: Job, res: Result) -> None:
        """Файла нет по ссылке: удалили один файл или закрыли всю ссылку? Один запрос — проверить."""
        try:
            _thread_api().meta(job.public_url, "/", limit=1)
        except LinkError as e:
            if e.link_level:
                self._link_error(job.link_id, e.kind, e.text)
                return
        self.counters["gone"] += 1
        self.conn.execute("UPDATE link_files SET status='gone', error=? WHERE link_id=? AND path=?",
                          (res.error, job.link_id, job.path))

    def _progress(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress < PROGRESS_EVERY_SEC:
            return
        self._last_progress = now
        c = self.counters
        done = c["downloaded"] + c["failed"] + c["deferred"] + c["limited"] + c["gone"]
        # «ошибок k» раньше «отложено» — тот же порядок, что у Диска
        print(f"Прогресс: {done}/{self.total} файлов | скачано {c['downloaded']} | ошибок {c['failed']} "
              f"| отложено {c['deferred'] + c['limited']} | {human_bytes(c['bytes'])}", flush=True)

    def _changed(self) -> bool:
        return meta_get(self.conn, "changed") is not None

    def download(self, jobs: list[Job]) -> bool:
        """Проходит очередь. True — по ходу добавили или убрали ссылки: нужен новый проход."""
        self.total += len(jobs)
        pending = deque(jobs)
        futures: dict = {}
        active = self._active_ok()
        checked_at = time.monotonic()
        changed = False
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while pending or futures:
                if time.monotonic() - checked_at >= 10:
                    checked_at = time.monotonic()
                    active = self._active_ok()
                    changed = changed or self._changed()
                while pending and len(futures) < self.workers * 2 and not self.aborted and not changed:
                    job = pending.popleft()
                    if job.link_id not in active:
                        continue  # ссылку убрали, сломалась или упёрлась в лимит
                    self.tried.add((job.link_id, job.path))
                    futures[pool.submit(_download_file, job, self.download_dir, self.dead)] = job
                if not futures:
                    break
                done, _ = wait(list(futures), timeout=WAKE_POLL_SEC, return_when=FIRST_COMPLETED)
                for fut in done:
                    futures.pop(fut)
                    res = fut.result()
                    self._apply(res)
                    if res.status in ("limited", "link_error") and (
                            self.limit_streak.get(res.job.link_id, 0) >= LIMIT_STREAK or res.status == "link_error"):
                        active.discard(res.job.link_id)  # остальные файлы этой ссылки сейчас не пробуем
                self._progress()
        self._progress(force=True)
        return changed and not self.aborted

    def run(self, relist: bool = False) -> None:
        while True:
            meta_set(self.conn, "changed", None)
            self.list_links(relist)
            relist = False
            jobs = [j for j in _take_jobs(self.conn, time.time()) if (j.link_id, j.path) not in self.tried]
            if not jobs:
                return
            if not self.download(jobs) or self.aborted:
                return

    def waiting(self) -> tuple[int, int]:
        """(файлов ждут Яндекса, из них — лимита) по активным ссылкам после раунда."""
        row = self.conn.execute(
            "SELECT COUNT(*), SUM(f.error LIKE 'link_limit |%' OR l.limit_until > ?) "
            "FROM link_files f JOIN links l ON l.id = f.link_id "
            "WHERE l.state='active' AND l.error_kind IS NULL AND f.status='pending'",
            (time.time(),),
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def unlisted(self) -> int:
        """Ссылки, чей список Яндекс пока не отдал."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM links WHERE state='active' AND error_kind IS NULL AND listed_at IS NULL"
        ).fetchone()[0]

    def exit_code(self) -> int:
        c = self.counters
        if self.aborted == "local_disk":
            return EXIT_DISK_FULL
        waiting, _limited = self.waiting()
        unlisted = self.unlisted()
        if (waiting or unlisted) and c["api_ok"] == 0 and c["api_fail"] > 0 and c["downloaded"] == 0:
            return EXIT_OUTAGE
        if waiting or unlisted:
            return EXIT_DEFERRED
        return EXIT_PARTIAL if c["failed"] or c["link_errors"] else EXIT_OK


def _retry_every() -> int:
    return safe_int(os.environ.get("YADISK_RETRY_EVERY_SEC"), 1800, 60, 24 * 3600)


def _nearest_limit(conn: sqlite3.Connection) -> float | None:
    row = conn.execute("SELECT MIN(limit_until) FROM links WHERE state='active' AND limit_until > ?",
                       (time.time(),)).fetchone()
    return row[0] if row and row[0] else None


def wait_until(conn: sqlite3.Connection, ts: float) -> bool:
    """Ждёт до ts. True — нажали «Попробовать сейчас» или добавили ссылку."""
    while time.time() < ts:
        if meta_get(conn, "wake") is not None or meta_get(conn, "changed") is not None:
            meta_set(conn, "wake", None)
            return True
        time.sleep(min(WAKE_POLL_SEC, max(0.0, ts - time.time())))
    return False


def run(conn: sqlite3.Connection, download_dir: str, workers: int, wait_mode: bool = False,
        relist: bool = False) -> int:
    dead = DeadHosts()
    purge_removed(conn)
    while True:
        meta_set(conn, "state", "running")
        meta_set(conn, "next_retry_at", None)
        rnd = Round(conn, download_dir, workers, dead)
        with keep_awake():
            rnd.run(relist)
        relist = False
        purge_removed(conn)
        code = rnd.exit_code()
        waiting, limited = rnd.waiting()
        if rnd.counters["deferred"]:
            print(f"storage_down | Отложено {rnd.counters['deferred']} файлов: их серверы хранения Яндекса "
                  f"сейчас не отвечают. Программа повторит их позже.", flush=True)
        if not wait_mode or code not in (EXIT_OUTAGE, EXIT_DEFERRED):
            meta_set(conn, "state", None)
            meta_set(conn, "next_retry_at", None)
            print("Готово: всё доступное по ссылкам скачано." if code == EXIT_OK else f"Раунд закончен (код {code}).",
                  flush=True)
            return code
        delay = float(_retry_every())
        until = _nearest_limit(conn)
        if until and waiting and waiting == limited and not rnd.unlisted():
            delay = max(60.0, until - time.time())  # ждут только лимита — до полуночи раунды бесполезны
        next_at = time.time() + delay
        meta_set(conn, "state", "waiting")
        meta_set(conn, "next_retry_at", str(next_at))
        print(f"Жду Яндекса: {_files(waiting)}. Следующая попытка в {_hhmm(next_at)}", flush=True)
        # Не даём компьютеру уснуть ради серверов, которые могут ожить, но не ради лимита до полуночи
        with keep_awake() if waiting > limited else nullcontext():
            if wait_until(conn, next_at):
                dead.clear()
                print("Пробую снова — по вашей просьбе", flush=True)


def cmd_status(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT l.id, l.name, l.state, l.error, COUNT(f.path) AS total, "
        "SUM(f.status='downloaded') AS done, SUM(f.status='pending') AS pending, SUM(f.status='failed') AS failed "
        "FROM links l LEFT JOIN link_files f ON f.link_id = l.id GROUP BY l.id ORDER BY l.id").fetchall()
    if not rows:
        print("Ссылок пока нет. Добавьте: public_download.py --add https://disk.yandex.ru/d/…")
    for r in rows:
        extra = f" — {r['error']}" if r["error"] else ""
        print(f"{label(r)} [{r['state']}]: скачано {r['done'] or 0} из {r['total']}, "
              f"ждут {r['pending'] or 0}, ошибок {r['failed'] or 0}{extra}")
    return EXIT_OK


def cmd_add(conn: sqlite3.Connection, url: str) -> int:
    try:
        public_url, sub = normalize_public_url(url)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return EXIT_PARTIAL
    meta = None
    try:
        meta = PublicApi().meta(public_url, sub, limit=1)
    except LinkError as e:
        if e.link_level:
            print(e.text, file=sys.stderr)
            return EXIT_PARTIAL
        print(f"{e.text} — ссылку всё равно добавляю, список соберу позже")
    link_id, new = add_link(conn, public_url, sub, meta)
    row = conn.execute("SELECT id, name FROM links WHERE id=?", (link_id,)).fetchone()
    print(f"{'Добавил' if new else 'Уже в списке'}: {label(row)}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    p = argparse.ArgumentParser(prog="public_download", description="Скачивание по публичным ссылкам Яндекс Диска")
    p.add_argument("--add", metavar="ССЫЛКА", help="добавить ссылку в список")
    p.add_argument("--status", action="store_true", help="показать список ссылок")
    p.add_argument("--wait", action="store_true", help="повторять отложенное раундами, пока не скачается всё")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"потоков (макс {MAX_DISK_WORKERS})")
    p.add_argument("--relist", action="store_true", help="собрать списки ссылок заново")
    args = p.parse_args(argv)
    load_dotenv(ENV_FILE)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    conn = connect_db()
    try:
        if args.add:
            return cmd_add(conn, args.add)
        if args.status:
            return cmd_status(conn)
        download_dir = (os.getenv("DOWNLOAD_DIR") or str(PROJECT / "downloads")).strip()
        target = os.path.join(download_dir, LINKS_DIRNAME)
        try:
            ensure_dir(to_long_path(target))
        except OSError as e:
            print(f"local_disk | Не получается создать папку {target}: {e}. "
                  f"Проверьте, что диск подключён, или выберите другую папку.", flush=True)
            return EXIT_DISK_FULL
        print(f"Скачиваю по ссылкам в {target}", flush=True)
        return run(conn, download_dir, args.workers, wait_mode=args.wait, relist=args.relist)
    except KeyboardInterrupt:
        print("Прервано. Уже скачанное сохранено.", flush=True)
        return EXIT_INTERRUPTED
    finally:
        try:
            meta_set(conn, "state", None)
        except sqlite3.Error:
            pass
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
