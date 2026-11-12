"""Диск переполнен: Яндекс закрыл программам доступ по ключу (403 DiskAPIDisabledForOverdraftUserError).

Так бывает, когда закончилась подписка и файлы занимают больше бесплатных 10 ГБ (у аккаунта
владельца в ноябре 2026 в место стал считаться и Безлимит). Раньше программа называла это «Яндекс не отвечает» и повторяла
сбор списка каждые 30 минут — или просила новый ключ, который не поможет.
"""

import argparse
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yadisk
from fastapi.testclient import TestClient

import downloader
import main
import webapp.access
import webapp.control
import webapp.demo
import webapp.folders
import webapp.paths
import webapp.queue_db
import webapp.rescue
import webapp.rescue_view
import webapp.tasks
import webui
from state import State
from utils import EXIT_OVERDRAFT
from webapp.diagnostics import EXIT_EXPLAIN

GB = 1024 ** 3
ASSETS = Path(__file__).resolve().parent.parent / "webui_assets"
OVERDRAFT = "DiskAPIDisabledForOverdraftUserError"


def overdraft_error():
    return yadisk.exceptions.ForbiddenError(
        OVERDRAFT, "API недоступен. Файлы занимают больше места, чем у вас есть.")


class _Client:
    """REST-клиент переполненного Диска: всё, кроме сведений о Диске, отвечает 403."""

    token = "t"

    def __init__(self, check=None):
        self._check = check

    def check_token(self):
        if self._check is not None:
            return self._check
        raise overdraft_error()

    def get_disk_info(self):
        return SimpleNamespace(used_space=130 * GB, total_space=10 * GB, is_paid=False,
                               user=SimpleNamespace(login="u", display_name="U", uid="1"))

    def get_files(self, **kwargs):
        raise overdraft_error()

    def listdir(self, path, **kwargs):
        raise overdraft_error()

    def get_download_link(self, remote):
        raise overdraft_error()

    def close(self):
        pass


def test_overdraft_is_told_from_other_403():
    assert downloader.is_overdraft(overdraft_error())
    assert not downloader.is_overdraft(yadisk.exceptions.ForbiddenError("DiskForbiddenError", "нет прав"))
    assert not downloader.is_overdraft(yadisk.exceptions.UnauthorizedError("401"))
    assert not downloader.is_overdraft(RuntimeError(OVERDRAFT))


def test_token_state_says_the_disk_is_full_not_that_the_key_is_bad(monkeypatch):
    monkeypatch.setattr(downloader, "make_client", lambda token: _Client())
    state, msg = downloader.token_state("y0_real-looking-token")
    assert state == "overdraft"
    assert "переполнен" in msg and "Получи новый" not in msg
    assert "130" in msg and "10" in msg  # сколько занято и сколько есть


def test_cli_exits_with_overdraft_code_before_any_listing(monkeypatch, capsys):
    monkeypatch.setattr(main, "token_state", lambda token: ("overdraft", "Диск переполнен"))
    assert main._require_token({"token": "t"}) == EXIT_OVERDRAFT
    assert main.cmd_check({"token": "t"}, argparse.Namespace()) == EXIT_OVERDRAFT
    assert "overdraft |" in capsys.readouterr().out


def test_listing_that_hits_overdraft_is_not_a_yandex_outage(tmp_path, monkeypatch, capsys):
    """Ключ прошёл проверку, а список отвечает 403 «переполнен»: раньше папка считалась
    непрочитанной, и сбор кончался кодом 3 «Яндекс не отвечает» — по кругу каждые 30 минут."""
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "state.db")
    monkeypatch.setattr(main, "token_state", lambda token: ("ok", "OK"))
    monkeypatch.setattr(main, "make_client", lambda token: _Client(check=True))
    code = main.cmd_scan({"token": "t"}, argparse.Namespace(root=None, roots=None))
    out = capsys.readouterr().out
    assert code == EXIT_OVERDRAFT
    assert "overdraft |" in out and "server_50x |" not in out


@pytest.mark.parametrize("root", ["/", "disk:/Фото"])
def test_walk_passes_overdraft_up(tmp_path, root):
    with State(str(tmp_path / "state.db")) as st:
        with pytest.raises(yadisk.exceptions.ForbiddenError) as e:
            downloader.scan_tree(_Client(check=True), st, root=root)
    assert downloader.scan_exit_code(e.value) == EXIT_OVERDRAFT


def _errors(db) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT error FROM files WHERE status='failed'")]
    finally:
        con.close()


def test_download_stops_at_the_first_overdraft_answer(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    with State(str(db)) as st:
        st.mark_pending_batch([(f"disk:/f{i}.jpg", 5, None, None) for i in range(30)])
        monkeypatch.setattr(downloader, "_get_thread_client", lambda token: _Client(check=True))
        counters = downloader.download_all("t", st, str(tmp_path / "dl"), workers=1)
    assert counters["aborted"] == "overdraft"
    assert counters["failed"] < 30  # не жжёт всю очередь
    assert main._download_exit_code(counters) == EXIT_OVERDRAFT
    errors = _errors(db)
    assert errors and all(e.startswith("overdraft |") for e in errors)


def test_overdraft_failures_go_back_to_the_queue(tmp_path, monkeypatch):
    """Подписку продлили — файлы, упавшие на «переполнен», снова в очереди при следующем запуске."""
    db = tmp_path / "state.db"
    with State(str(db)) as st:
        st.mark_pending_batch([("disk:/a.jpg", 5, None, None)])
        st.mark_failed("disk:/a.jpg", "overdraft | ForbiddenError: API недоступен")
    monkeypatch.setattr(webapp.paths, "DB_FILE", db)
    assert webapp.queue_db.reset_transient_disk_failures() == 1
    assert webapp.queue_db.reason_text("overdraft | x").startswith("Диск переполнен")


def test_webui_token_check_reports_overdraft_with_numbers(monkeypatch):
    monkeypatch.setattr(webapp.access, "make_browse_client", lambda token: _Client())
    monkeypatch.setitem(webapp.access._token_check_cache, "token", None)
    data = webapp.access.check_token("y0_unique-token-overdraft")
    assert data["ok"] is False and data["overdraft"] is True
    assert data["used"] == 130 * GB and data["total"] == 10 * GB and data["login"] == "u"
    assert "ForbiddenError" not in data["msg"]


def test_overdraft_does_not_hide_the_disk_from_a_run(monkeypatch):
    """Ключ на переполненном Диске — не «нет ключа»: Диск запускается и останавливается на понятном действии."""
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "overdraft": True})
    monkeypatch.setattr(webapp.access, "check_cookies", lambda: {"ok": False})
    src = webapp.access.preflight_sources(True, True)
    assert src["run_disk"] is True and src["error"] is None


def test_folder_tree_does_not_ask_for_a_new_key(monkeypatch):
    monkeypatch.setattr(webapp.folders.config, "get_config", lambda: {"token": "t"})
    monkeypatch.setattr(webapp.folders, "make_browse_client", lambda token: _Client(check=True))
    res = webapp.folders.api_disk_folders("disk:/")
    assert res["ok"] is False and res.get("action") != "token"
    assert "переполнен" in res["msg"]


# ── Rescue: код 6 — действие человека, а не «Яндекс не отвечает» ──


def test_rescue_stops_the_disk_on_overdraft(monkeypatch):
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": True, "login": "t", "used": 1})
    monkeypatch.setattr(webapp.access, "check_cookies", lambda: {"ok": False})
    r = webapp.rescue.Rescue()
    r.retry_every = 0.05
    r.skip_unlim = True
    monkeypatch.setattr(webapp.tasks.tm, "start", lambda slot, script, args: (True, "ok"))
    monkeypatch.setattr(r, "_wait_task", lambda slot, on_finish_msg="": EXIT_OVERDRAFT)
    r._run()
    st = r.status()
    disk = st["branches"]["disk"]
    assert disk["state"] == "needs_action" and disk["action"] == "overdraft"
    assert r.state == "failed"
    texts = [e["text"] for e in st["events"]]
    assert any("переполнен" in t for t in texts)
    assert not any("не отвечает" in t for t in texts)


def _disk_waits() -> dict:
    return {"running": False, "started_at": 100.0, "finished_at": 200.0, "state": "failed",
            "next_retry_at": None,
            "branches": {"disk": {"state": "needs_action", "action": "overdraft", "state_since": 150.0},
                         "unlim": {"state": "off"}}}


def _view(run: dict, token: dict) -> dict:
    empty = webapp.queue_db.empty_db_stats()
    return webapp.rescue_view.build_rescue_status(run, empty, empty, token, {"ok": False},
                                                  {"download_dir": "D:/Яндекс"})


FULL = {"ok": False, "overdraft": True, "used": 130 * GB, "total": 10 * GB}


def test_home_explains_overdraft_with_numbers():
    view = _view(_disk_waits(), FULL)
    action = view["action"]
    assert action["id"] == "overdraft" and action["source"] == "disk"
    assert "переполнен" in action["title"]
    assert "130 ГБ" in action["text"] and "10 ГБ" in action["text"] and "44 дня" in action["text"]
    assert view["headline"] == action["title"]


def test_home_shows_overdraft_before_any_run():
    """Диск уже переполнен, а запуска ещё не было: сразу объяснить, а не звать к первой настройке."""
    idle = {"running": False, "started_at": None, "state": "idle", "next_retry_at": None,
            "branches": {"disk": {"state": "off"}, "unlim": {"state": "off"}}}
    assert _view(idle, FULL)["action"]["id"] == "overdraft"


def test_renewed_subscription_offers_to_continue():
    view = _view(_disk_waits(), {"ok": True, "login": "u"})
    assert view["action"] is None
    assert view["sources"]["disk"]["reason"] == webapp.rescue_view.RENEWED_TEXT["overdraft"]


def test_overdraft_demo_state_exists():
    assert "overdraft" in webapp.demo.DEMO_STATES
    data = webapp.demo.demo_rescue("overdraft", {"download_dir": "D:/Яндекс"})
    assert data["rescue"]["action"]["id"] == "overdraft"
    assert data["token"].get("overdraft") is True


def test_overdraft_has_an_explanation_for_its_exit_code():
    assert "переполнен" in EXIT_EXPLAIN[EXIT_OVERDRAFT]


# ── После ревью: кнопка «продолжить», ввод ключа, лишние шаги ──


def _running_rescue(monkeypatch, action="overdraft"):
    r = webapp.rescue.Rescue()
    monkeypatch.setattr(r, "is_running", lambda: True)
    b = r.branches["disk"]
    b.state, b.action = "needs_action", action
    restarted = []
    monkeypatch.setattr(r, "_restart_branch", lambda br: restarted.append(br.name))
    monkeypatch.setattr(webapp.control, "rescue", r)
    return r, restarted


def test_continue_button_restarts_the_disk_while_unlim_still_runs(monkeypatch):
    """Безлимит качается часами, Диск стоит на «переполнен». Подписку продлили — кнопка
    «Я продлил подписку — продолжить» запускает Диск сразу, а не отвечает «Спасение уже идёт»."""
    r, restarted = _running_rescue(monkeypatch)
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": True, "login": "u"})
    res = TestClient(webui.app, headers={"Host": "localhost:8000"}).post("/api/rescue/start").json()
    assert res["ok"] is True and restarted == ["disk"]


def test_continue_button_says_the_disk_is_still_full(monkeypatch):
    r, restarted = _running_rescue(monkeypatch)
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: dict(FULL))
    res = TestClient(webui.app, headers={"Host": "localhost:8000"}).post("/api/rescue/start").json()
    assert res["ok"] is False and "переполнен" in res["msg"] and restarted == []


def test_saving_a_working_key_resumes_an_overdraft_branch(monkeypatch):
    r, restarted = _running_rescue(monkeypatch)
    assert r.resume_access("disk") is True and restarted == ["disk"]


def test_key_form_and_setup_accept_a_key_of_a_full_disk():
    """Ключ на переполненном Диске рабочий: форма его сохраняет, а мастер настройки идёт дальше.
    Иначе люди, у которых только что кончилась подписка, не могли бы даже начать."""
    js = (ASSETS / "static" / "js")
    components = (js / "components.js").read_text(encoding="utf-8")
    assert "if (!c.ok && !c.unreachable && !c.overdraft)" in components
    setup = (js / "screens" / "setup.js").read_text(encoding="utf-8")
    assert "s.token.ok || s.token.unreachable || s.token.overdraft" in setup


class _Steps:
    """Подменяет запуск задач: каждый шаг Диска кончается кодом 6, шаги записываются."""

    def __init__(self, monkeypatch, r):
        self.steps = []
        monkeypatch.setattr(webapp.tasks.tm, "start",
                            lambda slot, script, args: (self.steps.append(args[0]), (True, "ok"))[1])
        monkeypatch.setattr(r, "_wait_task", lambda slot, on_finish_msg="": EXIT_OVERDRAFT)


def _rescue_with_db(monkeypatch, tmp_path, status):
    db = tmp_path / "state.db"
    with State(str(db)) as st:
        st.mark_pending_batch([("disk:/a.jpg", 5, None, None)])
        if status == "downloaded":
            st.mark_downloaded("disk:/a.jpg", str(tmp_path / "a.jpg"))
    monkeypatch.setattr(webapp.paths, "DB_FILE", db)
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": True, "login": "t", "used": 1})
    monkeypatch.setattr(webapp.access, "check_cookies", lambda: {"ok": False})
    r = webapp.rescue.Rescue()
    r.retry_every = 0.05
    r.skip_unlim = True
    return r


def test_download_on_a_full_disk_does_not_list_again(monkeypatch, tmp_path):
    """Очередь есть, списка нет: сначала качаем. Скачивание кончилось «переполнен» — сбор списка
    после него тоже кончится им же, лишний шаг не нужен."""
    r = _rescue_with_db(monkeypatch, tmp_path, "pending")
    steps = _Steps(monkeypatch, r)
    r._run()
    assert steps.steps == ["download"]
    assert r.status()["branches"]["disk"]["action"] == "overdraft"


def test_listing_on_a_full_disk_is_not_followed_by_a_download(monkeypatch, tmp_path):
    r = _rescue_with_db(monkeypatch, tmp_path, "downloaded")
    steps = _Steps(monkeypatch, r)
    r._run()
    assert steps.steps == ["scan"]
    texts = [e["text"] for e in r.status()["events"]]
    assert not any("Список не обновился" in t for t in texts)
    assert r.status()["branches"]["disk"]["action"] == "overdraft"


def _idle() -> dict:
    return {"running": False, "started_at": None, "state": "idle", "next_retry_at": None,
            "branches": {"disk": {"state": "off"}, "unlim": {"state": "off"}}}


def test_full_disk_is_not_shown_when_only_unlim_is_chosen():
    empty = webapp.queue_db.empty_db_stats()
    view = webapp.rescue_view.build_rescue_status(_idle(), empty, empty, FULL, {"ok": True},
                                                  {"download_dir": "D:/Яндекс"}, disk_selected=False)
    assert (view["action"] or {}).get("id") != "overdraft"


def test_full_disk_is_not_shown_when_everything_is_already_saved():
    done = dict(webapp.queue_db.empty_db_stats(), total=10, downloaded=10, bytes_total=100, bytes_done=100,
                bytes_selected_total=100)
    view = webapp.rescue_view.build_rescue_status(_idle(), done, webapp.queue_db.empty_db_stats(), FULL,
                                                  {"ok": False}, {"download_dir": "D:/Яндекс"})
    assert (view["action"] or {}).get("id") != "overdraft"


def test_cli_download_says_why_it_stopped(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main, "download_all", lambda *a, **k: {"downloaded": 0, "skipped": 0, "failed": 1,
                                                               "deferred": 0, "bytes": 0, "aborted": "overdraft"})
    monkeypatch.setattr(main, "_restore_dates", lambda state, d: None)
    code = main._run_download({"download_dir": str(tmp_path), "token": "t"}, None, 1)
    assert code == EXIT_OVERDRAFT
    assert "overdraft |" in capsys.readouterr().out


def test_start_on_a_full_disk_with_nothing_else_to_download_explains_instead(monkeypatch):
    r = webapp.rescue.Rescue()
    started = []
    monkeypatch.setattr(r, "start", lambda **kw: started.append(kw) or (True, "ok"))
    monkeypatch.setattr(webapp.control, "rescue", r)
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: dict(FULL))
    monkeypatch.setattr(webapp.access, "check_cookies", lambda: {"ok": False})
    res = TestClient(webui.app, headers={"Host": "localhost:8000"}).post("/api/rescue/start").json()
    assert res["ok"] is False and "переполнен" in res["msg"] and started == []
