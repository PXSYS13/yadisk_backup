"""Общая настройка тестов: корень проекта в sys.path, изоляция от файлов проекта."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_project_files(tmp_path, monkeypatch):
    """Web UI читает/пишет .env, cookies.json и БД в корне проекта.

    Тесты не должны ни зависеть от них, ни портить их: подменяем пути на tmp.
    """
    import webui

    monkeypatch.setattr(webui, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(webui, "COOKIES_FILE", tmp_path / "cookies.json")
    monkeypatch.setattr(webui, "DB_FILE", tmp_path / "state.db")
    monkeypatch.setattr(webui, "UNLIM_DB_FILE", tmp_path / "unlim_state.db")
    monkeypatch.setattr(webui, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(webui, "TASK_LOG_DIR", tmp_path / "logs" / "tasks")
    monkeypatch.setattr(webui, "WEBUI_LOG", tmp_path / "logs" / "webui.log")
    monkeypatch.setattr(webui, "HEALTH_LOG", tmp_path / "logs" / "yandex_health.jsonl")
    # Настройки, которые программа пишет прямо в os.environ: после теста — как было.
    # Пустое значение = «по умолчанию» (всё качать, превью включены, демо выключено)
    for key in ("BACKUP_SOURCES", "BACKUP_CATEGORIES", "BACKUP_DISK_FOLDERS", "GENTLE_MODE",
                "YADISK_PREVIEWS", "YADISK_DEMO", "YADISK_RETRY_EVERY_SEC", "YA_DISK_TOKEN"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "downloads"))
