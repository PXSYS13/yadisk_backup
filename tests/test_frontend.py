"""Проверки интерфейса 2.0 без браузера: синтаксис, id, действия, статика, тексты."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import utils
import webapp.access
import webapp.demo
import webapp.folders
import webapp.paths
import webui

ASSETS = Path(webapp.paths.TEMPLATES_DIR)
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
    text = APP_HTML.read_text(encoding="utf-8") + _all_js()
    assert not re.search(r"ничего не удаля(ется|ет)", text, re.IGNORECASE)


def test_index_serves_the_ui_with_versioned_static():
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    page = client.get("/").text
    assert "/static/js/core.js?v=" in page and "__APP_VERSION__" not in page
    assert utils.APP_VERSION in page
    # Закладки на прежний интерфейс (удалён в 2.7.1) открывают этот же
    assert client.get("/", params={"ui": "old"}).text == page


def test_demo_states_are_complete(monkeypatch):
    monkeypatch.setenv("YADISK_DEMO", "1")
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    for state in webapp.demo.DEMO_STATES:
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
process.stdout.write(JSON.stringify({ stats: nodes['home-stats'].innerHTML, where: nodes['home-where'].innerHTML,
  space: nodes['home-space'].innerHTML, sources: nodes['home-sources'].innerHTML }));
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
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
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


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_home_warns_about_space_and_counts_complete_folders(monkeypatch):
    monkeypatch.setenv("YADISK_DEMO", "1")
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    low = _render_home(client.get("/api/status?demo=low_space").json())
    assert "Места на диске может не хватить" in low["space"] and "свободно 40,0 ГБ" in low["space"]
    assert "Выбрать другую папку" not in low["space"]  # идёт скачивание: папку меняют после остановки
    assert "Скачаны целиком: <b>2</b> из 6 папок" in low["sources"] and 'href="#/details/disk"' in low["sources"]
    assert _render_home(client.get("/api/status?demo=downloading").json())["space"] == ""
    paused = client.get("/api/status?demo=stopped").json()
    paused["rescue"]["space"]["short"] = True
    assert "Выбрать другую папку" in _render_home(paused)["space"]
    full = client.get("/api/status?demo=disk_full").json()
    full["rescue"]["space"]["short"] = True
    assert _render_home(full)["space"] == ""  # про место уже говорит баннер «нужно действие»
    assert "Все папки скачаны целиком" in _render_home(client.get("/api/status?demo=done").json())["sources"]


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
App.supporters = null;
out.loading = App.thanksStrip();
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
    # Пока никто не поддержал — тихое приглашение, пока список не загрузился — ничего
    assert "Имена поддержавших появляются здесь" in out["empty"] and 'href="#/support"' in out["empty"]
    assert out["loading"] == ""
    assert out["money"][0].endswith("₽") and "500" in out["money"][0]
    assert "12,50" in out["money"][1] and out["money"][1].endswith("$")
    assert out["money"][2] == ""
    assert out["initials"] == ["АП", "С"]


# «Помогло? Поддержите»: когда карточка появляется и когда молчит — в node, без браузера
_DONATE_HARNESS = _THANKS_HARNESS.split("App.supporters = JSON.parse")[0] + r"""
const r = (state, files, extra) => Object.assign({ state, summary: { downloaded_files: files, downloaded_bytes: files * 1e6 } }, extra || {});
const fresh = { donated: false, closed: [] };
const m = (x, seen) => { const v = App.donateMoment(x, seen || fresh); return v && `${v.kind}:${v.key}`; };
const res = {
  done_small: m(r('done', 12)), done_big: m(r('done', 12345)),
  run_small: m(r('running', 999)), run_1000: m(r('running', 1500)), wait_10000: m(r('waiting', 10001)),
  closed: m(r('running', 1500), { donated: false, closed: ['m-1000'] }),
  closed_next: m(r('running', 10500), { donated: false, closed: ['m-1000'] }),
  done_after_close: m(r('done', 1500), { donated: false, closed: ['m-1000'] }),
  donated: m(r('done', 50000), { donated: true, closed: [] }),
  action: m(r('needs_action', 5000, { action: { id: 'token' } })), idle: m(r('idle', 5000)),
  nothing: m(r('done', 0)), stopped: m(r('stopped', 5000)),
  card: App.donateCard(App.donateMoment(r('done', 12345), fresh), 'https://dalink.to/x'),
  card_ms: App.donateCard(App.donateMoment(r('running', 10001), fresh), ''),
};
process.stdout.write(JSON.stringify(res));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_donate_card_asks_once_per_milestone():
    res = subprocess.run(["node", "-e", _DONATE_HARNESS], capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "JS_DIR": str(ASSETS / "static" / "js")})
    assert res.returncode == 0, res.stderr
    # Числа ru-RU разделяются неразрывным пробелом
    out = json.loads(res.stdout.replace("\u00a0", " ").replace(" ", " "))
    # «Готово» — всегда повод спросить, во время скачивания — только на круглых числах
    assert out["done_small"] == "done:m-0" and out["done_big"] == "done:m-10000"
    assert out["run_small"] is None and out["run_1000"] == "milestone:m-1000"
    assert out["wait_10000"] == "milestone:m-10000"
    # «Не сейчас» закрывает порог: до следующего порога — тишина, «Готово» на нём же — тоже
    assert out["closed"] is None and out["closed_next"] == "milestone:m-10000"
    assert out["done_after_close"] is None
    # «Уже поддерживаю», нужное действие, простой, пауза и ноль файлов — не спрашиваем
    for key in ("donated", "action", "idle", "nothing", "stopped"):
        assert out[key] is None, key
    assert "Программа помогла?" in out["card"] and 'href="https://dalink.to/x"' in out["card"]
    assert 'data-action="donate-open"' in out["card"] and 'data-key="m-10000"' in out["card"]
    assert "Уже 10 000 файлов на вашем компьютере" in out["card_ms"]
    assert "https://www.donationalerts.com/r/davronsvv" in out["card_ms"]


# «Папки» в «Подробнее» у Диска: список, сводка и переход внутрь папки — в node, fetch подменён
_FOLDERS_HARNESS = r"""
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
const pages = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const asked = [];
global.fetch = async (url) => {
  const want = new URL(url, 'http://localhost').searchParams.get('path');
  if (want) asked.push(want);
  return { ok: true, status: 200, json: async () => pages[want] || { items: [], reasons: {}, total: 0 } };
};
for (const f of ['core.js', 'components.js', 'screens/details.js']) require(path.join(process.env.JS_DIR, f));
const App = window.App;
const tick = () => new Promise((r) => setTimeout(r, 20));
(async () => {
  App.screens.details.mount(el());
  App.screens.details.show({ source: 'disk' });
  await tick();
  const root = nodes['det-folders'].innerHTML;
  App.actions['fp-open']({ dataset: { path: 'disk:/Фото' } });
  await tick();
  process.stdout.write(JSON.stringify({ root, inner: nodes['det-folders'].innerHTML, asked }));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_details_shows_which_folders_are_complete():
    pages = {p: webapp.folders.demo_folder_progress(p) for p in ("disk:/", "disk:/Фото")}
    res = subprocess.run(["node", "-e", _FOLDERS_HARNESS], input=json.dumps(pages), capture_output=True, text=True,
                         encoding="utf-8", env={**os.environ, "JS_DIR": str(ASSETS / "static" / "js")})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    root, inner = out["root"], out["inner"]
    assert out["asked"] == ["disk:/", "disk:/Фото"]
    assert "<b>Весь Диск</b>" in root and "Скачаны целиком: <b>3</b> из 6 папок" in root
    assert root.count("скачана целиком") == 3 and "ждут Яндекса: 1 330" in root.replace("\xa0", " ")
    assert 'data-action="fp-open" data-path="disk:/Фото"' in root  # в «Фото» есть подпапки
    assert root.count('data-action="open-disk-folder"') == 2  # «Музыку» уже перенесли
    assert "в папке скачивания её уже нет" in root
    assert 'data-path="disk:/">Весь Диск</button>' in inner and "<b>Фото</b>" in inner
    assert "Файлы прямо в этой папке" in inner and "2022" in inner and "не скачались" not in inner
