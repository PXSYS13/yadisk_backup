"""Страховочные превью: уменьшенная копия фото, пока оригинал лежит на молчащем сервере."""

import os
import sqlite3

import pytest
import requests

import downloader
import organize
import storage_fetch as sf
import unlim_worker as uw
import webui
from state import State
from utils import PREVIEW_DIRNAME, keep_awake, preview_path_for, previews_enabled

JPEG = b"\xff\xd8\xff\xe0" + b"preview" * 10
DEAD = "s1klg.storage.yandex.net"


class _Stream:
    def __init__(self, data=b"", status=200, headers=None):
        self._data = data
        self.status_code = status
        self.headers = headers if headers is not None else {"Content-Type": "image/jpeg"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size):
        if self._data:
            yield self._data


class _Http:
    """https://dl/<host>/<x> → 302 → host; https://preview/<x> → JPEG; dead host — тишина."""

    def __init__(self):
        self.hits = []

    def get(self, url, stream=False, timeout=None, allow_redirects=True):
        host = url.split("/")[2]
        self.hits.append(host)
        if host == "dl":
            return _Stream(status=302, headers={"Location": "https://" + url.split("/", 3)[3]})
        if host == "preview":
            return _Stream(JPEG)
        raise requests.exceptions.ConnectTimeout("timed out")


def test_preview_path_and_switch(tmp_path, monkeypatch):
    base = str(tmp_path)
    assert preview_path_for(base, os.path.join(base, "Фото", "a.HEIC")) == \
        os.path.join(base, PREVIEW_DIRNAME, "Фото", "a.HEIC.jpg")
    monkeypatch.delenv("YADISK_PREVIEWS", raising=False)
    assert previews_enabled()
    monkeypatch.setenv("YADISK_PREVIEWS", "0")
    assert not previews_enabled()


def test_keep_awake_never_breaks_the_caller():
    with keep_awake():
        pass


def test_fetch_preview_is_atomic(tmp_path):
    dst = tmp_path / "p" / "a.jpg.jpg"
    assert sf.fetch_preview(_Http(), "https://preview/a", str(dst)) is None
    assert dst.read_bytes() == JPEG

    class Html(_Http):
        def get(self, url, **kw):
            return _Stream(b"<html>403</html>", status=200, headers={"Content-Type": "text/html"})

    bad = tmp_path / "b.jpg"
    failure = sf.fetch_preview(Html(), "https://preview/b", str(bad))
    assert failure is not None and not bad.exists()
    assert not list(tmp_path.rglob("*.part"))


class _DiskClient:
    token = "y0_test"

    def __init__(self):
        self.meta_calls = []

    def get_download_link(self, remote):
        return f"https://dl/{DEAD}/{remote.split('/')[-1]}"

    def get_meta(self, remote, **kw):
        self.meta_calls.append(remote)
        return type("Meta", (), {"preview": f"https://preview/{remote.split('/')[-1]}"})()


@pytest.fixture()
def disk_net(monkeypatch):
    downloader.DEAD_HOSTS.clear()
    http = _Http()
    monkeypatch.setattr(downloader, "_thread_http", lambda: http)
    monkeypatch.setattr(downloader, "_thread_preview_http", lambda token: http)
    monkeypatch.setattr(downloader.time, "sleep", lambda s: None)
    monkeypatch.delenv("YADISK_PREVIEWS", raising=False)
    yield http
    downloader.DEAD_HOSTS.clear()


def test_disk_deferred_photo_gets_a_preview(tmp_path, disk_net):
    cli = _DiskClient()
    res = downloader._download_one(downloader.DownloadJob("disk:/Фото/a.HEIC", 5, None), cli, str(tmp_path))
    assert res.status == "deferred"
    assert res.preview_path.endswith(os.path.join(PREVIEW_DIRNAME, "Фото", "a.HEIC.jpg"))
    assert open(res.preview_path, "rb").read() == JPEG

    st = State(str(tmp_path / "state.db"))
    st.mark_pending_batch([("disk:/Фото/a.HEIC", 5, None)])
    st.mark_deferred("disk:/Фото/a.HEIC", res.storage_host, res.error, preview_path=res.preview_path)
    assert st._conn().execute("SELECT preview_path FROM files").fetchone()[0] == res.preview_path
    st.close()
    assert webui.db_stats(tmp_path / "state.db")["previews"] == 1


def test_disk_no_preview_for_videos_or_when_switched_off(tmp_path, disk_net, monkeypatch):
    cli = _DiskClient()
    res = downloader._download_one(downloader.DownloadJob("disk:/v.mp4", 5, None), cli, str(tmp_path))
    assert res.status == "deferred" and res.preview_path == "" and cli.meta_calls == []
    monkeypatch.setenv("YADISK_PREVIEWS", "0")
    res = downloader._download_one(downloader.DownloadJob("disk:/b.jpg", 5, None), cli, str(tmp_path))
    assert res.preview_path == "" and cli.meta_calls == []


@pytest.fixture()
def unlim_db(tmp_path, monkeypatch):
    path = tmp_path / "unlim_state.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, name TEXT, size INTEGER, md5 TEXT,"
        " status TEXT DEFAULT 'pending', local_path TEXT, error TEXT, worker_id INTEGER,"
        " assigned_at TIMESTAMP, downloaded_at TIMESTAMP);"
    )
    con.executemany("INSERT INTO unlim_files (file_id, name, size) VALUES (?, ?, ?)",
                    [("/photounlim/a.HEIC", "a.HEIC", 5), ("/photounlim/v.MOV", "v.MOV", 5)])
    con.commit()
    con.close()
    monkeypatch.setattr(uw, "DB_FILE", str(path))
    monkeypatch.setattr(uw, "EXTRACT_DIR", str(tmp_path / "dl" / "_unlim"))
    monkeypatch.setattr(uw, "ZIP_TMP_DIR", str(tmp_path / "dl" / "_tmp"))
    monkeypatch.setattr(uw.time, "sleep", lambda s: None)
    monkeypatch.setattr(uw, "get_sk_uid", lambda s: ("sk", "uid"))
    monkeypatch.delenv("YADISK_PREVIEWS", raising=False)
    uw.ensure_attempts_column()
    return path


class _UnlimSession(_Http):
    """mpfs/url → ссылка на молчащий сервер; mpfs/info → размеры с превью."""

    def __init__(self):
        super().__init__()
        self.methods = []

    def post(self, url, params=None, json=None, timeout=None):
        method = json["apiMethod"]
        self.methods.append(method)
        path = json["requestParams"]["path"]
        name = path.rsplit("/", 1)[-1]
        if method == "mpfs/info":
            data = {"meta": {"sizes": [{"name": "ORIGINAL", "url": "//dl/x"},
                                       {"name": "XXXL", "url": f"//preview/{name}"}]}}
        else:
            data = {"file": f"//dl/{DEAD}/{name}"}
        return type("R", (), {"status_code": 200, "text": "", "headers": {}, "json": lambda self: data})()


def test_unlim_deferred_photo_gets_a_preview(tmp_path, unlim_db):
    w = uw.Worker(1, batch_size=10)
    w.session = _UnlimSession()
    w._process(uw.take_batch(1, size=10))

    preview = tmp_path / "dl" / PREVIEW_DIRNAME / "_unlim" / "a.HEIC.jpg"
    assert preview.read_bytes() == JPEG
    con = sqlite3.connect(unlim_db)
    rows = dict(con.execute("SELECT name, preview_path FROM unlim_files"))
    con.close()
    assert rows["a.HEIC"] == str(preview)
    assert rows["v.MOV"] is None  # у видео превью — только кадр, его не сохраняем
    assert w.session.methods.count("mpfs/info") == 1
    assert webui.db_stats(unlim_db, "unlim_files", "file_id")["previews"] == 1


def test_organize_never_sorts_previews_into_photos():
    assert PREVIEW_DIRNAME in organize.SKIP_DIRS


def test_waiting_subline_mentions_previews():
    db = webui._empty_db_stats()
    db.update(total=10, pending=8, deferred=8, downloaded=2, previews=5)
    st = {"running": True, "started_at": 1.0, "state": "running", "next_retry_at": 1800.0,
          "branches": {"disk": {"state": "waiting"}, "unlim": {"state": "off"}}}
    r = webui.build_rescue_status(st, db, webui._empty_db_stats(), {"ok": True}, {"ok": False},
                                  {"download_dir": "D:\\B"}, now=100.0)
    assert r["sources"]["disk"]["files"]["preview_only"] == 5
    assert "превью" in r["subline"]
    assert r["summary"]["preview_only_files"] == 5
