"""Эндпоинты интерфейса 2.0: настройки, запуск, повтор, списки нескачанного, папки, токен."""

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import webui


@pytest.fixture()
def client():
    return TestClient(webui.app, headers={"Host": "localhost:8000"})


def test_settings_default_to_everything(client):
    st = client.get("/api/settings").json()
    assert st["sources"] == ["disk", "unlim"]
    assert st["categories"] is None
    assert st["gentle"] is False and st["previews"] is True
    assert "photos" in st["category_labels"]


def test_settings_roundtrip_and_validation(client, monkeypatch):
    r = client.post("/api/settings", json={"sources": ["unlim"], "categories": ["photos", "videos"],
                                           "gentle": True, "previews": False}).json()
    st = r["settings"]
    assert st["sources"] == ["unlim"] and st["categories"] == ["photos", "videos"]
    assert st["gentle"] is True and st["previews"] is False
    assert client.post("/api/settings", json={"all_categories": True}).json()["settings"]["categories"] is None
    assert client.post("/api/settings", json={"sources": []}).status_code == 400
    assert client.post("/api/settings", json={"categories": ["nonsense"]}).status_code == 400
    monkeypatch.setattr(webui.pipeline, "is_running", lambda: True)
    assert client.post("/api/settings", json={"sources": ["disk"]}).status_code == 409
    # Переключатели можно менять и во время скачивания
    assert client.post("/api/settings", json={"gentle": False}).status_code == 200


def test_rescue_start_uses_saved_settings(client, monkeypatch):
    calls = {}
    monkeypatch.setattr(webui, "preflight_sources", lambda d, u: {"error": None, "notes": [], "run_disk": d, "run_unlim": u})
    monkeypatch.setattr(webui.pipeline, "start", lambda **kw: (calls.update(kw), (True, "Спасение запущено"))[1])
    client.post("/api/settings", json={"sources": ["disk"], "categories": ["photos"], "gentle": True})
    r = client.post("/api/rescue/start").json()
    assert r["ok"] is True
    assert calls["do_disk"] is True and calls["do_unlim"] is False
    assert calls["disk_categories"] == ["photos"] and calls["workers"] == 2


def _disk_db(path, rows):
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT, status TEXT,"
        " local_path TEXT, error TEXT, downloaded_at TIMESTAMP, storage_host TEXT, preview_path TEXT);"
    )
    con.executemany("INSERT INTO files (remote_path, size, status, error, preview_path) VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def test_problem_files_explain_reasons(client, tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _disk_db(db, [
        ("disk:/a.jpg", 10, "pending", "storage_down | сервер молчит", "/p/a.jpg.jpg"),
        ("disk:/b.pdf", 20, "failed", "PathNotFound: gone", None),
        ("disk:/c.txt", 30, "failed", "local_disk | OSError: no space", None),
        ("disk:/d.txt", 40, "pending", None, None),
        ("disk:/e.txt", 50, "downloaded", None, None),
    ])
    monkeypatch.setattr(webui, "DB_FILE", db)
    r = client.get("/api/disk/failed").json()
    assert r["total"] == 3
    states = {x["name"]: (x["state"], x["preview"]) for x in r["items"]}
    assert states == {"a.jpg": ("deferred", True), "b.pdf": ("failed", False), "c.txt": ("failed", False)}
    assert any("не отвечает" in k for k in r["reasons"])
    assert any("больше нет" in k for k in r["reasons"])
    assert any("ваш диск" in k for k in r["reasons"])
    assert client.get("/api/nope/failed").status_code == 404


def test_retry_failed_requeues_and_starts(client, tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _disk_db(db, [("disk:/b.pdf", 20, "failed", "server_50x | 500", None)])
    monkeypatch.setattr(webui, "DB_FILE", db)
    monkeypatch.setattr(webui, "api_rescue_start", lambda: {"ok": True, "msg": "Спасение запущено"})
    r = client.post("/api/rescue/retry_failed", json={"source": "disk"}).json()
    assert r["ok"] and r["count"] == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT status FROM files").fetchone()[0] == "pending"
    con.close()


def test_open_folder_only_opens_the_download_folder(client, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(webui, "get_config", lambda: {"download_dir": str(tmp_path)})
    monkeypatch.setattr(webui.os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(webui.subprocess, "Popen", lambda args, **kw: opened.append(args[-1]))
    assert client.post("/api/open_folder", json={"which": "download"}).json()["ok"] is True
    assert opened and str(opened[0]) == str(tmp_path)
    assert client.post("/api/open_folder", json={"which": "previews"}).json()["ok"] is False  # папки ещё нет
    assert client.post("/api/open_folder", json={"which": "../../Windows"}).status_code == 400


def test_token_snapshot_does_not_block_after_first_check(monkeypatch):
    calls = []

    def slow(token):
        calls.append(token)
        time.sleep(0.3)
        data = {"ok": True, "login": "x"}
        webui._token_check_cache.update({"token": token, "at": time.time(), "data": data})
        return data

    monkeypatch.setattr(webui, "check_token", slow)
    monkeypatch.setitem(webui._token_check_cache, "token", None)
    monkeypatch.setitem(webui._token_check_cache, "data", None)
    assert webui.token_snapshot("t")["ok"] is True  # первый раз — синхронно
    webui._token_check_cache["at"] = 0  # устарело
    t0 = time.monotonic()
    assert webui.token_snapshot("t")["ok"] is True  # отдаёт прошлое значение сразу
    assert time.monotonic() - t0 < 0.2
    for _ in range(50):
        if len(calls) == 2:
            break
        time.sleep(0.02)
    assert len(calls) == 2  # а свежую проверку запустил в фоне


def test_reason_text_plain_language():
    assert "не отвечает" in webui.reason_text("storage_down | x")
    assert "медлен" in webui.reason_text("download_timeout | x") or "долго" in webui.reason_text("download_timeout | x")
    assert webui.reason_text(None) == ""
