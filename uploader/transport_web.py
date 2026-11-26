"""Прямой транспорт загрузки в /photounlim через web-API disk.yandex.ru/models-v2 (Транспорт A).

Протокол (подтверждён живым зондом в Фазе 0 и проверкой 2-чанкового PUT):
  1. POST /models-v2?m=mpfs/store {path: "/photounlim/<name>", force: 0, size, md5, sha256}
     - HTTP 200 status="hardlinked" -> мгновенная фиксация без передачи байт;
     - HTTP 200 {upload_url, oid}   -> переход к шагу 2;
     - HTTP 409 code=72             -> файл с таким же содержимым уже есть в /photounlim;
     - HTTP 409 code=47             -> занято имя другим файлом -> добавляем _<md5[:6]> и повторяем;
     - HTTP 507 code=85/311/312     -> лимит хранилища или лимит загрузки.
  2. Потоковый возобновляемый PUT на upload_url:
     - заголовки Content-Type, X-Disk-Uploader-Wait-Complete-Upload: false;
     - большие файлы (> chunk_bytes) шлются чанками через RFC 9110 `Content-Range: bytes start-end/total`;
     - при обрыве соединения HEAD upload_url возвращает Content-Length (уже принятые байты),
       и дозагрузка продолжается точно с места обрыва.
  3. Опрос POST /models-v2?m=mpfs/bulk-operation-status {oids: [oid]} до DONE / COMPLETED,
     затем подтверждение `storage_type == "photounlim"` (с резервным `mpfs/info` если resource опущен).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import requests

from uploader.cookies import (
    AuthExpiredError,
    CaptchaError,
    RateLimitError,
    UnlimApiError,
    YandexUnavailableError,
    get_sk_uid,
    is_captcha_response,
    make_session,
    raw_api_call,
)
from uploader.quota_guard import QuotaGuard, QuotaGuardError
from uploader.state import UploadRow
from uploader.transport_base import ProgressCallback, UploadResult
from uploader.utils import (
    DEFAULT_CHUNK_MB,
    CancelledError,
    name_with_tag,
    redact_urls,
    to_long_path,
)

log = logging.getLogger("unlim_uploader")

UNLIM_PREFIX = "/photounlim/"

MPFS_STORE_FILE_EXISTS = 47
MPFS_CONTENT_ALREADY_EXISTS = 72
MPFS_NO_FREE_SPACE = 85
MPFS_UPLOAD_LIMIT_QUOTA = 311
MPFS_UPLOAD_LIMIT_TECH = 312

MAX_NAME_COLLISION_TRIES = 6
MAX_CHUNK_RETRIES = 4
STREAM_READ_BLOCK = 256 * 1024
ALLOWED_UPLOAD_HOST_SUFFIXES = (".yandex.net", ".yandex.ru")


class _LocalStreamError(Exception):
    """Обёртка локальной OSError внутри генератора тела PUT, чтобы urllib3 не прятал её в ConnectionError."""

    def __init__(self, cause: OSError):
        super().__init__(str(cause))
        self.cause = cause


class _UploadUrlExpired(Exception):
    """Временная ссылка upload_url истекла (404/410) — нужно запросить новый mpfs/store."""


def _validate_upload_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise UnlimApiError("mpfs/store вернул небезопасную ссылку загрузки (ожидался HTTPS)")
    try:
        ipaddress.ip_address(host)
        raise UnlimApiError("mpfs/store вернул IP-адрес вместо домена Яндекса")
    except ValueError:
        pass
    if not any(host.endswith(sfx) for sfx in ALLOWED_UPLOAD_HOST_SUFFIXES):
        raise UnlimApiError(f"mpfs/store вернул посторонний хост загрузки: {host}")
    return url


def _sleep_interruptible(seconds: float, stop_event: threading.Event | None) -> None:
    if stop_event is not None:
        if stop_event.wait(seconds):
            raise CancelledError("Загрузка остановлена пользователем")
    else:
        time.sleep(seconds)


class SessionContext:
    """Потокобезопасный держатель sk/uid и per-thread HTTP-сессий с автообновлением при bad_sk."""

    def __init__(self, session: requests.Session, sk: str, uid: str, *, owns_session: bool = False):
        self.session = session
        self._owns_root_session = owns_session
        self._seed_cookies: dict[str, str] | None = getattr(session, "yadisk_seed_cookies", None)
        self._base_url: str | None = getattr(session, "yadisk_base", None)
        self._can_clone = isinstance(session, requests.Session) and self._seed_cookies is not None
        self._lock = threading.Lock()
        self._tls = threading.local()
        self._created_sessions: list[requests.Session] = []
        self._sk = sk
        self._uid = uid
        self._generation = 0

    def thread_session(self) -> requests.Session:
        """Возвращает изолированную requests.Session текущего потока (для фейковых сессий в тестах — корневую)."""
        if not self._can_clone:
            return self.session
        s = getattr(self._tls, "session", None)
        if s is None:
            s = make_session(cookies=self._seed_cookies, base=self._base_url)
            self._tls.session = s
            with self._lock:
                self._created_sessions.append(s)
        return s

    def get(self) -> tuple[str, str, int]:
        with self._lock:
            return self._sk, self._uid, self._generation

    def refresh_if_stale(self, seen_generation: int) -> tuple[str, str, int]:
        with self._lock:
            if self._generation != seen_generation:
                return self._sk, self._uid, self._generation
            sk, uid = get_sk_uid(self.thread_session())
            self._sk = sk
            self._uid = uid
            self._generation += 1
            log.info("Обновил sk/uid сессии (generation=%d)", self._generation)
            return self._sk, self._uid, self._generation

    def close(self) -> None:
        with self._lock:
            sessions = list(self._created_sessions)
            self._created_sessions.clear()
        for s in sessions:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        if self._owns_root_session:
            try:
                self.session.close()
            except Exception:  # noqa: BLE001
                pass


class _ChunkStream:
    """Потоковый генератор среза [start:end] локального файла с прогрессом и поддержкой остановки."""

    def __init__(
        self,
        local_path: str,
        start: int,
        length: int,
        total_size: int,
        *,
        on_progress: ProgressCallback | None = None,
        stop_event: threading.Event | None = None,
    ):
        self.local_path = local_path
        self.start = start
        self.length = length
        self.total_size = total_size
        self.on_progress = on_progress
        self.stop_event = stop_event

    def __len__(self) -> int:
        return self.length

    def __iter__(self) -> Iterator[bytes]:
        sent = 0
        try:
            with open(to_long_path(self.local_path), "rb") as f:
                f.seek(self.start)
                remaining = self.length
                while remaining > 0:
                    if self.stop_event is not None and self.stop_event.is_set():
                        raise CancelledError("Загрузка остановлена пользователем")
                    block = f.read(min(STREAM_READ_BLOCK, remaining))
                    if not block:
                        raise OSError("Локальный файл уменьшился или был изменён во время чтения")
                    remaining -= len(block)
                    sent += len(block)
                    if self.on_progress is not None:
                        self.on_progress(self.start + sent, self.total_size)
                    yield block
        except OSError as e:
            raise _LocalStreamError(e) from None


def _parse_mpfs_error(data: Any) -> tuple[int | None, str]:
    if not isinstance(data, dict):
        return None, ""
    err = data.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        try:
            code_int = int(code) if code is not None else None
        except (TypeError, ValueError):
            code_int = None
        title = str(err.get("title") or err.get("message") or err.get("type") or "")
        return code_int, title
    if err is not None:
        return None, str(err)
    return None, ""


class WebModelsTransport:
    """Прямая загрузка фото и видео в /photounlim через models-v2 (без сторонних ключей)."""

    name = "models-v2"

    def __init__(
        self,
        ctx: SessionContext,
        *,
        chunk_mb: int = DEFAULT_CHUNK_MB,
        put_session: requests.Session | None = None,
        poll_sleep: float = 0.75,
        verify_storage_type: bool = True,
    ):
        self.ctx = ctx
        self.chunk_bytes = max(1, int(chunk_mb)) * 1024 * 1024
        self._injected_put_session = put_session
        self._put_tls = threading.local()
        self._put_lock = threading.Lock()
        self._created_put_sessions: list[requests.Session] = []
        self.poll_sleep = poll_sleep
        self.verify_storage_type = verify_storage_type

    def _get_put_session(self) -> requests.Session:
        if self._injected_put_session is not None:
            return self._injected_put_session
        s = getattr(self._put_tls, "session", None)
        if s is None:
            s = requests.Session()
            self._put_tls.session = s
            with self._put_lock:
                self._created_put_sessions.append(s)
        return s

    def close(self) -> None:
        with self._put_lock:
            sessions = list(self._created_put_sessions)
            self._created_put_sessions.clear()
        for s in sessions:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> WebModelsTransport:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _call_with_sk_retry(
        self,
        method: str,
        params: Any,
        *,
        timeout: float | tuple[float, float] = (15.0, 60.0),
    ):
        sk, uid, gen = self.ctx.get()
        try:
            return raw_api_call(self.ctx.thread_session(), sk, uid, method, params, timeout=timeout)
        except AuthExpiredError:
            sk, uid, _ = self.ctx.refresh_if_stale(gen)
            return raw_api_call(self.ctx.thread_session(), sk, uid, method, params, timeout=timeout)

    def _fetch_resource_info(self, path: str) -> dict[str, Any] | None:
        resp = self._call_with_sk_retry(
            "mpfs/info",
            {"path": path, "meta": "size,md5,mimetype,mediatype,etime,storage_type"},
        )
        if resp.status_code == 200 and isinstance(resp.data, dict) and not resp.data.get("error"):
            return resp.data
        return None

    def _request_store(
        self,
        remote_name: str,
        size: int,
        md5: str,
        sha256: str,
    ) -> tuple[str, dict[str, Any]]:
        candidate = remote_name
        for attempt in range(MAX_NAME_COLLISION_TRIES):
            path = f"{UNLIM_PREFIX}{candidate}"
            resp = self._call_with_sk_retry(
                "mpfs/store",
                {
                    "path": path,
                    "force": 0,
                    "size": int(size),
                    "md5": md5.lower(),
                    "sha256": sha256.lower(),
                },
            )
            if (
                resp.status_code == 200
                and isinstance(resp.data, dict)
                and not resp.data.get("error")
            ):
                return candidate, resp.data

            code, title = _parse_mpfs_error(resp.data)
            if (
                resp.status_code == 409 or resp.status_code == 200
            ) and code == MPFS_CONTENT_ALREADY_EXISTS:
                return candidate, {"status": "duplicate_remote", "path": path, "title": title}
            if (
                resp.status_code == 409 or resp.status_code == 200
            ) and code == MPFS_STORE_FILE_EXISTS:
                tag = f"_{md5[:6]}" if attempt == 0 else f"_{md5[:6]}_{attempt + 1}"
                candidate = name_with_tag(remote_name, tag)
                log.info("Имя %s занято другим файлом в /photounlim -> пробую %s", path, candidate)
                continue
            if resp.status_code == 507 or code in (
                MPFS_NO_FREE_SPACE,
                MPFS_UPLOAD_LIMIT_QUOTA,
                MPFS_UPLOAD_LIMIT_TECH,
            ):
                raise QuotaGuardError(
                    f"Яндекс отклонил загрузку (HTTP {resp.status_code}, code={code}): {title or redact_urls(resp.text)[:160]}"
                )
            if resp.status_code >= 500:
                raise YandexUnavailableError(
                    f"mpfs/store: ошибка сервера Яндекса HTTP {resp.status_code}: {title or redact_urls(resp.text)[:160]}"
                )
            raise UnlimApiError(
                f"mpfs/store: HTTP {resp.status_code} (code={code}): {title or redact_urls(resp.text)[:160]}"
            )
        raise UnlimApiError(f"Не удалось подобрать свободное имя в /photounlim для {remote_name}")

    def _query_uploaded_offset(self, upload_url: str) -> int:
        sess = self._get_put_session()
        try:
            r = sess.head(upload_url, timeout=(15.0, 30.0))
        except requests.RequestException as e:
            raise YandexUnavailableError(f"HEAD upload_url: {type(e).__name__}") from None
        if r.status_code in (404, 410):
            raise _UploadUrlExpired(f"Ссылка загрузки истекла (HTTP {r.status_code})")
        if r.status_code == 200:
            raw_len = r.headers.get("Content-Length") or r.headers.get("content-length") or "0"
            try:
                return max(0, int(raw_len))
            except ValueError:
                log.warning("HEAD upload_url вернул некорректный Content-Length=%r", raw_len)
                return 0
        raise YandexUnavailableError(f"HEAD upload_url: HTTP {r.status_code}")

    def _put_file_resumable(
        self,
        upload_url: str,
        local_path: str,
        total_size: int,
        mime: str,
        *,
        on_progress: ProgressCallback | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Отправляет файл на upload_url чанками (RFC 9110 Content-Range) с докачкой по HEAD Content-Length."""
        sess = self._get_put_session()
        offset = 0
        retries = 0
        while offset < total_size:
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Загрузка остановлена пользователем")
            end = min(total_size, offset + self.chunk_bytes)
            chunk_len = end - offset
            headers: dict[str, str] = {
                "Content-Type": mime or "application/octet-stream",
                "X-Disk-Uploader-Wait-Complete-Upload": "false",
                "Content-Length": str(chunk_len),
            }
            if offset > 0 or end < total_size:
                headers["Content-Range"] = f"bytes {offset}-{end - 1}/{total_size}"

            stream = _ChunkStream(
                local_path,
                offset,
                chunk_len,
                total_size,
                on_progress=on_progress,
                stop_event=stop_event,
            )
            try:
                r = sess.put(
                    upload_url,
                    data=stream,
                    headers=headers,
                    timeout=(20.0, 180.0),
                    allow_redirects=False,
                )
            except CancelledError:
                raise
            except _LocalStreamError as local_err:
                raise local_err.cause from None
            except requests.RequestException as e:
                retries += 1
                if retries > MAX_CHUNK_RETRIES:
                    raise YandexUnavailableError(
                        f"Обрыв передачи на {offset}/{total_size}: {type(e).__name__}"
                    ) from None
                _sleep_interruptible(min(10.0, 2.0**retries), stop_event)
                try:
                    offset = min(total_size, self._query_uploaded_offset(upload_url))
                except YandexUnavailableError as head_err:
                    log.warning("Не удалось уточнить смещение через HEAD: %s", head_err)
                continue

            if is_captcha_response(r):
                raise CaptchaError(
                    f"Сервер загрузки Яндекса показал SmartCaptcha (HTTP {r.status_code})"
                )
            if r.status_code in (200, 201, 202):
                offset = end
                retries = 0
                continue
            if r.status_code in (401, 403):
                raise AuthExpiredError(
                    f"Сервер загрузки отклонил авторизацию (HTTP {r.status_code})"
                )
            if r.status_code in (404, 410):
                raise _UploadUrlExpired(f"Ссылка загрузки устарела (HTTP {r.status_code})")
            if r.status_code == 429:
                raise RateLimitError("Сервер загрузки Яндекса вернул HTTP 429")
            if r.status_code == 507:
                raise QuotaGuardError("Сервер загрузки Яндекса вернул HTTP 507 (нет места)")
            if r.status_code == 400:
                raise UnlimApiError(f"PUT upload_url: HTTP 400: {redact_urls(r.text)[:160]}")
            if r.status_code in (408, 416, 500, 502, 503, 504):
                retries += 1
                if retries > MAX_CHUNK_RETRIES:
                    raise YandexUnavailableError(
                        f"PUT upload_url: HTTP {r.status_code}: {redact_urls(r.text)[:160]}"
                    )
                _sleep_interruptible(min(10.0, 2.0**retries), stop_event)
                try:
                    offset = min(total_size, self._query_uploaded_offset(upload_url))
                except YandexUnavailableError as head_err:
                    log.warning("HEAD после HTTP %d не ответил: %s", r.status_code, head_err)
                continue
            raise UnlimApiError(
                f"PUT upload_url: неожиданный HTTP {r.status_code}: {redact_urls(r.text)[:160]}"
            )

    def _wait_operation(
        self,
        oid: str,
        total_size: int,
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        max_wait = 60.0 if total_size < 50 * 1024 * 1024 else 300.0
        deadline = time.monotonic() + max_wait
        delay = self.poll_sleep

        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Ожидание фиксации файла остановлено")
            try:
                resp = self._call_with_sk_retry("mpfs/bulk-operation-status", {"oids": [oid]})
            except (AuthExpiredError, CaptchaError, RateLimitError):
                raise
            except YandexUnavailableError as e:
                log.warning(
                    "bulk-operation-status временный сбой (%s) — продолжаю ждать фиксацию", e
                )
                _sleep_interruptible(delay, stop_event)
                delay = min(3.0, delay * 1.35)
                continue

            if resp.status_code == 200 and isinstance(resp.data, dict):
                if resp.data.get("error"):
                    raise UnlimApiError(
                        f"bulk-operation-status: ошибка API: {redact_urls(str(resp.data.get('error')))[:180]}"
                    )
                op = resp.data.get(oid)
                if isinstance(op, dict):
                    state = str(op.get("state") or "").upper()
                    if state in ("DONE", "COMPLETED"):
                        res = op.get("resource")
                        return res if isinstance(res, dict) else None
                    if state in ("FAILED", "ERROR", "ABORTED", "CANCELLED"):
                        err = op.get("error")
                        raise UnlimApiError(
                            f"Яндекс не смог сохранить файл ({state}): {redact_urls(str(err))[:200]}"
                        )
            elif resp.status_code >= 500:
                log.warning("bulk-operation-status HTTP %d — повторяю", resp.status_code)
            else:
                raise UnlimApiError(
                    f"bulk-operation-status: неожиданный HTTP {resp.status_code}: {redact_urls(resp.text)[:160]}"
                )

            _sleep_interruptible(delay, stop_event)
            delay = min(3.0, delay * 1.35)

        raise YandexUnavailableError(
            f"Истекло время ожидания фиксации файла в Яндексе (oid={oid[:12]}…)"
        )

    def _confirm_photounlim(
        self, resource: dict[str, Any] | None, target_path: str
    ) -> dict[str, Any] | None:
        if not self.verify_storage_type:
            return resource
        raw_meta = resource.get("meta") if isinstance(resource, dict) else None
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        if not isinstance(resource, dict) or meta.get("storage_type") is None:
            fetched = self._fetch_resource_info(target_path)
            if fetched is not None:
                resource = fetched
        QuotaGuard.verify_resource(resource, require_present=True)
        return resource

    def upload_file(
        self,
        row: UploadRow,
        *,
        md5: str,
        sha256: str,
        on_progress: ProgressCallback | None = None,
        stop_event: threading.Event | None = None,
    ) -> UploadResult:
        if not os.path.isfile(to_long_path(row.local_path)):
            raise FileNotFoundError(f"Файл исчез с диска: {row.local_path}")

        for url_attempt in range(2):
            final_name, store_data = self._request_store(row.remote_name, row.size, md5, sha256)
            target_path = f"{UNLIM_PREFIX}{final_name}"

            if store_data.get("status") == "duplicate_remote":
                return UploadResult(
                    outcome="duplicate_remote",
                    remote_id=target_path,
                    remote_name=final_name,
                    note="дубль по содержимому уже в Безлимите (Яндекс code 72)",
                )

            if store_data.get("status") == "hardlinked":
                if on_progress is not None:
                    on_progress(row.size, row.size)
                resource = self._confirm_photounlim(store_data.get("resource"), target_path)
                return UploadResult(
                    outcome="hardlinked",
                    remote_id=target_path,
                    remote_name=final_name,
                    resource=resource,
                    note="мгновенная загрузка (hardlink)",
                )

            upload_url = store_data.get("upload_url")
            oid = store_data.get("oid")
            if not upload_url or not oid:
                raise UnlimApiError(
                    f"mpfs/store не вернул upload_url/oid (ключи: {sorted(store_data.keys())})"
                )

            safe_url = _validate_upload_url(str(upload_url))
            try:
                self._put_file_resumable(
                    safe_url,
                    row.local_path,
                    row.size,
                    row.mime,
                    on_progress=on_progress,
                    stop_event=stop_event,
                )
            except _UploadUrlExpired:
                if url_attempt == 0:
                    log.info(
                        "Ссылка загрузки для %s устарела — запрашиваю новую через mpfs/store",
                        row.name,
                    )
                    continue
                raise YandexUnavailableError(
                    "Ссылка загрузки повторно устарела — повторю позже"
                ) from None

            resource = self._wait_operation(str(oid), row.size, stop_event=stop_event)
            resource = self._confirm_photounlim(resource, target_path)

            actual_path = str((resource or {}).get("path") or target_path)
            actual_name = str((resource or {}).get("name") or final_name)
            return UploadResult(
                outcome="uploaded",
                remote_id=actual_path,
                remote_name=actual_name,
                resource=resource,
            )

        raise YandexUnavailableError(f"Не удалось завершить загрузку {row.name}")
