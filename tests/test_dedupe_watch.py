"""dedupe.py и watch.py: снятые выбором файлы не считаются ни скачанными, ни «оставленной» копией."""

import dedupe
import watch
from state import FOLDER_SKIP_MARKER, State


def _db(tmp_path):
    st = State(str(tmp_path / "state.db"))
    return st


def test_dedupe_does_not_keep_a_copy_that_will_never_be_downloaded(tmp_path, monkeypatch, capsys):
    """Двойник в невыбранной папке не скачается: файл из выбранной папки не должен стать «дублем»."""
    st = _db(tmp_path)
    md5 = "a" * 32
    st.mark_pending_batch([("disk:/Архив 2019/IMG_0001.JPG", 10, md5), ("disk:/Фотокамера/IMG_0001.JPG", 10, md5)])
    st._conn().execute("UPDATE files SET status='skipped', error=? WHERE remote_path LIKE 'disk:/Архив%'",
                       (FOLDER_SKIP_MARKER,))
    st.close()
    monkeypatch.setattr(dedupe, "DB", str(tmp_path / "state.db"))
    assert dedupe.main() == 0
    with State(str(tmp_path / "state.db")) as st:
        rows = {r["remote_path"]: r["status"] for r in st._conn().execute("SELECT remote_path, status FROM files")}
    assert rows["disk:/Фотокамера/IMG_0001.JPG"] == "pending"


def test_watch_does_not_count_deselected_files_as_done(tmp_path, monkeypatch):
    st = _db(tmp_path)
    st.mark_pending_batch([(f"disk:/off/{i}.jpg", 1, None) for i in range(9)] + [("disk:/on/a.jpg", 1, None)])
    st._conn().execute("UPDATE files SET status='skipped', error=? WHERE remote_path LIKE 'disk:/off/%'",
                       (FOLDER_SKIP_MARKER,))
    st.close()
    monkeypatch.setattr(watch, "DB_FILE", tmp_path / "state.db")
    s = watch.fetch_state()["stats"]
    assert (s["total"], s["skipped"], s["not_selected"], s["pending"]) == (1, 0, 9, 1)
