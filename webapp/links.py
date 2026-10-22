"""Раздел «По ссылке»: состояние скачивания по публичным ссылкам и адреса для него.

Само скачивание — public_download.py --wait в слоте links. Webui его не убивает,
чтобы поговорить с ним: пишет в links_meta отметки changed (ссылку добавили или убрали)
и wake («Попробовать сейчас»), а процесс опрашивает их раз в 2 секунды.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from public_download import LinkError, PublicApi, add_link, link_key, normalize_public_url, poke, remove_link
from public_download import connect_db as links_connect
from unlim_common import DEFERRED_MARKER
from utils import EXIT_DISK_FULL, LINKS_DIRNAME, files_text, hhmm, sqlite_ro_uri, sqlite_tx
from webapp import config, paths
from webapp.tasks import SLOT_LINKS, organize_moving, tm

log = logging.getLogger("webui")
router = APIRouter()

LINK_ERROR_TEXT = {
    "not_found": "Ссылка больше не работает: её удалили или закрыли",
    "password": "Ссылка с паролем — такие программа пока не скачивает",
    "forbidden": "Владелец закрыл скачивание по этой ссылке",
}


def links_rows() -> tuple[list[dict], dict]:
    """Ссылки со счётчиками файлов и служебные отметки — одним чтением, только на чтение."""
    if not paths.LINKS_DB_FILE.exists():
        return [], {}
    c = None
    try:
        c = sqlite3.connect(sqlite_ro_uri(paths.LINKS_DB_FILE), uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            """
            SELECT l.id, l.name, l.kind, l.error_kind, l.error, l.limit_until, l.listed_at, l.added_at,
                   SUM(f.status IS NOT NULL AND f.status <> 'gone') AS total,
                   SUM(f.status = 'downloaded') AS done,
                   SUM(f.status = 'pending' AND COALESCE(f.error, '') LIKE ?) AS deferred,
                   SUM(f.status = 'pending' AND COALESCE(f.error, '') LIKE 'link_limit |%') AS limited,
                   SUM(f.status = 'pending') AS pending,
                   SUM(f.status = 'failed') AS failed,
                   SUM(CASE WHEN f.status IN ('pending', 'downloaded', 'failed') THEN COALESCE(f.size, 0) END) AS bytes_total,
                   SUM(CASE WHEN f.status = 'downloaded' THEN COALESCE(f.size, 0) END) AS bytes_done
              FROM links l LEFT JOIN link_files f ON f.link_id = l.id
             WHERE l.state = 'active'
             GROUP BY l.id ORDER BY l.added_at DESC, l.id DESC
            """,
            (DEFERRED_MARKER + "%",),
        )]
        meta = {k: v for k, v in c.execute("SELECT key, value FROM links_meta")}
        return rows, meta
    except sqlite3.Error:
        return [], {}
    finally:
        if c is not None:
            c.close()


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def build_links_status(rows: list[dict], meta: dict, task: dict | None, now: float, folder: str) -> dict:
    """Блок /api/status.links: каждая ссылка — состояние простыми словами и счётчики."""
    running = bool(task and task.get("running"))
    waiting_round = running and meta.get("state") == "waiting"
    next_retry = _num(meta.get("next_retry_at")) if waiting_round else None
    exit_code = task.get("exit_code") if task and not running else None
    problem = None
    if exit_code == EXIT_DISK_FULL:
        problem = {"kind": "local_disk",
                   "text": "Не получается записать файлы в папку: нет места, нет прав или диск отключён"}
    items = []
    for r in rows:
        total, done, failed = int(r["total"] or 0), int(r["done"] or 0), int(r["failed"] or 0)
        pending, deferred, limited = int(r["pending"] or 0), int(r["deferred"] or 0), int(r["limited"] or 0)
        queue = max(0, pending - deferred - limited)
        limit_until = r["limit_until"] if r["limit_until"] and r["limit_until"] > now else None
        if r["error_kind"]:
            state, text = "error", r["error"] or LINK_ERROR_TEXT.get(r["error_kind"], "Ссылка не работает")
        elif r["listed_at"] is None:
            state, text = ("listing", "Получаю список файлов") if running and not waiting_round else (
                "paused", "Список файлов ещё не получен")
        elif limit_until and pending:
            state, text = "limited", f"Яндекс ограничил скачивание по этой ссылке — продолжу в {hhmm(limit_until)}"
        elif pending and running and queue and not waiting_round:
            state, text = "downloading", "Скачиваю"
        elif pending and running:
            state = "waiting"
            text = f"Ждут Яндекса: {files_text(pending)}" + (f", повтор в {hhmm(next_retry)}" if next_retry else "")
        elif pending:
            state, text = "paused", f"На паузе — осталось {files_text(pending)}"
        elif failed:
            state, text = "done_partial", f"Готово, не скачались: {files_text(failed)}"
        else:
            state, text = "done", "Готово: все файлы скачаны" if total else "Готово: файлов по ссылке нет"
        items.append({
            "id": r["id"], "name": r["name"] or "Ссылка", "kind": r["kind"], "state": state, "status_text": text,
            "added_at": r["added_at"], "limit_until": limit_until,
            "files": {"total": total, "done": done, "pending": pending, "queue": queue,
                      "deferred": deferred, "limited": limited, "failed": failed},
            "bytes": {"total": int(r["bytes_total"] or 0), "done": int(r["bytes_done"] or 0)},
        })
    any_pending = any(i["files"]["pending"] or i["state"] == "paused" for i in items)
    return {
        "running": running,
        "state": ("waiting" if waiting_round else "running") if running else ("paused" if any_pending else "idle"),
        "next_retry_at": next_retry,
        "last_activity_at": task.get("last_line_at") if running and task else None,
        "problem": problem,
        "folder": folder,
        "items": items,
    }


def links_status(cfg: dict, tasks: dict) -> dict:
    rows, meta = links_rows()
    return build_links_status(rows, meta, tasks.get(SLOT_LINKS), time.time(),
                              os.path.join(cfg["download_dir"], LINKS_DIRNAME))


def demo_links(now: float, folder: str) -> dict:
    """Пример раздела «По ссылке» для демо-режима: все состояния сразу."""
    def row(i, name, total, done, pending=0, deferred=0, limited=0, failed=0, error_kind=None, limit_until=None,
            listed=True, size=3_500_000):
        return {"id": i, "name": name, "kind": "dir", "error_kind": error_kind,
                "error": LINK_ERROR_TEXT.get(error_kind) if error_kind else None, "limit_until": limit_until,
                "listed_at": now - 600 if listed else None, "added_at": now - i * 3600, "total": total,
                "done": done, "pending": pending, "deferred": deferred, "limited": limited, "failed": failed,
                "bytes_total": total * size, "bytes_done": done * size}
    rows = [
        row(1, "Свадьба Ани и Пети", 412, 268, pending=144, deferred=31),
        row(2, "Курс фотографии — материалы", 96, 40, pending=56, limited=56, limit_until=now + 5 * 3600),
        row(3, "Отчёты 2024", 18, 18),
        row(4, "Старая ссылка", 0, 0, error_kind="not_found", listed=False),
    ]
    task = {"running": True, "last_line_at": now - 4, "exit_code": None}
    return build_links_status(rows, {"state": "running"}, task, now, folder)


def links_start() -> tuple[bool, str]:
    """Запускает скачивание по ссылкам, если оно ещё не идёт. Бережный режим — меньше потоков."""
    if tm.any_alive(SLOT_LINKS):
        return True, "Скачивание по ссылкам уже идёт"
    if organize_moving():
        return False, "Сначала дождитесь конца сортировки: она переносит файлы, и скачивание их перепутает"
    workers = 2 if config.get_settings().get("gentle") else 3
    return tm.start(SLOT_LINKS, "public_download.py", ["--wait", "--workers", str(workers)])


# ── Адреса ──


class LinkAddPayload(BaseModel):
    url: str = Field(max_length=4000)


class LinkIdPayload(BaseModel):
    id: int | None = Field(None, ge=1, le=2 ** 63 - 1)  # SQLite не примет число больше


@router.post("/api/links/add")
def api_links_add(p: LinkAddPayload):
    """«Скачать» по ссылке: проверить её у Яндекса, добавить в список и начать качать."""
    try:
        public_url, sub = normalize_public_url(p.url)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    conn = links_connect(paths.LINKS_DB_FILE)
    try:
        existing = conn.execute("SELECT id, state FROM links WHERE url=?", (link_key(public_url, sub),)).fetchone()
        meta, note = None, ""
        if not existing or existing["state"] == "removed":
            try:
                meta = PublicApi(timeout=(5, 10)).meta(public_url, sub, limit=1)
            except LinkError as e:
                if e.link_level:
                    raise HTTPException(400, e.text) from None
                note = "Яндекс сейчас не ответил — программа попробует сама"
        link_id, new = add_link(conn, public_url, sub, meta)
        poke(conn)
    finally:
        conn.close()
    ok, msg = links_start()
    log.info("ссылка #%s: %s", link_id, "добавлена" if new else "уже в списке")
    text = ("Ссылка добавлена — начинаю скачивание" if new else "Эта ссылка уже в списке") + (f". {note}" if note else "")
    return {"ok": ok, "msg": text if ok else msg, "id": link_id, "new": new}


@router.post("/api/links/remove")
def api_links_remove(p: LinkIdPayload):
    """«Убрать из списка»: файлы на диске остаются. Идущее скачивание пропустит ссылку само."""
    if p.id is None:
        raise HTTPException(400, "Не указано, какую ссылку убрать")
    conn = links_connect(paths.LINKS_DB_FILE)
    try:
        removed = remove_link(conn, p.id, purge=not tm.any_alive(SLOT_LINKS))
        poke(conn)
    finally:
        conn.close()
    log.info("ссылка #%s убрана из списка", p.id)
    return {"ok": removed, "msg": "Убрал ссылку из списка. Скачанные файлы остались в папке" if removed
            else "Такой ссылки в списке нет"}


@router.post("/api/links/retry_now")
def api_links_retry_now(p: LinkIdPayload):
    """«Попробовать сейчас»: не ждать раунда, вернуть упавшие и снять паузу лимита."""
    conn = links_connect(paths.LINKS_DB_FILE)
    try:
        only = " AND link_id=?" if p.id is not None else ""
        params = (p.id,) if p.id is not None else ()
        with sqlite_tx(conn):
            conn.execute(f"UPDATE link_files SET status='pending', error=NULL WHERE status='failed'{only}", params)
            conn.execute(f"UPDATE links SET limit_until=NULL, error_kind=NULL, error=NULL "
                         f"WHERE state='active'{only.replace('link_id', 'id')}", params)
        # Будить — только идущий процесс: новый начнёт с полного раунда и без этого, а
        # оставшийся флаг пропустил бы его первое ожидание
        poke(conn, wake=tm.any_alive(SLOT_LINKS))
    finally:
        conn.close()
    ok, msg = links_start()
    return {"ok": ok, "msg": "Пробую снова прямо сейчас" if ok else msg}


@router.post("/api/links/relist")
def api_links_relist(p: LinkIdPayload):
    """«Проверить новые файлы»: собрать список ссылки заново — владелец мог что-то добавить."""
    if p.id is None:
        raise HTTPException(400, "Не указано, какую ссылку проверить")
    conn = links_connect(paths.LINKS_DB_FILE)
    try:
        with sqlite_tx(conn):
            conn.execute("UPDATE links SET listed_at=NULL WHERE id=? AND state='active'", (p.id,))
        poke(conn, wake=tm.any_alive(SLOT_LINKS))
    finally:
        conn.close()
    ok, msg = links_start()
    return {"ok": ok, "msg": "Проверяю, что нового по ссылке" if ok else msg}


@router.post("/api/links/resume")
def api_links_resume():
    ok, msg = links_start()
    return {"ok": ok, "msg": "Продолжаю скачивание по ссылкам" if ok else msg}


@router.post("/api/links/stop")
def api_links_stop():
    if not tm.any_alive(SLOT_LINKS):
        return {"ok": True, "msg": "Скачивание по ссылкам и так остановлено"}
    ok, _msg = tm.stop(SLOT_LINKS)
    return {"ok": ok, "msg": "Остановил скачивание по ссылкам. Скачанное сохранено"}
