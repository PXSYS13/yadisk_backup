"""/api/status — всё, что интерфейс опрашивает раз в 1,5 секунды.

Ключи ответа — API для фронтенда (и прежнего интерфейса): переименовывать их нельзя.
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter

from utils import APP_VERSION, DONATION_URL, LINKS_DIRNAME, TELEGRAM_CHANNEL, TELEGRAM_HANDLE, YOUTUBE_VIDEO_URL
from webapp import access, config, diagnostics, paths
from webapp.demo import DEMO_STATES, demo_rescue
from webapp.folders import disk_folders_summary
from webapp.links import demo_links, links_status
from webapp.local_disk import free_space, local_size
from webapp.queue_db import disk_db_stats, unlim_db_stats
from webapp.rescue import rescue
from webapp.rescue_view import build_rescue_status, db_checked_done, progress_meter, source_live
from webapp.tasks import tm

router = APIRouter()


@router.get("/api/status")
def api_status(demo: str | None = None):
    cfg = config.get_config()
    safe_config = {
        "download_dir": cfg.get("download_dir", ""),
        "download_dir_set": cfg.get("download_dir_set", False),
        "max_workers": cfg.get("max_workers", 4),
        "token_masked": config.mask_token(cfg.get("token", "")),
    }
    token = access.token_snapshot(cfg["token"])
    cookies = access.check_cookies()
    disk_db = disk_db_stats()
    unlim_db = unlim_db_stats()
    pipe_st = rescue.status()
    free = free_space(cfg["download_dir"])
    demo_data = demo_rescue(demo, cfg) if demo in DEMO_STATES and os.environ.get("YADISK_DEMO") == "1" else None
    if demo_data:
        token, cookies = demo_data["token"], demo_data["cookies"]
        disk_db, unlim_db = demo_data["disk_db"], demo_data["unlim_db"]
        free = demo_data["free_space"]
    diag = diagnostics.diagnose_tasks()
    disk_local = local_size(cfg["download_dir"])
    unlim_local = local_size(os.path.join(cfg["download_dir"], "_unlim"))
    tasks = tm.status()
    live = None
    folders = None
    if not demo_data:
        live = {}
        branches = pipe_st.get("branches") or {}
        for name, db in (("disk", disk_db), ("unlim", unlim_db)):
            checked, done = db_checked_done(db)
            rate = progress_meter.sample(name, checked, done)
            live[name] = source_live(name, tasks, (branches.get(name) or {}).get("state"), rate)
        folders = disk_folders_summary(paths.DB_FILE) if disk_db.get("total") else None
    return {
        "app_version": APP_VERSION,
        "donation_url": DONATION_URL,
        "telegram_url": TELEGRAM_CHANNEL,
        "telegram_handle": TELEGRAM_HANDLE,
        "youtube_url": YOUTUBE_VIDEO_URL,
        "config": safe_config,
        "token": token,
        "cookies": cookies,
        "onboarding": access.onboarding_state(cfg, token, cookies, disk_db, unlim_db),
        "disk": {
            "db": disk_db,
            "local": disk_local,
        },
        "unlim": {
            "db": unlim_db,
            "local": unlim_local,
        },
        "tasks": tasks,
        "pipeline": pipe_st,
        "rescue": demo_data["rescue"] if demo_data else build_rescue_status(
            pipe_st, disk_db, unlim_db, token, cookies, cfg, diag, free, disk_local, unlim_local,
            live=live, disk_folders=folders),
        "settings": config.get_settings(),
        "links": demo_links(time.time(), os.path.join(cfg["download_dir"], LINKS_DIRNAME)) if demo_data
        else links_status(cfg, tasks),
        "demo": demo if demo_data else None,
        "diagnostics": diag,
        "yandex_health": diagnostics.health_snapshot(),
        "free_space": free,
        "news": diagnostics.NEWS_LINKS,
        "exit_codes": {str(code): text for code, text in diagnostics.EXIT_EXPLAIN.items()},
    }
