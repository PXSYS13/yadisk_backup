"""Страховочные превью: уменьшенная копия фото, пока оригинал лежит на молчащем сервере."""

import os
import sqlite3
from pathlib import Path

import pytest
import requests

import downloader
import organize
import storage_fetch as sf
import unlim_worker as uw
import utils
import webapp.queue_db
import webapp.rescue_view
from state import CATEGORY_SKIP_MARKER, State
from unlim_common import clear_dead_hosts
from utils import IS_WINDOWS, PREVIEW_DIRNAME, keep_awake, preview_path_for, previews_enabled, remove_preview

JPEG = b"\xff\xd8\xff\xe0" + b"preview" * 10
ORIGINAL = b"HEIC!"  # оригинал фото: 5 байт, как size в очереди
DEAD = "s1klg.storage.yandex.net"
LIVE = "s2nrg.storage.yandex.net"


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
    """https://dl/<host>/<x> → 302 → host; https://preview/<x> → JPEG; LIVE — оригинал; DEAD — тишина."""

    def __init__(self):
        self.hits = []

    def get(self, url, stream=False, timeout=None, allow_redirects=True):
        host = url.split("/")[2]
        self.hits.append(host)
        if host == "dl":
            return _Stream(status=302, headers={"Location": "https://" + url.split("/", 3)[3]})
        if host == "preview":
            return _Stream(JPEG)
        if host == LIVE:
            return _Stream(ORIGINAL, headers={"Content-Type": "application/octet-stream",
                                              "Content-Length": str(len(ORIGINAL))})
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

    def __init__(self, host=DEAD):
        self.host = host  # сервер хранения, на который ведут ссылки на файлы
        self.meta_calls = []

    def get_download_link(self, remote):
        return f"https://dl/{self.host}/{remote.split('/')[-1]}"

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
    assert Path(res.preview_path).read_bytes() == JPEG

    st = State(str(tmp_path / "state.db"))
    st.mark_pending_batch([("disk:/Фото/a.HEIC", 5, None)])
    st.mark_deferred("disk:/Фото/a.HEIC", res.storage_host, res.error, preview_path=res.preview_path)
    assert st._conn().execute("SELECT preview_path FROM files").fetchone()[0] == res.preview_path
    st.close()
    assert webapp.queue_db.db_stats(tmp_path / "state.db")["previews"] == 1


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
    """mpfs/url → ссылка на сервер host (по умолчанию молчащий); mpfs/info → размеры с превью."""

    def __init__(self, host=DEAD):
        super().__init__()
        self.host = host
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
            data = {"file": f"//dl/{self.host}/{name}"}
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
    assert webapp.queue_db.db_stats(unlim_db, "unlim_files", "file_id")["previews"] == 1


def test_organize_never_sorts_previews_into_photos():
    assert PREVIEW_DIRNAME in organize.SKIP_DIRS


def test_waiting_subline_mentions_previews():
    db = webapp.queue_db.empty_db_stats()
    db.update(total=10, pending=8, deferred=8, downloaded=2, previews=5)
    st = {"running": True, "started_at": 1.0, "state": "running", "next_retry_at": 1800.0,
          "branches": {"disk": {"state": "waiting"}, "unlim": {"state": "off"}}}
    r = webapp.rescue_view.build_rescue_status(st, db, webapp.queue_db.empty_db_stats(), {"ok": True}, {"ok": False},
                                  {"download_dir": "D:\\B"}, now=100.0)
    assert r["sources"]["disk"]["files"]["preview_only"] == 5
    assert "превью" in r["subline"]
    assert r["summary"]["preview_only_files"] == 5


# ── Оригинал на месте — превью убирается ────────────────────────────────────
# Люди принимали оставшееся превью (до 1280 px) за «испорченный» оригинал


def _put(path, data=JPEG):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return str(path)


def test_remove_preview_deletes_the_file_and_emptied_folders_only(tmp_path):
    dl = str(tmp_path / "dl")
    root = os.path.join(dl, PREVIEW_DIRNAME)
    one = _put(os.path.join(root, "Фото", "2024", "a.HEIC.jpg"))
    two = _put(os.path.join(root, "Фото", "b.jpg.jpg"))
    assert remove_preview(one, dl)
    assert not os.path.exists(one)
    assert not os.path.exists(os.path.join(root, "Фото", "2024"))  # опустевшая подпапка убрана
    assert os.path.exists(two)  # соседнее превью и его папка на месте
    assert remove_preview(two, dl)
    assert os.path.isdir(root) and os.listdir(root) == []  # саму «_превью» не трогаем
    # Превью уже нет (удалили руками) или его не было — запись в базе можно забыть
    assert remove_preview(one, dl) and remove_preview(None, dl) and remove_preview("", dl)


@pytest.mark.parametrize("rel", [
    "Фото/a.jpg",                              # оригинал, а не превью
    f"{PREVIEW_DIRNAME}/../Фото/b.jpg.jpg",    # выход из «_превью» через ..
    f"{PREVIEW_DIRNAME}2/c.jpg.jpg",           # похожая папка рядом
    f"{PREVIEW_DIRNAME}/notes.txt",            # в «_превью», но не превью
])
def test_remove_preview_never_touches_other_files(tmp_path, rel):
    dl = str(tmp_path / "dl")
    path = _put(os.path.join(dl, *rel.split("/")))
    assert remove_preview(path, dl) is False
    assert os.path.exists(path)


def test_remove_preview_handles_long_paths(tmp_path):
    # Глубокие папки Диска: путь превью длиннее 260 символов, пишется через to_long_path
    dl = str(tmp_path / "dl")
    deep = os.path.join(dl, PREVIEW_DIRNAME, *[f"папка_с_длинным_именем_{i}" for i in range(12)])
    path = os.path.join(deep, "a.jpg.jpg")
    assert len(path) > 300
    _put(utils.to_long_path(path))
    assert remove_preview(path, dl)
    assert not os.path.exists(utils.to_long_path(path))
    assert os.listdir(os.path.join(dl, PREVIEW_DIRNAME)) == []


def test_remove_preview_leaves_folders_alone(tmp_path):
    dl = str(tmp_path / "dl")
    folder = os.path.join(dl, PREVIEW_DIRNAME, "x.jpg")
    os.makedirs(os.path.join(folder, "inner"))
    assert remove_preview(folder, dl) is False
    assert os.path.isdir(os.path.join(folder, "inner"))


def _dir_link(link, target):
    """Ссылка на папку: symlink, а на Windows без прав на него — junction."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if not IS_WINDOWS:
            raise
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


def test_remove_preview_does_not_follow_a_link_out_of_previews(tmp_path):
    dl = tmp_path / "dl"
    outside = tmp_path / "Мои фото"
    victim = _put(str(outside / "a.jpg.jpg"))
    (dl / PREVIEW_DIRNAME).mkdir(parents=True)
    link = dl / PREVIEW_DIRNAME / "Фото"
    try:
        _dir_link(link, outside)
    except (OSError, ImportError, AttributeError) as e:
        pytest.skip(f"ссылку на папку не создать: {e}")
    try:
        assert remove_preview(str(link / "a.jpg.jpg"), str(dl)) is False
        assert os.path.exists(victim)
    finally:
        try:
            os.rmdir(link)  # junction и symlink на папку в Windows
        except OSError:
            os.unlink(link)


def test_remove_preview_survives_a_locked_file(tmp_path, monkeypatch):
    dl = str(tmp_path / "dl")
    path = _put(os.path.join(dl, PREVIEW_DIRNAME, "a.jpg.jpg"))

    def locked(p):
        raise PermissionError(13, "файл открыт в программе просмотра", p)

    monkeypatch.setattr(utils.os, "remove", locked)
    assert remove_preview(path, dl) is False
    assert os.path.exists(path)


def _disk_state(tmp_path):
    st = State(str(tmp_path / "state.db"))
    st.mark_pending_batch([("disk:/Фото/2024/a.HEIC", len(ORIGINAL), None)])
    return st


def _disk_row(st):
    return tuple(st._conn().execute("SELECT status, preview_path FROM files").fetchone())


def test_disk_preview_goes_away_when_the_original_arrives(tmp_path, disk_net, monkeypatch):
    cli = _DiskClient()
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: cli)
    dl = str(tmp_path / "dl")
    st = _disk_state(tmp_path)
    assert downloader.download_all("y0_test", st, dl, workers=1)["deferred"] == 1
    preview = os.path.join(dl, PREVIEW_DIRNAME, "Фото", "2024", "a.HEIC.jpg")
    assert Path(preview).read_bytes() == JPEG
    assert _disk_row(st) == ("pending", preview)

    # Сервер хранения ожил: оригинал скачан, превью рядом больше не лежит
    downloader.DEAD_HOSTS.clear()
    cli.host = LIVE
    assert downloader.download_all("y0_test", st, dl, workers=1)["downloaded"] == 1
    assert Path(os.path.join(dl, "Фото", "2024", "a.HEIC")).read_bytes() == ORIGINAL
    assert not os.path.exists(preview)
    assert not os.path.exists(os.path.join(dl, PREVIEW_DIRNAME, "Фото"))  # опустевшие подпапки убраны
    assert os.path.isdir(os.path.join(dl, PREVIEW_DIRNAME))
    assert _disk_row(st) == ("downloaded", None)
    st.close()
    assert webapp.queue_db.db_stats(tmp_path / "state.db")["previews"] == 0


def test_disk_preview_goes_away_when_the_original_is_already_in_the_folder(tmp_path, disk_net, monkeypatch):
    # Докачка: оригинал уже лежит в папке — файл помечается пропущенным, превью убирается
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: _DiskClient(LIVE))
    dl = str(tmp_path / "dl")
    st = _disk_state(tmp_path)
    original = _put(os.path.join(dl, "Фото", "2024", "a.HEIC"), ORIGINAL)
    preview = _put(preview_path_for(dl, original))
    st.mark_deferred("disk:/Фото/2024/a.HEIC", DEAD, "сервер молчит", preview_path=preview)
    assert downloader.download_all("y0_test", st, dl, workers=1)["skipped"] == 1
    assert not os.path.exists(preview) and Path(original).read_bytes() == ORIGINAL
    assert _disk_row(st) == ("skipped", None)
    st.close()


def test_disk_old_previews_of_saved_photos_are_swept(tmp_path, disk_net):
    """Превью, оставшиеся от прошлых версий, убираются при следующем запуске скачивания.

    У фото, снятых выбором категорий, оригинала в папке нет — их превью остаются.
    """
    dl = str(tmp_path / "dl")
    st = State(str(tmp_path / "state.db"))
    rows = {"disk:/a.jpg": ("downloaded", None), "disk:/b.jpg": ("skipped", None),
            "disk:/c.jpg": ("skipped", CATEGORY_SKIP_MARKER), "disk:/d.jpg": ("downloaded", None)}
    st.mark_pending_batch([(rp, 5, None) for rp in rows])
    paths = {rp: _put(preview_path_for(dl, os.path.join(dl, rp[6:]))) for rp in rows}
    paths["disk:/d.jpg"] = _put(str(tmp_path / "Другая папка" / "d.jpg.jpg"))  # не в «_превью» — чужой файл
    for rp, (status, error) in rows.items():
        st._conn().execute("UPDATE files SET status=?, error=?, preview_path=? WHERE remote_path=?",
                           (status, error, paths[rp], rp))
    downloader.download_all("y0_test", st, dl, workers=1)  # очередь пуста — только уборка
    left = dict(st._conn().execute("SELECT remote_path, preview_path FROM files"))
    st.close()
    assert left == {"disk:/a.jpg": None, "disk:/b.jpg": None,
                    "disk:/c.jpg": paths["disk:/c.jpg"], "disk:/d.jpg": paths["disk:/d.jpg"]}
    assert [rp for rp in rows if os.path.exists(paths[rp])] == ["disk:/c.jpg", "disk:/d.jpg"]


def test_disk_preview_cleanup_errors_never_break_the_download(tmp_path, disk_net, monkeypatch):
    cli = _DiskClient()
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: cli)
    dl = str(tmp_path / "dl")
    st = _disk_state(tmp_path)
    downloader.download_all("y0_test", st, dl, workers=1)
    preview = os.path.join(dl, PREVIEW_DIRNAME, "Фото", "2024", "a.HEIC.jpg")

    def boom(*a):
        raise RuntimeError("неожиданная ошибка уборки")

    monkeypatch.setattr(downloader, "remove_preview", boom)
    downloader.DEAD_HOSTS.clear()
    cli.host = LIVE
    assert downloader.download_all("y0_test", st, dl, workers=1)["downloaded"] == 1
    assert os.path.exists(os.path.join(dl, "Фото", "2024", "a.HEIC"))
    assert _disk_row(st) == ("downloaded", preview)  # запись осталась — уберём в следующий раз
    monkeypatch.setattr(downloader, "remove_preview", remove_preview)
    downloader.download_all("y0_test", st, dl, workers=1)
    assert _disk_row(st) == ("downloaded", None) and not os.path.exists(preview)
    st.close()


def _unlim_rows(db):
    con = sqlite3.connect(db)
    try:
        return {name: (status, pv) for name, status, pv in
                con.execute("SELECT name, status, preview_path FROM unlim_files")}
    finally:
        con.close()


def test_unlim_preview_goes_away_when_the_original_arrives(tmp_path, unlim_db):
    w = uw.Worker(1, batch_size=10)
    w.session = _UnlimSession()
    w._process(uw.take_batch(1, size=10))
    preview = tmp_path / "dl" / PREVIEW_DIRNAME / "_unlim" / "a.HEIC.jpg"
    assert preview.read_bytes() == JPEG

    # Новый раунд забывает молчащие серверы; сервер ожил — оригинал скачан, превью убрано
    clear_dead_hosts(str(unlim_db))
    w.session = _UnlimSession(LIVE)
    w._process(uw.take_batch(1, size=10))
    assert (tmp_path / "dl" / "_unlim" / "a.HEIC").read_bytes() == ORIGINAL
    assert not preview.exists() and not preview.parent.exists()
    assert (tmp_path / "dl" / PREVIEW_DIRNAME).is_dir()
    assert _unlim_rows(unlim_db)["a.HEIC"] == ("downloaded", None)


def test_unlim_old_previews_are_swept_at_worker_start(tmp_path, unlim_db):
    old = _put(str(tmp_path / "dl" / PREVIEW_DIRNAME / "_unlim" / "a.HEIC.jpg"))
    foreign = _put(str(tmp_path / "Другая папка" / "v.MOV.jpg"))  # не в «_превью» — не трогаем
    con = sqlite3.connect(unlim_db)
    con.executemany("UPDATE unlim_files SET status='downloaded', preview_path=? WHERE name=?",
                    [(old, "a.HEIC"), (foreign, "v.MOV")])
    con.commit()
    con.close()
    assert uw.sweep_previews() == 1
    assert not os.path.exists(old) and os.path.exists(foreign)
    assert _unlim_rows(unlim_db) == {"a.HEIC": ("downloaded", None), "v.MOV": ("downloaded", foreign)}


def test_missing_preview_is_not_logged_as_a_refusal(tmp_path, disk_net, monkeypatch, caplog):
    """HTTP 434 на превью — у Яндекса нет копии. Строка «refused | …» включала баннер
    «Яндекс отказывает в скачивании безлимита» у человека, который качал только Диск."""
    import logging

    class NoPreview(_Http):
        def get(self, url, stream=False, timeout=None, allow_redirects=True):
            if url.split("/")[2] == "preview":
                return _Stream(status=434, headers={"Content-Type": "text/plain"})
            return super().get(url, stream, timeout, allow_redirects)

    http = NoPreview()
    monkeypatch.setattr(downloader, "_thread_preview_http", lambda token: http)
    with caplog.at_level(logging.INFO, logger="yadisk_backup"):
        res = downloader._download_one(downloader.DownloadJob("disk:/Фото/a.HEIC", 5, None), _DiskClient(), str(tmp_path))
    assert res.status == "deferred" and not res.preview_path
    lines = [r for r in caplog.records if "превью" in r.getMessage()]
    assert lines and all(r.levelno == logging.INFO for r in lines)
    assert all("refused |" not in r.getMessage() for r in lines)
    assert "нет уменьшенной копии" in lines[0].getMessage()


def test_preview_failure_text_drops_the_category_label():
    missing, text = sf.preview_failure_text(sf.FetchFailure("refused", "HTTP 434 при скачивании файла"))
    assert missing and "|" not in text
    missing, text = sf.preview_failure_text(sf.FetchFailure("server_50x", "HTTP 500 при скачивании файла"))
    assert not missing and "server_50x" not in text and "HTTP 500" in text
