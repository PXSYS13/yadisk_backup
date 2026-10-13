"""Веб-дашборд для бэкапа Яндекс.Диска.

Запуск:
  python webui.py
  → http://localhost:8000

Что умеет:
  - Сохранить токен в .env через UI
  - Сохранить cookies.json через UI (для безлимита)
  - Запуск scan / download / verify / retry для обычного диска
  - Запуск collect / worker для photoslice (безлимита)
  - Запуск organize (сортировка по полочкам)
  - Live прогресс через polling
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from downloader import account_info, api_error_details, make_client
from unlim_common import (
    RELEASE_IN_PROGRESS_SQL,
    missing_required_cookies,
    reset_failed_files,
    unlim_tx,
)
from utils import (
    APP_VERSION,
    CATEGORY_LABELS,
    DEFAULT_UNLIM_BATCH,
    DONATION_URL,
    MAX_DISK_WORKERS,
    MAX_UNLIM_BATCH,
    MAX_UNLIM_WORKERS,
    TELEGRAM_CHANNEL,
    TELEGRAM_HANDLE,
    YOUTUBE_VIDEO_URL,
    categorize,
    classify_yandex_error,
    ensure_utf8_stdio,
    is_safe_dir,
    is_safe_download_dir,
    safe_int,
    sqlite_ro_uri,
    sqlite_tx,
)
from yandex_health import HealthMonitor

PROJECT = Path(__file__).resolve().parent
ENV_FILE = PROJECT / ".env"
load_dotenv(ENV_FILE)

DB_FILE = PROJECT / "state.db"
UNLIM_DB_FILE = PROJECT / "unlim_state.db"
COOKIES_FILE = PROJECT / "cookies.json"
TEMPLATES_DIR = PROJECT / "webui_assets"
LOG_DIR = PROJECT / "logs"
TASK_LOG_DIR = LOG_DIR / "tasks"
WEBUI_LOG = LOG_DIR / "webui.log"
HEALTH_LOG = LOG_DIR / "yandex_health.jsonl"
# Лог задачи дописывается при каждом запуске слота; при превышении — уходит в .1
TASK_LOG_MAX_BYTES = 10 * 1024 * 1024

HOST = "127.0.0.1"
PORT = 8000

log = logging.getLogger("webui")


def _setup_logging() -> None:
    """Файл-лог сервера: действия пользователя, запуски задач, ошибки — без секретов."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    handler = RotatingFileHandler(WEBUI_LOG, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(fmt)
    for name in ("webui", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)

# Ссылки для объяснений в UI: что происходит с Яндексом и где следить за новостями
NEWS_LINKS = [
    {"title": "РБК: «Яндекс» ограничил часть функций Диска (11.10.2026)",
     "url": "https://www.rbc.ru/society/11/10/2026/6acbb470b603d0e16d9a1cb0"},
    {"title": "Поддержка Яндекса: скачивание всех файлов с Диска",
     "url": "https://yandex.ru/support/yandex-360/customers/disk/web/ru/actions-files/download-all"},
    {"title": "Новости и статус серверов в Telegram", "url": TELEGRAM_CHANNEL},
]

# Коды выхода дочерних скриптов (см. main.py и unlim_worker.py)
EXIT_PARTIAL = 1       # main.py download: часть файлов упала, остальное скачано
EXIT_AUTH = 2
EXIT_OUTAGE = 3
EXIT_DISK_FULL = 4
EXIT_INTERRUPTED = 130
_EXIT_EXPLAIN = {
    EXIT_AUTH: "куки или токен устарели — обновите их в настройках",
    EXIT_OUTAGE: "Яндекс больше часа не отдаёт ZIP-архивы (отказ, лимит запросов или сбой серверов) — запустите позже",
    EXIT_DISK_FULL: "на локальном диске нет места — освободите место и запустите снова",
}

# Монитор состояния серверов Яндекса; стартует в main(), в тестах остаётся None
health_monitor: HealthMonitor | None = None

# Слоты фоновых задач — по этим именам фронтенд находит логи и статус
SLOT_DISK = "disk"
SLOT_UNLIM_COLLECT = "unlim_collect"
SLOT_ORGANIZE = "organize"
UNLIM_WORKER_SLOT_PREFIX = "unlim_w"  # unlim_w1, unlim_w2, ...
UNLIM_SLOT_PREFIX = "unlim"  # collect + все воркеры

# Маркер в колонке error: файл пропущен из-за выбора категорий (обратимо)
CATEGORY_SKIP_MARKER = "not in selected categories"


def get_config() -> dict:
    """Перечитывает .env заново."""
    load_dotenv(ENV_FILE, override=True)
    try:
        saved = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    except Exception:
        saved = {}
    return {
        "token": (os.getenv("YA_DISK_TOKEN", "") or "").strip(),
        "download_dir": (os.getenv("DOWNLOAD_DIR", str(PROJECT / "downloads")) or "").strip(),
        # Папку выбирали осознанно (есть в .env), а не взяли дефолт
        "download_dir_set": bool((saved.get("DOWNLOAD_DIR") or "").strip()),
        "max_workers": safe_int(os.getenv("MAX_WORKERS"), default=4, min_val=1, max_val=16),
    }


def mask_token(token: str) -> str:
    """Маскирует токен для отображения: y0_xxxx****xxxx"""
    if not token or len(token) < 8:
        return ""
    if len(token) <= 12:
        return token[:3] + "***"
    return f"{token[:6]}…{token[-4:]} ({len(token)} симв.)"


def _resp(t: tuple[bool, str]) -> dict:
    return {"ok": t[0], "msg": t[1]}


app = FastAPI(title="Yandex.Disk Backup UI")


# ── CSRF защита: блокируем POST с чужих сайтов ─────────────────────────────

_ALLOWED_ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
_ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}", "localhost", "127.0.0.1"}


@app.middleware("http")
async def csrf_guard(request, call_next):
    """Блокирует запросы с чужих сайтов (CSRF) и DNS-rebinding.

    Host проверяется для ВСЕХ методов: иначе страница с чужого домена, перепривязанного
    на 127.0.0.1, прочитала бы /api/status (логин, пути, имена файлов, логи).
    Origin — для state-changing методов.
    """
    host = (request.headers.get("host") or "").lower()
    if host and host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "Forbidden host"}, status_code=403)
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin") or ""
        if origin and origin not in _ALLOWED_ORIGINS:
            return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
    return await call_next(request)


# ── Менеджер фоновых процессов ──────────────────────────────────────────────


class Task:
    """Один фоновый процесс + поток, читающий его stdout в кольцевой буфер и в файл.

    Кольцевой буфер (300 строк) — для живой консоли в UI; файл logs/tasks/<slot>.log —
    чтобы после перезапуска сервера было что прислать в поддержку.
    """

    def __init__(self, name: str, proc: subprocess.Popen, log_file: Path | None = None,
                 cmd: list[str] | None = None):
        self.name = name
        self.proc = proc
        self.cmd = list(cmd or [])
        self.log_file = log_file
        self.started_at = time.time()
        self.was_stopped = False
        self.log: deque[str] = deque(maxlen=300)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _open_log_file(self):
        if self.log_file is None:
            return None
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            if self.log_file.exists() and self.log_file.stat().st_size > TASK_LOG_MAX_BYTES:
                os.replace(self.log_file, str(self.log_file) + ".1")
            f = open(self.log_file, "a", encoding="utf-8")
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} старт: {' '.join(self.cmd)} =====\n")
            return f
        except OSError:
            return None

    def _read(self):
        f = self._open_log_file()
        try:
            for line in self.proc.stdout:  # type: ignore
                line = line.rstrip("\r\n")
                if line:
                    if not re.match(r"^\d{2}:\d{2}:\d{2}\b", line):
                        ts = time.strftime("%H:%M:%S")
                        line = f"{ts}  {line}"
                    self.log.append(line)
                    if f is not None:
                        try:
                            f.write(line + "\n")
                        except OSError:
                            f = None
        except Exception:
            pass
        finally:
            # Забираем код возврата, иначе на Linux/macOS остаётся зомби-процесс
            try:
                self.proc.wait(timeout=10)
            except Exception:
                pass
            if f is not None:
                try:
                    f.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} конец: exit={self.proc.poll()}"
                            f"{' (остановлено пользователем)' if self.was_stopped else ''} =====\n")
                    f.close()
                except OSError:
                    pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self):
        self.was_stopped = True
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def join(self, timeout: float = 2.0) -> None:
        """Дожидается выхода процесса и reader-потока (вызывать после stop)."""
        try:
            self.proc.wait(timeout=timeout)
        except Exception:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=1.0)

    def close(self) -> None:
        """Закрывает pipe stdout — когда процесс уже мёртв."""
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
        except Exception:
            pass

    def snapshot(self) -> dict:
        return {
            "running": self.alive(),
            "elapsed": time.time() - self.started_at,
            "exit_code": self.proc.poll(),
            "was_stopped": self.was_stopped,
            "log_tail": list(self.log)[-80:],
            "log_file": self.log_file.name if self.log_file else None,
        }


class TaskManager:
    """Реестр задач по слотам. В одном слоте — не больше одной живой задачи."""

    def __init__(self):
        self._tasks: dict[str, Task] = {}  # ключ — название/слот
        self._lock = threading.Lock()

    def start(self, slot: str, script: str, args: list[str]) -> tuple[bool, str]:
        """slot — логическое имя ('disk', 'unlim_collect', 'unlim_w1', ...).
        Можно одновременно несколько задач в разных слотах.
        """
        with self._lock:
            existing = self._tasks.get(slot)
            if existing and existing.alive():
                return False, f"Уже идёт: {slot}"
            # Чистка зомби — если предыдущая задача в слоте умерла, закроем pipe и забудем
            if existing:
                existing.join(timeout=1.0)
                existing.close()
                del self._tasks[slot]
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            cmd = [sys.executable, "-u", script] + args
            proc = subprocess.Popen(
                cmd, cwd=str(PROJECT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env,
            )
            log.info("задача %s запущена: %s %s (pid %s)", slot, script, " ".join(args), proc.pid)
            self._tasks[slot] = Task(slot, proc, log_file=TASK_LOG_DIR / f"{slot}.log", cmd=[script, *args])
            return True, f"Запущено: {slot}"

    def get(self, slot: str) -> Task | None:
        with self._lock:
            return self._tasks.get(slot)

    def stop(self, slot: str) -> tuple[bool, str]:
        with self._lock:
            t = self._tasks.get(slot)
            if not t or not t.alive():
                return False, f"Нет активной задачи {slot}"
            t.stop()
            log.info("задача %s остановлена пользователем", slot)
            return True, f"Остановлено: {slot}"

    def stop_matching(self, prefix: str) -> list[Task]:
        """Останавливает все живые задачи, чей слот начинается с prefix. Возвращает их."""
        stopped: list[Task] = []
        with self._lock:
            for slot, t in self._tasks.items():
                if slot.startswith(prefix) and t.alive():
                    t.stop()
                    stopped.append(t)
                    log.info("задача %s остановлена (по префиксу %r)", slot, prefix)
        return stopped

    def stop_all(self) -> int:
        return len(self.stop_matching(""))

    def any_alive(self, prefix: str) -> bool:
        with self._lock:
            return any(slot.startswith(prefix) and t.alive() for slot, t in self._tasks.items())

    def status(self) -> dict:
        with self._lock:
            return {slot: t.snapshot() for slot, t in self._tasks.items()}

    def recent_log_lines(self, per_task: int = 40) -> list[str]:
        with self._lock:
            lines: list[str] = []
            for t in self._tasks.values():
                lines.extend(list(t.log)[-per_task:])
            return lines

    def clear_logs(self, pattern: str) -> int:
        """Очищает буфер логов слота; 'prefix*' — всех слотов с таким префиксом."""
        with self._lock:
            cleared = 0
            for slot, t in self._tasks.items():
                if slot == pattern or (pattern.endswith("*") and slot.startswith(pattern[:-1])):
                    t.log.clear()
                    cleared += 1
            return cleared


tm = TaskManager()


def _launch_unlim_workers(
    n: int,
    batch: int,
    starter: Callable[[str, str, list[str]], tuple[bool, str]] | None = None,
) -> dict:
    """Запускает n воркеров безлимита в слотах unlim_w1..unlim_wN.

    starter — чем запускать (по умолчанию tm.start; pipeline подставляет свой,
    чтобы помечать задачи как свои).
    """
    starter = starter or tm.start
    if not tm.any_alive(UNLIM_WORKER_SLOT_PREFIX):
        _release_orphaned_unlim()
    n = max(1, min(n, MAX_UNLIM_WORKERS))
    batch = max(1, min(batch, MAX_UNLIM_BATCH))
    started: list[int] = []
    failed: list[dict] = []
    for i in range(1, n + 1):
        ok, msg = starter(
            f"{UNLIM_WORKER_SLOT_PREFIX}{i}", "unlim_worker.py",
            ["--id", str(i), "--batch", str(batch)],
        )
        if ok:
            started.append(i)
        else:
            failed.append({"id": i, "msg": msg})
    return {"ok": True, "started": started, "failed": failed}


def plan_sources(do_disk: bool, do_unlim: bool, token_ok: bool, cookies_ok: bool) -> dict:
    """Что реально можно скачать при текущих токене и cookies.

    Нет токена — обычный диск пропускаем, если есть что качать в безлимите; нет
    cookies — наоборот. Ошибка только когда скачать нечего совсем: тогда пользователь
    получает подсказку, что настроить, а не падение на первом шаге.
    """
    run_disk = do_disk and token_ok
    run_unlim = do_unlim and cookies_ok
    notes: list[str] = []
    if do_disk and not token_ok and run_unlim:
        notes.append("⚠️ Токен не задан или не работает: обычный диск пропускаю, качаю только безлимит. "
                     "Добавить токен: на Главной кнопка «Получить и ввести токен».")
    if do_unlim and not cookies_ok and run_disk:
        notes.append("⚠️ Cookies не загружены: безлимит пропускаю, качаю только обычный диск. "
                     "Добавить cookies: на Главной кнопка «Добавить cookies».")
    error = None
    if not run_disk and not run_unlim:
        if do_disk and do_unlim:
            error = ("Нечего скачивать: нет ни токена, ни cookies. "
                     "Настройте хотя бы одно на Главной в блоке «Путь к бэкапу».")
        elif do_disk:
            error = "Для обычного диска нужен токен: на Главной кнопка «Получить и ввести токен»."
        elif do_unlim:
            error = "Для безлимита нужны cookies: на Главной кнопка «Добавить cookies»."
        else:
            error = "Выберите хотя бы одно: обычный диск или безлимит."
    return {"run_disk": run_disk, "run_unlim": run_unlim, "notes": notes, "error": error}


def preflight_sources(do_disk: bool, do_unlim: bool) -> dict:
    """plan_sources по текущим настройкам — до запуска, чтобы ответить пользователю сразу."""
    token_ok = bool(check_token(get_config()["token"]).get("ok")) if do_disk else False
    cookies_ok = bool(check_cookies().get("ok")) if do_unlim else False
    return plan_sources(do_disk, do_unlim, token_ok, cookies_ok)


def _release_orphaned_unlim() -> int:
    """Живых воркеров нет — значит, все in_progress осиротели: воркер с другим id
    (например, после запуска с 4 воркерами, а потом с 2) их сам не сбросит никогда.
    """
    if not UNLIM_DB_FILE.exists():
        return 0
    try:
        with unlim_tx(UNLIM_DB_FILE) as c:
            return c.execute(RELEASE_IN_PROGRESS_SQL).rowcount
    except Exception:
        return 0


def _stop_and_wait(tasks: list[Task]) -> None:
    for t in tasks:
        t.join(timeout=2.0)


def _free_space(path: str) -> dict | None:
    """Свободное место на диске папки скачивания (или ближайшего существующего предка)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        usage = shutil.disk_usage(p)
    except OSError:
        return None
    return {"free": usage.free, "total": usage.total, "path": str(p)}


# ── Pipeline ────────────────────────────────────────────────────────────────


class Pipeline:
    """Автоматическая последовательность: scan → download диска → collect → workers безлимита.
    Сам мониторит окончание каждого шага и переходит к следующему.
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop = False
        self._owned_slots: set[str] = set()  # слоты которые pipeline сам запустил
        self.state: str = "idle"  # idle | running | done | failed | stopped
        self.step: str = "—"
        self.log: deque[str] = deque(maxlen=120)
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.skip_disk: bool = False
        self.skip_unlim: bool = False
        self.workers: int = 4
        self.batch: int = DEFAULT_UNLIM_BATCH
        # None — качать всё; список категорий — отфильтровать после scan/collect
        self.disk_categories: list[str] | None = None
        self.unlim_categories: list[str] | None = None
        # Часть запрошенных источников пропущена (нет токена или cookies)
        self.partial: bool = False

    def _own_start(self, slot: str, script: str, args: list[str]) -> tuple[bool, str]:
        """Запускает задачу через tm и помечает её как принадлежащую pipeline'у."""
        ok, msg = tm.start(slot, script, args)
        if ok:
            self._owned_slots.add(slot)
        return ok, msg

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"{ts}  {msg}")
        log.info("pipeline: %s", msg)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, workers: int = 4, do_disk: bool = True, do_unlim: bool = True,
              batch: int = DEFAULT_UNLIM_BATCH,
              disk_categories: list[str] | None = None,
              unlim_categories: list[str] | None = None) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                return False, "Pipeline уже идёт"
            self._stop = False
            self._owned_slots.clear()
            self.state = "running"
            self.step = "старт"
            self.log.clear()
            self.started_at = time.time()
            self.finished_at = None
            self.workers = max(1, min(workers, MAX_UNLIM_WORKERS))
            self.batch = max(1, min(batch, MAX_UNLIM_BATCH))
            self.skip_disk = not do_disk
            self.skip_unlim = not do_unlim
            self.disk_categories = list(disk_categories) if disk_categories is not None else None
            self.unlim_categories = list(unlim_categories) if unlim_categories is not None else None
            self.partial = False
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, "Pipeline запущен"

    def stop(self) -> tuple[bool, str]:
        if not self.is_running():
            return False, "Pipeline не идёт"
        self._stop = True
        # Гасим ТОЛЬКО задачи которые pipeline сам запустил
        for slot in list(self._owned_slots):
            tm.stop(slot)
        self._log("⏹ остановлено пользователем")
        return True, "Остановлено"

    def _wait_task(self, slot: str, on_finish_msg: str) -> int:
        """Блокирующее ожидание завершения задачи в слоте."""
        while not self._stop:
            t = tm.get(slot)
            if not t:
                time.sleep(0.5)
                continue
            if not t.alive():
                code = t.proc.poll()
                self._log(f"{on_finish_msg} (exit={code})")
                return code if code is not None else 0
            time.sleep(1.0)
        return -1

    def _run_step(self, slot: str, script: str, args: list[str],
                  start_msg: str, finish_msg: str, fail_msg: str,
                  tolerate_partial: bool = False) -> bool:
        """Запускает задачу и ждёт её. False — шаг провалился или pipeline остановлен.

        tolerate_partial — exit=1 («часть файлов упала») не останавливает pipeline:
        один удалённый с Диска файл не должен отменять скачивание безлимита.
        """
        self._log(start_msg)
        ok, msg = self._own_start(slot, script, args)
        if not ok:
            self._log(f"❌ {fail_msg}: {msg}")
            self.state = "failed"
            return False
        code = self._wait_task(slot, finish_msg)
        if self._stop:
            self._abort()
            return False
        task = tm.get(slot)
        if (task is not None and task.was_stopped) or code == EXIT_INTERRUPTED:
            self._log(f"⏹ {fail_msg}: остановлено пользователем")
            self._abort()
            return False
        if code == 0:
            return True
        if code == EXIT_PARTIAL and tolerate_partial:
            self._log(f"⚠️ {fail_msg}: часть файлов упала — продолжаю. "
                      f"Упавшие можно повторить кнопкой «Повторить ошибки».")
            return True
        why = _EXIT_EXPLAIN.get(code)
        self._log(f"❌ {fail_msg} завершился с ошибкой (exit={code}){': ' + why if why else ''}")
        self.state = "failed"
        return False

    def _apply_categories(self, db_file: Path, table: str, id_col: str,
                          categories: list[str], label: str) -> None:
        unskip_category_marker(db_file, table)
        skipped, kept = filter_to_categories(db_file, table, id_col, categories)
        names = ", ".join(CATEGORY_LABELS.get(c, c) for c in categories) or "ничего"
        self._log(f"🎯 {label}: качаем {names} — в очереди {kept}, пропущено {skipped}")

    def _workers_outcome(self, ids: list[int]) -> str:
        """done | stopped | failed — по кодам выхода воркеров, а не по факту их завершения."""
        codes: list[int | None] = []
        for i in ids:
            t = tm.get(f"{UNLIM_WORKER_SLOT_PREFIX}{i}")
            if t is None:
                continue
            if t.was_stopped:
                return "stopped"
            codes.append(t.proc.poll())
        bad = [c for c in codes if c not in (0, None)]
        if not bad:
            return "done"
        for c in bad:
            if c in _EXIT_EXPLAIN:
                self._log(f"❌ воркеры безлимита остановились: {_EXIT_EXPLAIN[c]}")
                return "failed"
        if len(bad) == len(codes):
            self._log(f"❌ все воркеры безлимита завершились с ошибкой (exit={bad[0]})")
            return "failed"
        self._log(f"⚠️ часть воркеров завершилась с ошибкой (exit={sorted(set(bad))}), остальные доработали")
        return "done"

    def _run(self):
        try:
            cfg = get_config()

            # === Что реально можно скачать: без токена — только безлимит, без cookies — только диск ===
            self.step = "проверка доступа"
            tok: dict = {"ok": False}
            if not self.skip_disk:
                self._log("🔑 Проверяю токен...")
                tok = check_token(cfg["token"])
                if tok.get("ok"):
                    self._log(f"✓ Токен OK: {tok.get('login')}, {tok.get('used', 0) / 1e9:.1f} ГБ занято")
                else:
                    self._log(f"✗ Токен: {tok.get('msg')}")
            cookies: dict = {"ok": False}
            if not self.skip_unlim:
                cookies = check_cookies()
                self._log(f"{'✓' if cookies.get('ok') else '✗'} Cookies: {cookies.get('msg')}")
            src = plan_sources(not self.skip_disk, not self.skip_unlim,
                               bool(tok.get("ok")), bool(cookies.get("ok")))
            for note in src["notes"]:
                self._log(note)
            if src["error"]:
                self._log(f"❌ {src['error']}")
                self.state = "failed"
                return
            self.partial = bool(src["notes"])

            # === ДИСК ===
            if src["run_disk"]:
                if self._stop:
                    self._abort()
                    return
                self.step = "сканирую диск"
                if not self._run_step(SLOT_DISK, "main.py", ["scan"],
                                      "🔍 Запускаю scan...", "✓ scan завершён", "scan диска"):
                    return
                if self.disk_categories is not None:
                    self._apply_categories(DB_FILE, "files", "remote_path",
                                           self.disk_categories, "Диск")

                self.step = "качаю диск"
                if not self._run_step(
                    SLOT_DISK, "main.py",
                    ["download", "--workers", str(min(self.workers, MAX_DISK_WORKERS)), "--no-scan"],
                    f"⬇️ Запускаю download (workers={min(self.workers, MAX_DISK_WORKERS)})...",
                    "✓ download диска завершён", "download диска",
                    tolerate_partial=True,
                ):
                    return
            elif self.skip_disk:
                self._log("⏭ Шаг 'диск' пропущен")

            # === БЕЗЛИМИТ ===
            if src["run_unlim"]:
                if self._stop:
                    self._abort()
                    return
                self.step = "собираю список безлимита"
                if not self._run_step(SLOT_UNLIM_COLLECT, "unlim_download.py", ["--collect"],
                                      "🗂 Запускаю unlim collect...", "✓ список безлимита собран",
                                      "сбор списка безлимита"):
                    return
                if self.unlim_categories is not None:
                    self._apply_categories(UNLIM_DB_FILE, "unlim_files", "file_id",
                                           self.unlim_categories, "Безлимит")

                # workers
                self.step = f"скачиваю безлимит ({self.workers} воркеров, batch={self.batch})"
                self._log(f"🚀 Запускаю {self.workers} воркеров (пачка={self.batch})...")
                res = _launch_unlim_workers(self.workers, self.batch, starter=self._own_start)
                for i in res["started"]:
                    self._log(f"  ✓ воркер {i} запущен")
                for f in res["failed"]:
                    self._log(f"  ✗ воркер {f['id']}: {f['msg']}")

                # Ждём пока все воркеры закончат
                while not self._stop and tm.any_alive(UNLIM_WORKER_SLOT_PREFIX):
                    time.sleep(1.0)
                if self._stop:
                    self._abort()
                    return
                outcome = self._workers_outcome(res["started"])
                if outcome == "stopped":
                    self._log("⏹ воркеры безлимита остановлены пользователем")
                    self._abort()
                    return
                if outcome == "failed":
                    self.state = "failed"
                    return
                self._log("✓ все воркеры безлимита завершили работу")
            elif self.skip_unlim:
                self._log("⏭ Шаг 'безлимит' пропущен")

            if self._stop:
                self._abort()
                return

            self.state = "done"
            self.step = "финиш"
            self._log("🏁 Готово, но часть источников пропущена (см. выше)" if self.partial else "🏆 ВСЁ ГОТОВО!")
        except Exception as e:
            diag = classify_yandex_error(e)
            if diag["is_yandex_server"]:
                self._log(f"⚠️ Сбой серверов Яндекса: {diag['title']}")
                self._log(f"   {diag['message']}")
                self._log(f"   Новости дата-центров Яндекса: {TELEGRAM_CHANNEL}")
            else:
                self._log(f"❌ Ошибка pipeline: {e}")
            self.state = "failed"
        finally:
            self.finished_at = time.time()

    def _abort(self):
        self.state = "stopped"
        self.step = "остановлено"

    def status(self) -> dict:
        if not self.started_at:
            elapsed = None
        elif self.finished_at:
            elapsed = self.finished_at - self.started_at
        else:
            elapsed = time.time() - self.started_at

        return {
            "state": self.state,
            "running": self.is_running(),
            "step": self.step,
            "started_at": self.started_at,
            "elapsed": elapsed,
            "partial": self.partial,
            "log": list(self.log),
        }


pipeline = Pipeline()


# ── Чтение состояния ────────────────────────────────────────────────────────

# Белый список для SQL-идентификаторов — защита от инъекций через f-string
_ALLOWED_TABLES = {"files", "unlim_files"}
_ALLOWED_COLUMNS = {"remote_path", "file_id"}


def _validate_sql_ident(value: str, allowed: set[str], label: str) -> str:
    """Проверяет что SQL-идентификатор из белого списка."""
    if value not in allowed:
        raise ValueError(f"Недопустимый {label}: {value!r}")
    return value


def _empty_db_stats() -> dict:
    return {
        "total": 0, "pending": 0, "downloaded": 0, "skipped": 0,
        "failed": 0, "in_progress": 0,
        "category_skipped": 0, "category_skipped_bytes": 0,
        "bytes_total": 0, "bytes_done": 0,
        "bytes_done_real": 0, "bytes_pending": 0,
        "bytes_in_progress": 0, "bytes_failed": 0,
        "bytes_selected_total": 0,
        "done_10m": 0, "bytes_10m": 0,
    }


def db_stats(db_file: Path, table: str = "files",
             id_col: str = "remote_path") -> dict:
    table = _validate_sql_ident(table, _ALLOWED_TABLES, "table")
    id_col = _validate_sql_ident(id_col, _ALLOWED_COLUMNS, "column")
    out = _empty_db_stats()
    if not db_file.exists():
        out["recent"] = []
        return out
    c = None
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        for r in c.execute(
            f"SELECT status, error, COUNT(*) AS n, COALESCE(SUM(size),0) AS b "
            f"FROM {table} GROUP BY status, (COALESCE(error,'') = ?)",
            (CATEGORY_SKIP_MARKER,),
        ):
            st = r["status"]
            err = r["error"] or ""
            n = r["n"]
            b = r["b"]
            out[st] = out.get(st, 0) + n
            out["total"] += n
            out["bytes_total"] += b
            if st == "pending":
                out["bytes_pending"] += b
            elif st == "in_progress":
                out["bytes_in_progress"] += b
            elif st == "failed":
                out["bytes_failed"] += b
            elif st == "downloaded":
                out["bytes_done_real"] += b
                out["bytes_done"] += b
            elif st == "skipped":
                if err == CATEGORY_SKIP_MARKER:
                    out["category_skipped"] += n
                    out["category_skipped_bytes"] += b
                else:
                    out["bytes_done"] += b

        out["bytes_selected_total"] = max(0, out["bytes_total"] - out["category_skipped_bytes"])
        # Скорость за последние 10 минут — для оценки времени в мастере плана.
        # files пишет downloaded_at как 'YYYY-MM-DDTHH:MM:SS', unlim — 'YYYY-MM-DD HH:MM:SS' (оба UTC)
        rate = c.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM {table} "
            f"WHERE status='downloaded' AND downloaded_at IS NOT NULL "
            f"AND replace(downloaded_at, 'T', ' ') >= datetime('now', '-10 minutes')"
        ).fetchone()
        out["done_10m"] = rate["n"]
        out["bytes_10m"] = rate["b"]
        recent = list(c.execute(
            f"SELECT {id_col} AS path, status, size FROM {table} "
            f"WHERE downloaded_at IS NOT NULL "
            f"ORDER BY downloaded_at DESC LIMIT 10"
        ))
        out["recent"] = [{"path": r["path"], "status": r["status"],
                          "size": r["size"]} for r in recent]
        return out
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        if c is not None:
            c.close()


# Кэш для тяжёлых операций (polling /api/status каждые 1.5 сек)
_local_size_cache: dict[str, dict] = {}
# 60 сек, а не 10: обход папки — это диск, а по нему в это же время идёт
# скачивание. /api/status опрашивается каждые 1.5 сек, так что дёшево тут важно.
_LOCAL_SIZE_TTL = 60  # секунд
_LOCAL_SIZE_MAX_PATHS = 4
_LOCAL_SIZE_WALK_BUDGET = 2.0  # макс секунд на обход


def local_size(path: str) -> dict:
    r"""Размер папки с TTL-кэшем и лимитом времени обхода.
    Защита от корня диска (C:\) — не обходим, иначе зависнет на десятках ГБ.
    """
    if not os.path.isdir(path):
        return {"files": 0, "bytes": 0}

    abs_path = os.path.abspath(path)
    if len(abs_path) <= 3 and abs_path.endswith(":\\"):
        return {"files": 0, "bytes": 0, "skipped": "корень диска"}

    cached = _local_size_cache.get(path)
    now = time.time()
    if cached and (now - cached["at"] < _LOCAL_SIZE_TTL):
        return cached["data"]

    n = 0
    b = 0
    partial = False
    deadline = now + _LOCAL_SIZE_WALK_BUDGET
    try:
        for dp, _, fns in os.walk(path):
            if time.time() > deadline:
                partial = True  # не досчитали — честно скажем UI
                break
            for fn in fns:
                try:
                    b += os.path.getsize(os.path.join(dp, fn))
                    n += 1
                except OSError:
                    pass
    except OSError:
        pass

    data = {"files": n, "bytes": b, "partial": partial}
    # Ограничиваем размер кэша — выкидываем самый старый путь
    if len(_local_size_cache) >= _LOCAL_SIZE_MAX_PATHS and path not in _local_size_cache:
        oldest = min(_local_size_cache.items(), key=lambda x: x[1]["at"])[0]
        _local_size_cache.pop(oldest, None)
    _local_size_cache[path] = {"at": now, "data": data}
    return data


# Кэш для check_token: хранит только ОДИН токен (последний проверенный).
# Так не накапливаются старые токены в памяти.
_token_check_cache: dict = {"token": None, "at": 0, "data": None}
_TOKEN_TTL = 30  # секунд


def check_token(token: str) -> dict:
    if not token or token.startswith("AQAAA_paste"):
        return {"ok": False, "msg": "Токен не задан"}
    # Кэш — не дёргать Яндекс каждые 1.5 сек!
    now = time.time()
    if (_token_check_cache["token"] == token
            and now - _token_check_cache["at"] < _TOKEN_TTL):
        return _token_check_cache["data"]
    try:
        cli = make_client(token)
        try:
            if not cli.check_token():
                data = {"ok": False, "msg": "Токен невалиден: Яндекс ответил, что он не действует. "
                                           "Скопируйте токен заново целиком."}
                _token_check_cache.update({"token": token, "at": now, "data": data})
                return data
            acc = account_info(cli)
        finally:
            cli.close()
        data = {"ok": True, "msg": "OK", "used": acc["used"], "total": acc["total"],
                "photo_unlim": acc["photo_unlim"], "login": acc["login"], "account": acc}
        log.info("токен проверен: %s, занято %s из %s, безлимит %s", acc["login"],
                 acc["used"], acc["total"], acc["photo_unlim"])
    except Exception as e:
        data = {"ok": False, "msg": f"{type(e).__name__}: {e}{api_error_details(e)}"}
        log.warning("проверка токена не удалась: %s", data["msg"])
    _token_check_cache.update({"token": token, "at": now, "data": data})
    return data


def onboarding_state(cfg: dict, token: dict, cookies: dict, disk_db: dict, unlim_db: dict) -> dict:
    """Воронка первого запуска: что уже настроено, какой шаг следующий."""
    steps = {
        "token": bool(token.get("ok")),
        "dir": bool(cfg.get("download_dir_set")),
        "cookies": bool(cookies.get("ok")),
        "scanned": bool(disk_db.get("total") or unlim_db.get("total")),
        "downloaded": bool(disk_db.get("downloaded") or unlim_db.get("downloaded")),
    }
    if not steps["token"] and not steps["cookies"]:
        next_step = "token"
    elif not steps["dir"]:
        next_step = "dir"
    elif not steps["cookies"]:
        next_step = "cookies"   # опционально — UI даёт «пропустить»
    elif not steps["downloaded"]:
        next_step = "download"
    else:
        next_step = "done"
    return {**steps, "next": next_step}


def _cookies_report(arr: list) -> dict:
    """Проверяет массив кук (формат Cookie-Editor) на наличие обязательных."""
    if not isinstance(arr, list):
        return {"ok": False, "msg": "Должен быть массив", "count": 0}
    missing = missing_required_cookies(arr)
    if missing:
        return {"ok": False,
                "msg": f"Не хватает кук: {', '.join(missing)}. "
                       f"Залогинься в браузере на disk.yandex.ru и заэкспортируй заново.",
                "count": len(arr)}
    return {"ok": True, "msg": f"Куки OK ({len(arr)} шт)", "count": len(arr)}


def check_cookies() -> dict:
    if not COOKIES_FILE.exists():
        return {"ok": False, "msg": "cookies.json не загружен", "count": 0}
    try:
        raw = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "msg": f"Ошибка: {e}", "count": 0}
    if not isinstance(raw, list):
        return {"ok": False, "msg": "Формат не Cookie-Editor", "count": 0}
    return _cookies_report(raw)


def _affects(disk: tuple[str, str], unlim: tuple[str, str]) -> list[dict]:
    return [
        {"what": "Обычный диск (REST API)", "state": disk[0], "text": disk[1]},
        {"what": "Безлимит (ZIP-архивы)", "state": unlim[0], "text": unlim[1]},
    ]


# Правила диагностики по маркерам в логах задач. Порядок = приоритет.
# markers — подстроки (в нижнем регистре); первая сработавшая категория определяет
# объяснение, счётчики считаются по всем.
_DIAG_RULES: list[dict] = [
    {
        "kind": "auth_expired",
        "markers": ("auth_expired", "401 unauthorized", "403 forbidden"),
        "title": "Яндекс отклонил авторизацию (HTTP 401/403)",
        "message": "Авторизация отклонена Яндексом (HTTP 401/403). Токен или cookies устарели.",
        "explain": "Токен Диска или cookies.json протухли. Это не сбой Яндекса: нужно заново "
                   "получить токен или заэкспортировать куки из браузера, где вы залогинены.",
        "recommendation": "Обновите токен Яндекс.Диска или cookies.json в настройках Web UI.",
        "affects": _affects(("fail", "нужен новый токен"), ("fail", "нужны свежие куки")),
    },
    {
        "kind": "local_disk",
        "markers": ("local_disk",),
        "title": "Проблема с вашим диском, а не с Яндексом",
        "message": "На локальном диске нет места, нет прав на запись или папка скачивания недоступна.",
        "explain": "Скрипт не может записать файлы: закончилось место, папка на отключённом диске "
                   "или нет прав. Повторные попытки бесполезны, пока это не исправлено.",
        "recommendation": "Освободите место или укажите другую папку скачивания в настройках, затем повторите.",
        "affects": _affects(("fail", "ждёт места на диске"), ("fail", "ждёт места на диске")),
    },
    {
        "kind": "bulk_disabled",
        "markers": ("bulk_unavailable", "not_zip |", "refused |"),
        "title": "Яндекс не отдаёт ZIP-архивы — массовое скачивание ограничено",
        "message": "Воркер безлимита раз за разом не получает архив от Яндекса.",
        "explain": "С 11 октября 2026 после атак на дата-центры Яндекс отключил массовое скачивание "
                   "в веб-версии Диска (папки, наборы файлов, альбомы). Безлимитное фото-хранилище "
                   "доступно только через такие архивы, поэтому его скачивание сейчас не работает. "
                   "Одиночные файлы обычного диска через официальный API Яндекс отдаёт.",
        "recommendation": "Качайте обычный диск сейчас. Безлимит оставьте: воркер ждёт растущими "
                          "паузами, попытки файлов не сжигает и сам продолжит, когда Яндекс вернёт функцию "
                          "(через час без успеха он выйдет — запустите снова позже).",
        "affects": _affects(("ok", "работает"), ("wait", "ждёт разрешения Яндекса")),
    },
    {
        "kind": "rate_limit",
        "markers": ("rate_limit", "429 toomanyrequests", "429 too many requests"),
        "title": "Яндекс ограничивает частоту запросов (HTTP 429)",
        "message": "Яндекс ограничивает частоту запросов (HTTP 429 Too Many Requests).",
        "explain": "Серверы Яндекса просят сбавить темп. Это не отключение функции: при получении 429 "
                   "все воркеры делают общую паузу (от 45 секунд до 10 минут) и продолжают сами.",
        "recommendation": "Если 429 повторяются, остановите скачивание и запустите снова с 1–2 воркерами "
                          "и пачкой 10.",
        "affects": _affects(("wait", "работает с паузами"), ("wait", "работает с паузами")),
    },
    {
        "kind": "server_50x",
        "markers": ("server_50x", "504 gateway timeout", "сбой сервера яндекса"),
        "title": "Сбой или перегрузка серверов Яндекса (HTTP 50x)",
        "message": "Сбой или перегрузка серверов архивации Яндекса (50x/504).",
        "explain": "Узлы Яндекса отвечают ошибками 500–504: перегрузка или повреждённая инфраструктура "
                   "дата-центров. Скрипт повторяет запросы с паузами, данные не теряются.",
        "recommendation": "Сервер Яндекса перегружен. Снизьте размер пачки до 10–15 и число воркеров до 2.",
        "affects": _affects(("wait", "работает с повторами"), ("wait", "работает с повторами")),
    },
    {
        "kind": "timeout",
        "markers": ("download_timeout", "timeout", "таймаут"),
        "title": "Яндекс отвечает слишком медленно (таймауты)",
        "message": "Превышено время ожидания ответа от Яндекса (сетевой таймаут).",
        "explain": "Ответы приходят дольше лимита ожидания — перегрузка на стороне Яндекса "
                   "или нестабильное соединение.",
        "recommendation": "Проверьте стабильность интернет-соединения или уменьшите нагрузку.",
        "affects": _affects(("wait", "работает с повторами"), ("wait", "работает с повторами")),
    },
    {
        "kind": "prepare_failed",
        "markers": ("prepare_failed",),
        "title": "Яндекс не смог подготовить архив файлов",
        "message": "Яндекс не смог подготовить архив файлов.",
        "explain": "Сервис архивации отвечает ошибкой на запрос сборки ZIP. Пачки автоматически "
                   "делятся и повторяются.",
        "recommendation": "Пачки автоматически делятся и повторяются. Рекомендуется снизить число воркеров.",
        "affects": _affects(("ok", "работает"), ("wait", "повторяет пачки")),
    },
]
_DIAG_FLAG_KINDS = ("rate_limit", "server_50x", "auth_expired", "timeout", "prepare_failed")


def diagnose_tasks() -> dict:
    """Анализирует логи активных и недавних задач: что не так, почему и что делать.

    Возвращает и старые поля (*_detected, message, recommendation), и новые:
    kind, title, explain, affects, counts, links.
    """
    lines = [ln.lower() for ln in tm.recent_log_lines(40)]
    counts = {
        rule["kind"]: sum(1 for ln in lines if any(m in ln for m in rule["markers"]))
        for rule in _DIAG_RULES
    }
    diag: dict = {f"{k}_detected": False for k in _DIAG_FLAG_KINDS}
    diag.update({
        "kind": None, "title": None, "message": None, "explain": None,
        "recommendation": None, "affects": [], "counts": counts, "links": NEWS_LINKS,
    })
    for rule in _DIAG_RULES:
        if counts[rule["kind"]]:
            diag["kind"] = rule["kind"]
            for key in ("title", "message", "explain", "recommendation", "affects"):
                diag[key] = rule[key]
            if rule["kind"] in _DIAG_FLAG_KINDS:
                diag[f"{rule['kind']}_detected"] = True
            break
    return diag


# ── Endpoints: статус и конфиг ──────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = TEMPLATES_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>UI не установлен</h1>")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


GUIDE_FILE = PROJECT / "INSTRUCTION_GET_TOKEN.md"
_guide_cache: dict = {"mtime": None, "html": None}
_ALERT_TITLES = {"NOTE": "Примечание", "TIP": "Совет", "IMPORTANT": "Важно",
                 "WARNING": "Внимание", "CAUTION": "Осторожно"}

_GUIDE_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Инструкция · Yandex.Disk Backup</title>
<style>
  :root{--bg:#000;--surface:#1c1c1e;--surface2:#2c2c2e;--text:#f5f5f7;--muted:#aeaeb2;
        --line:rgba(255,255,255,.1);--link:#6cb8ff;--green:#30d158;--yellow:#ffd60a;--red:#ff453a}
  body{background:var(--bg);color:var(--text);margin:0;padding:24px 16px 64px;
       font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Inter,system-ui,sans-serif}
  main{max-width:860px;margin:0 auto}
  a{color:var(--link)} a:hover{text-decoration:underline}
  .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;gap:12px;flex-wrap:wrap}
  .top a{text-decoration:none;padding:6px 12px;border:1px solid var(--line);border-radius:10px}
  h1{font-size:26px;line-height:1.25} h2{font-size:21px;margin-top:40px;padding-top:16px;border-top:1px solid var(--line)}
  h3{font-size:17px;margin-top:24px}
  code{background:var(--surface2);padding:2px 6px;border-radius:6px;font-size:13.5px}
  pre{background:var(--surface);padding:12px 14px;border-radius:10px;overflow:auto} pre code{background:none;padding:0}
  kbd{background:var(--surface2);border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:1px 6px;font-size:13px}
  table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px;display:block;overflow-x:auto}
  th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top} th{background:var(--surface)}
  blockquote{margin:16px 0;padding:10px 16px;border-left:3px solid var(--link);background:var(--surface);border-radius:0 10px 10px 0}
  blockquote.tip{border-color:var(--green)} blockquote.warning,blockquote.caution{border-color:var(--red)}
  blockquote.important{border-color:var(--yellow)}
  img{max-width:100%;vertical-align:middle} hr{border:0;border-top:1px solid var(--line);margin:28px 0}
</style></head><body><main>
<div class="top"><a href="/">← Вернуться в программу</a>
<a href="https://github.com/PXSYS13/yadisk_backup/blob/main/INSTRUCTION_GET_TOKEN.md" target="_blank" rel="noopener">Открыть на GitHub</a></div>
{{body}}
</main></body></html>"""


_GUIDE_SCHEME = (
    "**Схема:** ваш аккаунт Яндекса → 🔑 токен (Полигон) и 🍪 cookies (Cookie-Editor) → "
    "программа на `http://localhost:8000` → «Скачать всё» → 📁 папка на вашем компьютере.\n\n"
)


def _github_slug(value: str, separator: str = "-") -> str:
    """Якоря как у GitHub — оглавление инструкции работает и внутри программы."""
    value = value.strip().lower()
    value = re.sub(r"[^\w\- ]", "", value)
    return value.replace(" ", separator)


def render_guide() -> str:
    """INSTRUCTION_GET_TOKEN.md → HTML в стиле программы.

    Файл в ZIP — это сырой Markdown, а имя «*.md» Telegram и браузеры принимают
    за адрес сайта (.md — доменная зона). Поэтому инструкция открывается здесь.
    """
    import markdown

    mtime = GUIDE_FILE.stat().st_mtime
    if _guide_cache["mtime"] == mtime:
        return _guide_cache["html"]
    text = GUIDE_FILE.read_text(encoding="utf-8")
    # Диаграмма mermaid без GitHub выглядит как код — показываем ту же схему текстом
    text = re.sub(r"```mermaid.*?```\s*", _GUIDE_SCHEME, text, flags=re.S)
    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"slugify": _github_slug}},
    )
    body = re.sub(
        r"<blockquote>\s*<p>\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*",
        lambda m: f'<blockquote class="{m.group(1).lower()}"><p><strong>{_ALERT_TITLES[m.group(1)]}.</strong> ',
        body,
    )
    html = _GUIDE_PAGE.replace("{{body}}", body)
    _guide_cache.update({"mtime": mtime, "html": html})
    return html


@app.get("/guide", response_class=HTMLResponse)
def guide():
    if not GUIDE_FILE.exists():
        return RedirectResponse(
            url="https://github.com/PXSYS13/yadisk_backup/blob/main/INSTRUCTION_GET_TOKEN.md", status_code=307)
    return HTMLResponse(render_guide())


@app.get("/donate")
def donate():
    """Перенаправляет на страницу пожертвования / вкладку доната."""
    return RedirectResponse(url="/?tab=donate", status_code=307)


@app.get("/api/status")
def api_status():
    cfg = get_config()
    safe_config = {
        "download_dir": cfg.get("download_dir", ""),
        "download_dir_set": cfg.get("download_dir_set", False),
        "max_workers": cfg.get("max_workers", 4),
        "token_masked": mask_token(cfg.get("token", "")),
    }
    token = check_token(cfg["token"])
    cookies = check_cookies()
    disk_db = db_stats(DB_FILE, "files", "remote_path")
    unlim_db = db_stats(UNLIM_DB_FILE, "unlim_files", "file_id")
    return {
        "app_version": APP_VERSION,
        "donation_url": DONATION_URL,
        "telegram_url": TELEGRAM_CHANNEL,
        "telegram_handle": TELEGRAM_HANDLE,
        "youtube_url": YOUTUBE_VIDEO_URL,
        "config": safe_config,
        "token": token,
        "cookies": cookies,
        "onboarding": onboarding_state(cfg, token, cookies, disk_db, unlim_db),
        "disk": {
            "db": disk_db,
            "local": local_size(cfg["download_dir"]),
        },
        "unlim": {
            "db": unlim_db,
            "local": local_size(os.path.join(cfg["download_dir"], "_unlim")),
        },
        "tasks": tm.status(),
        "pipeline": pipeline.status(),
        "diagnostics": diagnose_tasks(),
        "yandex_health": health_monitor.snapshot() if health_monitor else None,
        "free_space": _free_space(cfg["download_dir"]),
        "news": NEWS_LINKS,
    }


# ── Логи: просмотр и архив для поддержки ────────────────────────────────────

_LOG_NAME_RE = re.compile(r"^(tasks/)?[A-Za-z0-9_.-]+\.(log|jsonl)(\.\d+)?$")


def _log_path(name: str) -> Path:
    """Путь к файлу лога по имени из API. Только внутри logs/, только логи."""
    if not _LOG_NAME_RE.match(name or ""):
        raise HTTPException(400, "Недопустимое имя лога")
    p = (LOG_DIR / name).resolve()
    if LOG_DIR.resolve() not in p.parents:
        raise HTTPException(400, "Недопустимое имя лога")
    if not p.is_file():
        raise HTTPException(404, "Лог не найден")
    return p


def list_logs() -> list[dict]:
    out = []
    if not LOG_DIR.exists():
        return out
    for p in sorted(LOG_DIR.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(LOG_DIR).as_posix()
        if not _LOG_NAME_RE.match(rel):
            continue
        st = p.stat()
        out.append({"name": rel, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def tail_file(path: Path, lines: int) -> list[str]:
    """Последние N строк файла без чтения всего файла в память."""
    lines = max(1, min(lines, 2000))
    chunk = 64 * 1024
    data = b""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        while pos > 0 and data.count(b"\n") <= lines:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]


@app.get("/api/logs")
def api_logs():
    return {"logs": list_logs(), "dir": str(LOG_DIR)}


@app.get("/api/logs/tail")
def api_logs_tail(name: str, lines: int = 200):
    p = _log_path(name)
    return {"name": name, "lines": tail_file(p, lines), "size": p.stat().st_size}


def support_info() -> dict:
    """Диагностика для поддержки: версия, окружение, конфиг без секретов, состояние БД и задач."""
    cfg = get_config()
    token = check_token(cfg["token"])
    acc = token.get("account") or {}
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app_version": APP_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "config": {
            "download_dir": cfg["download_dir"],
            "download_dir_set": cfg["download_dir_set"],
            "max_workers": cfg["max_workers"],
            "token_masked": mask_token(cfg["token"]),
            "token_ok": token.get("ok"),
            "token_msg": token.get("msg"),
            "login": acc.get("login"),
            "disk_used": acc.get("used"),
            "disk_total": acc.get("total"),
            "photo_unlim": acc.get("photo_unlim"),
            "is_paid": acc.get("is_paid"),
        },
        "cookies": check_cookies(),
        "free_space": _free_space(cfg["download_dir"]),
        "disk_db": db_stats(DB_FILE, "files", "remote_path"),
        "unlim_db": db_stats(UNLIM_DB_FILE, "unlim_files", "file_id"),
        "tasks": tm.status(),
        "pipeline": pipeline.status(),
        "diagnostics": diagnose_tasks(),
        "yandex_health": health_monitor.snapshot() if health_monitor else None,
    }


def build_support_bundle() -> Path:
    """ZIP со всеми логами и support-info.json — одним файлом в поддержку."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Старые архивы не копим
    for old in LOG_DIR.glob("yadisk-logs-*.zip"):
        try:
            if time.time() - old.stat().st_mtime > 3600:
                old.unlink()
        except OSError:
            pass
    name = f"yadisk-logs-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    bundle = LOG_DIR / name
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in list_logs():
            try:
                zf.write(LOG_DIR / entry["name"], entry["name"])
            except OSError:
                pass
        zf.writestr("support-info.json", json.dumps(support_info(), ensure_ascii=False, indent=2, default=str))
    log.info("собран архив логов для поддержки: %s", name)
    return bundle


@app.get("/api/logs/bundle")
def api_logs_bundle():
    bundle = build_support_bundle()
    return FileResponse(str(bundle), media_type="application/zip", filename=bundle.name)


class StartPayload(BaseModel):
    workers: int = Field(default=4, ge=1, le=MAX_UNLIM_WORKERS)
    batch: int = Field(default=DEFAULT_UNLIM_BATCH, ge=1, le=MAX_UNLIM_BATCH)
    do_disk: bool = True
    do_unlim: bool = True
    # None — качать всё; список — отфильтровать по категориям после scan/collect
    disk_categories: list[str] | None = None
    unlim_categories: list[str] | None = None


@app.post("/api/start")
def api_start(payload: StartPayload):
    src = preflight_sources(payload.do_disk, payload.do_unlim)
    if src["error"]:
        return {"ok": False, "msg": src["error"]}
    ok, msg = pipeline.start(workers=payload.workers,
                             do_disk=payload.do_disk,
                             do_unlim=payload.do_unlim,
                             batch=payload.batch,
                             disk_categories=payload.disk_categories,
                             unlim_categories=payload.unlim_categories)
    return {"ok": ok, "msg": msg, "notes": src["notes"] if ok else []}


# ── Мастер «План восстановления» ────────────────────────────────────────────


def _degraded(health: dict | None, diag: dict) -> bool:
    """Серверы Яндекса сейчас не в порядке — предлагаем щадящие настройки."""
    if health and health.get("overall") not in (None, "ok", "unknown"):
        return True
    return diag.get("kind") in ("bulk_disabled", "server_50x", "rate_limit", "timeout")


@app.get("/api/plan")
def api_plan():
    """Всё, что нужно мастеру: доступ, что есть в БД, скорость, место, рекомендации."""
    cfg = get_config()
    disk_db = db_stats(DB_FILE, "files", "remote_path")
    unlim_db = db_stats(UNLIM_DB_FILE, "unlim_files", "file_id")
    health = health_monitor.snapshot() if health_monitor else None
    diag = diagnose_tasks()
    degraded = _degraded(health, diag)
    rate_bps = (disk_db.get("bytes_10m", 0) + unlim_db.get("bytes_10m", 0)) / 600.0
    return {
        "access": {
            "token": check_token(cfg["token"]),
            "cookies": check_cookies(),
            "health": health,
        },
        "disk": {"preview": preview_db(DB_FILE, "files", "remote_path"), "db": disk_db},
        "unlim": {"preview": preview_db(UNLIM_DB_FILE, "unlim_files", "file_id"), "db": unlim_db},
        "download_dir": cfg["download_dir"],
        "free_space": _free_space(cfg["download_dir"]),
        "rate_bps": rate_bps,
        "recommended": {
            "gentle": degraded,
            "workers": 2 if degraded else 4,
            "batch": 10 if degraded else DEFAULT_UNLIM_BATCH,
        },
        "diagnostics": diag,
        "tasks_running": tm.any_alive(""),
        "pipeline_running": pipeline.is_running(),
    }


class PlanStartPayload(BaseModel):
    do_disk: bool = True
    do_unlim: bool = True
    disk_categories: list[str] = Field(default_factory=lambda: ["photos", "videos"])
    unlim_categories: list[str] = Field(default_factory=lambda: ["photos", "videos"])
    workers: int = Field(default=2, ge=1, le=MAX_UNLIM_WORKERS)
    batch: int = Field(default=10, ge=1, le=MAX_UNLIM_BATCH)


@app.post("/api/plan/start")
def api_plan_start(payload: PlanStartPayload):
    if not payload.do_disk and not payload.do_unlim:
        return {"ok": False, "msg": "Выберите хотя бы одно: диск или безлимит"}
    if tm.any_alive(""):
        return {"ok": False, "msg": "Сначала остановите текущие задачи"}
    src = preflight_sources(payload.do_disk, payload.do_unlim)
    if src["error"]:
        return {"ok": False, "msg": src["error"]}
    ok, msg = pipeline.start(
        workers=payload.workers, batch=payload.batch,
        do_disk=payload.do_disk, do_unlim=payload.do_unlim,
        disk_categories=payload.disk_categories if payload.do_disk else None,
        unlim_categories=payload.unlim_categories if payload.do_unlim else None,
    )
    return {"ok": ok, "msg": msg, "notes": src["notes"] if ok else []}


@app.post("/api/start/stop")
def api_start_stop():
    return _resp(pipeline.stop())


class TokenPayload(BaseModel):
    token: str


def _ensure_env_file() -> None:
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")


@app.post("/api/config/token/check")
def api_check_token(payload: TokenPayload):
    """Проверка без сохранения."""
    return check_token((payload.token or "").strip())


@app.post("/api/config/token")
def api_set_token(payload: TokenPayload):
    token = (payload.token or "").strip()
    if not token:
        raise HTTPException(400, "Пустой токен")
    _ensure_env_file()
    set_key(str(ENV_FILE), "YA_DISK_TOKEN", token, quote_mode="always")
    load_dotenv(ENV_FILE, override=True)
    check = check_token(token)
    log.info("токен сохранён (%s): %s", mask_token(token), check.get("login") if check.get("ok") else check.get("msg"))
    return {"ok": True, "check": check}


@app.post("/api/config/token/clear")
def api_clear_token():
    if ENV_FILE.exists():
        set_key(str(ENV_FILE), "YA_DISK_TOKEN", "", quote_mode="never")
        load_dotenv(ENV_FILE, override=True)
    log.info("токен удалён пользователем")
    return {"ok": True}


class DownloadDirPayload(BaseModel):
    download_dir: str


@app.post("/api/config/download_dir")
def api_set_dir(payload: DownloadDirPayload):
    d = (payload.download_dir or "").strip()
    ok, msg = is_safe_download_dir(d)
    if not ok:
        log.warning("папка отклонена: %r — %s", d, msg)
        raise HTTPException(400, msg)
    _ensure_env_file()
    set_key(str(ENV_FILE), "DOWNLOAD_DIR", d, quote_mode="always")
    load_dotenv(ENV_FILE, override=True)
    log.info("папка скачивания сохранена: %s", d)
    return {"ok": True, "download_dir": d, "free_space": _free_space(d)}


class CookiesPayload(BaseModel):
    cookies: list


@app.post("/api/cookies/check")
def api_check_cookies(payload: CookiesPayload):
    """Проверка без сохранения."""
    return _cookies_report(payload.cookies)


@app.post("/api/cookies")
def api_set_cookies(payload: CookiesPayload):
    """Сохраняет cookies.json атомарно через tmp+replace с предварительной валидацией."""
    val = _cookies_report(payload.cookies)
    if not val.get("ok"):
        raise HTTPException(400, val.get("msg", "Невалидные cookies"))
    arr = payload.cookies
    tmp = COOKIES_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, COOKIES_FILE)
    except Exception as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise HTTPException(500, f"Не удалось сохранить cookies.json: {e}")
    log.info("cookies сохранены: %d шт", len(arr))
    return {"ok": True, "check": check_cookies(), "saved_count": len(arr)}


@app.post("/api/cookies/clear")
def api_clear_cookies():
    if COOKIES_FILE.exists():
        try:
            COOKIES_FILE.unlink()
        except OSError as e:
            return {"ok": False, "msg": str(e)}
    log.info("cookies удалены пользователем")
    return {"ok": True}


# ── Выбор категорий ─────────────────────────────────────────────────────────


def _category_key_expr(table: str, id_col: str) -> str:
    """По чему определять категорию: у безлимита расширение надёжнее брать из name."""
    return f"COALESCE(name, {id_col})" if table == "unlim_files" else id_col


def preview_db(db_file: Path, table: str, id_col: str) -> dict:
    """Группирует файлы в БД по категориям."""
    table = _validate_sql_ident(table, _ALLOWED_TABLES, "table")
    id_col = _validate_sql_ident(id_col, _ALLOWED_COLUMNS, "column")
    if not db_file.exists():
        return {"empty": True, "categories": {}}
    c = None
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        summary: dict[str, dict] = {
            k: {"count": 0, "bytes": 0, "label": label}
            for k, label in CATEGORY_LABELS.items()
        }
        total = 0
        for row in c.execute(
            f"SELECT {_category_key_expr(table, id_col)}, COALESCE(size, 0) FROM {table} "
            f"WHERE status IN ('pending','downloaded','skipped','in_progress','failed')"
        ):
            cat = categorize(row[0])
            summary[cat]["count"] += 1
            summary[cat]["bytes"] += row[1]
            total += 1
        return {"empty": total == 0, "total": total, "categories": summary}
    except sqlite3.Error as e:
        return {"empty": True, "error": str(e)}
    finally:
        if c is not None:
            c.close()


def filter_to_categories(db_file: Path, table: str, id_col: str,
                         selected: list[str]) -> tuple[int, int]:
    """Помечает файлы вне выбранных категорий как 'skipped' (одним проходом).
    Уже скачанные/skipped/in_progress не трогаем.
    Возвращает (skipped_count, kept_pending).
    """
    table = _validate_sql_ident(table, _ALLOWED_TABLES, "table")
    id_col = _validate_sql_ident(id_col, _ALLOWED_COLUMNS, "column")
    if not db_file.exists():
        return (0, 0)
    sel = set(selected)
    c = sqlite3.connect(db_file, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        rows = c.execute(
            f"SELECT {id_col}, {_category_key_expr(table, id_col)} FROM {table} WHERE status='pending'"
        ).fetchall()
        to_skip = [ident for (ident, key) in rows if categorize(key) not in sel]
        kept = len(rows) - len(to_skip)
        if to_skip:
            with sqlite_tx(c):
                c.executemany(
                    f"UPDATE {table} SET status='skipped', error=? WHERE {id_col}=?",
                    [(CATEGORY_SKIP_MARKER, p) for p in to_skip],
                )
        return (len(to_skip), kept)
    finally:
        c.close()


def unskip_category_marker(db_file: Path, table: str) -> int:
    """Сбрасывает skipped-with-marker обратно в pending — на случай повторного выбора."""
    table = _validate_sql_ident(table, _ALLOWED_TABLES, "table")
    if not db_file.exists():
        return 0
    c = sqlite3.connect(db_file, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        with sqlite_tx(c):
            return c.execute(
                f"UPDATE {table} SET status='pending', error=NULL "
                f"WHERE status='skipped' AND error=?",
                (CATEGORY_SKIP_MARKER,),
            ).rowcount
    finally:
        c.close()


@app.get("/api/disk/preview")
def disk_preview():
    return preview_db(DB_FILE, "files", "remote_path")


@app.get("/api/unlim/preview")
def unlim_preview():
    return preview_db(UNLIM_DB_FILE, "unlim_files", "file_id")


class SelectivePayload(BaseModel):
    categories: list[str]
    workers: int = Field(default=4, ge=1, le=MAX_UNLIM_WORKERS)
    batch: int = Field(default=DEFAULT_UNLIM_BATCH, ge=1, le=MAX_UNLIM_BATCH)


@app.post("/api/disk/download_selected")
def disk_download_selected(payload: SelectivePayload):
    # Сначала вернуть всё что было ранее помечено как "не выбрано" в pending,
    # потом отфильтровать заново и запустить download
    unskip_category_marker(DB_FILE, "files")
    filter_to_categories(DB_FILE, "files", "remote_path", payload.categories)
    workers = max(1, min(payload.workers, MAX_DISK_WORKERS))
    return _resp(tm.start(SLOT_DISK, "main.py",
                          ["download", "--workers", str(workers), "--no-scan"]))


@app.post("/api/unlim/download_selected")
def unlim_download_selected(payload: SelectivePayload):
    unskip_category_marker(UNLIM_DB_FILE, "unlim_files")
    filter_to_categories(UNLIM_DB_FILE, "unlim_files", "file_id", payload.categories)
    return _launch_unlim_workers(payload.workers, payload.batch)


# ── Endpoints: диск ─────────────────────────────────────────────────────────


@app.post("/api/disk/scan")
def disk_scan():
    return _resp(tm.start(SLOT_DISK, "main.py", ["scan"]))


class DownloadOpts(BaseModel):
    workers: int = Field(default=4, ge=1, le=MAX_DISK_WORKERS)


@app.post("/api/disk/download")
def disk_download(opts: DownloadOpts):
    return _resp(tm.start(SLOT_DISK, "main.py",
                          ["download", "--workers", str(opts.workers), "--no-scan"]))


@app.post("/api/disk/retry")
def disk_retry():
    return _resp(tm.start(SLOT_DISK, "main.py", ["retry-failed"]))


@app.post("/api/disk/verify")
def disk_verify():
    return _resp(tm.start(SLOT_DISK, "main.py", ["verify"]))


@app.post("/api/disk/stop")
def disk_stop():
    return _resp(tm.stop(SLOT_DISK))


def _safe_remove_db_files(base_file: Path) -> tuple[bool, str]:
    """Удаляет файл SQLite и связанные wal/shm файлы с ожиданием и обработкой блокировок."""
    deadline = time.monotonic() + 3.0
    while True:
        errors = []
        for suffix in ("", "-wal", "-shm"):
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


@app.post("/api/disk/clear")
def disk_clear():
    """Полная очистка БД сканирования диска + надёжная остановка задач."""
    if pipeline.is_running():
        pipeline.stop()
    _stop_and_wait(tm.stop_matching(SLOT_DISK))
    return _resp(_safe_remove_db_files(DB_FILE))


@app.post("/api/unlim/clear")
def unlim_clear():
    """Полная очистка БД безлимита + надёжная остановка воркеров."""
    if pipeline.is_running():
        pipeline.stop()
    _stop_and_wait(tm.stop_matching(UNLIM_SLOT_PREFIX))
    return _resp(_safe_remove_db_files(UNLIM_DB_FILE))


# ── Endpoints: безлимит (photoslice) ────────────────────────────────────────


@app.post("/api/unlim/collect")
def unlim_collect():
    return _resp(tm.start(SLOT_UNLIM_COLLECT, "unlim_download.py", ["--collect"]))


class WorkersPayload(BaseModel):
    workers: int = Field(default=4, ge=1, le=MAX_UNLIM_WORKERS)
    batch: int = Field(default=DEFAULT_UNLIM_BATCH, ge=1, le=MAX_UNLIM_BATCH)


@app.post("/api/unlim/start_workers")
def unlim_start_workers(payload: WorkersPayload):
    return _launch_unlim_workers(payload.workers, payload.batch)


@app.post("/api/unlim/retry")
def unlim_retry(payload: WorkersPayload = WorkersPayload()):
    """Сбрасывает файлы с ошибками обратно в pending и перезапускает воркеры."""
    if not UNLIM_DB_FILE.exists():
        return {"ok": False, "msg": "БД безлимита пуста — сначала соберите список файлов"}
    try:
        # Если воркеры сейчас не работают — сбрасываем и зависшие in_progress
        reset_failed_files(
            UNLIM_DB_FILE,
            include_in_progress=not tm.any_alive(UNLIM_WORKER_SLOT_PREFIX),
        )
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    return _launch_unlim_workers(payload.workers, payload.batch)


@app.post("/api/unlim/stop")
def unlim_stop():
    return {"ok": True, "stopped": len(tm.stop_matching(UNLIM_SLOT_PREFIX))}


# ── Endpoints: organize / общее ─────────────────────────────────────────────


class OrganizePayload(BaseModel):
    input: str
    action: str = "report-only"  # report-only | move


def _is_safe_organize_path(path: str) -> tuple[bool, str]:
    """Сортировать можно только существующую НЕ системную папку и не папку самого инструмента."""
    ok, msg = is_safe_dir(path, must_exist=True)
    if not ok:
        return ok, msg
    try:
        root = Path(path).resolve()
        if root == PROJECT or root in PROJECT.parents:
            return False, "Нельзя сортировать папку самого инструмента: уедут state.db, .env и скрипты"
    except OSError:
        return False, "Невалидный путь"
    return True, "OK"


@app.post("/api/organize")
def organize(payload: OrganizePayload):
    ok, msg = _is_safe_organize_path(payload.input)
    if not ok:
        return {"ok": False, "msg": msg}
    if payload.action not in ("report-only", "move"):
        return {"ok": False, "msg": "Неизвестное действие"}
    if payload.action == "move" and (tm.any_alive(SLOT_DISK) or tm.any_alive(UNLIM_SLOT_PREFIX)):
        return {"ok": False, "msg": "Сначала остановите скачивание: сортировка во время загрузки перепутает файлы"}
    return _resp(tm.start(SLOT_ORGANIZE, "organize.py",
                          ["--input", payload.input,
                           "--action", payload.action]))


@app.post("/api/organize/stop")
def organize_stop():
    return _resp(tm.stop(SLOT_ORGANIZE))


@app.post("/api/stop_all")
def stop_all():
    return {"ok": True, "stopped": tm.stop_all()}


@app.post("/api/task/{slot}/clear_log")
def clear_task_log(slot: str):
    """Очищает буфер логов задачи (или всех slot с префиксом: 'unlim_w*')."""
    return {"ok": True, "cleared": tm.clear_logs(slot)}


if (TEMPLATES_DIR / "static").exists():
    app.mount("/static",
              StaticFiles(directory=str(TEMPLATES_DIR / "static")),
              name="static")


# ── Запуск ──────────────────────────────────────────────────────────────────


def _open_browser_when_ready(host: str, port: int):
    """Открывает браузер только когда сервер начал отвечать."""
    import http.client
    import webbrowser
    url = f"http://{host}:{port}"
    for _ in range(60):  # ждём до ~30 сек
        time.sleep(0.5)
        try:
            conn = http.client.HTTPConnection(host, port, timeout=1)
            conn.request("GET", "/api/status")
            resp = conn.getresponse()
            if resp.status == 200:
                conn.close()
                break
            conn.close()
        except Exception:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    global health_monitor
    ensure_utf8_stdio()
    _setup_logging()
    log.info("webui v%s старт: python %s, %s, проект %s", APP_VERSION,
             platform.python_version(), platform.platform(), PROJECT)

    import uvicorn
    url = f"http://{HOST}:{PORT}"

    # Монитор Яндекса: два лёгких запроса в минуту, история в logs/yandex_health.jsonl
    health_monitor = HealthMonitor(token_getter=lambda: get_config()["token"], log_path=HEALTH_LOG)
    health_monitor.start()
    print("=" * 65)
    print(f"  Yandex.Disk Backup — Web UI by Davron (PXSYS) · v{APP_VERSION}")
    print(f"  Адрес: {url}")
    print(f"  Telegram: {TELEGRAM_CHANNEL} (новости, помощь, статус Яндекса)")
    print(f"  YouTube: {YOUTUBE_VIDEO_URL}")
    print("=" * 65)
    print()
    print("  Сервер запускается, браузер откроется автоматически.")
    print("  Чтобы остановить — закрой это окно или нажми Ctrl+C.")
    print()

    # Открыть браузер в фоне когда сервер готов (YADISK_NO_BROWSER=1 — не открывать: dev/CI)
    if os.getenv("YADISK_NO_BROWSER") != "1":
        threading.Thread(
            target=_open_browser_when_ready,
            args=(HOST, PORT),
            daemon=True,
        ).start()

    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
