"""Тесты санитизации путей — главная защита от записи мимо DOWNLOAD_DIR."""

import sqlite3

import pytest

from utils import (
    human_bytes,
    human_duration,
    sanitize_component,
    sanitize_remote_path,
    sqlite_ro_uri,
)


@pytest.mark.parametrize("bad", ['a<b', 'a>b', 'a:b', 'a"b', 'a|b', 'a?b', 'a*b'])
def test_forbidden_chars_replaced(bad):
    assert sanitize_component(bad) == "a_b"


def test_slashes_are_not_separators():
    """Имя файла с слешем не должно превращаться в подпапку."""
    assert sanitize_component("a/b") == "a_b"
    assert sanitize_component(r"a\b") == "a_b"


def test_control_chars_replaced():
    assert sanitize_component("a\x00b\x1fc") == "a_b_c"


def test_dot_names_are_neutralized():
    assert sanitize_component(".") == "_"
    assert sanitize_component("..") == "_"
    assert sanitize_component("...") == "_"


def test_trailing_dots_and_spaces_stripped():
    assert sanitize_component("file.  ") == "file"
    assert sanitize_component("name ") == "name"


def test_reserved_windows_names_prefixed():
    assert sanitize_component("CON") == "_CON"
    assert sanitize_component("com1.txt") == "_com1.txt"
    assert sanitize_component("console.txt") == "console.txt"


def test_empty_component():
    assert sanitize_component("") == "_"


def test_sanitize_remote_path_strips_disk_prefix():
    assert sanitize_remote_path("disk:/foo/bar.jpg") == "foo/bar.jpg"
    assert sanitize_remote_path("/foo//bar.jpg") == "foo/bar.jpg"


def test_sanitize_remote_path_blocks_traversal():
    """'..' в пути не должен уводить выше папки загрузки."""
    out = sanitize_remote_path("disk:/../../etc/passwd")
    assert ".." not in out.split("/")
    assert out == "_/_/etc/passwd"


def test_sanitize_remote_path_backslash_component():
    out = sanitize_remote_path(r"disk:/photos/..\..\evil.txt")
    assert ".." not in out.replace("\\", "/").split("/")
    # весь хвост схлопнулся в ОДНО имя файла — разделителей больше нет
    assert out == "photos/.._.._evil.txt"


def test_sqlite_ro_uri_survives_hash_in_path(tmp_path):
    """'#' в пути раньше молча открывал ПУСТУЮ базу вместо нашей."""
    d = tmp_path / "back#up"
    d.mkdir()
    db = d / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE files (a)")
    con.commit()
    con.close()

    c = sqlite3.connect(sqlite_ro_uri(db), uri=True)
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master")]
    c.close()
    assert tables == ["files"]


def test_sqlite_ro_uri_is_readonly(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE files (a)")
    con.commit()
    con.close()

    c = sqlite3.connect(sqlite_ro_uri(db), uri=True)
    with pytest.raises(sqlite3.OperationalError):
        c.execute("INSERT INTO files VALUES (1)")
    c.close()


def test_human_bytes():
    assert human_bytes(0) == "0.00 B"
    assert human_bytes(1536) == "1.50 KB"
    assert human_bytes(1024 ** 3) == "1.00 GB"


def test_human_duration():
    assert human_duration(0) == "00:00:00"
    assert human_duration(3661) == "01:01:01"
