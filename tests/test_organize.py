"""Сортировка: отчёт честно предсказывает, сколько папок исчезнет, и показывает примеры переносов."""

import json
import os
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


def test_jpeg_date_is_the_shot_not_the_last_edit(tmp_path):
    """В EXIF первой идёт дата последней правки (DateTime), съёмка — дальше: берём самую раннюю."""
    exif = b"Exif\x00\x00" + b"2026:10:01 12:00:00\x00" + b"2014:07:15 09:30:00\x00"
    app1 = b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8" + app1 + b"\xff\xd9")
    assert organize.get_exif_date(str(photo)) == (2014, 7)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="символьные ссылки на Windows требуют прав")
def test_sorting_does_not_follow_links_out_of_the_folder(tmp_path):
    outside = tmp_path / "other_drive"
    outside.mkdir()
    (outside / "keep.jpg").write_bytes(b"x")
    root = tmp_path / "backup"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"y")
    os.symlink(outside, root / "link", target_is_directory=True)
    found = {os.path.basename(f["path"]) for f in organize.scan(str(root))}
    assert found == {"a.jpg"}
