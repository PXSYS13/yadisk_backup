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
    import public_download
    import supporters
    import webapp.paths
    import webapp.updates

    monkeypatch.setattr(webapp.paths, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(webapp.paths, "COOKIES_FILE", tmp_path / "cookies.json")
    monkeypatch.setattr(webapp.paths, "DB_FILE", tmp_path / "state.db")
    monkeypatch.setattr(webapp.paths, "UNLIM_DB_FILE", tmp_path / "unlim_state.db")
    monkeypatch.setattr(webapp.paths, "LINKS_DB_FILE", tmp_path / "links_state.db")
    monkeypatch.setattr(public_download, "DB_FILE", tmp_path / "links_state.db")
    monkeypatch.setattr(public_download, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(webapp.paths, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(webapp.paths, "TASK_LOG_DIR", tmp_path / "logs" / "tasks")
    monkeypatch.setattr(webapp.paths, "WEBUI_LOG", tmp_path / "logs" / "webui.log")
    monkeypatch.setattr(webapp.paths, "HEALTH_LOG", tmp_path / "logs" / "yandex_health.jsonl")
    # «Нас поддерживают»: без похода на GitHub и без настоящего supporters.json
    monkeypatch.setattr(supporters, "REMOTE_URL", "")
    monkeypatch.setattr(supporters, "LOCAL_FILE", tmp_path / "supporters.json")
    monkeypatch.setattr(supporters, "_cache", None)
    # Проверка новой версии: без похода на GitHub, память о прошлых ответах — с нуля
    monkeypatch.setattr(webapp.updates, "RELEASES_URL", "")
    monkeypatch.setattr(webapp.updates, "_state", {"asked_at": None, "latest": None, "busy": False})
    monkeypatch.setattr(webapp.updates, "_thread", None)
    # Настройки, которые программа пишет прямо в os.environ: после теста — как было.
    # Пустое значение = «по умолчанию» (всё качать, превью включены, демо выключено)
    for key in ("BACKUP_SOURCES", "BACKUP_CATEGORIES", "BACKUP_DISK_FOLDERS", "GENTLE_MODE",
                "YADISK_PREVIEWS", "YADISK_DEMO", "YADISK_RETRY_EVERY_SEC", "YA_DISK_TOKEN",
                "YADISK_WEBUI"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "downloads"))
