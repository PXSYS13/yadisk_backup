"""Коды выхода говорят правду: «доступ устарел» — только когда он и правда устарел.

Во время сбоев Яндекса проверки токена и кук падают по сети или с 50x. Раньше это
превращалось в код 2 «обновите токен/куки», и люди зря перевыпускали доступ.
"""

import argparse

import pytest
import requests
import yadisk

import downloader
import main
import unlim_common as uc
import unlim_download as ud
import unlim_worker as uw
import webui
from utils import EXIT_AUTH, EXIT_OUTAGE


class _Client:
    def __init__(self, check):
        self._check = check

    def check_token(self):
        if isinstance(self._check, Exception):
            raise self._check
        return self._check

    def close(self):
        pass


@pytest.mark.parametrize(
    "check,state",
    [
        (False, "invalid"),
        (yadisk.exceptions.UnauthorizedError("401"), "invalid"),
        (requests.exceptions.ConnectionError("no route"), "unreachable"),
        (yadisk.exceptions.InternalServerError("500"), "unreachable"),
    ],
)
def test_token_state_tells_invalid_from_unreachable(monkeypatch, check, state):
    monkeypatch.setattr(downloader, "make_client", lambda token: _Client(check))
    got, msg = downloader.token_state("y0_real-looking-token")
    assert got == state
    assert msg


@pytest.mark.parametrize("state,code", [("invalid", EXIT_AUTH), ("unreachable", EXIT_OUTAGE)])
def test_main_maps_token_state_to_exit_code(monkeypatch, state, code):
    monkeypatch.setattr(main, "token_state", lambda token: (state, "msg"))
    assert main._require_token({"token": "t"}) == code
    assert main.cmd_check({"token": "t"}, argparse.Namespace()) == code


def test_webui_marks_unreachable_token_check(monkeypatch):
    monkeypatch.setattr(webui, "make_client", lambda token: _Client(requests.exceptions.ReadTimeout("slow")))
    monkeypatch.setitem(webui._token_check_cache, "token", None)
    data = webui.check_token("y0_unique-token-for-this-test")
    assert data["ok"] is False
    assert data.get("unreachable") is True


class _Resp:
    def __init__(self, status, url="https://disk.yandex.ru/client/disk", text=""):
        self.status_code = status
        self.url = url
        self.text = text


class _Sess:
    def __init__(self, answer):
        self.answer = answer

    def get(self, url, timeout=30):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize(
    "answer,error",
    [
        (_Resp(503), uc.YandexUnavailableError),
        (_Resp(429), uc.YandexUnavailableError),
        (requests.exceptions.ConnectTimeout("timed out"), uc.YandexUnavailableError),
        (_Resp(401), uc.AuthExpiredError),
        (_Resp(200, url="https://passport.yandex.ru/auth"), uc.AuthExpiredError),
    ],
)
def test_get_sk_uid_separates_outage_from_expired_cookies(answer, error):
    with pytest.raises(error):
        uc.get_sk_uid(_Sess(answer))


def test_worker_exits_3_when_yandex_is_down_at_start(monkeypatch):
    def down(_s):
        raise uc.YandexUnavailableError("HTTP 503")

    monkeypatch.setattr(uw, "get_sk_uid", down)
    monkeypatch.setattr(uw, "make_session", lambda: _Sess(_Resp(200)))
    with pytest.raises(SystemExit) as e:
        uw.Worker(1).authorize()
    assert e.value.code == EXIT_OUTAGE


def test_refused_403_with_live_session_never_prints_auth_expired(tmp_path, monkeypatch, capsys):
    import sqlite3
    db = tmp_path / "unlim_state.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, name TEXT, size INTEGER, md5 TEXT,"
        " status TEXT DEFAULT 'pending', local_path TEXT, error TEXT, worker_id INTEGER,"
        " assigned_at TIMESTAMP, downloaded_at TIMESTAMP, attempts INTEGER DEFAULT 0);"
        "INSERT INTO unlim_files (file_id, name, size) VALUES ('1', 'a.jpg', 10);"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(uw, "DB_FILE", str(db))
    monkeypatch.setattr(uw.time, "sleep", lambda s: None)
    monkeypatch.setattr(uw, "get_sk_uid", lambda s: ("sk", "uid"))
    w = uw.Worker(1)
    (item,) = uw.take_batch(1, size=1)
    w._on_prepare_failed([item], uw.PrepareError("auth_expired", "HTTP 403 Forbidden", 403))
    out = capsys.readouterr().out
    assert "auth_expired |" not in out  # Web UI показал бы «доступ устарел»
    assert "refused |" in out


def test_collect_exits_3_when_yandex_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(ud, "DB_FILE", str(tmp_path / "unlim_state.db"))
    monkeypatch.setattr(ud, "_tls", __import__("threading").local())
    monkeypatch.setattr(ud, "load_cookies", lambda: {})

    def down(_s):
        raise uc.YandexUnavailableError("HTTP 502")

    monkeypatch.setattr(ud, "get_sk_uid", down)
    assert ud.cmd_collect() == EXIT_OUTAGE


def test_collect_exits_3_when_no_list_could_be_fetched(tmp_path, monkeypatch):
    monkeypatch.setattr(ud, "DB_FILE", str(tmp_path / "unlim_state.db"))
    monkeypatch.setattr(ud, "_tls", __import__("threading").local())
    monkeypatch.setattr(ud, "load_cookies", lambda: {})
    monkeypatch.setattr(ud, "get_sk_uid", lambda s: ("sk", "uid"))

    def broken(*a, **k):
        raise RuntimeError("mpfs/list: HTTP 500 (Проблема на стороне серверов Яндекса)")

    monkeypatch.setattr(ud, "api_call", broken)
    assert ud.cmd_collect() == EXIT_OUTAGE
