"""Публичные альбомы: полный обход курсора, оригиналы и существующая очередь."""

import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient

import public_download as pd
import webapp.links
import webapp.tasks
import webui
from state import DEFERRED_MARKER
from utils import EXIT_DEFERRED, EXIT_OK, EXIT_OUTAGE, EXIT_PARTIAL

ALBUM = "https://disk.yandex.ru/a/TestAlbum"
DEAD = "s1klg.storage.yandex.net"


def response(body=b"", data=None, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode("utf-8") if data is not None else body
    result._content_consumed = True
    result.headers.update({"Content-Length": str(len(result._content)), "Content-Type": "application/octet-stream"})
    result.headers.update(headers or {})
    return result


class AlbumSession:
    """Веб-клиент с порциями по два файла — меньше LIST_LIMIT REST API папок."""

    def __init__(self):
        self.ids = [f"{i:024x}" for i in range(1, 6)]
        self.files = {key: ("Оригинал " + key).encode("utf-8") for key in self.ids}
        self.names = {}                 # ID → своё имя фото вместо «одинаковое.JPG»
        self.pages = []
        self.downloads = []
        self.broken_page = False
        self.repeated_page = False
        self.broken_file = None
        self.api_error = None           # ответ с ошибкой в JSON на любой запрос веб-клиента
        self.post_status = None         # HTTP-код ответа на любой запрос веб-клиента
        self.empty_incomplete = False
        self.session_count = 0          # открытые страницы: альбома и отдельных фото
        self.item_pages = []            # страницы отдельных фото
        self.page_status = 200          # HTTP-код страницы альбома
        self.page_html = None           # страница без данных альбома (проверка на робота)
        self.root_error = None          # errorCode альбома в данных страницы
        self.environment = None         # подмена environment в данных страницы
        self.sk = "anonymous-sk"        # sk, который выдаёт страница
        self.stale = None               # "http" | "json": как Яндекс отклоняет запрос со старым sk
        self.missing = {}               # ID → "http" | "json": album-download-url отвечает 404
        self.dead = set()               # ID оригиналов на молчащем сервере хранения
        self.previews_in_list = False   # в сведениях о фото есть ссылка на превью
        self.spoiled = {}               # ID → функция, которая портит сведения о фото
        self.posts = []                 # (запрос веб-клиента, ID фото или курсор)

    def resource(self, key):
        md5 = hashlib.md5(self.files[key]).hexdigest()
        meta = {"size": len(self.files[key]), "original": f"https://downloader.disk.yandex.ru/disk?etag={md5}"}
        if self.previews_in_list:
            meta["xxxlPreview"] = f"https://downloader.disk.yandex.ru/preview?item={key}"
        item = {"id": key, "albumItemId": key, "type": "file", "name": self.names.get(key, "одинаковое.JPG"),
                "meta": meta}
        return self.spoiled[key](item) if key in self.spoiled else item

    def get(self, url, timeout=None, **kwargs):
        if url.startswith(ALBUM):
            self.session_count += 1
            if url != ALBUM:
                self.item_pages.append(url.rsplit("/", 1)[-1])
            if self.page_status != 200:
                return response(status=self.page_status)
            if self.page_html is not None:
                return response(body=self.page_html.encode("utf-8"))
            resources = {key: self.resource(key) for key in self.ids[:2]}
            resources["root"] = {"type": "album", "name": "Альбом", "hash": "public-hash",
                                 "children": self.ids[:2], "completed": False}
            if self.root_error:
                resources["root"] = {"errorCode": self.root_error}
            overlays = {}
            if url != ALBUM:
                resources["slider"] = {"meta": {"xxxlPreview": "https://downloader.disk.yandex.ru/preview"}}
                overlays["sliderResourceId"] = "slider"
            store = {"resources": resources, "rootResourceId": "root", "overlays": overlays,
                     "environment": self.environment if self.environment is not None else {"sk": self.sk}}
            return response(body=f'<script type="application/json" id="store-prefetch">{json.dumps(store)}</script>'.encode("utf-8"))
        if url.startswith("https://downloader.disk.yandex.ru/disk?"):
            key = parse_qs(urlsplit(url).query)["item"][0]
            self.downloads.append(key)
            if key in self.dead:
                return response(status=302, headers={"Location": f"https://{DEAD}/f?item={key}"})
            return response(body=self.files[key], status=500 if key == self.broken_file else 200)
        if url.startswith("https://downloader.disk.yandex.ru/preview"):
            return response(body=b"JPEGPREVIEW", headers={"Content-Type": "image/jpeg"})
        if urlsplit(url).hostname == DEAD:
            raise requests.exceptions.ConnectTimeout("timed out")
        raise AssertionError("Неожиданный запрос")

    def post(self, url, data, headers, timeout=None):
        params = json.loads(unquote(data))
        assert params["hash"] == "public-hash"
        assert headers["Content-Type"] == "text/plain"
        assert headers["X-Retpath-Y"] == ALBUM
        action = url.rsplit("/", 1)[-1]
        self.posts.append((action, params.get("itemId") or params.get("lastItemId")))
        if params["sk"] != self.sk:
            if self.stale == "http":
                return response(status=403)
            return response(data={"error": True, "statusCode": 401})
        if self.post_status is not None:
            return response(status=self.post_status)
        if self.api_error is not None:
            return response(data=self.api_error)
        if action == "fetch-album-list":
            self.pages.append(params["lastItemId"])
            if self.broken_page:
                return response(status=500)
            start = self.ids.index(params["lastItemId"]) + 1
            keys = self.ids[start:start + 2]
            if self.repeated_page:
                keys = self.ids[:2]
            if self.empty_incomplete:
                keys = []
            return response(data={"resources": [self.resource(key) for key in keys],
                                  "completed": start + len(keys) >= len(self.ids)})
        if action == "album-download-url":
            key = params["itemId"]
            if key in self.missing:
                if self.missing[key] == "http":
                    return response(status=404)
                return response(data={"error": True, "statusCode": 404})
            return response(data={"error": False, "statusCode": 200,
                                  "data": {"url": f"https://downloader.disk.yandex.ru/disk?item={key}"}})
        raise AssertionError("Неожиданный запрос")


@pytest.fixture()
def album(monkeypatch):
    session = AlbumSession()
    api = pd.PublicApi(session)
    monkeypatch.setattr(pd, "_thread_api", lambda: api)
    monkeypatch.setattr(pd, "_thread_http", lambda: session)
    monkeypatch.setattr(pd.time, "sleep", lambda _: None)
    monkeypatch.setattr(pd, "_album_previews", {})
    monkeypatch.setenv("YADISK_PREVIEWS", "0")
    return session, api


@pytest.fixture()
def db():
    conn = pd.connect_db()
    yield conn
    conn.close()


def add(conn):
    pd.add_link(conn, ALBUM, meta={"name": "Альбом", "type": "album"})
    return conn.execute("SELECT * FROM links").fetchone()


def link(conn):
    return conn.execute("SELECT * FROM links").fetchone()


def status_of(conn, key):
    return conn.execute("SELECT status FROM link_files WHERE path LIKE ?", (f"%__{key}%",)).fetchone()[0]


def card(task=None):
    """Карточка ссылки в разделе «По ссылке» — как её строит /api/status."""
    rows, meta = webapp.links.links_rows()
    return webapp.links.build_links_status(rows, meta, task, time.time(), "f")["items"][0]


def test_short_album_pages_are_all_listed_and_duplicate_names_survive(db, album):
    session, api = album
    assert pd.list_link(api, db, add(db)) == 5
    rows = db.execute("SELECT path, size, md5 FROM link_files").fetchall()
    assert len({row["path"].casefold() for row in rows}) == 5
    assert session.pages == [session.ids[1], session.ids[3]]
    assert all(row["size"] and row["md5"] for row in rows)
    assert db.execute("SELECT kind FROM links").fetchone()[0] == "album"


def test_originals_use_existing_integrity_checks_and_are_not_downloaded_twice(db, album, tmp_path):
    session, api = album
    pd.list_link(api, db, add(db))
    assert pd.run(db, str(tmp_path), 1) == EXIT_OK
    assert sorted(session.downloads) == session.ids
    for row in db.execute("SELECT local_path FROM link_files"):
        with open(row["local_path"], "rb") as saved:
            assert saved.read() in session.files.values()
    assert pd.run(db, str(tmp_path), 1) == EXIT_OK
    assert len(session.downloads) == 5
    # Пересбор списка также сохраняет завершённые записи и стабильные пути.
    pd.list_link(api, db, db.execute("SELECT * FROM links").fetchone())
    assert db.execute("SELECT COUNT(*) FROM link_files WHERE status='downloaded'").fetchone()[0] == 5
    assert "anonymous-sk" not in "".join(db.iterdump())
    assert "downloader.disk.yandex.ru" not in "".join(db.iterdump())


@pytest.mark.parametrize("failure", ["broken_page", "repeated_page", "empty_incomplete"])
def test_incomplete_listing_never_marks_missing_files_gone(db, album, failure):
    session, api = album
    pd.list_link(api, db, add(db))
    previous = dict(db.execute("SELECT * FROM links").fetchone())
    setattr(session, failure, True)
    with pytest.raises(pd.LinkError):
        pd.list_link(api, db, previous)
    current = dict(db.execute("SELECT * FROM links").fetchone())
    assert current["gen"] == previous["gen"] and current["listed_at"] == previous["listed_at"]
    assert db.execute("SELECT COUNT(*) FROM link_files WHERE status='gone'").fetchone()[0] == 0


def test_temporarily_unavailable_album_file_is_retried_by_existing_rounds(db, album, tmp_path):
    session, api = album
    session.broken_file = session.ids[0]
    pd.list_link(api, db, add(db))
    assert pd.run(db, str(tmp_path), 1) == EXIT_DEFERRED
    assert db.execute("SELECT COUNT(*) FROM link_files WHERE status='downloaded'").fetchone()[0] == 4
    session.broken_file = None
    assert pd.run(db, str(tmp_path), 1) == EXIT_OK
    assert db.execute("SELECT COUNT(*) FROM link_files WHERE status='downloaded'").fetchone()[0] == 5


@pytest.mark.parametrize("error,kind,hold", [
    ({"error": True, "statusCode": 404}, "missing", False),
    ({"error": True, "statusCode": 429}, "rate_limit", True),
    ({"error": True, "statusCode": 401}, "refused", True),
    ({"error": True, "statusCode": 500}, "server_50x", False),
    ({"type": "captcha", "captcha": {"url": "https://example.invalid"}}, "captcha", True),
])
def test_web_api_errors_are_sanitized_without_losing_session_secrets(album, error, kind, hold):
    session, api = album
    session.api_error = error
    with pytest.raises(pd.LinkError) as caught:
        api.download_href(ALBUM, "/photo__" + session.ids[0] + ".JPG")
    assert (caught.value.kind, caught.value.hold, caught.value.link_level) == (kind, hold, False)
    assert "anonymous-sk" not in caught.value.text and "public-hash" not in caught.value.text
    # Проверку в браузере программа за себя не пройдёт: обещаем повтор, а не просим открыть альбом
    assert "откройте" not in caught.value.text


def test_long_listing_preserves_its_cursor_and_preview_uses_slider_metadata(album):
    session, api = album
    api.meta(ALBUM)
    api._album._states[ALBUM]["at"] -= 1000
    page = api.meta(ALBUM, offset=2)
    assert len(page["_embedded"]["items"]) == 2 and session.session_count == 1
    preview = api.meta(ALBUM, "/photo__" + session.ids[4] + ".JPG", preview_size="XXXL")
    assert preview["preview"] == "https://downloader.disk.yandex.ru/preview"


# ── 404 про одно фото ───────────────────────────────────────────────────────


@pytest.mark.parametrize("answer", ["json", "http"])
def test_missing_album_photo_waits_for_a_full_listing_instead_of_finishing_the_album(db, album, tmp_path, answer):
    """404 на ссылку одного фото бывает и у живого фото (Яндекс сменил адрес, устарел hash):
    фото ждёт, а удалённым его считает только полный список альбома."""
    session, api = album
    key = session.ids[2]
    session.missing[key] = answer
    pd.list_link(api, db, add(db))
    assert pd.run(db, str(tmp_path), 1) == EXIT_DEFERRED
    row = db.execute("SELECT status, error, attempts FROM link_files WHERE path LIKE ?", (f"%__{key}%",)).fetchone()
    assert row["status"] == "pending" and row["error"].startswith(DEFERRED_MARKER) and row["attempts"] == 0
    assert db.execute("SELECT error_kind FROM links").fetchone()[0] is None
    item = card()
    assert item["files"]["pending"] == 1 and "все файлы скачаны" not in item["status_text"]
    # Фото по-прежнему в альбоме — ждёт дальше
    pd.list_link(api, db, link(db))
    assert status_of(db, key) == "pending"
    # Владелец убрал фото: полный список это и решает
    session.ids.remove(key)
    pd.list_link(api, db, link(db))
    assert status_of(db, key) == "gone"
    assert card()["state"] == "done"


def test_album_answering_404_for_every_photo_is_never_emptied(db, album, tmp_path):
    """Яндекс сменил запрос ссылок или отклоняет hash: без этого альбом «готов» и пуст."""
    session, api = album
    session.missing = dict.fromkeys(session.ids, "json")
    pd.list_link(api, db, add(db))
    assert pd.run(db, str(tmp_path), 1) == EXIT_DEFERRED
    assert db.execute("SELECT COUNT(*) FROM link_files WHERE status='pending'").fetchone()[0] == 5
    assert card()["state"] != "done"


# ── Проверка на робота, 429, отказы ─────────────────────────────────────────


@pytest.mark.parametrize("answer", [
    {"json": {"type": "captcha", "captcha": {"url": "https://example.invalid"}}},
    {"json": {"error": True, "statusCode": 429}},
    {"http": 429},
    {"http": 400},
])
def test_captcha_429_and_refusals_hold_the_album_until_the_next_round(db, album, tmp_path, monkeypatch, answer):
    session, api = album
    sleeps = []
    monkeypatch.setattr(pd.time, "sleep", sleeps.append)
    pd.list_link(api, db, add(db))
    session.api_error, session.post_status = answer.get("json"), answer.get("http")
    assert pd.run(db, str(tmp_path), 1) == EXIT_OUTAGE  # Яндекс отказывает — повтор позже
    asked = [p for p in session.posts if p[0] == "album-download-url"]
    # Ни повторов, ни пауз по каждому файлу: после первого отказа ссылку не трогаем. Второй
    # запрос — файл, который поток уже взял в работу (workers × 2)
    assert len(asked) <= 2 and not sleeps
    rows = db.execute("SELECT status, attempts FROM link_files").fetchall()
    assert {r["status"] for r in rows} == {"pending"} and not any(r["attempts"] for r in rows)
    row = link(db)
    assert row["error_kind"] is None and "программа попробует снова позже" in row["error"]
    item = card({"running": True})
    assert item["state"] == "waiting" and row["error"] in item["status_text"]
    # Следующий раунд пробует снова сам — и забывает причину
    session.api_error = session.post_status = None
    assert pd.run(db, str(tmp_path), 1) == EXIT_OK
    assert link(db)["error"] is None


def test_album_page_without_data_holds_the_link_instead_of_reloading_it_for_every_file(db, album, tmp_path):
    session, api = album
    pd.list_link(api, db, add(db))
    api._album._states[ALBUM]["at"] -= 1000  # потоки откроют страницу альбома заново
    session.page_html = "<html>Подтвердите, что запросы отправляли вы, а не робот</html>"
    loads = session.session_count
    assert pd.run(db, str(tmp_path), 1) == EXIT_OUTAGE
    assert session.session_count - loads <= 2 and not session.downloads
    assert "проверку на робота" in link(db)["error"]


@pytest.mark.parametrize("page,words", [
    ("captcha", "Список файлов ещё не получен. Яндекс не отдал данные альбома: попросил проверку на робота"),
    (500, "Список файлов ещё не получен. Яндекс сейчас не отдал данные альбома"),
])
def test_album_listing_problem_is_told_on_its_card(db, album, tmp_path, page, words):
    session, _api = album
    add(db)
    if page == "captcha":
        session.page_html = "<html>робот?</html>"
    else:
        session.page_status = page
    assert pd.run(db, str(tmp_path), 1) == EXIT_OUTAGE
    item = card()
    assert item["state"] == "paused" and item["status_text"].startswith(words)
    # В этом раунде список уже не получен: пока программа качает другие ссылки, карточка
    # не обещает «Получаю список файлов»
    assert card({"running": True})["status_text"].startswith(words)


def test_deferred_album_photos_get_previews_from_the_listing_without_opening_their_pages(
        db, album, tmp_path, monkeypatch):
    session, api = album
    monkeypatch.setenv("YADISK_PREVIEWS", "1")
    session.previews_in_list = True
    session.dead.update(session.ids)
    pd.list_link(api, db, add(db))
    assert pd.run(db, str(tmp_path), 1) == EXIT_DEFERRED
    assert session.item_pages == []
    previews = [r[0] for r in db.execute("SELECT preview_path FROM link_files")]
    assert len(set(previews)) == 5 and all(Path(p).read_bytes() == b"JPEGPREVIEW" for p in previews)


def test_album_previews_open_few_photo_pages_per_round_when_the_listing_has_none(db, album, tmp_path, monkeypatch):
    session, api = album
    monkeypatch.setenv("YADISK_PREVIEWS", "1")
    monkeypatch.setattr(pd, "ALBUM_PREVIEW_PAGES", 2)
    session.dead.update(session.ids)
    pd.list_link(api, db, add(db))
    assert pd.run(db, str(tmp_path), 1) == EXIT_DEFERRED
    assert len(session.item_pages) == 2
    assert db.execute("SELECT COUNT(preview_path) FROM link_files").fetchone()[0] == 2


# ── Отказ сессии и владельца ────────────────────────────────────────────────


@pytest.mark.parametrize("stale", ["http", "json"])
def test_refused_session_is_renewed_once_and_the_album_keeps_downloading(db, album, tmp_path, stale):
    session, api = album
    pd.list_link(api, db, add(db))
    session.sk, session.stale = "fresh-sk", stale  # Яндекс сменил sk сессии: старый отклоняется
    assert pd.run(db, str(tmp_path), 1) == EXIT_OK
    assert link(db)["error_kind"] is None and sorted(session.downloads) == session.ids
    assert session.session_count == 2  # список и одно обновление сессии, а не страница на файл


def test_album_refusing_even_a_fresh_session_waits_for_the_next_round_not_for_a_click(db, album, tmp_path):
    session, api = album
    pd.list_link(api, db, add(db))
    session.post_status = 403
    assert pd.run(db, str(tmp_path), 1) == EXIT_OUTAGE
    row = link(db)
    assert row["error_kind"] is None and "отклонил" in row["error"]
    session.post_status = None
    assert pd.run(db, str(tmp_path), 1) == EXIT_OK


def test_owner_closing_the_album_still_stops_the_link(db, album, tmp_path):
    session, _api = album
    add(db)
    session.root_error = 403
    assert pd.run(db, str(tmp_path), 1) == EXIT_PARTIAL
    assert link(db)["error_kind"] == "forbidden" and card()["state"] == "error"


# ── Неполные сведения о фото ────────────────────────────────────────────────


@pytest.mark.parametrize("spoil", [
    lambda r: {**r, "albumItemId": "../../outside"},
    lambda r: {**r, "albumItemId": None},
    lambda r: {**r, "type": "dir"},
    lambda r: {**r, "meta": {"size": -1}},
    lambda r: {**r, "meta": {**r["meta"], "size": "12"}},
    lambda r: {**r, "meta": "сломано"},
    lambda r: "не словарь",
    lambda r: None,
], ids=["id-path", "id-none", "dir", "size-negative", "size-text", "meta-text", "not-a-dict", "none"])
@pytest.mark.parametrize("where", [0, 3], ids=["album-page", "next-page"])
def test_malformed_album_items_are_skipped_and_the_listing_counts_as_incomplete(db, album, capsys, spoil, where):
    session, api = album
    pd.list_link(api, db, add(db))
    removed = session.ids.pop()  # владелец убрал фото — но список неполный, и это не видно
    session.spoiled[session.ids[where]] = spoil
    assert pd.list_link(api, db, link(db)) == 3
    assert db.execute("SELECT COUNT(*) FROM link_files WHERE status='gone'").fetchone()[0] == 0
    assert status_of(db, removed) == "pending"
    assert "Пропущено 1 файл" in capsys.readouterr().out


def test_odd_album_data_never_crashes_the_round(db, album, tmp_path):
    session, _api = album
    add(db)
    session.spoiled[session.ids[0]] = lambda r: "не словарь"
    assert pd.run(db, str(tmp_path), 1) == EXIT_OK
    assert db.execute("SELECT COUNT(*) FROM link_files WHERE status='downloaded'").fetchone()[0] == 4


def test_odd_answer_to_a_download_link_request_is_a_link_error(album):
    session, api = album
    session.api_error = {"data": "ссылка"}
    with pytest.raises(pd.LinkError) as caught:
        api.download_href(ALBUM, "/photo__" + session.ids[0] + ".JPG")
    assert caught.value.kind == "server_50x"


def test_adding_an_album_with_odd_data_is_not_an_http_500(album, monkeypatch):
    session, _api = album
    session.environment = "не словарь"
    monkeypatch.setattr(webapp.tasks.tm, "start", lambda *a: (True, "ok"))
    monkeypatch.setattr(webapp.tasks.tm, "any_alive", lambda prefix: False)
    monkeypatch.setattr(webapp.links, "PublicApi", lambda timeout=None: pd.PublicApi(session, timeout))
    r = TestClient(webui.app, headers={"Host": "localhost:8000"}).post("/api/links/add", json={"url": ALBUM})
    assert r.status_code == 200 and "попробует сама" in r.json()["msg"]


# ── Имена файлов ────────────────────────────────────────────────────────────


def test_album_rows_keep_yandex_names_and_only_files_on_disk_get_cleaned(db, album, tmp_path, monkeypatch):
    session, api = album
    key = session.ids[0]
    session.names[key] = "Встреча: итоги?.jpg"
    pd.list_link(api, db, add(db))
    path = f"/Встреча: итоги?__{key}.jpg"
    assert path in {r[0] for r in db.execute("SELECT path FROM link_files")}
    assert pd.run(db, str(tmp_path), 1) == EXIT_OK
    local = db.execute("SELECT local_path FROM link_files WHERE path=?", (path,)).fetchone()[0]
    assert os.path.basename(local) == f"Встреча_ итоги___{key}.jpg"
    # Правка очистки имён не меняет записи: новый сбор списка не качает дубликаты
    monkeypatch.setattr(pd, "sanitize_component", lambda name: name.replace(":", "-").replace("?", "-"))
    pd.list_link(api, db, link(db))
    assert tuple(db.execute("SELECT COUNT(*), SUM(status='downloaded') FROM link_files").fetchone()) == (5, 5)


def test_long_cyrillic_album_names_keep_the_photo_id_and_fit_in_bytes(db, album, monkeypatch):
    """255 — предел имени в байтах UTF-8 на Linux и macOS: русская буква — два байта."""
    import utils

    session, api = album
    key = session.ids[0]
    session.names[key] = "Я" * 200 + ".jpg"
    pd.list_link(api, db, add(db))
    monkeypatch.setattr(utils, "IS_WINDOWS", False)
    job = next(j for j in pd._take_jobs(db, time.time()) if key in j.path)
    name = os.path.basename(pd.local_target("/dl", job))
    assert len(name.encode("utf-8")) <= utils.MAX_NAME_LEN and name.endswith(f"__{key}.jpg")
    assert name.startswith("Я" * 100)
