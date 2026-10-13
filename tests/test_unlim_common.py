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
