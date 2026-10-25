"""Скачивание папки, файла или альбома по публичной ссылке Яндекс Диска.

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
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

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
    MAX_NAME_LEN,
    avoid_case_collision,
    categorize,
    ensure_dir,
    ensure_utf8_stdio,
    files_text,
    hhmm,
    human_bytes,
    is_sqlite_disk_error,
    keep_awake,
    local_copy_matches,
    local_error_kind,
    name_with_tag,
    part_path_for,
    path_key,
    preview_path_for,
    previews_enabled,
    remove_preview,
    remove_quiet,
    resolve_local_path,
    retry_every_sec,
    sanitize_component,
    short_hash,
    sqlite_tx,
    to_long_path,
    too_big_for_folder,
    under_webui,
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
# Сколько страниц отдельных фото альбома поток скачивания открывает за раунд ради превью, если в
# списке альбома ссылки на превью не нашлось: страница — целый HTML, сотни подряд похожи на робота
ALBUM_PREVIEW_PAGES = 5
_ALBUM_ITEM_ID = re.compile(r"[a-f\d]{24}", re.I)
_CAPTCHA_TEXT = "Яндекс попросил проверку на робота — программа попробует снова позже"

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

    Принимает disk.yandex.ru/d/КЛЮЧ, /i/ (файл), /a/ (альбом), yadi.sk, другие домены
    Яндекса и ссылку на подпапку (…/d/КЛЮЧ/Подпапка — тогда качается только она). Ошибка —
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
        # Ссылка, скопированная при просмотре одного фото (…/a/КЛЮЧ/ID), ведёт в тот же альбом
        if len(parts) < 2 or not re.fullmatch(r"[\w-]+", parts[1], flags=re.ASCII):
            raise ValueError("Вставьте ссылку на альбом: disk.yandex.ru/a/КЛЮЧ")
        return f"https://disk.yandex.ru/a/{parts[1]}", "/"
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
    """Публичный API не дал того, что нужно. kind — для логики, text — для человека.

    hold — Яндекс просит паузы (проверка на робота, 429, отказ веб-клиента альбомов): ссылку
    не трогаем до следующего раунда, а не повторяем каждый её файл.
    """

    def __init__(self, kind: str, text: str, hold: bool = False):
        super().__init__(text)
        self.kind = kind
        self.text = text
        self.hold = hold

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


def _is_album(public_url: str) -> bool:
    """Ссылка на альбом (disk.yandex.ru/a/…): её обслуживает веб-клиент, а не публичный API."""
    return urlsplit(public_url).path.startswith("/a/")


class PublicApi:
    """Публичный API без ключа: заголовка Authorization нет вовсе."""

    def __init__(self, session=None, timeout=API_TIMEOUT):
        self.session = session or _new_session()
        self.timeout = timeout
        self._album = None

    def _send(self, method: str, url: str, **kwargs):
        """Запрос без разбора кода ответа: таймаут и обрыв связи — LinkError."""
        try:
            return getattr(self.session, method)(url, timeout=self.timeout, **kwargs)
        except requests.exceptions.Timeout:
            raise LinkError("timeout", "Яндекс отвечал слишком долго") from None
        except requests.exceptions.RequestException:
            raise LinkError("network", "Нет связи с Яндексом — проверьте интернет") from None

    def _request(self, method: str, url: str, **kwargs):
        resp = self._send(method, url, **kwargs)
        if resp.status_code != 200:
            raise _error_for(resp)
        return resp

    @staticmethod
    def _json(resp) -> dict:
        try:
            data = resp.json()
        except ValueError:
            raise LinkError("server_50x", "Яндекс ответил непонятно") from None
        if not isinstance(data, dict):
            raise LinkError("server_50x", "Яндекс ответил непонятно")
        return data

    def _get(self, url: str, params: dict) -> dict:
        return self._json(self._request("get", url, params=params))

    def _album_api(self):
        if self._album is None:
            self._album = PublicAlbumApi(self.session, self.timeout)
        return self._album

    def meta(self, public_url: str, path: str = "/", limit: int | None = None, offset: int = 0,
             preview_size: str | None = None) -> dict:
        if _is_album(public_url):
            return self._album_api().meta(public_url, path, limit, offset, preview_size)
        params = {"public_key": public_url, "limit": LIST_LIMIT if limit is None else limit, "offset": offset}
        if path and path != "/":
            params["path"] = path
        if preview_size:
            params.update(preview_size=preview_size, preview_crop="false")
        return self._get(API, params)

    def download_href(self, public_url: str, path: str = "/") -> str:
        if _is_album(public_url):
            return self._album_api().download_href(public_url, path)
        params = {"public_key": public_url}
        if path and path != "/":
            params["path"] = path
        href = self._get(API + "/download", params).get("href")
        if not href:
            raise LinkError("server_50x", "Яндекс не выдал ссылку на файл")
        return href


# Ссылки на превью фото альбомов из их списка: (адрес альбома, ID фото) → ссылка. Список собирает
# главный поток, а превью сохраняют потоки скачивания. Только в памяти: ссылки временные
_album_previews: dict[tuple[str, str], str] = {}


@contextmanager
def _album_data():
    """Данные альбома не того вида — LinkError: раунд не падает, а webui не отвечает ошибкой 500."""
    try:
        yield
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        raise LinkError("server_50x", "Яндекс отдал непонятные данные альбома") from None


def _album_item(path: str) -> tuple[str, str | None]:
    """Путь фото альбома в базе («/имя__ID.jpg») → (имя файла у Яндекса, ID фото или None)."""
    stem, ext = os.path.splitext(path.rsplit("/", 1)[-1])
    name, sep, item_id = stem.rpartition("__")
    if not sep or not _ALBUM_ITEM_ID.fullmatch(item_id):
        return stem + ext, None
    return name + ext, item_id


def album_file_name(path: str) -> str:
    """Имя фото альбома на диске: очищенное имя с ID фото.

    В базе имя лежит как у Яндекса: правка очистки имён меняет только место на диске, а не
    запись, и новый сбор списка не качает дубликаты. Длину меряет name_len, как файловая
    система (на Linux и macOS — байты UTF-8), с запасом под хвосты, которые допишет
    программа: хеш при совпадении имён и «.jpg» у превью.
    """
    name, item_id = _album_item(path)
    clean = sanitize_component(name)
    return name_with_tag(clean, f"__{item_id}", MAX_NAME_LEN - 16) if item_id else clean


def _album_md5(meta: dict) -> str | None:
    """MD5 фото: meta.md5 или etag в ссылке на оригинал. None — Яндекс его не сообщил."""
    md5 = meta.get("md5")
    if not isinstance(md5, str) or not md5:
        original = meta.get("original")
        try:
            md5 = parse_qs(urlsplit(original).query).get("etag", [""])[0].strip('"') if isinstance(original, str) else ""
        except ValueError:
            md5 = ""
    return md5.lower() if re.fullmatch(r"[a-f\d]{32}", md5, flags=re.I) else None


class PublicAlbumApi(PublicApi):
    """Адаптер веб-клиента альбомов: анонимная сессия, курсор и свежие ссылки.

    REST API папок не знает альбомы. В SQLite хранятся только имя (как у Яндекса), размер,
    MD5 и ID фото в пути; cookies, sk и временные ссылки остаются в памяти. Проверка на
    робота, 429 и отказы веб-клиента — LinkError с hold: ссылка ждёт следующего раунда.
    """

    def __init__(self, session=None, timeout=API_TIMEOUT):
        super().__init__(session, timeout)
        self._states: dict[str, dict] = {}
        # У каждого потока раунда свой клиент (_thread_api) — и свой запас страниц фото для превью
        self._preview_pages = ALBUM_PREVIEW_PAGES

    @staticmethod
    def _failure(code: int, missing: str = "Яндекс не нашёл данные альбома") -> LinkError:
        """Код ответа веб-клиента — HTTP или statusCode в JSON — в LinkError."""
        if code in (404, 410):
            return LinkError("missing", missing)
        if code == 429:
            return LinkError("rate_limit", "Яндекс просит обращаться к альбому реже — программа попробует снова позже",
                             hold=True)
        if 400 <= code < 500:
            return LinkError("refused", f"Яндекс отклонил запрос к альбому ({code}) — программа попробует снова позже",
                             hold=True)
        return LinkError("server_50x", "Яндекс сейчас не отдал данные альбома")

    def _store(self, url: str) -> dict:
        resp = self._send("get", url)
        if resp.status_code in (401, 403):
            resp = self._send("get", url)  # отказ сессии, а не владельца: ещё раз
        if resp.status_code in (404, 410):
            raise LinkError("not_found", "Ссылка на альбом больше не работает: её удалили или закрыли")
        if resp.status_code != 200:
            raise self._failure(resp.status_code)
        resp.encoding = "utf-8"
        match = re.search(r'''<script\b(?=[^>]*\bid=["']store-prefetch["'])[^>]*>(.*?)</script>''',
                          resp.text, flags=re.S | re.I)
        if not match:
            raise LinkError("captcha", "Яндекс не отдал данные альбома: попросил проверку на робота или изменил "
                                       "сайт — программа попробует снова позже", hold=True)
        try:
            store = json.loads(match.group(1))
            if not isinstance(store, dict) or not isinstance(store.get("resources"), dict):
                raise ValueError
        except (TypeError, ValueError):
            raise LinkError("server_50x", "Яндекс отдал непонятные данные альбома") from None
        return store

    def _load(self, public_url: str) -> dict:
        """Страница альбома → состояние обхода: имя, hash, sk и первая порция фото."""
        store = self._store(public_url)
        resources = store["resources"]
        root_id = store.get("rootResourceId")
        root = resources.get(root_id) if isinstance(root_id, str) else None
        root = root if isinstance(root, dict) else {}
        if root.get("errorCode") in (404, 410):
            raise LinkError("not_found", "Ссылка на альбом больше не работает: её удалили или закрыли")
        if root.get("errorCode") in (401, 403):
            raise LinkError("forbidden", "Владелец закрыл доступ к альбому")
        env = store.get("environment")
        sk = env.get("sk") if isinstance(env, dict) else None
        if root.get("type") != "album" or not root.get("hash") or not sk:
            raise LinkError("server_50x", "Яндекс не отдал сведения об альбоме")
        children = root.get("children")
        if not isinstance(children, list) or not isinstance(root.get("completed"), bool):
            raise LinkError("server_50x", "Яндекс не отдал список файлов альбома")
        name = root.get("name")
        return {"name": name if isinstance(name, str) else None, "hash": root["hash"], "sk": sk,
                # Фото без сведений в resources — пропуск, как и фото с неполными сведениями
                "items": [resources.get(key) if isinstance(key, str) else None for key in children],
                "completed": root["completed"], "cursors": {}, "seen": set(), "at": time.monotonic()}

    def _state(self, public_url: str, refresh: bool = False) -> dict:
        state = self._states.get(public_url)
        if state is not None and not refresh and time.monotonic() - state["at"] < 300:
            return state
        state = self._states[public_url] = self._load(public_url)
        return state

    def _renew(self, public_url: str, state: dict) -> None:
        """Свежие hash и sk в том же состоянии: курсор обхода и уже виденные фото остаются."""
        fresh = self._load(public_url)
        state.update(hash=fresh["hash"], sk=fresh["sk"], at=fresh["at"])

    def _post(self, public_url: str, action: str, params: dict, state: dict | None = None) -> dict:
        state = state if state is not None else self._state(public_url)
        # 404 здесь — ответ про одно фото или страницу списка, а не про весь альбом: его страница
        # открылась. Удалённым фото считает только полный список альбома (list_link)
        missing = ("Яндекс не нашёл это фото в альбоме" if action == "album-download-url"
                   else "Яндекс не отдал продолжение списка альбома")
        renewed = False
        while True:
            body = quote(json.dumps({**params, "hash": state["hash"], "sk": state["sk"]}), safe="~()*!.'")
            resp = self._send("post", f"https://disk.yandex.ru/public/api/{action}", data=body,
                              headers={"Content-Type": "text/plain", "X-Requested-With": "XMLHttpRequest",
                                       "X-Retpath-Y": public_url})
            code = resp.status_code
            if code == 200:
                data = self._json(resp)
                if data.get("type") == "captcha" or data.get("captcha"):
                    raise LinkError("captcha", _CAPTCHA_TEXT, hold=True)
                status = data.get("statusCode")
                if isinstance(status, int) and status >= 400:
                    code = status
                elif data.get("error"):
                    code = 500  # ошибка без понятного кода — сбой Яндекса
                else:
                    return data
            if code in (401, 403) and not renewed:
                # Отказ сессии, а не владельца (он — errorCode на странице альбома): свежие sk и hash
                self._renew(public_url, state)
                renewed = True
                continue
            raise self._failure(code, missing)

    @staticmethod
    def _item_id(path: str) -> str:
        item_id = _album_item(path)[1]
        if not item_id:
            raise LinkError("missing", "В записи нет ID фото альбома — сверю её со списком альбома")
        return item_id

    @staticmethod
    def _file(resource) -> dict | None:
        """Фото из списка альбома или None, если Яндекс отдал о нём не всё: такое фото — пропуск."""
        if not isinstance(resource, dict) or not isinstance(resource.get("meta"), dict):
            return None
        item_id, meta = resource.get("albumItemId"), resource["meta"]
        size = meta.get("size")
        if (resource.get("type") != "file" or not isinstance(item_id, str) or not _ALBUM_ITEM_ID.fullmatch(item_id)
                or not isinstance(size, int) or isinstance(size, bool) or size < 0):
            return None
        # В базе — имя как у Яндекса и ID фото: ID различает одинаковые имена и не зависит от
        # порядка страниц, а имя чистится только для диска (album_file_name). «/» и «\» меняем
        # уже здесь: путь в базе — из одной части
        name = str(resource.get("name") or "Файл").replace("/", "_").replace("\\", "_")
        stem, ext = os.path.splitext(name)
        preview = meta.get("xxxlPreview") or meta.get("defaultPreview")
        return {"type": "file", "id": item_id, "path": f"/{stem}__{item_id}{ext}", "size": size,
                "md5": _album_md5(meta), "preview": preview if isinstance(preview, str) else None}

    def _preview(self, public_url: str, path: str) -> dict:
        """Превью фото — из списка альбома. Нет его там — со страницы фото, но не больше
        ALBUM_PREVIEW_PAGES страниц на поток за раунд: ради превью сотни страниц не открываем."""
        item_id = self._item_id(path)
        url = _album_previews.get((public_url, item_id))
        if url or self._preview_pages <= 0:
            return {"preview": url}
        self._preview_pages -= 1
        try:
            store = self._store(f"{public_url}/{item_id}")
        except LinkError as e:
            if e.hold:
                self._preview_pages = 0  # Яндекс просит паузы — страниц фото до конца раунда не открываем
            raise
        slider = (store.get("overlays") or {}).get("sliderResourceId")
        meta = (store["resources"].get(slider) or {}).get("meta") or {}
        url = meta.get("xxxlPreview") or meta.get("defaultPreview")
        return {"preview": url if isinstance(url, str) else None}

    def meta(self, public_url: str, path: str = "/", limit: int | None = None, offset: int = 0,
             preview_size: str | None = None) -> dict:
        with _album_data():
            if path != "/":
                return self._preview(public_url, path)
            # Не обновляем начальную страницу посередине долгого обхода: это потеряет курсор.
            state = self._state(public_url, refresh=True) if offset == 0 else self._states.get(public_url)
            if state is None:
                raise LinkError("server_50x", "Не найдено начало списка альбома")
            if offset == 0:
                items, completed = state["items"], state["completed"]
            else:
                cursor = state["cursors"].get(offset)
                if not cursor:
                    raise LinkError("server_50x", "Не найдено продолжение списка альбома")
                data = self._post(public_url, "fetch-album-list", {"lastItemId": cursor}, state)
                items, completed = data.get("resources"), data.get("completed")
            if not isinstance(items, list) or not isinstance(completed, bool) or (not items and not completed):
                raise LinkError("server_50x", "Яндекс не отдал весь список альбома")
            # Фото с неполными сведениями пропускаем и считаем: по такому списку list_link
            # не считает исчезнувшим ничего, а остальное качается
            files = [f for f in map(self._file, items) if f]
            ids = {f["id"] for f in files}
            if len(ids) != len(files) or ids & state["seen"]:
                raise LinkError("server_50x", "Яндекс повторил файлы в списке альбома")
            state["seen"].update(ids)
            for f in files:
                if f["preview"]:
                    _album_previews[(public_url, f["id"])] = f["preview"]
            if not completed:
                last = items[-1]
                cursor = last.get("albumItemId") if isinstance(last, dict) else None
                if not isinstance(cursor, str) or not cursor:
                    raise LinkError("server_50x", "Яндекс не отдал продолжение списка альбома")
                if cursor in state["cursors"].values():
                    raise LinkError("server_50x", "Яндекс повторил страницу альбома")
                # Ключ — сколько элементов пройдено, с пропущенными: list_link считает так же
                state["cursors"][offset + len(items)] = cursor
            return {"type": "album", "name": state["name"],
                    "_embedded": {"items": files, "completed": completed, "skipped": len(items) - len(files)}}

    def download_href(self, public_url: str, path: str = "/") -> str:
        with _album_data():
            data = self._post(public_url, "album-download-url", {"itemId": self._item_id(path)})
            href = (data.get("data") or {}).get("url")
        if not isinstance(href, str) or not href:
            raise LinkError("server_50x", "Яндекс не выдал ссылку на оригинал из альбома")
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

    Файлы, которых больше нет, помечаются 'gone' — только после полного обхода без
    пропусков. LinkError решает вызывающий.
    """
    gen = int(link["gen"] or 0) + 1
    root = link["root_path"] or "/"
    found = 0
    skipped = 0  # фото альбома, о которых Яндекс отдал не всё
    batch: list[tuple] = []
    first = api.meta(link["public_url"], root)
    kind = first.get("type") or "dir"
    name = first.get("name") or link["name"]
    if kind == "file":
        # Ссылка на файл внутри папки (…/d/KEY/Папка/файл.jpg) хранит путь файла: с «/»
        # Яндекс отдал бы всю папку по ссылке, а не этот файл
        batch.append((root, first.get("size"), first.get("md5")))
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
                skipped += emb.get("skipped", 0)
                offset += len(items) + emb.get("skipped", 0)
                total = emb.get("total")
                if "completed" in emb:
                    if emb["completed"]:
                        break
                elif not items or (total is not None and offset >= int(total)) or (total is None and len(items) < LIST_LIMIT):
                    break
    _upsert_files(conn, link["id"], gen, batch)
    if skipped:
        print(f"Пропущено {files_text(skipped)} ({label(link)}): Яндекс отдал о них не всё — "
              f"проверю их при следующем сборе списка", flush=True)
    with sqlite_tx(conn):
        if not skipped:  # список с пропусками неполон: исчезнувшим по нему не считаем ничего
            conn.execute("UPDATE link_files SET status='gone' WHERE link_id=? AND gen<>? "
                         "AND status IN ('pending','failed')", (link["id"], gen))
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
    local: str = ""  # место на диске на этот проход (Round._assign_targets)


@dataclass
class Result:
    job: Job
    status: str  # downloaded | deferred | failed | limited | held | gone | link_error
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
    if _is_album(job.public_url):
        return os.path.join(base, album_file_name(job.path))
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


def drop_previews(conn: sqlite3.Connection, download_dir: str, previews: list[tuple[int, str, str]]) -> int:
    """Удаляет превью файлов, чьи оригиналы скачаны: (link_id, path, путь превью). Сколько удалено.

    Никогда не бросает: уборка не должна ронять скачивание. Неудалённое превью (файл
    открыт в программе просмотра) остаётся записанным, его уберёт следующий раунд.
    """
    try:
        gone = [p for p in previews if remove_preview(p[2], download_dir)]
        if gone:
            with sqlite_tx(conn):
                conn.executemany("UPDATE link_files SET preview_path=NULL "
                                 "WHERE link_id=? AND path=? AND preview_path=?", gone)
        if len(gone) < len(previews):
            log.warning("не удалено превью: %d (файл занят или лежит не в «_превью»)", len(previews) - len(gone))
        return len(gone)
    except Exception as e:  # noqa: BLE001 — уборка не должна ронять скачивание
        log.warning("уборка превью: %s", type(e).__name__)  # в тексте ошибки — путь с именем ссылки
        return 0


def sweep_previews(conn: sqlite3.Connection, download_dir: str) -> int:
    """Превью уже скачанных файлов, которые не удалились в прошлый раз, — удалить."""
    try:
        rows = conn.execute("SELECT link_id, path, preview_path FROM link_files "
                            "WHERE status='downloaded' AND preview_path IS NOT NULL").fetchall()
    except sqlite3.Error as e:
        log.warning("уборка превью: %s", e)
        return 0
    return drop_previews(conn, download_dir, [(r[0], r[1], r[2]) for r in rows])


def _download_file(job: Job, download_dir: str, dead: DeadHosts) -> Result:
    """Скачивает один файл ссылки. Никогда не бросает: любой исход — Result."""
    api = _thread_api()
    local = ""
    part = ""
    try:
        local = avoid_case_collision(job.local or local_target(download_dir, job))
        parent = os.path.dirname(local)
        if parent:
            ensure_dir(to_long_path(parent))
        if local_copy_matches(local, job.size, job.md5):
            return Result(job, "downloaded", local)
        if why := too_big_for_folder(parent or download_dir, job.size):
            return Result(job, "failed", error=f"local_file | {why}")
        if job.storage_host and dead.is_dead(job.storage_host):
            # Сервер этого файла недавно молчал — не беспокоим Яндекс до следующего раунда
            return Result(job, "deferred", error=f"{DEFERRED_MARKER}сервер хранения {job.storage_host} недавно не отвечал",
                          storage_host=job.storage_host)
        # Не «имя.part»: в чужой папке такое имя бывает у настоящего файла, и его затёрло бы
        part = to_long_path(part_path_for(local))
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
                if e.kind in ("limit", "not_found", "missing") or e.link_level or e.hold:
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
            if failure.is_local or failure.is_local_file or attempt + 1 >= tries:
                break
            time.sleep(10 if failure.kind == "rate_limit" else 2 * (attempt + 1))
        remove_quiet(part)
        if api_error is not None:
            # Яндекс не выдал ссылку на файл — это сбой Яндекса, а не файла: ждём раунда
            return Result(job, "deferred", error=f"{DEFERRED_MARKER}{api_error.text}",
                          api_ok=api_ok, api_failed=not api_ok)
        if failure is not None and (failure.is_local or failure.is_local_file):
            return Result(job, "failed", error=f"{failure.kind} | {failure.text}", api_ok=api_ok)
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
        if e.hold:
            # Проверка на робота, 429, отказ: не повторяем — остальные файлы ссылки ждут раунда
            return Result(job, "held", error=f"{e.kind} | {e.text}", error_kind=e.kind, api_failed=True)
        if e.kind == "missing":
            # Фото альбома не нашлось: удалённым его считает только полный список альбома,
            # а не ответ про одно фото — он бывает и у живого фото (смена адреса, устаревший hash)
            return Result(job, "deferred", error=f"{DEFERRED_MARKER}{e.text}", api_ok=True)
        if e.kind == "not_found" and not _is_album(job.public_url):
            return Result(job, "gone", error=f"not_found | {e.text}", api_ok=True)
        # У альбома not_found — сама его страница: альбома больше нет
        return Result(job, "link_error", error=f"{e.kind} | {e.text}", error_kind=e.kind, api_ok=True)
    except OSError as e:
        if part:
            remove_quiet(part)
        return Result(job, "failed", error=f"{local_error_kind(e)} | {type(e).__name__}: {e}")
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
        self.counters = {"downloaded": 0, "failed": 0, "deferred": 0, "limited": 0, "held": 0, "gone": 0,
                         "link_errors": 0, "bytes": 0, "api_ok": 0, "api_fail": 0}
        self.aborted: str | None = None
        self.tried: set[tuple[int, str]] = set()
        self.limit_streak: dict[int, int] = {}
        # Ссылки, которые до конца раунда не трогаем: Яндекс просит паузы (LinkError.hold)
        self.held: set[int] = set()
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
            if link["id"] in self.held:
                continue
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
                elif e.hold:
                    self.counters["api_fail"] += 1
                    self._hold(link["id"], e.kind, e.text)  # и файлы ссылки в этом раунде не трогаем
                else:
                    self.counters["api_fail"] += 1
                    if link["listed_at"] is None:
                        self._note(link["id"], e.text)  # иначе карточка скажет только «список не получен»
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

    def _note(self, link_id: int, text: str) -> None:
        """Почему исправная ссылка стоит — для её карточки: error без error_kind. Раунд сбросит."""
        self.conn.execute("UPDATE links SET error=? WHERE id=? AND error_kind IS NULL", (text, link_id))

    def _hold(self, link_id: int, kind: str, text: str) -> None:
        """Яндекс просит паузы (проверка на робота, 429, отказ): ссылку — до следующего раунда."""
        if link_id in self.held:
            return
        self.held.add(link_id)
        self._note(link_id, text)
        row = self.conn.execute("SELECT id, name FROM links WHERE id=?", (link_id,)).fetchone()
        print(f"{kind} | {label(row) if row else f'#{link_id}'}: {text}", flush=True)

    # ── скачивание ──

    def _active_ok(self) -> set[int]:
        now = time.time()
        return {r[0] for r in self.conn.execute(
            "SELECT id FROM links WHERE state='active' AND error_kind IS NULL "
            "AND (limit_until IS NULL OR limit_until <= ?)", (now,))} - self.held

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
            row = self.conn.execute("SELECT preview_path FROM link_files WHERE link_id=? AND path=?", key).fetchone()
            self.conn.execute(
                "UPDATE link_files SET status='downloaded', local_path=?, error=NULL, storage_host=NULL, "
                "downloaded_at=? WHERE link_id=? AND path=?",
                (res.local_path, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), *key))
            if row and row[0]:
                # Оригинал на месте — страховочное превью больше не нужно
                drop_previews(self.conn, self.download_dir, [(*key, row[0])])
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
                      f"продолжу в {hhmm(until)}", flush=True)
        elif res.status == "held":
            c["held"] += 1  # файл не тронут: ждёт следующего раунда вместе со всей ссылкой
            self._hold(job.link_id, res.error_kind, res.error.split(" | ", 1)[-1])
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
        done = c["downloaded"] + c["failed"] + c["deferred"] + c["limited"] + c["held"] + c["gone"]
        # «ошибок k» раньше «отложено» — тот же порядок, что у Диска
        print(f"Прогресс: {done}/{self.total} файлов | скачано {c['downloaded']} | ошибок {c['failed']} "
              f"| отложено {c['deferred'] + c['limited'] + c['held']} | {human_bytes(c['bytes'])}", flush=True)

    def _changed(self) -> bool:
        return meta_get(self.conn, "changed") is not None

    def _assign_targets(self, jobs: list[Job]) -> None:
        """Каждому файлу — своё место на диске. «Docs/a.txt» и «docs/a.txt» в чужой папке на
        Windows и macOS — один и тот же файл: второй затёр бы первый. Он получает хеш в имени."""
        claimed = {path_key(r[0]) for r in self.conn.execute(
            "SELECT local_path FROM link_files WHERE status='downloaded' AND local_path IS NOT NULL")}
        for job in sorted(jobs, key=lambda j: (j.link_id, j.path)):
            local = local_target(self.download_dir, job)
            if path_key(local) in claimed:
                folder, name = os.path.split(local)
                local = os.path.join(folder, name_with_tag(name, f"_{short_hash(f'{job.link_id}:{job.path}')}"))
            claimed.add(path_key(local))
            job.local = local

    def download(self, jobs: list[Job]) -> bool:
        """Проходит очередь. True — по ходу добавили или убрали ссылки: нужен новый проход."""
        self._assign_targets(jobs)
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
                    if res.status in ("held", "link_error") or (
                            res.status == "limited" and self.limit_streak.get(res.job.link_id, 0) >= LIMIT_STREAK):
                        active.discard(res.job.link_id)  # остальные файлы этой ссылки сейчас не пробуем
                self._progress()
        self._progress(force=True)
        return changed and not self.aborted

    def run(self, relist: bool = False) -> None:
        # Причины прошлого раунда (_note) устарели: ссылки пробуем снова — новые запишутся сами
        self.conn.execute("UPDATE links SET error=NULL WHERE error_kind IS NULL AND error IS NOT NULL")
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
    sweep_previews(conn, download_dir)
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
            print(f"storage_down | Отложено {files_text(rnd.counters['deferred'])}: их серверы хранения Яндекса "
                  f"сейчас не отвечают. Файлы не потеряны — их скачает следующая попытка.", flush=True)
            if not wait_mode and not under_webui():
                print("  Повторить их: python public_download.py --wait — программа будет пробовать их сама "
                      "каждые 30 минут.", flush=True)
        if not wait_mode or code not in (EXIT_OUTAGE, EXIT_DEFERRED):
            meta_set(conn, "state", None)
            meta_set(conn, "next_retry_at", None)
            print("Готово: всё доступное по ссылкам скачано." if code == EXIT_OK else f"Раунд закончен (код {code}).",
                  flush=True)
            return code
        delay = float(retry_every_sec())
        until = _nearest_limit(conn)
        if until and waiting and waiting == limited and not rnd.unlisted():
            delay = max(60.0, until - time.time())  # ждут только лимита — до полуночи раунды бесполезны
        next_at = time.time() + delay
        meta_set(conn, "state", "waiting")
        meta_set(conn, "next_retry_at", str(next_at))
        print(f"Жду Яндекса: {files_text(waiting)}. Следующая попытка в {hhmm(next_at)}", flush=True)
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
    except sqlite3.OperationalError as e:
        if not is_sqlite_disk_error(e):
            raise
        # База лежит на том же диске, что и файлы: он кончился — это код 4, а не падение
        print(f"local_disk | Не получается записать базу links_state.db: {e}. Освободите место "
              f"или проверьте, что диск подключён.", flush=True)
        return EXIT_DISK_FULL
    finally:
        try:
            meta_set(conn, "state", None)
        except sqlite3.Error:
            pass
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
