"""Тесты общего модуля безлимита unlim_common."""

import json
import sqlite3
import pytest
import unlim_common as uc


def test_make_cid():
    cid = uc.make_cid("12345")
    assert cid.startswith("12345")
    assert len(cid) > len("12345")


def test_load_cookies_valid(tmp_path):
    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text(
        json.dumps([
            {"name": "Session_id", "value": "xyz"},
            {"name": "yandexuid", "value": "123"},
            {"name": "empty", "value": None},
            {"name": "empty_str", "value": ""},
        ]),
        encoding="utf-8",
    )
    loaded = uc.load_cookies(str(cookies_path))
    assert loaded == {"Session_id": "xyz", "yandexuid": "123"}


def test_load_cookies_missing(tmp_path):
    with pytest.raises(SystemExit):
        uc.load_cookies(str(tmp_path / "nonexistent.json"))


def test_load_cookies_invalid_format(tmp_path):
    cookies_path = tmp_path / "bad.json"
    cookies_path.write_text(json.dumps({"name": "val"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        uc.load_cookies(str(cookies_path))


def test_load_cookies_malformed_entries(tmp_path):
    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text(
        json.dumps([
            "not a dict",
            12345,
            None,
            {"wrong_key": "val"},
            {"name": 999, "value": "abc"},
            {"name": "Session_id", "value": "valid_session"},
            {"name": "yandexuid", "value": 123},  # non-string value
        ]),
        encoding="utf-8",
    )
    loaded = uc.load_cookies(str(cookies_path))
    assert loaded == {"Session_id": "valid_session"}



def test_ensure_attempts_column(tmp_path):
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, name TEXT)"
    )
    con.commit()
    con.close()

    uc.ensure_attempts_column(db_path)

    con = sqlite3.connect(db_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(unlim_files)")}
    con.close()
    assert "attempts" in cols


def test_make_session(tmp_path):
    cookies = {"Session_id": "test_session", "yandexuid": "999"}
    session = uc.make_session(cookies=cookies)
    assert session.headers["User-Agent"] == uc.UA
    assert session.headers["Origin"] == uc.BASE
    assert session.cookies.get("Session_id", domain=".yandex.ru") == "test_session"


def test_get_sk_uid_auth_expired():
    class FakeResponse:
        url = "https://passport.yandex.ru/auth"
        status_code = 302
        text = "redirect to login"

    class FakeSession:
        def get(self, url, timeout=30):
            return FakeResponse()

    with pytest.raises(uc.AuthExpiredError):
        uc.get_sk_uid(FakeSession())


def test_get_sk_uid_success():
    class FakeResponse:
        url = "https://disk.yandex.ru/client/disk"
        status_code = 200
        text = '<html><script>{"sk":"my_test_sk","uid":"12345678"}</script></html>'

    class FakeSession:
        def get(self, url, timeout=30):
            return FakeResponse()

    sk, uid = uc.get_sk_uid(FakeSession())
    assert sk == "my_test_sk"
    assert uid == "12345678"


def test_get_all_clusters_pagination(monkeypatch):
    import unlim_download as ud

    calls = []

    def fake_api_call(s, sk, uid, method, params=None, retries=3):
        calls.append(params.copy())
        offset = params.get("offset", 0)
        if offset == 0:
            return {"items": [{"id": "c1"}, {"id": "c2"}]}
        elif offset == 2:
            return {"items": [{"id": "c3"}]}
        return {"items": []}

    monkeypatch.setattr(ud, "api_call", fake_api_call)
    clusters = ud.get_all_clusters(None, "sk", "uid", "psid", batch_size=2)
    assert len(clusters) == 3
    assert [c["id"] for c in clusters] == ["c1", "c2", "c3"]
    assert len(calls) == 2


@pytest.fixture()
def unlim_db(tmp_path, monkeypatch):
    import unlim_download as ud
    monkeypatch.setattr(ud, "DB_FILE", str(tmp_path / "unlim_state.db"))
    monkeypatch.setattr(ud, "_tls", __import__("threading").local())
    ud.db_init()
    return ud


def _unlim_item(name, size):
    return {"type": "file", "name": name, "path": f"/photounlim/{name}",
            "meta": {"size": size, "md5": "a" * 32, "mimetype": "image/jpeg"}}


def test_collect_via_list_pages_through_photounlim(unlim_db, monkeypatch, capsys):
    ud = unlim_db
    monkeypatch.setattr(ud, "LIST_PAGE", 2)
    root = {"type": "dir", "path": "/photounlim", "id": "/photounlim/"}
    pages = {0: [root, _unlim_item("a.jpg", 10), _unlim_item("b.jpg", 20)],
             2: [root, _unlim_item("c.jpg", 30)]}
    calls = []

    def fake_api_call(s, sk, uid, method, params=None, retries=3):
        assert method == "mpfs/list" and params["path"] == "/photounlim" and "size" in params["meta"]
        calls.append(params["offset"])
        return pages[params["offset"]]

    monkeypatch.setattr(ud, "api_call", fake_api_call)
    assert ud.collect_via_list(None, "sk", "uid") == 3
    assert calls == [0, 2]
    rows = ud.db().execute("SELECT file_id, name, size, md5, status FROM unlim_files ORDER BY name").fetchall()
    assert [tuple(r) for r in rows] == [
        ("/photounlim/a.jpg", "a.jpg", 10, "a" * 32, "pending"),
        ("/photounlim/b.jpg", "b.jpg", 20, "a" * 32, "pending"),
        ("/photounlim/c.jpg", "c.jpg", 30, "a" * 32, "pending"),
    ]
    assert "Найдено: 3 файлов" in capsys.readouterr().out  # строку читает Web UI


def test_save_listed_files_does_not_duplicate_photoslice_rows(unlim_db):
    ud = unlim_db
    ud.db().execute("INSERT INTO unlim_files (file_id, name, size, status) "
                    "VALUES ('photounlim:/a.jpg', 'a.jpg', 0, 'downloaded')")
    assert ud.save_listed_files([_unlim_item("a.jpg", 10), _unlim_item("b.jpg", 20)]) == 1
    rows = {r["file_id"]: (r["size"], r["md5"], r["status"]) for r in ud.db().execute("SELECT * FROM unlim_files")}
    assert rows["photounlim:/a.jpg"] == (10, "a" * 32, "downloaded")  # дополнен, не задублирован
    assert rows["/photounlim/b.jpg"][2] == "pending"
    assert len(rows) == 2


def test_reset_failed_files_migrates_a_database_from_an_old_version(tmp_path):
    """Web UI сбрасывает ошибки до сбора списка: база без attempts не должна ронять его."""
    db_path = str(tmp_path / "unlim_state.db")
    con = sqlite3.connect(db_path)
    con.executescript(
        "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, name TEXT, size INTEGER, md5 TEXT,"
        " status TEXT DEFAULT 'pending', local_path TEXT, error TEXT, worker_id INTEGER,"
        " assigned_at TIMESTAMP, downloaded_at TIMESTAMP);"
        "INSERT INTO unlim_files (file_id, status, error) VALUES ('1', 'failed', 'boom'), ('2', 'pending', NULL);"
    )
    con.close()

    assert uc.reset_failed_files(db_path) == 1

    con = sqlite3.connect(db_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(unlim_files)")}
    rows = con.execute("SELECT file_id, status, attempts, error FROM unlim_files ORDER BY file_id").fetchall()
    con.close()
    assert {"attempts", "storage_host", "preview_path"} <= cols
    assert rows == [("1", "pending", 0, None), ("2", "pending", 0, None)]


def test_reset_failed_files_without_a_file_list(tmp_path):
    """База есть, а таблицы файлов нет (воркер запускали до сбора списка) — сбрасывать нечего."""
    db_path = str(tmp_path / "unlim_state.db")
    uc.ensure_unlim_schema(db_path)  # так её создаёт воркер: только unlim_meta
    assert uc.reset_failed_files(db_path, include_in_progress=True) == 0
