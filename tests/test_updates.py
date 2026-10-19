"""«Вышла версия X.Y.Z»: номера версий, GitHub раз в 6 часов в фоне, тишина при сбоях, /api/status.update."""

import json
import threading
import time

import pytest
import requests
import urllib3
from fastapi.testclient import TestClient

import utils
import webapp.access
import webapp.demo
import webapp.status
import webapp.updates as updates
import webui

PAGE = "https://github.com/PXSYS13/yadisk_backup/releases/tag/v2.8.0"


def _wait_check():
    """Дождаться фоновой проверки: она не должна пережить подмены теста."""
    if updates._thread is not None:
        updates._thread.join(timeout=10)
        assert not updates._thread.is_alive()


@pytest.fixture
def github(monkeypatch):
    """Подменённый GitHub: answers — очередь ответов (None — не ответил), calls — сколько раз спросили."""
    answers, calls = [], []

    def fake_fetch(url=None):
        calls.append(1)
        return answers.pop(0) if answers else None

    monkeypatch.setattr(updates, "RELEASES_URL", "https://example.test/latest")
    monkeypatch.setattr(updates, "fetch_latest", fake_fetch)
    yield answers, calls
    _wait_check()


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.raw = self

    def read(self, n, decode_content=True):
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_versions_compare_as_numbers_not_strings():
    assert updates.parse_version("v2.7.10") == (2, 7, 10)
    assert updates.parse_version(" 2.8 ") == (2, 8, 0)
    for junk in (None, 280, "", "latest", "v2", "v2.8.0-beta", "2.8.0.1", "v2.x.1", "v" + "9" * 50 + ".0.0"):
        assert updates.parse_version(junk) is None, junk
    assert updates.is_newer("v2.10.0", "2.9.9")  # строкой «2.10.0» меньше «2.9.9»
    assert updates.is_newer("v2.7.10", "2.7.9")
    assert updates.is_newer("v3.0.0", "2.99.99")
    assert not updates.is_newer("v2.7.2", "2.7.2")
    assert not updates.is_newer("v2.6.1", "2.7.2")  # архив main новее последнего релиза — молчим
    assert not updates.is_newer("latest", "2.7.2") and not updates.is_newer("v2.8.0", None)


def test_update_info_only_when_github_is_newer():
    latest = {"version": "2.8.0", "url": PAGE}
    assert updates.update_info(latest, "2.7.2") == {"current": "2.7.2", "latest": "2.8.0", "url": PAGE}
    assert updates.update_info(latest, "2.8.0") is None
    assert updates.update_info(latest, "2.10.0") is None
    assert updates.update_info(None, "2.7.2") is None
    assert updates.update_info({"version": "999.0.0", "url": PAGE})["current"] == utils.APP_VERSION


def test_fetch_latest_reads_tag_and_release_page(monkeypatch):
    seen = {}
    answer = {}

    def fake_get(url, **kw):
        seen.update(kw, url=url)
        return answer["r"]

    monkeypatch.setattr(updates.requests, "get", fake_get)
    answer["r"] = _Resp(200, {"tag_name": "v2.8.0", "html_url": PAGE, "body": "Что нового: " + "я" * 50000})
    assert updates.fetch_latest("https://example.test/latest") == {"version": "2.8.0", "url": PAGE}
    assert seen["url"] == "https://example.test/latest" and seen["timeout"] == updates.FETCH_TIMEOUT
    # Ссылка не на релиз этого проекта — «Что нового» ведёт на страницу релизов проекта
    answer["r"] = _Resp(200, {"tag_name": "2.8", "html_url": "https://example.com/x"})
    assert updates.fetch_latest("https://example.test/latest") == {"version": "2.8.0", "url": updates.RELEASES_PAGE}


def test_fetch_latest_is_silent_on_any_failure(monkeypatch):
    answer = {}

    def fake_get(url, **kw):
        if isinstance(answer["r"], Exception):
            raise answer["r"]
        return answer["r"]

    class Broken(_Resp):
        def read(self, n, decode_content=True):
            raise urllib3.exceptions.ProtocolError("обрыв посреди ответа")

    monkeypatch.setattr(updates.requests, "get", fake_get)
    for bad in (requests.ConnectionError("нет сети"), requests.Timeout("GitHub молчит"),
                _Resp(403, {"message": "API rate limit exceeded"}), _Resp(404, b"Not Found"),
                _Resp(200, b"<html>"), _Resp(200, b"\xff\xfe"), _Resp(200, b" " * (updates.MAX_BYTES + 10)),
                _Resp(200, [1, 2]), _Resp(200, {"tag_name": "latest"}), _Resp(200, {"name": "без номера"}),
                _Resp(200, {"tag_name": "v2.9.0", "draft": True}),
                _Resp(200, {"tag_name": "v2.9.0", "prerelease": True}), Broken(200, b"")):
        answer["r"] = bad
        assert updates.fetch_latest("https://example.test/latest") is None, bad

    # Проверка выключена (так в тестах) — без единого запроса
    def no_requests(url, **kw):
        raise AssertionError("запрос к GitHub при выключенной проверке")

    monkeypatch.setattr(updates.requests, "get", no_requests)
    assert updates.fetch_latest("") is None


def test_asks_github_in_background_at_most_every_6_hours(github, monkeypatch):
    answers, calls = github
    monkeypatch.setattr(utils, "APP_VERSION", "2.7.2")
    answers.extend([{"version": "2.8.0", "url": PAGE}, None, {"version": "2.9.0", "url": PAGE}])
    t0, every = 1000.0, updates.CHECK_EVERY_SEC
    # Первый опрос статуса GitHub не ждёт: ответа ещё нет, проверка ушла в фон
    assert updates.snapshot(now=t0) is None
    _wait_check()
    assert len(calls) == 1
    info = {"current": "2.7.2", "latest": "2.8.0", "url": PAGE}
    assert updates.snapshot(now=t0 + 1) == info
    # Следующие 6 часов — из памяти
    assert updates.snapshot(now=t0 + every - 1) == info
    _wait_check()
    assert len(calls) == 1
    # Через 6 часов GitHub не ответил — плашка остаётся по прошлому ответу
    assert updates.snapshot(now=t0 + every) == info
    _wait_check()
    assert len(calls) == 2 and updates.snapshot(now=t0 + every + 1) == info
    # Ещё через 6 часов — вышла следующая версия
    updates.snapshot(now=t0 + 2 * every)
    _wait_check()
    assert len(calls) == 3 and updates.snapshot(now=t0 + 2 * every + 1)["latest"] == "2.9.0"


def test_failures_from_the_start_keep_quiet(github):
    answers, calls = github
    assert updates.snapshot(now=0) is None
    _wait_check()
    assert updates.snapshot(now=1) is None and len(calls) == 1
    # Сбой самой проверки не оставляет её «занятой» навсегда
    assert updates._state["busy"] is False


def test_slow_github_never_blocks_status(monkeypatch):
    release = threading.Event()
    calls = []

    def slow_fetch(url=None):
        calls.append(1)
        release.wait(10)
        return {"version": "999.0.0", "url": PAGE}

    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    monkeypatch.setattr(updates, "RELEASES_URL", "https://example.test/latest")
    monkeypatch.setattr(updates, "fetch_latest", slow_fetch)
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    try:
        started = time.monotonic()
        assert client.get("/api/status").json()["update"] is None
        # Пока GitHub думает, вторая проверка не запускается
        assert client.get("/api/status").json()["update"] is None
        assert time.monotonic() - started < 5
    finally:
        release.set()
        _wait_check()
    assert len(calls) == 1
    assert client.get("/api/status").json()["update"] == {"current": utils.APP_VERSION, "latest": "999.0.0",
                                                          "url": PAGE}


def test_status_update_key_keeps_the_other_keys(monkeypatch):
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    st = webapp.status.api_status()
    assert st["update"] is None  # в тестах проверка выключена
    for key in ("app_version", "donation_url", "telegram_url", "telegram_handle", "youtube_url", "config",
                "token", "cookies", "onboarding", "disk", "unlim", "tasks", "pipeline", "rescue", "settings",
                "links", "demo", "diagnostics", "yandex_health", "free_space", "news", "exit_codes"):
        assert key in st, key
    monkeypatch.setitem(updates._state, "latest", {"version": "999.0.0", "url": PAGE})
    assert webapp.status.api_status()["update"] == {"current": utils.APP_VERSION, "latest": "999.0.0", "url": PAGE}
    monkeypatch.setitem(updates._state, "latest", {"version": utils.APP_VERSION, "url": PAGE})
    assert webapp.status.api_status()["update"] is None


def test_demo_state_update(monkeypatch):
    monkeypatch.setenv("YADISK_DEMO", "1")
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    assert "update" in webapp.demo.DEMO_STATES
    st = client.get("/api/status?demo=update").json()
    upd = st["update"]
    assert st["demo"] == "update" and upd["current"] == utils.APP_VERSION
    assert updates.is_newer(upd["latest"], upd["current"])
    assert upd["url"].startswith("https://github.com/PXSYS13/yadisk_backup/releases")
    # Главная прячет плашку, пока нужно действие, — в демо его нет
    assert st["rescue"]["action"] is None and st["rescue"]["state"] == "idle"
    # Остальные демо-состояния — без плашки, даже если GitHub знает о новой версии
    monkeypatch.setitem(updates._state, "latest", {"version": "999.0.0", "url": PAGE})
    assert client.get("/api/status?demo=ready").json()["update"] is None
    # Без YADISK_DEMO демо выключено — настоящий ответ
    monkeypatch.delenv("YADISK_DEMO")
    assert client.get("/api/status?demo=update").json()["update"]["latest"] == "999.0.0"
