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
