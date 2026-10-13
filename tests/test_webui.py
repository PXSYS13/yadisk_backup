"""Тесты серверной логики дашборда (без запуска uvicorn)."""

import sqlite3

import pytest

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



