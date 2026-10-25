"""Публичные альбомы: полный обход курсора, оригиналы и существующая очередь."""

import hashlib
import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import requests

import public_download as pd
from utils import EXIT_DEFERRED, EXIT_OK

ALBUM = "https://disk.yandex.ru/a/TestAlbum"


def response(body=b"", data=None, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data).encode("utf-8") if data is not None else body
    result._content_consumed = True
    result.headers.update({"Content-Length": str(len(result._content)), "Content-Type": "application/octet-stream"})
    return result


class AlbumSession:
    """Веб-клиент с порциями по два файла — меньше LIST_LIMIT REST API папок."""

    def __init__(self):
        self.ids = [f"{i:024x}" for i in range(1, 6)]
        self.files = {key: ("Оригинал " + key).encode("utf-8") for key in self.ids}
        self.pages = []
        self.downloads = []
        self.broken_page = False
        self.repeated_page = False
        self.broken_file = None
        self.api_error = None
        self.empty_incomplete = False
        self.session_count = 0

    def resource(self, key):
        md5 = hashlib.md5(self.files[key]).hexdigest()
        return {"id": key, "albumItemId": key, "type": "file", "name": "одинаковое.JPG",
                "meta": {"size": len(self.files[key]), "original": f"https://downloader.disk.yandex.ru/disk?etag={md5}"}}

    def get(self, url, timeout=None, **kwargs):
        if url.startswith(ALBUM):
            self.session_count += 1
            resources = {key: self.resource(key) for key in self.ids[:2]}
            resources["root"] = {"type": "album", "name": "Альбом", "hash": "public-hash",
                                 "children": self.ids[:2], "completed": False}
            overlays = {}
            if url != ALBUM:
                key = url.rsplit("/", 1)[-1]
                resources["slider"] = {"meta": {"xxxlPreview": "https://downloader.disk.yandex.ru/preview"}}
                overlays["sliderResourceId"] = "slider"
            store = {"resources": resources, "rootResourceId": "root", "environment": {"sk": "anonymous-sk"},
                     "overlays": overlays}
            return response(body=f'<script type="application/json" id="store-prefetch">{json.dumps(store)}</script>'.encode("utf-8"))
        if url.startswith("https://downloader.disk.yandex.ru/disk?"):
            key = parse_qs(urlsplit(url).query)["item"][0]
            self.downloads.append(key)
            return response(body=self.files[key], status=500 if key == self.broken_file else 200)
        raise AssertionError("Неожиданный запрос")

    def post(self, url, data, headers, timeout=None):
        params = json.loads(unquote(data))
        assert params["hash"] == "public-hash" and params["sk"] == "anonymous-sk"
        assert headers["Content-Type"] == "text/plain"
        assert headers["X-Retpath-Y"] == ALBUM
        if self.api_error is not None:
            return response(data=self.api_error)
        if url.endswith("/fetch-album-list"):
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
        if url.endswith("/album-download-url"):
            return response(data={"error": False, "statusCode": 200,
                                  "data": {"url": f'https://downloader.disk.yandex.ru/disk?item={params["itemId"]}'}})
        raise AssertionError("Неожиданный запрос")


@pytest.fixture()
def album(monkeypatch):
    session = AlbumSession()
    api = pd.PublicApi(session)
    monkeypatch.setattr(pd, "_thread_api", lambda: api)
    monkeypatch.setattr(pd, "_thread_http", lambda: session)
    monkeypatch.setattr(pd.time, "sleep", lambda _: None)
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


@pytest.mark.parametrize("error,kind", [
    ({"error": True, "statusCode": 404}, "not_found"),
    ({"error": True, "statusCode": 429}, "rate_limit"),
    ({"error": True, "statusCode": 500}, "server_50x"),
    ({"type": "captcha", "captcha": {"url": "https://example.invalid"}}, "refused"),
])
def test_web_api_errors_are_sanitized_without_losing_session_secrets(album, error, kind):
    session, api = album
    session.api_error = error
    with pytest.raises(pd.LinkError) as caught:
        api.download_href(ALBUM, "/photo__" + session.ids[0] + ".JPG")
    assert caught.value.kind == kind
    assert "anonymous-sk" not in caught.value.text and "public-hash" not in caught.value.text


def test_long_listing_preserves_its_cursor_and_preview_uses_slider_metadata(album):
    session, api = album
    api.meta(ALBUM)
    api._album._states[ALBUM]["at"] -= 1000
    page = api.meta(ALBUM, offset=2)
    assert len(page["_embedded"]["items"]) == 2 and session.session_count == 1
    preview = api.meta(ALBUM, "/photo__" + session.ids[4] + ".JPG", preview_size="XXXL")
    assert preview["preview"] == "https://downloader.disk.yandex.ru/preview"


@pytest.mark.parametrize("field,value", [("albumItemId", "../../outside"), ("albumItemId", None),
                                        ("type", "dir"), ("meta", {"size": -1})])
def test_malformed_album_items_fail_instead_of_silently_disappearing(album, field, value):
    session, _api = album
    resource = session.resource(session.ids[0])
    resource[field] = value
    with pytest.raises(pd.LinkError):
        pd.PublicAlbumApi._file(resource)
