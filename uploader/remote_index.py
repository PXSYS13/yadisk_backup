"""Индексация уже загруженных файлов в /photounlim через mpfs/list для локальной дедупликации."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import requests

from uploader.cookies import (
    AuthExpiredError,
    UnlimApiError,
    YandexUnavailableError,
    raw_api_call,
)
from uploader.media import plausible_ts
from uploader.state import UploadState
from uploader.transport_web import SessionContext
from uploader.utils import CancelledError, redact_urls

log = logging.getLogger("unlim_uploader")

UNLIM_ROOT = "/photounlim"
LIST_PAGE_SIZE = 1000
LIST_META = "size,md5,mimetype,mediatype,etime"
LIST_PAGE_PAUSES = (10, 25)


def _extract_file_time(item: dict[str, Any]) -> float | None:
    meta = item.get("meta")
    meta_dict = meta if isinstance(meta, dict) else {}
    return plausible_ts(item.get("etime") or meta_dict.get("etime")) or plausible_ts(
        item.get("mtime")
    )


def _fetch_page_once(
    ctx: SessionContext,
    offset: int,
    amount: int,
) -> list[dict[str, Any]] | None:
    """Возвращает список элементов страницы или None при HTTP 404 (пустой /photounlim на новом аккаунте)."""
    params = {"path": UNLIM_ROOT, "amount": amount, "offset": offset, "meta": LIST_META}
    sk, uid, gen = ctx.get()
    try:
        resp = raw_api_call(
            ctx.thread_session(), sk, uid, "mpfs/list", params, timeout=(15.0, 90.0)
        )
    except AuthExpiredError:
        sk, uid, _ = ctx.refresh_if_stale(gen)
        resp = raw_api_call(
            ctx.thread_session(), sk, uid, "mpfs/list", params, timeout=(15.0, 90.0)
        )

    if resp.status_code == 404:
        return None
    if resp.status_code == 200 and isinstance(resp.data, list):
        return resp.data
    if resp.status_code == 200 and isinstance(resp.data, dict) and resp.data.get("error"):
        raise UnlimApiError(
            f"mpfs/list: ошибка API: {redact_urls(str(resp.data.get('error')))[:180]}"
        )
    if resp.status_code >= 500:
        raise YandexUnavailableError(
            f"mpfs/list: HTTP {resp.status_code}: {redact_urls(resp.text)[:160]}"
        )
    raise UnlimApiError(f"mpfs/list: HTTP {resp.status_code}: {redact_urls(resp.text)[:160]}")


def _fetch_page_patiently(
    ctx: SessionContext,
    offset: int,
    *,
    amount: int = LIST_PAGE_SIZE,
    stop_event: threading.Event | None = None,
) -> list[dict[str, Any]] | None:
    for pause in LIST_PAGE_PAUSES:
        if stop_event is not None and stop_event.is_set():
            raise CancelledError("Индексация облака остановлена")
        try:
            return _fetch_page_once(ctx, offset, amount)
        except YandexUnavailableError as e:
            log.warning("mpfs/list offset=%d: %s — повтор через %d с", offset, e, pause)
            if stop_event is not None:
                if stop_event.wait(pause):
                    raise CancelledError("Индексация облака остановлена") from None
            else:
                import time

                time.sleep(pause)
    return _fetch_page_once(ctx, offset, amount)


def sync_remote_index(
    session_or_ctx: requests.Session | SessionContext,
    sk: str | None,
    uid: str | None,
    state: UploadState,
    *,
    page_size: int = LIST_PAGE_SIZE,
    stop_event: threading.Event | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Читает весь /photounlim постранично и сохраняет (file_id, name, size, md5, file_time) в БД."""
    ctx = (
        session_or_ctx
        if isinstance(session_or_ctx, SessionContext)
        else SessionContext(session_or_ctx, sk or "", uid or "")
    )

    offset = 0
    seen_paths: set[str] = set()
    rows: list[tuple[str, str, int, str | None, float | None]] = []
    aborted_repeat = False

    while True:
        if stop_event is not None and stop_event.is_set():
            raise CancelledError("Индексация облака остановлена")
        page = _fetch_page_patiently(ctx, offset, amount=page_size, stop_event=stop_event)
        if page is None:
            # На новом аккаунте раздел /photounlim ещё не создан — это пустой список
            log.info("mpfs/list: раздел /photounlim пока пуст (HTTP 404)")
            break

        items = [
            it
            for it in page
            if isinstance(it, dict) and str(it.get("path") or "").startswith(UNLIM_ROOT + "/")
        ]

        new_on_page = 0
        for it in items:
            fpath = str(it.get("path") or "")
            if fpath in seen_paths:
                continue
            seen_paths.add(fpath)
            new_on_page += 1
            raw_meta = it.get("meta")
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            try:
                size = int(meta.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            md5 = str(meta.get("md5") or "").strip().lower() or None
            name = str(it.get("name") or fpath.rsplit("/", 1)[-1])
            rows.append((fpath, name, size, md5, _extract_file_time(it)))

        if on_progress is not None:
            on_progress(len(rows))

        if len(items) < page_size:
            break
        if new_on_page == 0:
            log.warning(
                "mpfs/list: страница offset=%d не принесла новых файлов — останавливаю обход",
                offset,
            )
            aborted_repeat = True
            break
        offset += page_size

    saved = state.replace_remote_index(rows, complete=not aborted_repeat)
    log.info("Индекс /photounlim обновлён: %d файлов (complete=%s)", saved, not aborted_repeat)
    return saved
