"""Проверки интерфейса 2.0 без браузера: синтаксис, id, действия, статика, тексты."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import webui

ASSETS = Path(webui.TEMPLATES_DIR)
APP_HTML = ASSETS / "app.html"
JS_FILES = sorted((ASSETS / "static" / "js").rglob("*.js"))


def _all_js() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in JS_FILES)


def test_there_is_a_new_ui():
    assert APP_HTML.exists()
    assert len(JS_FILES) >= 9


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_js_syntax(path):
    res = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_every_byid_exists_in_markup_or_templates():
    html = APP_HTML.read_text(encoding="utf-8")
    js = _all_js()
    ids = set(re.findall(r"byId\('([^']+)'\)", js))
    defined = set(re.findall(r'id="([^"$]+)"', html)) | set(re.findall(r'id="([^"$]+)"', js))
    missing = sorted(i for i in ids if i not in defined)
    assert not missing, f"нет элементов с id: {missing}"


def test_every_data_action_has_a_handler():
    html = APP_HTML.read_text(encoding="utf-8")
    js = _all_js()
    used = set(re.findall(r'data-action="([a-z0-9-]+)"', html + js))
    registered = set(re.findall(r"App\.action\('([a-z0-9-]+)'", js))
    assert used, "в интерфейсе нет действий"
    assert not sorted(used - registered), f"нет обработчиков: {sorted(used - registered)}"


def test_static_references_exist_and_have_proper_types():
    html = APP_HTML.read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="(/static/[^"?]+)', html)
    assert refs
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    for ref in refs:
        assert (ASSETS / ref.removeprefix("/")).exists(), ref
        resp = client.get(ref)
        assert resp.status_code == 200, ref
        ctype = resp.headers["content-type"]
        assert ("javascript" in ctype) if ref.endswith(".js") else ("css" in ctype), (ref, ctype)


def test_no_markdown_file_names_in_ui_text():
    # .md — домен верхнего уровня: Telegram и браузеры делают из имени файла мёртвую ссылку
    text = APP_HTML.read_text(encoding="utf-8") + _all_js()
    assert not re.findall(r"\b[\w-]+\.md\b", text)


def test_index_switches_between_new_and_old_ui(monkeypatch):
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    new = client.get("/?ui=new").text
    assert "/static/js/core.js?v=" in new and "__APP_VERSION__" not in new
    assert webui.APP_VERSION in new
    old = client.get("/?ui=old").text
    assert "/static/js/core.js" not in old
    monkeypatch.setattr(webui, "UI_DEFAULT", "new")
    assert "/static/js/core.js" in client.get("/").text


def test_demo_states_are_complete(monkeypatch):
    monkeypatch.setenv("YADISK_DEMO", "1")
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    for state in webui.DEMO_STATES:
        st = client.get(f"/api/status?demo={state}").json()
        assert st["demo"] == state
        r = st["rescue"]
        assert r["headline"], state
        assert set(r["sources"]) == {"disk", "unlim"}
    # Без YADISK_DEMO демо выключено — настоящий статус
    monkeypatch.delenv("YADISK_DEMO")
    assert client.get("/api/status?demo=waiting").json()["demo"] is None
