"""Тесты SQLite-трекера прогресса."""

import os

import pytest

from state import State
from utils import _short_hash, legacy_local_path


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


def test_changed_md5_resets_file_found_on_disk_to_pending(st):
    """Найденный при докачке (skipped без ошибки) — та же скачанная копия: новая версия на Диске
    возвращает его в очередь, а не остаётся старой навсегда."""
    st.mark_pending("disk:/a.jpg", 100, "md5-old")
    st.mark_skipped("disk:/a.jpg", "C:/out/a.jpg")
    st.mark_pending("disk:/a.jpg", 120, "md5-new")
    row = next(iter(st.get_pending()))
    assert (row["remote_path"], row["size"], row["md5"]) == ("disk:/a.jpg", 120, "md5-new")
    # Без изменений — остаётся найденным
    st.mark_skipped("disk:/a.jpg", "C:/out/a.jpg")
    st.mark_pending("disk:/a.jpg", 120, "md5-new")
    assert st.get_stats()["skipped"] == 1


def test_pending_reader_survives_a_write_from_another_process(st, tmp_path):
    """Скачивание читает очередь, пока «Повторить ошибки» в Web UI пишет в ту же базу.

    Открытый на всё скачивание SELECT держал снимок базы: после чужой записи каждая
    запись скачивания падала с «database is locked».
    """
    import sqlite3

    import state as state_mod

    st.mark_pending_batch([(f"disk:/f{i:04d}.bin", i, None) for i in range(state_mod.PENDING_CHUNK + 50)])
    st.mark_pending("disk:/failed.bin", 1, None)
    st.mark_failed("disk:/failed.bin", "server_50x | x")
    pending = st.get_pending()
    first = next(pending)
    other = sqlite3.connect(st.db_path, timeout=5)
    with other:
        other.execute("UPDATE files SET status='pending', error=NULL WHERE status='failed'")
    other.close()
    st.mark_downloaded(first["remote_path"], "/out/" + first["remote_path"])
    rest = [r["remote_path"] for r in pending]
    assert len(rest) == state_mod.PENDING_CHUNK + 49  # все, кроме первого; порции стыкуются без потерь
    assert first["remote_path"] not in rest


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


def _local_paths(st):
    return {r["remote_path"]: r["local_path"]
            for r in st._conn().execute("SELECT remote_path, local_path FROM files")}


def test_assign_keeps_file_names_in_cleaned_folder(st, tmp_path):
    out = str(tmp_path / "out")
    rp = "disk:/Встреча: итоги/Words/SomeDox.docx"
    st.mark_pending(rp, 1, None)
    st.assign_local_paths(out)
    assert _local_paths(st)[rp] == os.path.join(out, "Встреча_ итоги", "Words", "SomeDox.docx")


@pytest.mark.parametrize("order", [("disk:/a:b/x.txt", "disk:/a_b/x.txt"),
                                   ("disk:/a_b/x.txt", "disk:/a:b/x.txt")])
def test_assign_gives_clean_name_to_the_valid_path(st, tmp_path, order):
    """«a:b/x.txt» и «a_b/x.txt» ложатся в одну папку a_b: чистое имя — у пути, который не чистили,
    хотя ':' сортируется раньше '_'."""
    out = str(tmp_path / "out")
    for rp in order:
        st.mark_pending(rp, 1, None)
    st.assign_local_paths(out)
    rows = _local_paths(st)
    assert rows["disk:/a_b/x.txt"] == os.path.join(out, "a_b", "x.txt")
    assert rows["disk:/a:b/x.txt"] == os.path.join(out, "a_b", f"x_{_short_hash('disk:/a:b/x.txt')}.txt")


def test_assign_keeps_already_assigned_hashed_path(st, tmp_path):
    """Путь, назначенный до 2.3.1 (с хешем), не пересчитывается: файл не переедет на новое место."""
    out = str(tmp_path / "out")
    rp = "disk:/Встреча: итоги/SomeDox.docx"
    old = legacy_local_path(out, rp)
    st.mark_pending(rp, 1, None)
    st._conn().execute("UPDATE files SET local_path=? WHERE remote_path=?", (old, rp))
    st.assign_local_paths(out)
    assert _local_paths(st)[rp] == old


def test_assign_downloaded_twin_forces_hash(st, tmp_path):
    out = str(tmp_path / "out")
    st.mark_pending("disk:/a_b/x.txt", 1, None)
    st.mark_downloaded("disk:/a_b/x.txt", os.path.join(out, "a_b", "x.txt"))
    st.mark_pending("disk:/a:b/x.txt", 1, None)
    st.assign_local_paths(out)
    assert _local_paths(st)["disk:/a:b/x.txt"] == \
        os.path.join(out, "a_b", f"x_{_short_hash('disk:/a:b/x.txt')}.txt")


def test_assign_reuses_file_saved_under_old_name(st, tmp_path):
    """После «Забыть список файлов» файл под именем до 2.3.1 узнаётся, а не качается второй раз."""
    out = str(tmp_path / "out")
    saved, fresh = "disk:/Встреча: итоги/SomeDox.docx", "disk:/Встреча: итоги/New.docx"
    old = legacy_local_path(out, saved)
    os.makedirs(os.path.dirname(old))
    with open(old, "wb") as f:
        f.write(b"x")
    st.mark_pending_batch([(saved, 1, None), (fresh, 1, None)])
    st.assign_local_paths(out)
    rows = _local_paths(st)
    assert rows[saved] == old
    assert rows[fresh] == os.path.join(out, "Встреча_ итоги", "New.docx")  # файла нет — новое имя


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


