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
import webapp.access
from utils import EXIT_AUTH, EXIT_OUTAGE, EXIT_PARTIAL


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
    monkeypatch.setattr(webapp.access, "make_browse_client", lambda token: _Client(requests.exceptions.ReadTimeout("slow")))
    monkeypatch.setitem(webapp.access._token_check_cache, "token", None)
    data = webapp.access.check_token("y0_unique-token-for-this-test")
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


def test_unwritable_download_folder_is_a_local_disk_problem(monkeypatch, capsys):
    # Диск отключён или путь неверный: раньше процесс падал с кодом 1, и интерфейс
    # принимал это за «скачано, часть не скачалась»
    from utils import EXIT_DISK_FULL

    def broken(path):
        raise OSError(22, "Синтаксическая ошибка в имени файла")

    monkeypatch.setattr(main, "ensure_dir", broken)
    assert main._run_download({"download_dir": "X:\нет", "token": "t"}, None, 1) == EXIT_DISK_FULL
    assert "local_disk |" in capsys.readouterr().out


def test_scan_exits_3_when_a_folder_could_not_be_listed(tmp_path, monkeypatch, capsys):
    """Папка не прочиталась после повторов: список неполный — код 3, а не «собрано» с кодом 0."""
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "state.db")
    monkeypatch.setattr(main, "token_state", lambda token: ("ok", "OK"))
    monkeypatch.setattr(main, "make_client", lambda token: _Client(True))

    def scan(client, state, root, on_file, on_note):
        on_note("unreadable_folders", ["disk:/Фото/2019"])
        return 5, 10

    monkeypatch.setattr(main, "scan_tree", scan)
    code = main.cmd_scan({"token": "t"}, argparse.Namespace(root=None, roots=None))
    out = capsys.readouterr().out
    assert code == EXIT_OUTAGE
    assert "server_50x |" in out and "Итого: 5 файлов" in out


@pytest.mark.parametrize("exc,code", [
    (yadisk.exceptions.UnauthorizedError("401"), EXIT_AUTH),
    (yadisk.exceptions.InternalServerError("500"), EXIT_OUTAGE),
    (requests.exceptions.ConnectionError("no route"), EXIT_OUTAGE),
])
def test_scan_maps_a_broken_listing_to_an_exit_code(tmp_path, monkeypatch, capsys, exc, code):
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "state.db")
    monkeypatch.setattr(main, "token_state", lambda token: ("ok", "OK"))
    monkeypatch.setattr(main, "make_client", lambda token: _Client(True))

    def scan(**kwargs):
        raise exc

    monkeypatch.setattr(main, "scan_tree", scan)
    assert main.cmd_scan({"token": "t"}, argparse.Namespace(root=None, roots=None)) == code
    out = capsys.readouterr().out
    assert ("auth_expired |" if code == EXIT_AUTH else "server_50x |") in out


def test_collect_exits_3_when_the_listing_breaks_after_some_pages(tmp_path, monkeypatch, capsys):
    """Страница 2 не пришла: собранное сохранено, но код 3 — иначе Web UI запомнил бы список как полный."""
    monkeypatch.setattr(ud, "DB_FILE", str(tmp_path / "unlim_state.db"))
    monkeypatch.setattr(ud, "_tls", __import__("threading").local())
    monkeypatch.setattr(ud, "load_cookies", lambda: {})
    monkeypatch.setattr(ud, "get_sk_uid", lambda s: ("sk", "uid"))

    def partial(s, sk, uid):
        ud.save_listed_files([{"path": "/photounlim/a.jpg", "name": "a.jpg", "type": "file", "meta": {"size": 1}}])
        raise RuntimeError("mpfs/list: HTTP 503")

    monkeypatch.setattr(ud, "collect_via_list", partial)
    monkeypatch.setattr(ud, "collect_via_photoslice", lambda s, sk, uid: None)
    assert ud.cmd_collect() == EXIT_OUTAGE
    assert ud.status_counts() == {"pending": 1}
    assert "server_50x |" in capsys.readouterr().out


def test_collect_exits_3_when_the_disk_page_has_no_session_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(ud, "DB_FILE", str(tmp_path / "unlim_state.db"))
    monkeypatch.setattr(ud, "_tls", __import__("threading").local())
    monkeypatch.setattr(ud, "load_cookies", lambda: {})

    def no_keys(_s):
        raise uc.UnlimError("Не нашёл sk/uid в HTML — Яндекс изменил формат")

    monkeypatch.setattr(ud, "get_sk_uid", no_keys)
    assert ud.cmd_collect() == EXIT_OUTAGE


@pytest.mark.parametrize("exc", [uc.CookiesFormatError("cookies.json должен быть массивом"),
                                 ValueError("Expecting value: line 1 column 1")])
def test_collect_exits_2_when_cookies_cannot_be_read(tmp_path, monkeypatch, capsys, exc):
    monkeypatch.setattr(ud, "DB_FILE", str(tmp_path / "unlim_state.db"))
    monkeypatch.setattr(ud, "_tls", __import__("threading").local())

    def broken():
        raise exc

    monkeypatch.setattr(ud, "load_cookies", broken)
    assert ud.cmd_collect() == EXIT_AUTH
    assert "auth_expired |" in capsys.readouterr().out


def test_worker_exits_3_when_disk_page_has_no_session_keys(monkeypatch):
    def no_keys(_s):
        raise uc.UnlimError("Не нашёл sk/uid в HTML")

    monkeypatch.setattr(uw, "get_sk_uid", no_keys)
    monkeypatch.setattr(uw, "make_session", lambda: _Sess(_Resp(200)))
    with pytest.raises(SystemExit) as e:
        uw.Worker(1).authorize()
    assert e.value.code == EXIT_OUTAGE


def test_worker_exits_2_when_cookies_cannot_be_read(monkeypatch, capsys):
    def broken():
        raise uc.CookiesFormatError("Нет cookies.json")

    monkeypatch.setattr(uw, "make_session", broken)
    with pytest.raises(SystemExit) as e:
        uw.Worker(1).authorize()
    assert e.value.code == EXIT_AUTH
    assert "auth_expired |" in capsys.readouterr().out


def test_worker_exits_1_when_files_failed_and_nothing_waits(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "unlim_state.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, name TEXT, size INTEGER, md5 TEXT,"
        " status TEXT DEFAULT 'pending', local_path TEXT, error TEXT, worker_id INTEGER,"
        " assigned_at TIMESTAMP, downloaded_at TIMESTAMP, attempts INTEGER DEFAULT 0);"
        "INSERT INTO unlim_files (file_id, name, size, status) VALUES ('1', 'a.jpg', 10, 'failed');"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(uw, "DB_FILE", str(db))
    monkeypatch.setattr(uw.time, "sleep", lambda s: None)
    uw.ensure_attempts_column()
    assert uw.Worker(1).run() == EXIT_PARTIAL


@pytest.mark.parametrize("exc,code", [
    (yadisk.exceptions.InternalServerError("500"), EXIT_OUTAGE),
    (downloader.RootNotFound("disk:/Нет такой"), EXIT_PARTIAL),
])
def test_console_download_reports_a_broken_auto_scan(tmp_path, monkeypatch, capsys, exc, code):
    """`main.py download` с пустой базой сам собирает список: сбой сбора — код выхода, а не трейсбек."""
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "state.db")
    monkeypatch.setattr(main, "token_state", lambda token: ("ok", "OK"))
    monkeypatch.setattr(main, "make_client", lambda token: _Client(True))

    def scan(client, state, root, on_note):
        raise exc

    monkeypatch.setattr(main, "scan_tree", scan)
    args = argparse.Namespace(workers=1, wait=False, no_scan=False, root="/Нет такой")
    assert main.cmd_download({"token": "t", "max_workers": 1, "download_dir": str(tmp_path)}, args) == code
    assert "Traceback" not in capsys.readouterr().out


def test_verify_checks_only_files_that_should_be_in_the_folder(tmp_path, monkeypatch, capsys):
    """Снятый выбором файл с прежним local_path — не «отсутствует локально»."""
    from state import FOLDER_SKIP_MARKER, State

    monkeypatch.setattr(main, "DB_FILE", tmp_path / "state.db")
    kept = tmp_path / "kept.txt"
    kept.write_bytes(b"x")
    with State(str(main.DB_FILE)) as st:
        st.mark_pending_batch([("disk:/kept.txt", 1, None), ("disk:/off/gone.txt", 1, None)])
        st.mark_downloaded("disk:/kept.txt", str(kept))
        st.mark_skipped("disk:/off/gone.txt", str(tmp_path / "off" / "gone.txt"))
        st._conn().execute("UPDATE files SET error=? WHERE remote_path='disk:/off/gone.txt'", (FOLDER_SKIP_MARKER,))
    assert main.cmd_verify({}, argparse.Namespace()) == 0
    assert "Проверено: 1" in capsys.readouterr().out
