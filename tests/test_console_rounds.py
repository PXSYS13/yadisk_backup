"""Повторы отложенных файлов: --wait в консоли, итог каждой попытки и честные тексты.

Сообщество спрашивало: «доля удачных скачиваний резко упала — у всех так?» Она падает,
когда живые файлы уже скачаны: в очереди остаются файлы молчащих серверов. Поэтому
программа показывает отдельно, сколько из отложенных отдал Яндекс за попытку, а в
консоли не обещает «повторю позже», если её об этом не попросили (--wait).
"""

import argparse
import contextlib
import sqlite3
import time

import pytest
import requests

import downloader
import main
import unlim_worker as uw
import utils
import webapp.access
import webapp.listing
import webapp.paths
import webapp.rescue
import webapp.tasks
from state import State
from utils import EXIT_DEFERRED, EXIT_INTERRUPTED, EXIT_OK

DEAD = "s1klg.storage.yandex.net"
ALIVE = "s2nrg.storage.yandex.net"


# ── Тексты ──────────────────────────────────────────────────────────────────


def test_round_text_counts_retries_in_plain_russian():
    assert utils.deferred_round_text(1, 1) == "Из 1 файла, ждавших Яндекса, скачано 1 (100%)"
    assert utils.deferred_round_text(1234, 56) == "Из 1 234 файлов, ждавших Яндекса, скачано 56 (4%)"
    assert utils.deferred_round_text(21, 0) == (
        "Яндекс пока не отдал ни одного из 21 файла, ждавших его: их серверы хранения ещё молчат")
    # В очереди были и новые файлы — процент от отложенных был бы враньём
    assert "%" not in utils.deferred_round_text(3, 5)


def test_files_text_and_clock():
    assert [utils.files_text(n) for n in (1, 3, 11, 22, 1234)] == [
        "1 файл", "3 файла", "11 файлов", "22 файла", "1 234 файла"]
    assert utils.hhmm(None) == ""
    assert utils.hhmm(time.mktime((2026, 10, 19, 14, 5, 0, 0, 0, -1))) == "14:05"
    assert utils.utc_ts("2026-10-19T11:00:00") == utils.utc_ts("2026-10-19 11:00:00")


def test_retry_interval_and_webui_marker(monkeypatch):
    monkeypatch.setenv("YADISK_RETRY_EVERY_SEC", "")
    assert utils.retry_every_sec() == 1800
    monkeypatch.setenv("YADISK_RETRY_EVERY_SEC", "5")
    assert utils.retry_every_sec() == 60  # не чаще раза в минуту
    monkeypatch.setenv(utils.UNDER_WEBUI_ENV, "1")
    assert utils.under_webui()
    monkeypatch.setenv(utils.UNDER_WEBUI_ENV, "")
    assert not utils.under_webui()


# ── main.py download --wait ─────────────────────────────────────────────────


class _Stream:
    def __init__(self, data=b"", status=200, headers=None):
        self._data = data
        self.status_code = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        if self._data:
            yield self._data


class _Http:
    """downloader → 302 → сервер хранения. storage: сервер → байты или исключение."""

    def __init__(self, storage):
        self.storage = storage

    def get(self, url, stream=False, timeout=None, allow_redirects=True):
        host = url.split("/")[2]
        if host == "dl":
            return _Stream(status=302, headers={"Location": "https://" + url.split("/", 3)[3]})
        answer = self.storage[host]
        if isinstance(answer, Exception):
            raise answer
        return _Stream(answer)


class _Client:
    def __init__(self, hosts):
        self.hosts = hosts

    def get_download_link(self, remote):
        return f"https://dl/{self.hosts[remote]}/{remote.split('/')[-1]}"

    def close(self):
        pass


@pytest.fixture()
def disk(tmp_path, monkeypatch):
    """Диск из двух фото: a.jpg на молчащем сервере, b.jpg на живом."""
    downloader.DEAD_HOSTS.clear()
    http = _Http({DEAD: requests.exceptions.ConnectTimeout("timed out"), ALIVE: b"photo"})
    hosts = {"disk:/a.jpg": DEAD, "disk:/b.jpg": ALIVE}
    monkeypatch.setattr(downloader, "_thread_http", lambda: http)
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: _Client(hosts))
    monkeypatch.setattr(downloader.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "token_state", lambda token: ("ok", "Токен OK."))
    monkeypatch.setattr(main, "keep_awake", contextlib.nullcontext)
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "state.db")
    with State(str(main.DB_FILE)) as st:
        st.mark_pending_batch([("disk:/a.jpg", 5, None), ("disk:/b.jpg", 5, None)])
    yield http
    downloader.DEAD_HOSTS.clear()


def _cfg(tmp_path):
    return {"token": "t", "download_dir": str(tmp_path / "dl"), "max_workers": 1}


def _args(wait):
    return argparse.Namespace(workers=1, wait=wait, no_scan=True, root="/")


def test_wait_retries_deferred_files_until_yandex_gives_them(tmp_path, disk, monkeypatch, capsys):
    waits = []

    def sleep_until(ts):
        waits.append(ts)
        assert len(waits) < 3, "сервер ожил, а файл всё ещё не скачан"
        disk.storage[DEAD] = b"photo"  # пока ждали, сервер ожил
        return True

    monkeypatch.setattr(main, "_sleep_until", sleep_until)
    assert main.cmd_download(_cfg(tmp_path), _args(wait=True)) == EXIT_OK
    out = capsys.readouterr().out
    assert len(waits) == 1
    assert "Ждут Яндекса: 1 файл на 1 сервере хранения. Следующая попытка в" in out
    # Второй раунд пробует сервер, который молчал в первом: память о мёртвых серверах сброшена
    assert "Попытка №2: Из 1 файла, ждавших Яндекса, скачано 1 (100%)." in out
    assert "Отложенных файлов больше нет" in out
    with State(str(main.DB_FILE)) as st:
        assert st.get_stats()["downloaded"] == 2


def test_ctrl_c_while_waiting_keeps_the_queue(tmp_path, disk, monkeypatch):
    monkeypatch.setattr(main, "_sleep_until", lambda ts: False)
    assert main.cmd_download(_cfg(tmp_path), _args(wait=True)) == EXIT_INTERRUPTED
    with State(str(main.DB_FILE)) as st:
        assert st.get_deferred_stats()["files"] == 1


def test_sleep_until_turns_ctrl_c_into_false(monkeypatch, capsys):
    def interrupted(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(main.time, "sleep", interrupted)
    assert main._sleep_until(main.time.time() + 60) is False
    assert "Скачанное сохранено" in capsys.readouterr().out


@pytest.mark.parametrize("under_ui", [False, True])
def test_console_hint_only_outside_webui(tmp_path, disk, monkeypatch, capsys, under_ui):
    monkeypatch.setenv(utils.UNDER_WEBUI_ENV, "1" if under_ui else "")
    assert main.cmd_download(_cfg(tmp_path), _args(wait=False)) == EXIT_DEFERRED
    out = capsys.readouterr().out
    assert "storage_down | Отложено 1 файл:" in out
    # Раньше консоль обещала «Программа повторит их позже», но в консоли повторов не было
    assert "Программа повторит" not in out
    assert ("main.py download --wait" in out) is (not under_ui)


def test_stats_tells_waiting_files_from_untried(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "state.db")
    with State(str(main.DB_FILE)) as st:
        st.mark_pending_batch([("disk:/a.jpg", 5, None), ("disk:/b.jpg", 7, None), ("disk:/c.jpg", 9, None)])
        st.mark_deferred("disk:/a.jpg", DEAD, "сервер хранения не отвечает")
        st.mark_deferred("disk:/b.jpg", DEAD, "сервер хранения не отвечает")
        st.mark_downloaded("disk:/c.jpg", str(tmp_path / "c.jpg"))
    assert main.cmd_stats({}, argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "ещё не пробовали:  0" in out
    assert "ждут Яндекса:      2 (12.00 B, серверов хранения: 1)" in out
    assert "Последний файл скачан:" in out
    assert "Всё, что Яндекс отдаёт сейчас, уже скачано" in out


# ── Web UI ──────────────────────────────────────────────────────────────────


def test_webui_tasks_know_they_run_under_webui(monkeypatch):
    seen = {}

    class Proc:
        pid = 1
        stdout = iter(())

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def popen(cmd, **kw):
        seen.update(kw["env"])
        return Proc()

    monkeypatch.setattr(webapp.tasks.subprocess, "Popen", popen)
    ok, _msg = webapp.tasks.TaskManager().start("disk", "main.py", ["download"])
    assert ok and seen[utils.UNDER_WEBUI_ENV] == "1"


def test_feed_reports_how_many_waiting_files_yandex_gave():
    r = webapp.rescue.Rescue()
    b = r.branches["disk"]
    r._report_retry(b, {"deferred": 10, "pending": 10, "downloaded": 5}, {"downloaded": 8})
    r._report_retry(b, {"deferred": 4, "pending": 4, "downloaded": 8}, {"downloaded": 8})
    # В очереди были и новые файлы — это не чистый повтор, молчим
    r._report_retry(b, {"deferred": 4, "pending": 9, "downloaded": 8}, {"downloaded": 20})
    assert [(e["kind"], e["text"]) for e in r.events] == [
        ("ok", "Из 10 файлов, ждавших Яндекса, скачано 3 (30%)"),
        ("info", "Яндекс пока не отдал ни одного из 4 файлов, ждавших его: их серверы хранения ещё молчат"),
    ]


def test_rescue_rounds_report_retries_in_the_feed(tmp_path, monkeypatch):
    db = webapp.paths.DB_FILE
    with State(str(db)) as st:
        st.mark_pending_batch([("disk:/a.jpg", 5, None), ("disk:/b.jpg", 5, None)])
        st.mark_deferred("disk:/a.jpg", DEAD, "сервер хранения не отвечает")
        st.mark_deferred("disk:/b.jpg", DEAD, "сервер хранения не отвечает")
    webapp.listing.write_listing("disk", {"at": time.time(), "whole": True, "folders": []})
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": True, "login": "t", "used": 1})
    monkeypatch.setattr(webapp.tasks.tm, "start", lambda slot, script, args: (True, "ok"))
    r = webapp.rescue.Rescue()
    r.retry_every = 0.05
    r.skip_unlim = True
    rounds = iter([("disk:/a.jpg", EXIT_DEFERRED), ("disk:/b.jpg", EXIT_OK)])

    def download_step(slot, on_finish_msg=""):
        remote, code = next(rounds)  # Яндекс отдаёт по одному отложенному файлу за попытку
        con = sqlite3.connect(db)
        con.execute("UPDATE files SET status='downloaded', error=NULL, downloaded_at='2026-10-19 12:00:00' "
                    "WHERE remote_path=?", (remote,))
        con.commit()
        con.close()
        return code

    monkeypatch.setattr(r, "_wait_task", download_step)
    r._run()
    assert r.state == "done"
    texts = [e["text"] for e in r.events]
    assert "Из 2 файлов, ждавших Яндекса, скачано 1 (50%)" in texts
    assert "Из 1 файла, ждавших Яндекса, скачано 1 (100%)" in texts


# ── unlim_worker.py --wait ──────────────────────────────────────────────────


def test_unlim_worker_wait_runs_rounds_with_a_fresh_worker(monkeypatch, capsys):
    runs = []
    codes = iter([EXIT_DEFERRED, EXIT_OK])

    def run(self):
        runs.append(self)
        code = next(codes)
        if code == EXIT_OK:
            self.total_files = 3
        return code

    monkeypatch.setattr(uw.Worker, "authorize", lambda self: None)
    monkeypatch.setattr(uw.Worker, "run", run)
    waiting = iter([3, 3, 3])  # перед первой попыткой, после неё, перед второй
    monkeypatch.setattr(uw, "count_deferred", lambda db: next(waiting))
    cleared = []
    monkeypatch.setattr(uw, "clear_dead_hosts", lambda db: cleared.append(db) or 0)
    monkeypatch.setattr(uw, "recover_stuck", lambda worker_id=None: 0)
    monkeypatch.setattr(uw, "retry_every_sec", lambda: 0)
    assert uw._run_rounds(uw.Worker(1)) == EXIT_OK
    out = capsys.readouterr().out
    assert len(runs) == 2 and runs[0] is not runs[1]  # счётчики прошлой попытки не мешают новой
    assert cleared  # молчавшим серверам — новый шанс
    assert "Ждут Яндекса: 3 файла. Следующая попытка в" in out
    assert "попытка №2: Из 3 файлов, ждавших Яндекса, скачано 3 (100%)" in out


def test_unlim_worker_wait_stops_when_a_person_is_needed(monkeypatch):
    def need_cookies(self):
        raise SystemExit(utils.EXIT_AUTH)

    monkeypatch.setattr(uw.Worker, "authorize", need_cookies)
    monkeypatch.setattr(uw, "count_deferred", lambda db: 0)
    assert uw._run_rounds(uw.Worker(1)) == utils.EXIT_AUTH
