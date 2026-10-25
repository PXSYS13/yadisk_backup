"""Тесты санитизации путей — главная защита от записи мимо DOWNLOAD_DIR."""

import sqlite3

import pytest

from utils import (
    IS_WINDOWS,
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
    f_upper.write_text("dummy", encoding="utf-8")

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
    import utils
    from utils import resolve_local_path

    out_dir = str(tmp_path / "downloads")
    existing: set[str] = set()
    p1 = resolve_local_path(out_dir, "disk:/docs/file.txt", existing_paths=existing)
    p2 = resolve_local_path(out_dir, "disk:/docs/FILE.txt", existing_paths=existing)

    assert p1 != p2
    if utils.CASE_INSENSITIVE_PATHS:
        # NTFS и APFS не различают регистр — второй файл обязан получить хеш-суффикс
        assert p1.lower() != p2.lower()
    else:
        # На case-sensitive ФС это разные файлы, суффикс не нужен
        assert p1.lower() == p2.lower()


@pytest.mark.parametrize("folder, local_folder", [
    ("Встреча: итоги", "Встреча_ итоги"),
    ("Разное и т.д.", "Разное и т.д"),
    ("CON", "_CON"),
])
def test_cleaned_folder_keeps_file_names(tmp_path, folder, local_folder):
    """Чистка имени папки не трогает имена файлов внутри — связи между файлами проекта живы."""
    import os
    from utils import resolve_local_path

    out_dir = str(tmp_path / "downloads")
    p = resolve_local_path(out_dir, f"disk:/Загрузки/{folder}/Words/SomeDox.docx", existing_paths=set())
    assert p == os.path.join(out_dir, "Загрузки", local_folder, "Words", "SomeDox.docx")


def test_cleaned_file_name_still_gets_hash(tmp_path):
    import os
    from utils import _short_hash, resolve_local_path

    out_dir = str(tmp_path / "downloads")
    rp = "disk:/Документы/a?b.docx"
    assert resolve_local_path(out_dir, rp, existing_paths=set()) == \
        os.path.join(out_dir, "Документы", f"a_b_{_short_hash(rp)}.docx")


def test_twin_of_cleaned_folder_gets_hash_only_on_collision(tmp_path):
    import os
    from utils import _short_hash, resolve_local_path

    out_dir = str(tmp_path / "downloads")
    existing: set[str] = set()
    clean = resolve_local_path(out_dir, "disk:/a_b/x.txt", existing_paths=existing)
    twin = resolve_local_path(out_dir, "disk:/a:b/x.txt", existing_paths=existing)
    other = resolve_local_path(out_dir, "disk:/a:b/y.txt", existing_paths=existing)
    assert clean == os.path.join(out_dir, "a_b", "x.txt")
    assert twin == os.path.join(out_dir, "a_b", f"x_{_short_hash('disk:/a:b/x.txt')}.txt")
    assert other == os.path.join(out_dir, "a_b", "y.txt")  # совпадения нет — имя прежнее


def test_without_existing_paths_keeps_pre_231_rule(tmp_path):
    """Без набора занятых путей (ссылки, запасной путь downloader) — хеш при любой чистке, как раньше."""
    import os
    from utils import _short_hash, legacy_local_path, resolve_local_path

    out_dir = str(tmp_path / "downloads")
    rp = "disk:/Встреча: итоги/SomeDox.docx"
    old = os.path.join(out_dir, "Встреча_ итоги", f"SomeDox_{_short_hash(rp)}.docx")
    assert resolve_local_path(out_dir, rp) == old
    assert legacy_local_path(out_dir, rp) == old
    # Искать нечего: путь чистый или чистили само имя файла — для них правило не менялось
    assert legacy_local_path(out_dir, "disk:/Загрузки/SomeDox.docx") is None
    assert legacy_local_path(out_dir, "disk:/Встреча: итоги/a?b.docx") is None


def test_taken_hashed_name_gets_a_number(tmp_path):
    import os
    from utils import _short_hash, path_key, resolve_local_path

    out_dir = str(tmp_path / "downloads")
    rp = "disk:/a:b/x.txt"
    h = _short_hash(rp)
    existing = {path_key(os.path.join(out_dir, "a_b", "x.txt")),
                path_key(os.path.join(out_dir, "a_b", f"x_{h}.txt"))}
    assert resolve_local_path(out_dir, rp, existing_paths=existing) == \
        os.path.join(out_dir, "a_b", f"x_{h}_2.txt")


@pytest.mark.parametrize("path, clean", [
    ("disk:/Загрузки/Project1/Words/SomeDox.docx", True),
    ("disk:/Встреча: итоги/SomeDox.docx", False),
    ("disk:/Разное и т.д./x.txt", False),
    ("disk:/a/b?.txt", False),
    ("disk:/CON/x.txt", False),
])
def test_remote_path_is_clean(path, clean):
    from utils import remote_path_is_clean

    assert remote_path_is_clean(path) is clean


def test_path_key_folds_case_only_where_fs_ignores_it(monkeypatch):
    import os
    import utils

    upper, lower = os.path.join("Out", "Docs", "A.txt"), os.path.join("out", "docs", "a.txt")
    monkeypatch.setattr(utils, "CASE_INSENSITIVE_PATHS", True)
    assert utils.path_key(upper) == utils.path_key(lower)
    monkeypatch.setattr(utils, "CASE_INSENSITIVE_PATHS", False)
    assert utils.path_key(upper) != utils.path_key(lower)


@pytest.mark.skipif(not IS_WINDOWS, reason="смешанные слеши — один и тот же путь только на Windows")
def test_path_key_mixed_slashes_on_windows():
    from utils import path_key

    assert path_key("D:/YAD-bak\\a_b\\x.txt") == path_key("D:\\YAD-bak\\A_B\\x.txt")


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
    f1.write_text("dummy", encoding="utf-8")
    assert organize.get_date(str(f1)) == (2022, 4)

    f2 = tmp_path / "IMG_20210930_1234.png"
    f2.write_text("dummy", encoding="utf-8")
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
    unknown.write_text("data", encoding="utf-8")
    # Устанавливаем mtime на фиксированную дату (2020-02-01)
    import time
    ts = time.mktime((2020, 2, 1, 12, 0, 0, 0, 0, -1))
    os.utime(str(unknown), (ts, ts))

    assert organize.get_date(str(unknown)) == (2020, 2)






@pytest.mark.parametrize("name", ["COM0.txt", "LPT¹.jpg", "CON .txt", "nul .tar.gz", "CONIN$", "conout$.log"])
def test_less_known_reserved_windows_names_prefixed(name):
    """Windows считает устройствами и COM0/LPT¹, и «CON .txt» (пробел перед точкой)."""
    assert sanitize_component(name) == "_" + name


def test_names_too_long_for_any_filesystem_are_shortened():
    """Имя длиннее 255 байт не создать на Linux/macOS (255 знаков — на Windows): файл не скачался бы
    никогда. Укороченное сохраняет тип файла, а хеш не даёт слиться двум длинным именам."""
    from utils import MAX_NAME_LEN, name_len

    a, b = "Я" * 300 + "-а.docx", "Я" * 300 + "-б.docx"
    short_a, short_b = sanitize_component(a), sanitize_component(b)
    assert short_a.endswith(".docx") and short_a != short_b
    assert name_len(short_a) <= MAX_NAME_LEN - 16
    # Что помещалось раньше — не меняется (имена уже скачанных файлов остаются прежними)
    fits = "Я" * 120 + ".docx"
    assert sanitize_component(fits) == fits


def test_resolve_keeps_hashed_names_within_the_limit(tmp_path):
    from utils import MAX_NAME_LEN, name_len, resolve_local_path

    name = "a" * (MAX_NAME_LEN - 4) + ":.txt"  # чистка добавит хеш — имя вылезло бы за предел
    local = resolve_local_path(str(tmp_path), f"disk:/{name}")
    assert name_len(local.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]) <= MAX_NAME_LEN
    assert local.endswith(".txt")
    taken: set[str] = set()
    first = resolve_local_path(str(tmp_path), "disk:/" + "b" * MAX_NAME_LEN, taken)
    second = resolve_local_path(str(tmp_path), "disk:/" + "b" * MAX_NAME_LEN + "/../x", taken)
    assert first != second


def test_part_file_never_takes_a_real_file_name(tmp_path):
    """«видео.mp4.part» бывает настоящим файлом той же папки Диска — временный файл его не затрёт."""
    import os

    from utils import part_path_for

    target = os.path.join(str(tmp_path), "видео.mp4")
    part = part_path_for(target)
    assert os.path.dirname(part) == str(tmp_path)  # та же папка: переименование атомарно
    assert part != target + ".part" and part.endswith(".part")
    assert part_path_for(target) == part != part_path_for(target + ".part")


def test_file_too_big_for_fat32_is_caught_before_download(monkeypatch, tmp_path):
    import utils

    monkeypatch.setattr(utils, "filesystem_type", lambda path: "FAT32")
    assert utils.too_big_for_folder(str(tmp_path), 5 * 1024 ** 3)
    assert utils.too_big_for_folder(str(tmp_path), 4 * 1024 ** 3 - 1) is None
    monkeypatch.setattr(utils, "filesystem_type", lambda path: "NTFS")
    assert utils.too_big_for_folder(str(tmp_path), 5 * 1024 ** 3) is None


def test_file_specific_write_errors_are_told_from_disk_problems(monkeypatch):
    import errno

    import utils
    from utils import is_local_disk_error, local_error_kind

    monkeypatch.setattr(utils, "IS_WINDOWS", False)  # Linux и macOS: решает errno
    for code in (errno.ENAMETOOLONG, errno.EFBIG, errno.EINVAL):  # EINVAL — имя, которое FAT не примет
        assert local_error_kind(OSError(code, "x")) == "local_file"
    for code in (errno.ENOSPC, errno.EIO, errno.EACCES, errno.ENOENT):
        err = OSError(code, "x")
        assert is_local_disk_error(err) and local_error_kind(err) == "local_disk"


def _windows_error(code, winerror=None):
    """OSError, каким его делает Python на Windows: errno по таблице, код Win32 — в winerror.

    open() и write() идут через библиотеку C, у их ошибок кода Win32 нет (winerror None).
    """
    err = OSError(code, "x")
    err.winerror = winerror
    return err


@pytest.mark.parametrize("winerror,code,kind", [
    (123, "EINVAL", "local_file"),   # ERROR_INVALID_NAME: недопустимое имя
    (206, "ENOENT", "local_file"),   # ERROR_FILENAME_EXCED_RANGE: слишком длинное имя
    (223, "EINVAL", "local_file"),   # ERROR_FILE_TOO_LARGE
    (1167, "EINVAL", "local_disk"),  # ERROR_DEVICE_NOT_CONNECTED: выдернули флешку
    (64, "EINVAL", "local_disk"),    # ERROR_NETNAME_DELETED: отвалился сетевой диск
    (59, "EINVAL", "local_disk"),    # ERROR_UNEXP_NET_ERR
    (121, "EINVAL", "local_disk"),   # ERROR_SEM_TIMEOUT: NAS не ответил
    (1117, "EINVAL", "local_disk"),  # ERROR_IO_DEVICE
    (None, "EINVAL", "local_disk"),  # write() на пропавший диск или print в закрытый вывод
    (112, "ENOSPC", "local_disk"),   # ERROR_DISK_FULL
    (None, "ENAMETOOLONG", "local_file"),
])
def test_windows_einval_blames_the_disk_unless_win32_names_the_file(monkeypatch, winerror, code, kind):
    """На Windows EINVAL — это любой код Win32 без своего errno. Если считать его ошибкой файла,
    отвалившийся NAS переводит всю живую очередь в ошибки вместо остановки с кодом 4."""
    import errno

    import utils

    monkeypatch.setattr(utils, "IS_WINDOWS", True)
    err = _windows_error(getattr(errno, code), winerror)
    assert utils.is_local_disk_error(err)
    assert utils.local_error_kind(err) == kind


@pytest.mark.skipif(not IS_WINDOWS, reason="коды Win32 в errno переводит только Python на Windows")
def test_real_windows_errors_are_classified_by_win32_code():
    from utils import local_error_kind

    assert local_error_kind(OSError(None, "x", None, 123)) == "local_file"
    assert local_error_kind(OSError(None, "x", None, 206)) == "local_file"
    assert local_error_kind(OSError(None, "x", None, 1167)) == "local_disk"
    assert local_error_kind(OSError(None, "x", None, 64)) == "local_disk"


def test_path_key_joins_nfc_and_nfd_on_macos(monkeypatch):
    import unicodedata

    import utils

    nfc, nfd = unicodedata.normalize("NFC", "Отчёт.docx"), unicodedata.normalize("NFD", "Отчёт.docx")
    monkeypatch.setattr(utils.sys, "platform", "darwin")
    assert utils.path_key(nfc) == utils.path_key(nfd)
    monkeypatch.setattr(utils.sys, "platform", "win32")
    assert utils.path_key(nfc) != utils.path_key(nfd)  # NTFS их различает
