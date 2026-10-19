"""Кнопки интерфейса: запуск и остановка, повторы, настройки, папки, проверка скачанного,
«Забыть список», сортировка."""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from state import State
from unlim_common import requeue_verify_problems, reset_failed_files
from utils import (
    CATEGORY_LABELS,
    DEFAULT_UNLIM_BATCH,
    IS_WINDOWS,
    LINKS_DIRNAME,
    PREVIEW_DIRNAME,
    encode_disk_folders,
    files_text,
    is_safe_dir,
    is_safe_download_dir,
    normalize_disk_folders,
    remote_parts,
    sqlite_ro_uri,
    to_disk_path,
    to_long_path,
)
from webapp import access, config, paths
from webapp.local_disk import free_space
from webapp.queue_db import list_problem_files, preview_db, reset_failed_disk_files
from webapp.rescue import rescue
from webapp.tasks import (
    SLOT_DISK,
    SLOT_LINKS,
    SLOT_ORGANIZE,
    SLOT_UNLIM_COLLECT,
    UNLIM_SLOT_PREFIX,
    organize_moving,
    stop_and_wait,
    tm,
)

log = logging.getLogger("webui")
router = APIRouter()


def _resp(t: tuple[bool, str]) -> dict:
    return {"ok": t[0], "msg": t[1]}


# Папки самой программы: скачанное с Диска легло бы поверх её файлов и баз
PROGRAM_SUBDIRS = ("logs", "webui_assets", "webapp", "docs", "tests", "tools", ".git", ".venv")
ORGANIZE_BUSY = "Сначала дождитесь конца сортировки: она переносит файлы, и скачивание их перепутает"


# ── Настройки: что и как качать ──


class SettingsPayload(BaseModel):
    sources: list[str] | None = None
    categories: list[str] | None = None
    all_categories: bool = False
    disk_folders: list[str] | None = None
    all_folders: bool = False
    gentle: bool | None = None
    previews: bool | None = None
    resume: bool | None = None


@router.get("/api/settings")
def api_get_settings():
    return config.get_settings()


@router.post("/api/settings")
def api_set_settings(p: SettingsPayload):
    changes_what = (p.sources is not None or p.categories is not None or p.all_categories
                    or p.disk_folders is not None or p.all_folders)
    if changes_what and rescue.is_running():
        raise HTTPException(409, "Сначала остановите скачивание — потом можно изменить, что качать")
    gentle_was = config.get_settings()["gentle"] if p.gentle is not None else None
    updates: dict[str, str] = {}
    folders_note = ""
    if p.all_folders:
        updates["BACKUP_DISK_FOLDERS"] = ""
        folders_note = "весь Диск"
    elif p.disk_folders is not None:
        try:
            folders = normalize_disk_folders(p.disk_folders)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if not folders:
            raise HTTPException(400, "Отметьте хотя бы одну папку или выберите «Весь Диск»")
        updates["BACKUP_DISK_FOLDERS"] = encode_disk_folders(folders)
        folders_note = f"{len(folders)} папок"
    if p.sources is not None:
        sources = [s for s in config.SOURCES if s in p.sources]
        if not sources:
            raise HTTPException(400, "Выберите хотя бы одно: Диск или Безлимит")
        updates["BACKUP_SOURCES"] = ",".join(sources)
    if p.all_categories:
        updates["BACKUP_CATEGORIES"] = ""
    elif p.categories is not None:
        cats = [c for c in p.categories if c in CATEGORY_LABELS]
        if not cats:
            raise HTTPException(400, "Выберите хотя бы один тип файлов")
        updates["BACKUP_CATEGORIES"] = "" if set(cats) == set(CATEGORY_LABELS) else ",".join(cats)
    if p.gentle is not None:
        updates["GENTLE_MODE"] = "1" if p.gentle else "0"
    if p.previews is not None:
        updates["YADISK_PREVIEWS"] = "1" if p.previews else "0"
    if p.resume is not None:
        updates["BACKUP_RESUME_ON_START"] = "1" if p.resume else "0"
    config.ensure_env_file()
    for key, value in updates.items():
        config.env_set_verified(key, value)
    # Имена папок в журнал не пишем: журнал уходит в отчёт для поддержки
    shown = [f"{k}={folders_note if k == 'BACKUP_DISK_FOLDERS' else v}" for k, v in updates.items()]
    log.info("настройки сохранены: %s", ", ".join(shown) or "без изменений")
    out = {"ok": True, "settings": config.get_settings()}
    # Число одновременных скачиваний задаётся при запуске: идущее скачивание
    # переключатель не меняет, и человек ждал бы, что интернет освободится сразу
    if p.gentle is not None and p.gentle != gentle_was and rescue.is_running():
        out["msg"] = (f"{'Бережный' if p.gentle else 'Обычный'} режим заработает после перезапуска: "
                      f"нажмите на Главной «Остановить», а потом «Продолжить». Скачанное сохранится.")
    return out


class DownloadDirPayload(BaseModel):
    download_dir: str


@router.post("/api/config/download_dir")
def api_set_dir(payload: DownloadDirPayload):
    d = (payload.download_dir or "").strip()
    if rescue.is_running():
        # Раунды спасения качают в одну папку; смена посреди них расколола бы бэкап
        raise HTTPException(409, "Сначала остановите скачивание — потом можно сменить папку")
    if tm.any_alive(SLOT_LINKS):
        raise HTTPException(409, "Сначала остановите скачивание по ссылкам — потом можно сменить папку")
    if organize_moving():
        raise HTTPException(409, ORGANIZE_BUSY)
    ok, msg = is_safe_download_dir(d)
    if ok and _inside_program(d):
        ok, msg = False, "Это папка самой программы: файлы с Диска легли бы поверх её файлов. Выберите другую"
    if not ok:
        log.warning("папка отклонена: %r — %s", d, msg)
        raise HTTPException(400, msg)
    config.env_set("DOWNLOAD_DIR", d)
    log.info("папка скачивания сохранена: %s", d)
    return {"ok": True, "download_dir": d, "free_space": free_space(d)}


def _inside_program(path: str) -> bool:
    """Сама папка программы или её служебная подпапка (logs, webui_assets …)."""
    try:
        p = Path(path).resolve()
    except OSError:
        return False
    if p == paths.PROJECT:
        return True
    return any(p == paths.PROJECT / sub or (paths.PROJECT / sub) in p.parents for sub in PROGRAM_SUBDIRS)


# ── «Режим спасения»: запуск, повторы, остановка ──


def rescue_workers(settings: dict) -> tuple[int, int]:
    """Бережный режим — вдвое меньше одновременных скачиваний."""
    return (2, DEFAULT_UNLIM_BATCH) if settings.get("gentle") else (4, DEFAULT_UNLIM_BATCH)


@router.post("/api/rescue/start")
def api_rescue_start():
    """Главная кнопка «Сохранить мои файлы»: всё по сохранённым настройкам."""
    st = config.get_settings()
    do_disk, do_unlim = "disk" in st["sources"], "unlim" in st["sources"]
    if rescue.is_running():
        return _resp(rescue.start())  # идёт — значит «попробовать отложенные сейчас»
    if organize_moving():
        return {"ok": False, "msg": ORGANIZE_BUSY}
    if verify_running():
        return {"ok": False, "msg": VERIFY_BUSY}
    src = access.preflight_sources(do_disk, do_unlim)
    if src["error"]:
        return {"ok": False, "msg": src["error"]}
    workers, batch = rescue_workers(st)
    ok, msg = rescue.start(workers=workers, batch=batch, do_disk=do_disk, do_unlim=do_unlim,
                           disk_categories=st["categories"], unlim_categories=st["categories"],
                           disk_folders=st["disk_folders"])
    if ok:
        config.set_resume_pending(True)
    return {"ok": ok, "msg": msg, "notes": src["notes"] if ok else []}


def resume_after_restart() -> bool:
    """Программу закрыли посреди скачивания (окно, перезагрузка, пропал свет) — продолжаем сами,
    как просили в сообществе. Только если человек не нажимал «Остановить» и не выключил это
    в «Настройках». True — скачивание запущено."""
    if os.environ.get("YADISK_DEMO") == "1" or rescue.is_running():
        return False
    if not config.resume_pending() or not config.get_settings()["resume"]:
        return False
    res = api_rescue_start()
    if res.get("ok"):
        log.info("продолжаю скачивание после перезапуска программы")
        print("  Скачивание шло, когда программу закрыли, — продолжаю его сам.")
        rescue.note("run", "Программу закрыли посреди скачивания — продолжаю с того же места")
        return True
    log.info("после перезапуска скачивание не продолжил: %s", res.get("msg"))
    rescue.note("warn", f"Не получилось продолжить скачивание после перезапуска: {res.get('msg')}")
    return False


class RetryFailedPayload(BaseModel):
    source: str = "all"  # disk | unlim | all


@router.post("/api/rescue/retry_failed")
def api_rescue_retry_failed(p: RetryFailedPayload):
    """«Повторить для N файлов»: упавшие — снова в очередь, и сразу качать."""
    if verify_running():
        return {"ok": False, "msg": VERIFY_BUSY}
    n = 0
    if p.source in ("disk", "all"):
        try:
            n += reset_failed_disk_files()
        except sqlite3.Error as e:
            return {"ok": False, "msg": f"База Диска занята: {e}"}
    if p.source in ("unlim", "all") and paths.UNLIM_DB_FILE.exists():
        n += reset_failed_files(str(paths.UNLIM_DB_FILE))
    if rescue.is_running():
        ok, msg = rescue.requeue_now(p.source)
        if ok is not None:
            return {"ok": ok, "msg": f"Вернул в очередь {n} файлов: {msg}" if ok else msg, "count": n}
        # Спасение уже заканчивало — даём ему закрыться и запускаем заново
        rescue.wait_finished(3)
    res = api_rescue_start()
    res["count"] = n
    if res.get("ok"):
        res["msg"] = f"Вернул в очередь {n} файлов и запустил скачивание"
    return res


@router.post("/api/rescue/retry_now")
def api_rescue_retry_now():
    """«Попробовать сейчас»: не ждать раунда, повторить отложенные файлы немедленно."""
    return _resp(rescue.retry_now())


class RelistPayload(BaseModel):
    source: str = Field(default="all", pattern="^(disk|unlim|all)$")


@router.post("/api/rescue/relist")
def api_rescue_relist(p: RelistPayload):
    """«Обновить список» и «Проверить новые файлы» (all): собрать список заново, не дожидаясь
    шести часов — когда на Диск только что добавили файлы.

    Идёт спасение — пересобирает ветки этих источников; не идёт — запускает его по настройкам.
    """
    enabled = config.get_settings()["sources"]
    sources = [s for s in ("disk", "unlim") if s in enabled] if p.source == "all" else [p.source]
    if not sources or not set(sources) <= set(enabled):
        return {"ok": False, "msg": "Этот источник выключен в «Настройках» → «Что скачивать»"}
    if verify_running():
        return {"ok": False, "msg": VERIFY_BUSY}
    results = [rescue.relist(s) for s in sources]
    if any(ok is None for ok, _ in results):
        return api_rescue_start()
    done = [msg for ok, msg in results if ok]
    return {"ok": bool(done), "msg": done[0] if done else results[0][1]}


@router.post("/api/start/stop")
def api_start_stop():
    # «Остановить» — воля человека: после перезапуска программа сама не продолжит
    config.set_resume_pending(False)
    return _resp(rescue.stop())


# ── Что не скачалось, где лежат файлы ──


@router.get("/api/{source}/failed")
def api_problem_files(source: str, limit: int = 200):
    limit = max(1, min(limit, 2000))
    if source == "disk":
        return list_problem_files(paths.DB_FILE, "files", "remote_path", limit)
    if source == "unlim":
        return list_problem_files(paths.UNLIM_DB_FILE, "unlim_files", "file_id", limit)
    raise HTTPException(404, "Нет такого источника")


@router.get("/api/disk/preview")
def disk_preview():
    return preview_db(paths.DB_FILE, "files", "remote_path")


@router.get("/api/unlim/preview")
def unlim_preview():
    return preview_db(paths.UNLIM_DB_FILE, "unlim_files", "file_id")


class OpenFolderPayload(BaseModel):
    which: str = "download"  # download | unlim | previews | links | disk_folder
    link_id: int | None = Field(None, ge=1, le=2 ** 63 - 1)  # для links: папка одной ссылки
    path: str | None = None  # для disk_folder: папка Диска, например disk:/Фото/2023


def _link_folder(base: Path, link_id: int) -> Path | None:
    """Папка ссылки — только из базы и только внутри папки скачивания."""
    if not paths.LINKS_DB_FILE.exists():
        return None
    c = sqlite3.connect(sqlite_ro_uri(paths.LINKS_DB_FILE), uri=True, timeout=5)
    try:
        row = c.execute("SELECT local_dir, kind FROM links WHERE id=?", (link_id,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        c.close()
    if not row or not row[0]:
        return None
    path = base.joinpath(*row[0].split("/"))
    if row[1] == "file":
        path = path.parent  # ссылка на один файл — открываем папку, где он лежит
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return None
    return path


def _disk_folder_local(base: Path, remote: str) -> Path | None:
    """Папка Диска внутри папки скачивания — с теми же очищенными именами, что при скачивании."""
    parts = remote_parts(to_disk_path(remote.strip()))[1]
    if not parts:
        return None
    path = base.joinpath(*parts)
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return None
    return path


@router.post("/api/open_folder")
def api_open_folder(p: OpenFolderPayload):
    """Открывает папку скачивания в проводнике. Только настроенную — никаких чужих путей."""
    base = Path(config.get_config()["download_dir"])
    sub = {"download": "", "unlim": "_unlim", "previews": PREVIEW_DIRNAME, "links": LINKS_DIRNAME,
           "disk_folder": ""}.get(p.which)
    if sub is None:
        raise HTTPException(400, "Неизвестная папка")
    path = base / sub if sub else base
    if p.which == "links" and p.link_id is not None:
        path = _link_folder(base, p.link_id) or path
    if p.which == "disk_folder":
        path = _disk_folder_local(base, p.path or "")
        if path is None:
            raise HTTPException(400, "Неизвестная папка")
        if not os.path.isdir(to_long_path(str(path))):
            return {"ok": False, "msg": "Этой папки нет в папке скачивания — возможно, её перенесли или удалили"}
    if not path.exists():
        return {"ok": False, "msg": "Папки пока нет — в ней ещё ничего не скачано"}
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # noqa: S606 — только папка из настроек
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"Не получилось открыть папку: {e}. Путь: {path}"}
    return {"ok": True, "msg": "Открыл папку", "path": str(path)}


# ── Проверка скачанного: все ли файлы на месте и совпадают ли они с Яндексом ──

# Источник → (слот, скрипт, аргументы, вид задачи). Слот общий со скачиванием этого
# источника: пока проверка читает файлы, скачивание туда не запускается
VERIFY_TASKS = {
    "disk": (SLOT_DISK, "main.py", ["verify"], "disk_verify"),
    "unlim": (SLOT_UNLIM_COLLECT, "unlim_download.py", ["--verify"], "unlim_verify"),
}
VERIFY_BUSY = ("Сейчас идёт проверка скачанных файлов. Дождитесь её конца или остановите её "
               "в «Подробнее» — потом нажмите «Продолжить».")


class VerifyPayload(BaseModel):
    source: str = Field(default="disk", pattern="^(disk|unlim)$")


def verify_running(source: str | None = None) -> bool:
    """Идёт проверка скачанного: у источника или, без source, у любого."""
    for name, (slot, _script, _args, kind) in VERIFY_TASKS.items():
        t = tm.get(slot)
        if source in (None, name) and t is not None and t.alive() and getattr(t, "kind", None) == kind:
            return True
    return False


@router.post("/api/verify/start")
def api_verify_start(p: VerifyPayload):
    """«Проверить скачанные файлы»: на месте ли они, совпадают ли размер и MD5 с Яндексом.

    Только когда спасение не идёт: оно само пишет в эту папку, и проверка нашла бы
    «испорченными» файлы, которые как раз докачиваются.
    """
    if rescue.is_running():
        raise HTTPException(409, "Проверить файлы можно, когда скачивание остановлено или закончено: "
                                 "сейчас программа сама записывает файлы в папку")
    if verify_running(p.source):
        return {"ok": False, "msg": "Проверка уже идёт"}
    slot, script, args, _kind = VERIFY_TASKS[p.source]
    if tm.any_alive(SLOT_DISK if p.source == "disk" else UNLIM_SLOT_PREFIX):
        return {"ok": False, "msg": "Сейчас собирается список файлов — проверьте файлы, когда он соберётся"}
    if not (paths.DB_FILE if p.source == "disk" else paths.UNLIM_DB_FILE).exists():
        return {"ok": False, "msg": "Проверять пока нечего: программа ещё ничего не скачала"}
    ok, msg = tm.start(slot, script, args)
    return {"ok": ok, "msg": "Проверяю скачанные файлы" if ok else msg}


@router.post("/api/verify/stop")
def api_verify_stop(p: VerifyPayload):
    """«Остановить проверку». Итог не сохраняется — проверку можно запустить заново."""
    if not verify_running(p.source):
        return {"ok": False, "msg": "Проверка сейчас не идёт"}
    ok, _msg = tm.stop(VERIFY_TASKS[p.source][0])
    return {"ok": ok, "msg": "Проверка остановлена" if ok else "Проверка уже закончилась"}


@router.post("/api/verify/requeue")
def api_verify_requeue(p: VerifyPayload):
    """«Скачать заново»: что проверка не нашла в папке или нашла испорченным — снова в очередь.

    Качать не начинает: эти файлы скачает следующее «Продолжить» на Главной.
    """
    if rescue.is_running():
        raise HTTPException(409, "Сначала остановите скачивание — потом можно вернуть файлы в очередь")
    if verify_running(p.source):
        return {"ok": False, "msg": "Дождитесь конца проверки"}
    try:
        if p.source == "unlim":
            n = requeue_verify_problems(str(paths.UNLIM_DB_FILE))
        elif paths.DB_FILE.exists():
            with State(str(paths.DB_FILE)) as st:
                n = st.requeue_verify_problems()
        else:
            n = 0
    except sqlite3.Error as e:
        return {"ok": False, "msg": f"База занята ({e}) — попробуйте ещё раз через минуту"}
    if not n:
        return {"ok": False, "count": 0,
                "msg": "Возвращать нечего: эти файлы уже в очереди. Если сомневаетесь, проверьте файлы ещё раз"}
    log.info("проверка скачанного (%s): вернул в очередь %d файлов", p.source, n)
    return {"ok": True, "count": n,
            "msg": f"{files_text(n)} снова в очереди. Нажмите «Продолжить» на Главной — программа скачает их заново"}


# ── Список файлов: забыть (собрать заново — /api/rescue/relist) ──


def safe_remove_db_files(base_file: Path) -> tuple[bool, str]:
    """Удаляет файл SQLite и связанные wal/shm файлы с ожиданием и обработкой блокировок."""
    deadline = time.monotonic() + 3.0
    while True:
        errors = []
        # Сначала журналы: оставшийся -wal без базы SQLite применил бы к новой пустой базе
        for suffix in ("-wal", "-shm", ""):
            f = Path(str(base_file) + suffix)
            if f.exists():
                try:
                    f.unlink()
                except OSError as e:
                    errors.append((f.name, e))
        if not errors:
            return True, "БД успешно очищена"
        if time.monotonic() >= deadline:
            err_details = "; ".join(f"{name}: {err}" for name, err in errors)
            return False, f"База данных заблокирована другим процессом ({err_details}). Попробуйте позже."
        time.sleep(0.3)


@router.post("/api/disk/clear")
def disk_clear():
    """«Забыть список файлов» Диска: очистка БД + надёжная остановка задач."""
    config.set_resume_pending(False)
    if rescue.is_running():
        rescue.stop()
    stop_and_wait(tm.stop_matching(SLOT_DISK))
    return _resp(safe_remove_db_files(paths.DB_FILE))


@router.post("/api/unlim/clear")
def unlim_clear():
    """«Забыть список файлов» Безлимита: очистка БД + надёжная остановка воркеров."""
    config.set_resume_pending(False)
    if rescue.is_running():
        rescue.stop()
    stop_and_wait(tm.stop_matching(UNLIM_SLOT_PREFIX))
    return _resp(safe_remove_db_files(paths.UNLIM_DB_FILE))


# ── Сортировка ──


class OrganizePayload(BaseModel):
    input: str
    action: str = "report-only"  # report-only | move


def is_safe_organize_path(path: str) -> tuple[bool, str]:
    """Сортировать можно только существующую НЕ системную папку и не папку самого инструмента."""
    ok, msg = is_safe_dir(path, must_exist=True)
    if not ok:
        return ok, msg
    try:
        root = Path(path).resolve()
        if root == paths.PROJECT or root in paths.PROJECT.parents:
            return False, "Нельзя сортировать папку самого инструмента: уедут state.db, .env и скрипты"
    except OSError:
        return False, "Невалидный путь"
    return True, "OK"


@router.post("/api/organize")
def organize(payload: OrganizePayload):
    ok, msg = is_safe_organize_path(payload.input)
    if not ok:
        return {"ok": False, "msg": msg}
    if payload.action not in ("report-only", "move"):
        return {"ok": False, "msg": "Неизвестное действие"}
    # И во время ожидания раунда: следующий раунд скачал бы разложенные файлы заново
    if payload.action == "move" and (rescue.is_running() or tm.any_alive(SLOT_DISK)
                                     or tm.any_alive(UNLIM_SLOT_PREFIX) or tm.any_alive(SLOT_LINKS)):
        return {"ok": False, "msg": "Сначала остановите скачивание: сортировка во время загрузки перепутает файлы"}
    return _resp(tm.start(SLOT_ORGANIZE, "organize.py",
                          ["--input", payload.input,
                           "--action", payload.action]))


@router.post("/api/organize/stop")
def organize_stop():
    return _resp(tm.stop(SLOT_ORGANIZE))
