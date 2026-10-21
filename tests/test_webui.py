"""Тесты серверной логики дашборда (без запуска uvicorn)."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import utils
import webapp.access
import webapp.config
import webapp.control
import webapp.diagnostics
import webapp.local_disk
import webapp.pages
import webapp.paths
import webapp.queue_db
import webapp.rescue
import webapp.status
import webapp.tasks
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
    assert utils.categorize(path) == expected


# ── маскировка токена ───────────────────────────────────────────────────────


def test_mask_token_hides_middle():
    token = "y0_AgAAAABsecretsecretsecret"
    masked = webapp.config.mask_token(token)
    assert "secretsecret" not in masked
    assert masked.startswith("y0_AgA")


def test_mask_token_empty():
    assert webapp.config.mask_token("") == ""
    assert webapp.config.mask_token("short") == ""


# ── защита organize от системных папок ──────────────────────────────────────


def test_organize_rejects_windows_dir():
    ok, msg = webapp.control.is_safe_organize_path(r"C:\Windows")
    assert ok is False
    assert "Системная" in msg


def test_organize_rejects_program_files_subdir():
    ok, _ = webapp.control.is_safe_organize_path(r"C:\Program Files\что-то")
    assert ok is False


def test_organize_rejects_missing_dir(tmp_path):
    ok, msg = webapp.control.is_safe_organize_path(str(tmp_path / "нет-такой"))
    assert ok is False
    assert "не существует" in msg


def test_organize_allows_normal_dir(tmp_path):
    ok, msg = webapp.control.is_safe_organize_path(str(tmp_path))
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
    skipped, kept = webapp.queue_db.filter_to_categories(disk_db, "files", "remote_path", ["photos"])
    assert (skipped, kept) == (2, 1)
    st = statuses(disk_db)
    assert st["disk:/a.jpg"] == "pending"
    assert st["disk:/b.mp4"] == "skipped"
    assert st["disk:/c.pdf"] == "skipped"
    assert st["disk:/d.jpg"] == "downloaded"  # скачанное не трогаем


def test_filter_is_reversible(disk_db):
    webapp.queue_db.filter_to_categories(disk_db, "files", "remote_path", ["photos"])
    n = webapp.queue_db.unskip_category_marker(disk_db, "files")
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

    webapp.queue_db.filter_to_categories(disk_db, "files", "remote_path", ["photos"])
    webapp.queue_db.unskip_category_marker(disk_db, "files")
    st = statuses(disk_db)
    assert st["disk:/c.pdf"] == "skipped"  # дубликат остался дубликатом


# ── статистика ──────────────────────────────────────────────────────────────


def test_db_stats_counts(disk_db):
    out = webapp.queue_db.db_stats(disk_db, "files", "remote_path")
    assert out["total"] == 4
    assert out["pending"] == 3
    assert out["downloaded"] == 1
    assert out["bytes_total"] == 100
    assert out["bytes_done"] == 40


def test_db_stats_missing_file(tmp_path):
    out = webapp.queue_db.db_stats(tmp_path / "нет.db", "files", "remote_path")
    assert out["total"] == 0
    assert out["recent"] == []


def test_preview_groups_by_category(disk_db):
    out = webapp.queue_db.preview_db(disk_db, "files", "remote_path")
    assert out["empty"] is False
    assert out["categories"]["photos"]["count"] == 2
    assert out["categories"]["videos"]["count"] == 1
    assert out["categories"]["documents"]["count"] == 1


def test_local_size_marks_partial_scan(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"12345")
    webapp.local_disk._local_size_cache.clear()
    out = webapp.local_disk.local_size(str(tmp_path))
    assert out["bytes"] == 5
    assert out["partial"] is False


def _tree(root):
    (root / "a" / "b").mkdir(parents=True)
    (root / "f.bin").write_bytes(b"12345")
    (root / "a" / "g.bin").write_bytes(b"123")
    (root / "a" / "b" / "h.bin").write_bytes(b"1")


def test_local_size_counts_nested_folders(tmp_path):
    _tree(tmp_path)
    webapp.local_disk._local_size_cache.clear()
    assert webapp.local_disk.local_size(str(tmp_path)) == {"files": 3, "bytes": 9, "partial": False}
    assert webapp.local_disk.local_size(str(tmp_path / "нет такой")) == {"files": 0, "bytes": 0, "partial": False}


def test_walk_size_says_when_time_ran_out(tmp_path, monkeypatch):
    # Большая папка или медленный диск: не успели — так и говорим, а не выдаём неполную цифру за полную
    _tree(tmp_path)
    ticks = iter([0.0])  # перед корнем время ещё есть, дальше часы ушли за срок
    assert webapp.local_disk._walk_size(str(tmp_path), 1.0, clock=lambda: next(ticks, 5.0)) == (1, 5, True)
    assert webapp.local_disk._walk_size(str(tmp_path), float("inf")) == (3, 9, False)
    # В _unlim десятки тысяч фото в одной папке — срок проверяется и посреди неё
    flat = tmp_path / "flat"
    flat.mkdir()
    for i in range(5):
        (flat / f"{i}.jpg").write_bytes(b"x")
    monkeypatch.setattr(webapp.local_disk, "_WALK_CLOCK_EVERY", 2)
    ticks = iter([0.0])
    files, _size, partial = webapp.local_disk._walk_size(str(flat), 1.0, clock=lambda: next(ticks, 5.0))
    assert partial and files == 1


def test_folder_size_partial_reaches_rescue_status(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp.config, "get_config", lambda: {"token": "", "download_dir": str(tmp_path), "max_workers": 4})
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": True, "login": "test"})
    (tmp_path / "f.bin").write_bytes(b"12345")
    webapp.local_disk._local_size_cache.clear()
    folder = webapp.status.api_status()["rescue"]["sources"]["disk"]["folder"]
    assert folder == {"path": str(tmp_path), "size_bytes": 5, "size_partial": False}

    monkeypatch.setattr(webapp.local_disk, "_walk_size", lambda path, deadline: (40, 40 * 1024 ** 3, True))
    webapp.local_disk._local_size_cache.clear()
    sources = webapp.status.api_status()["rescue"]["sources"]
    assert sources["disk"]["folder"]["size_partial"] is True
    assert sources["disk"]["folder"]["size_bytes"] == 40 * 1024 ** 3
    assert sources["unlim"]["folder"]["size_partial"] is False  # папки _unlim нет — считать нечего
    webapp.local_disk._local_size_cache.clear()


def test_sql_ident_whitelist_rejects_injection(disk_db):
    with pytest.raises(ValueError, match="Недопустимый table"):
        webapp.queue_db.db_stats(disk_db, "files; DROP TABLE files; --", "remote_path")

    with pytest.raises(ValueError, match="Недопустимый column"):
        webapp.queue_db.preview_db(disk_db, "files", "1=1; --")


def test_donate_redirect():
    # Экран «Поддержать», а не «Главная»: README даёт людям адрес /donate
    resp = webapp.pages.donate()
    assert resp.status_code == 307
    assert resp.headers["location"] == "/#/support"


def test_donation_url_constant():
    assert utils.DONATION_URL == "https://www.donationalerts.com/r/davronsvv"


def test_api_status_does_not_leak_raw_token(monkeypatch):
    secret = "y0_AgAAAAA_super_secret_raw_token_xyz"
    monkeypatch.setattr(webapp.config, "get_config", lambda: {
        "token": secret,
        "download_dir": "D:/downloads",
        "max_workers": 4,
    })
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": True, "login": "test"})
    st = webapp.status.api_status()
    cfg = st["config"]
    assert "token" not in cfg  # сырой токен НИКОГДА не должен отдаваться
    assert "token_masked" in cfg
    assert "secret" not in cfg["token_masked"]


def test_organize_rejects_other_drive_windows():
    ok, msg = webapp.control.is_safe_organize_path(r"D:\Windows")
    assert ok is False
    assert "Системная" in msg

    ok2, _ = webapp.control.is_safe_organize_path(r"E:\Program Files\App")
    assert ok2 is False


def test_organize_rejects_user_home_dir():
    import os
    home = os.path.expanduser("~")
    ok, msg = webapp.control.is_safe_organize_path(home)
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

    out = webapp.queue_db.db_stats(db, "files", "remote_path")
    assert out["total"] == 4
    # bytes_done должен считать downloaded (10) + resumed (30), НО НЕ cat_skip (50)
    assert out["bytes_done"] == 40
    assert out["category_skipped"] == 1
    assert out["category_skipped_bytes"] == 50


def test_pipeline_status_elapsed():
    pipe = webapp.rescue.Rescue()
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

    t = webapp.tasks.Task("test_slot", FakeProc())
    assert t.was_stopped is False
    t.stop()
    assert t.was_stopped is True


def test_get_config_safe_max_workers(monkeypatch):
    monkeypatch.setenv("MAX_WORKERS", "invalid_garbage")
    cfg = webapp.config.get_config()
    assert cfg["max_workers"] == 4

    monkeypatch.setenv("MAX_WORKERS", "100")
    cfg = webapp.config.get_config()
    assert cfg["max_workers"] == 16

    monkeypatch.setenv("MAX_WORKERS", "-10")
    cfg = webapp.config.get_config()
    assert cfg["max_workers"] == 1


def test_api_set_dir_validation(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(webapp.paths, "ENV_FILE", tmp_path / ".env")
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
    monkeypatch.setattr(webapp.paths, "COOKIES_FILE", cookies_path)
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
    monkeypatch.setattr(webapp.paths, "ENV_FILE", env_file)
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})

    target_dir = str(tmp_path / "my backup#1")
    resp = client.post("/api/config/download_dir", json={"download_dir": target_dir})
    assert resp.status_code == 200

    vals = dotenv_values(str(env_file))
    assert vals["DOWNLOAD_DIR"] == target_dir


def test_disk_clear_waits_for_task_stop(tmp_path, monkeypatch):
    db_file = tmp_path / "state.db"
    db_file.write_text("fake db content", encoding="utf-8")
    monkeypatch.setattr(webapp.paths, "DB_FILE", db_file)

    calls = []

    class MockTask:
        def alive(self):
            return True

        def stop(self):
            calls.append("stop")

        def terminate(self):
            calls.append("stop")

        def wait_or_kill(self, timeout=5.0):
            pass

        def join(self, timeout=2.0):
            calls.append("join")

    monkeypatch.setitem(webapp.tasks.tm._tasks, "disk", MockTask())

    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    resp = client.post("/api/disk/clear")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert calls == ["stop", "join"]
    assert not db_file.exists()


class _FakeProc:
    """Мёртвый процесс без вывода — чтобы собрать настоящий webapp.tasks.Task."""
    stdout = []

    def poll(self):
        return 0

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def test_clear_endpoints_work_with_real_task_objects(tmp_path, monkeypatch):
    """Раньше clear падал с AttributeError: код обращался к несуществующему Task.thread."""
    monkeypatch.setattr(webapp.paths, "DB_FILE", tmp_path / "state.db")
    monkeypatch.setattr(webapp.paths, "UNLIM_DB_FILE", tmp_path / "unlim_state.db")
    monkeypatch.setitem(webapp.tasks.tm._tasks, "disk", webapp.tasks.Task("disk", _FakeProc()))
    monkeypatch.setitem(webapp.tasks.tm._tasks, "unlim_w1", webapp.tasks.Task("unlim_w1", _FakeProc()))

    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    assert client.post("/api/disk/clear").json()["ok"] is True
    assert client.post("/api/unlim/clear").json()["ok"] is True


def test_task_manager_prefix_helpers(monkeypatch):
    class AliveTask:
        def __init__(self):
            self.stopped = False

        def alive(self):
            return not self.stopped

        def stop(self):
            self.stopped = True

        def terminate(self):
            self.stopped = True

        def wait_or_kill(self, timeout=5.0):
            pass

    w1, w2, collect = AliveTask(), AliveTask(), AliveTask()
    monkeypatch.setitem(webapp.tasks.tm._tasks, "unlim_w1", w1)
    monkeypatch.setitem(webapp.tasks.tm._tasks, "unlim_w2", w2)
    monkeypatch.setitem(webapp.tasks.tm._tasks, "unlim_collect", collect)

    assert webapp.tasks.tm.any_alive("unlim_w") is True

    stopped = webapp.tasks.tm.stop_matching("unlim_w")
    assert {id(t) for t in stopped} == {id(w1), id(w2)}
    assert collect.stopped is False
    assert webapp.tasks.tm.any_alive("unlim_w") is False


def test_launch_unlim_workers_caps_values():
    launched = []

    def starter(slot, script, args):
        launched.append((slot, args))
        return True, "ok"

    res = webapp.rescue.launch_unlim_workers(100, 10_000, starter=starter)
    assert res["started"] == list(range(1, utils.MAX_UNLIM_WORKERS + 1))
    assert all(args[-1] == str(utils.MAX_UNLIM_BATCH) for _, args in launched)
    assert launched[0][0] == "unlim_w1"


def test_get_from_foreign_host_is_blocked():
    """DNS-rebinding: чужой Host не должен читать /api/status."""
    client = TestClient(webui.app, headers={"Host": "evil.example"})
    assert client.get("/api/status").status_code == 403
    ok_client = TestClient(webui.app, headers={"Host": "127.0.0.1:8000"})
    assert ok_client.get("/api/disk/preview").status_code == 200


def test_organize_rejects_project_dir():
    ok, msg = webapp.control.is_safe_organize_path(str(webapp.paths.PROJECT))
    assert ok is False
    assert "инструмента" in msg
    ok, _ = webapp.control.is_safe_organize_path(str(webapp.paths.PROJECT.parent))
    assert ok is False


def test_organize_move_refused_while_downloading(tmp_path, monkeypatch):
    class Alive:
        def alive(self):
            return True

    monkeypatch.setitem(webapp.tasks.tm._tasks, "unlim_w1", Alive())
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    resp = client.post("/api/organize", json={"input": str(tmp_path), "action": "move"})
    assert resp.json()["ok"] is False
    assert "остановите" in resp.json()["msg"]


class _LogOnlyTask:
    def __init__(self, lines, finished_ago=0.0):
        import time
        self.log = list(lines)
        self.finished_at = time.time() - finished_ago

    def alive(self):
        return False


def test_diagnose_explains_bulk_disabled(monkeypatch):
    monkeypatch.setitem(webapp.tasks.tm._tasks, "unlim_w1", _LogOnlyTask([
        "12:00:01  [w1] batch 3: ⚠️ Сбой отдачи архива узлом Яндекса (1 файлов): not_zip | вместо ZIP пришёл text/html",
        "12:00:40  [w1] bulk_unavailable | Яндекс 5 пачек подряд не отдал архив.",
    ]))
    diag = webapp.diagnostics.diagnose_tasks()
    assert diag["kind"] == "bulk_disabled"
    assert diag["counts"]["bulk_disabled"] == 2
    assert diag["affects"][0]["state"] == "ok"      # диск работает
    assert diag["affects"][1]["state"] == "wait"    # безлимит ждёт
    assert diag["links"]


def test_diagnose_auth_has_priority_and_keeps_legacy_flags(monkeypatch):
    monkeypatch.setitem(webapp.tasks.tm._tasks, "disk", _LogOnlyTask([
        "FAIL | server_50x | BadGatewayError", "FAIL | auth_expired | UnauthorizedError",
    ]))
    diag = webapp.diagnostics.diagnose_tasks()
    assert diag["kind"] == "auth_expired"
    assert diag["auth_expired_detected"] is True
    assert diag["server_50x_detected"] is False
    assert diag["counts"]["server_50x"] == 1


def test_diagnose_local_disk_is_not_blamed_on_yandex(monkeypatch):
    monkeypatch.setitem(webapp.tasks.tm._tasks, "disk", _LogOnlyTask([
        "FAIL | local_disk | OSError: [Errno 28] No space left on device: 'IMG_5003.jpg.part'",
    ]))
    assert webapp.diagnostics.diagnose_tasks()["kind"] == "local_disk"


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
    out = webapp.queue_db.db_stats(db)
    assert out["done_10m"] == 1
    assert out["bytes_10m"] == 100


def test_pipeline_unlim_only_skips_token_check(monkeypatch):
    """Только безлимит и нет cookies: токен не проверяем, и честно падаем — скачивать нечего."""
    calls = []
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: calls.append(tok) or {"ok": False, "msg": "no"})
    monkeypatch.setattr(webapp.access, "check_cookies", lambda: {"ok": False, "msg": "нет кук"})
    pipe = webapp.rescue.Rescue()
    pipe.skip_disk = True
    pipe._run()
    assert calls == []
    assert pipe.state == "failed"
    assert any("Для Безлимита нужен доступ" in line for line in pipe.log)


def test_plan_sources_matrix():
    ps = webapp.access.plan_sources
    full = ps(True, True, True, True)
    assert (full["run_disk"], full["run_unlim"], full["notes"], full["error"]) == (True, True, [], None)

    no_token = ps(True, True, False, True)
    assert (no_token["run_disk"], no_token["run_unlim"]) == (False, True)
    assert "пропускаю" in no_token["notes"][0] and no_token["error"] is None

    no_cookies = ps(True, True, True, False)
    assert (no_cookies["run_disk"], no_cookies["run_unlim"]) == (True, False)
    assert "Безлимит пропускаю" in no_cookies["notes"][0]

    nothing = ps(True, True, False, False)
    assert nothing["error"] and "ни ключа доступа к Диску, ни доступа к Безлимиту" in nothing["error"]
    assert "ключ доступа" in ps(True, False, False, True)["error"]
    assert "Доступ к Безлимиту" in ps(False, True, True, False)["error"]
    assert ps(False, False, True, True)["error"]


def test_pipeline_without_token_downloads_unlim_only(monkeypatch):
    """Ровно то, что было в логе: «Скачать всё» без токена, но с cookies — раньше падало целиком."""
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    monkeypatch.setattr(webapp.access, "check_cookies", lambda: {"ok": True, "msg": "Куки OK (23 шт)", "count": 23})
    started = []
    monkeypatch.setattr(webapp.tasks.tm, "start", lambda slot, script, args: (started.append(slot), (True, "ok"))[1])
    pipe = webapp.rescue.Rescue()
    monkeypatch.setattr(pipe, "_wait_task", lambda slot, on_finish_msg="": 0)
    pipe._run()
    assert pipe.state == "done"
    assert pipe.partial is True
    assert "disk" not in started and "unlim_collect" in started
    assert any("обычный Диск пропускаю" in line for line in pipe.log)
    assert pipe.status()["partial"] is True


def test_rescue_start_refuses_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    resp = client.post("/api/rescue/start")  # по умолчанию включены оба источника
    body = resp.json()
    assert body["ok"] is False
    assert "ни ключа доступа к Диску, ни доступа к Безлимиту" in body["msg"]
    assert webapp.rescue.rescue.is_running() is False


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
    monkeypatch.setattr(webapp.paths, "DB_FILE", db)
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": True, "login": "t", "used": 1})
    monkeypatch.setattr(webapp.tasks.tm, "start", lambda slot, script, args: (True, "ok"))
    pipe = webapp.rescue.Rescue()
    pipe.skip_unlim = True
    pipe.disk_categories = ["photos"]

    def finish(slot, on_finish_msg=""):
        # «Скачивание» забирает всю очередь, как настоящий main.py download
        if "скачиваю" in on_finish_msg:
            with sqlite3.connect(db) as c:
                c.execute("UPDATE files SET status='downloaded' WHERE status='pending'")
        return 0

    monkeypatch.setattr(pipe, "_wait_task", finish)
    pipe._run()
    assert pipe.state == "done"
    con = sqlite3.connect(db)
    st = dict(con.execute("SELECT remote_path, status FROM files"))
    con.close()
    # Фото выбраны и скачаны, документ снят выбором категорий и не качался
    assert st == {"disk:/a.jpg": "downloaded", "disk:/b.pdf": "skipped"}


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
    out = webapp.queue_db.preview_db(db, "unlim_files", "file_id")
    assert out["categories"]["photos"]["count"] == 1
    assert out["categories"]["videos"]["count"] == 1
    skipped, kept = webapp.queue_db.filter_to_categories(db, "unlim_files", "file_id", ["photos"])
    assert (skipped, kept) == (1, 1)


def test_onboarding_state_next_step():
    cfg_unset = {"download_dir_set": False}
    empty = {"total": 0, "downloaded": 0}
    st = webapp.access.onboarding_state(cfg_unset, {"ok": False}, {"ok": False}, empty, empty)
    assert st["next"] == "token" and st["token"] is False
    st = webapp.access.onboarding_state(cfg_unset, {"ok": True}, {"ok": False}, empty, empty)
    assert st["next"] == "dir"
    st = webapp.access.onboarding_state({"download_dir_set": True}, {"ok": True}, {"ok": False}, empty, empty)
    assert st["next"] == "cookies"
    st = webapp.access.onboarding_state({"download_dir_set": True}, {"ok": True}, {"ok": True},
                                {"total": 10, "downloaded": 0}, empty)
    assert st["next"] == "download" and st["scanned"] is True
    st = webapp.access.onboarding_state({"download_dir_set": True}, {"ok": True}, {"ok": True},
                                {"total": 10, "downloaded": 10}, empty)
    assert st["next"] == "done"


def test_status_exposes_onboarding_and_version(monkeypatch):
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    st = webapp.status.api_status()
    assert st["app_version"] == utils.APP_VERSION
    assert st["onboarding"]["next"] == "token"
    assert st["config"]["download_dir_set"] is False


def test_task_writes_log_file(tmp_path):
    class Proc:
        stdout = ["первая строка\n", "12:00:00  вторая\n"]
        pid = 1

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    log_file = tmp_path / "logs" / "tasks" / "disk.log"
    t = webapp.tasks.Task("disk", Proc(), log_file=log_file, cmd=["main.py", "scan"])
    t.thread.join(timeout=2)
    text = log_file.read_text(encoding="utf-8")
    assert "старт: main.py scan" in text
    assert "первая строка" in text and "вторая" in text
    assert "конец: exit=0" in text
    assert t.snapshot()["log_file"] == "disk.log"


def test_logs_api_lists_tails_and_rejects_traversal(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    (logs / "tasks").mkdir(parents=True)
    (logs / "webui.log").write_text("a\nb\nc\n", encoding="utf-8")
    (logs / "tasks" / "disk.log").write_text("\n".join(f"line{i}" for i in range(500)) + "\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})

    names = {entry["name"] for entry in client.get("/api/logs").json()["logs"]}
    assert names == {"webui.log", "tasks/disk.log"}

    tail = client.get("/api/logs/tail", params={"name": "tasks/disk.log", "lines": 3}).json()
    assert tail["lines"] == ["line497", "line498", "line499"]
    assert client.get("/api/logs/tail", params={"name": "webui.log"}).json()["lines"] == ["a", "b", "c"]

    assert client.get("/api/logs/tail", params={"name": "../secret.txt"}).status_code == 400
    assert client.get("/api/logs/tail", params={"name": "tasks/../../secret.txt"}).status_code == 400
    assert client.get("/api/logs/tail", params={"name": "missing.log"}).status_code == 404


def test_logs_bundle_contains_logs_and_support_info(tmp_path, monkeypatch):
    import io

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unlim_worker.log").write_text("w1 b1 download: not_zip\n", encoding="utf-8")
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    resp = client.get("/api/logs/bundle")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")
    import zipfile
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        assert "unlim_worker.log" in names and "support-info.json" in names
        info = json.loads(zf.read("support-info.json"))
    assert info["app_version"] == utils.APP_VERSION
    assert "token" not in info["config"]          # сырой токен не уходит
    assert info["config"]["token_ok"] is False


def test_guide_renders_instruction_with_working_toc():
    """Инструкция открывается в программе: якоря оглавления совпадают с заголовками."""
    import re as _re

    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    resp = client.get("/guide")
    assert resp.status_code == 200
    html = resp.text
    assert "Шаг 4. Получите токен" in html and "webui.command" in html
    assert "[!NOTE]" not in html and "flowchart" not in html
    assert 'class="note"' in html
    ids = set(_re.findall(r'id="([^"]+)"', html))
    toc = {h[1:] for h in _re.findall(r'href="(#[^"]+)"', html)}
    assert toc and toc <= ids, sorted(toc - ids)


def test_guide_and_readme_images_exist_and_are_served():
    """Картинки инструкции: те же пути docs/images/… работают на GitHub и в /guide."""
    import re as _re

    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    srcs = [src for src in _re.findall(r'<img[^>]+src="([^"]+)"', client.get("/guide").text)
            if not src.startswith("https://")]
    assert len(srcs) >= 3 and all(src.startswith("docs/images/") for src in srcs)
    for src in srcs:
        resp = client.get("/" + src)
        assert resp.status_code == 200 and resp.headers["content-type"] == "image/webp", src
    assert client.get("/docs/images/nope.webp").status_code == 404
    assert client.get("/docs/images/..%2F..%2F.env").status_code == 404
    assert client.get("/docs/images/README.md").status_code == 404
    readme = (webapp.paths.PROJECT / "README.md").read_text(encoding="utf-8")
    for path in set(_re.findall(r"docs/images/[\w.-]+", readme)):
        assert (webapp.paths.PROJECT / path).is_file(), path


def test_ui_has_no_bare_instruction_filename_links():
    """«INSTRUCTION_GET_TOKEN.md» как текст ссылки: Telegram и браузер считают .md доменом."""
    assets = webapp.paths.TEMPLATES_DIR
    text = (assets / "app.html").read_text(encoding="utf-8") + "".join(
        p.read_text(encoding="utf-8") for p in sorted((assets / "static" / "js").rglob("*.js")))
    assert ">INSTRUCTION_GET_TOKEN.md<" not in text
    assert 'href="/guide"' in text


def test_unlim_retry_without_db_does_not_create_it(tmp_path, monkeypatch):
    db_file = tmp_path / "unlim_state.db"
    monkeypatch.setattr(webapp.paths, "UNLIM_DB_FILE", db_file)
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    resp = client.post("/api/rescue/retry_failed", json={"source": "unlim"})
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

    stats = webapp.queue_db.db_stats(db)
    assert stats["bytes_pending"] == 100
    assert stats["bytes_in_progress"] == 200
    assert stats["bytes_failed"] == 300
    assert stats["bytes_done_real"] == 400
    assert stats["category_skipped_bytes"] == 500
    assert stats["bytes_selected_total"] == 1000






