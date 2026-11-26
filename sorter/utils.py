"""Общие утилиты: пути Windows, санитизация имён, хеширование, даты файлов, SQLite и логи."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("photo_sorter")

IS_WINDOWS = sys.platform.startswith("win")
CASE_INSENSITIVE_PATHS = IS_WINDOWS or sys.platform == "darwin"
WIN_LONG_PATH_THRESHOLD = 240
MAX_FILENAME_BYTES = 240

MIN_PLAUSIBLE_TS = 315_532_800.0  # 1980-01-01 UTC
MAX_AHEAD_SEC = 86_400.0

RUSSIAN_MONTHS: dict[int, str] = {
    1: "01 Январь",
    2: "02 Февраль",
    3: "03 Март",
    4: "04 Апрель",
    5: "05 Май",
    6: "06 Июнь",
    7: "07 Июль",
    8: "08 Август",
    9: "09 Сентябрь",
    10: "10 Октябрь",
    11: "11 Ноябрь",
    12: "12 Декабрь",
}

_FORBIDDEN_NAME_CHARS = re.compile(r'[\x00-\x1f<>:"|?*\\/]+')
_URL_REDACT_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_WIN_LONG_DRIVE_RE = re.compile(r"^\\\\\?\\[A-Za-z]:\\")
_REPARSE_SYMLINK_TAGS: tuple[int, ...] = tuple(
    tag
    for tag in (
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", None),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None),
    )
    if tag is not None
)
_WIN_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


class CancelledError(Exception):
    """Операция остановлена пользователем."""


def ensure_utf8_stdio() -> None:
    """Переводит stdout/stderr в UTF-8 на Windows, чтобы кириллица не падала в cp1251/cp866."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except (OSError, ValueError, AttributeError) as e:
                log.debug("ensure_utf8_stdio: %s", e)


def redact_urls(text: str) -> str:
    """Скрывает query-параметры и хвосты URL в сообщениях об ошибках и логах."""
    if not text:
        return ""

    def _mask(m: re.Match[str]) -> str:
        raw = m.group(0)
        head = raw.split("?", 1)[0]
        parts = head.split("/", 4)
        if len(parts) >= 4:
            return "/".join(parts[:3]) + "/<redacted>"
        return head

    return _URL_REDACT_RE.sub(_mask, text)


def validate_dir_safe(
    raw_path: str, *, label: str = "папка", allow_unc: bool = False
) -> str:
    """Проверяет, что путь папки безопасен (без управляющих символов, UNC/device путей)."""
    if not isinstance(raw_path, str):
        raise ValueError(f"Некорректный путь ({label})")
    cleaned = raw_path.strip()
    if not cleaned:
        raise ValueError(f"Не указана {label}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError(f"Путь ({label}) содержит недопустимые управляющие символы")
    if not allow_unc:
        norm_slash = cleaned.replace("/", "\\")
        upper = norm_slash.upper()
        if (
            upper.startswith("\\\\.\\")
            or upper.startswith("\\\\??\\")
            or upper.startswith("\\\\?\\UNC\\")
        ):
            raise ValueError(
                "Сетевые UNC-пути и пути устройств отключены из соображений безопасности"
            )
        if upper.startswith("\\\\?\\"):
            if not _WIN_LONG_DRIVE_RE.match(norm_slash):
                raise ValueError(
                    "Недопустимый префикс пути устройства Windows (разрешены только локальные диски)"
                )
            if any(seg == ".." for seg in norm_slash[4:].split("\\")):
                raise ValueError(
                    "Сегменты '..' запрещены в путях с префиксом \\\\?\\"
                )
        elif norm_slash.startswith("\\\\"):
            raise ValueError(
                "Сетевые UNC-пути (\\\\сервер\\папка) отключены; подключите папку как локальный диск"
            )

    abs_path = os.path.abspath(cleaned)
    if not allow_unc:
        # Проверяем ближайшего существующего предка на разрешение в UNC/device (например, через symlink)
        cur = strip_long_prefix(abs_path)
        while cur:
            try:
                if os.path.exists(to_long_path(cur)):
                    real_raw = os.path.realpath(to_long_path(cur)).replace("/", "\\")
                    real_upper = real_raw.upper()
                    if (
                        real_upper.startswith("\\\\?\\UNC\\")
                        or real_upper.startswith("\\\\.\\")
                        or real_upper.startswith("\\\\??\\")
                        or (
                            real_raw.startswith("\\\\")
                            and not _WIN_LONG_DRIVE_RE.match(real_raw)
                        )
                    ):
                        raise ValueError(
                            "Сетевые UNC-пути и пути устройств отключены из соображений безопасности"
                        )
                    break
            except OSError:
                break
            parent = os.path.dirname(cur)
            if not parent or parent == cur:
                break
            cur = parent
    return abs_path


def strip_long_prefix(path: str) -> str:
    """Снимает Windows-префикс длинного пути (\\\\?\\ или \\\\?\\UNC\\)."""
    if path[:8].upper() == "\\\\?\\UNC\\":
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def is_symlink_or_junction(
    path: str | os.PathLike[str], st: os.stat_result | None = None
) -> bool:
    """Возвращает True для симлинков и Windows NTFS junction / mount points."""
    lp = to_long_path(os.fspath(path))
    if st is None:
        try:
            st = os.lstat(lp)
        except OSError:
            return False
    if stat.S_ISLNK(st.st_mode):
        return True
    reparse_tag = getattr(st, "st_reparse_tag", 0)
    if reparse_tag and reparse_tag in _REPARSE_SYMLINK_TAGS:
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if callable(isjunction):
        try:
            if isjunction(lp):
                return True
        except OSError:
            pass
    return False


def to_long_path(path: str) -> str:
    """Добавляет \\\\?\\ на Windows для длинных путей, чтобы os.stat/open не падали с ENOENT."""
    if not IS_WINDOWS or not path:
        return path
    if path.startswith("\\\\?\\"):
        return path
    abs_p = os.path.abspath(path)
    if len(abs_p) < WIN_LONG_PATH_THRESHOLD:
        return abs_p
    if abs_p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_p[2:]
    return "\\\\?\\" + abs_p


def path_key(path: str) -> str:
    """Нормализованный ключ пути для словарей и БД с учётом регистронезависимости ОС."""
    norm = os.path.normpath(strip_long_prefix(path))
    return norm.casefold() if CASE_INSENSITIVE_PATHS else norm


def is_path_inside(child_path: str, root_path: str) -> bool:
    """Проверяет после разрешения симлинков/junction, что child_path находится внутри root_path."""
    try:
        root_real = path_key(os.path.realpath(to_long_path(root_path)))
        child_real = path_key(os.path.realpath(to_long_path(child_path)))
        return os.path.commonpath([root_real, child_real]) == root_real
    except (OSError, ValueError):
        return False


def truncate_utf8_name(stem: str, ext: str, max_bytes: int = MAX_FILENAME_BYTES) -> str:
    """Укорачивает основу имени так, чтобы (stem + ext) в UTF-8 не превышал max_bytes."""
    ext_bytes = len(ext.encode("utf-8"))
    budget = max(1, max_bytes - ext_bytes)
    raw = stem.encode("utf-8")
    if len(raw) <= budget:
        return stem + ext
    cut = raw[:budget]
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    safe_stem = cut.decode("utf-8", errors="ignore").rstrip(". ") or "f"
    return safe_stem + ext


def sanitize_segment(name: str, fallback: str = "Без_имени", max_bytes: int = 120) -> str:
    """Чистит имя папки/человека: запрещает разделители, .., управляющие символы и имена Windows."""
    cleaned = unicodedata.normalize("NFC", name or "").strip()
    cleaned = _FORBIDDEN_NAME_CHARS.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(". ")
    if not cleaned or cleaned in (".", ".."):
        cleaned = fallback
    head, dot, tail = cleaned.partition(".")
    if head.strip().upper() in _WIN_RESERVED_NAMES:
        cleaned = f"{head}_{dot}{tail}" if dot else f"{cleaned}_"
    return truncate_utf8_name(cleaned, "", max_bytes)


def sanitize_filename(name: str, fallback_stem: str = "media") -> str:
    """Чистит имя файла с сохранением расширения и защитой от зарезервированных имён Windows."""
    cleaned = unicodedata.normalize("NFC", name or "").strip()
    cleaned = _FORBIDDEN_NAME_CHARS.sub("_", cleaned)
    cleaned = cleaned.strip(". ")
    if not cleaned:
        cleaned = fallback_stem
    stem, ext = os.path.splitext(cleaned)
    ext = ext[:20]
    stem = stem.strip(". ")
    if not stem or stem in (".", ".."):
        stem = fallback_stem
    head, dot, tail = stem.partition(".")
    if head.strip().upper() in _WIN_RESERVED_NAMES:
        stem = f"{head}_{dot}{tail}" if dot else f"{stem}_"
    return truncate_utf8_name(stem, ext, MAX_FILENAME_BYTES)


def name_with_tag(filename: str, tag: str, max_bytes: int = MAX_FILENAME_BYTES) -> str:
    """Вставляет суффикс перед расширением: IMG_0001.JPG + '_2' -> IMG_0001_2.JPG."""
    stem, ext = os.path.splitext(filename)
    return truncate_utf8_name(stem, tag + ext, max_bytes)


def compute_sha256(
    local_path: str,
    *,
    chunk_size: int = 1024 * 1024,
    stop_event: threading.Event | None = None,
) -> str:
    """Считает полный SHA-256 файла потоково (read-only)."""
    h = hashlib.sha256()
    with open(to_long_path(local_path), "rb") as f:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Остановлено пользователем во время хеширования")
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def compute_partial_hash(local_path: str, size: int | None = None) -> str:
    """Быстрый отпечаток файла (размер + до 64 КБ начала, середины и конца) для инкрементального скана."""
    lp = to_long_path(local_path)
    sample = 65536
    h = hashlib.sha256()
    with open(lp, "rb") as f:
        if size is None:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(0)
        h.update(str(size).encode("ascii"))
        h.update(b":")
        if size <= sample * 3:
            h.update(f.read())
        else:
            h.update(f.read(sample))
            f.seek(size // 2 - sample // 2)
            h.update(f.read(sample))
            f.seek(max(0, size - sample))
            h.update(f.read(sample))
    return h.hexdigest()


def plausible_ts(ts: float | int | None, now: float | None = None) -> float | None:
    """Возвращает timestamp, если дата между 1980 и (сейчас + 1 сутки), иначе None."""
    if ts is None:
        return None
    try:
        val = float(ts)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(val) or val < MIN_PLAUSIBLE_TS:
        return None
    ceiling = (time.time() if now is None else now) + MAX_AHEAD_SEC
    if val > ceiling:
        return None
    return val


@contextmanager
def sqlite_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Короткая транзакция BEGIN IMMEDIATE -> COMMIT / ROLLBACK."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as rb_err:
            log.warning("sqlite_tx rollback error: %s", rb_err)
        raise


_FATAL_STORAGE_ERRNOS: frozenset[int] = frozenset({errno.ENOSPC, errno.EROFS, errno.EIO})
_FATAL_STORAGE_WINERRORS: frozenset[int] = frozenset({112, 39, 19, 21})


def is_sqlite_disk_error(exc: BaseException) -> bool:
    """Фатальные ошибки SQLite (переполнение диска, I/O, повреждение БД, блокировка)."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    low = str(exc).lower()
    return any(
        t in low
        for t in (
            "disk is full",
            "disk i/o error",
            "readonly database",
            "unable to open database",
            "database disk image is malformed",
            "database is locked",
            "not a database",
        )
    )


def is_fatal_storage_error(exc: BaseException) -> bool:
    """Фатальные ошибки хранилища/БД: любая sqlite3.Error или дисковый сбой (ENOSPC/EROFS/EIO)."""
    if isinstance(exc, sqlite3.Error):
        return True
    if isinstance(exc, OSError):
        winerr = getattr(exc, "winerror", None)
        if winerr is not None and int(winerr) in _FATAL_STORAGE_WINERRORS:
            return True
        err_no = getattr(exc, "errno", None)
        if err_no is not None and int(err_no) in _FATAL_STORAGE_ERRNOS:
            return True
    return False


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= (n % 100) <= 19:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


def format_age_ru(years: int) -> str:
    """Форматирует возраст ребёнка для имени папки: 'меньше 1 года', '1 год', '4 года', '7 лет'."""
    if years <= 0:
        return "до 1 года"
    return f"{years} {plural(years, 'год', 'года', 'лет')}"


def files_text(n: int) -> str:
    return f"{int(n):,}".replace(",", " ") + " " + plural(n, "файл", "файла", "файлов")


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "0 Б"
    val = float(n)
    sign = "-" if val < 0 else ""
    val = abs(val)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if val < 1024.0 or unit == "ТБ":
            if unit == "Б":
                return f"{sign}{int(val)} {unit}"
            return f"{sign}{val:.1f} {unit}" if val < 100 else f"{sign}{val:.0f} {unit}"
        val /= 1024.0
    return f"{sign}{val:.1f} ТБ"


def hhmm(ts: float | None = None) -> str:
    t = time.time() if ts is None else ts
    return time.strftime("%H:%M:%S", time.localtime(t))


# --- Сохранение дат файлов и папок (Windows SetFileTime / macOS setattrlist / POSIX utime) ---

_WIN_EPOCH_DIFF = 11_644_473_600.0

if IS_WINDOWS:
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE
    _SetFileTime = _kernel32.SetFileTime
    _SetFileTime.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _SetFileTime.restype = wintypes.BOOL
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _FILE_WRITE_ATTRIBUTES = 0x0100
    _FILE_SHARE_ALL = 0x07
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


def _to_filetime(ts: float) -> _FILETIME:
    ticks = int(round((ts + _WIN_EPOCH_DIFF) * 10_000_000))
    return _FILETIME(ticks & 0xFFFFFFFF, (ticks >> 32) & 0xFFFFFFFF)


def _set_win_times(
    long_path: str, *, created: float | None, modified: float | None, is_dir: bool
) -> None:
    """Ставит CreationTime и LastWriteTime на Windows без разыменования симлинков/junction."""
    flags = _FILE_FLAG_OPEN_REPARSE_POINT | (
        _FILE_FLAG_BACKUP_SEMANTICS if is_dir else _FILE_ATTRIBUTE_NORMAL
    )
    handle = _CreateFileW(
        long_path,
        _FILE_WRITE_ATTRIBUTES,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ct = ctypes.byref(_to_filetime(created)) if created is not None else None
        mt_ft = _to_filetime(modified) if modified is not None else None
        mt = ctypes.byref(mt_ft) if mt_ft is not None else None
        if not _SetFileTime(handle, ct, mt, mt):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _CloseHandle(handle)


def _set_macos_birthtime(path: str, created: float) -> bool:
    """Ставит birthtime (ATTR_CMN_CRTIME) на macOS через libc setattrlist без следования ссылкам."""
    if sys.platform != "darwin":
        return False
    try:

        class _AttrList(ctypes.Structure):
            _fields_ = [
                ("bitmapcount", ctypes.c_uint16),
                ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32),
                ("volattr", ctypes.c_uint32),
                ("dirattr", ctypes.c_uint32),
                ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32),
            ]

        class _TimeSpec(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

        libc = ctypes.CDLL("libc.dylib", use_last_error=True)
        libc.setattrlist.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(_AttrList),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint,
        ]
        libc.setattrlist.restype = ctypes.c_int
        attr = _AttrList(5, 0, 0x00000200, 0, 0, 0, 0)
        sec = int(created)
        nsec = int((created - sec) * 1_000_000_000)
        ts_struct = _TimeSpec(sec, max(0, min(nsec, 999_999_999)))
        res: int = libc.setattrlist(
            os.fsencode(path),
            ctypes.byref(attr),
            ctypes.byref(ts_struct),
            ctypes.sizeof(ts_struct),
            0x00000001,
        )
        return res == 0
    except Exception:  # noqa: BLE001
        return False


def set_file_time(path: str, ts: float | None, created: float | None = None) -> bool:
    """Ставит обычному файлу дату изменения ts и дату создания created (Windows/macOS/Linux)."""
    when = plausible_ts(ts)
    birth = plausible_ts(created) if created is not None else None
    if when is None:
        when = birth
    if when is None:
        return False
    try:
        long_path = to_long_path(path)
        if not stat.S_ISREG(os.lstat(long_path).st_mode):
            return False
        effective_birth = min(birth, when) if birth is not None else when
        if IS_WINDOWS:
            _set_win_times(long_path, created=effective_birth, modified=when, is_dir=False)
            return True
        if os.utime in os.supports_follow_symlinks:
            os.utime(long_path, (when, when), follow_symlinks=False)
        else:
            os.utime(long_path, (when, when))
        if sys.platform == "darwin":
            _set_macos_birthtime(long_path, effective_birth)
        return True
    except (OSError, OverflowError, ValueError) as e:
        log.debug("set_file_time failed for %s: %s", path, e)
        return False


def set_dir_time(
    path: str, created: float | None = None, modified: float | None = None
) -> bool:
    """Ставит существующей папке дату создания и изменения без следования симлинкам."""
    c_ts = plausible_ts(created) if created is not None else None
    m_ts = plausible_ts(modified) if modified is not None else None
    if c_ts is None and m_ts is None:
        return False
    if m_ts is None:
        m_ts = c_ts
    if c_ts is None:
        c_ts = m_ts
    elif m_ts is not None:
        c_ts = min(c_ts, m_ts)
    try:
        long_path = to_long_path(path)
        st = os.lstat(long_path)
        if not stat.S_ISDIR(st.st_mode):
            return False
        if IS_WINDOWS:
            _set_win_times(long_path, created=c_ts, modified=m_ts, is_dir=True)
            return True
        if m_ts is not None:
            if os.utime in os.supports_follow_symlinks:
                os.utime(long_path, (m_ts, m_ts), follow_symlinks=False)
            else:
                os.utime(long_path, (m_ts, m_ts))
        if sys.platform == "darwin" and c_ts is not None:
            _set_macos_birthtime(long_path, c_ts)
        return True
    except (OSError, OverflowError, ValueError) as e:
        log.debug("set_dir_time failed for %s: %s", path, e)
        return False


def apply_folder_dates(folder_dates: Iterable[tuple[str, float | None, float | None]]) -> int:
    """Проставляет даты папкам снизу вверх, чтобы вложенные не сбрасывали mtime родителя."""
    unique: dict[str, tuple[str, float | None, float | None]] = {}
    for path, created, modified in folder_dates:
        if not path:
            continue
        key = os.path.normcase(os.path.abspath(path))
        prev = unique.get(key)
        if prev is None:
            unique[key] = (path, created, modified)
        else:
            unique[key] = (
                prev[0],
                prev[1] if prev[1] is not None else created,
                prev[2] if prev[2] is not None else modified,
            )
    ordered = sorted(
        unique.values(), key=lambda x: (len(Path(x[0]).parts), len(x[0])), reverse=True
    )
    updated = 0
    for path, created, modified in ordered:
        if set_dir_time(path, created=created, modified=modified):
            updated += 1
    return updated


@contextmanager
def keep_awake() -> Iterator[None]:
    """Не даёт компьютеру уснуть во время долгого сканирования или сортировки."""
    caffeinate = None
    if IS_WINDOWS:
        try:
            res = ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
            if not res:
                log.debug("SetThreadExecutionState returned 0")
        except Exception as e:  # noqa: BLE001
            log.debug("keep_awake Windows error: %s", e)
    elif sys.platform == "darwin":
        try:
            caffeinate = subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            log.debug("keep_awake caffeinate unavailable: %s", e)
            caffeinate = None
    try:
        yield
    finally:
        if IS_WINDOWS:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception as e:  # noqa: BLE001
                log.debug("keep_awake Windows reset error: %s", e)
        elif caffeinate is not None:
            try:
                caffeinate.terminate()
                try:
                    caffeinate.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    caffeinate.kill()
                    caffeinate.wait(timeout=2)
            except OSError as e:
                log.debug("keep_awake caffeinate stop error: %s", e)


class WindowsSafeRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler, не теряющий записи и бэкапы при WinError 32 (файл открыт на чтение)."""

    def __init__(
        self,
        filename: str | os.PathLike[str],
        maxBytes: int = 5 * 1024 * 1024,
        backupCount: int = 3,
        encoding: str = "utf-8",
        delay: bool = True,
    ) -> None:
        super().__init__(
            os.fspath(filename),
            mode="a",
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay,
        )

    def doRollover(self) -> None:
        if getattr(self, "_in_rollover", False):
            return
        self._in_rollover = True
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
            if self.backupCount > 0:
                try:
                    for i in range(self.backupCount - 1, 0, -1):
                        sfn = self.rotation_filename(f"{self.baseFilename}.{i}")
                        dfn = self.rotation_filename(f"{self.baseFilename}.{i + 1}")
                        if os.path.exists(sfn):
                            os.replace(sfn, dfn)
                    dfn = self.rotation_filename(f"{self.baseFilename}.1")
                    if os.path.exists(self.baseFilename):
                        os.replace(self.baseFilename, dfn)
                except OSError as exc:
                    if getattr(exc, "winerror", None) != 32:
                        log.warning(
                            "Не удалось выполнить ротацию файла лога %s: %s",
                            self.baseFilename,
                            exc,
                        )
            if not self.delay and self.stream is None:
                self.stream = self._open()
        finally:
            self._in_rollover = False
