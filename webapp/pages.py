"""Страницы и мелкие адреса: интерфейс (/), «Поддержать» (/donate), /api/hello, /api/supporters."""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

import supporters
from utils import APP_VERSION, DONATION_URL
from webapp import paths
from webapp.ports import APP_ID

router = APIRouter()

# Какой интерфейс открывать по «/»: new — версия 2.0, old — прежний (?ui=old)
UI_DEFAULT = "new"


def _new_ui(ui: str | None = None) -> bool:
    return (ui or UI_DEFAULT) == "new" and (paths.TEMPLATES_DIR / "app.html").exists()


@router.get("/", response_class=HTMLResponse)
def index(ui: str | None = None):
    html_path = paths.TEMPLATES_DIR / ("app.html" if _new_ui(ui) else "index.html")
    if not html_path.exists():
        return HTMLResponse("<h1>UI не установлен</h1>")
    html = html_path.read_text(encoding="utf-8")
    # ?v=версия у статики — после обновления браузер не возьмёт старый JS из кэша
    return HTMLResponse(html.replace("__APP_VERSION__", APP_VERSION))


@router.get("/donate")
def donate():
    """Перенаправляет на экран «Поддержать» (README даёт этот адрес людям).

    Новый интерфейс не знает ?tab= прежнего и открывал «Главную»: ему — #/support,
    прежнему — его вкладка доната.
    """
    return RedirectResponse(url="/#/support" if _new_ui() else "/?tab=donate", status_code=307)


@router.get("/api/hello")
def api_hello():
    """Кто держит порт: вторая копия программы находит по нему первую и не запускается."""
    return {"app": APP_ID, "version": APP_VERSION, "folder": str(paths.PROJECT)}


@router.get("/api/supporters")
def api_supporters(demo: str | None = None):
    """«Нас поддерживают»: список ведёт автор в supporters.json на GitHub.

    Синхронная функция — опрос /api/status не ждёт GitHub; список кешируется.
    """
    data = supporters.demo() if demo and os.environ.get("YADISK_DEMO") == "1" else supporters.get_supporters()
    return {"ok": True, **data, "people": supporters.count_people(data["supporters"]),
            "donation_url": DONATION_URL}
