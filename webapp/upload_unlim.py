"""Роутер загрузки фото и видео с компьютера в Безлимит Яндекс.Диска (/api/upload/*).

Использует общие куки (`cookies.json`) и защиту localhost/CSRF из `yadisk_backup`.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from uploader import paths as up_paths
from uploader.cookies import (
    AuthExpiredError,
    CookiesFormatError,
    UnlimApiError,
    YandexUnavailableError,
)
from uploader.state import UploadState
from uploader.utils import validate_source_dir_safe
from uploader.webapp import config as up_cfg
from uploader.webapp import manager as up_mgr

log = logging.getLogger("webui.upload_unlim")
router = APIRouter(prefix="/api/upload", tags=["upload_unlim"])

FileStatusFilter = Literal[
    "all",
    "pending",
    "uploading",
    "uploaded",
    "skipped",
    "skipped_remote",
    "skipped_dup_local",
    "skipped_limit",
    "skipped_unsupported",
    "failed",
]


class UploadSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: str | None = None
    max_workers: int | None = None
    chunk_mb: int | None = None
    strict_quota_guard: bool | None = None
    watch_mode: bool | None = None
    watch_interval_sec: int | None = None


class UploadScanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: str | None = None


class UploadStartPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: str | None = None
    allow_quota_count: bool = False
    refresh_index: bool = False
    watch_mode: bool | None = None


def _csv_safe_cell(val: object) -> str:
    """Нейтрализует формулы Excel (DDE/CSV Injection) в текстовых ячейках."""
    s = "" if val is None else str(val)
    stripped = s.lstrip(" \t\r\n")
    if stripped.startswith(("=", "+", "-", "@")) or s.startswith(("\t", "\r")):
        return "'" + s
    return s


@router.get("/status")
def api_upload_status():
    return up_mgr.manager.status_dict()


@router.post("/cookies/check")
def api_upload_check_cookies():
    try:
        account = up_mgr.manager.verify_account_live()
        return {"ok": True, "account": account}
    except (AuthExpiredError, CookiesFormatError) as e:
        up_mgr.manager.set_account_cache(None, str(e))
        raise HTTPException(status_code=401, detail=str(e)) from e
    except YandexUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except (UnlimApiError, RuntimeError) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/settings")
def api_upload_settings(payload: UploadSettingsPayload):
    updates = payload.model_dump(exclude_none=True)
    if "source_dir" in updates:
        raw_src = str(updates["source_dir"]).strip()
        if raw_src:
            try:
                clean_src = validate_source_dir_safe(raw_src)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            if not os.path.isdir(clean_src):
                raise HTTPException(status_code=400, detail=f"Папка не найдена: {clean_src}")
            updates["source_dir"] = clean_src
        else:
            updates["source_dir"] = ""
    saved = up_cfg.save_config(updates)
    return {"ok": True, "config": saved}


@router.post("/folder/pick")
def api_upload_pick_folder():
    if up_mgr.manager.is_running():
        raise HTTPException(status_code=409, detail="Сначала остановите текущую загрузку")
    raw_cur = up_cfg.get_config().get("source_dir", "")
    cur = ""
    if raw_cur:
        try:
            cur = validate_source_dir_safe(raw_cur)
        except ValueError:
            cur = ""
    chosen, pick_err = up_mgr.pick_folder_dialog(cur)
    if pick_err:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось открыть системный диалог выбора папки: {pick_err}",
        )
    if not chosen:
        return {"picked": False, "path": cur}
    try:
        clean = validate_source_dir_safe(chosen)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    up_cfg.save_config({"source_dir": clean})
    return {"picked": True, "path": clean}


@router.post("/scan")
def api_upload_scan(payload: UploadScanPayload):
    src = (payload.source_dir or "").strip() or up_cfg.get_config().get("source_dir", "")
    try:
        clean = up_mgr.manager.start_scan(src)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    up_cfg.save_config({"source_dir": clean})
    return {"ok": True}


@router.post("/start")
def api_upload_start(payload: UploadStartPayload):
    src = (payload.source_dir or "").strip() or up_cfg.get_config().get("source_dir", "")
    try:
        clean = up_mgr.manager.start_upload(
            source_dir=src,
            allow_quota_count=payload.allow_quota_count,
            refresh_index=payload.refresh_index,
            watch_mode=payload.watch_mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    up_cfg.save_config({"source_dir": clean})
    return {"ok": True}


@router.post("/stop")
def api_upload_stop():
    stopped = up_mgr.manager.stop()
    return {"ok": True, "stopping": stopped}


@router.post("/retry-failed")
def api_upload_retry_failed():
    if up_mgr.manager.is_running():
        raise HTTPException(status_code=409, detail="Сначала остановите текущую загрузку")
    state = UploadState(up_paths.DB_FILE)
    count = state.retry_failed()
    up_mgr.manager.add_event("info", f"Возвращено в очередь для повтора: {count}")
    return {"ok": True, "requeued": count}


@router.post("/reset")
def api_upload_reset():
    if up_mgr.manager.is_running():
        raise HTTPException(status_code=409, detail="Сначала остановите текущую загрузку")
    state = UploadState(up_paths.DB_FILE)
    state.clear_queue()
    up_mgr.manager.add_event(
        "info", "Локальная очередь загрузки очищена (оригиналы на диске и облако не затронуты)"
    )
    return {"ok": True}


@router.get("/files")
def api_upload_files(
    status: Annotated[FileStatusFilter, Query()] = "all",
    search: Annotated[str, Query()] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    state = UploadState(up_paths.DB_FILE)
    items, total = state.list_files(
        status=status, search=search.strip() or None, offset=offset, limit=limit
    )
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@router.get("/export.csv")
def api_upload_export_csv(status: Annotated[FileStatusFilter, Query()] = "all"):
    try:
        state = UploadState(up_paths.DB_FILE)
        state.stats()
    except sqlite3.Error as e:
        log.exception("Не удалось открыть базу очереди загрузки для экспорта CSV")
        raise HTTPException(
            status_code=500, detail=f"Не удалось прочитать базу очереди: {e}"
        ) from e

    def _gen():
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";")
        yield "\ufeff"
        writer.writerow(
            [
                "Относительный путь",
                "Тип",
                "Размер (байт)",
                "Статус",
                "Путь в Безлимите",
                "Комментарий",
            ]
        )
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        try:
            for row in state.iter_files_for_csv(status=status, batch_size=1000):
                writer.writerow(
                    [
                        _csv_safe_cell(row.get("rel_path") or ""),
                        _csv_safe_cell(row.get("media_kind") or ""),
                        int(row.get("size") or 0),
                        _csv_safe_cell(row.get("status") or ""),
                        _csv_safe_cell(row.get("remote_id") or ""),
                        _csv_safe_cell(row.get("error") or ""),
                    ]
                )
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        except sqlite3.Error:
            log.exception("Ошибка SQLite во время потокового экспорта CSV")
            raise

    return StreamingResponse(
        _gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="yadisk_upload_{status}.csv"'},
    )
