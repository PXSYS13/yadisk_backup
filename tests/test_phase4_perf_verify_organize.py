"""Фаза 4: индексы БД, инкрементальный verify_cache, синхронизация путей organize.py ↔ БД, кэш .env."""

from __future__ import annotations

import hashlib
import os
import sqlite3

import organize
import unlim_common
import verify
import webapp.config
import webapp.paths
from state import State


def test_state_creates_performance_indexes_and_immediate_dated(tmp_path):
    with State(str(tmp_path / "state.db")) as st:
        idx = {
            r[0]
            for r in st._conn().execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"idx_files_status_size", "idx_files_previews", "idx_files_undated"} <= idx

        ts = 1_650_000_000.0
        st.mark_pending_batch([("disk:/a.jpg", 4, "a" * 32, ts)])
        st.mark_downloaded("disk:/a.jpg", str(tmp_path / "a.jpg"), dated=ts)
        # Дата уже выставлена при скачивании — повторный проход dates_to_check строку не берёт
        assert list(st.dates_to_check()) == []


def test_verify_cache_skips_md5_reread_on_unchanged_file(tmp_path, monkeypatch):
    f = tmp_path / "a.bin"
    data = b"hello world"
    f.write_bytes(data)
    md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
    item = verify.Item("disk:/a.bin", str(f), len(data), md5)

    with State(str(tmp_path / "state.db")) as st:
        s1, p1 = verify.check_files([item], cache=st.get_verify_cache(), save_cache=st.save_verify_cache)
        assert s1["ok"] == 1 and s1["cached"] == 0 and not p1

        # Повторная проверка: _md5 не должен вызываться для неизменившегося файла
        called = []
        monkeypatch.setattr(verify, "_md5", lambda *a, **k: (called.append(a), md5)[1])
        s2, p2 = verify.check_files([item], cache=st.get_verify_cache(), save_cache=st.save_verify_cache)
        assert s2["ok"] == 1 and s2["cached"] == 1 and not p2
        assert called == []

        # Изменяем mtime файла — кэш промахивается и пересчитывает MD5
        st_info = f.stat()
        os.utime(f, ns=(st_info.st_atime_ns, st_info.st_mtime_ns + 5_000_000_000))
        s3, p3 = verify.check_files([item], cache=st.get_verify_cache(), save_cache=st.save_verify_cache)
        assert s3["ok"] == 1 and s3["cached"] == 0 and not p3
        assert len(called) == 1


def test_unlim_verify_cache_roundtrip(tmp_path):
    db = str(tmp_path / "unlim_state.db")
    unlim_common.ensure_unlim_schema(db)
    unlim_common.save_unlim_verify_cache([(str(tmp_path / "x.jpg"), 10, 123456, "a" * 32)], db)
    cache = unlim_common.get_unlim_verify_cache(db)
    assert len(cache) == 1
    assert next(iter(cache.values())) == (10, 123456, "a" * 32)


def test_organize_sync_moved_paths_updates_state_and_unlim_dbs(tmp_path):
    old_disk = str(tmp_path / "dl" / "Папка" / "doc.pdf")
    new_disk = str(tmp_path / "dl" / "Документы" / "doc.pdf")
    old_unlim = str(tmp_path / "dl" / "_unlim" / "img.jpg")
    new_unlim = str(tmp_path / "dl" / "Фото" / "2023" / "2023-05" / "img.jpg")

    with State(str(tmp_path / "state.db")) as st:
        st.mark_pending("disk:/Папка/doc.pdf", 10, "a" * 32)
        st.mark_downloaded("disk:/Папка/doc.pdf", old_disk)

    u_db = tmp_path / "unlim_state.db"
    c = sqlite3.connect(str(u_db))
    c.executescript(unlim_common.UNLIM_SCHEMA)
    c.execute("INSERT INTO unlim_files (file_id, status, local_path) VALUES ('/photounlim/img.jpg', 'downloaded', ?)",
              (old_unlim,))
    c.commit()
    c.close()

    n = organize.sync_moved_paths_in_dbs([(old_disk, new_disk), (old_unlim, new_unlim)], db_dir=tmp_path)
    assert n == 2

    with State(str(tmp_path / "state.db")) as st:
        rows = list(st.get_all_downloaded())
        assert rows[0]["local_path"] == new_disk

    c = sqlite3.connect(str(u_db))
    row = c.execute("SELECT local_path FROM unlim_files WHERE file_id='/photounlim/img.jpg'").fetchone()
    c.close()
    assert row[0] == new_unlim


def test_config_dotenv_cache_invalidates_on_env_set():
    webapp.config.env_set("YA_DISK_TOKEN", "y0_first")
    assert webapp.config.get_config()["token"] == "y0_first"
    webapp.config.env_set_verified("YA_DISK_TOKEN", "y0_second")
    assert webapp.config.get_config()["token"] == "y0_second"
