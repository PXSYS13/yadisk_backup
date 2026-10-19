"""«Нас поддерживают»: список тех, кто поддержал проект, для Web UI.

Список лежит в supporters.json в репозитории: донаты DonationAlerts туда переносит
tools/sync_supporters.py — со страницы виджета, сохранённой из браузера (--from-widget),
или в GitHub Actions по ключу; остальное автор дописывает руками. Программа
берёт свежую версию с GitHub (не чаще раза в 15 минут, ждёт не дольше нескольких
секунд), а без интернета показывает копию, которая пришла вместе с программой.

Виджет DonationAlerts встроить нельзя: он запрещает показ внутри других страниц
(X-Frame-Options: SAMEORIGIN) и без секретного ключа виджета пустой, а ключ в открытом
коде стал бы общим. Поэтому ключ живёт только в секретах GitHub, а здесь нет ни ключей,
ни запросов к DonationAlerts.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import requests

PROJECT = Path(__file__).resolve().parent
LOCAL_FILE = PROJECT / "supporters.json"
REMOTE_URL = "https://raw.githubusercontent.com/PXSYS13/yadisk_backup/main/supporters.json"
# Свежий список берём раз в 15 минут; если GitHub не ответил — пробуем снова через 5 минут
CACHE_SEC = 900
RETRY_SEC = 300
FETCH_TIMEOUT = (3, 5)
MAX_BYTES = 256 * 1024
MAX_ITEMS = 500
MAX_NAME = 40
MAX_MESSAGE = 160
ANONYMOUS = "Аноним"

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")

_lock = threading.Lock()
_cache: dict | None = None  # {"at": monotonic, "until": monotonic, "result": {...}}


def _clean_text(value, limit: int) -> str:
    """Одна строка без лишних пробелов, не длиннее limit (обрезанное — с многоточием)."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _clean_date(value) -> str | None:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _clean_amount(value) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 10 ** 9:
        return None
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


def normalize(raw) -> dict:
    """Проверенный список {"updated", "supporters"}: мусор отброшен, новые сверху.

    Файл правит человек руками, поэтому ошибка в одной записи не должна прятать остальные.
    """
    items = raw.get("supporters") if isinstance(raw, dict) else None
    out: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        currency = item.get("currency")
        out.append({
            "name": _clean_text(item.get("name"), MAX_NAME) or ANONYMOUS,
            "amount": _clean_amount(item.get("amount")),
            "currency": currency if isinstance(currency, str) and _CURRENCY_RE.fullmatch(currency) else "RUB",
            "date": _clean_date(item.get("date")),
            "message": _clean_text(item.get("message"), MAX_MESSAGE) or None,
        })
        if len(out) >= MAX_ITEMS:
            break
    # Сортировка устойчивая: записи одного дня остаются в порядке файла
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return {"updated": _clean_date(raw.get("updated")) if isinstance(raw, dict) else None, "supporters": out}


def count_people(items: list[dict]) -> int:
    """Сколько людей: повторные донаты под одним именем — один человек, анонимы — каждый свой."""
    names = set()
    anonymous = 0
    for item in items:
        if item["name"] == ANONYMOUS:
            anonymous += 1
        else:
            names.add(item["name"].casefold())
    return len(names) + anonymous


def load_local(path: Path | None = None) -> dict | None:
    """Копия списка, пришедшая вместе с программой (None — файла нет или он испорчен)."""
    try:
        with open(path or LOCAL_FILE, encoding="utf-8") as f:
            return normalize(json.load(f))
    except (OSError, ValueError):
        return None


def fetch_remote(url: str | None = None) -> dict | None:
    """Свежий список с GitHub или None (нет сети, ответ не 200, слишком большой, не JSON)."""
    url = REMOTE_URL if url is None else url
    if not url:
        return None
    try:
        with requests.get(url, timeout=FETCH_TIMEOUT, stream=True,
                          headers={"Accept": "application/json"}) as r:
            if r.status_code != 200:
                return None
            body = r.raw.read(MAX_BYTES + 1, decode_content=True)
        if len(body) > MAX_BYTES:
            return None
        return normalize(json.loads(body.decode("utf-8")))
    except (requests.RequestException, OSError, ValueError, UnicodeDecodeError):
        return None


def get_supporters(now: float | None = None) -> dict:
    """Список для «Нас поддерживают»: с GitHub, иначе последний полученный, иначе копия из программы.

    source: github | local — откуда список (для журнала и тестов).
    """
    global _cache
    now = time.monotonic() if now is None else now
    with _lock:
        if _cache and now < _cache["until"]:
            return _cache["result"]
        stale = _cache["result"] if _cache and _cache["result"]["source"] == "github" else None
    remote = fetch_remote()
    if remote is not None:
        result, until = {**remote, "source": "github"}, now + CACHE_SEC
    else:
        local = load_local() or {"updated": None, "supporters": []}
        result, until = stale or {**local, "source": "local"}, now + RETRY_SEC
    with _lock:
        _cache = {"until": until, "result": result}
    return result


# Демо для проверки экранов без сети (YADISK_DEMO=1): имена придуманы
_DEMO = (
    ("Анна П.", 500, 0, "Спасибо, вернула все фото мамы!"), ("Сергей", 300, 1, None),
    ("Мария К.", 1000, 2, "Лучшая программа"), ("Дмитрий", 200, 3, None),
    ("Елена В.", 750, 4, "Спасли архив свадьбы"), ("Игорь", 150, 7, None), ("Ольга", 500, 8, None),
    ("Алексей Н.", 2000, 10, "Так держать"), ("Наталья", 250, 14, None), ("Павел", 100, 15, None),
    ("Татьяна С.", 400, 21, "Спасибо за труд"), ("Аноним", 300, 30, None),
)


def demo(today: date | None = None) -> dict:
    today = today or date.today()
    return {
        "updated": today.isoformat(),
        "source": "demo",
        "supporters": [{"name": n, "amount": a, "currency": "RUB", "date": (today - timedelta(days=d)).isoformat(),
                        "message": m} for n, a, d, m in _DEMO],
    }
