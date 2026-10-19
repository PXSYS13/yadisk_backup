"""Новая версия на GitHub: Главная подсказывает, что пора обновиться.

Программу скачивают ZIP-архивом и запускают webui.bat, поэтому о новых версиях люди
узнают только из Telegram, а важные исправления выходят почти каждый день. Раз в 6 часов
программа спрашивает у GitHub номер последнего релиза и, если он новее, показывает
спокойную плашку со ссылкой на архив. Сама она ничего не скачивает и не устанавливает.

GitHub спрашиваем в фоновом потоке: /api/status опрашивается раз в 1,5 с, и ждать GitHub
там нельзя. Нет сети, GitHub не ответил или ответил ерундой — молчим и помним последний
удачный ответ.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

import requests

import utils

log = logging.getLogger("webui")

REPO = "PXSYS13/yadisk_backup"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
# Запасная ссылка «Что нового», если GitHub прислал странную страницу релиза
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
CHECK_EVERY_SEC = 6 * 3600
FETCH_TIMEOUT = (3, 5)
# Обычно ответ — несколько КБ; описание релиза бывает до 125 000 знаков, по-русски это до 250 КБ
MAX_BYTES = 512 * 1024

_VERSION_RE = re.compile(r"v?(\d{1,6})\.(\d{1,6})(?:\.(\d{1,6}))?")

_lock = threading.Lock()
# asked_at — когда последний раз спрашивали GitHub (time.monotonic), latest — последний удачный ответ
_state: dict = {"asked_at": None, "latest": None, "busy": False}
_thread: threading.Thread | None = None  # последняя фоновая проверка (тесты ждут её)


def parse_version(text) -> tuple[int, int, int] | None:
    """«v2.7.10» → (2, 7, 10); не номер версии → None.

    Сравнивать версии строками нельзя: строкой «2.10.0» меньше «2.9.0».
    """
    if not isinstance(text, str):
        return None
    m = _VERSION_RE.fullmatch(text.strip())
    if not m:
        return None
    major, minor, patch = m.groups()
    return int(major), int(minor), int(patch or 0)


def is_newer(latest, current) -> bool:
    """Версия latest новее current (оба — строки вида «2.7.2» или «v2.7.2»)."""
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def fetch_latest(url: str | None = None) -> dict | None:
    """Последний релиз на GitHub: {"version": "2.8.0", "url": страница релиза} или None.

    None — нет сети, ответ не 200 (в том числе лимит запросов GitHub), слишком большой,
    не JSON или без номера версии. Проверка необязательная, поэтому ни одна ошибка
    отсюда не выходит.
    """
    url = RELEASES_URL if url is None else url
    if not url:
        return None
    try:
        with requests.get(url, timeout=FETCH_TIMEOUT, stream=True,
                          headers={"Accept": "application/vnd.github+json", "User-Agent": "yadisk_backup"}) as r:
            if r.status_code != 200:
                return None
            body = r.raw.read(MAX_BYTES + 1, decode_content=True)
        if len(body) > MAX_BYTES:
            return None
        data = json.loads(body.decode("utf-8"))
    except Exception:  # обрыв посреди ответа приходит исключением urllib3, а не requests
        return None
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    version = parse_version(data.get("tag_name"))
    if version is None:
        return None
    page = data.get("html_url")
    if not isinstance(page, str) or not page.startswith(f"https://github.com/{REPO}/releases/"):
        page = RELEASES_PAGE
    return {"version": "%d.%d.%d" % version, "url": page}


def update_info(latest: dict | None, current: str | None = None) -> dict | None:
    """{current, latest, url} для /api/status.update, если latest новее current, иначе None."""
    current = utils.APP_VERSION if current is None else current
    if not latest or not is_newer(latest.get("version"), current):
        return None
    return {"current": current, "latest": latest["version"], "url": latest["url"]}


def check() -> dict | None:
    """Спросить GitHub сейчас. Удачный ответ запоминается, неудача ничего не стирает."""
    found = None
    try:
        found = fetch_latest()
    finally:
        with _lock:
            if found is not None:
                _state["latest"] = found
            _state["busy"] = False
    if found is None:
        log.info("проверка новой версии: GitHub не ответил, спрошу снова через %d ч", CHECK_EVERY_SEC // 3600)
    elif is_newer(found["version"], utils.APP_VERSION):
        log.info("на GitHub вышла версия %s (у нас %s)", found["version"], utils.APP_VERSION)
    return found


def snapshot(now: float | None = None) -> dict | None:
    """/api/status.update: сразу из памяти, а GitHub — в фоне, не чаще раза в 6 часов.

    Первый ответ GitHub появится в одном из следующих опросов статуса.
    """
    global _thread
    now = time.monotonic() if now is None else now
    with _lock:
        asked = _state["asked_at"]
        start = (bool(RELEASES_URL) and not _state["busy"]
                 and (asked is None or now - asked >= CHECK_EVERY_SEC))
        if start:
            _state.update(asked_at=now, busy=True)
        latest = _state["latest"]
    if start:
        try:
            _thread = threading.Thread(target=check, name="update-check", daemon=True)
            _thread.start()
        except RuntimeError:  # поток не запустился — спросим в следующий раз
            with _lock:
                _state["busy"] = False
    return update_info(latest)


def demo(current: str | None = None) -> dict:
    """Демо-состояние update: «вышла» следующая версия."""
    current = utils.APP_VERSION if current is None else current
    major, minor, _patch = parse_version(current) or (0, 0, 0)
    return {"current": current, "latest": f"{major}.{minor + 1}.0", "url": RELEASES_PAGE}
