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
