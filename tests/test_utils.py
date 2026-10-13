"""Тесты санитизации путей — главная защита от записи мимо DOWNLOAD_DIR."""

import sqlite3

import pytest

from utils import (
    human_bytes,
    human_duration,
    human_duration_ru,
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
    assert human_duration(None) == "00:00:00"


def test_human_duration_ru():
    assert human_duration_ru(0) == "0с"
    assert human_duration_ru(45) == "45с"
    assert human_duration_ru(125) == "2м 05с"
    assert human_duration_ru(3665) == "1ч 01м 05с"
    assert human_duration_ru(None) == "—"


def test_categories_and_media():
    from utils import categorize, classify_dir, is_media

    assert is_media("photo.jpg") is True
    assert is_media("video.mp4") is True
    assert is_media("document.pdf") is False

    assert categorize("pic.PNG") == "photos"
    assert categorize("movie.mkv") == "videos"
    assert categorize("song.mp3") == "audio"
    assert categorize("report.docx") == "documents"
    assert categorize("archive.zip") == "archives"
    assert categorize("setup.exe") == "programs"
    assert categorize("unknown.xyz") == "other"

    assert classify_dir("pic.jpg") == "Фото"
    assert classify_dir("movie.avi") == "Видео"
    assert classify_dir("report.pdf") == "Документы"
    assert classify_dir("unknown.xyz") == "Прочее"


def test_avoid_case_collision(tmp_path):
    from utils import IS_WINDOWS, avoid_case_collision

    if not IS_WINDOWS:
        return

    f_upper = tmp_path / "IMAGE.PNG"
    f_upper.write_text("dummy")

    # Файл с тем же именем, но строчными буквами должен получить суффикс
    f_lower = str(tmp_path / "image.png")
    suffixed = avoid_case_collision(f_lower)
    assert suffixed.endswith("image_1.png")

    # Файл с точно таким же регистром не должен переименовываться (resume)
    same_case = avoid_case_collision(str(f_upper))
    assert same_case == str(f_upper)


def test_classify_yandex_error():
    from utils import TELEGRAM_CHANNEL, classify_yandex_error

    # None
    assert classify_yandex_error(None)["is_yandex_server"] is False

    # 502 Bad Gateway
    res_502 = classify_yandex_error("502 Bad Gateway: yandex backend died")
    assert res_502["is_yandex_server"] is True
    assert res_502["category"] == "server_down"
    assert TELEGRAM_CHANNEL in res_502["action"]

    # 429 Too Many Requests
    res_429 = classify_yandex_error("HTTP 429 Too Many Requests")
    assert res_429["is_yandex_server"] is True
    assert res_429["category"] == "rate_limit"

    # Connection reset
    res_conn = classify_yandex_error(ConnectionResetError("Connection reset by peer"))
    assert res_conn["is_yandex_server"] is True
    assert res_conn["category"] == "connection_drop"

    # 401 Unauthorized
    res_401 = classify_yandex_error("401 Unauthorized: token expired")
    assert res_401["is_yandex_server"] is False
    assert res_401["category"] == "auth_expired"

    # 404
    res_404 = classify_yandex_error("404 PathNotFound")
    assert res_404["is_yandex_server"] is False
    assert res_404["category"] == "not_found"


