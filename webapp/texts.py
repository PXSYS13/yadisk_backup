"""Короткие фразы для интерфейса: размеры по-русски, «когда это было», имена папок.

Число со словом («1 234 файла») и время «14:05» — в utils (files_text, hhmm): их
печатают и CLI-скрипты.
"""

from __future__ import annotations

import time
from datetime import datetime

from utils import hhmm


def ru_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return (f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.1f} {unit}").replace(".", ",")
        n /= 1024
    return f"{n:.1f} ТБ"


def when_text(ts: float, now: float | None = None) -> str:
    """«в 14:05», «вчера в 22:10», «12.10 в 09:00» — когда это было, по-человечески."""
    day = datetime.fromtimestamp(ts).date()
    today = datetime.fromtimestamp(now or time.time()).date()
    hm = hhmm(ts)
    if day == today:
        return f"в {hm}"
    if (today - day).days == 1:
        return f"вчера в {hm}"
    return f"{day:%d.%m} в {hm}"


def folder_label(path: str) -> str:
    """'disk:/Работа/2024' → 'Работа/2024' — как папку показывает интерфейс."""
    return path.removeprefix("disk:").strip("/") or "Диск"


def folders_text(folders: list[str], limit: int = 3) -> str:
    """«Фото, Работа/2024 и ещё 2» — коротко для журнала и ленты событий."""
    names = [folder_label(f) for f in folders]
    head = ", ".join(f"«{n}»" for n in names[:limit])
    return head + (f" и ещё {len(names) - limit}" if len(names) > limit else "")
