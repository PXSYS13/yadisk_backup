"""Тесты серверной логики дашборда (без запуска uvicorn)."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import webui

# ── классификация ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("disk:/a/photo.JPG", "photos"),
        ("disk:/a/clip.mp4", "videos"),
        ("disk:/a/song.flac", "audio"),
        ("disk:/a/doc.pdf", "documents"),
        ("disk:/a/pack.7z", "archives"),
        ("disk:/a/setup.exe", "programs"),
        ("disk:/a/noext", "other"),
        ("disk:/a/weird.qqq", "other"),
    ],
)
def test_categorize(path, expected):
    assert webui.categorize(path) == expected


# ── маскировка токена ───────────────────────────────────────────────────────


def test_mask_token_hides_middle():
    token = "y0_AgAAAABsecretsecretsecret"
    masked = webui.mask_token(token)
    assert "secretsecret" not in masked
    assert masked.startswith("y0_AgA")


def test_mask_token_empty():
    assert webui.mask_token("") == ""
    assert webui.mask_token("short") == ""


# ── защита organize от системных папок ──────────────────────────────────────


def test_organize_rejects_windows_dir():
    ok, msg = webui._is_safe_organize_path(r"C:\Windows")
    assert ok is False
    assert "Системная" in msg


def test_organize_rejects_program_files_subdir():
    ok, _ = webui._is_safe_organize_path(r"C:\Program Files\что-то")
    assert ok is False


def test_organize_rejects_missing_dir(tmp_path):
    ok, msg = webui._is_safe_organize_path(str(tmp_path / "нет-такой"))
    assert ok is False
    assert "не существует" in msg


def test_organize_allows_normal_dir(tmp_path):
    ok, msg = webui._is_safe_organize_path(str(tmp_path))
    assert ok is True, msg


# ── фильтр по категориям ────────────────────────────────────────────────────


@pytest.fixture()
def disk_db(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE files (
            remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,
            status TEXT NOT NULL DEFAULT 'pending', local_path TEXT,
            error TEXT, downloaded_at TIMESTAMP
        );
        """
    )
    con.executemany(
        "INSERT INTO files (remote_path, size, status) VALUES (?, ?, ?)",
        [
            ("disk:/a.jpg", 10, "pending"),
            ("disk:/b.mp4", 20, "pending"),
            ("disk:/c.pdf", 30, "pending"),
            ("disk:/d.jpg", 40, "downloaded"),
        ],
    )
    con.commit()
    con.close()
    return db


def statuses(db):
    con = sqlite3.connect(db)
    try:
        return dict(con.execute("SELECT remote_path, status FROM files"))
    finally:
        con.close()


def test_filter_keeps_only_selected_categories(disk_db):
    skipped, kept = webui.filter_to_categories(disk_db, "files", "remote_path", ["photos"])
    assert (skipped, kept) == (2, 1)
    st = statuses(disk_db)
    assert st["disk:/a.jpg"] == "pending"
    assert st["disk:/b.mp4"] == "skipped"
    assert st["disk:/c.pdf"] == "skipped"
    assert st["disk:/d.jpg"] == "downloaded"  # скачанное не трогаем


def test_filter_is_reversible(disk_db):
    webui.filter_to_categories(disk_db, "files", "remote_path", ["photos"])
    n = webui.unskip_category_marker(disk_db, "files")
    assert n == 2
    st = statuses(disk_db)
    assert st["disk:/b.mp4"] == "pending"
    assert st["disk:/c.pdf"] == "pending"


def test_unskip_does_not_touch_other_skips(disk_db):
    con = sqlite3.connect(disk_db)
    con.execute("UPDATE files SET status='skipped', error='duplicate (md5 match)' "
                "WHERE remote_path='disk:/c.pdf'")
    con.commit()
    con.close()

    webui.filter_to_categories(disk_db, "files", "remote_path", ["photos"])
    webui.unskip_category_marker(disk_db, "files")
    st = statuses(disk_db)
    assert st["disk:/c.pdf"] == "skipped"  # дубликат остался дубликатом


# ── статистика ──────────────────────────────────────────────────────────────


def test_db_stats_counts(disk_db):
    out = webui.db_stats(disk_db, "files", "remote_path")
    assert out["total"] == 4
    assert out["pending"] == 3
    assert out["downloaded"] == 1
    assert out["bytes_total"] == 100
    assert out["bytes_done"] == 40


def test_db_stats_missing_file(tmp_path):
    out = webui.db_stats(tmp_path / "нет.db", "files", "remote_path")
    assert out["total"] == 0
    assert out["recent"] == []


def test_preview_groups_by_category(disk_db):
    out = webui.preview_db(disk_db, "files", "remote_path")
    assert out["empty"] is False
    assert out["categories"]["photos"]["count"] == 2
    assert out["categories"]["videos"]["count"] == 1
    assert out["categories"]["documents"]["count"] == 1


def test_local_size_marks_partial_scan(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"12345")
    webui._local_size_cache.clear()
    out = webui.local_size(str(tmp_path))
    assert out["bytes"] == 5
    assert out["partial"] is False


def test_sql_ident_whitelist_rejects_injection(disk_db):
    with pytest.raises(ValueError, match="Недопустимый table"):
        webui.db_stats(disk_db, "files; DROP TABLE files; --", "remote_path")

    with pytest.raises(ValueError, match="Недопустимый column"):
        webui.preview_db(disk_db, "files", "1=1; --")


def test_donate_redirect():
    resp = webui.donate()
    assert resp.status_code == 307
    assert resp.headers["location"] == "/?tab=donate"


def test_donation_url_constant():
    assert webui.DONATION_URL == "https://www.donationalerts.com/r/davronsvv"


def test_api_status_does_not_leak_raw_token(monkeypatch):
    secret = "y0_AgAAAAA_super_secret_raw_token_xyz"
    monkeypatch.setattr(webui, "get_config", lambda: {
        "token": secret,
        "download_dir": "D:/downloads",
        "max_workers": 4,
    })
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": True, "login": "test"})
    st = webui.api_status()
    cfg = st["config"]
    assert "token" not in cfg  # сырой токен НИКОГДА не должен отдаваться
    assert "token_masked" in cfg
    assert "secret" not in cfg["token_masked"]


def test_organize_rejects_other_drive_windows():
    ok, msg = webui._is_safe_organize_path(r"D:\Windows")
    assert ok is False
    assert "Системная" in msg

    ok2, _ = webui._is_safe_organize_path(r"E:\Program Files\App")
    assert ok2 is False


def test_organize_rejects_user_home_dir():
    import os
    home = os.path.expanduser("~")
    ok, msg = webui._is_safe_organize_path(home)
    assert ok is False
    assert "домашнюю папку" in msg


def test_db_stats_excludes_category_skipped_from_bytes_done(tmp_path):
    db = tmp_path / "stats_cat.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE files (
            remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,
            status TEXT NOT NULL DEFAULT 'pending', local_path TEXT,
            error TEXT, downloaded_at TIMESTAMP
        );
        """
    )
    con.executemany(
        "INSERT INTO files (remote_path, size, status, error) VALUES (?, ?, ?, ?)",
        [
            ("disk:/done.jpg", 10, "downloaded", None),
            ("disk:/cat_skip.pdf", 50, "skipped", "not in selected categories"),
            ("disk:/resumed.jpg", 30, "skipped", None),
            ("disk:/pending.jpg", 20, "pending", None),
        ],
    )
    con.commit()
    con.close()

    out = webui.db_stats(db, "files", "remote_path")
    assert out["total"] == 4
    # bytes_done должен считать downloaded (10) + resumed (30), НО НЕ cat_skip (50)
    assert out["bytes_done"] == 40
    assert out["category_skipped"] == 1
    assert out["category_skipped_bytes"] == 50


def test_pipeline_status_elapsed():
    pipe = webui.Pipeline()
    assert pipe.status()["elapsed"] is None
    pipe.started_at = 100.0
    pipe.finished_at = 150.0
    assert pipe.status()["elapsed"] == 50.0


def test_task_was_stopped():
    class FakeProc:
        def poll(self):
            return 1
        def terminate(self):
            pass
        def wait(self, timeout=None):
            pass
        stdout = []

    t = webui.Task("test_slot", FakeProc())
    assert t.was_stopped is False
    t.stop()
    assert t.was_stopped is True


def test_api_unlim_retry(tmp_path, monkeypatch):
    db_file = tmp_path / "test_unlim_api.db"
    monkeypatch.setattr(webui, "UNLIM_DB_FILE", db_file)
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE unlim_files (
            file_id TEXT PRIMARY KEY, status TEXT, attempts INTEGER, error TEXT, worker_id INTEGER
        );
        INSERT INTO unlim_files VALUES ('f1', 'failed', 3, 'err', NULL);
        INSERT INTO unlim_files VALUES ('f2', 'in_progress', 1, 'err', 1);
        """
    )
    con.close()

    # Мокаем запуск воркеров
    started_slots = []
    monkeypatch.setattr(webui.tm, "start", lambda slot, script, args: (started_slots.append((slot, args)) or True, "ok"))

    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    resp = client.post("/api/unlim/retry", json={"workers": 2, "batch": 15})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(started_slots) == 2
    assert "--batch" in started_slots[0][1]
    assert "15" in started_slots[0][1]

    con = sqlite3.connect(db_file)
    rows = dict(con.execute("SELECT file_id, status FROM unlim_files").fetchall())
    con.close()
    assert rows["f1"] == "pending"
    assert rows["f2"] == "pending"


def test_pipeline_aborts_on_scan_failure(monkeypatch):
    monkeypatch.setattr(
        webui, "check_token", lambda tok: {"ok": True, "login": "test", "used": 100}
    )
    monkeypatch.setattr(webui.tm, "start", lambda slot, script, args: (True, "ok"))
    pipe = webui.Pipeline()
    pipe.skip_disk = False
    pipe.skip_unlim = False
    calls = []

    def fake_wait_task(slot, on_finish_msg=""):
        calls.append((slot, pipe.step))
        if pipe.step == "сканирую диск":
            return 1  # Failure
        return 0

    monkeypatch.setattr(pipe, "_wait_task", fake_wait_task)
    pipe._run()
    assert pipe.state == "failed"
    assert calls == [("disk", "сканирую диск")]


def test_pipeline_aborts_on_download_failure(monkeypatch):
    monkeypatch.setattr(
        webui, "check_token", lambda tok: {"ok": True, "login": "test", "used": 100}
    )
    monkeypatch.setattr(webui.tm, "start", lambda slot, script, args: (True, "ok"))
    pipe = webui.Pipeline()
    pipe.skip_disk = False
    pipe.skip_unlim = False
    calls = []

    def fake_wait_task(slot, on_finish_msg=""):
        calls.append((slot, pipe.step))
        if pipe.step == "качаю диск":
            return 2  # Error
        return 0

    monkeypatch.setattr(pipe, "_wait_task", fake_wait_task)
    pipe._run()
    assert pipe.state == "failed"
    assert len(calls) == 2
    assert calls[0] == ("disk", "сканирую диск")
    assert calls[1] == ("disk", "качаю диск")


def test_pipeline_aborts_on_unlim_collect_failure(monkeypatch):
    monkeypatch.setattr(
        webui, "check_token", lambda tok: {"ok": True, "login": "test", "used": 100}
    )
    monkeypatch.setattr(webui, "check_cookies", lambda: {"ok": True, "count": 5})
    monkeypatch.setattr(webui.tm, "start", lambda slot, script, args: (True, "ok"))
    pipe = webui.Pipeline()
    pipe.skip_disk = False
    pipe.skip_unlim = False
    calls = []

    def fake_wait_task(slot, on_finish_msg=""):
        calls.append((slot, pipe.step))
        if slot == "unlim_collect":
            return 1
        return 0

    monkeypatch.setattr(pipe, "_wait_task", fake_wait_task)
    pipe._run()
    assert pipe.state == "failed"
    assert ("unlim_collect", "собираю список безлимита") in calls


def test_pipeline_success_flow(monkeypatch):
    monkeypatch.setattr(
        webui, "check_token", lambda tok: {"ok": True, "login": "test", "used": 100}
    )
    monkeypatch.setattr(webui, "check_cookies", lambda: {"ok": True, "count": 5})
    monkeypatch.setattr(webui.tm, "start", lambda slot, script, args: (True, "ok"))
    pipe = webui.Pipeline()
    pipe.skip_disk = False
    pipe.skip_unlim = False
    calls = []

    def fake_wait_task(slot, on_finish_msg=""):
        calls.append((slot, pipe.step))
        return 0

    monkeypatch.setattr(pipe, "_wait_task", fake_wait_task)
    pipe._run()
    assert pipe.state == "done"
    steps = [c[1] for c in calls]
    assert "сканирую диск" in steps
    assert "качаю диск" in steps
    assert "собираю список безлимита" in steps


def test_get_config_safe_max_workers(monkeypatch):
    monkeypatch.setenv("MAX_WORKERS", "invalid_garbage")
    cfg = webui.get_config()
    assert cfg["max_workers"] == 4

    monkeypatch.setenv("MAX_WORKERS", "100")
    cfg = webui.get_config()
    assert cfg["max_workers"] == 16

    monkeypatch.setenv("MAX_WORKERS", "-10")
    cfg = webui.get_config()
    assert cfg["max_workers"] == 1


def test_api_set_dir_validation(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(webui, "ENV_FILE", tmp_path / ".env")
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})

    # Reject C:\
    resp = client.post("/api/config/download_dir", json={"download_dir": "C:\\"})
    assert resp.status_code == 400

    # Reject Windows directory
    windir = os.environ.get("WINDIR", r"C:\Windows")
    resp = client.post("/api/config/download_dir", json={"download_dir": windir})
    assert resp.status_code == 400

    # Reject home dir
    home = os.path.expanduser("~")
    resp = client.post("/api/config/download_dir", json={"download_dir": home})
    assert resp.status_code == 400

    # Reject empty
    resp = client.post("/api/config/download_dir", json={"download_dir": "   "})
    assert resp.status_code == 400

    # Accept safe dir
    safe_dir = str(tmp_path / "my_backup_folder")
    resp = client.post("/api/config/download_dir", json={"download_dir": safe_dir})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_api_set_cookies_validation(tmp_path, monkeypatch):
    cookies_path = tmp_path / "cookies.json"
    monkeypatch.setattr(webui, "COOKIES_FILE", cookies_path)
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})

    # Invalid JSON / not a list
    resp = client.post("/api/cookies", json={"cookies": "not a list"})
    assert resp.status_code in (400, 422)
    assert not cookies_path.exists()

    # Missing essential cookies (e.g. Session_id)
    resp = client.post(
        "/api/cookies", json={"cookies": [{"name": "foo", "value": "bar"}]}
    )
    assert resp.status_code == 400
    assert not cookies_path.exists()

    # Valid cookies
    valid = [
        {"name": "Session_id", "value": "valid_session"},
        {"name": "yandexuid", "value": "123"},
        {"name": "sessionid2", "value": "456"},
    ]
    resp = client.post("/api/cookies", json={"cookies": valid})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert cookies_path.exists()


def test_api_set_dir_quotes_hash_and_spaces(tmp_path, monkeypatch):
    from dotenv import dotenv_values

    env_file = tmp_path / ".env"
    monkeypatch.setattr(webui, "ENV_FILE", env_file)
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})

    target_dir = str(tmp_path / "my backup#1")
    resp = client.post("/api/config/download_dir", json={"download_dir": target_dir})
    assert resp.status_code == 200

    vals = dotenv_values(str(env_file))
    assert vals["DOWNLOAD_DIR"] == target_dir


def test_disk_clear_waits_for_task_stop(tmp_path, monkeypatch):
    db_file = tmp_path / "state.db"
    db_file.write_text("fake db content")
    monkeypatch.setattr(webui, "DB_FILE", db_file)

    calls = []

    class MockTask:
        def alive(self):
            return True

        def stop(self):
            calls.append("stop")

        def join(self, timeout=2.0):
            calls.append("join")

    monkeypatch.setitem(webui.tm._tasks, "disk", MockTask())

    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    resp = client.post("/api/disk/clear")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert calls == ["stop", "join"]
    assert not db_file.exists()


class _FakeProc:
    """Мёртвый процесс без вывода — чтобы собрать настоящий webui.Task."""
    stdout = []

    def poll(self):
        return 0

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def test_clear_endpoints_work_with_real_task_objects(tmp_path, monkeypatch):
    """Раньше clear падал с AttributeError: код обращался к несуществующему Task.thread."""
    monkeypatch.setattr(webui, "DB_FILE", tmp_path / "state.db")
    monkeypatch.setattr(webui, "UNLIM_DB_FILE", tmp_path / "unlim_state.db")
    monkeypatch.setitem(webui.tm._tasks, "disk", webui.Task("disk", _FakeProc()))
    monkeypatch.setitem(webui.tm._tasks, "unlim_w1", webui.Task("unlim_w1", _FakeProc()))

    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    assert client.post("/api/disk/clear").json()["ok"] is True
    assert client.post("/api/unlim/clear").json()["ok"] is True


def test_task_manager_prefix_helpers(monkeypatch):
    class AliveTask:
        def __init__(self):
            self.stopped = False
            self.log = ["x"]

        def alive(self):
            return not self.stopped

        def stop(self):
            self.stopped = True

    w1, w2, collect = AliveTask(), AliveTask(), AliveTask()
    monkeypatch.setitem(webui.tm._tasks, "unlim_w1", w1)
    monkeypatch.setitem(webui.tm._tasks, "unlim_w2", w2)
    monkeypatch.setitem(webui.tm._tasks, "unlim_collect", collect)

    assert webui.tm.any_alive("unlim_w") is True
    assert webui.tm.clear_logs("unlim_w*") == 2
    assert w1.log == [] and collect.log == ["x"]

    stopped = webui.tm.stop_matching("unlim_w")
    assert {id(t) for t in stopped} == {id(w1), id(w2)}
    assert collect.stopped is False
    assert webui.tm.any_alive("unlim_w") is False


def test_launch_unlim_workers_caps_values():
    launched = []

    def starter(slot, script, args):
        launched.append((slot, args))
        return True, "ok"

    res = webui._launch_unlim_workers(100, 10_000, starter=starter)
    assert res["started"] == list(range(1, webui.MAX_UNLIM_WORKERS + 1))
    assert all(args[-1] == str(webui.MAX_UNLIM_BATCH) for _, args in launched)
    assert launched[0][0] == "unlim_w1"


def test_get_from_foreign_host_is_blocked():
    """DNS-rebinding: чужой Host не должен читать /api/status."""
    client = TestClient(webui.app, headers={"Host": "evil.example"})
    assert client.get("/api/status").status_code == 403
    ok_client = TestClient(webui.app, headers={"Host": "127.0.0.1:8000"})
    assert ok_client.get("/api/disk/preview").status_code == 200


def test_organize_rejects_project_dir():
    ok, msg = webui._is_safe_organize_path(str(webui.PROJECT))
    assert ok is False
    assert "инструмента" in msg
    ok, _ = webui._is_safe_organize_path(str(webui.PROJECT.parent))
    assert ok is False


def test_organize_move_refused_while_downloading(tmp_path, monkeypatch):
    class Alive:
        def alive(self):
            return True

    monkeypatch.setitem(webui.tm._tasks, "unlim_w1", Alive())
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    resp = client.post("/api/organize", json={"input": str(tmp_path), "action": "move"})
    assert resp.json()["ok"] is False
    assert "остановите" in resp.json()["msg"]


class _LogOnlyTask:
    def __init__(self, lines):
        self.log = list(lines)

    def alive(self):
        return False


def test_diagnose_explains_bulk_disabled(monkeypatch):
    monkeypatch.setitem(webui.tm._tasks, "unlim_w1", _LogOnlyTask([
        "12:00:01  [w1] batch 3: ⚠️ Сбой отдачи архива узлом Яндекса (1 файлов): not_zip | вместо ZIP пришёл text/html",
        "12:00:40  [w1] bulk_unavailable | Яндекс 5 пачек подряд не отдал архив.",
    ]))
    diag = webui.diagnose_tasks()
    assert diag["kind"] == "bulk_disabled"
    assert diag["counts"]["bulk_disabled"] == 2
    assert diag["affects"][0]["state"] == "ok"      # диск работает
    assert diag["affects"][1]["state"] == "wait"    # безлимит ждёт
    assert diag["links"]


def test_diagnose_auth_has_priority_and_keeps_legacy_flags(monkeypatch):
    monkeypatch.setitem(webui.tm._tasks, "disk", _LogOnlyTask([
        "FAIL | server_50x | BadGatewayError", "FAIL | auth_expired | UnauthorizedError",
    ]))
    diag = webui.diagnose_tasks()
    assert diag["kind"] == "auth_expired"
    assert diag["auth_expired_detected"] is True
    assert diag["server_50x_detected"] is False
    assert diag["counts"]["server_50x"] == 1


def test_diagnose_local_disk_is_not_blamed_on_yandex(monkeypatch):
    monkeypatch.setitem(webui.tm._tasks, "disk", _LogOnlyTask([
        "FAIL | local_disk | OSError: [Errno 28] No space left on device: 'IMG_5003.jpg.part'",
    ]))
    assert webui.diagnose_tasks()["kind"] == "local_disk"


def test_db_stats_rate_window(tmp_path):
    db = tmp_path / "rate.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE files (
            remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,
            status TEXT NOT NULL DEFAULT 'pending', local_path TEXT,
            error TEXT, downloaded_at TIMESTAMP
        );
        INSERT INTO files VALUES ('disk:/fresh.jpg', 100, NULL, 'downloaded', 'x', NULL,
                                  strftime('%Y-%m-%dT%H:%M:%S', 'now', '-2 minutes'));
        INSERT INTO files VALUES ('disk:/old.jpg', 900, NULL, 'downloaded', 'x', NULL,
                                  strftime('%Y-%m-%dT%H:%M:%S', 'now', '-2 hours'));
        """
    )
    con.commit()
    con.close()
    out = webui.db_stats(db)
    assert out["done_10m"] == 1
    assert out["bytes_10m"] == 100


def test_pipeline_tolerates_partial_download_failure(monkeypatch):
    """exit=1 у download — «часть файлов упала», pipeline идёт дальше к безлимиту."""
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": True, "login": "t", "used": 1})
    monkeypatch.setattr(webui, "check_cookies", lambda: {"ok": True, "count": 5})
    monkeypatch.setattr(webui.tm, "start", lambda slot, script, args: (True, "ok"))
    pipe = webui.Pipeline()
    steps = []

    def fake_wait(slot, on_finish_msg=""):
        steps.append(pipe.step)
        return 1 if pipe.step == "качаю диск" else 0

    monkeypatch.setattr(pipe, "_wait_task", fake_wait)
    pipe._run()
    assert pipe.state == "done"
    assert "собираю список безлимита" in steps


def test_pipeline_fails_when_workers_exit_with_auth_error(monkeypatch):
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": True, "login": "t", "used": 1})
    monkeypatch.setattr(webui, "check_cookies", lambda: {"ok": True, "count": 5})
    monkeypatch.setattr(webui.tm, "start", lambda slot, script, args: (True, "ok"))

    class DeadProc:
        def poll(self):
            return webui.EXIT_AUTH

    class DeadTask:
        was_stopped = False
        proc = DeadProc()

        def alive(self):
            return False

    monkeypatch.setitem(webui.tm._tasks, "unlim_w1", DeadTask())
    pipe = webui.Pipeline()
    pipe.skip_disk = True
    pipe.workers = 1
    monkeypatch.setattr(pipe, "_wait_task", lambda slot, on_finish_msg="": 0)
    pipe._run()
    assert pipe.state == "failed"
    assert any("куки" in line for line in pipe.log)


def test_pipeline_unlim_only_skips_token_check(monkeypatch):
    calls = []
    monkeypatch.setattr(webui, "check_token", lambda tok: calls.append(tok) or {"ok": False, "msg": "no"})
    monkeypatch.setattr(webui, "check_cookies", lambda: {"ok": False, "msg": "нет кук"})
    pipe = webui.Pipeline()
    pipe.skip_disk = True
    pipe._run()
    assert calls == []
    assert pipe.state == "done"


def test_pipeline_applies_categories_after_scan(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE files (
            remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,
            status TEXT NOT NULL DEFAULT 'pending', local_path TEXT,
            error TEXT, downloaded_at TIMESTAMP
        );
        INSERT INTO files (remote_path, size) VALUES ('disk:/a.jpg', 1), ('disk:/b.pdf', 1);
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(webui, "DB_FILE", db)
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": True, "login": "t", "used": 1})
    monkeypatch.setattr(webui.tm, "start", lambda slot, script, args: (True, "ok"))
    pipe = webui.Pipeline()
    pipe.skip_unlim = True
    pipe.disk_categories = ["photos"]
    monkeypatch.setattr(pipe, "_wait_task", lambda slot, on_finish_msg="": 0)
    pipe._run()
    assert pipe.state == "done"
    con = sqlite3.connect(db)
    st = dict(con.execute("SELECT remote_path, status FROM files"))
    con.close()
    assert st == {"disk:/a.jpg": "pending", "disk:/b.pdf": "skipped"}


def test_api_plan_shape(monkeypatch):
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    plan = client.get("/api/plan").json()
    assert set(plan) >= {"access", "disk", "unlim", "free_space", "rate_bps", "recommended", "diagnostics"}
    assert plan["recommended"]["workers"] in (2, 4)
    assert plan["disk"]["preview"]["empty"] is True

    resp = client.post("/api/plan/start", json={"do_disk": False, "do_unlim": False})
    assert resp.json()["ok"] is False


def test_unlim_preview_categorizes_by_name(tmp_path, monkeypatch):
    db = tmp_path / "unlim_state.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE unlim_files (
            file_id TEXT PRIMARY KEY, name TEXT, size INTEGER, status TEXT DEFAULT 'pending', error TEXT
        );
        INSERT INTO unlim_files (file_id, name, size) VALUES ('abc123', 'IMG_1.jpg', 10), ('def456', 'clip.mp4', 20);
        """
    )
    con.commit()
    con.close()
    out = webui.preview_db(db, "unlim_files", "file_id")
    assert out["categories"]["photos"]["count"] == 1
    assert out["categories"]["videos"]["count"] == 1
    skipped, kept = webui.filter_to_categories(db, "unlim_files", "file_id", ["photos"])
    assert (skipped, kept) == (1, 1)


def test_unlim_retry_without_db_does_not_create_it(tmp_path, monkeypatch):
    db_file = tmp_path / "unlim_state.db"
    monkeypatch.setattr(webui, "UNLIM_DB_FILE", db_file)
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    resp = client.post("/api/unlim/retry", json={"workers": 1, "batch": 5})
    assert resp.json()["ok"] is False
    assert not db_file.exists()


def test_db_stats_detailed_breakdown(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE files (
            remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,
            status TEXT NOT NULL DEFAULT 'pending', local_path TEXT,
            error TEXT, downloaded_at TIMESTAMP
        );
        INSERT INTO files (remote_path, size, status, error) VALUES ('disk:/1.jpg', 100, 'pending', NULL);
        INSERT INTO files (remote_path, size, status, error) VALUES ('disk:/2.jpg', 200, 'in_progress', NULL);
        INSERT INTO files (remote_path, size, status, error) VALUES ('disk:/3.jpg', 300, 'failed', NULL);
        INSERT INTO files (remote_path, size, status, error) VALUES ('disk:/4.jpg', 400, 'downloaded', NULL);
        INSERT INTO files (remote_path, size, status, error) VALUES ('disk:/5.jpg', 500, 'skipped', 'not in selected categories');
        """
    )
    con.commit()
    con.close()

    stats = webui.db_stats(db)
    assert stats["bytes_pending"] == 100
    assert stats["bytes_in_progress"] == 200
    assert stats["bytes_failed"] == 300
    assert stats["bytes_done_real"] == 400
    assert stats["category_skipped_bytes"] == 500
    assert stats["bytes_selected_total"] == 1000






