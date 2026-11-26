"""Пути подсистемы загрузки в Безлимит, проксируемые из webapp.paths yadisk_backup.

Чтение через __getattr__ гарантирует, что подмена атрибутов webapp.paths в tests/conftest.py
мгновенно перенаправляет все файлы (cookies.json, БД очереди, конфиг и логи) во временную папку.
"""

from __future__ import annotations

from pathlib import Path

from webapp import paths as _app_paths

PROJECT = _app_paths.PROJECT
ASSETS_DIR = _app_paths.TEMPLATES_DIR


def __getattr__(name: str) -> Path:
    if name == "COOKIES_FILE":
        return _app_paths.COOKIES_FILE
    if name == "DB_FILE":
        return _app_paths.UPLOAD_DB_FILE
    if name == "CONFIG_FILE":
        return _app_paths.UPLOAD_CONFIG_FILE
    if name == "ENV_FILE":
        return _app_paths.ENV_FILE
    if name == "LOG_DIR":
        return _app_paths.LOG_DIR
    if name == "UPLOAD_LOG":
        return _app_paths.LOG_DIR / "upload.log"
    if name == "WEBUI_LOG":
        return _app_paths.WEBUI_LOG
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
