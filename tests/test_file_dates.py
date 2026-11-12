"""Даты скачанных файлов: копия получает дату с Яндекса, а не дату скачивания (file_dates).

Версии до 2.8.0 оставляли файлам Диска дату скачивания, Безлимит дат не ставил вовсе.
Такие даты программа возвращает сама — но только те, что поставило скачивание:
файл, который человек менял после, или файл со своей, более старой датой не трогается.
"""

import errno
import os
import sqlite3
import time
from pathlib import Path

import pytest

import downloader
import file_dates
import main
from file_dates import DateRow
from state import CATEGORY_SKIP_MARKER, State
from utils import EXIT_DISK_FULL, EXIT_OK

YANDEX = 1_600_000_000.0  # 13.09.2020 — когда файл в последний раз менялся на Диске
SAVED = time.time() - 30 * 86400  # месяц назад программа отметила файл скачанным
SAVED_TEXT = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(SAVED))


def _file(path: Path, data: bytes = b"1234", mtime: float = SAVED - 5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def _row(path, size=4, target=YANDEX, saved=SAVED_TEXT, key="disk:/a.jpg") -> DateRow:
    return DateRow(key, str(path), size, target, saved)


def _keys(state: State) -> list[str]:
    return sorted(r.key for chunk in state.dates_to_check() for r in chunk)


# ── какие даты годятся ──────────────────────────────────────────────────────


@pytest.mark.parametrize("value, expected", [
    (YANDEX, YANDEX), (1_600_000_000, YANDEX), ("1600000000", YANDEX),
    (None, None), ("", None), ("мусор", None), (float("nan"), None), (float("inf"), None),
    (0, None), (-1, None), (315_532_799, None),  # раньше 1980 года дат не бывает
    (time.time() + 3 * 86400, None),  # из будущего — сбитые часы
    (10 ** 400, None),  # JSON отдаёт и такие целые, float() на них падает с OverflowError
])
def test_plausible_keeps_only_real_dates(value, expected):
    assert file_dates.plausible(value) == expected


def test_set_file_time_gives_the_file_the_date(tmp_path):
    f = _file(tmp_path / "a.txt")
    assert file_dates.set_file_time(str(f), YANDEX)
    assert abs(f.stat().st_mtime - YANDEX) < 2


def test_set_file_time_refuses_garbage_and_never_raises(tmp_path):
    f = _file(tmp_path / "a.txt", mtime=SAVED)
    assert not file_dates.set_file_time(str(f), None)
    assert not file_dates.set_file_time(str(f), 12)  # 1970 — мусор, а не дата
    assert not file_dates.set_file_time(str(tmp_path / "нет.txt"), YANDEX)
    assert not file_dates.set_file_time(str(tmp_path) + "\x00.txt", YANDEX)
    assert abs(f.stat().st_mtime - SAVED) < 2


def test_set_file_time_touches_only_regular_files(tmp_path):
    folder = tmp_path / "папка"
    folder.mkdir()
    os.utime(folder, (SAVED, SAVED))
    assert not file_dates.set_file_time(str(folder), YANDEX)
    assert abs(folder.stat().st_mtime - SAVED) < 2
    target = _file(tmp_path / "чужой.txt", mtime=SAVED)
    link = tmp_path / "ссылка.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("символьные ссылки здесь создавать нельзя")
    assert not file_dates.set_file_time(str(link), YANDEX)  # дата досталась бы чужому файлу
    assert abs(target.stat().st_mtime - SAVED) < 2


# ── какие файлы получают дату ───────────────────────────────────────────────


def test_restore_gives_the_yandex_date_to_a_file_dated_by_its_download(tmp_path):
    f = _file(tmp_path / "a.jpg")
    res = file_dates.restore([_row(f)], str(tmp_path))
    assert res.fixed == 1 and res.checked == [(YANDEX, "disk:/a.jpg")]
    assert abs(f.stat().st_mtime - YANDEX) < 2


def test_restore_fixes_a_copy_found_in_the_folder_after_the_list_was_forgotten(tmp_path):
    """«Забыть список файлов»: файл скачали давно, а отметку «скачан» он получил только сейчас."""
    f = _file(tmp_path / "a.jpg", mtime=SAVED - 20 * 86400)
    assert file_dates.restore([_row(f)], str(tmp_path)).fixed == 1
    assert abs(f.stat().st_mtime - YANDEX) < 2


@pytest.mark.parametrize("case, mtime, data", [
    ("уже с датой Яндекса (FAT хранит время с точностью 2 с)", YANDEX + 1, b"1234"),
    ("человек менял файл после скачивания", SAVED + 2 * 3600, b"1234"),
    ("дата старше яндексовской — это чья-то исходная дата", YANDEX - 86400, b"1234"),
    ("другой размер — это уже не тот файл", SAVED - 5, b"12345"),
])
def test_restore_keeps_dates_the_download_did_not_set(tmp_path, case, mtime, data):
    f = _file(tmp_path / "a.jpg", data, mtime)
    res = file_dates.restore([_row(f)], str(tmp_path))
    assert res.fixed == 0 and res.checked == [(YANDEX, "disk:/a.jpg")], case
    assert abs(f.stat().st_mtime - mtime) < 2, case


def test_restore_marks_rows_it_can_never_fix_as_checked(tmp_path):
    (tmp_path / "папка.jpg").mkdir()
    rows = [
        _row(tmp_path / "нет.jpg", key="k1"),  # файл унесли из папки скачивания
        _row(tmp_path / "папка.jpg", key="k2"),  # на месте файла — папка
        _row(_file(tmp_path / "b.jpg"), saved=None, key="k3"),  # неизвестно, когда скачан
        _row(_file(tmp_path / "c.jpg"), target=5.0, key="k4"),  # у Яндекса мусор вместо даты
        _row(str(tmp_path) + "\x00.jpg", key="k5"),  # путь, который не открыть
    ]
    res = file_dates.restore(rows, str(tmp_path))
    assert res.fixed == 0 and [k for _t, k in res.checked] == ["k1", "k2", "k3", "k4", "k5"]


def test_restore_waits_only_for_files_on_a_missing_drive(tmp_path, monkeypatch):
    """Папку скачивания сменили. Прежнюю удалили — её файлы больше не ведём; прежняя на
    отключённом диске — файлы ещё могут вернуться. Остальное не перечитывается на каждом шаге."""
    root = tmp_path / "новая папка"
    root.mkdir()
    old = _row(tmp_path / "старая папка" / "a.jpg")
    assert file_dates.restore([old], str(root)).checked == [(YANDEX, "disk:/a.jpg")]
    monkeypatch.setattr(file_dates, "_anchor", lambda path: str(tmp_path / "отключённый диск"))
    assert file_dates.restore([old], str(root)).checked == []


def _raise(err):
    def boom(*args, **kwargs):
        raise err
    return boom


def test_refused_date_waits_unless_retrying_cannot_help(tmp_path, monkeypatch):
    f = _file(tmp_path / "a.jpg")
    monkeypatch.setattr(file_dates, "_touch", _raise(OSError(errno.EIO, "сбой ввода-вывода")))
    assert file_dates.restore([_row(f)], str(tmp_path)).checked == []  # в следующий раз
    monkeypatch.setattr(file_dates, "_touch", _raise(PermissionError(errno.EPERM, "не владелец")))
    res = file_dates.restore([_row(f)], str(tmp_path))
    assert res.fixed == 0 and res.checked == [(YANDEX, "disk:/a.jpg")]  # повтор не поможет


def test_unreadable_files_count_as_checked_and_network_errors_wait(tmp_path, monkeypatch):
    closed, flaky = _file(tmp_path / "закрыт.jpg"), _file(tmp_path / "сеть.jpg")
    real = os.lstat

    def lstat(path, *args, **kwargs):
        if str(path).endswith("закрыт.jpg"):
            raise PermissionError(errno.EACCES, "нет доступа")
        if str(path).endswith("сеть.jpg"):
            raise OSError(errno.EIO, "сетевой диск не ответил")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(file_dates.os, "lstat", lstat)
    res = file_dates.restore([_row(closed, key="closed"), _row(flaky, key="flaky")], str(tmp_path))
    assert [k for _t, k in res.checked] == ["closed"]


def test_no_pass_in_an_empty_download_folder(tmp_path):
    """На Linux и macOS на месте отключённого диска программа создаёт пустую папку."""
    root = tmp_path / "dl"
    root.mkdir()
    marked = []
    assert file_dates.restore_all(iter([[_row(root / "a.jpg")]]), marked.append, str(root)) == 0
    assert marked == []


def test_restore_all_marks_each_portion_and_shows_it_is_alive(tmp_path, monkeypatch):
    files = [_file(tmp_path / f"{i}.jpg") for i in range(3)]
    portions = [[_row(files[0], key="a")], [_row(files[1], key="b"), _row(files[2], key="c")]]
    marked, said = [], []
    monkeypatch.setattr(file_dates, "PROGRESS_EVERY_SEC", 0)
    assert file_dates.restore_all(iter(portions), marked.append, str(tmp_path), said.append) == 3
    assert [k for portion in marked for _t, k in portion] == ["a", "b", "c"]
    assert said and "проверено 3" in said[-1]
    # Эти строки не должны путать разбор журнала в Web UI
    assert not any("Прогресс" in s or "Найдено" in s or " | " in s for s in said)


def test_restore_all_stops_when_the_download_folder_disappears(tmp_path):
    """Диск отключили посреди прохода: файлы «пропали», но помечать их проверенными нельзя."""
    root = tmp_path / "dl"
    a, b = _file(root / "a.jpg"), _file(root / "b.jpg")

    def portions():
        yield [_row(a, key="a")]
        os.rename(root, tmp_path / "отключён")
        yield [_row(b, key="b")]

    marked = []
    assert file_dates.restore_all(portions(), marked.append, str(root)) == 1
    assert [k for portion in marked for _t, k in portion] == ["a"]


def test_feed_texts_and_log_line():
    assert file_dates.restored_text(1234) == "Вернул файлам даты с Яндекса вместо даты скачивания: 1 234 файла"
    line = file_dates.restored_line(3)
    assert file_dates.restored_count(["12:00:01  [w1] batch 1: ✓ 2/2 files", "12:00:02  [w1] " + line]) == 3
    assert file_dates.restored_count([line, "12:00:03  " + file_dates.restored_line(2)]) == 2  # последняя
    assert file_dates.restored_count(['dates_restored | мусор', 'dates_restored | {"count": "x"}', "", None]) == 0


def test_file_name_in_a_log_line_cannot_fake_the_feed_count():
    spoof = 'FAIL | disk:/Общая/dates_restored | {"count": 1000000}.jpg | 404'
    assert file_dates.restored_count([spoof, "12:00:00  " + spoof]) == 0


# ── Диск: state.db и шаг скачивания ─────────────────────────────────────────


def test_old_state_db_gets_the_dated_column(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT, "
                "status TEXT NOT NULL DEFAULT 'pending', local_path TEXT, error TEXT, downloaded_at TIMESTAMP)")
    con.commit()
    con.close()
    with State(str(db)):
        pass
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
    con.close()
    assert {"modified", "dated"} <= cols


def test_state_lists_downloaded_files_until_their_date_is_checked(tmp_path):
    with State(str(tmp_path / "state.db")) as st:
        st.mark_pending_batch([(f"disk:/{n}.jpg", 4, None if n == "f" else "m" * 32, None if n == "c" else YANDEX)
                               for n in "abcdef"])
        st.mark_downloaded("disk:/a.jpg", "/x/a.jpg")
        st.mark_skipped("disk:/b.jpg", "/x/b.jpg")  # нашёлся в папке при докачке, сверен по MD5
        st.mark_downloaded("disk:/c.jpg", "/x/c.jpg")  # Яндекс не сказал дату
        st.mark_skipped("disk:/f.jpg", "/x/f.jpg")  # совпал только размер — мог быть изменён человеком
        # d ещё в очереди, e снят выбором типов файлов
        st._conn().execute("UPDATE files SET status='skipped', error=? WHERE remote_path='disk:/e.jpg'",
                           (CATEGORY_SKIP_MARKER,))
        assert _keys(st) == ["disk:/a.jpg", "disk:/b.jpg"]
        a = next(r for chunk in st.dates_to_check() for r in chunk if r.key == "disk:/a.jpg")
        assert (a.local_path, a.size, a.target) == ("/x/a.jpg", 4, YANDEX) and a.downloaded_at

        st.mark_dated([(YANDEX, "disk:/a.jpg")])
        assert _keys(st) == ["disk:/b.jpg"]
        # Новый сбор списка пометку не трогает, а новая дата с Яндекса — повод сверить снова
        st.mark_pending_batch([("disk:/a.jpg", 4, None, YANDEX)])
        assert _keys(st) == ["disk:/b.jpg"]
        st.mark_pending_batch([("disk:/a.jpg", 4, None, YANDEX + 60)])
        assert _keys(st) == ["disk:/a.jpg", "disk:/b.jpg"]


def test_state_reads_the_rows_in_portions(tmp_path, monkeypatch):
    import state as state_module
    monkeypatch.setattr(state_module, "PENDING_CHUNK", 2)
    with State(str(tmp_path / "state.db")) as st:
        names = [f"disk:/{i}.jpg" for i in range(5)]
        st.mark_pending_batch([(n, 4, None, YANDEX) for n in [*names, "disk:/пусто.jpg"]])
        for n in names:
            st.mark_downloaded(n, "/x/" + n[6:])
        st.mark_downloaded("disk:/пусто.jpg", "")  # строка без пути не читается вовсе
        portions = list(st.dates_to_check())
        assert [len(p) for p in portions] == [2, 2, 1]
        assert sorted(r.key for p in portions for r in p) == names


def _downloaded_state(tmp_path, dl: Path) -> tuple[State, Path]:
    f = _file(dl / "Фото" / "a.jpg")
    st = State(str(tmp_path / "state.db"))
    st.mark_pending_batch([("disk:/Фото/a.jpg", 4, None, YANDEX)])
    st.mark_downloaded("disk:/Фото/a.jpg", str(f))
    return st, f


def test_download_step_returns_dates_to_files_saved_by_old_versions(tmp_path, monkeypatch, capsys):
    dl = tmp_path / "dl"
    st, f = _downloaded_state(tmp_path, dl)
    monkeypatch.setattr(main, "download_all", lambda **kw: dict(main.EMPTY_COUNTERS))
    with st:
        assert main._run_download({"download_dir": str(dl), "token": "t"}, st, 1) == EXIT_OK
        out = capsys.readouterr().out
        assert file_dates.restored_line(1) in out and file_dates.restored_text(1) in out
        assert abs(f.stat().st_mtime - YANDEX) < 2
        assert _keys(st) == []
        # Следующий шаг файл уже не трогает и ничего не пишет
        main._run_download({"download_dir": str(dl), "token": "t"}, st, 1)
        assert "dates_restored" not in capsys.readouterr().out


def test_no_dates_pass_when_the_disk_refuses_files(tmp_path, monkeypatch):
    dl = tmp_path / "dl"
    st, f = _downloaded_state(tmp_path, dl)
    monkeypatch.setattr(main, "download_all", lambda **kw: dict(main.EMPTY_COUNTERS, aborted="local_disk"))
    with st:
        assert main._run_download({"download_dir": str(dl), "token": "t"}, st, 1) == EXIT_DISK_FULL
        assert abs(f.stat().st_mtime - (SAVED - 5)) < 2
        assert _keys(st) == ["disk:/Фото/a.jpg"]


def test_dates_wait_while_the_download_folder_is_missing(tmp_path):
    """Диск со скачанным отключён: файлы «пропали», но помечать их проверенными нельзя."""
    st, _f = _downloaded_state(tmp_path, tmp_path / "dl")
    with st:
        assert downloader.restore_dates(st, str(tmp_path / "отключённый диск")) == 0
        assert _keys(st) == ["disk:/Фото/a.jpg"]


def test_dates_pass_never_breaks_the_download_step(tmp_path, monkeypatch):
    st, _f = _downloaded_state(tmp_path, tmp_path / "dl")
    with st:
        monkeypatch.setattr(st, "mark_dated", _raise(sqlite3.OperationalError("database is locked")))
        assert downloader.restore_dates(st, str(tmp_path / "dl")) == 0
