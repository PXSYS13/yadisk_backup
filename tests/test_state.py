"""Тесты SQLite-трекера прогресса."""

import pytest

from state import State


@pytest.fixture()
def st(tmp_path):
    s = State(str(tmp_path / "state.db"))
    yield s
    s.close()


def test_mark_pending_and_stats(st):
    st.mark_pending("disk:/a.jpg", 100, "md5a")
    st.mark_pending("disk:/b.jpg", 200, None)
    stats = st.get_stats()
    assert stats["pending"] == 2
    assert stats["total"] == 2
    assert stats["bytes_total"] == 300
    assert stats["bytes_downloaded"] == 0


def test_downloaded_file_is_not_reset_by_rescan(st):
    """Повторный scan не должен возвращать скачанный файл в очередь."""
    st.mark_pending("disk:/a.jpg", 100, "md5a")
    st.mark_downloaded("disk:/a.jpg", "C:/out/a.jpg")
    st.mark_pending("disk:/a.jpg", 100, "md5a")
    assert st.get_stats()["pending"] == 0
    assert st.get_stats()["downloaded"] == 1


def test_md5_is_kept_when_api_returns_none(st):
    st.mark_pending("disk:/a.jpg", 100, "md5a")
    st.mark_pending("disk:/a.jpg", 100, None)
    row = next(iter(st.get_pending()))
    assert row["md5"] == "md5a"


def test_failed_then_retry(st):
    st.mark_pending("disk:/a.jpg", 100, None)
    st.mark_failed("disk:/a.jpg", "network died")
    assert st.get_stats()["failed"] == 1
    assert st.reset_failed_to_pending() == 1
    assert st.get_stats()["pending"] == 1
    assert next(iter(st.get_failed()), None) is None


def test_downloaded_and_skipped_count_as_done(st):
    st.mark_pending("disk:/a.jpg", 100, None)
    st.mark_pending("disk:/b.jpg", 50, None)
    st.mark_downloaded("disk:/a.jpg", "C:/out/a.jpg")
    st.mark_skipped("disk:/b.jpg", "C:/out/b.jpg")
    stats = st.get_stats()
    assert stats["bytes_downloaded"] == 150
    assert len(list(st.get_all_downloaded())) == 2


def test_error_is_truncated(st):
    st.mark_pending("disk:/a.jpg", 1, None)
    st.mark_failed("disk:/a.jpg", "x" * 5000)
    row = next(iter(st.get_failed()))
    assert len(row["error"]) == 2000


def test_downloaded_at_is_written(st):
    st.mark_pending("disk:/a.jpg", 1, None)
    st.mark_downloaded("disk:/a.jpg", "C:/out/a.jpg")
    row = next(iter(st.get_all_downloaded()))
    assert row["local_path"] == "C:/out/a.jpg"


def test_existing_remote_paths(st):
    st.mark_pending("disk:/a.jpg", 1, None)
    st.mark_pending("disk:/b.jpg", 1, None)
    assert st.get_existing_remote_paths() == {"disk:/a.jpg", "disk:/b.jpg"}


def test_context_manager(tmp_path):
    db_path = str(tmp_path / "cm_test.db")
    with State(db_path) as s:
        s.mark_pending("disk:/test.jpg", 123, "md5")
        assert s.get_stats()["pending"] == 1
    # After exit, connections should be closed
    assert len(s._all_conns) == 0


def test_changed_md5_resets_downloaded_to_pending(st):
    """Файл отредактирован на Диске — бэкап должен догнать новую версию."""
    st.mark_pending("disk:/a.jpg", 100, "md5-old")
    st.mark_downloaded("disk:/a.jpg", "C:/out/a.jpg")
    st.mark_pending("disk:/a.jpg", 120, "md5-new")
    row = next(iter(st.get_pending()))
    assert row["remote_path"] == "disk:/a.jpg"
    assert row["md5"] == "md5-new"
    assert row["local_path"] == "C:/out/a.jpg"  # путь сохранён — перекачаем на то же место


def test_category_skipped_is_not_resurrected_by_rescan(st):
    st.mark_pending("disk:/doc.pdf", 10, "m1")
    st._conn().execute("UPDATE files SET status='skipped', error='not in selected categories'")
    st.mark_pending("disk:/doc.pdf", 10, "m2")
    assert st.get_stats()["skipped"] == 1


def test_assign_local_paths_follows_new_download_dir(st, tmp_path):
    st.mark_pending_batch([("disk:/a.jpg", 1, None), ("disk:/b.jpg", 1, None)])
    old_dir = str(tmp_path / "old")
    new_dir = str(tmp_path / "new")
    st.assign_local_paths(old_dir)
    st.mark_downloaded("disk:/a.jpg", str(tmp_path / "old" / "a.jpg"))

    st.assign_local_paths(new_dir)
    rows = {r["remote_path"]: r["local_path"] for r in st._conn().execute("SELECT remote_path, local_path FROM files")}
    assert rows["disk:/b.jpg"].startswith(new_dir)   # pending переназначен в новую папку
    assert rows["disk:/a.jpg"].startswith(old_dir)   # скачанный остаётся где лежит


def test_mark_pending_batch(st):
    items = [
        ("disk:/1.jpg", 100, "md1"),
        ("disk:/2.jpg", 200, "md2"),
        ("disk:/3.jpg", 300, None),
    ]
    st.mark_pending_batch(items)
    stats = st.get_stats()
    assert stats["pending"] == 3
    assert stats["bytes_total"] == 600


