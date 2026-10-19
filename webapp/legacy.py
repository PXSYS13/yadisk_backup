"""Адреса прежнего интерфейса (/?ui=old): мастер плана, ручной запуск шагов, воркеры.

Новый интерфейс ими не пользуется. Они остаются, пока остаётся webui_assets/index.html.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from unlim_common import reset_failed_files
from utils import DEFAULT_UNLIM_BATCH, MAX_DISK_WORKERS, MAX_UNLIM_BATCH, MAX_UNLIM_WORKERS
from webapp import access, config, diagnostics, paths
from webapp.local_disk import free_space
from webapp.queue_db import (
    disk_db_stats,
    filter_to_categories,
    preview_db,
    unlim_db_stats,
    unskip_category_marker,
)
from webapp.rescue import launch_unlim_workers, rescue
from webapp.tasks import (
    SLOT_DISK,
    SLOT_ORGANIZE,
    UNLIM_SLOT_PREFIX,
    UNLIM_WORKER_SLOT_PREFIX,
    tm,
)

router = APIRouter()


def _resp(t: tuple[bool, str]) -> dict:
    return {"ok": t[0], "msg": t[1]}


class StartPayload(BaseModel):
    workers: int = Field(default=4, ge=1, le=MAX_UNLIM_WORKERS)
    batch: int = Field(default=DEFAULT_UNLIM_BATCH, ge=1, le=MAX_UNLIM_BATCH)
    do_disk: bool = True
    do_unlim: bool = True
    # None — качать всё; список — отфильтровать по категориям после scan/collect
    disk_categories: list[str] | None = None
    unlim_categories: list[str] | None = None


@router.post("/api/start")
def api_start(payload: StartPayload):
    src = access.preflight_sources(payload.do_disk, payload.do_unlim)
    if src["error"]:
        return {"ok": False, "msg": src["error"]}
    # Папки — из Настроек: у прежнего интерфейса нет своего выбора, а качать весь Диск
    # вопреки сохранённому выбору было бы неожиданно
    ok, msg = rescue.start(workers=payload.workers,
                           do_disk=payload.do_disk,
                           do_unlim=payload.do_unlim,
                           batch=payload.batch,
                           disk_categories=payload.disk_categories,
                           unlim_categories=payload.unlim_categories,
                           disk_folders=config.get_settings()["disk_folders"])
    return {"ok": ok, "msg": msg, "notes": src["notes"] if ok else []}


# ── Мастер «План восстановления» ──


def _degraded(health: dict | None, diag: dict) -> bool:
    """Серверы Яндекса сейчас не в порядке — предлагаем щадящие настройки."""
    if health and health.get("overall") not in (None, "ok", "unknown"):
        return True
    return diag.get("kind") in ("bulk_disabled", "server_50x", "rate_limit", "timeout")


@router.get("/api/plan")
def api_plan():
    """Всё, что нужно мастеру: доступ, что есть в БД, скорость, место, рекомендации."""
    cfg = config.get_config()
    disk_db = disk_db_stats()
    unlim_db = unlim_db_stats()
    health = diagnostics.health_snapshot()
    diag = diagnostics.diagnose_tasks()
    degraded = _degraded(health, diag)
    rate_bps = (disk_db.get("bytes_10m", 0) + unlim_db.get("bytes_10m", 0)) / 600.0
    return {
        "access": {
            "token": access.check_token(cfg["token"]),
            "cookies": access.check_cookies(),
            "health": health,
        },
        "disk": {"preview": preview_db(paths.DB_FILE, "files", "remote_path"), "db": disk_db},
        "unlim": {"preview": preview_db(paths.UNLIM_DB_FILE, "unlim_files", "file_id"), "db": unlim_db},
        "download_dir": cfg["download_dir"],
        "free_space": free_space(cfg["download_dir"]),
        "rate_bps": rate_bps,
        "recommended": {
            "gentle": degraded,
            "workers": 2 if degraded else 4,
            "batch": 10 if degraded else DEFAULT_UNLIM_BATCH,
        },
        "diagnostics": diag,
        "tasks_running": tm.any_alive(""),
        "pipeline_running": rescue.is_running(),
    }


class PlanStartPayload(BaseModel):
    do_disk: bool = True
    do_unlim: bool = True
    # None — всё. Раньше по умолчанию были только фото и видео, и мастер на пустой базе
    # молча оставлял за бортом документы
    disk_categories: list[str] | None = None
    unlim_categories: list[str] | None = None
    workers: int = Field(default=2, ge=1, le=MAX_UNLIM_WORKERS)
    batch: int = Field(default=10, ge=1, le=MAX_UNLIM_BATCH)


@router.post("/api/plan/start")
def api_plan_start(payload: PlanStartPayload):
    if not payload.do_disk and not payload.do_unlim:
        return {"ok": False, "msg": "Выберите хотя бы одно: диск или безлимит"}
    # Скачивание по ссылкам живёт отдельно и мастеру не мешает
    if any(tm.any_alive(slot) for slot in (SLOT_DISK, UNLIM_SLOT_PREFIX, SLOT_ORGANIZE)):
        return {"ok": False, "msg": "Сначала остановите текущие задачи"}
    src = access.preflight_sources(payload.do_disk, payload.do_unlim)
    if src["error"]:
        return {"ok": False, "msg": src["error"]}
    ok, msg = rescue.start(
        workers=payload.workers, batch=payload.batch,
        do_disk=payload.do_disk, do_unlim=payload.do_unlim,
        disk_categories=payload.disk_categories if payload.do_disk else None,
        unlim_categories=payload.unlim_categories if payload.do_unlim else None,
        disk_folders=config.get_settings()["disk_folders"],
    )
    return {"ok": ok, "msg": msg, "notes": src["notes"] if ok else []}


# ── Ручной запуск шагов с вкладок прежнего интерфейса ──


class SelectivePayload(BaseModel):
    categories: list[str]
    workers: int = Field(default=4, ge=1, le=MAX_UNLIM_WORKERS)
    batch: int = Field(default=DEFAULT_UNLIM_BATCH, ge=1, le=MAX_UNLIM_BATCH)


@router.post("/api/disk/download_selected")
def disk_download_selected(payload: SelectivePayload):
    # Сначала вернуть всё что было ранее помечено как "не выбрано" в pending,
    # потом отфильтровать заново и запустить download
    unskip_category_marker(paths.DB_FILE, "files")
    filter_to_categories(paths.DB_FILE, "files", "remote_path", payload.categories)
    workers = max(1, min(payload.workers, MAX_DISK_WORKERS))
    return _resp(tm.start(SLOT_DISK, "main.py",
                          ["download", "--workers", str(workers), "--no-scan"]))


@router.post("/api/unlim/download_selected")
def unlim_download_selected(payload: SelectivePayload):
    unskip_category_marker(paths.UNLIM_DB_FILE, "unlim_files")
    filter_to_categories(paths.UNLIM_DB_FILE, "unlim_files", "file_id", payload.categories)
    return launch_unlim_workers(payload.workers, payload.batch)


class DownloadOpts(BaseModel):
    workers: int = Field(default=4, ge=1, le=MAX_DISK_WORKERS)


@router.post("/api/disk/download")
def disk_download(opts: DownloadOpts):
    return _resp(tm.start(SLOT_DISK, "main.py",
                          ["download", "--workers", str(opts.workers), "--no-scan"]))


@router.post("/api/disk/retry")
def disk_retry():
    return _resp(tm.start(SLOT_DISK, "main.py", ["retry-failed"]))


@router.post("/api/disk/verify")
def disk_verify():
    return _resp(tm.start(SLOT_DISK, "main.py", ["verify"]))


@router.post("/api/disk/stop")
def disk_stop():
    return _resp(tm.stop(SLOT_DISK))


class WorkersPayload(BaseModel):
    workers: int = Field(default=4, ge=1, le=MAX_UNLIM_WORKERS)
    batch: int = Field(default=DEFAULT_UNLIM_BATCH, ge=1, le=MAX_UNLIM_BATCH)


@router.post("/api/unlim/start_workers")
def unlim_start_workers(payload: WorkersPayload):
    return launch_unlim_workers(payload.workers, payload.batch)


@router.post("/api/unlim/retry")
def unlim_retry(payload: WorkersPayload = WorkersPayload()):
    """Сбрасывает файлы с ошибками обратно в pending и перезапускает воркеры."""
    if not paths.UNLIM_DB_FILE.exists():
        return {"ok": False, "msg": "БД безлимита пуста — сначала соберите список файлов"}
    try:
        # Если воркеры сейчас не работают — сбрасываем и зависшие in_progress
        reset_failed_files(
            paths.UNLIM_DB_FILE,
            include_in_progress=not tm.any_alive(UNLIM_WORKER_SLOT_PREFIX),
        )
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    return launch_unlim_workers(payload.workers, payload.batch)


@router.post("/api/unlim/stop")
def unlim_stop():
    return {"ok": True, "stopped": len(tm.stop_matching(UNLIM_SLOT_PREFIX))}


@router.post("/api/stop_all")
def stop_all():
    return {"ok": True, "stopped": tm.stop_all()}


@router.post("/api/task/{slot}/clear_log")
def clear_task_log(slot: str):
    """Очищает буфер логов задачи (или всех slot с префиксом: 'unlim_w*')."""
    return {"ok": True, "cleared": tm.clear_logs(slot)}
