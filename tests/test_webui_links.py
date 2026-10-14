"""Раздел «По ссылке» в Web UI: добавление, удаление, статус, папки и изоляция от спасения."""

import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import public_download as pd
import webui
from utils import LINKS_DIRNAME

LINK = "https://disk.yandex.ru/d/AbCd"


@pytest.fixture()
def client():
    return TestClient(webui.app, headers={"Host": "localhost:8000"})


@pytest.fixture()
def started(monkeypatch):
    """Запуски задач вместо настоящих процессов."""
    calls = []
    monkeypatch.setattr(webui.tm, "start", lambda slot, script, args: (calls.append((slot, script, list(args))), (True, "ok"))[1])
    monkeypatch.setattr(webui.tm, "any_alive", lambda prefix: False)
    return calls


class FakeApi:
    """PublicApi без сети: meta отвечает заранее заданным ответом или ошибкой."""

    answer: dict | Exception = {"name": "Свадьба", "type": "dir"}

    def __init__(self, *a, **kw):
        pass

    def meta(self, *a, **kw):
        if isinstance(FakeApi.answer, Exception):
            raise FakeApi.answer
        return FakeApi.answer


@pytest.fixture()
def api(monkeypatch):
    FakeApi.answer = {"name": "Свадьба", "type": "dir"}
    monkeypatch.setattr(webui, "PublicApi", FakeApi)
    return FakeApi


def _db():
    return pd.connect_db(webui.LINKS_DB_FILE)


def test_adding_a_link_checks_it_and_starts_downloading(client, started, api):
    r = client.post("/api/links/add", json={"url": "смотри фото"})
    assert r.status_code == 400 and "Яндекс Диска" in r.json()["detail"]
    r = client.post("/api/links/add", json={"url": f"  {LINK}?utm=1  "}).json()
    assert r["ok"] and r["new"] and "начинаю" in r["msg"]
    assert started == [("links", "public_download.py", ["--wait", "--workers", "3"])]
    again = client.post("/api/links/add", json={"url": LINK}).json()
    assert again["ok"] and not again["new"] and "уже в списке" in again["msg"]
    conn = _db()
    row = conn.execute("SELECT name, local_dir, public_url FROM links").fetchone()
    conn.close()
    assert (row["name"], row["local_dir"], row["public_url"]) == ("Свадьба", f"{LINKS_DIRNAME}/Свадьба", LINK)


def test_gentle_mode_uses_fewer_threads_for_links(client, started, api):
    client.post("/api/settings", json={"gentle": True})
    client.post("/api/links/add", json={"url": LINK})
    assert started[0][2] == ["--wait", "--workers", "2"]


@pytest.mark.parametrize("kind,text", [("not_found", "больше не работает"), ("password", "паролем")])
def test_dead_and_password_links_are_refused_with_plain_text(client, started, api, kind, text):
    api.answer = pd.LinkError(kind, pd._error_for(SimpleNamespace(
        status_code=404 if kind == "not_found" else 403,
        json=lambda: {"error": "DiskSymlinkPasswordRequiredError" if kind == "password" else "DiskNotFoundError"})).text)
    r = client.post("/api/links/add", json={"url": LINK})
    assert r.status_code == 400 and text in r.json()["detail"]
    assert not started


def test_link_is_added_even_when_yandex_is_silent(client, started, api):
    api.answer = pd.LinkError("timeout", "Яндекс отвечал слишком долго")
    r = client.post("/api/links/add", json={"url": LINK}).json()
    assert r["ok"] and "попробует сама" in r["msg"]
    conn = _db()
    assert conn.execute("SELECT name FROM links").fetchone()[0] == "AbCd"  # имя уточнится при сборе списка
    conn.close()


def test_adding_while_downloading_pokes_the_running_task(client, monkeypatch, api):
    calls = []
    monkeypatch.setattr(webui.tm, "start", lambda *a: calls.append(a) or (True, "ok"))
    monkeypatch.setattr(webui.tm, "any_alive", lambda prefix: prefix == webui.SLOT_LINKS)
    assert client.post("/api/links/add", json={"url": LINK}).json()["ok"]
    assert not calls
    conn = _db()
    assert pd.meta_get(conn, "changed") is not None
    conn.close()


def test_removing_a_link_keeps_files_and_forgets_its_queue(client, started, api):
    client.post("/api/links/add", json={"url": LINK})
    conn = _db()
    conn.execute("INSERT INTO link_files (link_id, path, status) VALUES (1, '/a.jpg', 'pending')")
    conn.close()
    r = client.post("/api/links/remove", json={"id": 1}).json()
    assert r["ok"] and "остались" in r["msg"]
    conn = _db()
    assert conn.execute("SELECT state FROM links").fetchone()[0] == "removed"
    assert conn.execute("SELECT COUNT(*) FROM link_files").fetchone()[0] == 0
    conn.close()
    assert client.post("/api/links/remove", json={"id": 1}).json()["ok"] is False
    assert client.post("/api/links/remove", json={}).status_code == 400


def test_retry_now_returns_failed_files_and_lifts_the_pause(client, started, api):
    client.post("/api/links/add", json={"url": LINK})
    conn = _db()
    conn.execute("UPDATE links SET limit_until=?, error_kind='forbidden', error='x' WHERE id=1", (time.time() + 3600,))
    conn.execute("INSERT INTO link_files (link_id, path, status, error) VALUES (1, '/a.jpg', 'failed', 'bad_file | x')")
    conn.close()
    started.clear()
    assert client.post("/api/links/retry_now", json={"id": 1}).json()["ok"]
    conn = _db()
    assert conn.execute("SELECT status FROM link_files").fetchone()[0] == "pending"
    assert tuple(conn.execute("SELECT limit_until, error_kind FROM links").fetchone()) == (None, None)
    assert pd.meta_get(conn, "wake") is not None
    conn.close()
    assert started and started[0][0] == "links"


def test_relist_and_resume_and_stop(client, started, api, monkeypatch):
    client.post("/api/links/add", json={"url": LINK})
    conn = _db()
    conn.execute("UPDATE links SET listed_at=1 WHERE id=1")
    conn.close()
    assert client.post("/api/links/relist", json={"id": 1}).json()["ok"]
    conn = _db()
    assert conn.execute("SELECT listed_at FROM links").fetchone()[0] is None
    conn.close()
    assert client.post("/api/links/resume").json()["ok"]
    assert "и так остановлено" in client.post("/api/links/stop").json()["msg"]
    stopped = []
    monkeypatch.setattr(webui.tm, "any_alive", lambda prefix: True)
    monkeypatch.setattr(webui.tm, "stop", lambda slot: stopped.append(slot) or (True, "ok"))
    assert client.post("/api/links/stop").json()["ok"] and stopped == ["links"]


def _row(**kw):
    base = {"id": 1, "name": "Свадьба", "kind": "dir", "error_kind": None, "error": None, "limit_until": None,
            "listed_at": 100.0, "added_at": 50.0, "total": 10, "done": 0, "deferred": 0, "limited": 0, "pending": 0,
            "failed": 0, "bytes_total": 1000, "bytes_done": 0}
    base.update(kw)
    return base


@pytest.mark.parametrize("row,task,meta,state,words", [
    (_row(error_kind="not_found"), None, {}, "error", "больше не работает"),
    (_row(listed_at=None), {"running": True}, {}, "listing", "список"),
    (_row(listed_at=None), None, {}, "paused", "не получен"),
    (_row(pending=5, limited=5, limit_until=10_000.0), {"running": True}, {"state": "waiting"}, "limited", "ограничил"),
    (_row(pending=5, done=5), {"running": True}, {}, "downloading", "Скачиваю"),
    (_row(pending=3, deferred=3, done=7), {"running": True}, {"state": "waiting", "next_retry_at": "4000"},
     "waiting", "Ждут Яндекса: 3 файла, повтор в"),
    (_row(pending=3, done=7), None, {}, "paused", "На паузе"),
    (_row(done=8, failed=2), None, {}, "done_partial", "не скачались: 2"),
    (_row(done=10), None, {}, "done", "все файлы скачаны"),
])
def test_link_states_are_told_in_plain_words(row, task, meta, state, words):
    st = webui.build_links_status([row], meta, task, 1000.0, "/dl/_ссылки")
    item = st["items"][0]
    assert item["state"] == state and words in item["status_text"]
    assert st["folder"] == "/dl/_ссылки"


def test_whole_links_block_reports_running_waiting_and_a_full_disk():
    rows = [_row(pending=3, deferred=3)]
    waiting = webui.build_links_status(rows, {"state": "waiting", "next_retry_at": "5000"},
                                       {"running": True, "last_line_at": 990.0}, 1000.0, "f")
    assert (waiting["state"], waiting["next_retry_at"], waiting["last_activity_at"]) == ("waiting", 5000.0, 990.0)
    full = webui.build_links_status(rows, {}, {"running": False, "exit_code": 4}, 1000.0, "f")
    assert full["state"] == "paused" and full["problem"]["kind"] == "local_disk"
    assert webui.build_links_status([], {}, None, 1000.0, "f")["state"] == "idle"


def test_status_carries_the_links_block(client, started, api):
    client.post("/api/links/add", json={"url": LINK})
    links = client.get("/api/status").json()["links"]
    assert links["items"][0]["name"] == "Свадьба" and links["items"][0]["state"] == "paused"
    assert links["folder"].endswith(LINKS_DIRNAME)


def test_demo_status_shows_every_link_state(client, monkeypatch):
    monkeypatch.setenv("YADISK_DEMO", "1")
    links = client.get("/api/status", params={"demo": "downloading"}).json()["links"]
    assert {i["state"] for i in links["items"]} == {"downloading", "limited", "done", "error"}


def test_link_log_lines_do_not_become_disk_banners(monkeypatch):
    fake = SimpleNamespace(alive=lambda: True, finished_at=None,
                           log=deque(["storage_down | Отложено 5 файлов"] * 5 + ["refused | x"] * 5))
    monkeypatch.setitem(webui.tm._tasks, webui.SLOT_LINKS, fake)
    assert webui.tm.recent_log_lines(40, exclude=(webui.SLOT_LINKS,)) == []
    assert webui.diagnose_tasks().get("kind") not in ("storage_down", "refused", "bulk_disabled")


def test_open_folder_of_a_link_comes_from_the_database_only(client, started, api, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(webui.os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(webui.subprocess, "Popen", lambda args: opened.append(args[-1]))
    client.post("/api/links/add", json={"url": LINK})
    base = Path(webui.get_config()["download_dir"])
    (base / LINKS_DIRNAME / "Свадьба").mkdir(parents=True)
    assert client.post("/api/open_folder", json={"which": "links", "link_id": 1}).json()["ok"]
    assert opened[-1].endswith(str(Path(LINKS_DIRNAME) / "Свадьба"))
    conn = _db()
    conn.execute("UPDATE links SET local_dir='../../Windows' WHERE id=1")
    conn.close()
    assert client.post("/api/open_folder", json={"which": "links", "link_id": 1}).json()["ok"]
    assert opened[-1].endswith(LINKS_DIRNAME)  # чужой путь из базы не открываем


def test_folder_change_and_sorting_wait_for_link_downloads(client, monkeypatch, tmp_path):
    monkeypatch.setattr(webui.tm, "any_alive", lambda prefix: prefix == webui.SLOT_LINKS)
    r = client.post("/api/config/download_dir", json={"download_dir": str(tmp_path / "new")})
    assert r.status_code == 409 and "ссылкам" in r.json()["detail"]
    (tmp_path / "sort").mkdir()
    r = client.post("/api/organize", json={"input": str(tmp_path / "sort"), "action": "move"}).json()
    assert r["ok"] is False


def test_stopping_the_rescue_leaves_link_downloads_alone(monkeypatch):
    prefixes = []
    monkeypatch.setattr(webui.tm, "stop_matching", lambda prefix: prefixes.append(prefix) or [])
    r = webui.Rescue()
    r._thread = SimpleNamespace(is_alive=lambda: True, join=lambda timeout=None: None)
    r.stop()
    assert prefixes and not any(webui.SLOT_LINKS.startswith(p) for p in prefixes)


def test_old_wizard_is_not_blocked_by_link_downloads(client, monkeypatch):
    monkeypatch.setattr(webui.tm, "any_alive", lambda prefix: prefix == webui.SLOT_LINKS)
    monkeypatch.setattr(webui, "preflight_sources", lambda d, u: {"error": None, "notes": [], "run_disk": d, "run_unlim": u})
    monkeypatch.setattr(webui.pipeline, "start", lambda **kw: (True, "ok"))
    assert client.post("/api/plan/start", json={}).json()["ok"] is True


def test_links_tab_screen_and_task_kind_exist():
    html = Path(webui.TEMPLATES_DIR, "app.html").read_text(encoding="utf-8")
    assert 'data-route="links"' in html and 'id="screen-links"' in html and "screens/links.js" in html
    assert webui.task_kind(["public_download.py", "--wait"]) == "links"
