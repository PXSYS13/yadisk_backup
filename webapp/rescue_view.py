"""Сводка «Режима спасения» для Главной: /api/status.rescue (schema 1).

Чистые функции от уже собранных данных: интерфейс не разбирает журналы, а читает
готовые состояние, заголовок, счётчики и признаки жизни. Их легко проверить тестом.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque

from utils import files_text, hhmm
from webapp.local_disk import LOW_FREE_BYTES
from webapp.tasks import SLOT_DISK, SLOT_UNLIM_COLLECT, UNLIM_SLOT_PREFIX
from webapp.texts import ru_bytes

RESCUE_SCHEMA = 1

# Действия, которых ждёт программа от человека (id → тексты; кнопку рисует интерфейс)
RESCUE_ACTIONS = {
    "setup": ("Сохраните свои файлы с Яндекс.Диска на компьютер",
              "Один раз дайте программе доступ к Диску и выберите папку — это 3–5 минут.",
              "Начать настройку"),
    "token": ("Яндекс перестал принимать ключ доступа к Диску",
              "Получите новый ключ — это минута. Скачанное не пропадёт, Безлимит продолжает качаться.",
              "Получить новый ключ"),
    "cookies": ("Нужно обновить доступ к Безлимиту",
                "Яндекс завершил вход, которым пользуется программа. Откройте disk.yandex.ru и "
                "скопируйте данные входа заново, как при настройке.",
                "Обновить доступ"),
    "free_space": ("На диске закончилось место",
                   "Освободите место или выберите папку на другом диске — продолжим с того же места.",
                   "Я освободил место — продолжить"),
    "send_logs": ("Что-то пошло не так",
                  "Скачайте отчёт для поддержки и пришлите его в Telegram — в нём нет ключа и паролей.",
                  "Скачать отчёт"),
    "folders": ("Выбранных папок больше нет на Диске",
                "Их переименовали, перенесли или удалили. Выберите папки заново в «Настройках» — "
                "уже скачанное не пропадёт.",
                "Выбрать папки"),
}
# Ключ к Диску отклонён, а Безлимит не подключён или уже не качается — не обещаем, что он продолжает
TOKEN_ALONE_TEXT = "Получите новый ключ — это минута. Скачанное не пропадёт."
BUSY_STATES = ("checking", "listing", "downloading", "waiting")

# Спокойные пояснения: ничего делать не нужно
RESCUE_NOTICES = {
    "storage_down": ("Часть файлов лежит на серверах Яндекса, которые сейчас не отвечают",
                     "Они не потеряны: программа повторяет их сама. Не удаляйте эти файлы с Диска и "
                     "не нажимайте в приложении Яндекса «Освободить место»."),
    "rate_limit": ("Яндекс просит обращаться реже",
                   "Программа делает паузы и продолжает сама. Ничего делать не нужно."),
    "server_50x": ("Яндекс отвечает с перебоями",
                   "Программа повторяет запросы сама. Ничего делать не нужно."),
    "timeout": ("Яндекс отвечает медленно",
                "Программа подождёт и повторит. Ничего делать не нужно."),
    "bulk_disabled": ("Яндекс сейчас отказывает в скачивании части файлов",
                      "Программа ждёт с паузами и продолжит сама."),
}


class ProgressMeter:
    """Сколько файлов обработано за последние минуты — чтобы было видно, что программа жива.

    Замер — при каждом опросе /api/status: «проверено» = скачано + ждут Яндекса +
    не скачались. Скачанные почти никогда не убывают, а отложенные при повторе
    уходят обратно в работу — поэтому приросты не бывают меньше нуля.
    """

    WINDOW_SEC = 300
    MIN_SPAN_SEC = 20

    def __init__(self):
        self._lock = threading.Lock()
        self._samples: dict[str, deque] = {}

    def sample(self, name: str, checked: int, done: int, now: float | None = None) -> dict | None:
        now = now or time.time()
        with self._lock:
            q = self._samples.setdefault(name, deque())
            q.append((now, checked, done))
            while len(q) > 1 and now - q[0][0] > self.WINDOW_SEC:
                q.popleft()
            t0, c0, d0 = q[0]
        span = now - t0
        if span < self.MIN_SPAN_SEC:
            return None
        return {"window_sec": round(span), "checked": max(0, checked - c0), "done": max(0, done - d0)}

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()


progress_meter = ProgressMeter()
_FOUND_RE = re.compile(r"Найдено[^:\n]*:\s*(\d+)")


def listing_found(task: dict | None) -> int | None:
    """Сколько файлов уже нашёл сбор списка — по последней строке «Найдено: N»."""
    for line in reversed((task or {}).get("log_tail") or []):
        m = _FOUND_RE.search(line)
        if m:
            return int(m.group(1))
    return None


def source_live(name: str, tasks: dict, branch_state: str | None, rate: dict | None) -> dict:
    """Признаки жизни источника: когда его задачи последний раз что-то писали и т. п."""
    if name == "disk":
        snaps = [tasks.get(SLOT_DISK)]
        listing = tasks.get(SLOT_DISK)
    else:
        snaps = [t for slot, t in tasks.items() if slot.startswith(UNLIM_SLOT_PREFIX)]
        listing = tasks.get(SLOT_UNLIM_COLLECT)
    snaps = [t for t in snaps if t]
    last = max((t.get("last_line_at") or 0 for t in snaps), default=0)
    return {
        "last_activity_at": last or None,
        "tasks_running": sum(1 for t in snaps if t.get("running")),
        "listing_found": listing_found(listing) if branch_state == "listing" else None,
        "recent": rate,
    }


def waits_for_person(branch: dict | None) -> bool:
    """Запуск остановился на этой части и ждёт человека: новый ключ, место, папки или отчёт."""
    return bool(branch) and branch.get("state") in ("needs_action", "error")


def db_checked_done(db: dict) -> tuple[int, int]:
    """(проверено, скачано) по db_stats: проверено = скачано + ждут Яндекса + не скачались."""
    excluded = int(db.get("category_skipped") or 0)
    done = int(db.get("downloaded") or 0) + max(0, int(db.get("skipped") or 0) - excluded)
    return done + int(db.get("deferred") or 0) + int(db.get("failed") or 0), done


def source_block(name: str, db: dict, branch: dict | None, configured: bool, folder: str,
                 local: dict | None, window_sec: float | None, live: dict | None = None,
                 now: float | None = None) -> dict:
    db = db or {}
    live = live or {}
    # «За последние N минут» — не дольше, чем идёт текущий этап: иначе сразу после
    # сбора списка вышло бы «за 5 минут новых файлов нет»
    recent = live.get("recent")
    since = (branch or {}).get("state_since")
    if recent and since and now:
        span = min(float(recent.get("window_sec") or 0), now - since)
        recent = dict(recent, window_sec=round(span)) if span >= ProgressMeter.MIN_SPAN_SEC else None
    total = int(db.get("total") or 0)
    excluded = int(db.get("category_skipped") or 0)
    deferred = int(db.get("deferred") or 0)
    pending_all = int(db.get("pending") or 0)
    files = {
        "total": total,
        "selected": max(0, total - excluded),
        "done": int(db.get("downloaded") or 0) + max(0, int(db.get("skipped") or 0) - excluded),
        "pending": max(0, pending_all - deferred),
        "in_progress": int(db.get("in_progress") or 0),
        "deferred": deferred,
        "failed": int(db.get("failed") or 0),
        "excluded": excluded,
        "preview_only": int(db.get("previews") or 0),
    }
    bytes_ = {
        "total": int(db.get("bytes_total") or 0),
        "selected": int(db.get("bytes_selected_total") or 0),
        "done": int(db.get("bytes_done") or 0),
        "pending": max(0, int(db.get("bytes_pending") or 0) - int(db.get("bytes_deferred") or 0)),
        "in_progress": int(db.get("bytes_in_progress") or 0),
        "deferred": int(db.get("bytes_deferred") or 0),
        "failed": int(db.get("bytes_failed") or 0),
        "excluded": int(db.get("category_skipped_bytes") or 0),
    }
    # Скорость и «осталось» — только по честной выборке: первые минуты Диск качает
    # крошечные файлы (очередь идёт от маленьких к большим), и прогноз был бы бредом
    speed = None
    if window_sec and window_sec >= 120 and (db.get("bytes_10m") or 0) >= 20 * 1024 * 1024:
        speed = float(db["bytes_10m"]) / min(600.0, window_sec)
    left = bytes_["pending"] + bytes_["in_progress"]
    eta = (left / speed) if speed and left else None
    if eta is not None and eta > 14 * 86400:
        eta = None
    branch = branch or {}
    state = branch.get("state") or ("idle" if configured else "off")
    if state == "off" and configured:
        state = "idle"
    return {
        "configured": configured,
        "enabled": configured and branch.get("state") != "off",
        "state": state,
        "activity": branch.get("activity") or "",
        "reason": branch.get("reason") or "",
        "action": branch.get("action"),
        "round": branch.get("round") or 0,
        "next_retry_at": branch.get("next_retry_at"),
        "wait_kind": branch.get("wait_kind"),
        "last_exit": branch.get("last_exit"),
        "files": files,
        "bytes": bytes_,
        "storage_hosts_down": int(db.get("deferred_hosts") or 0),
        "speed_bps": speed,
        "eta_sec": eta,
        # size_partial — папку не успели обойти целиком: в ней больше, чем size_bytes
        "folder": {"path": folder, "size_bytes": int((local or {}).get("bytes") or 0),
                   "size_partial": bool((local or {}).get("partial"))},
        # Признаки жизни: последние скачанные файлы, отклик задач, прирост за минуты
        "recent_files": [
            {"name": str(r.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1],
             "size": int(r.get("size") or 0), "at": r.get("at")}
            for r in (db.get("recent") or []) if r.get("status") == "downloaded"][:5],
        "last_activity_at": live.get("last_activity_at"),
        "tasks_running": int(live.get("tasks_running") or 0),
        "listing_found": live.get("listing_found"),
        "recent": recent,
        # Когда собран список: на карточке «Список файлов собран в 14:05 · Обновить список»
        "listed_at": db.get("listed_at"),
    }


def build_rescue_status(rescue_st: dict, disk_db: dict, unlim_db: dict, token: dict, cookies: dict,
                        cfg: dict, diag: dict | None = None, free_space: dict | None = None,
                        disk_local: dict | None = None, unlim_local: dict | None = None,
                        now: float | None = None, live: dict | None = None,
                        disk_folders: dict | None = None) -> dict:
    """Всё, что нужно главному экрану, одним объектом: без разбора логов во фронтенде.

    Чистая функция от уже собранных данных — её легко проверить тестом. live —
    признаки жизни по источникам (см. source_live), их собирает /api/status.
    disk_folders — сводка disk_folders_summary: сколько папок Диска скачано целиком.
    """
    now = now or time.time()
    live = live or {}
    branches = rescue_st.get("branches") or {}
    running = bool(rescue_st.get("running"))
    started = rescue_st.get("started_at")
    window = (now - started) if (running and started) else None
    download_dir = cfg.get("download_dir", "")
    disk_branch = branches.get("disk") if started else None
    unlim_branch = branches.get("unlim") if started else None
    # Часть подключена, если доступ к ней работает — или если запуск остановился на ней и ждёт
    # человека. Яндекс перестал принимать ключ: проверка ключа больше не проходит, но скачанное
    # Диском никуда не делось, а кнопку «Получить новый ключ» даёт действие этой части
    disk_on = bool(token.get("ok") or token.get("unreachable")) or waits_for_person(disk_branch)
    unlim_on = bool(cookies.get("ok")) or waits_for_person(unlim_branch)
    sources = {
        "disk": source_block("disk", disk_db, disk_branch, disk_on, download_dir, disk_local, window,
                             live.get("disk"), now),
        "unlim": source_block("unlim", unlim_db, unlim_branch, unlim_on,
                              os.path.join(download_dir, "_unlim") if download_dir else "",
                              unlim_local, window, live.get("unlim"), now),
    }
    sources["disk"]["folders"] = ({"total": int(disk_folders.get("total") or 0),
                                   "complete": int(disk_folders.get("complete") or 0)} if disk_folders else None)
    active = [s for s in sources.values() if s["enabled"]]

    # Состояние главного экрана
    branch_states = {s["state"] for s in active}
    if running:
        if "needs_action" in branch_states or "error" in branch_states:
            state = "needs_action"
        elif branch_states and branch_states <= {"waiting", "done", "off"} and "waiting" in branch_states:
            state = "waiting"
        else:
            state = "running"
    elif not started:
        state = "idle"
    else:
        state = {"done": "done", "failed": "needs_action", "stopped": "stopped"}.get(rescue_st.get("state"), "idle")

    # Что нужно от человека
    action_id, action_source = None, None
    key_renewed = False
    for name in ("disk", "unlim"):
        a = sources[name].get("action")
        if a and sources[name]["state"] in ("needs_action", "error"):
            if a == "token" and not running and token.get("ok"):
                # Запуск закончился на отклонённом ключе, а человек уже ввёл рабочий: дальше —
                # «Продолжить», а не снова «Получить новый ключ». Доступ к Безлимиту так не узнать:
                # его проверка смотрит только на файл, а не на то, пускает ли Яндекс
                key_renewed = True
                sources[name].update(state="stopped", action=None, reason="Ключ обновлён — можно продолжать")
                continue
            action_id, action_source = a, name
            break
    if action_id is None and state == "needs_action":
        if key_renewed:
            state = "stopped"
        else:
            action_id, action_source = "send_logs", "all"
    # Первая настройка — только когда не подключено ничего: часть, на которой запуск
    # остановился, считается подключённой, и её действие не заменяется рассказом о настройке
    if not running and not (disk_on or unlim_on):
        action_id, action_source = "setup", "all"
    action = None
    if action_id in RESCUE_ACTIONS:
        title, text, button = RESCUE_ACTIONS[action_id]
        variant = None
        if action_id == "token" and not (running and sources["unlim"]["state"] in BUSY_STATES):
            text = TOKEN_ALONE_TEXT
        if action_id == "free_space":
            free = (free_space or {}).get("free")
            if free is not None and free < LOW_FREE_BYTES:
                text = f"Свободно {ru_bytes(free)}. {text}"
            else:
                # Место есть — значит, папка недоступна: диск отключён, путь неверный или нет прав
                variant = "folder"
                title = "Не получается записать файлы в папку"
                text = ("Проверьте, что диск с папкой подключён и папка доступна, или выберите другую "
                        "папку — продолжим с того же места.")
                button = "Попробовать снова"
        action = {"id": action_id, "source": action_source, "title": title, "text": text, "button": button,
                  "variant": variant}

    # Спокойное пояснение
    notice = None
    if not action:
        kind = "storage_down" if any(s["files"]["deferred"] for s in active) else (diag or {}).get("kind")
        if kind in RESCUE_NOTICES:
            title, text = RESCUE_NOTICES[kind]
            notice = {"kind": kind, "title": title, "text": text}

    totals = {k: sum(s["files"][k] for s in active) for k in
              ("selected", "done", "pending", "in_progress", "deferred", "failed", "preview_only")}
    done_bytes = sum(s["bytes"]["done"] for s in active)
    next_retry = rescue_st.get("next_retry_at")
    complete = bool(active) and not (totals["pending"] or totals["in_progress"]
                                     or totals["deferred"] or totals["failed"])

    if action and action_id == "setup":
        headline, subline = action["title"], action["text"]
    elif state == "idle":
        what = [n for n, s in (("Диск", sources["disk"]), ("Безлимит", sources["unlim"])) if s["configured"]]
        headline = "Всё готово к скачиванию"
        subline = f"Скачаем {' и '.join(what) or 'ваши файлы'} в папку {download_dir}"
    elif state == "running" and active and all(s["state"] in ("checking", "listing") for s in active):
        headline = "Готовлюсь к скачиванию"
        # Большой Диск Яндекс перечисляет долго: люди видели «минута-две» и думали, что программа зависла
        subline = ("Проверяю доступ и получаю у Яндекса список ваших файлов. Обычно это несколько минут, "
                   "а если файлов сотни тысяч — до часа. Счётчик показывает, сколько уже найдено.")
    elif state == "running":
        headline = "Скачиваем ваши файлы"
        subline = ("Ничего делать не нужно: программа сама скачивает файлы и повторяет те, что Яндекс "
                   "пока не отдаёт. Только не закрывайте окно программы.")
    elif state == "waiting" and "crash" in {s.get("wait_kind") for s in active}:
        headline = "Скачивание прервалось — пробую ещё раз"
        subline = (f"Программа запустит скачивание снова в {hhmm(next_retry)}. Уже скачанное сохранено. "
                   "Если это повторится, она попросит прислать отчёт для поддержки.")
    elif state == "waiting" and {s.get("wait_kind") for s in active if s["state"] == "waiting"} == {"outage"}:
        headline = "Яндекс сейчас не отвечает"
        subline = (f"Программа попробует снова в {hhmm(next_retry)}. Уже скачанное сохранено — "
                   "оставьте программу открытой.")
    elif state == "waiting":
        headline = "Всё, что Яндекс отдаёт сейчас, уже скачано"
        previews = (f" Для {files_text(totals['preview_only'])} уже сохранены превью — уменьшенные копии."
                    if totals["preview_only"] else "")
        subline = (f"Ждут Яндекса: {files_text(totals['deferred'])}. Следующая попытка в {hhmm(next_retry)}."
                   f"{previews} Оставьте программу открытой.")
    elif state == "needs_action" and action and running:
        # Действие — в баннере над карточкой; здесь — что продолжает качаться
        headline = "Скачиваем ваши файлы"
        subline = "Часть файлов ждёт вашего действия — оно описано выше. Остальное продолжает качаться."
    elif state == "needs_action" and action:
        headline, subline = action["title"], action["text"]
    elif state == "done":
        if complete:
            headline = "Готово! Все ваши файлы сохранены"
            subline = f"{files_text(totals['done'])}, {ru_bytes(done_bytes)} — в папке {download_dir}"
        else:
            headline = "Скачано почти всё"
            subline = (f"Сохранено {files_text(totals['done'])}. Не скачались: {files_text(totals['failed'])}"
                       + (f", ждут Яндекса: {files_text(totals['deferred'])}" if totals["deferred"] else ""))
    elif state == "stopped" and key_renewed:
        headline = "Ключ обновлён — можно продолжать"
        subline = "Нажмите «Продолжить»: программа докачает остальное с того же места. Уже скачанное сохранено."
    elif state == "stopped":
        headline = "Скачивание на паузе"
        subline = "Уже скачанное сохранено. «Продолжить» докачает остальное без повторов."
    else:
        headline, subline = "Скачиваем ваши файлы", ""

    windows = [s["recent"] for s in active if s.get("recent")]
    recent = ({"window_sec": max(w["window_sec"] for w in windows),
               "checked": sum(w["checked"] for w in windows),
               "done": sum(w["done"] for w in windows)} if windows else None)

    # Хватит ли места: всё, что ещё не скачано (и то, что ждёт Яндекса), против свободного
    free_bytes = (free_space or {}).get("free")
    need = sum(s["bytes"]["pending"] + s["bytes"]["in_progress"] + s["bytes"]["deferred"] + s["bytes"]["failed"]
               for s in active)
    space = {"free": free_bytes, "need": need, "short": free_bytes is not None and need > free_bytes,
             "fs": (free_space or {}).get("fs"), "fat": bool((free_space or {}).get("fat"))}

    return {
        "schema": RESCUE_SCHEMA,
        "state": state,
        "running": running,
        "now": now,
        "headline": headline,
        "subline": subline,
        "started_at": started,
        "finished_at": None if running else rescue_st.get("finished_at"),
        "next_retry_at": next_retry,
        "can_retry_now": any(s["state"] == "waiting" for s in active),
        "action": action,
        "notice": notice,
        "space": space,
        "sources": sources,
        # Лента «Что происходит» — новые сверху; признаки жизни — по всем источникам
        "events": list(reversed(rescue_st.get("events") or []))[:12] if started else [],
        "last_activity_at": max((s.get("last_activity_at") or 0 for s in active), default=0) or None,
        "recent": recent,
        "summary": {
            "complete": complete,
            "selected_files": totals["selected"],
            "left_files": totals["pending"] + totals["in_progress"],
            "selected_bytes": sum(s["bytes"]["selected"] or s["bytes"]["total"] for s in active),
            "downloaded_files": totals["done"],
            "downloaded_bytes": done_bytes,
            "deferred_files": totals["deferred"],
            "failed_files": totals["failed"],
            "preview_only_files": totals["preview_only"],
            "skipped_sources": [n for n, s in sources.items() if not s["configured"]],
            "photo_unlim_bytes": int((token.get("account") or {}).get("photo_unlim") or 0),
        },
    }
