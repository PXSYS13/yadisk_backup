"""Скачивание по публичной ссылке: разбор ссылок, список файлов, раунды, лимит, ошибки."""

import hashlib
import os
import re
import sqlite3
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest
import requests

import public_download as pd
from state import DEFERRED_MARKER
from utils import EXIT_DEFERRED, EXIT_DISK_FULL, EXIT_OK, EXIT_PARTIAL, LINKS_DIRNAME, PREVIEW_DIRNAME

PROGRESS_RE = re.compile(r"Прогресс:\s*(\d+)\/(\d+)\s*файлов.*?ошибок\s*(\d+)")
LINK = "https://disk.yandex.ru/d/AbCd"
ALIVE = "s2nrg.storage.yandex.net"
DEAD = "s1klg.storage.yandex.net"


class Resp:
    def __init__(self, status=200, data=None, body=b"", headers=None):
        self.status_code = status
        self._data = data
        self._body = body
        self.headers = headers if headers is not None else (
            {"Content-Length": str(len(body)), "Content-Type": "application/octet-stream"} if body else {})

    def json(self):
        if self._data is None:
            raise ValueError("не JSON")
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)

    def iter_content(self, chunk_size):
        if self._body:
            yield self._body


class FakeYandex:
    """Публичная ссылка в памяти: путь файла → байты. Отвечает и как API, и как серверы хранения."""

    def __init__(self, files, name="Свадьба", single=False):
        self.files = dict(files)
        self.name = name
        self.single = single          # ссылка ведёт на один файл (корень — файл)
        self.dead = set()             # файлы на молчащем сервере хранения
        self.meta_errors = {}         # путь → (код, имя ошибки) для списка
        self.download_errors = {}     # путь → (код, имя ошибки) для ссылки на файл
        self.hrefs = {}               # путь → своя ссылка на файл
        self.md5_override = {}        # путь → MD5, который «сообщает» Яндекс
        self.calls = []

    def _md5(self, path):
        return self.md5_override.get(path) or hashlib.md5(self.files[path]).hexdigest()

    def _file(self, path):
        return {"type": "file", "name": path.rsplit("/", 1)[-1] or self.name, "path": path,
                "size": len(self.files[path]), "md5": self._md5(path)}

    def _dirs(self):
        out = {"/"}
        for p in self.files:
            parts = p.strip("/").split("/")[:-1]
            for i in range(1, len(parts) + 1):
                out.add("/" + "/".join(parts[:i]))
        return out

    def _children(self, path):
        prefix = path.rstrip("/") + "/"
        dirs = sorted(d for d in self._dirs() if d != path and d.startswith(prefix) and "/" not in d[len(prefix):])
        files = sorted(f for f in self.files if f.startswith(prefix) and "/" not in f[len(prefix):])
        return [{"type": "dir", "name": d.rsplit("/", 1)[-1], "path": d} for d in dirs] + [self._file(f) for f in files]

    @staticmethod
    def _error(code, name):
        return Resp(code, data={"error": name, "message": "x", "description": "x"})

    def get(self, url, params=None, timeout=None, stream=False, allow_redirects=True):
        params = params or {}
        if url == pd.API:
            path = params.get("path", "/")
            self.calls.append(("meta", path, params.get("offset", 0), params.get("preview_size")))
            if path in self.meta_errors:
                return self._error(*self.meta_errors[path])
            if params.get("preview_size"):
                return Resp(data={**self._file(path), "preview": f"https://downloader.disk.yandex.ru/preview?p={quote(path)}"})
            if self.single:
                return Resp(data={**self._file("/"), "name": self.name})
            if path != "/" and path not in self._dirs():
                return self._error(404, "DiskNotFoundError")
            kids = self._children(path)
            off, lim = int(params.get("offset", 0)), int(params.get("limit", 20))
            return Resp(data={"type": "dir", "name": self.name if path == "/" else path.rsplit("/", 1)[-1], "path": path,
                              "_embedded": {"items": kids[off:off + lim], "total": len(kids), "offset": off, "limit": lim}})
        if url == pd.API + "/download":
            path = params.get("path", "/")
            self.calls.append(("download", path))
            if path in self.download_errors:
                return self._error(*self.download_errors[path])
            return Resp(data={"href": self.hrefs.get(path, f"https://downloader.disk.yandex.ru/f?p={quote(path)}")})
        host = urlsplit(url).hostname
        self.calls.append(("get", host))
        path = unquote(parse_qs(urlsplit(url).query).get("p", [""])[0])
        if host == "downloader.disk.yandex.ru":
            if urlsplit(url).path == "/preview":  # превью отдаёт другой сервис — он живой
                return Resp(302, headers={"Location": f"https://{ALIVE}/preview?p={quote(path)}"})
            target = DEAD if path in self.dead else ALIVE
            return Resp(302, headers={"Location": f"https://{target}/f?p={quote(path)}"})
        if host == DEAD:
            raise requests.exceptions.ConnectTimeout("timed out")
        if host == ALIVE:
            if urlsplit(url).path == "/preview":
                return Resp(body=b"JPEGPREVIEW", headers={"Content-Length": "11", "Content-Type": "image/jpeg"})
            return Resp(body=self.files[path])
        raise AssertionError(f"неожиданный запрос к {host}")


@pytest.fixture()
def net(monkeypatch):
    """install(fake): подменяет сеть модуля и паузы."""
    def install(fake):
        monkeypatch.setattr(pd, "_thread_api", lambda: pd.PublicApi(fake))
        monkeypatch.setattr(pd, "_thread_http", lambda: fake)
        return fake
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    return install


@pytest.fixture()
def db():
    conn = pd.connect_db()
    yield conn
    conn.close()


def _add(conn, url=LINK, sub="/", name="Свадьба", kind="dir"):
    link_id, _new = pd.add_link(conn, url, sub, {"name": name, "type": kind})
    return link_id


def _files(conn):
    return {r["path"]: dict(r) for r in conn.execute("SELECT * FROM link_files")}


def _link(conn, link_id=1):
    return dict(conn.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone())


# ── Разбор ссылки ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,want", [
    ("https://disk.yandex.ru/d/AbC-1_x", ("https://disk.yandex.ru/d/AbC-1_x", "/")),
    ("disk.yandex.com/d/AbC/%D0%A4%D0%BE%D1%82%D0%BE/2024?x=1#y", ("https://disk.yandex.com/d/AbC", "/Фото/2024")),
    ("  https://yadi.sk/i/QWE  ", ("https://yadi.sk/i/QWE", "/")),
    ("https://yadi.sk/d/QWE/Sub", ("https://yadi.sk/d/QWE", "/Sub")),
    ("https://www.disk.yandex.ru/d/K", ("https://disk.yandex.ru/d/K", "/")),
    ("https://disk.360.yandex.ru/d/K", ("https://disk.360.yandex.ru/d/K", "/")),
    ("https://disk.yandex.ru/public/?hash=abc%3D", ("https://disk.yandex.ru/public/?hash=abc=", "/")),
])
def test_links_people_paste_are_understood(text, want):
    assert pd.normalize_public_url(text) == want


@pytest.mark.parametrize("text,word", [
    ("", "Вставьте"), ("https://example.com/d/x", "не ссылка Яндекс Диска"),
    ("https://disk.yandex.ru/client/disk/Фото", "собственного Диска"), ("https://disk.yandex.ru/a/xyz", "альбом"),
    ("https://disk.yandex.ru/d/", "/d/"),
])
def test_wrong_links_get_a_plain_explanation(text, word):
    with pytest.raises(ValueError, match=word):
        pd.normalize_public_url(text)


# ── Ошибки публичного API ───────────────────────────────────────────────────


@pytest.mark.parametrize("code,name,kind", [
    (404, "DiskNotFoundError", "not_found"), (410, "", "not_found"),
    (403, "DiskSymlinkPasswordRequiredError", "password"), (403, "DiskForbiddenError", "forbidden"),
    (429, "DiskResourceDownloadLimitExceededError", "limit"), (429, "TooManyRequestsError", "rate_limit"),
    (503, "", "server_50x"), (400, "BadRequest", "refused"),
])
def test_api_errors_become_plain_kinds(code, name, kind):
    err = pd._error_for(Resp(code, data={"error": name}))
    assert err.kind == kind and err.text and "Error" not in err.text
    assert err.link_level is (kind in ("not_found", "password", "forbidden"))


def test_api_transport_problems_are_named():
    class Raising:
        def __init__(self, exc):
            self.exc = exc

        def get(self, *a, **kw):
            raise self.exc

    with pytest.raises(pd.LinkError) as e:
        pd.PublicApi(Raising(requests.exceptions.ReadTimeout("t"))).meta(LINK)
    assert e.value.kind == "timeout"
    with pytest.raises(pd.LinkError) as e:
        pd.PublicApi(Raising(requests.exceptions.ConnectionError("c"))).meta(LINK)
    assert e.value.kind == "network"

    class NotJson:
        def get(self, *a, **kw):
            return Resp(200)

    with pytest.raises(pd.LinkError) as e:
        pd.PublicApi(NotJson()).meta(LINK)
    assert e.value.kind == "server_50x"


# ── Список файлов ───────────────────────────────────────────────────────────


def test_listing_walks_folders_page_by_page(db, net, monkeypatch):
    monkeypatch.setattr(pd, "LIST_LIMIT", 2)
    fake = net(FakeYandex({"/a.jpg": b"a", "/b.jpg": b"b", "/c.txt": b"c", "/Sub/d.jpg": b"d", "/Sub/Deep/e.pdf": b"e"}))
    link_id = _add(db)
    assert pd.list_link(pd._thread_api(), db, _link(db, link_id)) == 5
    assert set(_files(db)) == {"/a.jpg", "/b.jpg", "/c.txt", "/Sub/d.jpg", "/Sub/Deep/e.pdf"}
    root_pages = [c for c in fake.calls if c[0] == "meta" and c[1] == "/"]
    assert [c[2] for c in root_pages] == [0, 2]  # страницы по числу вернувшихся элементов
    link = _link(db, link_id)
    assert link["listed_at"] and link["gen"] == 1 and link["kind"] == "dir"


def test_relisting_marks_vanished_files_and_requeues_changed_ones(db, net):
    fake = net(FakeYandex({"/a.jpg": b"a", "/b.jpg": b"b", "/c.jpg": b"c"}))
    link_id = _add(db)
    pd.list_link(pd._thread_api(), db, _link(db, link_id))
    db.execute("UPDATE link_files SET status='downloaded' WHERE path IN ('/a.jpg', '/c.jpg')")
    fake.files["/a.jpg"] = b"new"   # владелец заменил файл
    del fake.files["/b.jpg"]        # и удалил другой
    pd.list_link(pd._thread_api(), db, _link(db, link_id))
    st = {p: r["status"] for p, r in _files(db).items()}
    assert st == {"/a.jpg": "pending", "/b.jpg": "gone", "/c.jpg": "downloaded"}


def test_link_folder_names_are_safe_unique_and_short(db):
    first = pd.link_folder_name(db, "Свадьба")
    db.execute("INSERT INTO links (url, public_url, local_dir) VALUES ('u1', 'u1', ?)", (first,))
    assert first == f"{LINKS_DIRNAME}/Свадьба"
    assert pd.link_folder_name(db, "свадьба") == f"{LINKS_DIRNAME}/свадьба (2)"
    assert pd.link_folder_name(db, "a:b?") == f"{LINKS_DIRNAME}/a_b_"
    assert len(pd.link_folder_name(db, "я" * 300).split("/", 1)[1]) <= 80
    assert pd.link_folder_name(db, " ") == f"{LINKS_DIRNAME}/Ссылка"


def test_adding_the_same_link_twice_keeps_one_row(db):
    assert pd.add_link(db, LINK, "/", {"name": "А", "type": "dir"}) == (1, True)
    assert pd.add_link(db, LINK, "/", None) == (1, False)
    pd.remove_link(db, 1, purge=True)
    assert pd.add_link(db, LINK, "/", None) == (1, True)  # убранную ссылку можно вернуть
    assert _link(db)["state"] == "active"


# ── Раунд скачивания ────────────────────────────────────────────────────────


def test_round_downloads_defers_silent_storage_and_reports_progress(db, net, tmp_path, capsys):
    fake = net(FakeYandex({"/a.jpg": b"photo-a", "/Sub/b.txt": b"text-b"}))
    fake.dead.add("/Sub/b.txt")
    _add(db)
    dl = str(tmp_path / "dl")
    assert pd.run(db, dl, workers=2) == EXIT_DEFERRED
    a = os.path.join(dl, LINKS_DIRNAME, "Свадьба", "a.jpg")
    assert open(a, "rb").read() == b"photo-a"
    rows = _files(db)
    assert rows["/a.jpg"]["status"] == "downloaded"
    assert rows["/Sub/b.txt"]["status"] == "pending"
    assert rows["/Sub/b.txt"]["error"].startswith(DEFERRED_MARKER) and rows["/Sub/b.txt"]["storage_host"] == DEAD
    out = capsys.readouterr().out
    m = PROGRESS_RE.search([ln for ln in out.splitlines() if ln.startswith("Прогресс:")][-1])
    assert m and (m.group(1), m.group(2), m.group(3)) == ("2", "2", "0")
    assert "отложено 1" in out and "storage_down | Отложено 1" in out
    assert "https://" not in out  # адреса ссылок в журнал не попадают
    # Следующий запуск: сервер ожил — докачивается только отложенный файл
    fake.dead.clear()
    fake.calls.clear()
    assert pd.run(db, dl, workers=2) == EXIT_OK
    assert [c for c in fake.calls if c[0] == "download"] == [("download", "/Sub/b.txt")]
    assert open(os.path.join(dl, LINKS_DIRNAME, "Свадьба", "Sub", "b.txt"), "rb").read() == b"text-b"


def test_wrong_md5_is_an_error_not_a_download(db, net, tmp_path):
    fake = net(FakeYandex({"/a.jpg": b"photo"}))
    fake.md5_override["/a.jpg"] = "0" * 32
    _add(db)
    assert pd.run(db, str(tmp_path / "dl"), workers=1) == EXIT_PARTIAL
    row = _files(db)["/a.jpg"]
    assert row["status"] == "failed" and row["error"].startswith("bad_file |")


def test_download_limit_pauses_the_link_without_burning_attempts(db, net, tmp_path, capsys):
    fake = net(FakeYandex({f"/{i}.jpg": b"x" for i in range(5)}))
    for i in range(5):
        fake.download_errors[f"/{i}.jpg"] = (429, "DiskResourceDownloadLimitExceededError")
    _add(db)
    assert pd.run(db, str(tmp_path / "dl"), workers=1) == EXIT_DEFERRED
    link = _link(db)
    assert link["limit_until"] and link["limit_until"] == pytest.approx(pd.next_limit_reset(link["limit_until"] - 60), abs=3600 * 24)
    rows = _files(db).values()
    assert all(r["status"] == "pending" and r["attempts"] == 0 for r in rows)
    # После трёх отказов подряд остальные файлы ссылки не трогаем (кроме уже отправленных в работу)
    assert pd.LIMIT_STREAK <= len([c for c in fake.calls if c[0] == "download"]) < 5
    assert "link_limit |" in capsys.readouterr().out


def test_next_limit_reset_is_ten_past_midnight_moscow():
    # 1 января 2027, 23:59 МСК → 2 января 00:10 МСК
    ts = 1798750740  # 2027-01-01 20:59 UTC
    assert pd.next_limit_reset(ts) - ts == 11 * 60


@pytest.mark.parametrize("code,name,kind", [(404, "DiskNotFoundError", "not_found"),
                                            (403, "DiskSymlinkPasswordRequiredError", "password")])
def test_broken_link_stops_without_file_requests(db, net, tmp_path, code, name, kind):
    fake = net(FakeYandex({"/a.jpg": b"a"}))
    fake.meta_errors["/"] = (code, name)
    _add(db)
    assert pd.run(db, str(tmp_path / "dl"), workers=1) == EXIT_PARTIAL
    assert _link(db)["error_kind"] == kind
    assert not [c for c in fake.calls if c[0] in ("download", "get")]


def test_foreign_download_host_is_refused_without_a_request(db, net, tmp_path):
    fake = net(FakeYandex({"/a.jpg": b"a"}))
    fake.hrefs["/a.jpg"] = "https://evil.example/a.jpg"
    _add(db)
    pd.run(db, str(tmp_path / "dl"), workers=1)
    assert _files(db)["/a.jpg"]["status"] == "failed"
    assert ("get", "evil.example") not in fake.calls


def test_single_file_link_lands_directly_in_the_links_folder(db, net, tmp_path):
    net(FakeYandex({"/": b"pdf-bytes"}, name="Договор.pdf", single=True))
    pd.add_link(db, "https://disk.yandex.ru/i/F1", "/", {"name": "Договор.pdf", "type": "file"})
    dl = str(tmp_path / "dl")
    assert pd.run(db, dl, workers=1) == EXIT_OK
    assert open(os.path.join(dl, LINKS_DIRNAME, "Договор.pdf"), "rb").read() == b"pdf-bytes"


def test_deferred_photo_gets_a_safety_preview(db, net, tmp_path):
    fake = net(FakeYandex({"/Свадьба/1.jpg": b"orig"}))
    fake.dead.add("/Свадьба/1.jpg")
    _add(db)
    dl = str(tmp_path / "dl")
    pd.run(db, dl, workers=1)
    row = _files(db)["/Свадьба/1.jpg"]
    want = os.path.join(dl, PREVIEW_DIRNAME, LINKS_DIRNAME, "Свадьба", "Свадьба", "1.jpg.jpg")
    assert row["preview_path"] == want and open(want, "rb").read() == b"JPEGPREVIEW"


def test_removed_link_is_skipped_and_forgotten(db, net, tmp_path):
    fake = net(FakeYandex({"/a.jpg": b"a"}))
    keep = _add(db)
    gone = _add(db, url="https://disk.yandex.ru/d/Other", name="Другая")
    pd.list_link(pd._thread_api(), db, _link(db, gone))
    pd.remove_link(db, gone, purge=False)  # скачивание шло — забудем файлы после раунда
    pd.run(db, str(tmp_path / "dl"), workers=1)
    assert {r["link_id"] for r in _files(db).values()} == {keep}
    assert ("download", "/a.jpg") in fake.calls


def test_full_local_disk_stops_the_round_with_code_4(db, net, tmp_path):
    net(FakeYandex({f"/{i}.jpg": b"x" for i in range(12)}))
    _add(db)
    dl = tmp_path / "dl"
    (dl / LINKS_DIRNAME).mkdir(parents=True)
    (dl / LINKS_DIRNAME / "Свадьба").write_text("файл на месте папки")  # папку не создать
    assert pd.run(db, str(dl), workers=1) == EXIT_DISK_FULL


def test_unwritable_download_folder_exits_4_with_a_plain_line(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("x")
    monkeypatch.setenv("DOWNLOAD_DIR", str(blocker))
    assert pd.main([]) == EXIT_DISK_FULL
    assert "local_disk |" in capsys.readouterr().out


def test_wait_mode_retries_when_woken_and_finishes(db, net, tmp_path, monkeypatch):
    fake = net(FakeYandex({"/a.jpg": b"a", "/b.jpg": b"b"}))
    fake.dead.add("/b.jpg")
    _add(db)
    other = sqlite3.connect(pd.DB_FILE, isolation_level=None)

    def sleep(_s):
        fake.dead.clear()  # сервер ожил, человек нажал «Попробовать сейчас»
        other.execute("INSERT OR REPLACE INTO links_meta (key, value) VALUES ('wake', '1')")

    monkeypatch.setattr(pd.time, "sleep", sleep)
    try:
        assert pd.run(db, str(tmp_path / "dl"), workers=1, wait_mode=True) == EXIT_OK
    finally:
        other.close()
    assert {r["status"] for r in _files(db).values()} == {"downloaded"}
    assert pd.meta_get(db, "next_retry_at") is None


def test_status_command_lists_links_without_addresses(db, capsys):
    _add(db)
    assert pd.cmd_status(db) == EXIT_OK
    out = capsys.readouterr().out
    assert "#1 «Свадьба»" in out and "https://" not in out
