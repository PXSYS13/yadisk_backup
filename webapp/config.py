"""Настройки программы в .env: ключ, папка скачивания, что и как качать.

Всё живёт в .env, чтобы пережить перезапуск программы. Кто хочет подменить настройки
в тесте, подменяет config.get_config: модули зовут её через модуль, а не копией.
"""

from __future__ import annotations

import os

from dotenv import dotenv_values, load_dotenv, set_key
from fastapi import HTTPException

from utils import CATEGORY_LABELS, MAX_DISK_FOLDERS, decode_disk_folders, previews_enabled, safe_int
from webapp import paths

# Источники, которые умеет качать «Режим спасения»
SOURCES = ("disk", "unlim")


def get_config() -> dict:
    """Перечитывает .env заново."""
    load_dotenv(paths.ENV_FILE, override=True)
    try:
        saved = dotenv_values(paths.ENV_FILE) if paths.ENV_FILE.exists() else {}
    except Exception:
        saved = {}
    return {
        "token": (os.getenv("YA_DISK_TOKEN", "") or "").strip(),
        "download_dir": (os.getenv("DOWNLOAD_DIR", str(paths.PROJECT / "downloads")) or "").strip(),
        # Папку выбирали осознанно (есть в .env), а не взяли дефолт
        "download_dir_set": bool((saved.get("DOWNLOAD_DIR") or "").strip()),
        "max_workers": safe_int(os.getenv("MAX_WORKERS"), default=4, min_val=1, max_val=16),
    }


def mask_token(token: str) -> str:
    """Маскирует токен для отображения: y0_xxxx****xxxx"""
    if not token or len(token) < 8:
        return ""
    if len(token) <= 12:
        return token[:3] + "***"
    return f"{token[:6]}…{token[-4:]} ({len(token)} симв.)"


def env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def get_settings() -> dict:
    """Что и как качать — живёт в .env, чтобы пережить перезапуск программы."""
    get_config()  # перечитать .env
    raw_sources = (os.getenv("BACKUP_SOURCES", "") or "disk,unlim").split(",")
    sources = [s for s in SOURCES if s in {x.strip() for x in raw_sources}] or list(SOURCES)
    raw_cats = (os.getenv("BACKUP_CATEGORIES", "") or "").strip()
    categories = [c for c in (x.strip() for x in raw_cats.split(",")) if c in CATEGORY_LABELS] or None
    return {
        "sources": sources,
        "categories": categories,          # None — всё
        # Папки Диска: None — весь Диск. JSON в .env, см. utils.encode_disk_folders
        "disk_folders": decode_disk_folders(os.getenv("BACKUP_DISK_FOLDERS")),
        "max_disk_folders": MAX_DISK_FOLDERS,
        "gentle": env_flag("GENTLE_MODE", False),
        "previews": previews_enabled(),
        "category_labels": dict(CATEGORY_LABELS),
    }


def ensure_env_file() -> None:
    if not paths.ENV_FILE.exists():
        paths.ENV_FILE.write_text("", encoding="utf-8")


def env_set(key: str, value: str, quote_mode: str = "always") -> None:
    """Пишет ключ в .env и сразу перечитывает .env в окружение программы."""
    ensure_env_file()
    set_key(str(paths.ENV_FILE), key, value, quote_mode=quote_mode)
    load_dotenv(paths.ENV_FILE, override=True)


def env_set_verified(key: str, value: str) -> None:
    """Пишет ключ в .env и перечитывает: то, что прочтёт программа, обязано совпасть.

    python-dotenv сам экранирует \\ и ' в одинарных кавычках (с версии 1.2.3), но
    подставляет ${…} — значение с таким куском после перечитывания изменилось бы.
    """
    set_key(str(paths.ENV_FILE), key, value, quote_mode="always")
    try:
        saved = dotenv_values(paths.ENV_FILE).get(key)
    except Exception:  # noqa: BLE001 — нечитаемый .env тоже повод отказать
        saved = None
    if (saved or "") != value:
        raise HTTPException(500, "Не получилось сохранить настройку: файл .env прочитался иначе. "
                                 "Скачайте отчёт для поддержки во вкладке «Помощь»")
    os.environ[key] = value
