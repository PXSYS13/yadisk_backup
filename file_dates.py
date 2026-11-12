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

import errno
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

from utils import files_text, is_inside, to_long_path, utc_ts

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


def _touch(path: str, when: float) -> bool:
    """Ставит дату обычному файлу. False — на месте файла папка или ссылка (дата досталась бы
    тому, на что она указывает). Ошибки файловой системы — наружу."""
    long_path = to_long_path(path)
    if not stat.S_ISREG(os.lstat(long_path).st_mode):
        return False
    if os.utime in os.supports_follow_symlinks:  # Linux, macOS; на Windows — только lstat выше
        os.utime(long_path, (when, when), follow_symlinks=False)
    else:
        os.utime(long_path, (when, when))
    return True


def set_file_time(path: str, ts) -> bool:
    """Ставит обычному файлу дату изменения ts. False — даты нет, это не файл или файловая
    система не дала. Не бросает."""
    when = plausible(ts)
    if when is None:
        return False
    try:
        return _touch(path, when)
    except (OSError, OverflowError, ValueError):
        return False


def _retry_helps(err: OSError) -> bool:
    """Повтор может помочь: сеть, занятый файл, сбой ввода-вывода."""
    return err.errno not in _PERSISTENT_ERRNOS


def _anchor(path: str) -> str:
    """Корень пути: «D:\\», «\\\\сервер\\папка\\» на Windows, «/» на Linux и macOS."""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return drive + os.sep if drive else os.sep


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
            drives[anchor] = os.path.isdir(anchor)
        return drives[anchor]

    res = Restored()
    for row in rows:
        verdict = _verdict(row, now, root, drive_present)
        if verdict == LATER:
            continue
        if verdict == FIX:
            try:
                if _touch(row.local_path, plausible(row.target, now)):
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
        if not os.path.isdir(to_long_path(root)):
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
