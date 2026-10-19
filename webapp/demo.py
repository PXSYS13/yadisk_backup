"""Демо-режим: все состояния главного экрана без настоящего Яндекса.

YADISK_DEMO=1 и /api/status?demo=<состояние> (или /?demo=<состояние> в браузере).
"""

from __future__ import annotations

import time

from utils import hhmm
from webapp.queue_db import empty_db_stats
from webapp.rescue_view import build_rescue_status

DEMO_STATES = ("setup", "ready", "listing", "downloading", "low_space", "waiting", "token", "cookies",
               "disk_full", "done", "done_partial", "stopped", "update")
# update — как ready, и на GitHub «вышла» новая версия (/api/status.update — webapp.updates.demo)

_DEMO_DISK_FILES = ("Документы/Договор аренды.pdf", "Фото/2023/Отпуск/IMG_2041.JPG",
                    "Фото/2023/Отпуск/IMG_2042.JPG", "Видео/День рождения.mp4", "Музыка/Плейлист.m3u")
_DEMO_UNLIM_FILES = ("2021-07-14 18-22-05.JPG", "2021-07-14 18-25-41.JPG", "2021-07-15 09-02-11.HEIC",
                     "2021-07-15 09-03-37.MOV", "2021-07-16 20-14-55.JPG")


def demo_rescue(kind: str, cfg: dict) -> dict:
    gb = 1024 ** 3
    now = time.time()

    def db(total, done, pending=0, deferred=0, failed=0, previews=0, size=6 * gb, speed=0, names=()):
        st = empty_db_stats()
        st.update(total=total, downloaded=done, pending=pending + deferred, deferred=deferred,
                  failed=failed, previews=previews, bytes_total=size,
                  bytes_done=int(size * done / max(total, 1)),
                  bytes_pending=int(size * (pending + deferred) / max(total, 1)),
                  bytes_deferred=int(size * deferred / max(total, 1)),
                  bytes_selected_total=size, bytes_10m=speed, deferred_hosts=deferred // 40)
        st["recent"] = [{"path": n, "status": "downloaded", "size": (3 + i * 7) * 1024 ** 2,
                         "at": now - 4 - i * 37} for i, n in enumerate(names)] if done else []
        st["listed_at"] = now - 1755 if total else None
        return st

    def alive(checked, done, listing=None, ago=2):
        return {"last_activity_at": now - ago, "tasks_running": 1, "listing_found": listing,
                "recent": {"window_sec": 300, "checked": checked, "done": done}}

    def events(*items):
        return [{"at": now - ago, "source": src, "kind": k, "text": text} for ago, src, k, text in items]

    start_events = [(1800, None, "run", "Начинаю: проверяю доступ к Яндексу"),
                    (1799, "disk", "ok", "Ключ доступа к Диску работает"),
                    (1799, "unlim", "ok", "Доступ к Безлимиту работает"),
                    (1798, "disk", "run", "Получаю у Яндекса список файлов"),
                    (1798, "unlim", "run", "Получаю у Яндекса список фото и видео"),
                    (1755, "disk", "ok", "Список получен: 6 015 файлов"),
                    (1755, "disk", "run", "Начинаю скачивание"),
                    (1741, "unlim", "ok", "Список получен: 13 602 файла"),
                    (1740, "unlim", "run", "Начинаю скачивание в 4 потока")]
    live: dict | None = None
    tok = {"ok": True, "login": "demo", "account": {"photo_unlim": 69 * gb}}
    coo = {"ok": True}
    st: dict = {"running": False, "started_at": None, "state": "idle", "next_retry_at": None,
                "branches": {"disk": {"state": "off"}, "unlim": {"state": "off"}}}
    disk, unlim = db(6015, 0, pending=6015), db(13602, 0, pending=13602, size=69 * gb)
    free = {"free": 120 * gb}

    def run(d, u, state="running", running=True, next_retry=None, ev=(), started_ago=1800):
        return {"running": running, "started_at": now - started_ago, "finished_at": None if running else now,
                "state": state, "next_retry_at": next_retry, "branches": {"disk": d, "unlim": u},
                "events": events(*ev)}

    dl = {"state": "downloading", "activity": "скачиваю файлы", "round": 1}
    folders: dict | None = {"total": 6, "complete": 2}
    if kind == "setup":
        tok, coo = {"ok": False}, {"ok": False}
        folders = None
    elif kind == "listing":
        st = run({"state": "listing", "round": 1}, {"state": "listing", "round": 1}, started_ago=40,
                 ev=[(40, None, "run", "Начинаю: проверяю доступ к Яндексу"),
                     (39, "disk", "ok", "Ключ доступа к Диску работает"),
                     (39, "unlim", "ok", "Доступ к Безлимиту работает"),
                     (38, "disk", "run", "Получаю у Яндекса список файлов"),
                     (38, "unlim", "run", "Получаю у Яндекса список фото и видео")])
        disk, unlim = db(0, 0), db(0, 0)
        live = {"disk": alive(0, 0, listing=2400), "unlim": alive(0, 0, listing=9000, ago=1)}
        folders = None
    elif kind in ("downloading", "low_space"):
        st = run(dl, dl, ev=start_events + [(600, "disk", "info", "Снова в очереди: 12 файлов, которые раньше не скачались"),
                                            (240, "disk", "ok", "Папка «Документы» скачана целиком")])
        disk = db(6015, 2100, pending=3500, deferred=415, speed=420 * 1024 ** 2, names=_DEMO_DISK_FILES)
        unlim = db(13602, 1900, pending=10900, deferred=802, previews=650, size=69 * gb, speed=380 * 1024 ** 2,
                   names=_DEMO_UNLIM_FILES)
        live = {"disk": alive(140, 18), "unlim": alive(260, 41, ago=1)}
        if kind == "low_space":  # осталось скачать ~63 ГБ, а свободно 40 — и диск в FAT32
            free = {"free": 40 * gb, "fs": "FAT32", "fat": True}
    elif kind == "waiting":
        wait = {"state": "waiting", "next_retry_at": now + 1500, "round": 2}
        st = run(wait, wait, next_retry=now + 1500, ev=start_events + [
            (310, "disk", "wait", "Всё доступное скачано. Ждут Яндекса: 4 812 файлов. Повтор в "
             + hhmm(now + 1500)),
            (300, "unlim", "wait", "Всё доступное скачано. Ждут Яндекса: 10 881 файл. Повтор в "
             + hhmm(now + 1500))])
        disk = db(6015, 1203, deferred=4812, previews=4100, names=_DEMO_DISK_FILES)
        unlim = db(13602, 2721, deferred=10881, previews=9500, size=69 * gb, names=_DEMO_UNLIM_FILES)
        live = {"disk": {"last_activity_at": now - 310}, "unlim": {"last_activity_at": now - 300}}
    elif kind == "token":
        st = run({"state": "needs_action", "action": "token", "round": 1}, dl, ev=start_events + [
            (120, "disk", "err", "Яндекс перестал принимать ключ доступа к Диску — эта часть на паузе")])
        disk = db(6015, 800, pending=5215, names=_DEMO_DISK_FILES)
        unlim = db(13602, 3000, pending=10602, size=69 * gb, names=_DEMO_UNLIM_FILES)
        live = {"disk": {"last_activity_at": now - 120}, "unlim": alive(220, 35)}
    elif kind == "cookies":
        st = run(dl, {"state": "needs_action", "action": "cookies", "round": 1}, ev=start_events + [
            (90, "unlim", "err", "Нужно обновить доступ к Безлимиту — эта часть на паузе")])
        disk = db(6015, 2500, pending=3515, names=_DEMO_DISK_FILES)
        unlim = db(13602, 400, pending=13202, size=69 * gb, names=_DEMO_UNLIM_FILES)
        live = {"disk": alive(120, 30), "unlim": {"last_activity_at": now - 90}}
    elif kind == "disk_full":
        st = run({"state": "needs_action", "action": "free_space"}, {"state": "needs_action", "action": "free_space"},
                 state="failed", running=False, ev=start_events + [
                     (60, None, "err", "На диске закончилось место — скачивание на паузе")])
        free = {"free": 120 * 1024 ** 2}
        disk = db(6015, 3000, pending=3015, names=_DEMO_DISK_FILES)
        unlim = db(13602, 5000, pending=8602, size=69 * gb, names=_DEMO_UNLIM_FILES)
    elif kind == "done":
        st = run({"state": "done"}, {"state": "done"}, state="done", running=False, ev=start_events + [
            (30, "disk", "ok", "Готово: все файлы сохранены"), (10, "unlim", "ok", "Готово: все файлы сохранены")])
        disk = db(6015, 6015, names=_DEMO_DISK_FILES)
        unlim = db(13602, 13602, size=69 * gb, names=_DEMO_UNLIM_FILES)
    elif kind == "done_partial":
        st = run({"state": "done"}, {"state": "done"}, state="done", running=False, ev=start_events + [
            (30, "disk", "ok", "Готово. Не скачалось: 20 файлов"), (10, "unlim", "ok", "Готово. Не скачалось: 12 файлов")])
        disk = db(6015, 5995, failed=20, names=_DEMO_DISK_FILES)
        unlim = db(13602, 13590, failed=12, size=69 * gb, names=_DEMO_UNLIM_FILES)
    elif kind == "stopped":
        st = run({"state": "stopped"}, {"state": "stopped"}, state="stopped", running=False, ev=start_events + [
            (15, None, "info", "Скачивание остановлено. Всё скачанное сохранено")])
        disk = db(6015, 1500, pending=4515, names=_DEMO_DISK_FILES)
        unlim = db(13602, 2000, pending=11602, size=69 * gb, names=_DEMO_UNLIM_FILES)
    # «На паузе» показывает и большую папку, которую не успели досчитать: «больше 31 ГБ»
    if kind == "done":
        folders = {"total": 6, "complete": 6}
    rescue_st = build_rescue_status(st, disk, unlim, tok, coo, cfg, {"kind": None}, free,
                                    {"bytes": 31 * gb, "partial": kind == "stopped"}, {"bytes": 12 * gb}, live=live,
                                    disk_folders=folders)
    return {"rescue": rescue_st, "token": tok, "cookies": coo, "disk_db": disk, "unlim_db": unlim,
            "free_space": dict(free, total=2048 * gb, path=cfg.get("download_dir", ""))}
