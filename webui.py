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
import mimetypes
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
import yadisk
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from downloader import account_info, api_error_details, make_client
from unlim_common import (
    DEFERRED_MARKER,
    RELEASE_IN_PROGRESS_SQL,
    clear_dead_hosts,
    missing_required_cookies,
    reset_failed_files,
    unlim_tx,
)
from utils import (
    APP_VERSION,
    CATEGORY_LABELS,
    DEFAULT_UNLIM_BATCH,
    DONATION_URL,
    EXIT_AUTH,
    EXIT_DEFERRED,
    EXIT_DISK_FULL,
    EXIT_INTERRUPTED,
    EXIT_OUTAGE,
    EXIT_PARTIAL,
    MAX_DISK_WORKERS,
    MAX_UNLIM_BATCH,
    MAX_UNLIM_WORKERS,
    IS_WINDOWS,
    PREVIEW_DIRNAME,
    TELEGRAM_CHANNEL,
    TELEGRAM_HANDLE,
    YOUTUBE_VIDEO_URL,
    categorize,
    classify_yandex_error,
    ensure_utf8_stdio,
    is_safe_dir,
    is_safe_download_dir,
    keep_awake,
    previews_enabled,
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

# Коды выхода дочерних скриптов (константы — в utils.py). Это же объяснение уходит во
# фронтенд через /api/status.exit_codes, своей копии у фронтенда нет.
_EXIT_EXPLAIN = {
    EXIT_PARTIAL: "часть файлов не скачалась — их можно повторить",
    EXIT_AUTH: "ключ доступа к Диску или доступ к Безлимиту устарел — обновите его в настройках",
    EXIT_OUTAGE: "Яндекс временно не отвечает или отказывает — программа повторит позже",
    EXIT_DISK_FULL: "на локальном диске нет места — освободите место и запустите снова",
    EXIT_DEFERRED: "всё доступное скачано, остальные файлы ждут, когда серверы Яндекса оживут",
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


# Диагностика смотрит только задачи, которые идут или закончились за последние 10 минут
DIAG_RECENT_SEC = 600


def task_kind(cmd: list[str]) -> str:
    """Что делает задача — по её команде (слот disk делят scan и download)."""
    script = os.path.basename(cmd[0]) if cmd else ""
    first = cmd[1] if len(cmd) > 1 else ""
    if script == "main.py":
        return "disk_scan" if first == "scan" else "disk_download" if first in (
            "download", "retry-failed") else f"disk_{first or 'other'}"
    return {
        "unlim_download.py": "unlim_collect",
        "unlim_worker.py": "unlim_worker",
        "organize.py": "organize",
    }.get(script, script.removesuffix(".py") or "other")


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
        self.kind = task_kind(self.cmd)
        self.started_at = time.time()
        self.finished_at: float | None = None
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
            self.finished_at = time.time()
            if f is not None:
                try:
                    f.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} конец: exit={self.proc.poll()}"
                            f"{' (остановлено пользователем)' if self.was_stopped else ''} =====\n")
                    f.close()
                except OSError:
                    pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def terminate(self) -> None:
        """Просит процесс завершиться и не ждёт — можно звать под блокировкой."""
        self.was_stopped = True
        if self.alive():
            self.proc.terminate()

    def wait_or_kill(self, timeout: float = 5.0) -> None:
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def stop(self):
        self.terminate()
        self.wait_or_kill()

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
            "kind": self.kind,
            "finished_at": self.finished_at,
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
            t.terminate()
        # Ждём уже без блокировки: иначе /api/status замирает, пока процесс выходит
        t.wait_or_kill()
        log.info("задача %s остановлена пользователем", slot)
        return True, f"Остановлено: {slot}"

    def stop_matching(self, prefix: str) -> list[Task]:
        """Останавливает все живые задачи, чей слот начинается с prefix. Возвращает их.

        Сначала всем шлём terminate, потом ждём каждую — вне блокировки: шесть
        воркеров раньше держали /api/status до 30 секунд.
        """
        stopped: list[Task] = []
        with self._lock:
            for slot, t in self._tasks.items():
                if slot.startswith(prefix) and t.alive():
                    t.terminate()
                    stopped.append(t)
                    log.info("задача %s остановлена (по префиксу %r)", slot, prefix)
        for t in stopped:
            t.wait_or_kill()
        return stopped

    def stop_all(self) -> int:
        return len(self.stop_matching(""))

    def any_alive(self, prefix: str) -> bool:
        with self._lock:
            return any(slot.startswith(prefix) and t.alive() for slot, t in self._tasks.items())

    def status(self) -> dict:
        with self._lock:
            return {slot: t.snapshot() for slot, t in self._tasks.items()}

    def recent_log_lines(self, per_task: int = 40, recent_sec: float = DIAG_RECENT_SEC) -> list[str]:
        """Хвосты логов живых задач и задач, закончившихся не раньше recent_sec назад.

        Старые задачи не учитываем: исправленная проблема не должна висеть баннером.
        """
        now = time.time()
        with self._lock:
            lines: list[str] = []
            for t in self._tasks.values():
                if t.alive() or (t.finished_at is not None and now - t.finished_at <= recent_sec):
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
    """plan_sources по текущим настройкам — до запуска, чтобы ответить пользователю сразу.

    Яндекс не ответил на проверку токена — это не «нет токена»: Диск всё равно пробуем.
    """
    tok = check_token(get_config()["token"]) if do_disk else {}
    token_ok = bool(tok.get("ok") or tok.get("unreachable"))
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


# ── Режим спасения ──────────────────────────────────────────────────────────

# Список файлов, собранный в этом процессе позже этого срока, при новом старте не пересобираем
RESCUE_RELIST_SEC = 6 * 3600
# Столько раундов подряд с непонятным кодом выхода — дальше просим прислать отчёт
RESCUE_STUCK_ROUNDS = 3
# Временные ошибки Диска: явный старт возвращает такие файлы в очередь
_DISK_TRANSIENT_ERRORS = ("local_disk |", "auth_expired |", "server_50x |",
                          "download_timeout |", "rate_limit |")
_SOURCE_LABEL = {"disk": "Диск", "unlim": "Безлимит"}
_BRANCH_STATE_TEXT = {
    "checking": "проверяю доступ", "listing": "получаю список файлов", "downloading": "скачиваю",
    "waiting": "ждём Яндекса", "done": "готово", "needs_action": "нужно ваше действие",
    "stopped": "остановлено", "error": "ошибка",
}


def _retry_every_sec() -> int:
    """Пауза между раундами повторов. YADISK_RETRY_EVERY_SEC — для живых проверок."""
    return safe_int(os.environ.get("YADISK_RETRY_EVERY_SEC"), 1800, 60, 24 * 3600)


def _hhmm(ts: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else ""


def _db_has_rows(db_file: Path, table: str) -> bool:
    table = _validate_sql_ident(table, _ALLOWED_TABLES, "table")
    if not db_file.exists():
        return False
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        try:
            return c.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        finally:
            c.close()
    except sqlite3.Error:
        return False


def _reset_transient_disk_failures() -> int:
    """Явный старт: файлы, упавшие из-за сбоев (а не из-за самих файлов), — снова в очередь."""
    if not DB_FILE.exists():
        return 0
    try:
        c = sqlite3.connect(DB_FILE, timeout=30)
    except sqlite3.Error:
        return 0
    try:
        where = " OR ".join("error LIKE ?" for _ in _DISK_TRANSIENT_ERRORS)
        with sqlite_tx(c):
            return c.execute(
                f"UPDATE files SET status='pending', error=NULL WHERE status='failed' AND ({where})",
                [p + "%" for p in _DISK_TRANSIENT_ERRORS],
            ).rowcount
    except sqlite3.Error:
        return 0
    finally:
        c.close()


class Branch:
    """Одна ветка спасения: «disk» (scan → download) или «unlim» (collect → воркеры)."""

    def __init__(self, name: str):
        self.name = name
        self.thread: threading.Thread | None = None
        self.wake = threading.Event()
        self.reset()

    def reset(self) -> None:
        self.state = "off"  # off|checking|listing|downloading|waiting|done|needs_action|stopped|error
        self.action: str | None = None  # token | cookies | free_space | send_logs
        self.reason = ""                # итог простыми словами
        self.activity = ""              # что делается прямо сейчас
        self.round = 0
        self.next_retry_at: float | None = None
        self.last_exit: dict | None = None
        self.odd_rounds = 0
        self.reset_done = False
        self.wake.clear()

    @property
    def label(self) -> str:
        return _SOURCE_LABEL[self.name]

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def snapshot(self) -> dict:
        return {
            "state": self.state, "action": self.action, "reason": self.reason,
            "activity": self.activity, "round": self.round,
            "next_retry_at": self.next_retry_at, "last_exit": self.last_exit,
            "running": self.alive(),
        }


class Rescue:
    """«Режим спасения»: Диск и Безлимит качаются параллельно, недоступное — раундами.

    Ветка проходит очередь до конца. Если остались файлы на молчащих серверах Яндекса
    (код 5) или Яндекс не отвечает (код 3), она ждёт retry_every секунд или кнопку
    «Попробовать сейчас» и качает снова — без повторного сбора списка. Код 2 ставит
    ветку в «нужно действие» (новый ключ или доступ), код 4 — обе ветки (нет места).

    status() сохраняет прежний формат Pipeline (state/step/log), чтобы старый
    интерфейс продолжал работать.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self.branches = {"disk": Branch("disk"), "unlim": Branch("unlim")}
        self.log: deque[str] = deque(maxlen=200)
        self.state = "idle"  # idle | running | done | failed | stopped
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.workers = 4
        self.batch = DEFAULT_UNLIM_BATCH
        self.skip_disk = False
        self.skip_unlim = False
        self.disk_categories: list[str] | None = None
        self.unlim_categories: list[str] | None = None
        self.partial = False
        self.retry_every = _retry_every_sec()
        self._owned_slots: set[str] = set()
        # Когда список файлов собирался в этом процессе — чтобы не пересобирать на каждый старт
        self._listed_at: dict[str, float] = {}

    # ── управление ──

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, workers: int = 4, do_disk: bool = True, do_unlim: bool = True,
              batch: int = DEFAULT_UNLIM_BATCH,
              disk_categories: list[str] | None = None,
              unlim_categories: list[str] | None = None) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                # Повторный старт во время ожидания = «Попробовать сейчас»
                if self._wake_waiting():
                    return True, "Пробую отложенные файлы прямо сейчас"
                return False, "Спасение уже идёт"
            self._stop_evt.clear()
            self._owned_slots.clear()
            self.state = "running"
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
            self.retry_every = _retry_every_sec()
            for b in self.branches.values():
                b.reset()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, "Спасение запущено"

    def retry_now(self) -> tuple[bool, str]:
        if not self.is_running():
            return False, "Спасение не запущено"
        if self._wake_waiting():
            return True, "Пробую отложенные файлы прямо сейчас"
        return False, "Сейчас нечего повторять — всё и так качается"

    def _wake_waiting(self) -> bool:
        waiting = [b for b in self.branches.values() if b.state == "waiting"]
        if waiting:
            clear_dead_hosts(str(UNLIM_DB_FILE))
            self._log("🔁 Повторяю отложенные файлы по запросу")
            for b in waiting:
                b.wake.set()
        return bool(waiting)

    def stop(self) -> tuple[bool, str]:
        if not self.is_running():
            return False, "Спасение не идёт"
        self._stop_evt.set()
        for b in self.branches.values():
            b.wake.set()
        # Гасим ВСЕ задачи Диска и Безлимита — в том числе запущенные с вкладок вручную
        _stop_and_wait(tm.stop_matching(SLOT_DISK) + tm.stop_matching(UNLIM_SLOT_PREFIX))
        if self._thread is not None:
            self._thread.join(timeout=10)
        _release_orphaned_unlim()
        self._log("⏹ остановлено пользователем")
        return True, "Остановлено"

    # ── статус ──

    def status(self) -> dict:
        if not self.started_at:
            elapsed = None
        elif self.finished_at:
            elapsed = self.finished_at - self.started_at
        else:
            elapsed = time.time() - self.started_at
        retry_at = [b.next_retry_at for b in self.branches.values() if b.next_retry_at]
        return {
            "state": self.state,
            "running": self.is_running(),
            "step": self._step_text(),
            "started_at": self.started_at,
            "elapsed": elapsed,
            "partial": self.partial,
            "log": list(self.log),
            "branches": {name: b.snapshot() for name, b in self.branches.items()},
            "next_retry_at": min(retry_at) if retry_at else None,
            "retry_every": self.retry_every,
        }

    def _step_text(self) -> str:
        parts = []
        for b in self.branches.values():
            if b.state == "off":
                continue
            text = _BRANCH_STATE_TEXT.get(b.state, b.state)
            if b.state == "waiting" and b.next_retry_at:
                text += f" до {_hhmm(b.next_retry_at)}"
            parts.append(f"{b.label}: {text}")
        return " · ".join(parts) or "—"

    # ── внутреннее ──

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"{ts}  {msg}")
        log.info("rescue: %s", msg)

    def _stopped(self) -> bool:
        return self._stop_evt.is_set()

    def _own_start(self, slot: str, script: str, args: list[str]) -> tuple[bool, str]:
        """Запускает задачу через tm и помечает её как свою."""
        ok, msg = tm.start(slot, script, args)
        if ok:
            self._owned_slots.add(slot)
        return ok, msg

    def _wait_task(self, slot: str, on_finish_msg: str = "") -> int:
        """Ждёт задачу, которую только что запустили в слоте. -1 — спасение остановлено."""
        task = tm.get(slot)
        while not self._stopped():
            if task is None:
                task = tm.get(slot)
            elif not task.alive():
                code = task.proc.poll()
                if on_finish_msg:
                    self._log(f"{on_finish_msg} (код {code})")
                if task.was_stopped:
                    return -1
                return code if code is not None else 0
            self._stop_evt.wait(0.5)
        return -1

    def _wait_workers(self, ids: list[int]) -> list[int | None] | None:
        """Ждёт воркеров безлимита. None — спасение или воркеры остановлены."""
        tasks = [tm.get(f"{UNLIM_WORKER_SLOT_PREFIX}{i}") for i in ids]
        while not self._stopped() and any(t is not None and t.alive() for t in tasks):
            self._stop_evt.wait(1.0)
        if self._stopped() or any(t is not None and t.was_stopped for t in tasks):
            return None
        return [t.proc.poll() for t in tasks if t is not None]

    def _run_step(self, b: Branch, slot: str, script: str, args: list[str], activity: str) -> int:
        """Запускает задачу ветки и ждёт её. Код выхода; -1 — остановлено."""
        b.activity = activity
        while True:
            if self._stopped():
                return -1
            ok, msg = self._own_start(slot, script, args)
            if ok:
                break
            # Слот занят задачей, запущенной вручную, — дождёмся её и запустим свою
            self._log(f"⏳ {b.label}: {activity} — жду уже запущенную задачу ({msg})")
            if self._stop_evt.wait(5):
                return -1
        code = self._wait_task(slot, f"{b.label}: {activity} — завершено")
        if code == EXIT_INTERRUPTED:
            return -1
        if code != -1:
            b.last_exit = {"code": code, "at": time.time(), "step": activity}
        return code

    def _needs_listing(self, name: str) -> bool:
        at = self._listed_at.get(name)
        return at is None or time.time() - at > RESCUE_RELIST_SEC

    def _apply_categories(self, db_file: Path, table: str, id_col: str,
                          categories: list[str], label: str) -> None:
        unskip_category_marker(db_file, table)
        skipped, kept = filter_to_categories(db_file, table, id_col, categories)
        names = ", ".join(CATEGORY_LABELS.get(c, c) for c in categories) or "ничего"
        self._log(f"🎯 {label}: качаем {names} — в очереди {kept}, пропущено {skipped}")

    def _apply_category_choice(self, db_file: Path, table: str, id_col: str,
                               categories: list[str] | None, label: str) -> None:
        if categories is None:
            # «Всё»: возвращаем то, что сняли прошлым выбором категорий
            n = unskip_category_marker(db_file, table)
            if n:
                self._log(f"↩️ {label}: вернул в очередь {n} файлов, снятых прошлым выбором категорий")
        else:
            self._apply_categories(db_file, table, id_col, categories, label)

    # ── главный поток и ветки ──

    def _run(self) -> None:
        # Поток живёт всё время спасения — на Windows флаг «не спать» привязан к нему
        with keep_awake():
            self._run_awake()

    def _run_awake(self) -> None:
        try:
            cfg = get_config()
            tok: dict = {"ok": False}
            if not self.skip_disk:
                self.branches["disk"].state = "checking"
                self._log("🔑 Проверяю ключ доступа к Диску...")
                tok = check_token(cfg["token"])
                if tok.get("ok"):
                    self._log(f"✓ Ключ работает: {tok.get('login')}, {tok.get('used', 0) / 1e9:.1f} ГБ занято")
                elif tok.get("unreachable"):
                    self._log("⚠️ Яндекс не ответил на проверку ключа — попробую качать, ключ, скорее всего, в порядке")
                else:
                    self._log(f"✗ Ключ: {tok.get('msg')}")
            cookies: dict = {"ok": False}
            if not self.skip_unlim:
                cookies = check_cookies()
                self._log(f"{'✓' if cookies.get('ok') else '✗'} Доступ к Безлимиту: {cookies.get('msg')}")
            token_usable = bool(tok.get("ok") or tok.get("unreachable"))
            src = plan_sources(not self.skip_disk, not self.skip_unlim, token_usable, bool(cookies.get("ok")))
            for note in src["notes"]:
                self._log(note)
            if src["error"]:
                self._log(f"❌ {src['error']}")
                for b in self.branches.values():
                    b.state = "off"
                self.state = "failed"
                return
            self.partial = bool(src["notes"])

            threads = []
            for name, run, target in (("disk", src["run_disk"], self._run_disk),
                                      ("unlim", src["run_unlim"], self._run_unlim)):
                b = self.branches[name]
                if not run:
                    b.state = "off"
                    continue
                b.state = "checking"
                b.thread = threading.Thread(target=self._branch_main, args=(b, target), daemon=True)
                b.thread.start()
                threads.append(b.thread)
            for t in threads:
                t.join()
            self.state = self._final_state()
            self._log({
                "done": "🏁 Готово, но часть источников пропущена (см. выше)" if self.partial else "🏆 ВСЁ ГОТОВО!",
                "failed": "⚠️ Нужно ваше действие — подробности выше",
                "stopped": "⏹ Спасение остановлено. Уже скачанное сохранено",
            }.get(self.state, self.state))
        except Exception as e:
            diag = classify_yandex_error(e)
            if diag["is_yandex_server"]:
                self._log(f"⚠️ Сбой серверов Яндекса: {diag['title']}")
                self._log(f"   {diag['message']}")
                self._log(f"   Новости дата-центров Яндекса: {TELEGRAM_CHANNEL}")
            else:
                self._log(f"❌ Ошибка: {e}")
            log.exception("rescue")
            self.state = "failed"
        finally:
            self.finished_at = time.time()

    def _final_state(self) -> str:
        states = [b.state for b in self.branches.values() if b.state != "off"]
        if any(s in ("needs_action", "error") for s in states):
            return "failed"
        if self._stopped() or any(s == "stopped" for s in states):
            return "stopped"
        return "done"

    def _branch_main(self, b: Branch, target: Callable[[Branch], None]) -> None:
        try:
            target(b)
        except Exception as e:  # noqa: BLE001 — ветка не должна ронять соседнюю
            log.exception("rescue %s", b.name)
            b.state = "error"
            b.action = "send_logs"
            b.reason = f"Внутренняя ошибка программы: {e}"
            self._log(f"❌ {b.label}: {e}")

    def _drive(self, b: Branch, prepare: Callable[[Branch], int], download: Callable[[Branch], int],
               db: tuple[Path, str, str]) -> None:
        """Общий цикл ветки: подготовить список → качать → ждать раунд, пока есть что качать."""
        ready = False
        while not self._stopped():
            b.round += 1
            if not ready:
                code = prepare(b)
                if code == -1:
                    return self._branch_stopped(b)
                if code != 0:
                    if not self._after_exit(b, code, db):
                        return
                    continue
                ready = True
            code = download(b)
            if code == -1:
                return self._branch_stopped(b)
            if not self._after_exit(b, code, db):
                return
        self._branch_stopped(b)

    def _branch_stopped(self, b: Branch) -> None:
        if b.state not in ("needs_action", "error"):
            b.state = "stopped"
        b.activity = ""
        b.next_retry_at = None

    def _after_exit(self, b: Branch, code: int, db: tuple[Path, str, str]) -> bool:
        """Что делать после шага с этим кодом. True — раунд отождан, качаем снова."""
        stats = db_stats(*db)
        if code == EXIT_AUTH:
            b.state = "needs_action"
            b.action = "token" if b.name == "disk" else "cookies"
            b.reason = ("Яндекс перестал принимать ключ доступа к Диску" if b.name == "disk"
                        else "Нужно обновить доступ к Безлимиту")
            self._log(f"🔑 {b.label}: {b.reason}. Остальное продолжает качаться")
            return False
        if code == EXIT_DISK_FULL:
            self._halt_for_disk()
            return False
        deferred = int(stats.get("deferred") or 0)
        if code in (EXIT_OUTAGE, EXIT_DEFERRED) or (code in (0, EXIT_PARTIAL) and deferred):
            b.odd_rounds = 0
            return self._wait_round(b, deferred, outage=(code == EXIT_OUTAGE))
        if code in (0, EXIT_PARTIAL):
            failed = int(stats.get("failed") or 0)
            b.state = "done"
            b.activity = ""
            b.reason = (f"Скачано почти всё: {failed} файлов не скачались" if failed
                        else "Все файлы сохранены")
            self._log(f"✓ {b.label}: {b.reason}")
            return False
        b.odd_rounds += 1
        self._log(f"⚠️ {b.label}: шаг завершился с кодом {code} ({b.odd_rounds}/{RESCUE_STUCK_ROUNDS})")
        if b.odd_rounds >= RESCUE_STUCK_ROUNDS:
            b.state = "needs_action"
            b.action = "send_logs"
            b.reason = "Что-то пошло не так — отправьте отчёт для поддержки"
            return False
        return self._wait_round(b, deferred, outage=True)

    def _wait_round(self, b: Branch, deferred: int, outage: bool) -> bool:
        b.state = "waiting"
        b.next_retry_at = time.time() + self.retry_every
        b.reason = (f"Ждут Яндекса: {deferred} файлов" if deferred and not outage
                    else "Яндекс сейчас не отвечает")
        b.activity = f"следующая попытка в {_hhmm(b.next_retry_at)}"
        self._log(f"⏳ {b.label}: {b.reason}. Следующая попытка в {_hhmm(b.next_retry_at)}")
        while not self._stopped():
            remaining = b.next_retry_at - time.time()
            if remaining <= 0 or b.wake.wait(min(remaining, 1.0)):
                break
        b.wake.clear()
        b.next_retry_at = None
        if self._stopped():
            self._branch_stopped(b)
            return False
        return True

    def _halt_for_disk(self) -> None:
        """Нет места — обе ветки ждут человека: скачанное дальше всё равно не ляжет."""
        for x in self.branches.values():
            if x.state != "off":
                x.state = "needs_action"
                x.action = "free_space"
                x.reason = "На диске закончилось место"
                x.activity = ""
        self._log("💾 На диске закончилось место — останавливаю скачивание. Освободите место и продолжите")
        self._stop_evt.set()
        for x in self.branches.values():
            x.wake.set()
        _stop_and_wait(tm.stop_matching(SLOT_DISK) + tm.stop_matching(UNLIM_SLOT_PREFIX))

    # ── Диск ──

    def _run_disk(self, b: Branch) -> None:
        self._drive(b, self._disk_prepare, self._disk_download, (DB_FILE, "files", "remote_path"))

    def _disk_prepare(self, b: Branch) -> int:
        if not b.reset_done:
            b.reset_done = True
            n = _reset_transient_disk_failures()
            if n:
                self._log(f"↩️ Диск: {n} файлов с временными ошибками снова в очереди")
        if self._needs_listing("disk"):
            b.state = "listing"
            code = self._run_step(b, SLOT_DISK, "main.py", ["scan"], "получаю список файлов")
            if code == -1:
                return -1
            if code == 0:
                self._listed_at["disk"] = time.time()
            elif _db_has_rows(DB_FILE, "files"):
                self._log(f"⚠️ Диск: список не обновился (код {code}) — качаю по уже собранному")
            else:
                return code
        self._apply_category_choice(DB_FILE, "files", "remote_path", self.disk_categories, "Диск")
        return 0

    def _disk_download(self, b: Branch) -> int:
        b.state = "downloading"
        workers = min(self.workers, MAX_DISK_WORKERS)
        return self._run_step(b, SLOT_DISK, "main.py", ["download", "--workers", str(workers), "--no-scan"],
                              "скачиваю файлы")

    # ── Безлимит ──

    def _run_unlim(self, b: Branch) -> None:
        self._drive(b, self._unlim_prepare, self._unlim_download, (UNLIM_DB_FILE, "unlim_files", "file_id"))

    def _unlim_prepare(self, b: Branch) -> int:
        if not b.reset_done:
            b.reset_done = True
            n = reset_failed_files(str(UNLIM_DB_FILE)) if UNLIM_DB_FILE.exists() else 0
            if n:
                self._log(f"↩️ Безлимит: {n} файлов с ошибками снова в очереди")
        if self._needs_listing("unlim"):
            b.state = "listing"
            code = self._run_step(b, SLOT_UNLIM_COLLECT, "unlim_download.py", ["--collect"],
                                  "получаю список файлов")
            if code == -1:
                return -1
            if code == 0:
                self._listed_at["unlim"] = time.time()
            elif _db_has_rows(UNLIM_DB_FILE, "unlim_files"):
                self._log(f"⚠️ Безлимит: список не обновился (код {code}) — качаю по уже собранному")
            else:
                return code
        self._apply_category_choice(UNLIM_DB_FILE, "unlim_files", "file_id", self.unlim_categories, "Безлимит")
        return 0

    def _unlim_download(self, b: Branch) -> int:
        b.state = "downloading"
        b.activity = "скачиваю файлы"
        # Каждый раунд даёт молчавшим серверам ещё один шанс
        clear_dead_hosts(str(UNLIM_DB_FILE))
        while True:
            if self._stopped():
                return -1
            res = _launch_unlim_workers(self.workers, self.batch, starter=self._own_start)
            if res["started"]:
                break
            # Все слоты заняты воркерами, запущенными вручную, — дождёмся их
            self._log("⏳ Безлимит: жду уже запущенных воркеров")
            if self._stop_evt.wait(5):
                return -1
        self._log(f"🚀 Безлимит: запущено воркеров — {len(res['started'])}")
        codes = self._wait_workers(res["started"])
        if codes is None:
            return -1
        code = self._combine_worker_codes(codes)
        b.last_exit = {"code": code, "at": time.time(), "step": "скачиваю файлы"}
        return code

    @staticmethod
    def _combine_worker_codes(codes: list[int | None]) -> int:
        real = [c for c in codes if c is not None]
        for important in (EXIT_DISK_FULL, EXIT_AUTH, EXIT_DEFERRED, EXIT_OUTAGE):
            if important in real:
                return important
        bad = [c for c in real if c != 0]
        return bad[0] if bad else 0


# Прежнее имя: эндпоинты и тесты обращаются к pipeline
Pipeline = Rescue
pipeline = Rescue()
rescue = pipeline


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
        # Отложенные (сервер хранения молчит) — часть pending
        "deferred": 0, "bytes_deferred": 0, "deferred_hosts": 0,
        # Страховочные превью у ещё не скачанных оригиналов
        "previews": 0,
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
            f"SELECT status, error, (COALESCE(error, '') LIKE ?) AS deferred, "
            f"COUNT(*) AS n, COALESCE(SUM(size),0) AS b "
            f"FROM {table} GROUP BY status, (COALESCE(error,'') = ?), (COALESCE(error, '') LIKE ?)",
            (DEFERRED_MARKER + "%", CATEGORY_SKIP_MARKER, DEFERRED_MARKER + "%"),
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
                if r["deferred"]:
                    out["deferred"] += n
                    out["bytes_deferred"] += b
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
        if out["deferred"]:
            try:
                out["deferred_hosts"] = c.execute(
                    f"SELECT COUNT(DISTINCT storage_host) FROM {table} "
                    f"WHERE status='pending' AND error LIKE ? AND storage_host IS NOT NULL",
                    (DEFERRED_MARKER + "%",),
                ).fetchone()[0]
            except sqlite3.OperationalError:  # старая БД без колонки storage_host
                pass
        try:
            out["previews"] = c.execute(
                f"SELECT COUNT(*) FROM {table} WHERE preview_path IS NOT NULL AND status <> 'downloaded'"
            ).fetchone()[0]
        except sqlite3.OperationalError:  # старая БД без колонки preview_path
            pass
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
    except (yadisk.exceptions.UnauthorizedError, yadisk.exceptions.ForbiddenError) as e:
        data = {"ok": False, "msg": f"Токен не принят Яндексом: {type(e).__name__}{api_error_details(e)}"}
        log.warning("токен отклонён: %s", data["msg"])
    except Exception as e:
        # Сеть, 50x, таймаут: Яндекс не ответил — это не значит, что токен плохой
        data = {"ok": False, "unreachable": True,
                "msg": f"Яндекс не ответил на проверку токена ({type(e).__name__}). Повторю позже."}
        log.warning("проверка токена не удалась: %s: %s%s", type(e).__name__, e, api_error_details(e))
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
        {"what": "Безлимит (веб-версия Диска)", "state": unlim[0], "text": unlim[1]},
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
        # Один отказ на один файл — не повод кричать «Яндекс отказывает»
        "min_lines": 3,
        "strong_markers": ("bulk_unavailable",),
        "title": "Яндекс отказывает в скачивании безлимита",
        "message": "Воркер безлимита раз за разом получает от Яндекса отказ вместо файлов.",
        "explain": "С 11 октября 2026 после атак на дата-центры Яндекс отключил массовое скачивание "
                   "в веб-версии Диска (папки, наборы файлов, альбомы, ZIP-архивы). Поэтому программа "
                   "качает безлимит по одному файлу, как кнопка «Скачать» в веб-версии. Сейчас Яндекс "
                   "отказывает и в этом: отвечает 403 при живой сессии или присылает страницу вместо файла. "
                   "Одиночные файлы обычного диска через официальный API Яндекс отдаёт.",
        "recommendation": "Качайте обычный диск сейчас. Безлимит оставьте: воркер ждёт растущими "
                          "паузами, попытки файлов не сжигает и сам продолжит, когда Яндекс снимет ограничение "
                          "(через час без успеха он выйдет — запустите снова позже).",
        "affects": _affects(("ok", "работает"), ("wait", "ждёт разрешения Яндекса")),
    },
    {
        "kind": "storage_down",
        "markers": ("storage_down",),
        "title": "Часть файлов лежит на серверах Яндекса, которые не отвечают",
        "message": "Серверы хранения Яндекса с частью файлов молчат — такие файлы отложены.",
        "explain": "Каждый файл хранится на конкретном сервере Яндекса. После атак на дата-центры "
                   "часть серверов не отвечает: эти файлы не отдаёт ни программа, ни веб-версия, ни "
                   "официальный API. Воркер откладывает их без сжигания попыток, качает остальные и "
                   "через 30 минут пробует отложенные снова.",
        "recommendation": "Ничего делать не нужно: доступные файлы качаются. Если воркер вышел с кодом 3, "
                          "остались только недоступные файлы — запустите скачивание позже. "
                          "Не удаляйте эти файлы с Диска.",
        "affects": _affects(("ok", "работает"), ("wait", "часть файлов отложена")),
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
        "message": "Сбой или перегрузка серверов Яндекса (50x/504).",
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
        "title": "Яндекс не выдал часть файлов безлимита",
        "message": "Яндекс отвечает ошибкой на запрос ссылки на файл или сборки архива.",
        "explain": "Веб-версия Диска вернула ошибку вместо ссылки на файл (или архива). Такие файлы "
                   "возвращаются в очередь и повторяются; после 3 неудачных попыток файл помечается ошибкой — "
                   "его можно повторить позже кнопкой «Повторить ошибки».",
        "recommendation": "Ничего делать не нужно: файлы повторяются сами. Если ошибок много — снизьте число воркеров.",
        "affects": _affects(("ok", "работает"), ("wait", "повторяет файлы")),
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
    def fired(rule: dict) -> bool:
        n = counts[rule["kind"]]
        if n >= rule.get("min_lines", 1):
            return True
        strong = rule.get("strong_markers", ())
        return bool(n) and any(m in ln for ln in lines for m in strong)

    for rule in _DIAG_RULES:
        if fired(rule):
            diag["kind"] = rule["kind"]
            for key in ("title", "message", "explain", "recommendation", "affects"):
                diag[key] = rule[key]
            if rule["kind"] in _DIAG_FLAG_KINDS:
                diag[f"{rule['kind']}_detected"] = True
            break
    return diag


# ── Endpoints: статус и конфиг ──────────────────────────────────────────────


# Какой интерфейс открывать по «/»: new — версия 2.0, old — прежний (?ui=old)
UI_DEFAULT = "new"


@app.get("/", response_class=HTMLResponse)
def index(ui: str | None = None):
    use_new = (ui or UI_DEFAULT) == "new" and (TEMPLATES_DIR / "app.html").exists()
    html_path = TEMPLATES_DIR / ("app.html" if use_new else "index.html")
    if not html_path.exists():
        return HTMLResponse("<h1>UI не установлен</h1>")
    html = html_path.read_text(encoding="utf-8")
    # ?v=версия у статики — после обновления браузер не возьмёт старый JS из кэша
    return HTMLResponse(html.replace("__APP_VERSION__", APP_VERSION))


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


# ── Сводка «Режима спасения» для интерфейса ────────────────────────────────

RESCUE_SCHEMA = 1


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _files(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " " + _plural(n, "файл", "файла", "файлов")


def _ru_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return (f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.1f} {unit}").replace(".", ",")
        n /= 1024
    return f"{n:.1f} ТБ"


# Действия, которых ждёт программа от человека (id → тексты; кнопку рисует интерфейс)
_RESCUE_ACTIONS = {
    "setup": ("Сохраните свои файлы с Яндекс.Диска на компьютер",
              "Один раз дайте программе доступ к Диску и выберите папку — это 3–5 минут.",
              "Начать настройку"),
    "token": ("Яндекс перестал принимать ключ доступа к Диску",
              "Получите новый ключ — это минута. Скачанное не пропадёт, Безлимит продолжает качаться.",
              "Получить новый ключ"),
    "cookies": ("Нужно обновить доступ к Безлимиту",
                "Яндекс завершил вход, которым пользуется программа. Откройте disk.yandex.ru и "
                "скопируйте данные входа заново, как при настройке.",
                "Обновить доступ"),
    "free_space": ("На диске закончилось место",
                   "Освободите место или выберите папку на другом диске — продолжим с того же места.",
                   "Я освободил место — продолжить"),
    "send_logs": ("Что-то пошло не так",
                  "Скачайте отчёт для поддержки и пришлите его в Telegram — в нём нет ключа и паролей.",
                  "Скачать отчёт"),
}

# Спокойные пояснения: ничего делать не нужно
_RESCUE_NOTICES = {
    "storage_down": ("Часть файлов лежит на серверах Яндекса, которые сейчас не отвечают",
                     "Они не потеряны: программа повторяет их сама. Не удаляйте эти файлы с Диска и "
                     "не нажимайте в приложении Яндекса «Освободить место»."),
    "rate_limit": ("Яндекс просит обращаться реже",
                   "Программа делает паузы и продолжает сама. Ничего делать не нужно."),
    "server_50x": ("Яндекс отвечает с перебоями",
                   "Программа повторяет запросы сама. Ничего делать не нужно."),
    "timeout": ("Яндекс отвечает медленно",
                "Программа подождёт и повторит. Ничего делать не нужно."),
    "bulk_disabled": ("Яндекс сейчас отказывает в скачивании части файлов",
                      "Программа ждёт с паузами и продолжит сама."),
}


def _source_block(name: str, db: dict, branch: dict | None, configured: bool, folder: str,
                  local: dict | None, window_sec: float | None) -> dict:
    db = db or {}
    total = int(db.get("total") or 0)
    excluded = int(db.get("category_skipped") or 0)
    deferred = int(db.get("deferred") or 0)
    pending_all = int(db.get("pending") or 0)
    files = {
        "total": total,
        "selected": max(0, total - excluded),
        "done": int(db.get("downloaded") or 0) + max(0, int(db.get("skipped") or 0) - excluded),
        "pending": max(0, pending_all - deferred),
        "in_progress": int(db.get("in_progress") or 0),
        "deferred": deferred,
        "failed": int(db.get("failed") or 0),
        "excluded": excluded,
        "preview_only": int(db.get("previews") or 0),
    }
    bytes_ = {
        "total": int(db.get("bytes_total") or 0),
        "selected": int(db.get("bytes_selected_total") or 0),
        "done": int(db.get("bytes_done") or 0),
        "pending": max(0, int(db.get("bytes_pending") or 0) - int(db.get("bytes_deferred") or 0)),
        "in_progress": int(db.get("bytes_in_progress") or 0),
        "deferred": int(db.get("bytes_deferred") or 0),
        "failed": int(db.get("bytes_failed") or 0),
        "excluded": int(db.get("category_skipped_bytes") or 0),
    }
    # Скорость и «осталось» — только по честной выборке: первые минуты Диск качает
    # крошечные файлы (очередь идёт от маленьких к большим), и прогноз был бы бредом
    speed = None
    if window_sec and window_sec >= 120 and (db.get("bytes_10m") or 0) >= 20 * 1024 * 1024:
        speed = float(db["bytes_10m"]) / min(600.0, window_sec)
    left = bytes_["pending"] + bytes_["in_progress"]
    eta = (left / speed) if speed and left else None
    if eta is not None and eta > 14 * 86400:
        eta = None
    branch = branch or {}
    state = branch.get("state") or ("idle" if configured else "off")
    if state == "off" and configured:
        state = "idle"
    return {
        "configured": configured,
        "enabled": configured and branch.get("state") != "off",
        "state": state,
        "activity": branch.get("activity") or "",
        "reason": branch.get("reason") or "",
        "action": branch.get("action"),
        "round": branch.get("round") or 0,
        "next_retry_at": branch.get("next_retry_at"),
        "last_exit": branch.get("last_exit"),
        "files": files,
        "bytes": bytes_,
        "storage_hosts_down": int(db.get("deferred_hosts") or 0),
        "speed_bps": speed,
        "eta_sec": eta,
        "folder": {"path": folder, "size_bytes": int((local or {}).get("bytes") or 0)},
    }


def build_rescue_status(rescue_st: dict, disk_db: dict, unlim_db: dict, token: dict, cookies: dict,
                        cfg: dict, diag: dict | None = None, free_space: dict | None = None,
                        disk_local: dict | None = None, unlim_local: dict | None = None,
                        now: float | None = None) -> dict:
    """Всё, что нужно главному экрану, одним объектом: без разбора логов во фронтенде.

    Чистая функция от уже собранных данных — её легко проверить тестом.
    """
    now = now or time.time()
    branches = rescue_st.get("branches") or {}
    running = bool(rescue_st.get("running"))
    started = rescue_st.get("started_at")
    window = (now - started) if (running and started) else None
    download_dir = cfg.get("download_dir", "")
    token_ok = bool(token.get("ok") or token.get("unreachable"))
    sources = {
        "disk": _source_block("disk", disk_db, branches.get("disk") if started else None, token_ok,
                              download_dir, disk_local, window),
        "unlim": _source_block("unlim", unlim_db, branches.get("unlim") if started else None,
                               bool(cookies.get("ok")), os.path.join(download_dir, "_unlim") if download_dir else "",
                               unlim_local, window),
    }
    active = [s for s in sources.values() if s["enabled"]]

    # Состояние главного экрана
    branch_states = {s["state"] for s in active}
    if running:
        if "needs_action" in branch_states or "error" in branch_states:
            state = "needs_action"
        elif branch_states and branch_states <= {"waiting", "done", "off"} and "waiting" in branch_states:
            state = "waiting"
        else:
            state = "running"
    elif not started:
        state = "idle"
    else:
        state = {"done": "done", "failed": "needs_action", "stopped": "stopped"}.get(rescue_st.get("state"), "idle")

    # Что нужно от человека
    action_id, action_source = None, None
    for name in ("disk", "unlim"):
        a = sources[name].get("action")
        if a and sources[name]["state"] in ("needs_action", "error"):
            action_id, action_source = a, name
            break
    if action_id is None and state == "needs_action":
        action_id, action_source = "send_logs", "all"
    if not running and not (token_ok or cookies.get("ok")):
        action_id, action_source = "setup", "all"
    action = None
    if action_id in _RESCUE_ACTIONS:
        title, text, button = _RESCUE_ACTIONS[action_id]
        if action_id == "free_space" and free_space:
            text = f"Свободно {_ru_bytes(free_space.get('free', 0))}. {text}"
        action = {"id": action_id, "source": action_source, "title": title, "text": text, "button": button}

    # Спокойное пояснение
    notice = None
    if not action:
        kind = "storage_down" if any(s["files"]["deferred"] for s in active) else (diag or {}).get("kind")
        if kind in _RESCUE_NOTICES:
            title, text = _RESCUE_NOTICES[kind]
            notice = {"kind": kind, "title": title, "text": text}

    totals = {k: sum(s["files"][k] for s in active) for k in
              ("selected", "done", "pending", "in_progress", "deferred", "failed", "preview_only")}
    done_bytes = sum(s["bytes"]["done"] for s in active)
    next_retry = rescue_st.get("next_retry_at")
    complete = bool(active) and not (totals["pending"] or totals["in_progress"]
                                     or totals["deferred"] or totals["failed"])

    if action and action_id == "setup":
        headline, subline = action["title"], action["text"]
    elif state == "idle":
        what = [n for n, s in (("Диск", sources["disk"]), ("Безлимит", sources["unlim"])) if s["configured"]]
        headline = "Всё готово к скачиванию"
        subline = f"Скачаем {' и '.join(what) or 'ваши файлы'} в папку {download_dir}"
    elif state == "running":
        headline = "Скачиваем ваши файлы"
        subline = (f"Готово {totals['done']:,} из {_files(totals['selected'])}".replace(",", " ")
                   + ". Не закрывайте чёрное окно программы — компьютер сам не уснёт, пока идёт скачивание.")
    elif state == "waiting":
        headline = "Всё, что Яндекс отдаёт сейчас, уже скачано"
        previews = (f" Для {_files(totals['preview_only'])} уже сохранены превью — уменьшенные копии."
                    if totals["preview_only"] else "")
        subline = (f"Ждут Яндекса: {_files(totals['deferred'])}. Следующая попытка в {_hhmm(next_retry)}."
                   f"{previews} Оставьте программу открытой.")
    elif state == "needs_action" and action and running:
        # Действие — в баннере над карточкой; здесь — что продолжает качаться
        headline = "Скачиваем ваши файлы"
        subline = (f"Готово {totals['done']:,} из {_files(totals['selected'])}".replace(",", " ")
                   + ". Чтобы скачать всё, нужно ваше действие — подробности выше.")
    elif state == "needs_action" and action:
        headline, subline = action["title"], action["text"]
    elif state == "done":
        if complete:
            headline = "Готово! Все ваши файлы сохранены"
            subline = f"{_files(totals['done'])}, {_ru_bytes(done_bytes)} — в папке {download_dir}"
        else:
            headline = "Скачано почти всё"
            subline = (f"Сохранено {_files(totals['done'])}. Не скачались: {_files(totals['failed'])}"
                       + (f", ждут Яндекса: {_files(totals['deferred'])}" if totals["deferred"] else ""))
    elif state == "stopped":
        headline = "Скачивание на паузе"
        subline = "Уже скачанное сохранено. «Продолжить» докачает остальное без повторов."
    else:
        headline, subline = "Скачиваем ваши файлы", ""

    return {
        "schema": RESCUE_SCHEMA,
        "state": state,
        "running": running,
        "headline": headline,
        "subline": subline,
        "started_at": started,
        "finished_at": None if running else rescue_st.get("finished_at"),
        "next_retry_at": next_retry,
        "can_retry_now": any(s["state"] == "waiting" for s in active),
        "action": action,
        "notice": notice,
        "sources": sources,
        "summary": {
            "complete": complete,
            "downloaded_files": totals["done"],
            "downloaded_bytes": done_bytes,
            "deferred_files": totals["deferred"],
            "failed_files": totals["failed"],
            "preview_only_files": totals["preview_only"],
            "skipped_sources": [n for n, s in sources.items() if not s["configured"]],
            "photo_unlim_bytes": int((token.get("account") or {}).get("photo_unlim") or 0),
        },
    }


@app.get("/api/status")
def api_status(demo: str | None = None):
    cfg = get_config()
    safe_config = {
        "download_dir": cfg.get("download_dir", ""),
        "download_dir_set": cfg.get("download_dir_set", False),
        "max_workers": cfg.get("max_workers", 4),
        "token_masked": mask_token(cfg.get("token", "")),
    }
    token = token_snapshot(cfg["token"])
    cookies = check_cookies()
    disk_db = db_stats(DB_FILE, "files", "remote_path")
    unlim_db = db_stats(UNLIM_DB_FILE, "unlim_files", "file_id")
    pipe_st = pipeline.status()
    free = _free_space(cfg["download_dir"])
    demo_data = demo_rescue(demo, cfg) if demo in DEMO_STATES and os.environ.get("YADISK_DEMO") == "1" else None
    if demo_data:
        token, cookies = demo_data["token"], demo_data["cookies"]
        disk_db, unlim_db = demo_data["disk_db"], demo_data["unlim_db"]
        free = demo_data["free_space"]
    diag = diagnose_tasks()
    disk_local = local_size(cfg["download_dir"])
    unlim_local = local_size(os.path.join(cfg["download_dir"], "_unlim"))
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
            "local": disk_local,
        },
        "unlim": {
            "db": unlim_db,
            "local": unlim_local,
        },
        "tasks": tm.status(),
        "pipeline": pipe_st,
        "rescue": demo_data["rescue"] if demo_data else build_rescue_status(
            pipe_st, disk_db, unlim_db, token, cookies, cfg, diag, free, disk_local, unlim_local),
        "settings": get_settings(),
        "demo": demo if demo_data else None,
        "diagnostics": diag,
        "yandex_health": health_monitor.snapshot() if health_monitor else None,
        "free_space": free,
        "news": NEWS_LINKS,
        "exit_codes": {str(code): text for code, text in _EXIT_EXPLAIN.items()},
    }


# ── Интерфейс 2.0: настройки, запуск, списки, папки, демо ──────────────────

_SOURCES = ("disk", "unlim")
# Почему файл не скачан — простыми словами (по префиксу ошибки «kind | …»)
_REASON_TEXT = (
    ("storage_down |", "Сервер Яндекса с этим файлом не отвечает — программа повторит сама"),
    ("server_50x |", "Сбой серверов Яндекса"),
    ("download_timeout |", "Яндекс отвечал слишком долго"),
    ("timeout |", "Яндекс отвечал слишком долго"),
    ("rate_limit |", "Яндекс просил обращаться реже"),
    ("auth_expired |", "Доступ устарел — обновите ключ или доступ к Безлимиту"),
    ("local_disk |", "Не удалось записать файл на ваш диск"),
    ("bad_file |", "Файл пришёл повреждённым"),
    ("http_error |", "Яндекс отказал в скачивании"),
    ("refused |", "Яндекс отказал в скачивании"),
    ("prepare_failed |", "Яндекс не выдал ссылку на файл"),
    ("pathnotfound", "Файла больше нет на Диске"),
)


def reason_text(error: str | None) -> str:
    low = (error or "").lower()
    for prefix, text in _REASON_TEXT:
        if low.startswith(prefix.lower()) or prefix.lower() in low[:40]:
            return text
    return "Другая ошибка — подробности в отчёте для поддержки" if low else ""


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def get_settings() -> dict:
    """Что и как качать — живёт в .env, чтобы пережить перезапуск программы."""
    get_config()  # перечитать .env
    raw_sources = (os.getenv("BACKUP_SOURCES", "") or "disk,unlim").split(",")
    sources = [s for s in _SOURCES if s in {x.strip() for x in raw_sources}] or list(_SOURCES)
    raw_cats = (os.getenv("BACKUP_CATEGORIES", "") or "").strip()
    categories = [c for c in (x.strip() for x in raw_cats.split(",")) if c in CATEGORY_LABELS] or None
    return {
        "sources": sources,
        "categories": categories,          # None — всё
        "gentle": _env_flag("GENTLE_MODE", False),
        "previews": previews_enabled(),
        "category_labels": dict(CATEGORY_LABELS),
    }


class SettingsPayload(BaseModel):
    sources: list[str] | None = None
    categories: list[str] | None = None
    all_categories: bool = False
    gentle: bool | None = None
    previews: bool | None = None


@app.get("/api/settings")
def api_get_settings():
    return get_settings()


@app.post("/api/settings")
def api_set_settings(p: SettingsPayload):
    changes_what = p.sources is not None or p.categories is not None or p.all_categories
    if changes_what and pipeline.is_running():
        raise HTTPException(409, "Сначала остановите скачивание — потом можно изменить, что качать")
    updates: dict[str, str] = {}
    if p.sources is not None:
        sources = [s for s in _SOURCES if s in p.sources]
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
    _ensure_env_file()
    for key, value in updates.items():
        set_key(str(ENV_FILE), key, value, quote_mode="always")
        os.environ[key] = value
    log.info("настройки сохранены: %s", ", ".join(f"{k}={v}" for k, v in updates.items()) or "без изменений")
    return {"ok": True, "settings": get_settings()}


def _rescue_workers(settings: dict) -> tuple[int, int]:
    """Бережный режим — вдвое меньше одновременных скачиваний."""
    return (2, DEFAULT_UNLIM_BATCH) if settings.get("gentle") else (4, DEFAULT_UNLIM_BATCH)


@app.post("/api/rescue/start")
def api_rescue_start():
    """Главная кнопка «Сохранить мои файлы»: всё по сохранённым настройкам."""
    st = get_settings()
    do_disk, do_unlim = "disk" in st["sources"], "unlim" in st["sources"]
    if pipeline.is_running():
        return _resp(pipeline.start())  # идёт — значит «попробовать отложенные сейчас»
    src = preflight_sources(do_disk, do_unlim)
    if src["error"]:
        return {"ok": False, "msg": src["error"]}
    workers, batch = _rescue_workers(st)
    ok, msg = pipeline.start(workers=workers, batch=batch, do_disk=do_disk, do_unlim=do_unlim,
                             disk_categories=st["categories"], unlim_categories=st["categories"])
    return {"ok": ok, "msg": msg, "notes": src["notes"] if ok else []}


class RetryFailedPayload(BaseModel):
    source: str = "all"  # disk | unlim | all


@app.post("/api/rescue/retry_failed")
def api_rescue_retry_failed(p: RetryFailedPayload):
    """«Повторить для N файлов»: упавшие — снова в очередь, и сразу качать."""
    n = 0
    if p.source in ("disk", "all") and DB_FILE.exists():
        c = sqlite3.connect(DB_FILE, timeout=30)
        try:
            with sqlite_tx(c):
                n += c.execute("UPDATE files SET status='pending', error=NULL WHERE status='failed'").rowcount
        except sqlite3.Error as e:
            return {"ok": False, "msg": f"База Диска занята: {e}"}
        finally:
            c.close()
    if p.source in ("unlim", "all") and UNLIM_DB_FILE.exists():
        n += reset_failed_files(str(UNLIM_DB_FILE))
    if pipeline.is_running():
        pipeline.retry_now()
        return {"ok": True, "msg": f"Вернул в очередь {n} файлов — уже качаю", "count": n}
    res = api_rescue_start()
    res["count"] = n
    if res.get("ok"):
        res["msg"] = f"Вернул в очередь {n} файлов и запустил скачивание"
    return res


def _list_problem_files(db_file: Path, table: str, id_col: str, limit: int) -> dict:
    """Нескачанные файлы: упавшие и отложенные, с причиной простыми словами."""
    table = _validate_sql_ident(table, _ALLOWED_TABLES, "table")
    id_col = _validate_sql_ident(id_col, _ALLOWED_COLUMNS, "column")
    out: dict = {"items": [], "reasons": {}, "total": 0}
    if not db_file.exists():
        return out
    c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        preview_col = "preview_path" if "preview_path" in cols else "NULL AS preview_path"
        where = ("status='failed' OR (status='pending' AND COALESCE(error, '') LIKE ?)")
        for r in c.execute(f"SELECT error, COUNT(*) AS n FROM {table} WHERE {where} GROUP BY substr(error, 1, 20)",
                           (DEFERRED_MARKER + "%",)):
            text = reason_text(r["error"])
            out["reasons"][text] = out["reasons"].get(text, 0) + r["n"]
            out["total"] += r["n"]
        for r in c.execute(
            f"SELECT {id_col} AS path, size, status, error, {preview_col} FROM {table} WHERE {where} "
            f"ORDER BY status DESC, {id_col} LIMIT ?",
            (DEFERRED_MARKER + "%", limit),
        ):
            out["items"].append({
                "path": r["path"], "name": os.path.basename(str(r["path"]).rstrip("/")),
                "size": r["size"] or 0,
                "state": "deferred" if r["status"] == "pending" else "failed",
                "reason": reason_text(r["error"]),
                "preview": bool(r["preview_path"]),
            })
    except sqlite3.Error as e:
        out["error"] = str(e)
    finally:
        c.close()
    return out


@app.get("/api/{source}/failed")
def api_problem_files(source: str, limit: int = 200):
    limit = max(1, min(limit, 2000))
    if source == "disk":
        return _list_problem_files(DB_FILE, "files", "remote_path", limit)
    if source == "unlim":
        return _list_problem_files(UNLIM_DB_FILE, "unlim_files", "file_id", limit)
    raise HTTPException(404, "Нет такого источника")


class OpenFolderPayload(BaseModel):
    which: str = "download"  # download | unlim | previews


@app.post("/api/open_folder")
def api_open_folder(p: OpenFolderPayload):
    """Открывает папку скачивания в проводнике. Только настроенную — никаких чужих путей."""
    base = Path(get_config()["download_dir"])
    sub = {"download": "", "unlim": "_unlim", "previews": PREVIEW_DIRNAME}.get(p.which)
    if sub is None:
        raise HTTPException(400, "Неизвестная папка")
    path = base / sub if sub else base
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


# ── Демо-режим: пройти все состояния главного экрана без настоящего Яндекса ──

DEMO_STATES = ("setup", "ready", "downloading", "waiting", "token", "cookies",
               "disk_full", "done", "done_partial", "stopped")


def demo_rescue(kind: str, cfg: dict) -> dict:
    gb = 1024 ** 3

    def db(total, done, pending=0, deferred=0, failed=0, previews=0, size=6 * gb, speed=0):
        st = _empty_db_stats()
        st.update(total=total, downloaded=done, pending=pending + deferred, deferred=deferred,
                  failed=failed, previews=previews, bytes_total=size,
                  bytes_done=int(size * done / max(total, 1)),
                  bytes_pending=int(size * (pending + deferred) / max(total, 1)),
                  bytes_deferred=int(size * deferred / max(total, 1)),
                  bytes_selected_total=size, bytes_10m=speed, deferred_hosts=deferred // 40)
        return st

    now = time.time()
    tok = {"ok": True, "login": "demo", "account": {"photo_unlim": 69 * gb}}
    coo = {"ok": True}
    st: dict = {"running": False, "started_at": None, "state": "idle", "next_retry_at": None,
                "branches": {"disk": {"state": "off"}, "unlim": {"state": "off"}}}
    disk, unlim = db(6015, 0, pending=6015), db(13602, 0, pending=13602, size=69 * gb)
    free = {"free": 120 * gb}

    def run(d, u, state="running", running=True, next_retry=None):
        return {"running": running, "started_at": now - 1800, "finished_at": None if running else now,
                "state": state, "next_retry_at": next_retry, "branches": {"disk": d, "unlim": u}}

    if kind == "setup":
        tok, coo = {"ok": False}, {"ok": False}
    elif kind == "downloading":
        st = run({"state": "downloading", "activity": "скачиваю файлы"},
                 {"state": "downloading", "activity": "скачиваю файлы"})
        disk = db(6015, 2100, pending=3500, deferred=415, speed=420 * 1024 ** 2)
        unlim = db(13602, 1900, pending=10900, deferred=802, previews=650, size=69 * gb, speed=380 * 1024 ** 2)
    elif kind == "waiting":
        st = run({"state": "waiting", "next_retry_at": now + 1500}, {"state": "waiting", "next_retry_at": now + 1500},
                 next_retry=now + 1500)
        disk = db(6015, 1203, deferred=4812, previews=4100)
        unlim = db(13602, 2721, deferred=10881, previews=9500, size=69 * gb)
    elif kind == "token":
        st = run({"state": "needs_action", "action": "token"}, {"state": "downloading", "activity": "скачиваю файлы"})
        disk, unlim = db(6015, 800, pending=5215), db(13602, 3000, pending=10602, size=69 * gb)
    elif kind == "cookies":
        st = run({"state": "downloading", "activity": "скачиваю файлы"}, {"state": "needs_action", "action": "cookies"})
        disk, unlim = db(6015, 2500, pending=3515), db(13602, 400, pending=13202, size=69 * gb)
    elif kind == "disk_full":
        st = run({"state": "needs_action", "action": "free_space"}, {"state": "needs_action", "action": "free_space"},
                 state="failed", running=False)
        free = {"free": 120 * 1024 ** 2}
        disk, unlim = db(6015, 3000, pending=3015), db(13602, 5000, pending=8602, size=69 * gb)
    elif kind == "done":
        st = run({"state": "done"}, {"state": "done"}, state="done", running=False)
        disk, unlim = db(6015, 6015), db(13602, 13602, size=69 * gb)
    elif kind == "done_partial":
        st = run({"state": "done"}, {"state": "done"}, state="done", running=False)
        disk, unlim = db(6015, 5995, failed=20), db(13602, 13590, failed=12, size=69 * gb)
    elif kind == "stopped":
        st = run({"state": "stopped"}, {"state": "stopped"}, state="stopped", running=False)
        disk, unlim = db(6015, 1500, pending=4515), db(13602, 2000, pending=11602, size=69 * gb)
    rescue_st = build_rescue_status(st, disk, unlim, tok, coo, cfg, {"kind": None}, free,
                                    {"bytes": 31 * gb}, {"bytes": 12 * gb})
    return {"rescue": rescue_st, "token": tok, "cookies": coo, "disk_db": disk, "unlim_db": unlim,
            "free_space": dict(free, total=2048 * gb, path=cfg.get("download_dir", ""))}


# ── Проверка токена без блокировки /api/status ─────────────────────────────

_token_refreshing = threading.Event()


def token_snapshot(token: str) -> dict:
    """Последняя проверка токена сразу, свежая — в фоне.

    /api/status опрашивается раз в 1,5 с: ждать ответа Яндекса там нельзя — во время
    сбоев проверка висит минутами, и весь интерфейс замирает.
    """
    cached = _token_check_cache
    if cached["token"] == token and cached["data"] is not None:
        if time.time() - cached["at"] >= _TOKEN_TTL and not _token_refreshing.is_set():
            _token_refreshing.set()

            def refresh():
                try:
                    check_token(token)
                finally:
                    _token_refreshing.clear()

            threading.Thread(target=refresh, daemon=True).start()
        return cached["data"]
    return check_token(token)  # первый раз — синхронно


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
    # None — всё. Раньше по умолчанию были только фото и видео, и мастер на пустой базе
    # молча оставлял за бортом документы
    disk_categories: list[str] | None = None
    unlim_categories: list[str] | None = None
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


@app.post("/api/rescue/retry_now")
def api_rescue_retry_now():
    """«Попробовать сейчас»: не ждать раунда, повторить отложенные файлы немедленно."""
    return _resp(pipeline.retry_now())


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
    if pipeline.is_running():
        # Раунды спасения качают в одну папку; смена посреди них расколола бы бэкап
        raise HTTPException(409, "Сначала остановите скачивание — потом можно сменить папку")
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
    # И во время ожидания раунда: следующий раунд скачал бы разложенные файлы заново
    if payload.action == "move" and (pipeline.is_running() or tm.any_alive(SLOT_DISK)
                                     or tm.any_alive(UNLIM_SLOT_PREFIX)):
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


# Windows берёт MIME из реестра, и там .js бывает text/plain — браузер такой скрипт не выполнит
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
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
