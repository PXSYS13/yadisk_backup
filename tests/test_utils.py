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


def test_resolve_local_path_deterministic_collision_resolution(tmp_path):
    from utils import resolve_local_path

    out_dir = str(tmp_path / "downloads")
    # Три удалённых пути, которые раньше санитизировались в одно имя a_b.jpg
    p_colon = resolve_local_path(out_dir, "disk:/photos/a:b.jpg")
    p_question = resolve_local_path(out_dir, "disk:/photos/a?b.jpg")
    p_underscore = resolve_local_path(out_dir, "disk:/photos/a_b.jpg")

    assert p_colon != p_question
    assert p_colon != p_underscore
    assert p_question != p_underscore

    # Стандартный путь без спецсимволов сохраняет чистое имя без хеша (не ломает resume)
    assert p_underscore.endswith("photos/a_b.jpg") or p_underscore.endswith("photos\\a_b.jpg")

    # Повторные вызовы детерминированы
    assert resolve_local_path(out_dir, "disk:/photos/a:b.jpg") == p_colon


def test_resolve_local_path_windows_case_collision(tmp_path):
    from utils import IS_WINDOWS, resolve_local_path

    out_dir = str(tmp_path / "downloads")
    existing: set[str] = set()
    p1 = resolve_local_path(out_dir, "disk:/docs/file.txt", existing_paths=existing)
    p2 = resolve_local_path(out_dir, "disk:/docs/FILE.txt", existing_paths=existing)

    assert p1 != p2
    if IS_WINDOWS:
        # NTFS не различает регистр — второй файл обязан получить хеш-суффикс
        assert p1.lower() != p2.lower()
    else:
        # На case-sensitive ФС это разные файлы, суффикс не нужен
        assert p1.lower() == p2.lower()


def test_is_safe_download_dir(tmp_path):
    import os
    from utils import is_safe_download_dir

    # Корни дисков
    ok, msg = is_safe_download_dir("C:\\")
    assert ok is False
    assert "корень" in msg

    ok, msg = is_safe_download_dir("D:\\")
    assert ok is False

    # Системная папка
    ok, msg = is_safe_download_dir("C:\\Windows")
    assert ok is False
    assert "Системная папка" in msg

    # Домашняя папка целиком
    home = os.path.expanduser("~")
    if home:
        ok, msg = is_safe_download_dir(home)
        assert ok is False
        assert "домашнюю" in msg

    # Пустой и относительный
    assert is_safe_download_dir("")[0] is False
    assert is_safe_download_dir("relative/path")[0] is False

    # Нормальная папка
    safe_dir = str(tmp_path / "my_backup")
    ok, _ = is_safe_download_dir(safe_dir)
    assert ok is True


def test_is_safe_dir_long_path_prefix_does_not_bypass_checks():
    from utils import is_safe_dir

    assert is_safe_dir(r"\\?\C:\Windows\Temp\bk")[0] is False
    assert is_safe_dir(r"\\?\C:\\")[0] is False


def test_local_disk_errors_are_recognized():
    import errno

    from utils import classify_yandex_error, is_local_disk_error

    no_space = OSError(errno.ENOSPC, "No space left on device", r"D:\backup\IMG_5003.jpg.part")
    assert is_local_disk_error(no_space) is True
    assert is_local_disk_error(ConnectionResetError(104, "reset")) is False
    assert is_local_disk_error(FileNotFoundError(errno.ENOENT, "gone")) is True

    # В пути «5003» и «notfound» — раньше это считалось сбоем Яндекса / 404
    diag = classify_yandex_error(no_space)
    assert diag["category"] == "local_disk"
    assert diag["is_yandex_server"] is False
    assert classify_yandex_error("local_disk | OSError: boom")["category"] == "local_disk"


def test_is_safe_dir_must_exist(tmp_path):
    from utils import is_safe_dir

    missing = str(tmp_path / "нет-такой")
    ok, msg = is_safe_dir(missing, must_exist=True)
    assert ok is False
    assert "не существует" in msg
    # Та же папка годится как цель скачивания — её создадут
    assert is_safe_dir(missing, must_exist=False)[0] is True

    ok, msg = is_safe_dir("relative/path", must_exist=True)
    assert ok is False
    assert "абсолютным" in msg


def test_safe_int():
    from utils import safe_int

    assert safe_int("4") == 4
    assert safe_int("garbage", default=4) == 4
    assert safe_int(None, default=5) == 5
    assert safe_int("-1", min_val=1) == 1
    assert safe_int("999", max_val=16) == 16


def test_organize_get_date_filename_regex(tmp_path):
    import organize

    f1 = tmp_path / "2022-04-15_vacation.jpg"
    f1.write_text("dummy")
    assert organize.get_date(str(f1)) == (2022, 4)

    f2 = tmp_path / "IMG_20210930_1234.png"
    f2.write_text("dummy")
    assert organize.get_date(str(f2)) == (2021, 9)


def test_organize_get_date_jpeg_exif(tmp_path):
    import organize

    # Создаём валидный минимальный JPEG с сегментом APP1 Exif и датой 2017:11:05
    exif_content = (
        b"Exif\x00\x00II*\x00\x08\x00\x00\x00\x01\x00\x03\x90\x02\x00\x14\x00\x00\x00"
        b"\x1a\x00\x00\x00\x00\x00\x00\x002017:11:05 15:30:00\x00"
    )
    app1 = b"\xff\xe1" + (len(exif_content) + 2).to_bytes(2, "big") + exif_content
    jpeg_data = b"\xff\xd8" + app1 + b"\xff\xd9"

    img_file = tmp_path / "DSC_0001.jpg"
    img_file.write_bytes(jpeg_data)

    assert organize.get_date(str(img_file)) == (2017, 11)


def test_organize_get_date_mtime_fallback(tmp_path):
    import os
    import organize

    unknown = tmp_path / "random_file.doc"
    unknown.write_text("data")
    # Устанавливаем mtime на фиксированную дату (2020-02-01)
    import time
    ts = time.mktime((2020, 2, 1, 12, 0, 0, 0, 0, -1))
    os.utime(str(unknown), (ts, ts))

    assert organize.get_date(str(unknown)) == (2020, 2)




