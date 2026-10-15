"""Проверки интерфейса 2.0 без браузера: синтаксис, id, действия, статика, тексты."""

import json
import os
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
    res = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
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


def test_sorting_never_promises_that_nothing_is_deleted():
    # «Разложить файлы» удаляет опустевшие папки и разбирает папки Диска — обещать обратное нельзя
    text = APP_HTML.read_text(encoding="utf-8") + _all_js() + (ASSETS / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"ничего не удаля(ется|ет)", text, re.IGNORECASE)


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


# Главная в node: заглушки DOM, на входе — /api/status, на выходе — разметка блоков
_HOME_HARNESS = r"""
const path = require('path');
global.window = globalThis;
global.addEventListener = () => {};
global.location = { search: '', hash: '', pathname: '/' };
const nodes = {};
const el = () => ({ innerHTML: '', textContent: '', className: '', dataset: {}, style: {},
  addEventListener() {}, setAttribute() {}, classList: { add() {}, remove() {}, toggle() {} } });
global.document = Object.assign(el(), { documentElement: el(), body: el(), createElement: el,
  querySelectorAll: () => [], querySelector: () => null, getElementById: (id) => (nodes[id] = nodes[id] || el()) });
global.localStorage = { getItem: () => null, setItem() {} };
global.matchMedia = () => ({ matches: false, addEventListener() {} });
for (const f of ['core.js', 'components.js', 'screens/home.js']) require(path.join(process.env.JS_DIR, f));
window.App.screens.home.update(JSON.parse(require('fs').readFileSync(0, 'utf8')));
process.stdout.write(JSON.stringify({ stats: nodes['home-stats'].innerHTML, where: nodes['home-where'].innerHTML }));
"""


def _render_home(status: dict) -> dict:
    res = subprocess.run(["node", "-e", _HOME_HARNESS], input=json.dumps(status), capture_output=True,
                         text=True, encoding="utf-8", env={**os.environ, "JS_DIR": str(ASSETS / "static" / "js")})
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_folder_size_says_more_when_not_fully_counted(monkeypatch):
    # Большую папку программа не успевает посчитать — «больше X», а не заниженная цифра без пометки
    monkeypatch.setenv("YADISK_DEMO", "1")
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    paused = client.get("/api/status?demo=stopped").json()
    assert paused["rescue"]["sources"]["disk"]["folder"]["size_partial"] is True
    html = _render_home(paused)
    assert "<small>больше</small> 31,0&nbsp;ГБ" in html["stats"]  # плитка «В папке»
    assert "Уже в папке: больше 31,0 ГБ" in html["where"]    # «Где мои файлы»

    done = client.get("/api/status?demo=done").json()
    assert done["rescue"]["sources"]["disk"]["folder"]["size_partial"] is False
    html = _render_home(done)
    assert "31,0 ГБ" in html["stats"] and "больше" not in html["stats"]
    assert "Уже в папке: 31,0 ГБ" in html["where"]


# «Нас поддерживают»: строка внизу главной и суммы — в node, без браузера
_THANKS_HARNESS = r"""
const path = require('path');
global.window = globalThis;
global.addEventListener = () => {};
global.location = { search: '', hash: '', pathname: '/' };
const el = () => ({ innerHTML: '', textContent: '', className: '', dataset: {}, style: {},
  addEventListener() {}, setAttribute() {}, classList: { add() {}, remove() {}, toggle() {} } });
global.document = Object.assign(el(), { documentElement: el(), body: el(), createElement: el,
  querySelectorAll: () => [], querySelector: () => null, getElementById: () => el() });
global.localStorage = { getItem: () => null, setItem() {} };
global.matchMedia = () => ({ matches: false, addEventListener() {} });
for (const f of ['core.js', 'components.js']) require(path.join(process.env.JS_DIR, f));
const App = window.App;
const out = { money: [App.money(500, 'RUB'), App.money(12.5, 'USD'), App.money(null, 'RUB')],
  initials: [App.initials('Анна П.'), App.initials('сергей')] };
App.supporters = JSON.parse(require('fs').readFileSync(0, 'utf8'));
out.strip = App.thanksStrip();
App.supporters = { supporters: [], people: 0 };
out.empty = App.thanksStrip();
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_thanks_strip_names_and_money():
    sp = {"people": 5, "supporters": [
        {"name": "Анна П.", "amount": 500, "currency": "RUB"}, {"name": "Аноним", "amount": 100, "currency": "RUB"},
        {"name": "<b>Сергей</b>", "amount": 300, "currency": "RUB"}, {"name": "Анна П.", "amount": 50, "currency": "RUB"},
        {"name": "Мария К.", "amount": 1000, "currency": "RUB"}]}
    res = subprocess.run(["node", "-e", _THANKS_HARNESS], input=json.dumps(sp), capture_output=True, text=True,
                         encoding="utf-8", env={**os.environ, "JS_DIR": str(ASSETS / "static" / "js")})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    strip = out["strip"]
    # Аноним и повторный донат не занимают место среди трёх имён; имена экранируются
    assert "Анна П., &lt;b&gt;Сергей&lt;/b&gt;, Мария К. и ещё 2" in strip
    assert 'href="#/support"' in strip and "<b>Сергей" not in strip
    assert out["empty"] == ""
    assert out["money"][0].endswith("₽") and "500" in out["money"][0]
    assert "12,50" in out["money"][1] and out["money"][1].endswith("$")
    assert out["money"][2] == ""
    assert out["initials"] == ["АП", "С"]
