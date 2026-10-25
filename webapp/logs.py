"""Журналы программы и отчёт для поддержки (ZIP с логами и support-info.json без секретов)."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from utils import APP_VERSION, scrub_urls
from webapp import access, config, diagnostics, paths
from webapp.links import links_status
from webapp.local_disk import free_space
from webapp.queue_db import disk_db_stats, unlim_db_stats
from webapp.rescue import rescue
from webapp.tasks import tm

log = logging.getLogger("webui")
router = APIRouter()

_LOG_NAME_RE = re.compile(r"^(tasks/)?[A-Za-z0-9_.-]+\.(log|jsonl)(\.\d+)?$")


def log_path(name: str) -> Path:
    """Путь к файлу лога по имени из API. Только внутри logs/, только логи."""
    if not _LOG_NAME_RE.match(name or ""):
        raise HTTPException(400, "Недопустимое имя лога")
    p = (paths.LOG_DIR / name).resolve()
    if paths.LOG_DIR.resolve() not in p.parents:
        raise HTTPException(400, "Недопустимое имя лога")
    if not p.is_file():
        raise HTTPException(404, "Лог не найден")
    return p


def list_logs() -> list[dict]:
    out = []
    if not paths.LOG_DIR.exists():
        return out
    for p in sorted(paths.LOG_DIR.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(paths.LOG_DIR).as_posix()
        if not _LOG_NAME_RE.match(rel):
            continue
        st = p.stat()
        out.append({"name": rel, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def tail_file(path: Path, lines: int) -> list[str]:
    """Последние N строк файла без чтения всего файла в память."""
    lines = max(1, min(lines, 2000))
    chunk = 64 * 1024
    data = b""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        while pos > 0 and data.count(b"\n") <= lines:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]


def support_info() -> dict:
    """Диагностика для поддержки: версия, окружение, конфиг без секретов, состояние БД и задач."""
    cfg = config.get_config()
    # Последняя проверка ключа, а не новая: отчёт нужен как раз тогда, когда Яндекс не отвечает
    token = access.token_snapshot(cfg["token"])
    acc = token.get("account") or {}
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app_version": APP_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "config": {
            "download_dir": cfg["download_dir"],
            "download_dir_set": cfg["download_dir_set"],
            "max_workers": cfg["max_workers"],
            "token_masked": config.mask_token(cfg["token"]),
            "token_ok": token.get("ok"),
            "token_msg": token.get("msg"),
            "login": acc.get("login"),
            "disk_used": acc.get("used"),
            "disk_total": acc.get("total"),
            "photo_unlim": acc.get("photo_unlim"),
            "is_paid": acc.get("is_paid"),
        },
        "cookies": access.check_cookies(),
        "free_space": free_space(cfg["download_dir"]),
        "disk_db": disk_db_stats(),
        "unlim_db": unlim_db_stats(),
        # Ссылки — только состояния и счётчики: ни адресов, ни имён папок
        "links": [{"id": i["id"], "state": i["state"], "files": i["files"]}
                  for i in links_status(cfg, tm.status())["items"]],
        "tasks": tm.status(),
        "pipeline": rescue.status(),
        "diagnostics": diagnostics.diagnose_tasks(),
        "yandex_health": diagnostics.health_snapshot(),
    }


def build_support_bundle() -> Path:
    """ZIP со всеми логами и support-info.json — одним файлом в поддержку."""
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Старые архивы не копим
    for old in paths.LOG_DIR.glob("yadisk-logs-*.zip"):
        try:
            if time.time() - old.stat().st_mtime > 3600:
                old.unlink()
        except OSError:
            pass
    name = f"yadisk-logs-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    bundle = paths.LOG_DIR / name
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in list_logs():
            try:
                _add_scrubbed(zf, paths.LOG_DIR / entry["name"], entry["name"])
            except OSError:
                pass
        info = json.dumps(support_info(), ensure_ascii=False, indent=2, default=str)
        zf.writestr("support-info.json", scrub_urls(info))
    log.info("собран архив логов для поддержки: %s", name)
    return bundle


def _add_scrubbed(zf: zipfile.ZipFile, path: Path, name: str) -> None:
    """Журнал в архив построчно, без путей и параметров ссылок.

    Подписанная ссылка на файл Яндекса — сама по себе доступ к нему; журналы прежних версий
    их ещё содержат, а отчёт люди выкладывают в чат и на GitHub.
    """
    # ZipInfo из файла — со временем изменения журнала: иначе в архиве у всех стоит 1980-01-01
    entry = zipfile.ZipInfo.from_file(path, name)
    entry.compress_type = zf.compression
    with open(path, "rb") as src, zf.open(entry, "w") as dst:
        for line in src:
            dst.write(scrub_urls(line.decode("utf-8", errors="replace")).encode("utf-8"))


@router.get("/api/logs")
def api_logs():
    return {"logs": list_logs(), "dir": str(paths.LOG_DIR)}


@router.get("/api/logs/tail")
def api_logs_tail(name: str, lines: int = 200):
    p = log_path(name)
    return {"name": name, "lines": tail_file(p, lines), "size": p.stat().st_size}


@router.get("/api/logs/bundle")
def api_logs_bundle():
    bundle = build_support_bundle()
    return FileResponse(str(bundle), media_type="application/zip", filename=bundle.name)
