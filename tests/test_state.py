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
