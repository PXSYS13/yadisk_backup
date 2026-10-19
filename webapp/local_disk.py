"""Локальный диск: свободное место, файловая система, размер папки скачивания.

Всё здесь дёргается из /api/status раз в 1,5 с, поэтому дорогое кешируется.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from utils import filesystem_type, is_fat_filesystem

# Меньше этого свободного места — «закончилось место», больше — значит, папка недоступна
LOW_FREE_BYTES = 1024 ** 3


def free_space(path: str) -> dict | None:
    """Свободное место на диске папки скачивания (или ближайшего существующего предка).

    fs — файловая система тома; fat — на него не записать файл больше 4 ГБ.
    """
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        usage = shutil.disk_usage(p)
    except OSError:
        return None
    fs = _fs_type_cached(str(p))
    return {"free": usage.free, "total": usage.total, "path": str(p), "fs": fs, "fat": is_fat_filesystem(fs)}


# Файловую систему спрашиваем не на каждый опрос статуса: она меняется, только если сменили папку
_FS_CACHE_SEC = 300
_fs_cache: dict[str, tuple[float, str | None]] = {}


def _fs_type_cached(path: str) -> str | None:
    now = time.monotonic()
    hit = _fs_cache.get(path)
    if hit and now - hit[0] < _FS_CACHE_SEC:
        return hit[1]
    fs = filesystem_type(path)
    _fs_cache[path] = (now, fs)
    return fs


# Кэш для тяжёлых операций (polling /api/status каждые 1.5 сек)
_local_size_cache: dict[str, dict] = {}
# 60 сек, а не 10: обход папки — это диск, а по нему в это же время идёт
# скачивание. /api/status опрашивается каждые 1.5 сек, так что дёшево тут важно.
_LOCAL_SIZE_TTL = 60  # секунд
_LOCAL_SIZE_MAX_PATHS = 4
_LOCAL_SIZE_WALK_BUDGET = 2.0  # макс секунд на обход
# В _unlim десятки тысяч фото лежат в одной папке — время проверяем и внутри неё
_WALK_CLOCK_EVERY = 2000  # записей папки


def _walk_size(path: str, deadline: float, clock: Callable[[], float] = time.monotonic) -> tuple[int, int, bool]:
    """(файлов, байт, не успели до deadline по clock) — обход через os.scandir.

    DirEntry.stat() на Windows берёт размер из самого листинга папки, без отдельного
    обращения к диску за каждым файлом, как было с os.walk + os.path.getsize. Ссылки
    на папки не разворачиваем: так обход не уйдёт за пределы папки и не зациклится.
    """
    files = size = seen = 0
    stack = [path]
    while stack:
        if clock() > deadline:
            return files, size, True
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    seen += 1
                    if not seen % _WALK_CLOCK_EVERY and clock() > deadline:
                        return files, size, True
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file():
                            size += entry.stat().st_size
                            files += 1
                    except OSError:
                        pass  # файл удалили или переименовали прямо во время обхода
        except OSError:
            pass  # папку не открыть (нет прав) — считаем остальное
    return files, size, False


def local_size(path: str) -> dict:
    r"""Размер папки с TTL-кэшем и лимитом времени обхода.

    partial=True — за _LOCAL_SIZE_WALK_BUDGET обход не успел (большая папка, медленный
    диск): в папке больше, чем files/bytes, и интерфейс пишет «больше X».
    Защита от корня диска (C:\) — не обходим, иначе зависнет на десятках ГБ.
    """
    if not os.path.isdir(path):
        return {"files": 0, "bytes": 0, "partial": False}

    abs_path = os.path.abspath(path)
    if len(abs_path) <= 3 and abs_path.endswith(":\\"):
        return {"files": 0, "bytes": 0, "partial": False, "skipped": "корень диска"}

    cached = _local_size_cache.get(path)
    now = time.time()
    if cached and (now - cached["at"] < _LOCAL_SIZE_TTL):
        return cached["data"]

    n, b, partial = _walk_size(path, time.monotonic() + _LOCAL_SIZE_WALK_BUDGET)
    data = {"files": n, "bytes": b, "partial": partial}
    # Ограничиваем размер кэша — выкидываем самый старый путь
    if len(_local_size_cache) >= _LOCAL_SIZE_MAX_PATHS and path not in _local_size_cache:
        oldest = min(_local_size_cache.items(), key=lambda x: x[1]["at"])[0]
        _local_size_cache.pop(oldest, None)
    _local_size_cache[path] = {"at": now, "data": data}
    return data
