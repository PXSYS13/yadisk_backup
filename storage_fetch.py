"""Скачивание одного файла с серверов хранения Яндекса — общая часть Диска и безлимита.

Ссылка Яндекса (downloader.disk.yandex.ru) ведёт редиректом на конкретный сервер
хранения (s123klg.storage.yandex.net). После атак на дата-центры в октябре 2026 часть
таких серверов молчит: ни REST API, ни веб-версия не получают от них ни байта. Поэтому
редирект разбирается здесь вручную: сервер, который недавно молчал, пропускается сразу,
а новый молчащий сервер даёт отказ за STORAGE_TIMEOUT_SEC, а не за минуты.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests

from utils import local_error_kind, scrub_urls

# Живой сервер хранения отвечает меньше чем за секунду, молчащий — не отвечает никогда
STORAGE_TIMEOUT_SEC = 10
# Сколько молчащий сервер считается недоступным, прежде чем его файлы пробуются снова
STORAGE_RETRY_SEC = 1800
# Ссылка на скачивание (первый шаг, до редиректа)
LINK_TIMEOUT = (30, 60)

_REDIRECT_CODES = (301, 302, 303, 307, 308)
# Оригинал фото или видео никогда не приходит страницей или JSON — это отказ Яндекса.
# Для обычного Диска проверка выключается: там бывают настоящие .html и .json.
_PAGE_CONTENT_TYPES = ("text/html", "application/json")


def response_trace(r) -> str:
    """Идентификаторы ответа Яндекса для логов — по ним поддержка Яндекса находит запрос."""
    headers = getattr(r, "headers", None) or {}
    parts = []
    for h in ("x-request-id", "x-yandex-req-id", "yandexuid-req-id"):
        v = headers.get(h) if hasattr(headers, "get") else None
        if v:
            parts.append(f"{h}={v}")
            break
    ctype = headers.get("content-type") if hasattr(headers, "get") else None
    if ctype:
        parts.append(f"type={str(ctype).split(';')[0]}")
    return f" ({', '.join(parts)})" if parts else ""


@dataclass(frozen=True)
class FetchFailure:
    """Почему архив или файл не скачался (или архив не распаковался).

    kind: local_disk | local_file (этот файл не записать в папку: длинное имя, FAT32) |
          not_zip | bad_zip | bad_file | server_50x | rate_limit | refused |
          timeout | connection | storage_down (сервер хранения не ответил) |
          storage_skip (он же уже известен как недоступный). str() даёт
          'категория | текст' для логов и Web UI.
    """

    kind: str
    text: str
    host: str = ""  # сервер хранения — для storage_down / storage_skip

    def __str__(self) -> str:
        return f"{self.kind} | {self.text}"

    @property
    def is_local(self) -> bool:
        return self.kind == "local_disk"

    @property
    def is_local_file(self) -> bool:
        """Не записать именно этот файл (имя, размер для FAT32): диск в порядке, качать дальше."""
        return self.kind == "local_file"

    @property
    def is_server_issue(self) -> bool:
        return self.kind in ("not_zip", "server_50x", "rate_limit", "refused", "timeout", "connection")

    @property
    def is_storage(self) -> bool:
        """Файл лежит на недоступном сервере хранения — отложить, а не считать ошибкой."""
        return self.kind in ("storage_down", "storage_skip")


class DeadHosts:
    """Молчащие серверы хранения в памяти одного процесса (потоки main.py download).

    Воркеры безлимита — отдельные процессы, у них общий реестр в unlim_state.db.
    """

    def __init__(self, ttl: float = STORAGE_RETRY_SEC):
        self.ttl = ttl
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def mark(self, host: str) -> None:
        if host:
            with self._lock:
                self._until[host] = time.monotonic() + self.ttl

    def is_dead(self, host: str) -> bool:
        with self._lock:
            until = self._until.get(host)
            if until is None:
                return False
            if until <= time.monotonic():
                del self._until[host]
                return False
            return True

    def count(self) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(1 for u in self._until.values() if u > now)

    def clear(self) -> None:
        with self._lock:
            self._until.clear()


def is_md5(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value.lower()) is not None


def fetch_to_file(
    session, url: str, tmp_path: str, *,
    expected_size: int | None = None,
    expected_md5: str | None = None,
    host_is_dead: Callable[[str], bool] | None = None,
    storage_timeout: float = STORAGE_TIMEOUT_SEC,
    reject_pages: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str, FetchFailure | None]:
    """Скачивает файл по ссылке Яндекса в tmp_path. Возвращает (байт, сервер, сбой | None).

    expected_size: None — размер неизвестен (пустой ответ считается сбоем), 0 — файл
    действительно пустой. При сбое tmp_path может остаться — убирает вызывающий.
    on_progress(written, total) вызывается на каждом куске. headers — только для первого
    запроса: ключ доступа не уходит на сервер хранения, куда ведёт редирект.
    """
    host = ""
    written = 0
    trace = ""
    try:
        first = {"headers": headers} if headers else {}
        r = session.get(url, stream=True, allow_redirects=False, timeout=LINK_TIMEOUT, **first)
        location = (getattr(r, "headers", None) or {}).get("Location")
        if r.status_code in _REDIRECT_CODES and location:
            r.close()
            location = urljoin(url, location)
            host = urlsplit(location).hostname or ""
            if host and host_is_dead and host_is_dead(host):
                return 0, host, FetchFailure("storage_skip", f"сервер хранения {host} недавно не отвечал", host)
            r = session.get(location, stream=True, timeout=(storage_timeout, storage_timeout))
        with r:
            r.raise_for_status()
            headers = getattr(r, "headers", {}) or {}
            trace = response_trace(r)
            ctype = (headers.get("Content-Type") or "").lower()
            if reject_pages and any(t in ctype for t in _PAGE_CONTENT_TYPES):
                snippet = next(iter(r.iter_content(512)), b"")[:200]
                return 0, host, FetchFailure(
                    "refused",
                    f"вместо файла пришёл {ctype.split(';')[0]}: {snippet.decode('utf-8', 'replace')!r}{trace}",
                    host,
                )
            content_len = int(headers.get("Content-Length", 0) or 0)
            total = expected_size or content_len
            # MD5 — контрольная сумма, которую отдаёт Яндекс: ловит битые файлы, а не подмену
            md5 = hashlib.md5(usedforsecurity=False) if is_md5(expected_md5) else None
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
                    if md5 is not None:
                        md5.update(chunk)
                    if on_progress:
                        on_progress(written, total)
    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        code = getattr(resp, "status_code", None)
        trace = response_trace(resp) if resp is not None else ""
        # В тексте исключения requests — подписанная ссылка на файл: её в журналы не пишем
        if code == 429:
            return written, host, FetchFailure(
                "rate_limit", f"HTTP 429 при скачивании файла: {scrub_urls(e)}{trace}", host)
        if code is not None and code >= 500:
            return written, host, FetchFailure(
                "server_50x", f"HTTP {code} при скачивании файла: {scrub_urls(e)}{trace}", host)
        return written, host, FetchFailure("refused", f"HTTP {code} при скачивании файла: {scrub_urls(e)}{trace}", host)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        if host and written == 0:
            # Сервер хранения не отдал ни байта — он недоступен, файл не виноват
            return 0, host, FetchFailure(
                "storage_down", f"сервер хранения {host} не отвечает ({type(e).__name__})", host)
        if isinstance(e, requests.exceptions.Timeout):
            return written, host, FetchFailure("timeout", f"таймаут при скачивании файла: {scrub_urls(e)}", host)
        return written, host, FetchFailure(
            "connection", f"обрыв соединения при скачивании файла: {scrub_urls(e)}", host)
    except OSError as e:
        if isinstance(e, requests.RequestException):  # сетевые сбои requests — тоже OSError
            return written, host, FetchFailure("connection", f"обрыв при скачивании файла: {scrub_urls(e)}", host)
        # Остальное — от записи файла: место, права, отключённый диск, длинное имя, FAT32.
        # Повтор скачивания тут не поможет, и Яндекс ни при чём
        return written, host, FetchFailure(local_error_kind(e), f"ошибка записи файла: {e}", host)
    except Exception as e:  # noqa: BLE001 — любой сбой должен стать FetchFailure, а не исключением
        return written, host, FetchFailure("connection", f"сбой скачивания файла: {scrub_urls(e)}", host)

    if content_len and written != content_len:
        return written, host, FetchFailure(
            "connection", f"файл оборвался: скачано {written} байт из {content_len}{trace}", host)
    if written == 0 and expected_size != 0:
        return written, host, FetchFailure("bad_file", f"Яндекс отдал пустой файл{trace}", host)
    if expected_size and written != expected_size:
        # Не помечаем скачанным то, что не совпало с описанием файла на Диске
        return written, host, FetchFailure(
            "bad_file", f"размер не совпал: скачано {written} байт, на Диске {expected_size}{trace}", host)
    if md5 is not None and md5.hexdigest() != expected_md5.lower():
        return written, host, FetchFailure("bad_file", f"MD5 не совпал с файлом на Диске{trace}", host)
    return written, host, None


# Так Яндекс отвечает, когда уменьшенной копии у файла нет вовсе
_NO_PREVIEW = ("HTTP 434", "HTTP 404")


def preview_failure_text(failure: FetchFailure) -> tuple[bool, str]:
    """(у Яндекса просто нет превью?, текст для журнала без метки «категория |»).

    Превью — бонус. Строка «refused | HTTP 434» о нём выглядела как отказ Яндекса,
    и диагностика объявляла «Яндекс отказывает в скачивании безлимита».
    """
    if any(code in failure.text for code in _NO_PREVIEW):
        return True, "у Яндекса нет уменьшенной копии"
    return False, f"не скачалось — {failure.text}"


def fetch_preview(session, url: str, dst: str, headers: dict[str, str] | None = None) -> FetchFailure | None:
    """Скачивает превью (JPEG) в dst атомарно. None — успех.

    Превью отдаёт отдельный сервис Яндекса: оно доступно и тогда, когда сервер
    хранения оригинала молчит. headers — для первого запроса (ключ доступа к Диску).
    """
    tmp = dst + ".part"
    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        _written, _host, failure = fetch_to_file(session, url, tmp, expected_size=None, reject_pages=True,
                                                 headers=headers)
        if failure is None:
            os.replace(tmp, dst)
        return failure
    except OSError as e:
        return FetchFailure(local_error_kind(e), f"ошибка записи превью: {e}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
