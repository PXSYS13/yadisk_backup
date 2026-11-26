"""Где лежат файлы программы.

Модули читают пути в момент вызова (paths.DB_FILE), а не копируют их при импорте:
tests/conftest.py подменяет их на временные, и подмена должна доходить до всех.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT / ".env"
# .env читаем раньше всего остального: в нём бывает YADISK_PORT, а порт выбирается при импорте
load_dotenv(ENV_FILE)

DB_FILE = PROJECT / "state.db"
UNLIM_DB_FILE = PROJECT / "unlim_state.db"
LINKS_DB_FILE = PROJECT / "links_state.db"
UPLOAD_DB_FILE = PROJECT / "upload_unlim_state.db"
UPLOAD_CONFIG_FILE = PROJECT / "upload_unlim_config.json"
PEOPLE_DB_FILE = PROJECT / "people_sorter.db"
PEOPLE_CONFIG_FILE = PROJECT / "people_sorter_config.json"
PEOPLE_MODELS_DIR = PROJECT / ".models"
PEOPLE_CACHE_DIR = PROJECT / ".cache"
PEOPLE_CROPS_DIR = PEOPLE_CACHE_DIR / "crops"
PEOPLE_THUMBS_DIR = PEOPLE_CACHE_DIR / "thumbs"
COOKIES_FILE = PROJECT / "cookies.json"
TEMPLATES_DIR = PROJECT / "webui_assets"
LOG_DIR = PROJECT / "logs"
TASK_LOG_DIR = LOG_DIR / "tasks"
WEBUI_LOG = LOG_DIR / "webui.log"
HEALTH_LOG = LOG_DIR / "yandex_health.jsonl"
