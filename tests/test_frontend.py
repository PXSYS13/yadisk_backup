"""Проверки интерфейса 2.0 без браузера: синтаксис, id, действия, статика, тексты."""

import copy
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
import webapp.queue_db
import webapp.rescue_view
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
  space: nodes['home-space'].innerHTML, sources: nodes['home-sources'].innerHTML, actions: nodes['home-actions'].innerHTML }));
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


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_home_offers_setup_even_after_a_previous_run(monkeypatch):
    # Ключ и доступ удалили после прошлого запуска: состояние «нужно действие» или «готово»,
    # а кнопка всё равно ведёт в настройку, а не пропадает или зовёт качать без доступа
    monkeypatch.setenv("YADISK_DEMO", "1")
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    st = client.get("/api/status?demo=setup").json()
    assert st["rescue"]["action"]["id"] == "setup"
    for state in ("idle", "needs_action", "done", "stopped"):
        st["rescue"]["state"] = state
        actions = _render_home(st)["actions"]
        assert 'data-action="go-setup"' in actions and "rescue-start" not in actions, state


# Та же Главная, но на выходе — все блоки: заголовок, плашка состояния, кнопки
_HOME_NODES_HARNESS = _HOME_HARNESS.split("process.stdout.write")[0] + r"""
process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(nodes).map(([k, v]) => [k, v.innerHTML || v.textContent]))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_home_asks_for_a_new_key_when_only_the_disk_was_downloading(monkeypatch):
    # Качали только Диск, Яндекс перестал принимать ключ: на Главной кнопка «Получить новый ключ»,
    # а не рассказ о первой настройке без единой кнопки
    monkeypatch.setenv("YADISK_DEMO", "1")
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    st = client.get("/api/status?demo=token").json()
    no = {"ok": False}
    run = {"running": False, "started_at": 100.0, "finished_at": 200.0, "state": "failed", "next_retry_at": None,
           "branches": {"disk": {"state": "needs_action", "action": "token",
                                 "reason": "Яндекс перестал принимать ключ доступа к Диску"},
                        "unlim": {"state": "off"}}}
    st["rescue"] = webapp.rescue_view.build_rescue_status(run, st["disk"]["db"], webapp.queue_db.empty_db_stats(),
                                                          no, no, st["config"])
    st["token"], st["cookies"] = no, no
    res = subprocess.run(["node", "-e", _HOME_NODES_HARNESS], input=json.dumps(st), capture_output=True, text=True,
                         encoding="utf-8", env={**os.environ, "JS_DIR": str(ASSETS / "static" / "js")})
    assert res.returncode == 0, res.stderr
    home = json.loads(res.stdout)
    assert 'data-action="go-access" data-field="token"' in home["home-actions"]
    assert "Получить новый ключ" in home["home-actions"]
    assert home["home-headline"] == "Яндекс перестал принимать ключ доступа к Диску"
    assert "Нужно ваше действие" in home["home-pill"]
    assert "Начать настройку" not in res.stdout and 'class="how"' not in home["home-stats"]
    # Скачанное Диском видно, карточка Диска ждёт действия, а не «не подключён»
    assert "Скачано" in home["home-stats"] and ">800<" in home["home-stats"]
    disk_card = home["home-sources"].split('class="card source"')[1]
    assert "нужно действие" in disk_card and "Подключить Диск" not in disk_card


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


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_thanks_strip_puts_top_supporters_first():
    sp = {"people": 4, "top": [{"name": "Мария К.", "sums": [{"amount": 1000, "currency": "RUB"}], "donations": 1}],
          "supporters": [{"name": "Анна П.", "amount": 100, "currency": "RUB"}, {"name": "Сергей", "amount": 50, "currency": "RUB"},
                         {"name": "Мария К.", "amount": 1000, "currency": "RUB"}, {"name": "Олег", "amount": 10, "currency": "RUB"}]}
    res = subprocess.run(["node", "-e", _THANKS_HARNESS], input=json.dumps(sp), capture_output=True, text=True,
                         encoding="utf-8", env={**os.environ, "JS_DIR": str(ASSETS / "static" / "js")})
    assert res.returncode == 0, res.stderr
    assert "Мария К., Анна П., Сергей и ещё 1" in json.loads(res.stdout)["strip"]


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


def _demo_statuses(monkeypatch) -> dict:
    monkeypatch.setenv("YADISK_DEMO", "1")
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    return {state: client.get(f"/api/status?demo={state}").json() for state in webapp.demo.DEMO_STATES}


# Уведомления браузера: о чём сообщить, когда состояние сменилось, — в node, без браузера
_NOTIFY_HARNESS = _THANKS_HARNESS.split("App.supporters = JSON.parse")[0] + r"""
const R = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const m = (a, b, fired) => {
  const v = App.notifyMoment(a ? R[a] : null, R[b], fired || []);
  return v && { key: v.key, title: v.title, body: v.body, sticky: !!v.sticky };
};
const waitKey = m('downloading', 'waiting').key;
const res = {
  first: m(null, 'done'), done: m('downloading', 'done'), done_again: m('done', 'done'),
  done_next_poll: m('done', 'done_next_poll'), wait_next_poll: m('waiting', 'waiting_next_poll'),
  partial: m('waiting', 'done_partial'), token: m('downloading', 'token'), cookies: m('downloading', 'cookies'),
  full: m('downloading', 'disk_full'), lost_key: m('downloading', 'lost_key'), still: m('token', 'cookies'),
  wait: m('downloading', 'waiting'), wait_next_round: m('downloading', 'waiting', [waitKey]),
  wait_still: m('waiting', 'waiting'), crash: m('downloading', 'crash'), outage: m('downloading', 'outage'),
  quiet: ['ready', 'setup', 'listing', 'low_space', 'stopped'].map((x) => m('downloading', x)),
  marks: Object.fromEntries(['token', 'done', 'downloading', 'waiting', 'setup'].map((x) => [x, App.titleMark(R[x], false)])),
  offline: App.titleMark(R.downloading, true),
};
process.stdout.write(JSON.stringify(res));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_notification_once_per_transition(monkeypatch):
    r = {state: st["rescue"] for state, st in _demo_statuses(monkeypatch).items()}
    # Перезапуск после сбоя и «Яндекс не отвечает» программа повторит сама — звать человека незачем
    r["crash"] = copy.deepcopy(r["waiting"])
    r["crash"]["sources"]["disk"]["wait_kind"] = "crash"
    r["outage"] = copy.deepcopy(r["waiting"])
    for src in r["outage"]["sources"].values():
        src["wait_kind"] = "outage"
    # Ключ перестал работать, а Безлимит не подключён: вместо действия сервер присылает «настройку»
    r["lost_key"] = copy.deepcopy(r["token"])
    r["lost_key"]["action"] = r["setup"]["action"]
    # Следующий опрос демо: время начала пересчитано, состояние то же
    for state in ("done", "waiting"):
        r[f"{state}_next_poll"] = dict(r[state], started_at=r[state]["started_at"] + 1.5)
    res = subprocess.run(["node", "-e", _NOTIFY_HARNESS], input=json.dumps(r), capture_output=True, text=True,
                         encoding="utf-8", env={**os.environ, "JS_DIR": str(ASSETS / "static" / "js")})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    # Первый опрос после загрузки страницы — тишина: человек и так смотрит на экран
    assert out["first"] is None
    assert out["done"]["title"] == "Готово: все файлы сохранены" and out["done"]["sticky"]
    assert out["done"]["body"] == r["done"]["subline"] and "в папке" in out["done"]["body"]
    assert out["done_again"] is None and out["done_next_poll"] is None and out["wait_next_poll"] is None
    assert out["partial"]["title"] == "Скачано почти всё" and "Не скачались" in out["partial"]["body"]
    # Нужно действие — заголовок и текст те же, что на Главной
    for state in ("token", "cookies", "disk_full"):
        key = "full" if state == "disk_full" else state
        action = r[state]["action"]
        assert out[key] == {"key": "action", "title": action["title"], "body": action["text"], "sticky": True}, state
    assert out["token"]["title"] == "Яндекс перестал принимать ключ доступа к Диску"
    assert out["full"]["title"] == "На диске закончилось место" and "Свободно" in out["full"]["body"]
    assert out["lost_key"]["title"] == "Нужно ваше действие"
    assert out["still"] is None  # одно действие сменилось другим — человек уже у компьютера
    # «Всё доступное скачано» — один раз за запуск, хотя повторы идут каждые 30 минут
    assert out["wait"]["title"] == "Всё доступное скачано, ждём Яндекса" and not out["wait"]["sticky"]
    assert out["wait"]["body"] == r["waiting"]["subline"] and "Ждут Яндекса" in out["wait"]["body"]
    for key in ("wait_next_round", "wait_still", "crash", "outage"):
        assert out[key] is None, key
    assert out["quiet"] == [None] * 5  # готов, настройка, сбор списка, скачивание, пауза
    assert out["marks"] == {"token": "● Нужно действие", "done": "✓ Готово", "downloading": "", "waiting": "",
                            "setup": ""}
    assert out["offline"] == "● Программа не отвечает"


# Опрос статуса → уведомление и метка во вкладке: Notification, разрешение, хранилище и фокус подменены
_WATCH_HARNESS = r"""
const path = require('path');
global.window = globalThis;
global.addEventListener = () => {};
global.location = { search: '', hash: '#/settings', pathname: '/' };
const el = () => ({ innerHTML: '', textContent: '', className: '', dataset: {}, style: {},
  addEventListener() {}, setAttribute() {}, classList: { add() {}, remove() {}, toggle() {} } });
global.document = Object.assign(el(), { documentElement: el(), body: el(), createElement: el,
  querySelectorAll: () => [], querySelector: () => null, getElementById: () => el(),
  title: 'Бэкап Яндекс.Диска', visibilityState: 'hidden', hasFocus: () => false });
const store = {};
global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
global.matchMedia = () => ({ matches: false, addEventListener() {} });
global.isSecureContext = true;
let focused = 0;
global.focus = () => { focused += 1; };
const shown = [];
global.Notification = class {
  constructor(title, opts) { Object.assign(this, { title, opts, closed: false }); shown.push(this); }
  close() { this.closed = true; }
};
Notification.permission = 'default';
let asked = 0;
Notification.requestPermission = () => { asked += 1; Notification.permission = 'granted'; return Promise.resolve('granted'); };
for (const f of ['core.js', 'components.js']) require(path.join(process.env.JS_DIR, f));
const App = window.App;
const S = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const steps = {};
const step = (name, prev, cur, offline) => {
  const before = shown.length;
  App.watchStatus(prev && S[prev], cur && S[cur], !!offline);
  steps[name] = { notified: shown.slice(before).map((n) => n.title), title: document.title };
};
(async () => {
  step('first', null, 'token');
  const before = App.notify.enabled();
  const answer = await App.notify.enable();
  step('token_again', 'token', 'token');
  step('back', 'token', 'downloading');
  step('token', 'downloading', 'token');
  step('offline', null, null, true);
  step('waiting', 'downloading', 'waiting');
  step('waiting_next_round', 'downloading', 'waiting');
  Object.assign(document, { visibilityState: 'visible', hasFocus: () => true });
  step('done_on_screen', 'waiting', 'done');
  Object.assign(document, { visibilityState: 'hidden', hasFocus: () => false });
  step('done_again', 'waiting', 'done');
  step('done_next_poll', 'done', 'done_next_poll');
  const action = shown.find((n) => n.opts.tag === 'yadisk-action');
  action.onclick();
  App.notify.disable();
  step('disabled', 'downloading', 'cookies');
  const saved = JSON.parse(store['yadisk-notify']);
  global.localStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  App.notifyPrefs.save({ on: true, fired: [] });
  process.stdout.write(JSON.stringify({ before, answer, asked, steps, focused, hash: location.hash,
    closed: action.closed, shown: shown.map((n) => [n.title, n.opts.tag, n.opts.requireInteraction]),
    saved, blocked: App.notifyPrefs.get() }));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_status_watch_notifies_once_and_marks_the_tab(monkeypatch):
    st = _demo_statuses(monkeypatch)
    st["done_next_poll"] = copy.deepcopy(st["done"])
    st["done_next_poll"]["rescue"]["started_at"] += 1.5  # демо пересчитывает время начала на каждом опросе
    res = subprocess.run(["node", "-e", _WATCH_HARNESS], input=json.dumps(st), capture_output=True, text=True,
                         encoding="utf-8", env={**os.environ, "JS_DIR": str(ASSETS / "static" / "js")})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    steps = out["steps"]
    base = "Бэкап Яндекс.Диска"
    # Разрешение спрашивают только по нажатию переключателя, сразу приходит пробное уведомление
    assert out["before"] is False and out["answer"] == "granted" and out["asked"] == 1
    # Первый опрос: метка во вкладке есть, уведомления нет
    assert steps["first"] == {"notified": [], "title": f"● Нужно действие — {base}"}
    assert steps["token_again"]["notified"] == [] and steps["back"] == {"notified": [], "title": base}
    assert steps["token"]["notified"] == ["Яндекс перестал принимать ключ доступа к Диску"]
    assert steps["offline"] == {"notified": [], "title": f"● Программа не отвечает — {base}"}
    assert steps["waiting"]["notified"] == ["Всё доступное скачано, ждём Яндекса"]
    assert steps["waiting_next_round"]["notified"] == []
    # Человек смотрит на вкладку — не мешаем, но «Готово» этого запуска уже не повторится
    assert steps["done_on_screen"] == {"notified": [], "title": f"✓ Готово — {base}"}
    assert steps["done_again"]["notified"] == [] and steps["done_next_poll"]["notified"] == []
    assert steps["disabled"] == {"notified": [], "title": f"● Нужно действие — {base}"}
    assert out["shown"] == [
        ["Уведомления включены", "yadisk-test", False],
        ["Яндекс перестал принимать ключ доступа к Диску", "yadisk-action", True],
        ["Всё доступное скачано, ждём Яндекса", "yadisk-wait", False],
    ]
    # Нажатие на уведомление: вкладка на виду, открыта Главная
    assert out["focused"] == 1 and out["closed"] is True and out["hash"] == "#/home"
    assert out["saved"]["on"] is False and [k.split(":")[0] for k in out["saved"]["fired"]] == ["wait", "done"]
    # Хранилище недоступно — выбор живёт до перезагрузки страницы, без ошибок
    assert out["blocked"] == {"on": True, "fired": []}


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


# «Вышла версия X.Y.Z»: плашка на Главной и строка в «Помощи» — в node, хранилище подменено
_UPDATE_HARNESS = r"""
const path = require('path');
global.window = globalThis;
global.addEventListener = () => {};
global.location = { search: '', hash: '', pathname: '/' };
const nodes = {};
const el = () => ({ innerHTML: '', textContent: '', className: '', dataset: {}, style: {},
  addEventListener() {}, setAttribute() {}, classList: { add() {}, remove() {}, toggle() {} } });
global.document = Object.assign(el(), { documentElement: el(), body: el(), createElement: el,
  querySelectorAll: () => [], querySelector: () => null, getElementById: (id) => (nodes[id] = nodes[id] || el()) });
const store = {};
let broken = false;
global.localStorage = {
  getItem: (k) => { if (broken) throw new Error('нет доступа'); return k in store ? store[k] : null; },
  setItem: (k, v) => { if (broken) throw new Error('нет доступа'); store[k] = String(v); },
};
global.matchMedia = () => ({ matches: false, addEventListener() {} });
for (const f of ['core.js', 'components.js', 'screens/home.js', 'screens/help.js']) require(path.join(process.env.JS_DIR, f));
const App = window.App;
App.toast = () => {};
const st = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const withUpdate = (latest) => Object.assign({}, st, { update: Object.assign({}, st.update, { latest }) });
const home = (s) => { App.screens.home.update(s); return nodes['home-update'].innerHTML; };
const help = (s) => { App.screens.help.update(s); return nodes['help-update'].innerHTML; };
App.screens.help.mount(el());
const out = { shown: home(st), help: help(st) };
out.action = home(Object.assign({}, st, { rescue: Object.assign({}, st.rescue, { action: { id: 'token' } }) }));
out.none = home(Object.assign({}, st, { update: null }));
App.actions['update-hide']({ dataset: { version: st.update.latest } });
out.stored = store['yadisk-update-hidden'];
out.hidden = home(st);
out.help_after_hide = help(st);
out.next = home(withUpdate('99.0.0'));
broken = true;  // приватный режим: хранилище бросает исключения
out.broken = home(withUpdate('98.0.0'));
App.actions['update-hide']({ dataset: { version: '98.0.0' } });
out.broken_hidden = home(withUpdate('98.0.0'));
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_update_banner_on_home_and_in_help(monkeypatch):
    monkeypatch.setenv("YADISK_DEMO", "1")
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    st = client.get("/api/status?demo=update").json()
    latest = st["update"]["latest"]
    res = subprocess.run(["node", "-e", _UPDATE_HARNESS], input=json.dumps(st), capture_output=True, text=True,
                         encoding="utf-8", env={**os.environ, "JS_DIR": str(ASSETS / "static" / "js")})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    shown = out["shown"]
    assert f"Вышла версия {latest}" in shown and f"У вас версия {utils.APP_VERSION}" in shown
    # Программа только подсказывает: архив скачивает сам человек по ссылке
    assert 'href="https://github.com/PXSYS13/yadisk_backup/archive/refs/heads/main.zip"' in shown
    assert f'href="{st["update"]["url"]}"' in shown and "Что нового" in shown
    assert "поверх старой папки" in shown and "прогресс скачивания сохранятся" in shown
    assert f'data-action="update-hide" data-version="{latest}"' in shown
    # В «Помощи» та же строка, но без «Скрыть»: туда приходят с проблемой, а новая версия может её решать
    assert f"Вышла версия {latest}" in out["help"] and "update-hide" not in out["help"]
    # Пока нужно действие — плашки нет; новой версии нет — тоже
    assert out["action"] == "" and out["none"] == ""
    # «Скрыть до следующей версии»: эту версию больше не показываем, следующую — снова
    assert out["stored"] == latest and out["hidden"] == ""
    assert f"Вышла версия {latest}" in out["help_after_hide"]
    assert "Вышла версия 99.0.0" in out["next"]
    # Хранилище недоступно — плашка работает, а скрытие помнится до перезагрузки страницы
    assert "Вышла версия 98.0.0" in out["broken"] and out["broken_hidden"] == ""


# Ядро: перерисовка без лишних записей, склонения, демо и обновление статуса — в node, fetch подменён
_CORE_HARNESS = r"""
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
const fetches = [];
const answers = [];
global.fetch = (url, opts) => {
  fetches.push(`${(opts && opts.method) || 'GET'} ${url}`);
  return new Promise((resolve) => answers.push(resolve));
};
require(path.join(process.env.JS_DIR, 'core.js'));
const App = window.App;
const later = () => new Promise((r) => setTimeout(r, 0));
(async () => {
  const out = {};
  // Браузер записывает разметку по-своему (<path/> → <path></path>): та же разметка — DOM не трогаем
  let writes = 0;
  const box = { set innerHTML(v) { writes++; this.v = v; }, get innerHTML() { return String(this.v).replace('/>', '></path>'); } };
  const svg = '<svg><path d="M0 0"/></svg>';
  App.html(box, svg);
  App.html(box, svg);
  out.same = writes;
  App.forgetHtml(box);
  App.html(box, svg);
  out.forgotten = writes;
  out.filesOf = [1, 2, 5, 11, 21, 0].map(App.fmt.filesOf);
  // Демо показывает придуманные данные — кнопки не уходят в настоящую программу
  App.state.status = { demo: 'waiting' };
  const blocked = await App.api.post('/api/rescue/start');
  out.demo = { ok: blocked.ok, sent: fetches.length };
  App.state.status = null;
  // Кнопка что-то поменяла, пока запрос статуса был в пути: его ответ не рисуем, спрашиваем снова
  const shown = [];
  App.render = () => shown.push(App.state.status && App.state.status.n);
  const reply = (n) => answers.shift()({ ok: true, status: 200, json: async () => ({ n }) });
  const first = App.refresh();
  App.refresh();
  reply(1);
  await later();
  reply(2);
  await first;
  out.shown = shown;
  out.asked = fetches.length;
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_core_repaints_only_changes_and_keeps_fresh_status():
    res = subprocess.run(["node", "-e", _CORE_HARNESS], capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "JS_DIR": str(ASSETS / "static" / "js")})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["same"] == 1 and out["forgotten"] == 2
    assert out["filesOf"] == ["1 файла", "2 файлов", "5 файлов", "11 файлов", "21 файла", "0 файлов"]
    assert out["demo"] == {"ok": False, "sent": 0}
    assert out["shown"] == [2] and out["asked"] == 2


# «Проверка скачанных файлов» в «Подробнее»: карточка читает строки verify_* из журнала задачи
_VERIFY_HARNESS = r"""
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
const inp = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const posted = [];
global.fetch = async (url, opts) => {
  if (opts && opts.method === 'POST') posted.push([url, JSON.parse(opts.body)]);
  const data = url.startsWith('/api/verify/requeue') ? { ok: true, count: 5, msg: 'ok' }
    : url.startsWith('/api/status') ? null : { items: [], reasons: {}, total: 0 };
  return { ok: true, status: 200, json: async () => data };
};
for (const f of ['core.js', 'components.js', 'screens/details.js']) require(path.join(process.env.JS_DIR, f));
const App = window.App;
App.toast = () => {};
const d = App.screens.details;
const S = (tasks, running = false) => Object.assign({}, inp.base, { tasks, pipeline: { running } });
const verifyTask = (lines, running, extra = {}) => ({ disk: Object.assign({ kind: 'disk_verify', running,
  finished_at: running ? null : inp.at, exit_code: running ? null : 1, was_stopped: false, log_tail: lines }, extra) });
const snap = () => ({ main: nodes['det-verify'].innerHTML, actions: nodes['det-verify-actions'].innerHTML,
  list: nodes['det-verify-list'].innerHTML });
(async () => {
  d.mount(el());
  d.show({ source: 'disk' });
  const out = {};
  d.update(S({})); out.idle = snap();
  d.update(S({}, true)); out.idle_busy = snap();
  d.update(S(verifyTask(['21:23:03  ' + inp.progress], true))); out.running = snap();
  d.update(S(verifyTask(['21:23:03  ' + inp.progress, '21:23:09  ' + inp.summary], false))); out.result = snap();
  d.update(S(verifyTask([inp.summary], false), true)); out.result_busy = snap();
  d.update(S(verifyTask([inp.ok], false))); out.ok = snap();
  d.update(S(verifyTask([], false, { was_stopped: true, exit_code: null }))); out.stopped = snap();
  d.update(S({ disk: { kind: 'disk_scan', running: true, log_tail: [] } })); out.listing = snap();
  d.update(S(verifyTask([inp.unchecked], false))); out.unchecked = snap();
  App.confirm = async () => true;
  d.update(S(verifyTask([inp.summary], false)));
  await App.actions['verify-requeue']({ dataset: { source: 'disk', count: '5', bytes: '10200327', at: String(inp.at) } });
  d.update(S(verifyTask([inp.summary], false))); out.requeued = snap();
  d.update(S(verifyTask([inp.summary], false), true)); out.requeued_busy = snap();
  out.posted = posted.filter(([url]) => url.startsWith('/api/verify'));
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js не установлен")
def test_details_verify_card_states():
    import verify

    summary = {"checked": 32, "ok": 27, "missing": 3, "size": 1, "md5": 1, "unreadable": 0, "no_md5": 1,
               "unchecked": 0, "bytes": 32_101_855, "bad_bytes": 10_200_327, "all_missing": False, "seconds": 3,
               "examples": {"missing": [{"path": "disk:/Фото/нет.jpg", "size": 700}],
                            "size": [{"path": "disk:/Видео/Свадьба.mp4", "size": 8_000_000, "local_size": 3_100_000}],
                            "md5": [{"path": "disk:/Фото/IMG_1102.JPG", "size": 640_000}]}}
    ok = dict(summary, checked=28, ok=28, missing=0, size=0, md5=0, bad_bytes=0, examples={})
    progress = {"stage": "md5", "done": 3, "total": 27, "bytes_done": 1_610_612_736,
                "bytes_total": 4_316_856_824, "eta_sec": 125, "found": 0}
    inp = {"base": {"rescue": {"sources": {"disk": {"files": {"done": 32, "selected": 37}}}},
                    "disk": {"db": {"recent": []}}},
           "at": 1760891234.5, "summary": verify.summary_line(summary), "ok": verify.summary_line(ok),
           "progress": verify.progress_line(progress),
           "unchecked": verify.summary_line(dict(ok, checked=0, ok=0, unchecked=1200))}
    res = subprocess.run(["node", "-e", _VERIFY_HARNESS], input=json.dumps(inp), capture_output=True, text=True,
                         encoding="utf-8", env={**os.environ, "JS_DIR": str(ASSETS / "static" / "js")})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.replace("\xa0", " "))

    idle = out["idle"]
    assert "читает каждый файл целиком" in idle["main"]
    assert 'data-action="verify-start"' in idle["actions"] and "disabled" not in idle["actions"]
    # Пока качается — кнопка неактивна и сказано почему
    assert " disabled>" in out["idle_busy"]["actions"]
    assert "Можно, когда скачивание остановлено или закончено" in out["idle_busy"]["actions"]

    running = out["running"]
    assert "Сверяю содержимое с Яндексом (MD5): 1,5 ГБ из 4,0 ГБ" in running["main"]
    assert "37% · осталось ≈ 2 мин · пока всё в порядке" in running["main"]
    assert 'data-action="verify-stop"' in running["actions"]

    result = out["result"]
    assert "Проверено 32 файла. Не хватает 3 файлов, повреждено 2" in result["main"]
    assert "У 1 файла Яндекс не сообщил контрольную сумму" in result["main"]
    assert 'data-action="verify-requeue"' in result["actions"] and 'data-count="5"' in result["actions"]
    assert "Проверить ещё раз" in result["actions"]
    assert "/Фото/нет.jpg" in result["list"] and "disk:/" not in result["list"]
    assert "на диске 3,0 МБ, у Яндекса 7,6 МБ" in result["list"]
    assert result["actions"].count(" disabled>") == 0 and out["result_busy"]["actions"].count(" disabled>") == 2

    assert "Проверено 28 файлов — всё в порядке" in out["ok"]["main"] and out["ok"]["list"] == ""
    assert "verify-requeue" not in out["ok"]["actions"]
    assert "Проверка остановлена" in out["stopped"]["main"]
    assert "Сейчас собирается список файлов" in out["listing"]["actions"]
    # Безлимит старых версий: файлы скачаны, но где лежат — не записано
    assert "У 1 200 файлов программа старой версии не записала" in out["unchecked"]["main"]
    assert "verify-start" not in out["unchecked"]["actions"]

    # «Скачать заново» → файлы в очереди
    assert out["posted"] == [["/api/verify/requeue", {"source": "disk"}]]
    # «Скачать сейчас» — здесь же: после «Готово» на Главной нет «Продолжить»
    assert "5 файлов снова в очереди. Нажмите «Скачать сейчас»" in out["requeued"]["main"]
    assert 'data-action="rescue-start"' in out["requeued"]["actions"] and 'href="#/home"' in out["requeued"]["actions"]
    assert "verify-requeue" not in out["requeued"]["actions"] and "verify-start" not in out["requeued"]["actions"]
    assert "Скачивание уже идёт" in out["requeued_busy"]["main"] and "rescue-start" not in out["requeued_busy"]["actions"]


def test_help_answers_deleted_files_with_real_buttons():
    """«Случайно удалил файлы…»: «Помощь» и README называют кнопки так, как они подписаны в программе."""
    ui = "\n".join(p.read_text(encoding="utf-8") for p in JS_FILES if p.name != "help.js")
    reasons = (Path(webapp.paths.PROJECT) / "webapp" / "queue_db.py").read_text(encoding="utf-8")
    help_js = (ASSETS / "static" / "js" / "screens" / "help.js").read_text(encoding="utf-8")
    readme = (Path(webapp.paths.PROJECT) / "README.md").read_text(encoding="utf-8")
    for text in (help_js, readme):
        assert "Случайно удалил файлы, пока шло скачивание" in text
        assert "30 дней" in text and "Корзин" in text and "«Восстановить»" in text
        for label in ("Подробнее", "Проверить скачанные файлы", "Скачать заново", "Скачать сейчас",
                      "Повторить не скачавшиеся"):
            assert f"«{label}»" in text and label in ui, label
        assert "«Файла больше нет на Диске»" in text and "Файла больше нет на Диске" in reasons
