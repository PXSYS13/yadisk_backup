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


@router.get("/", response_class=HTMLResponse)
def index():
    html_path = paths.TEMPLATES_DIR / "app.html"
    if not html_path.exists():
        return HTMLResponse("<h1>UI не установлен</h1>")
    html = html_path.read_text(encoding="utf-8")
    # ?v=версия у статики — после обновления браузер не возьмёт старый JS из кэша
    return HTMLResponse(html.replace("__APP_VERSION__", APP_VERSION))


@router.get("/donate")
def donate():
    """Перенаправляет на экран «Поддержать» (README даёт этот адрес людям)."""
    return RedirectResponse(url="/#/support", status_code=307)


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
    top_min = data.get("top_min_rub", supporters.TOP_MIN_RUB)
    return {"ok": True, **data, "people": supporters.count_people(data["supporters"]),
            "top": supporters.top_supporters(data["supporters"], top_min), "top_min_rub": top_min,
            "donation_url": DONATION_URL}
