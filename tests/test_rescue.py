"""«Режим спасения»: ветки Диска и Безлимита параллельно, отложенное — раундами."""

import sqlite3
import threading

import pytest

import webui
from utils import EXIT_AUTH, EXIT_DEFERRED, EXIT_DISK_FULL, EXIT_OUTAGE, EXIT_PARTIAL


class Script:
    """Подменяет запуск задач: коды выхода по слотам, по очереди; последний повторяется."""

    def __init__(self, monkeypatch, rescue, codes, worker_codes=(0,)):
        self.codes = {slot: list(seq) for slot, seq in codes.items()}
        self.worker_codes = list(worker_codes)
        self.started = []
        self.lock = threading.Lock()
        monkeypatch.setattr(webui.tm, "start", self._start)
        monkeypatch.setattr(rescue, "_wait_task", self._wait)
        monkeypatch.setattr(rescue, "_wait_workers", self._wait_workers)

    def _start(self, slot, script, args):
        with self.lock:
            self.started.append((slot, script, tuple(args)))
        return True, "ok"

    def _next(self, seq):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def _wait(self, slot, on_finish_msg=""):
        with self.lock:
            return self._next(self.codes.setdefault(slot, [0]))

    def _wait_workers(self, ids):
        with self.lock:
            code = self._next(self.worker_codes)
        return [code for _ in ids]

    def steps(self, slot=None):
        return [(s, a[0] if a else "") for s, _sc, a in self.started if slot in (None, s)]


@pytest.fixture()
def rescue(monkeypatch):
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": True, "login": "t", "used": 1})
    monkeypatch.setattr(webui, "check_cookies", lambda: {"ok": True, "msg": "ok", "count": 5})
    r = webui.Rescue()
    r.retry_every = 0.05  # раунды в тесте — мгновенные
    r.workers = 1
    return r


def test_both_branches_run_and_finish(rescue, monkeypatch):
    sc = Script(monkeypatch, rescue, {"disk": [0, 0], "unlim_collect": [0]})
    rescue._run()
    assert rescue.state == "done"
    assert sc.steps("disk") == [("disk", "scan"), ("disk", "download")]
    assert ("unlim_collect", "--collect") in sc.steps()
    assert any(s.startswith("unlim_w") for s, _ in sc.steps())
    assert {b["state"] for b in rescue.status()["branches"].values()} == {"done"}


def test_deferred_files_get_another_round_without_relisting(rescue, monkeypatch):
    sc = Script(monkeypatch, rescue, {"disk": [0, EXIT_DEFERRED, EXIT_DEFERRED, 0]},
                worker_codes=[EXIT_DEFERRED, 0])
    rescue._run()
    assert rescue.state == "done"
    disk = sc.steps("disk")
    assert disk.count(("disk", "scan")) == 1  # список не пересобирается в раундах
    assert disk.count(("disk", "download")) == 3
    assert sum(1 for s, _ in sc.steps() if s.startswith("unlim_w")) == 2 * rescue.workers
    assert any("Следующая попытка" in line for line in rescue.log)


def test_expired_token_stops_only_the_disk(rescue, monkeypatch):
    Script(monkeypatch, rescue, {"disk": [0, EXIT_AUTH], "unlim_collect": [0]})
    rescue._run()
    branches = rescue.status()["branches"]
    assert branches["disk"]["state"] == "needs_action" and branches["disk"]["action"] == "token"
    assert branches["unlim"]["state"] == "done"
    assert rescue.state == "failed"


def test_full_disk_pauses_both_branches(rescue, monkeypatch):
    stopped = []
    monkeypatch.setattr(webui.tm, "stop_matching", lambda prefix: stopped.append(prefix) or [])
    Script(monkeypatch, rescue, {"disk": [0, EXIT_DISK_FULL], "unlim_collect": [0]},
           worker_codes=[EXIT_DEFERRED])
    rescue._run()
    branches = rescue.status()["branches"]
    assert {b["action"] for b in branches.values()} == {"free_space"}
    assert {"disk", "unlim"} <= set(stopped)


def test_yandex_down_at_listing_waits_and_retries(rescue, monkeypatch):
    rescue.skip_unlim = True
    sc = Script(monkeypatch, rescue, {"disk": [EXIT_OUTAGE, 0, 0]})
    rescue._run()
    assert rescue.state == "done"
    assert sc.steps("disk") == [("disk", "scan"), ("disk", "scan"), ("disk", "download")]


def test_unknown_exit_codes_ask_for_a_report_after_a_few_rounds(rescue, monkeypatch):
    rescue.skip_unlim = True
    Script(monkeypatch, rescue, {"disk": [0, 7]})
    rescue._run()
    disk = rescue.status()["branches"]["disk"]
    assert disk["state"] == "needs_action" and disk["action"] == "send_logs"


def test_partial_failures_finish_the_branch(rescue, monkeypatch):
    rescue.skip_unlim = True
    Script(monkeypatch, rescue, {"disk": [0, EXIT_PARTIAL]})
    rescue._run()
    assert rescue.status()["branches"]["disk"]["state"] == "done"


def test_unreachable_token_check_still_downloads_the_disk(rescue, monkeypatch):
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": False, "unreachable": True, "msg": "timeout"})
    rescue.skip_unlim = True
    sc = Script(monkeypatch, rescue, {"disk": [0, 0]})
    rescue._run()
    assert ("disk", "download") in sc.steps("disk")


def test_start_while_waiting_retries_now_and_clears_dead_hosts(rescue, monkeypatch):
    cleared = []
    monkeypatch.setattr(webui, "clear_dead_hosts", lambda path: cleared.append(path) or 0)
    rescue.retry_every = 3600  # без кнопки раунд не начнётся
    rescue.skip_unlim = True
    Script(monkeypatch, rescue, {"disk": [0, EXIT_DEFERRED, 0]})
    t = threading.Thread(target=rescue._run, daemon=True)
    rescue._thread = t
    t.start()
    for _ in range(200):
        if rescue.branches["disk"].state == "waiting":
            break
        t.join(0.01)
    assert rescue.branches["disk"].state == "waiting"
    assert rescue.status()["next_retry_at"] is not None
    ok, msg = rescue.start()  # «Скачать всё» во время ожидания = «Попробовать сейчас»
    assert ok and "сейчас" in msg
    t.join(5)
    assert rescue.state == "done"
    assert cleared  # молчавшим серверам дали новый шанс


def test_stop_kills_every_disk_and_unlim_task(rescue, monkeypatch):
    stopped = []
    monkeypatch.setattr(webui.tm, "stop_matching", lambda prefix: stopped.append(prefix) or [])
    rescue.retry_every = 3600
    Script(monkeypatch, rescue, {"disk": [0, EXIT_DEFERRED]}, worker_codes=[EXIT_DEFERRED])
    t = threading.Thread(target=rescue._run, daemon=True)
    rescue._thread = t
    t.start()
    for _ in range(200):
        if all(b.state == "waiting" for b in rescue.branches.values()):
            break
        t.join(0.01)
    ok, _ = rescue.stop()
    assert ok
    assert {"disk", "unlim"} <= set(stopped)
    assert rescue.state == "stopped"
    assert {b["state"] for b in rescue.status()["branches"].values()} == {"stopped"}


def test_choosing_everything_brings_back_deselected_categories(rescue, monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending', local_path TEXT, error TEXT, downloaded_at TIMESTAMP);"
    )
    con.execute("INSERT INTO files (remote_path, size, status, error) VALUES ('disk:/a.pdf', 1, 'skipped', ?)",
                (webui.CATEGORY_SKIP_MARKER,))
    con.commit()
    con.close()
    monkeypatch.setattr(webui, "DB_FILE", db)
    rescue.skip_unlim = True
    Script(monkeypatch, rescue, {"disk": [0, 0]})
    rescue._run()  # disk_categories=None — «всё»
    con = sqlite3.connect(db)
    assert con.execute("SELECT status FROM files").fetchone()[0] == "pending"
    con.close()


def test_status_keeps_the_old_pipeline_shape(rescue):
    st = rescue.status()
    assert {"state", "running", "step", "started_at", "elapsed", "partial", "log"} <= set(st)
    assert {"branches", "next_retry_at", "retry_every"} <= set(st)


def test_combine_worker_codes():
    combine = webui.Rescue._combine_worker_codes
    assert combine([0, 0]) == 0
    assert combine([0, EXIT_DEFERRED]) == EXIT_DEFERRED
    assert combine([EXIT_DEFERRED, EXIT_AUTH]) == EXIT_AUTH
    assert combine([EXIT_AUTH, EXIT_DISK_FULL]) == EXIT_DISK_FULL
    assert combine([None, 0]) == 0
