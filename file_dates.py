"""Даты скачанных файлов: копия получает дату с Яндекса, а не дату скачивания.

Яндекс помнит, когда файл загрузили на Диск или изменили в последний раз (REST `modified`,
mpfs `mtime`), а у фото и видео — ещё и когда их сняли (`etime`). Исходной даты с компьютера
он не хранит, поэтому и официальный клиент ставит ту дату, что знает Яндекс.

Новый файл получает дату сразу после скачивания (set_file_time). Скачанным раньше её
возвращает restore: версии до 2.8.0 оставляли файлам Диска дату скачивания, Безлимит
до 2.8.9 дат не ставил вовсе. Меняется только дата, которую поставило скачивание или
копирование файла: если человек менял файл потом или у файла своя, более старая дата,
она остаётся.
"""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from utils import IS_WINDOWS, files_text, is_inside, to_long_path, utc_ts

# Раньше 1980 года дат не бывает: FAT и ZIP их не хранят, у Яндекса это мусор
MIN_FILE_TIME = 315_532_800.0  # 01.01.1980 UTC
# Дата из будущего — сбитые часы; сутки запаса на часовые пояса
MAX_AHEAD_SEC = 86_400.0
# FAT хранит время с точностью до 2 секунд
TOLERANCE_SEC = 2.0
# Файл дописан раньше, чем база отметила его скачанным. Час запаса — потому что FAT хранит
# местное время и после перехода на летнее или зимнее время дата файла сдвигается на час.
# Файл, изменённый позже, менял человек — его дату не трогаем
DOWNLOAD_SLACK_SEC = 3600.0
# Ошибки, которые повтор не исправит: нет прав, имя не годится этой системе, диск только
# для чтения. С ними файл считается проверенным, иначе его смотрели бы на каждом шаге
_PERSISTENT_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.ENAMETOOLONG, errno.EINVAL,
                                errno.ELOOP, errno.EROFS})
# Как часто проход по скачанным пишет в журнал, что он ещё идёт
PROGRESS_EVERY_SEC = 20.0
# Строка журнала для Web UI: сколько файлов шаг скачивания вернул к датам Яндекса
RESTORED_PREFIX = "dates_restored | "
# Метка — в начале строки, после времени журнала задачи и номера воркера. Имя файла внутри
# строки об ошибке («FAIL | … | <путь>») число в ленте не подделает
_RESTORED_LINE = re.compile(r"^(?:\d{2}:\d{2}:\d{2}\s+)?(?:\[w\d+\]\s+)?"
                            + re.escape(RESTORED_PREFIX) + r"(\{.*\})\s*$")

# Решения по одному файлу
FIX, DONE, LATER = "fix", "done", "later"

Mark = Callable[[list[tuple[float | None, str]]], None]


def plausible(ts, now: float | None = None) -> float | None:
    """Дата с Яндекса в секундах эпохи — или None, если её нет или она бессмысленна."""
    try:
        value = float(ts)
    except (TypeError, ValueError, OverflowError):  # OverflowError — целое на сотни цифр
        return None
    if not math.isfinite(value) or value < MIN_FILE_TIME:
        return None
    if value > (time.time() if now is None else now) + MAX_AHEAD_SEC:
        return None
    return value


_WIN_EPOCH_DIFF = 11_644_473_600.0  # секунд между 01.01.1601 и 01.01.1970 UTC


if IS_WINDOWS:
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE
    _SetFileTime = _kernel32.SetFileTime
    _SetFileTime.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
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


def _to_filetime(ts: float):
    ticks = int(round((ts + _WIN_EPOCH_DIFF) * 10_000_000))
    return _FILETIME(ticks & 0xFFFFFFFF, (ticks >> 32) & 0xFFFFFFFF)


def _set_win_times(long_path: str, *, created: float | None, modified: float | None, is_dir: bool) -> None:
    """Ставит CreationTime и LastWriteTime на Windows через CreateFileW + SetFileTime.

    FILE_FLAG_OPEN_REPARSE_POINT гарантирует, что символические ссылки и junction не разыменовываются.
    """
    flags = _FILE_FLAG_OPEN_REPARSE_POINT | (_FILE_FLAG_BACKUP_SEMANTICS if is_dir else _FILE_ATTRIBUTE_NORMAL)
    handle = _CreateFileW(long_path, _FILE_WRITE_ATTRIBUTES, _FILE_SHARE_ALL, None, _OPEN_EXISTING, flags, None)
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
                ("bitmapcount", ctypes.c_uint16), ("reserved", ctypes.c_uint16),
                ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32),
            ]

        class _TimeSpec(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

        libc = ctypes.CDLL("libc.dylib", use_last_error=True)
        libc.setattrlist.argtypes = [
            ctypes.c_char_p, ctypes.POINTER(_AttrList), ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_uint,
        ]
        libc.setattrlist.restype = ctypes.c_int
        attr = _AttrList(5, 0, 0x00000200, 0, 0, 0, 0)  # ATTR_BIT_MAP_COUNT=5, ATTR_CMN_CRTIME=0x200
        sec = int(created)
        nsec = int((created - sec) * 1_000_000_000)
        ts = _TimeSpec(sec, max(0, min(nsec, 999_999_999)))
        return libc.setattrlist(os.fsencode(path), ctypes.byref(attr), ctypes.byref(ts),
                                ctypes.sizeof(ts), 0x00000001) == 0  # FSOPT_NOFOLLOW
    except Exception:  # noqa: BLE001 — лучшая попытка; mtime уже выставлен через os.utime
        return False


def _touch(path: str, when: float, created: float | None = None) -> bool:
    """Ставит дату обычному файлу (modified и CreationTime/birthtime). False — на месте файла
    папка или ссылка (дата досталась бы тому, на что она указывает). Ошибки ФС — наружу."""
    long_path = to_long_path(path)
    if not stat.S_ISREG(os.lstat(long_path).st_mode):
        return False
    birth = min(created, when) if created is not None else when
    if IS_WINDOWS:
        _set_win_times(long_path, created=birth, modified=when, is_dir=False)
        return True
    if os.utime in os.supports_follow_symlinks:  # Linux, macOS; на Windows — только lstat выше
        os.utime(long_path, (when, when), follow_symlinks=False)
    else:
        os.utime(long_path, (when, when))
    if sys.platform == "darwin":
        _set_macos_birthtime(long_path, birth)
    return True


def set_file_time(path: str, ts, created=None) -> bool:
    """Ставит обычному файлу дату изменения ts (и дату создания created / min(created, ts)).
    False — даты нет, это не файл или файловая система не дала. Не бросает."""
    when = plausible(ts)
    birth = plausible(created) if created is not None else None
    if when is None:
        when = birth
    if when is None:
        return False
    try:
        return _touch(path, when, birth)
    except (OSError, OverflowError, ValueError):
        return False


def set_dir_time(path: str, created=None, modified=None) -> bool:
    """Ставит существующей локальной папке дату создания и изменения.

    Не создаёт папки и не следует по символическим ссылкам/junction. Не бросает.
    """
    c_ts = plausible(created) if created is not None else None
    m_ts = plausible(modified) if modified is not None else None
    if c_ts is None and m_ts is None:
        return False
    if m_ts is None:
        m_ts = c_ts
    if c_ts is None:
        c_ts = m_ts
    else:
        c_ts = min(c_ts, m_ts)
    try:
        long_path = to_long_path(path)
        st = os.lstat(long_path)
        if not stat.S_ISDIR(st.st_mode):
            return False
        if IS_WINDOWS:
            _set_win_times(long_path, created=c_ts, modified=m_ts, is_dir=True)
            return True
        if os.utime in os.supports_follow_symlinks:
            os.utime(long_path, (m_ts, m_ts), follow_symlinks=False)
        else:
            os.utime(long_path, (m_ts, m_ts))
        if sys.platform == "darwin":
            _set_macos_birthtime(long_path, c_ts)
        return True
    except (OSError, OverflowError, ValueError):
        return False


def apply_folder_dates(folder_dates: Iterable[tuple[str, float | None, float | None]]) -> int:
    """Проставляет даты папкам снизу вверх (сначала самые глубокие подпапки).

    Такой порядок гарантирует, что обновление вложенной папки не сбросит mtime родителя.
    Трогает только существующие папки; пустые не создаёт. Возвращает число обновлённых папок.
    """
    unique: dict[str, tuple[str, float | None, float | None]] = {}
    for path, created, modified in folder_dates:
        if not path:
            continue
        key = os.path.normcase(os.path.abspath(path))
        prev = unique.get(key)
        if prev is None:
            unique[key] = (path, created, modified)
        else:
            unique[key] = (prev[0], prev[1] if prev[1] is not None else created,
                           prev[2] if prev[2] is not None else modified)
    ordered = sorted(unique.values(), key=lambda x: (len(Path(x[0]).parts), len(x[0])), reverse=True)
    updated = 0
    for path, created, modified in ordered:
        if set_dir_time(path, created=created, modified=modified):
            updated += 1
    return updated


def _retry_helps(err: OSError) -> bool:
    """Повтор может помочь: сеть, занятый файл (WinError 32/33), сбой ввода-вывода."""
    if IS_WINDOWS and getattr(err, "winerror", None) in (32, 33):
        return True  # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION: файл открыт антивирусом/просмотрщиком
    return err.errno not in _PERSISTENT_ERRNOS


def _posix_mount_root(path: str) -> str | None:
    """Корень внешнего диска на Linux/macOS (/Volumes/USB, /media/user/USB, /mnt/USB) или None."""
    parts = [p for p in os.path.abspath(path).split("/") if p]
    if len(parts) >= 2 and parts[0] in ("Volumes", "mnt"):
        return "/" + "/".join(parts[:2])
    if len(parts) >= 3 and parts[0] == "media":
        return "/" + "/".join(parts[:3])
    if len(parts) >= 4 and parts[0] == "run" and parts[1] == "media":
        return "/" + "/".join(parts[:4])
    return None


def _anchor(path: str) -> str:
    """Корень пути: «D:\\», «\\\\сервер\\папка\\» на Windows, внешний том или «/» на Linux и macOS."""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if drive:
        return drive + os.sep
    return _posix_mount_root(path) or os.sep


def _has_files(root: str) -> bool:
    """В папке скачивания что-то есть. Пустая — это не та папка: например, на месте
    отключённого диска (Linux, macOS) программа только что создала пустую."""
    try:
        with os.scandir(to_long_path(root)) as entries:
            return any(True for _ in entries)
    except OSError:
        return False


class DateRow(NamedTuple):
    """Скачанный файл, которому, может быть, пора вернуть дату."""

    key: str  # remote_path Диска или file_id Безлимита — по нему база помечает строку
    local_path: str
    size: int | None  # размер на Яндексе
    target: float | None  # дата с Яндекса, как лежит в базе
    downloaded_at: str | None  # когда программа отметила файл скачанным (UTC из базы)
    created: float | None = None  # дата создания файла на Яндексе (если известна)


@dataclass
class Restored:
    fixed: int = 0  # сколько файлов получили дату
    # (дата, key) строк, которые больше смотреть не нужно, — их помечает база
    checked: list[tuple[float | None, str]] = field(default_factory=list)


def _verdict(row: DateRow, now: float, root: str, drive_present: Callable[[str], bool]) -> str:
    """FIX — поставить дату; DONE — смотреть больше нечего; LATER — попробовать в следующий раз.

    root — нынешняя папка скачивания (она есть: это проверяют до прохода и после порции).
    """
    target = plausible(row.target, now)
    if target is None:
        return DONE
    try:
        st = os.lstat(to_long_path(row.local_path))
    except (FileNotFoundError, NotADirectoryError):
        # Файл унесли, удалили или разложили сортировкой — программа его больше не ведёт.
        # Ждём только файл с отключённого диска (прежняя папка скачивания): он может вернуться
        if is_inside(row.local_path, root) or drive_present(row.local_path):
            return DONE
        return LATER
    except ValueError:
        return DONE  # путь, который не открыть (нулевой символ), — смотреть нечего
    except OSError as e:
        return LATER if _retry_helps(e) else DONE
    if not stat.S_ISREG(st.st_mode):
        return DONE  # на месте файла папка или ссылка — не наше
    if abs(st.st_mtime - target) <= TOLERANCE_SEC:
        return DONE  # дата уже с Яндекса
    saved = utc_ts(row.downloaded_at)
    if row.size is None or st.st_size != row.size or saved is None:
        return DONE  # другой файл или неизвестно, когда скачан, — не трогаем
    # Дату поставило скачивание: она позже яндексовской и не позже отметки «скачан».
    # Более старая дата — чья-то исходная, более поздняя — правка человека
    if target + TOLERANCE_SEC < st.st_mtime <= saved + DOWNLOAD_SLACK_SEC:
        return FIX
    return DONE


def restore(rows: Iterable[DateRow], root: str, now: float | None = None) -> Restored:
    """Возвращает дату с Яндекса файлам, чью дату поставило скачивание. root — папка скачивания."""
    now = time.time() if now is None else now
    drives: dict[str, bool] = {}  # есть ли диск — один раз на корень пути за проход

    def drive_present(path: str) -> bool:
        anchor = _anchor(path)
        if anchor not in drives:
            if not IS_WINDOWS and anchor != os.sep and _posix_mount_root(path) == anchor:
                # На Linux/macOS точка монтирования отключённого диска бывает пустой папкой
                drives[anchor] = _has_files(anchor)
            else:
                drives[anchor] = os.path.isdir(anchor)
        return drives[anchor]

    res = Restored()
    for row in rows:
        verdict = _verdict(row, now, root, drive_present)
        if verdict == LATER:
            continue
        if verdict == FIX:
            try:
                birth = plausible(row.created, now) if row.created is not None else None
                if _touch(row.local_path, plausible(row.target, now), birth):
                    res.fixed += 1
            except OSError as e:
                if _retry_helps(e):
                    continue  # файл занят или сеть — в следующий раз
            except (OverflowError, ValueError):
                pass
        res.checked.append((row.target, row.key))
    return res


def restore_all(portions: Iterable[list[DateRow]], mark: Mark, root: str,
                say: Callable[[str], None] | None = None) -> int:
    """Проходит по скачанным порциями: ставит даты и помечает проверенные строки в базе.

    Возвращает, сколько файлов получили дату. Помечает после каждой порции, поэтому
    прерванный проход (остановили, выключили компьютер) продолжится с того же места.
    Папки скачивания root нет или она пустая (диск отключён) — прохода нет: иначе все файлы
    сочлись бы унесёнными. Пропала посреди прохода — проход кончается, а порция не
    помечается: её «пропавшие» файлы на самом деле никуда не делись.
    """
    if not _has_files(root):
        return 0
    fixed = seen = 0
    last = time.monotonic()
    for portion in portions:
        res = restore(portion, root)
        fixed += res.fixed
        if not _has_files(root):
            break
        if res.checked:
            mark(res.checked)
        seen += len(portion)
        if say and time.monotonic() - last >= PROGRESS_EVERY_SEC:
            say(f"Возвращаю файлам даты с Яндекса: проверено {files_text(seen)}")
            last = time.monotonic()
    return fixed


def restored_text(n: int) -> str:
    """Для ленты и консоли: сколько файлов получили дату с Яндекса."""
    return f"Вернул файлам даты с Яндекса вместо даты скачивания: {files_text(n)}"


def restored_line(n: int) -> str:
    """Строка журнала для Web UI: Rescue читает её после шага скачивания."""
    return RESTORED_PREFIX + json.dumps({"count": int(n)})


def restored_count(lines: Iterable[str | None]) -> int:
    """Сколько файлов вернули к датам Яндекса — по последней такой строке журнала шага."""
    count = 0
    for line in lines:
        m = _RESTORED_LINE.match(line or "")
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        value = data.get("count") if isinstance(data, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            count = value
    return count
