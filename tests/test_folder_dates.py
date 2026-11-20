"""Фаза 3: синхронизация дат создания и изменения папок и файлов (Диск и публичные ссылки)."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import downloader
import file_dates
import public_download as pd
from state import State
from utils import IS_WINDOWS

T_2019 = datetime(2019, 5, 10, 12, 0, 0, tzinfo=timezone.utc).timestamp()
T_2020 = datetime(2020, 8, 15, 9, 30, 0, tzinfo=timezone.utc).timestamp()
T_2021 = datetime(2021, 11, 20, 18, 0, 0, tzinfo=timezone.utc).timestamp()
T_2022 = datetime(2022, 3, 1, 7, 15, 0, tzinfo=timezone.utc).timestamp()


def _creation_time(st: os.stat_result) -> float | None:
    if hasattr(st, "st_birthtime"):
        return st.st_birthtime
    if IS_WINDOWS:
        return st.st_ctime
    return None


def test_set_file_time_sets_creation_and_modified_times(tmp_path):
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"img")
    assert file_dates.set_file_time(str(f), T_2021, created=T_2019)
    st = f.stat()
    assert abs(st.st_mtime - T_2021) < 2
    ct = _creation_time(st)
    if ct is not None:
        assert abs(ct - T_2019) < 2

    # Без created: CreationTime не новее LastWriteTime (равен modified)
    f2 = tmp_path / "doc.pdf"
    f2.write_bytes(b"pdf")
    assert file_dates.set_file_time(str(f2), T_2020)
    st2 = f2.stat()
    assert abs(st2.st_mtime - T_2020) < 2
    ct2 = _creation_time(st2)
    if ct2 is not None:
        assert abs(ct2 - T_2020) < 2


def test_set_dir_time_and_bottom_up_apply_folder_dates(tmp_path):
    parent = tmp_path / "Фото"
    child = parent / "2020"
    deep = child / "Лето"
    deep.mkdir(parents=True)
    missing = tmp_path / "НеСуществует"
    reg = tmp_path / "file.txt"
    reg.write_bytes(b"x")

    assert not file_dates.set_dir_time(str(missing), created=T_2019, modified=T_2020)
    assert not missing.exists()
    assert not file_dates.set_dir_time(str(reg), created=T_2019, modified=T_2020)

    updated = file_dates.apply_folder_dates([
        (str(parent), T_2019, T_2022),
        (str(deep), T_2020, T_2021),
        (str(child), T_2019, T_2020),
        (str(missing), T_2019, T_2020),
    ])
    assert updated == 3
    assert not missing.exists()

    for folder, exp_c, exp_m in ((parent, T_2019, T_2022), (child, T_2019, T_2020), (deep, T_2020, T_2021)):
        st = folder.stat()
        assert abs(st.st_mtime - exp_m) < 2
        ct = _creation_time(st)
        if ct is not None:
            assert abs(ct - exp_c) < 2


def test_state_folders_schema_migration_and_fallback(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT, "
                "status TEXT NOT NULL DEFAULT 'pending', local_path TEXT, error TEXT, downloaded_at TIMESTAMP)")
    con.commit()
    con.close()

    with State(str(db)) as st:
        cols = {r[1] for r in st._conn().execute("PRAGMA table_info(files)")}
        assert "created" in cols
        st.mark_pending_batch([
            ("disk:/Архив/2020/a.jpg", 10, "a" * 32, T_2021, T_2020),
            ("disk:/Архив/2020/b.jpg", 10, "b" * 32, T_2022, T_2019),
        ])
        assert st.unique_parent_folders() == {"disk:/Архив", "disk:/Архив/2020"}

        fb = st.derive_folder_dates_from_files()
        assert fb["disk:/Архив/2020"] == (T_2019, T_2022)
        assert fb["disk:/Архив"] == (T_2019, T_2022)

        st.save_folders_batch([("disk:/Архив", T_2019, T_2020)])
        assert st.get_folder_dates() == {"disk:/Архив": (T_2019, T_2020)}
        assert st.known_folder_paths() == {"disk:/Архив"}


class _Entry:
    def __init__(self, path: str, etype: str, *, size=10, md5="a" * 32, created=None, modified=None):
        self.path = path
        self.name = path.rstrip("/").rsplit("/", 1)[-1]
        self.type = etype
        self.size = size
        self.md5 = md5
        self.share = None
        self.created = datetime.fromtimestamp(created, tz=timezone.utc) if created else None
        self.modified = datetime.fromtimestamp(modified, tz=timezone.utc) if modified else None


def test_hybrid_folder_date_collection_and_restore(tmp_path):
    class _Client:
        def __init__(self):
            self.flat = [
                _Entry("disk:/Фото/2020/img.jpg", "file", created=T_2020, modified=T_2021),
                _Entry("disk:/Доки/НетДоступа/doc.pdf", "file", created=T_2021, modified=T_2022),
            ]
            self.dirs = {
                "disk:/": [
                    _Entry("disk:/Фото", "dir", created=T_2019, modified=T_2022),
                    _Entry("disk:/Доки", "dir", created=T_2020, modified=T_2022),
                    _Entry("disk:/Пустая", "dir", created=T_2019, modified=T_2019),
                ],
                "disk:/Фото": [
                    _Entry("disk:/Фото/2020", "dir", created=T_2019, modified=T_2020),
                ],
            }

        def get_files(self, offset=0, limit=200, max_items=None, **kw):
            return iter(self.flat[offset:offset + limit])

        def listdir(self, path, **kw):
            if path not in self.dirs:
                raise RuntimeError("temporary error")
            return iter(self.dirs[path])

    dl = tmp_path / "dl"
    (dl / "Фото" / "2020").mkdir(parents=True)
    (dl / "Доки" / "НетДоступа").mkdir(parents=True)

    with State(str(tmp_path / "state.db")) as st:
        downloader.scan_tree(_Client(), st, root="/")
        exact = st.get_folder_dates()
        assert exact["disk:/Фото"] == (T_2019, T_2022)
        assert exact["disk:/Фото/2020"] == (T_2019, T_2020)
        assert "disk:/Доки/НетДоступа" not in exact

        updated = downloader.restore_folder_dates(st, str(dl))
        assert updated == 4
        # Пустая папка не создаётся автоматически
        assert not (dl / "Пустая").exists()
        # Точные даты из API
        assert abs((dl / "Фото" / "2020").stat().st_mtime - T_2020) < 2
        assert abs((dl / "Фото").stat().st_mtime - T_2022) < 2
        # Запасные даты из файлов для папки, чей listdir упал
        assert abs((dl / "Доки" / "НетДоступа").stat().st_mtime - T_2022) < 2
        ct = _creation_time((dl / "Доки" / "НетДоступа").stat())
        if ct is not None:
            assert abs(ct - T_2021) < 2


def test_public_links_sync_file_and_folder_dates(tmp_path, monkeypatch):
    conn = pd.connect_db(tmp_path / "links.db")
    try:
        link_id, _ = pd.add_link(conn, "https://disk.yandex.ru/d/TestKey", "/", {"name": "Отпуск", "type": "dir"})
        link = conn.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone()

        class _Api:
            def meta(self, public_url, path="/", limit=None, offset=0, preview_size=None):
                if path == "/":
                    return {
                        "type": "dir", "name": "Отпуск",
                        "created": "2019-05-10T12:00:00+00:00",
                        "modified": "2022-03-01T07:15:00+00:00",
                        "_embedded": {
                            "total": 1,
                            "items": [
                                {"type": "dir", "path": "/День1",
                                 "created": "2020-08-15T09:30:00+00:00",
                                 "modified": "2021-11-20T18:00:00+00:00"},
                            ],
                        },
                    }
                return {
                    "type": "dir", "name": "День1",
                    "_embedded": {
                        "total": 1,
                        "items": [
                            {"type": "file", "path": "/День1/sea.jpg", "size": 5, "md5": None,
                             "created": "2020-08-15T09:30:00+00:00",
                             "modified": "2021-11-20T18:00:00+00:00"},
                        ],
                    },
                }

            def download_href(self, public_url, path):
                return "https://downloader.disk.yandex.ru/disk/sea"

        pd.list_link(_Api(), conn, link)
        row = conn.execute("SELECT file_time, created FROM link_files WHERE path='/День1/sea.jpg'").fetchone()
        assert abs(row["file_time"] - T_2021) < 2
        assert abs(row["created"] - T_2020) < 2

        dl = tmp_path / "downloads"
        dl.mkdir()
        monkeypatch.setattr(pd, "_thread_api", lambda: _Api())
        monkeypatch.setattr(pd, "_thread_http", lambda: None)

        def fake_fetch(http, href, part, **kw):
            Path(part).write_bytes(b"12345")
            return 5, "s1.storage.yandex.net", None

        monkeypatch.setattr(pd, "fetch_to_file", fake_fetch)
        r = pd.Round(conn, str(dl), workers=1, dead=pd.DeadHosts())
        r.run()

        local_file = dl / "_ссылки" / "Отпуск" / "День1" / "sea.jpg"
        assert local_file.is_file()
        assert abs(local_file.stat().st_mtime - T_2021) < 2
        ct_f = _creation_time(local_file.stat())
        if ct_f is not None:
            assert abs(ct_f - T_2020) < 2

        day_dir = dl / "_ссылки" / "Отпуск" / "День1"
        root_dir = dl / "_ссылки" / "Отпуск"
        assert abs(day_dir.stat().st_mtime - T_2021) < 2
        assert abs(root_dir.stat().st_mtime - T_2022) < 2
        ct_r = _creation_time(root_dir.stat())
        if ct_r is not None:
            assert abs(ct_r - T_2019) < 2
    finally:
        conn.close()
