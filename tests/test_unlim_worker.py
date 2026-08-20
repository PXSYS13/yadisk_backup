"""Тесты воркера безлимита: распаковка ZIP и учёт того, что реально приехало."""

import os
import sqlite3
import zipfile

import pytest

import unlim_worker as uw


# ── защита от zip-slip ──────────────────────────────────────────────────────


def test_safe_extract_normal_name(tmp_path):
    base = str(tmp_path)
    dst = uw.safe_extract_dst(base, "2020/photo.jpg")
    assert dst == os.path.join(base, "2020", "photo.jpg")


def test_safe_extract_strips_parent_refs(tmp_path):
    base = str(tmp_path / "extract")
    os.makedirs(base)
    dst = uw.safe_extract_dst(base, "../../evil.txt")
    assert dst == os.path.join(base, "evil.txt")


def test_safe_extract_rejects_drive_letter(tmp_path):
    """'C:/evil.txt' — os.path.join просто выбросил бы базу и записал в корень диска."""
    base = str(tmp_path / "extract")
    os.makedirs(base)
    dst = uw.safe_extract_dst(base, "C:/evil.txt")
    assert dst == os.path.join(base, "evil.txt")


def test_safe_extract_rejects_absolute(tmp_path):
    base = str(tmp_path / "extract")
    os.makedirs(base)
    dst = uw.safe_extract_dst(base, "/etc/passwd")
    assert dst == os.path.join(base, "etc", "passwd")


def test_safe_extract_result_always_inside_base(tmp_path):
    base = str(tmp_path / "extract")
    os.makedirs(base)
    for name in ["../x", r"..\..\x", "C:/x", "//server/share/x", "./x", "a/../../x"]:
        dst = uw.safe_extract_dst(base, name)
        if dst is None:
            continue
        assert os.path.commonpath([os.path.realpath(base), os.path.realpath(dst)]) \
            == os.path.realpath(base), name


def test_safe_extract_empty_name(tmp_path):
    assert uw.safe_extract_dst(str(tmp_path), "../..") is None
    assert uw.safe_extract_dst(str(tmp_path), "") is None


# ── сопоставление «что просили» ↔ «что приехало» ────────────────────────────


def test_split_marks_only_extracted_files():
    batch = [
        {"file_id": "1", "name": "a.jpg", "size": 100},
        {"file_id": "2", "name": "b.jpg", "size": 200},
        {"file_id": "3", "name": "c.jpg", "size": 300},
    ]
    done, missing = uw.split_by_extracted(batch, [("a.jpg", 100), ("c.jpg", 300)])
    assert done == ["1", "3"]
    assert missing == ["2"]


def test_split_all_missing_when_zip_empty():
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    done, missing = uw.split_by_extracted(batch, [])
    assert done == []
    assert missing == ["1"]


def test_split_respects_multiplicity():
    """Два одинаковых файла в пачке требуют двух записей в архиве."""
    batch = [
        {"file_id": "1", "name": "a.jpg", "size": 100},
        {"file_id": "2", "name": "a.jpg", "size": 100},
    ]
    done, missing = uw.split_by_extracted(batch, [("a.jpg", 100)])
    assert done == ["1"]
    assert missing == ["2"]


def test_split_size_must_match():
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    done, missing = uw.split_by_extracted(batch, [("a.jpg", 999)])
    assert done == []
    assert missing == ["1"]


def test_split_ignores_zip_subfolders():
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    done, missing = uw.split_by_extracted(batch, [("2020/a.jpg", 100)])
    assert done == ["1"]
    assert missing == []


# ── БД: попытки и переход в failed ──────────────────────────────────────────


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "unlim_state.db")
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE unlim_files (
            file_id TEXT PRIMARY KEY, cluster_id TEXT, name TEXT, size INTEGER,
            md5 TEXT, mime TEXT, status TEXT DEFAULT 'pending', local_path TEXT,
            error TEXT, worker_id INTEGER, assigned_at TIMESTAMP,
            downloaded_at TIMESTAMP
        );
        """
    )
    con.executemany(
        "INSERT INTO unlim_files (file_id, name, size) VALUES (?, ?, ?)",
        [("1", "a.jpg", 10), ("2", "b.jpg", 20)],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(uw, "DB_FILE", path)
    return path


def status_of(db_path, file_id):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT status, attempts FROM unlim_files WHERE file_id=?", (file_id,)
        ).fetchone()
    finally:
        con.close()


def test_migration_adds_attempts_column(db):
    uw.ensure_attempts_column()
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(unlim_files)")}
    con.close()
    assert "attempts" in cols


def test_take_batch_returns_names_and_sizes(db):
    uw.ensure_attempts_column()
    batch = uw.take_batch(worker_id=1, size=10)
    assert {it["file_id"] for it in batch} == {"1", "2"}
    assert all("name" in it and "size" in it for it in batch)
    # повторный вызов ничего не отдаёт — файлы уже in_progress
    assert uw.take_batch(worker_id=2, size=10) == []


def test_mark_pending_counts_attempts_then_fails(db):
    uw.ensure_attempts_column()
    for expected_attempt in (1, 2):
        uw.mark_pending(["1"], "boom")
        status, attempts = status_of(db, "1")
        assert (status, attempts) == ("pending", expected_attempt)

    uw.mark_pending(["1"], "boom")
    status, attempts = status_of(db, "1")
    assert status == "failed"          # больше не крутится в бесконечном цикле
    assert attempts == uw.MAX_ATTEMPTS


def test_mark_done(db):
    uw.ensure_attempts_column()
    uw.mark_done(["1"])
    status, _ = status_of(db, "1")
    assert status == "downloaded"


# ── интеграция: злой архив не вылезает из папки ─────────────────────────────


def test_download_and_extract_contains_malicious_zip(tmp_path, monkeypatch):
    extract_dir = tmp_path / "extract"
    zip_dir = tmp_path / "zips"
    monkeypatch.setattr(uw, "EXTRACT_DIR", str(extract_dir))
    monkeypatch.setattr(uw, "ZIP_TMP_DIR", str(zip_dir))

    src_zip = tmp_path / "payload.zip"
    with zipfile.ZipFile(src_zip, "w") as zf:
        zf.writestr("ok.jpg", b"good")
        zf.writestr("../../escaped.txt", b"bad")
        zf.writestr("C:/escaped2.txt", b"bad")

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield self._data

    class FakeSession:
        def get(self, url, stream=False, timeout=None):
            return FakeResponse(src_zip.read_bytes())

    extracted, total = uw.download_and_extract(FakeSession(), "http://x", 1, 1)

    names = sorted(n for n, _ in extracted)
    assert names == ["escaped.txt", "escaped2.txt", "ok.jpg"]
    # всё легло внутрь extract_dir и никуда больше
    assert (extract_dir / "ok.jpg").exists()
    assert (extract_dir / "escaped.txt").exists()
    assert (extract_dir / "escaped2.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert total == 10  # 4 ("good") + 3 + 3


def test_split_falls_back_to_size_when_zip_renamed():
    """Яндекс может переименовать файл в архиве — по размеру всё равно засчитываем."""
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    done, missing = uw.split_by_extracted(batch, [("a (1).jpg", 100)])
    assert done == ["1"]
    assert missing == []


def test_split_fallback_does_not_invent_files():
    """Если в архиве нет файла подходящего размера — честно считаем непришедшим."""
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    done, missing = uw.split_by_extracted(batch, [("b.jpg", 7)])
    assert done == []
    assert missing == ["1"]


def test_split_fallback_respects_counts():
    batch = [
        {"file_id": "1", "name": "a.jpg", "size": 100},
        {"file_id": "2", "name": "b.jpg", "size": 100},
    ]
    done, missing = uw.split_by_extracted(batch, [("renamed.jpg", 100)])
    assert done == ["1"]
    assert missing == ["2"]
