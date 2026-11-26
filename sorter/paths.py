"""Пути подсистемы сортировки по людям, проксируемые из webapp.paths yadisk_backup.

При работе внутри yadisk_backup пути автоматически следуют за webapp.paths (включая изоляцию
в tests/conftest.py). Вызов configure_data_dir(root) позволяет явно перенаправить каталог данных
в тестах или CLI (--data-dir).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from webapp import paths as _app_paths

BASE_DIR: Path = _app_paths.PROJECT
ASSETS_DIR: Path = _app_paths.TEMPLATES_DIR

_override_root: Path | None = None
_env_data = os.environ.get("PHOTO_SORTER_DATA_DIR", "").strip()
if _env_data:
    _override_root = Path(_env_data).resolve()


def configure_data_dir(root: Path | str | None) -> Path:
    """Перенаправляет изменяемые пути сортировщика в root (или None для возврата к webapp.paths)."""
    global _override_root
    if root is None:
        _override_root = None
        return _app_paths.PROJECT
    _override_root = Path(root).resolve()
    return _override_root


def __getattr__(name: str) -> Path:
    if _override_root is not None:
        mapping = {
            "DATA_DIR": _override_root,
            "DB_FILE": _override_root / "people_sorter.db",
            "CONFIG_FILE": _override_root / "people_sorter_config.json",
            "MODELS_DIR": _override_root / ".models",
            "CACHE_DIR": _override_root / ".cache",
            "CROPS_DIR": _override_root / ".cache" / "crops",
            "THUMBS_DIR": _override_root / ".cache" / "thumbs",
            "LOG_DIR": _override_root / "logs",
            "WEBUI_LOG": _override_root / "logs" / "webui.log",
        }
        if name in mapping:
            return mapping[name]
    else:
        mapping = {
            "DATA_DIR": _app_paths.PROJECT,
            "DB_FILE": _app_paths.PEOPLE_DB_FILE,
            "CONFIG_FILE": _app_paths.PEOPLE_CONFIG_FILE,
            "MODELS_DIR": _app_paths.PEOPLE_MODELS_DIR,
            "CACHE_DIR": _app_paths.PEOPLE_CACHE_DIR,
            "CROPS_DIR": _app_paths.PEOPLE_CROPS_DIR,
            "THUMBS_DIR": _app_paths.PEOPLE_THUMBS_DIR,
            "LOG_DIR": _app_paths.LOG_DIR,
            "WEBUI_LOG": _app_paths.WEBUI_LOG,
        }
        if name in mapping:
            return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def ensure_runtime_dirs() -> None:
    """Создаёт каталоги для моделей, кеша кропов, превью и логов (не трогая исходники)."""
    models_dir = __getattr__("MODELS_DIR")
    cache_dir = __getattr__("CACHE_DIR")
    crops_dir = __getattr__("CROPS_DIR")
    thumbs_dir = __getattr__("THUMBS_DIR")
    log_dir = __getattr__("LOG_DIR")
    for d in (models_dir, cache_dir, crops_dir, thumbs_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)
    if not sys.platform.startswith("win"):
        for private_dir in (cache_dir, crops_dir, thumbs_dir, log_dir):
            try:
                os.chmod(private_dir, 0o700)
            except OSError:
                pass
