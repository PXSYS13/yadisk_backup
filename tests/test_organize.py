"""Сортировка: отчёт честно предсказывает, сколько папок исчезнет, и показывает примеры переносов."""

import json
import os
import struct
import sys

import pytest

import organize


def _write(root, rel, data):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


@pytest.fixture()
def backup(tmp_path):
    """Папка скачивания: дерево Диска, служебные папки, недокачанное и пустая папка."""
    root = str(tmp_path / "backup")
    files = {
        "Загрузки/Project1/Words/SomeDox.docx": b"docx",
        "Загрузки/Project1/img/logo_2023-05-01.png": b"logo",
        "Comments/Some-feedback-2024-12-30/a.doc": b"doc",
        "Comments/Some-feedback-2024-12-30/b.docx": b"b-docx",
        "Comments/Some-feedback-2024-12-30/c.ai": b"illustrator",
        "Comments/Some-feedback-2024-12-30/shot_2024-12-30.png": b"shot",
        "Кэш/Thumbs.db": b"thumbs",               # .db сортировка не берёт — папка останется
        "Недокачано/x.jpg.part": b"part",         # недокачанное — тоже
        "_превью/Загрузки/a.jpg.jpg": b"preview",
        "_ссылки/Свадьба/a.jpg": b"link",
        "Документы/old.pdf": b"old",
        "root.txt": b"root",
    }
    for rel, data in files.items():
        _write(root, rel, data)
    os.makedirs(os.path.join(root, "Пустая"))
    return root


def _run(monkeypatch, capsys, root, action="report-only"):
    monkeypatch.setattr(sys, "argv", ["organize.py", "--input", root, "--action", action])
    assert organize.main() == 0
    out = capsys.readouterr().out

    def tagged(tag):
        lines = [ln for ln in out.splitlines() if ln.startswith(tag)]
        return json.loads(lines[-1][len(tag):]) if lines else None

    return tagged("organize_summary | "), tagged("organize_result | "), out


def test_report_counts_folders_and_shows_examples(monkeypatch, capsys, backup):
    summary, result, out = _run(monkeypatch, capsys, backup)
    # Загрузки, Project1, Words, img, Comments, Some-feedback-2024-12-30 и пустая папка
    assert summary["folders"] == 7
    assert summary["examples"] == [
        {"from": os.path.join("Загрузки", "Project1", "Words", "SomeDox.docx"),
         "to": os.path.join("Документы", "SomeDox.docx")},
        {"from": os.path.join("Загрузки", "Project1", "img", "logo_2023-05-01.png"),
         "to": os.path.join("Фото", "2023", "2023-05", "logo_2023-05-01.png")},
        {"from": os.path.join("Comments", "Some-feedback-2024-12-30", "c.ai"),
         "to": os.path.join("Прочее", "c.ai")},
    ]
    assert result is None
    assert "Папок исчезнет:  7" in out
    # Это только отчёт — ничего не сдвинулось
    assert os.path.isfile(os.path.join(backup, "Загрузки", "Project1", "Words", "SomeDox.docx"))
    assert os.path.isdir(os.path.join(backup, "Пустая"))


def test_move_removes_exactly_the_predicted_folders(monkeypatch, capsys, backup):
    summary, result, _ = _run(monkeypatch, capsys, backup, action="move")
    assert result["cleaned"] == summary["folders"] == 7
    assert result["errors"] == 0
    assert os.path.isfile(os.path.join(backup, "Документы", "SomeDox.docx"))
    assert not os.path.exists(os.path.join(backup, "Загрузки"))
    assert not os.path.exists(os.path.join(backup, "Пустая"))
    for kept in ("Кэш", "Недокачано", "_превью", "_ссылки"):
        assert os.path.isdir(os.path.join(backup, kept))


def test_subfolder_input_does_not_count_itself(monkeypatch, capsys, backup):
    summary, _, _ = _run(monkeypatch, capsys, os.path.join(backup, "Загрузки"))
    assert summary["folders"] == 3  # Project1, Words, img — сама «Загрузки» и всё выше не трогаются


def test_doomed_dirs_without_disk():
    root = "R"
    a, ab, keep = os.path.join(root, "a"), os.path.join(root, "a", "b"), os.path.join(root, "keep")
    tree = [
        (root, ["a", "keep"], ["top.txt"]),
        (a, ["b"], ["one.doc"]),
        (ab, [], ["two.png"]),
        (keep, [], ["busy.part"]),
    ]
    moving = {os.path.join(root, "top.txt"), os.path.join(a, "one.doc"), os.path.join(ab, "two.png")}
    assert organize.doomed_dirs(root, tree, moving, [os.path.join(root, "Документы", "one.doc")]) == sorted([a, ab])
    # Папка, куда что-то переедет, не удаляется — и её родители тоже
    assert organize.doomed_dirs(root, tree, moving, [os.path.join(ab, "two.png")]) == []


def test_printable_survives_undecodable_names():
    assert organize._printable("a\udcffb") == "a?b"


GPS_DATE_STAMP = 0x001D


def _jpeg_with_exif(path, ifd0=None, exif=None, gps=None):
    """JPEG с настоящим EXIF (TIFF, little-endian): ifd0, exif, gps — тег → строка-дата."""
    ifd0, exif, gps = dict(ifd0 or {}), dict(exif or {}), dict(gps or {})

    def size(tags, links=0):
        return 2 + 12 * (len(tags) + links) + 4

    off_exif = 8 + size(ifd0, bool(exif) + bool(gps))
    off_gps = off_exif + (size(exif) if exif else 0)
    data_at = off_gps + (size(gps) if gps else 0)
    blob = bytearray()

    def table(tags, links=()):
        entries = []
        for tag, value in tags.items():
            raw = value.encode("ascii") + b"\x00"
            entries.append(struct.pack("<HHII", tag, 2, len(raw), data_at + len(blob)))
            blob.extend(raw)
        entries += [struct.pack("<HHII", tag, 4, 1, offset) for tag, offset in links]
        return struct.pack("<H", len(entries)) + b"".join(entries) + b"\x00" * 4

    links = [(0x8769, off_exif)] * bool(exif) + [(0x8825, off_gps)] * bool(gps)
    tiff = b"II*\x00" + struct.pack("<I", 8) + table(ifd0, links)
    tiff += (table(exif) if exif else b"") + (table(gps) if gps else b"")
    payload = b"Exif\x00\x00" + tiff + bytes(blob)
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    path.write_bytes(b"\xff\xd8" + app1 + b"\xff\xd9")


def test_jpeg_date_is_the_shot_not_the_last_edit(tmp_path):
    """В EXIF первой идёт дата последней правки (DateTime, IFD0), съёмка — в Exif IFD."""
    photo = tmp_path / "photo.jpg"
    _jpeg_with_exif(photo, ifd0={0x0132: "2026:10:01 12:00:00"}, exif={0x9003: "2014:07:15 09:30:00"})
    assert organize.get_exif_date(str(photo)) == (2014, 7)


def test_jpeg_date_is_the_shot_not_gps_time(tmp_path):
    """Снимок 1 июля в 00:30 по местному времени — 30 июня по UTC, и GPS-дата говорит «июнь».
    Самая ранняя из всех дат клала его в прошлый месяц; съёмка (DateTimeOriginal) главнее."""
    photo = tmp_path / "photo.jpg"
    _jpeg_with_exif(photo, ifd0={0x0132: "2026:10:01 12:00:00"},
                    exif={0x9003: "2014:07:01 00:30:00", 0x9004: "2019:02:03 10:00:00"},
                    gps={GPS_DATE_STAMP: "2014:06:30"})
    assert organize.get_exif_date(str(photo)) == (2014, 7)


def test_jpeg_date_falls_back_from_shot_to_digitized_to_edit(tmp_path):
    photo = tmp_path / "scan.jpg"
    # Даты съёмки нет (пустая) — берётся оцифровка, а не более ранняя дата правки
    _jpeg_with_exif(photo, ifd0={0x0132: "2012:01:01 10:00:00"},
                    exif={0x9003: "    :  :     :  :  ", 0x9004: "2019:02:03 10:00:00"})
    assert organize.get_exif_date(str(photo)) == (2019, 2)
    _jpeg_with_exif(photo, ifd0={0x0132: "2012:01:01 10:00:00"}, gps={GPS_DATE_STAMP: "2011:05:05"})
    assert organize.get_exif_date(str(photo)) == (2012, 1)
    _jpeg_with_exif(photo, gps={GPS_DATE_STAMP: "2011:05:05"})
    assert organize.get_exif_date(str(photo)) is None  # одна GPS-дата — не дата съёмки


def _dir_link(target, link):
    """Ссылка на папку: на Windows — junction (символьным ссылкам там нужны права администратора)."""
    if sys.platform.startswith("win"):
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_sorting_does_not_follow_links_out_of_the_folder(tmp_path):
    outside = tmp_path / "other_drive"
    outside.mkdir()
    (outside / "keep.jpg").write_bytes(b"x")
    root = tmp_path / "backup"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"y")
    _dir_link(outside, root / "link")
    found = {os.path.basename(f["path"]) for f in organize.scan(str(root))}
    assert found == {"a.jpg"}


def test_folder_with_a_link_inside_is_not_promised_to_vanish(monkeypatch, capsys, tmp_path):
    """Папка, где лежит ссылка на другой диск, после переноса не пустеет — уборка её оставляет.
    Отчёт обещал, что она исчезнет: «Папок исчезнет» не сходилось с «Удалено пустых папок»."""
    outside = tmp_path / "other_drive"
    outside.mkdir()
    (outside / "keep.jpg").write_bytes(b"x")
    root = tmp_path / "backup"
    (root / "Альбом").mkdir(parents=True)
    (root / "Альбом" / "a.jpg").write_bytes(b"y")
    (root / "Пустая" / "Вложенная").mkdir(parents=True)
    _dir_link(outside, root / "Альбом" / "link")

    summary, _, _ = _run(monkeypatch, capsys, str(root))
    assert summary["folders"] == 2  # «Пустая» и «Вложенная»; «Альбом» держит ссылка
    summary, result, _ = _run(monkeypatch, capsys, str(root), action="move")
    assert result["cleaned"] == summary["folders"] == 2
    assert os.path.isdir(root / "Альбом" / "link") and (outside / "keep.jpg").read_bytes() == b"x"


def test_cloud_folders_are_sorted_too(tmp_path, monkeypatch):
    """Папки OneDrive («Рабочий стол», «Документы» с резервным копированием) — тоже точки повторной
    обработки Windows, но не ссылки. Раньше сортировка молча не заходила ни в одну из них."""
    root = tmp_path / "backup"
    (root / "Альбом").mkdir(parents=True)
    (root / "Альбом" / "a.jpg").write_bytes(b"x")
    cloud = os.path.normcase(str(root / "Альбом"))
    real_lstat = os.lstat

    class CloudStat:
        st_file_attributes = 0x10 | 0x400  # FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT
        st_reparse_tag = 0x9000601A  # IO_REPARSE_TAG_CLOUD_6: «файлы по запросу» OneDrive

        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return getattr(self._st, name)

    def lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        return CloudStat(st) if os.path.normcase(os.fspath(path)) == cloud else st

    monkeypatch.setattr(organize.os, "lstat", lstat)
    assert not organize._is_link_or_mount(str(root / "Альбом"))
    found = {os.path.basename(f["path"]) for f in organize.scan(str(root))}
    assert found == {"a.jpg"}
