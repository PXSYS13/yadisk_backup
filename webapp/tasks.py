"""Фоновые процессы: CLI-скрипты в именованных слотах и их журналы.

Слот — логическое имя задачи ('disk', 'unlim_collect', 'unlim_w1', …). По этим именам
фронтенд находит логи и состояние в /api/status.tasks, поэтому они — часть API.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from utils import UNDER_WEBUI_ENV
from webapp import paths

log = logging.getLogger("webui")

# Слоты фоновых задач — по этим именам фронтенд находит логи и статус
SLOT_DISK = "disk"
SLOT_UNLIM_COLLECT = "unlim_collect"
SLOT_ORGANIZE = "organize"
UNLIM_WORKER_SLOT_PREFIX = "unlim_w"  # unlim_w1, unlim_w2, ...
UNLIM_SLOT_PREFIX = "unlim"  # collect + все воркеры
SLOT_LINKS = "links"  # скачивание по публичным ссылкам — живёт отдельно от спасения
# Проверка скачанного идёт в слоте своего источника: пока она читает файлы, скачивание
# туда же не запускается (main.py verify — в disk, unlim_download.py --verify — в unlim_collect)
VERIFY_KINDS = ("disk_verify", "unlim_verify")

# Диагностика смотрит только задачи, которые идут или закончились за последние 10 минут
DIAG_RECENT_SEC = 600
# Лог задачи дописывается при каждом запуске слота; при превышении — уходит в .1
TASK_LOG_MAX_BYTES = 10 * 1024 * 1024


def task_kind(cmd: list[str]) -> str:
    """Что делает задача — по её команде (слот disk делят scan, download и verify)."""
    script = os.path.basename(cmd[0]) if cmd else ""
    first = cmd[1] if len(cmd) > 1 else ""
    if script == "main.py":
        return "disk_scan" if first == "scan" else "disk_download" if first in (
            "download", "retry-failed") else f"disk_{first or 'other'}"
    if script == "unlim_download.py" and "--verify" in cmd:
        return "unlim_verify"
    return {
        "unlim_download.py": "unlim_collect",
        "unlim_worker.py": "unlim_worker",
        "organize.py": "organize",
        "public_download.py": "links",
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
        # Когда процесс последний раз что-то напечатал — интерфейс показывает, что он жив
        self.last_line_at: float | None = None
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
                    self.last_line_at = time.time()
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
            "last_line_at": self.last_line_at,
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
            env[UNDER_WEBUI_ENV] = "1"  # консольные подсказки скриптам здесь не нужны
            cmd = [sys.executable, "-u", script] + args
            proc = subprocess.Popen(
                cmd, cwd=str(paths.PROJECT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env,
            )
            log.info("задача %s запущена: %s %s (pid %s)", slot, script, " ".join(args), proc.pid)
            self._tasks[slot] = Task(slot, proc, log_file=paths.TASK_LOG_DIR / f"{slot}.log",
                                     cmd=[script, *args])
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

    def any_alive(self, prefix: str) -> bool:
        with self._lock:
            return any(slot.startswith(prefix) and t.alive() for slot, t in self._tasks.items())

    def status(self) -> dict:
        with self._lock:
            return {slot: t.snapshot() for slot, t in self._tasks.items()}

    def recent_log_lines(self, per_task: int = 40, recent_sec: float = DIAG_RECENT_SEC,
                         exclude: tuple[str, ...] = (), with_slots: bool = False,
                         exclude_kinds: tuple[str, ...] = ()) -> list:
        """Хвосты логов живых задач и задач, закончившихся не раньше recent_sec назад.

        Старые задачи не учитываем: исправленная проблема не должна висеть баннером.
        exclude — слоты, чьи строки к этим объяснениям не относятся; exclude_kinds — то же
        по виду задачи (проверка скачанного делит слот со скачиванием).
        with_slots — пары (слот, строка): часть правил диагностики касается одного источника.
        """
        now = time.time()
        with self._lock:
            lines: list = []
            for slot, t in self._tasks.items():
                if slot in exclude or getattr(t, "kind", None) in exclude_kinds:
                    continue
                if t.alive() or (t.finished_at is not None and now - t.finished_at <= recent_sec):
                    tail = list(t.log)[-per_task:]
                    lines.extend([(slot, ln) for ln in tail] if with_slots else tail)
            return lines


tm = TaskManager()


def organize_moving() -> bool:
    """Идёт сортировка с переносом файлов: качать и менять папку сейчас нельзя.

    Файлы уезжают из-под скачивания: база помнит прежние места, и следующий раунд скачал бы
    разложенные файлы заново (или положил новый файл туда, куда сортировка уже перенесла другой).
    """
    t = tm.get(SLOT_ORGANIZE)
    return t is not None and t.alive() and "move" in t.cmd


def stop_and_wait(tasks: list[Task]) -> None:
    """Дожидается остановленных задач и их reader-потоков."""
    for t in tasks:
        t.join(timeout=2.0)
