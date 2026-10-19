"""Папки Диска: дерево для выбора в «Настройках» и какие папки уже скачаны целиком."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import yadisk
from fastapi import APIRouter

from downloader import api_error_details, list_disk_folders, make_browse_client
from state import SELECTION_MARKERS
from unlim_common import DEFERRED_MARKER
from utils import remote_parts, sqlite_ro_uri, to_disk_path, to_long_path
from webapp import config, paths

log = logging.getLogger("webui")
router = APIRouter()

# Сводку по папкам верхнего уровня (Главная, лента) считаем по всей базе — не чаще раза в минуту
FOLDERS_SUMMARY_SEC = 60
# Столько папок одного уровня отдаём интерфейсу; об остальных — только число
FOLDER_PROGRESS_LIMIT = 500

# rest — путь внутри папки, cut — где кончается имя подпапки (0 — файл лежит прямо в папке)
_FOLDER_PROGRESS_SQL = """
SELECT child, COUNT(*) AS n, MAX(deep) AS deep,
       SUM(done) AS done, SUM(deferred) AS deferred, SUM(failed) AS failed, SUM(excluded) AS excluded,
       SUM(CASE WHEN excluded THEN 0 ELSE size END) AS b_sel,
       SUM(CASE WHEN done THEN size ELSE 0 END) AS b_done
FROM (
    SELECT CASE WHEN cut > 0 THEN substr(rest, 1, cut - 1) END AS child,
           cut > 0 AND instr(substr(rest, cut + 1), '/') > 0 AS deep,
           COALESCE(size, 0) AS size,
           status = 'downloaded' OR (status = 'skipped' AND COALESCE(error, '') NOT IN (?, ?)) AS done,
           status = 'pending' AND COALESCE(error, '') LIKE ? AS deferred,
           status = 'failed' AS failed,
           status = 'skipped' AND COALESCE(error, '') IN (?, ?) AS excluded
    FROM (SELECT substr(remote_path, ?) AS rest, instr(substr(remote_path, ?), '/') AS cut,
                 status, error, size
          FROM files WHERE substr(remote_path, 1, ?) = ?)
)
GROUP BY child
"""


def folder_split(parent: str) -> tuple[str, str]:
    """Папка Диска и префикс путей внутри неё: ('disk:/Фото', 'disk:/Фото/'), корень — ('disk:/', 'disk:/')."""
    path = to_disk_path((parent or "").strip() or "/").rstrip("/")
    if path == "disk:":
        return "disk:/", "disk:/"
    return path, path + "/"


def _progress_counts(r: sqlite3.Row) -> dict:
    excluded = int(r["excluded"] or 0)
    files = int(r["n"]) - excluded
    done, deferred, failed = int(r["done"] or 0), int(r["deferred"] or 0), int(r["failed"] or 0)
    return {"files": files, "done": done, "deferred": deferred, "failed": failed,
            "waiting": max(0, files - done - deferred - failed), "excluded": excluded,
            "bytes": int(r["b_sel"] or 0), "bytes_done": int(r["b_done"] or 0),
            "complete": files > 0 and done >= files}


def folder_progress(db_file: Path, parent: str = "disk:/", download_dir: str = "",
                    limit: int | None = FOLDER_PROGRESS_LIMIT) -> dict:
    """Как скачаны подпапки parent: сколько выбранных файлов каждой уже на компьютере.

    complete — всё выбранное в папке скачано. Такую папку можно переносить на другой диск:
    скачанное программа помнит по базе и второй раз не качает. Снятое выбором (категории,
    папки) не считается. Префикс сравнивается через substr, как в filter_to_folders: в именах
    бывают % и _. here — файлы прямо в parent; on_disk — подпапка есть в download_dir.
    """
    path, prefix = folder_split(parent)
    out: dict = {"ok": True, "path": path, "folders": [], "here": None,
                 "complete": 0, "total": 0, "more": 0, "empty": True}
    if not db_file.exists():
        return out
    c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        n = len(prefix)
        rows = c.execute(_FOLDER_PROGRESS_SQL, (*SELECTION_MARKERS, DEFERRED_MARKER + "%", *SELECTION_MARKERS,
                                                n + 1, n + 1, n, prefix)).fetchall()
    except sqlite3.Error as e:
        return {"ok": False, "path": path, "msg": f"База Диска занята, попробуйте через минуту: {e}"}
    finally:
        c.close()
    folders = []
    for r in rows:
        item = _progress_counts(r)
        if r["child"] is None:
            out["here"] = item
            continue
        item.update(name=r["child"], path=prefix + r["child"], has_sub=bool(r["deep"]))
        folders.append(item)
    folders.sort(key=lambda f: (f["name"].casefold(), f["name"]))
    counted = [f for f in folders if f["files"]]
    out.update(total=len(counted), complete=sum(1 for f in counted if f["complete"]), empty=not rows)
    if limit is not None and len(folders) > limit:
        out["more"] = len(folders) - limit
        folders = folders[:limit]
    if download_dir:
        for f in folders:
            local = os.path.join(download_dir, *remote_parts(f["path"])[1])
            f["on_disk"] = os.path.isdir(to_long_path(local))
    out["folders"] = folders
    return out


_folders_summary_cache: dict[str, tuple[float, dict | None]] = {}
_folders_summary_lock = threading.Lock()


def disk_folders_summary(db_file: Path, force: bool = False) -> dict | None:
    """Папки верхнего уровня Диска: всего и скачано целиком (names — их имена).

    Кэш на FOLDERS_SUMMARY_SEC: /api/status опрашивается каждые 1,5 секунды. None — список
    ещё не собран или базу не прочитать.
    """
    key = str(db_file)
    now = time.monotonic()
    with _folders_summary_lock:
        hit = _folders_summary_cache.get(key)
    if hit and not force and now - hit[0] < FOLDERS_SUMMARY_SEC:
        return hit[1]
    res = folder_progress(db_file, limit=None)
    data = None
    if res.get("ok") and not res.get("empty"):
        names = [f["name"] for f in res["folders"] if f["complete"]]
        data = {"total": res["total"], "complete": len(names), "names": names}
    with _folders_summary_lock:
        _folders_summary_cache[key] = (now, data)
    return data


# ── Демо: дерево и «Папки» без Яндекса ──

# Демо-дерево: (имя, общая, своя) — чтобы проверить экран без Яндекса
_DEMO_FOLDERS = {
    "disk:/": [("Документы", False, False), ("Загрузки", False, False), ("Работа", False, False),
               ("Семейный архив", True, False), ("Фото", False, False), ("Для мамы", True, True)],
    "disk:/Загрузки": [("Project1", False, False), ("Telegram", False, False)],
    "disk:/Загрузки/Project1": [("Photos", False, False), ("Words", False, False)],
    "disk:/Фото": [("2022", False, False), ("2023", False, False), ("2024", False, False)],
    "disk:/Семейный архив": [("Свадьба", True, False), ("Дача", True, False)],
    "disk:/Работа": [("Отчёты, 2024", False, False), ("Договоры 'старые'", False, False)],
}


def demo_folders(path: str) -> dict:
    items = sorted(_DEMO_FOLDERS.get(path.rstrip("/") if path != "disk:/" else path, []),
                   key=lambda item: (item[0].casefold(), item[0]))
    base = "disk:/" if path == "disk:/" else path.rstrip("/") + "/"
    return {"ok": True, "path": path, "truncated": False,
            "folders": [{"name": n, "path": base + n, "shared": s, "owned": o} for n, s, o in items]}


# Демо «Папок»: (имя, файлов, скачано, ждут Яндекса, не скачались, ГБ, есть подпапки, лежит в папке)
_DEMO_PROGRESS = {
    "disk:/": [("Видео", 120, 64, 56, 0, 41.0, False, True), ("Документы", 412, 412, 0, 0, 1.2, False, True),
               ("Загрузки", 236, 236, 0, 0, 3.4, False, True), ("Музыка", 1210, 1210, 0, 0, 7.8, False, False),
               ("Работа", 530, 498, 20, 12, 2.1, False, True), ("Фото", 3507, 2177, 1330, 0, 18.6, True, True)],
    "disk:/Фото": [("2022", 1102, 1102, 0, 0, 5.9, False, True), ("2023", 1240, 610, 630, 0, 6.5, False, True),
                   ("2024", 1153, 453, 700, 0, 6.2, False, True)],
}


def demo_folder_progress(parent: str) -> dict:
    path, prefix = folder_split(parent)
    folders = []
    for name, files, done, deferred, failed, size_gb, sub, on_disk in _DEMO_PROGRESS.get(path, []):
        size = int(size_gb * 1024 ** 3)
        folders.append({"name": name, "path": prefix + name, "files": files, "done": done,
                        "deferred": deferred, "failed": failed, "waiting": files - done - deferred - failed,
                        "excluded": 0, "bytes": size, "bytes_done": size * done // files,
                        "complete": done >= files, "has_sub": sub, "on_disk": on_disk})
    here = ({"files": 12, "done": 12, "deferred": 0, "failed": 0, "waiting": 0, "excluded": 0,
             "bytes": 48 * 1024 ** 2, "bytes_done": 48 * 1024 ** 2, "complete": True}
            if path == "disk:/Фото" else None)
    return {"ok": True, "path": path, "folders": folders, "here": here, "more": 0, "empty": not folders,
            "complete": sum(1 for f in folders if f["complete"]), "total": len(folders)}


def _demo_on(demo: str | None) -> bool:
    return bool(demo) and os.environ.get("YADISK_DEMO") == "1"


# ── Адреса ──


@router.get("/api/disk/folders")
def api_disk_folders(path: str = "disk:/", demo: str | None = None):
    """Подпапки для дерева в Настройках. Синхронная функция — опрос /api/status Яндекс не ждёт."""
    path = to_disk_path(path.strip() or "/")
    if path == "disk:":
        path = "disk:/"
    if _demo_on(demo):
        return demo_folders(path)
    token = config.get_config()["token"]
    if not token:
        return {"ok": False, "action": "token", "msg": "Чтобы выбрать папки, добавьте ключ доступа к Диску"}
    client = make_browse_client(token)
    try:
        folders, truncated = list_disk_folders(client, path)
    except (yadisk.exceptions.UnauthorizedError, yadisk.exceptions.ForbiddenError):
        return {"ok": False, "action": "token", "msg": "Яндекс не принял ключ доступа к Диску — получите новый"}
    except (yadisk.exceptions.PathNotFoundError, yadisk.exceptions.WrongResourceTypeError):
        return {"ok": False, "msg": "Этой папки больше нет на Диске — обновите список"}
    except Exception as e:  # noqa: BLE001 — человеку нужен понятный текст, подробности в журнал
        log.warning("дерево папок: %s%s", type(e).__name__, api_error_details(e))
        return {"ok": False, "msg": "Яндекс сейчас не ответил — попробуйте ещё раз через минуту"}
    finally:
        client.close()
    return {"ok": True, "path": path, "folders": folders, "truncated": truncated}


@router.get("/api/disk/folder_progress")
def api_disk_folder_progress(path: str = "disk:/", demo: str | None = None):
    """Какие папки Диска уже скачаны целиком — карточка «Папки» в «Подробнее» у Диска."""
    if _demo_on(demo):
        return demo_folder_progress(path)
    return folder_progress(paths.DB_FILE, path, config.get_config()["download_dir"])
