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
    monkeypatch.setattr(webapp.paths, "UPLOAD_DB_FILE", tmp_path / "upload_unlim_state.db")
    monkeypatch.setattr(webapp.paths, "UPLOAD_CONFIG_FILE", tmp_path / "upload_unlim_config.json")
    monkeypatch.setattr(webapp.paths, "PEOPLE_DB_FILE", tmp_path / "people_sorter.db")
    monkeypatch.setattr(webapp.paths, "PEOPLE_CONFIG_FILE", tmp_path / "people_sorter_config.json")
    monkeypatch.setattr(webapp.paths, "PEOPLE_MODELS_DIR", tmp_path / ".models")
    monkeypatch.setattr(webapp.paths, "PEOPLE_CACHE_DIR", tmp_path / ".cache")
    monkeypatch.setattr(webapp.paths, "PEOPLE_CROPS_DIR", tmp_path / ".cache" / "crops")
    monkeypatch.setattr(webapp.paths, "PEOPLE_THUMBS_DIR", tmp_path / ".cache" / "thumbs")
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
    # «Обновить программу»: никакого обновления, перезапуска и запрета задач из прошлых тестов
    import webapp.selfupdate
    import webapp.tasks

    for name in ("_job", "_thread", "_restart_hook", "_port", "exit_code", "_updated", "_fallback"):
        monkeypatch.setattr(webapp.selfupdate, name, None)
    monkeypatch.setattr(webapp.tasks.tm, "_frozen", None)
    monkeypatch.delenv(webapp.selfupdate.RESTART_ENV, raising=False)
    monkeypatch.delenv(webapp.selfupdate.URL_ENV, raising=False)
    # Сохранение кук спрашивает Яндекс, пускает ли он с ними: в тестах — без сети, «пускает»
    import webapp.access
    monkeypatch.setattr(webapp.access, "get_sk_uid", lambda s: ("sk", "1"))
    # Настройки, которые программа пишет прямо в os.environ: после теста — как было.
    # Пустое значение = «по умолчанию» (всё качать, превью включены, демо выключено)
    for key in ("BACKUP_SOURCES", "BACKUP_CATEGORIES", "BACKUP_DISK_FOLDERS", "GENTLE_MODE",
                "YADISK_PREVIEWS", "YADISK_DEMO", "YADISK_RETRY_EVERY_SEC", "YA_DISK_TOKEN",
                "YADISK_WEBUI", "BACKUP_RESUME_ON_START", "BACKUP_RESUME_PENDING"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "downloads"))
    import sorter.paths as sorter_paths
    import sorter.webapp.manager as sorter_mgr_mod
    import uploader.webapp.manager as upload_mgr_mod
    import webapp.people as people_mod

    sorter_paths.configure_data_dir(None)
    fresh_upload_mgr = upload_mgr_mod.UploadManager()
    fresh_people_mgr = sorter_mgr_mod.TaskManager()
    monkeypatch.setattr(upload_mgr_mod, "manager", fresh_upload_mgr)
    monkeypatch.setattr(sorter_mgr_mod, "manager", fresh_people_mgr)
    monkeypatch.setattr(people_mod, "manager", fresh_people_mgr)
    yield
    # Жёсткий выход после перезапуска (os._exit) не должен сработать посреди следующих тестов
    fallback = webapp.selfupdate._fallback
    if fallback is not None:
        fallback.cancel()
    fresh_upload_mgr.shutdown(timeout=5.0)
    fresh_people_mgr.shutdown(timeout=5.0)
    sorter_paths.configure_data_dir(None)
    # unlim_download держит соединение с базой на поток: закрываем то, что открыл тест
    ud = sys.modules.get("unlim_download")
    conn = getattr(getattr(ud, "_tls", None), "c", None)
    if conn is not None:
        conn.close()
        ud._tls.c = None
