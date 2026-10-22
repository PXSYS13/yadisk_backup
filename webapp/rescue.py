"""«Режим спасения»: Диск и Безлимит качаются параллельно, недоступное — раундами.

Ветка Диска: scan → download (main.py), ветка Безлимита: collect → воркеры
(unlim_download.py, unlim_worker.py). Шаги — отдельные процессы в слотах tasks.tm,
решения — по их кодам выхода (utils.EXIT_*) и по счётчикам в базах.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from state import SELECTION_MARKERS
from unlim_common import clear_dead_hosts, reset_failed_files
from utils import (
    CATEGORY_LABELS,
    DEFAULT_UNLIM_BATCH,
    EXIT_AUTH,
    EXIT_DEFERRED,
    EXIT_DISK_FULL,
    EXIT_INTERRUPTED,
    EXIT_OUTAGE,
    EXIT_PARTIAL,
    MAX_DISK_WORKERS,
    MAX_UNLIM_BATCH,
    MAX_UNLIM_WORKERS,
    TELEGRAM_CHANNEL,
    classify_yandex_error,
    deferred_round_text,
    files_text,
    hhmm,
    keep_awake,
    plural,
    retry_every_sec,
)
from webapp import access, config, paths
from webapp.folders import FOLDERS_SUMMARY_SEC, disk_folders_summary
from webapp.listing import disk_listing_plan, listing_stale, read_listing, write_listing
from webapp.local_disk import LOW_FREE_BYTES, free_space
from webapp.queue_db import (
    db_has_pending,
    db_has_rows,
    db_stats,
    filter_to_categories,
    filter_to_folders,
    release_orphaned_unlim,
    reset_transient_disk_failures,
    unlim_has_rows_outside,
    unskip_category_marker,
    unskip_markers,
)
from webapp.tasks import (
    SLOT_DISK,
    SLOT_UNLIM_COLLECT,
    UNLIM_SLOT_PREFIX,
    UNLIM_WORKER_SLOT_PREFIX,
    stop_and_wait,
    tm,
)
from webapp.texts import folder_label, folders_text, when_text

log = logging.getLogger("webui")

# Столько раундов подряд с непонятным кодом выхода — дальше просим прислать отчёт
RESCUE_STUCK_ROUNDS = 3
# Процесс скачивания оборвался, не дойдя до конца очереди, — повтор через минуту, а не через полчаса
RESCUE_CRASH_RETRY_SEC = 60
# Подготовка ветки остановилась и ждёт человека (например, выбранных папок больше нет)
PREPARE_NEEDS_ACTION = -2
SOURCE_LABEL = {"disk": "Диск", "unlim": "Безлимит"}
BRANCH_STATE_TEXT = {
    "checking": "проверяю доступ", "listing": "получаю список файлов", "downloading": "скачиваю",
    "waiting": "ждём Яндекса", "done": "готово", "needs_action": "нужно ваше действие",
    "stopped": "остановлено", "error": "ошибка",
}


def launch_unlim_workers(
    n: int,
    batch: int,
    starter: Callable[[str, str, list[str]], tuple[bool, str]] | None = None,
) -> dict:
    """Запускает n воркеров безлимита в слотах unlim_w1..unlim_wN.

    starter — чем запускать (по умолчанию tm.start; Rescue подставляет свой,
    чтобы помечать задачи как свои).
    """
    starter = starter or tm.start
    if not tm.any_alive(UNLIM_WORKER_SLOT_PREFIX):
        release_orphaned_unlim()
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


class Branch:
    """Одна ветка спасения: «disk» (scan → download) или «unlim» (collect → воркеры)."""

    def __init__(self, name: str):
        self.name = name
        self.thread: threading.Thread | None = None
        self.wake = threading.Event()
        self.reset()

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        # Когда начался текущий этап — по нему считается «за последние N минут»
        if value != getattr(self, "_state", None):
            self._state = value
            self.state_since = time.time()

    def reset(self) -> None:
        self.state = "off"  # off|checking|listing|downloading|waiting|done|needs_action|stopped|error
        self.action: str | None = None  # token | cookies | free_space | send_logs
        self.reason = ""                # итог простыми словами
        self.activity = ""              # что делается прямо сейчас
        self.round = 0
        self.next_retry_at: float | None = None
        self.wait_kind: str | None = None  # deferred | outage | crash — чего ждёт ветка
        self.last_exit: dict | None = None
        self.odd_rounds = 0
        self.reset_done = False
        self.relist = False  # «Обновить список»: перед следующим скачиванием собрать список заново
        # Список устарел, но по нему ещё есть что качать: сначала качаем, после шага собираем заново
        self.list_after = False
        self.requeued = False  # «Повторить ошибки» во время шага: после него сразу новый раунд
        self.wake.clear()

    @property
    def label(self) -> str:
        return SOURCE_LABEL[self.name]

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def snapshot(self) -> dict:
        return {
            "state": self.state, "state_since": self.state_since, "action": self.action, "reason": self.reason,
            "activity": self.activity, "round": self.round,
            "next_retry_at": self.next_retry_at, "wait_kind": self.wait_kind, "last_exit": self.last_exit,
            "running": self.alive(),
        }


class Rescue:
    """«Режим спасения»: Диск и Безлимит качаются параллельно, недоступное — раундами.

    Ветка проходит очередь до конца. Если остались файлы на молчащих серверах Яндекса
    (код 5) или Яндекс не отвечает (код 3), она ждёт retry_every секунд или кнопку
    «Попробовать сейчас» и качает снова — без повторного сбора списка. Код 2 ставит
    ветку в «нужно действие» (новый ключ или доступ), код 4 — обе ветки (нет места).

    status() сохраняет прежний формат Pipeline (state/step/log): это /api/status.pipeline,
    из него интерфейс берёт running, а build_rescue_status — состояние для Главной.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self.branches = {"disk": Branch("disk"), "unlim": Branch("unlim")}
        self.log: deque[str] = deque(maxlen=200)
        # Лента «Что происходит» на Главной: простые фразы без кодов и жаргона
        self.events: deque[dict] = deque(maxlen=60)
        self.state = "idle"  # idle | running | done | failed | stopped
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.workers = 4
        self.batch = DEFAULT_UNLIM_BATCH
        self.skip_disk = False
        self.skip_unlim = False
        self.disk_categories: list[str] | None = None
        self.unlim_categories: list[str] | None = None
        self.disk_folders: list[str] | None = None  # None — весь Диск
        self.partial = False
        self.retry_every = retry_every_sec()
        self._owned_slots: set[str] = set()
        # Главный поток уже дождался веток и заканчивает: перезапускать ветку поздно
        self._closing = False
        # Лента «Папка … скачана целиком»: какие папки верхнего уровня уже были скачаны
        # (None — ещё не считали) и когда считали последний раз
        self._folders_done: set[str] | None = None
        self._folders_at = 0.0
        # Источники, где человек нажал «Обновить список»: их список собирается до скачивания,
        # а не после первого шага (см. Branch.list_after)
        self._relist_asked: set[str] = set()

    # ── управление ──

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, workers: int = 4, do_disk: bool = True, do_unlim: bool = True,
              batch: int = DEFAULT_UNLIM_BATCH,
              disk_categories: list[str] | None = None,
              unlim_categories: list[str] | None = None,
              disk_folders: list[str] | None = None) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                # Повторный старт во время ожидания = «Попробовать сейчас»
                if self._wake_waiting():
                    return True, "Пробую отложенные файлы прямо сейчас"
                return False, "Спасение уже идёт"
            self._stop_evt.clear()
            self._owned_slots.clear()
            self._closing = False
            self.state = "running"
            self.log.clear()
            self.events.clear()
            self.started_at = time.time()
            self.finished_at = None
            self.workers = max(1, min(workers, MAX_UNLIM_WORKERS))
            self.batch = max(1, min(batch, MAX_UNLIM_BATCH))
            self.skip_disk = not do_disk
            self.skip_unlim = not do_unlim
            self.disk_categories = list(disk_categories) if disk_categories is not None else None
            self.unlim_categories = list(unlim_categories) if unlim_categories is not None else None
            self.disk_folders = list(disk_folders) if disk_folders else None
            self.partial = False
            self.retry_every = retry_every_sec()
            self._folders_done, self._folders_at = None, 0.0
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

    def relist(self, source: str) -> tuple[bool | None, str]:
        """«Обновить список»: забыть прошлый сбор списка источника и собрать его заново.

        None — спасение не идёт: вызывающий запускает его целиком, и ветка соберёт список.
        """
        self._forget_listing(source)
        with self._lock:
            self._relist_asked.add(source)
            if not self.is_running() or self._closing:
                return None, ""
            b = self.branches[source]
            if b.state == "off":
                return False, f"{b.label} сейчас не скачивается — проверьте доступ в «Настройках»"
            if b.state in ("checking", "listing"):
                return True, "Список и так собирается прямо сейчас"
            if b.state == "downloading":
                b.relist = True
                self._event(source, "info", "Обновлю список файлов, как только закончится текущее скачивание")
                return True, "Обновлю список, как только закончится текущее скачивание"
            self._event(source, "run", "Обновляю список файлов — по вашей просьбе")
            if b.alive():  # ждёт следующей попытки
                b.relist = True
                b.wake.set()
                return True, "Обновляю список файлов"
            # Ветка закончила (готово или нужно действие), а соседняя ещё работает — запускаем эту снова
            self._restart_branch(b)
            return True, "Обновляю список файлов"

    def requeue_now(self, source: str = "all") -> tuple[bool | None, str]:
        """«Повторить ошибки» во время спасения: файлы уже вернули в очередь — пусть их качают.

        Ждущая ветка просыпается, качающая возьмёт их после текущего шага, закончившая
        запускается снова. None — спасение не идёт или уже заканчивается: вызывающий
        запускает его целиком.
        """
        with self._lock:
            if not self.is_running() or self._closing:
                return None, ""
            texts = []
            for b in self.branches.values():
                if source not in ("all", b.name) or b.state == "off":
                    continue
                if b.state == "waiting" and b.alive():
                    b.wake.set()
                    texts.append(f"{b.label} качает их сейчас")
                elif b.alive():
                    b.requeued = True
                    texts.append(f"{b.label} возьмёт их после текущего шага")
                elif b.state == "done":
                    self._restart_branch(b)
                    texts.append(f"{b.label} качает их сейчас")
            if not texts:
                return False, "Сейчас их некому качать: сначала сделайте то, о чём просит программа"
            self._event(None, "run", "Файлы, которые не скачались, снова в очереди — по вашей просьбе")
            return True, "; ".join(texts)

    def wait_finished(self, timeout: float) -> bool:
        """Ждёт конца главного потока (когда он уже заканчивает). True — закончился."""
        t = self._thread
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()

    def _restart_branch(self, b: Branch) -> None:
        """Ветка закончила, а спасение ещё идёт: запускаем её снова (вызывать под self._lock)."""
        b.reset()
        b.state = "checking"
        target = self._run_disk if b.name == "disk" else self._run_unlim
        b.thread = threading.Thread(target=self._branch_main, args=(b, target), daemon=True)
        b.thread.start()

    def _take_relist(self, source: str) -> bool:
        """Человек просил «Обновить список» этого источника (и просьба теперь исполняется)."""
        with self._lock:
            asked = source in self._relist_asked
            self._relist_asked.discard(source)
            return asked

    def _list_after(self, b: Branch, rec: dict | None) -> None:
        """Список устарел (или собран без записи о сборе — прежним интерфейсом), а по нему ещё
        есть что качать: сначала качаем, после шага собираем заново. Большой Диск Яндекс
        перечисляет часами, и всё это время скачивание стояло бы."""
        b.list_after = True
        what = "Список фото и видео" if b.name == "unlim" else "Список файлов"
        text = (f"{what} собран {when_text(rec['at'])} — сначала качаю по нему, потом обновлю его" if rec
                else "Сначала качаю по уже собранному списку файлов, потом обновлю его")
        self._log(f"📋 {b.label}: {text}")
        self._event(b.name, "info", text)

    def _forget_listing(self, source: str) -> None:
        try:
            write_listing(source, None)
        except sqlite3.Error as e:
            log.warning("не удалось забыть сбор списка %s: %s", source, e)

    def _remember_listing(self, source: str, rec: dict) -> None:
        try:
            write_listing(source, rec)
        except sqlite3.Error as e:  # не страшно: следующий запуск просто соберёт список заново
            log.warning("не удалось запомнить сбор списка %s: %s", source, e)

    def _wake_waiting(self) -> bool:
        waiting = [b for b in self.branches.values() if b.state == "waiting"]
        if waiting:
            clear_dead_hosts(str(paths.UNLIM_DB_FILE))
            self._log("🔁 Повторяю отложенные файлы по запросу")
            self._event(None, "run", "Пробую отложенные файлы прямо сейчас — по вашей просьбе")
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
        stop_and_wait(tm.stop_matching(SLOT_DISK) + tm.stop_matching(UNLIM_SLOT_PREFIX))
        if self._thread is not None:
            self._thread.join(timeout=10)
        release_orphaned_unlim()
        self._log("⏹ остановлено пользователем")
        self._event(None, "info", "Скачивание остановлено. Всё скачанное сохранено")
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
            "events": list(self.events),
        }

    def _step_text(self) -> str:
        parts = []
        for b in self.branches.values():
            if b.state == "off":
                continue
            text = BRANCH_STATE_TEXT.get(b.state, b.state)
            if b.state == "waiting" and b.next_retry_at:
                text += f" до {hhmm(b.next_retry_at)}"
            parts.append(f"{b.label}: {text}")
        return " · ".join(parts) or "—"

    # ── внутреннее ──

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"{ts}  {msg}")
        log.info("rescue: %s", msg)

    def _event(self, source: str | None, kind: str, text: str) -> None:
        """Событие для ленты на Главной. kind: run | ok | wait | warn | err | info."""
        self.events.append({"at": time.time(), "source": source, "kind": kind, "text": text})

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
            if slot == SLOT_DISK and self.branches["disk"].state == "downloading":
                self._watch_folders()
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

    def _task_lines(self, slot: str) -> list[str]:
        """Журнал задачи слота — после шага из него читаются находки сбора (тесты подменяют)."""
        task = tm.get(slot)
        return list(task.log) if task else []

    def _scan_findings(self) -> tuple[list[str], list[str]]:
        """(пропавшие выбранные папки, найденные общие папки) — из строк `kind | {json}`."""
        missing: list[str] = []
        shared: list[str] = []
        for line in self._task_lines(SLOT_DISK):
            for kind, target in (("folder_missing | ", missing), ("shared_folders | ", shared)):
                pos = line.find(kind)
                if pos == -1:
                    continue
                try:
                    data = json.loads(line[pos + len(kind):])
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                if kind.startswith("folder_missing") and isinstance(data.get("path"), str):
                    target.append(data["path"])
                elif isinstance(data.get("names"), list):
                    target.extend(str(n) for n in data["names"])
        return missing, shared

    def _apply_disk_selection(self) -> None:
        """Очередь Диска по выбору человека: сначала папки, потом типы файлов."""
        returned = unskip_markers(paths.DB_FILE, "files", SELECTION_MARKERS)
        folders, cats = self.disk_folders, self.disk_categories
        if not folders and cats is None:
            if returned:
                self._log(f"↩️ Диск: вернул в очередь {returned} файлов, снятых прошлым выбором")
                self._event("disk", "info", self._returned_text(returned))
            return
        skipped = 0
        if folders:
            n, _ = filter_to_folders(paths.DB_FILE, folders)
            skipped += n
        if cats is not None:
            n, _ = filter_to_categories(paths.DB_FILE, "files", "remote_path", cats)
            skipped += n
        kept = int(db_stats(paths.DB_FILE, "files", "remote_path").get("pending") or 0)
        what = []
        if folders:
            what.append(f"папки {folders_text(folders)}")
        if cats is not None:
            what.append(", ".join(CATEGORY_LABELS.get(c, c) for c in cats) or "ничего")
        self._log(f"🎯 Диск: качаем {'; '.join(what)} — в очереди {kept}, пропущено {skipped}")
        if folders:
            self._event("disk", "info", f"Качаю только выбранные папки: {folders_text(folders)}")

    @staticmethod
    def _returned_text(n: int) -> str:
        """Лента: в очередь вернулись файлы, снятые прежним выбором. Прежний интерфейс выбирал
        типы файлов сам, а Главная качает по «Настройкам», — люди видели скачок числа файлов."""
        return (f"Снова в очереди: {files_text(n)}, {plural(n, 'который', 'которые', 'которые')} раньше "
                f"не выбирали. Скачиваю всё, как отмечено в «Настройках»; если часть файлов не нужна, "
                f"выберите типы файлов там")

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
                self._event("unlim" if table == "unlim_files" else "disk", "info", self._returned_text(n))
        else:
            self._apply_categories(db_file, table, id_col, categories, label)

    # ── главный поток и ветки ──

    def _run(self) -> None:
        # Поток живёт всё время спасения — на Windows флаг «не спать» привязан к нему
        with keep_awake():
            self._run_awake()

    def _run_awake(self) -> None:
        try:
            cfg = config.get_config()
            self._event(None, "run", "Начинаю: проверяю доступ к Яндексу")
            tok: dict = {"ok": False}
            if not self.skip_disk:
                self.branches["disk"].state = "checking"
                self._log("🔑 Проверяю ключ доступа к Диску...")
                tok = access.check_token(cfg["token"])
                if tok.get("ok"):
                    self._log(f"✓ Ключ работает: {tok.get('login')}, {tok.get('used', 0) / 1e9:.1f} ГБ занято")
                    self._event("disk", "ok", "Ключ доступа к Диску работает")
                elif tok.get("unreachable"):
                    self._log("⚠️ Яндекс не ответил на проверку ключа — попробую качать, ключ, скорее всего, в порядке")
                    self._event("disk", "warn", "Яндекс не ответил на проверку ключа — всё равно пробую качать")
                else:
                    self._log(f"✗ Ключ: {tok.get('msg')}")
            cookies: dict = {"ok": False}
            if not self.skip_unlim:
                cookies = access.check_cookies()
                self._log(f"{'✓' if cookies.get('ok') else '✗'} Доступ к Безлимиту: {cookies.get('msg')}")
                if cookies.get("ok"):
                    self._event("unlim", "ok", "Доступ к Безлимиту работает")
            token_usable = bool(tok.get("ok") or tok.get("unreachable"))
            src = access.plan_sources(not self.skip_disk, not self.skip_unlim, token_usable,
                                      bool(cookies.get("ok")))
            for note in src["notes"]:
                self._log(note)
            if not self.skip_disk and not src["run_disk"] and not src["error"]:
                self._event("disk", "warn", "Обычный Диск пропускаю: нет рабочего ключа доступа")
            if not self.skip_unlim and not src["run_unlim"] and not src["error"]:
                self._event("unlim", "warn", "Безлимит пропускаю: доступ к нему не подключён")
            if src["error"]:
                self._log(f"❌ {src['error']}")
                self._event(None, "err", "Нечего скачивать: подключите Диск или Безлимит в «Настройках»")
                for b in self.branches.values():
                    b.state = "off"
                self.state = "failed"
                return
            self.partial = bool(src["notes"])

            for name, run, target in (("disk", src["run_disk"], self._run_disk),
                                      ("unlim", src["run_unlim"], self._run_unlim)):
                b = self.branches[name]
                if not run:
                    b.state = "off"
                    continue
                b.state = "checking"
                b.thread = threading.Thread(target=self._branch_main, args=(b, target), daemon=True)
                b.thread.start()
            self._join_branches()
            self.state = self._final_state()
            self._log({
                "done": "🏁 Готово, но часть источников пропущена (см. выше)" if self.partial else "🏆 ВСЁ ГОТОВО!",
                "failed": "⚠️ Нужно ваше действие — подробности выше",
                "stopped": "⏹ Спасение остановлено. Уже скачанное сохранено",
            }.get(self.state, self.state))
            if self.state == "failed":
                self._event(None, "err", "Скачивание приостановлено: нужно ваше действие — оно описано сверху")
        except Exception as e:
            diag = classify_yandex_error(e)
            if diag["is_yandex_server"]:
                self._log(f"⚠️ Сбой серверов Яндекса: {diag['title']}")
                self._log(f"   {diag['message']}")
                self._log(f"   Новости дата-центров Яндекса: {TELEGRAM_CHANNEL}")
                self._event(None, "err", f"Сбой серверов Яндекса: {diag['title']}")
            else:
                self._log(f"❌ Ошибка: {e}")
                self._event(None, "err", "Ошибка программы — скачайте отчёт для поддержки во вкладке «Помощь»")
            log.exception("rescue")
            self.state = "failed"
        finally:
            self.finished_at = time.time()

    def _join_branches(self) -> None:
        """Ждёт ветки — и ту, что «Обновить список» запустил заново, пока шла соседняя."""
        while True:
            with self._lock:
                alive = [b.thread for b in self.branches.values() if b.alive()]
                if not alive:
                    self._closing = True
                    return
            for t in alive:
                t.join()

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
            self._event(b.name, "err", "Ошибка программы — скачайте отчёт для поддержки во вкладке «Помощь»")

    def _drive(self, b: Branch, prepare: Callable[[Branch], int], download: Callable[[Branch], int],
               db: tuple[Path, str, str], list_late: Callable[[Branch], int]) -> None:
        """Общий цикл ветки: подготовить список → качать → ждать раунд, пока есть что качать.

        list_late — сбор списка, который prepare отложил (b.list_after) до конца первого шага
        скачивания: большой Диск Яндекс перечисляет часами, и всё это время скачивание стояло бы.
        """
        ready = False
        while not self._stopped():
            b.round += 1
            if b.relist:  # «Обновить список» во время ожидания
                b.relist = False
                b.list_after = False
                ready = False
            if not ready:
                code = prepare(b)
                if code == -1:
                    return self._branch_stopped(b)
                if code == PREPARE_NEEDS_ACTION:
                    return  # ветка уже ждёт человека — причина и кнопка выставлены
                if code != 0:
                    if not self._after_exit(b, code, db, step="prepare"):
                        return
                    continue
                ready = True
            before = db_stats(*db)
            code = download(b)
            if code == -1:
                return self._branch_stopped(b)
            if b.relist and code not in (EXIT_AUTH, EXIT_DISK_FULL):
                # «Обновить список» нажали во время скачивания — собираем список сразу, без паузы
                continue
            if b.list_after and code not in (EXIT_AUTH, EXIT_DISK_FULL):
                b.list_after = False
                untried = self._untried(db)
                code_list = list_late(b)
                if code_list == -1:
                    return self._branch_stopped(b)
                if code_list == PREPARE_NEEDS_ACTION:
                    return
                if code_list in (EXIT_AUTH, EXIT_DISK_FULL):
                    self._after_exit(b, code_list, db, step="prepare")
                    return
                if self._untried(db) > untried:
                    # В обновлённом списке есть новые файлы — их качаем сразу, а не через раунд
                    self._event(b.name, "run", "В обновлённом списке есть новые файлы — качаю их")
                    continue
            if not self._after_exit(b, code, db, before=before):
                return
        self._branch_stopped(b)

    @staticmethod
    def _untried(db: tuple[Path, str, str]) -> int:
        """Сколько файлов в очереди ещё ни разу не пробовали (без ждущих Яндекса)."""
        stats = db_stats(*db)
        return max(0, int(stats.get("pending") or 0) - int(stats.get("deferred") or 0))

    def _report_retry(self, b: Branch, before: dict, after: dict) -> None:
        """Лента: сколько из отложенных файлов отдал Яндекс за эту попытку.

        Люди видят, что доля удачных попыток падает, и спрашивают, сломалось ли что-то.
        Падает она потому, что живые файлы уже скачаны и в очереди остались те, чьи серверы
        молчат; по этим строкам от попытки к попытке видно, оживляет ли Яндекс серверы.
        """
        waited = int(before.get("deferred") or 0)
        untried = int(before.get("pending") or 0) - waited
        # Только чистый повтор: если в очереди были и новые файлы, их скачанные смешались бы с повтором
        if not waited or untried > 0 or "error" in after:
            return
        got = max(0, int(after.get("downloaded") or 0) - int(before.get("downloaded") or 0))
        text = deferred_round_text(waited, got)
        self._log(f"📈 {b.label}: {text}")
        self._event(b.name, "ok" if got else "info", text)

    def _branch_stopped(self, b: Branch) -> None:
        if b.state not in ("needs_action", "error"):
            b.state = "stopped"
        b.activity = ""
        b.next_retry_at = None

    def _after_exit(self, b: Branch, code: int, db: tuple[Path, str, str], step: str = "download",
                    before: dict | None = None) -> bool:
        """Что делать после шага с этим кодом. True — раунд отождан, качаем снова.

        step="prepare" — не удался сбор списка: из этого ветка никогда не становится «готово»,
        иначе пустая база после упавшего сбора выглядела бы как «все файлы сохранены».
        before — счётчики базы до скачивания: по ним в ленту уходит итог повтора отложенных.
        """
        if code == EXIT_AUTH:
            b.state = "needs_action"
            b.action = "token" if b.name == "disk" else "cookies"
            b.reason = ("Яндекс перестал принимать ключ доступа к Диску" if b.name == "disk"
                        else "Нужно обновить доступ к Безлимиту")
            self._log(f"🔑 {b.label}: {b.reason}. Остальное продолжает качаться")
            self._event(b.name, "err", f"{b.reason} — эта часть на паузе")
            return False
        if code == EXIT_DISK_FULL:
            self._halt_for_disk()
            return False
        if step == "prepare":
            return self._after_prepare_exit(b, code)
        stats = db_stats(*db)
        if before is not None:
            self._report_retry(b, before, stats)
        if b.requeued:
            # «Повторить ошибки» нажали во время скачивания: файлы уже в очереди — качаем их сразу,
            # а не считаем недошедший до конца очереди шаг обрывом
            b.requeued = False
            if code in (0, EXIT_PARTIAL, EXIT_DEFERRED):
                self._event(b.name, "run", "Файлы, которые не скачались, снова в очереди — качаю их")
                return True
        deferred = int(stats.get("deferred") or 0)
        # in_progress — пачки воркера, который упал или работает не из этого спасения: это не «готово»
        left = max(0, int(stats.get("pending") or 0) - deferred) + int(stats.get("in_progress") or 0)
        if code in (0, EXIT_PARTIAL) and left:
            # Шаг «закончился», а в очереди остались нетронутые файлы: процесс оборвался
            # (у Python код падения тоже 1). Это не «скачано почти всё»
            return self._step_interrupted(b, code, left, deferred)
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
            self._event(b.name, "ok", f"Готово. Не скачалось: {files_text(failed)}" if failed
                        else "Готово: все файлы сохранены")
            return False
        b.odd_rounds += 1
        self._log(f"⚠️ {b.label}: шаг завершился с кодом {code} ({b.odd_rounds}/{RESCUE_STUCK_ROUNDS})")
        if b.odd_rounds >= RESCUE_STUCK_ROUNDS:
            b.state = "needs_action"
            b.action = "send_logs"
            b.reason = "Что-то пошло не так — отправьте отчёт для поддержки"
            self._event(b.name, "err", "Не получается продолжить — скачайте отчёт для поддержки")
            return False
        self._event(b.name, "warn", "Что-то пошло не так — попробую ещё раз чуть позже")
        # Неизвестный код — процесс упал или его убили, а не «Яндекс не отвечает»
        return self._wait_round(b, deferred, outage=False, kind="crash",
                                delay=min(self.retry_every, RESCUE_CRASH_RETRY_SEC))

    def _after_prepare_exit(self, b: Branch, code: int) -> bool:
        """Сбор списка завершился с ошибкой (не доступ и не диск): ждём и собираем снова."""
        if code == EXIT_OUTAGE:
            b.odd_rounds = 0
            return self._wait_round(b, 0, outage=True, resume="Снова получаю список файлов")
        b.odd_rounds += 1
        self._log(f"⚠️ {b.label}: сбор списка не удался (код {code}) ({b.odd_rounds}/{RESCUE_STUCK_ROUNDS})")
        if b.odd_rounds >= RESCUE_STUCK_ROUNDS:
            b.state = "needs_action"
            b.action = "send_logs"
            b.reason = "Не получается получить список файлов — отправьте отчёт для поддержки"
            b.activity = ""
            self._event(b.name, "err", b.reason)
            return False
        self._event(b.name, "warn", "Не получилось получить список файлов — пробую ещё раз через минуту")
        return self._wait_round(b, 0, outage=False, kind="crash",
                                delay=min(self.retry_every, RESCUE_CRASH_RETRY_SEC),
                                reason="Сбор списка файлов прервался — пробую ещё раз",
                                resume="Снова получаю список файлов")

    def _wait_round(self, b: Branch, deferred: int, outage: bool, kind: str | None = None,
                    delay: float | None = None, reason: str | None = None,
                    resume: str | None = None) -> bool:
        """Ждёт следующий раунд. kind: deferred — файлы на молчащих серверах, outage —
        Яндекс не отвечает, crash — процесс оборвался (повтор раньше). reason — чем
        заменить стандартный текст причины, resume — слово в ленту при возобновлении."""
        kind = kind or ("deferred" if deferred and not outage else "outage")
        b.state = "waiting"
        b.wait_kind = kind
        b.next_retry_at = time.time() + (self.retry_every if delay is None else delay)
        at = hhmm(b.next_retry_at)
        b.reason = reason or {"deferred": f"Ждут Яндекса: {files_text(deferred)}",
                              "outage": "Яндекс сейчас не отвечает",
                              "crash": "Скачивание прервалось — пробую ещё раз"}[kind]
        b.activity = f"следующая попытка в {at}"
        self._log(f"⏳ {b.label}: {b.reason}. Следующая попытка в {at}")
        if kind == "deferred":
            self._event(b.name, "wait", f"Всё доступное скачано. Ждут Яндекса: {files_text(deferred)}. Повтор в {at}")
        elif kind == "outage":
            self._event(b.name, "wait", f"Яндекс сейчас не отвечает. Повтор в {at}")
        manual = False
        while not self._stopped():
            remaining = b.next_retry_at - time.time()
            if remaining <= 0:
                break
            if b.wake.wait(min(remaining, 1.0)):
                manual = True
                break
        b.wake.clear()
        b.next_retry_at = None
        b.wait_kind = None
        if self._stopped():
            self._branch_stopped(b)
            return False
        if not manual:
            self._event(b.name, "run", resume or ("Запускаю скачивание снова" if kind == "crash"
                                                  else "Пробую отложенные файлы снова"))
        return True

    def _step_interrupted(self, b: Branch, code: int, left: int, deferred: int) -> bool:
        """Шаг вернул 0/1, но не дошёл до конца очереди: короткая пауза и ещё раз."""
        b.odd_rounds += 1
        self._log(f"⚠️ {b.label}: скачивание прервалось (код {code}), не тронуто файлов: {left} "
                  f"({b.odd_rounds}/{RESCUE_STUCK_ROUNDS})")
        if b.odd_rounds >= RESCUE_STUCK_ROUNDS:
            b.state = "needs_action"
            b.action = "send_logs"
            b.reason = "Скачивание всё время прерывается — скачайте отчёт для поддержки"
            self._event(b.name, "err", b.reason)
            return False
        self._event(b.name, "warn", "Скачивание прервалось — пробую ещё раз через минуту")
        return self._wait_round(b, deferred, outage=False, kind="crash",
                                delay=min(self.retry_every, RESCUE_CRASH_RETRY_SEC))

    def _halt_for_disk(self) -> None:
        """Нет места или папка недоступна — обе ветки ждут человека: файлы всё равно не лягут."""
        free = (free_space(config.get_config()["download_dir"]) or {}).get("free")
        reason = ("На диске закончилось место" if free is not None and free < LOW_FREE_BYTES
                  else "Не получается записать файлы в папку")
        for x in self.branches.values():
            if x.state != "off":
                x.state = "needs_action"
                x.action = "free_space"
                x.reason = reason
                x.activity = ""
        self._log(f"💾 {reason} — останавливаю скачивание. Освободите место или выберите другую папку")
        self._event(None, "err", f"{reason} — скачивание на паузе")
        self._stop_evt.set()
        for x in self.branches.values():
            x.wake.set()
        stop_and_wait(tm.stop_matching(SLOT_DISK) + tm.stop_matching(UNLIM_SLOT_PREFIX))

    # ── Диск ──

    def _run_disk(self, b: Branch) -> None:
        self._drive(b, self._disk_prepare, self._disk_download, (paths.DB_FILE, "files", "remote_path"),
                    self._disk_list_late)

    def _disk_prepare(self, b: Branch) -> int:
        if not b.reset_done:
            b.reset_done = True
            n = reset_transient_disk_failures()
            if n:
                self._log(f"↩️ Диск: {n} файлов с временными ошибками снова в очереди")
                self._event("disk", "info", f"Снова в очереди: {files_text(n)}, которые раньше не скачались")
        explicit = self._take_relist("disk")
        folders = self.disk_folders
        rec = read_listing("disk")
        mode, roots = disk_listing_plan(folders, rec, time.time())
        if mode == "none":
            self._log(f"📋 Диск: список файлов собран {when_text(rec['at'])} — заново не собираю")
            self._event("disk", "info", f"Список файлов собран {when_text(rec['at'])} — заново не собираю")
        elif (not explicit and db_has_pending(paths.DB_FILE, "files")
              # Новые отмеченные папки собираем сразу: их файлов в очереди ещё нет
              and (rec is None or disk_listing_plan(folders, rec, rec["at"])[0] == "none")):
            self._list_after(b, rec)
        else:
            code = self._disk_list(b, mode, roots, rec)
            if code:
                return code
        self._apply_disk_selection()
        return 0

    def _disk_list_late(self, b: Branch) -> int:
        """Сбор списка, отложенный до конца первого шага скачивания (Branch.list_after)."""
        rec = read_listing("disk")
        mode, roots = disk_listing_plan(self.disk_folders, rec, time.time())
        if mode == "none":
            return 0
        code = self._disk_list(b, mode, roots, rec)
        if code == 0:
            self._apply_disk_selection()
        return code

    def _disk_list(self, b: Branch, mode: str, roots: list[str] | None, rec: dict | None) -> int:
        """Сбор списка Диска (main.py scan). 0 — можно качать: список получен или есть прежний."""
        folders = self.disk_folders
        b.state = "listing"
        if mode == "new":
            self._event("disk", "run", f"Получаю у Яндекса список файлов в новых папках: {folders_text(roots)}")
        elif roots:
            self._event("disk", "run", f"Получаю у Яндекса список файлов в папках {folders_text(roots)}")
        else:
            self._event("disk", "run", "Получаю у Яндекса список файлов")
        args = ["scan", *[f"--root={p}" for p in roots]] if roots else ["scan"]
        code = self._run_step(b, SLOT_DISK, "main.py", args, "получаю список файлов")
        if code == -1:
            return -1
        missing, shared = self._scan_findings() if code == 0 else ([], [])
        if folders and missing and set(folders) <= set(missing):
            b.state = "needs_action"
            b.action = "folders"
            b.reason = "Выбранных папок больше нет на Диске"
            b.activity = ""
            self._log("📁 Диск: выбранных папок больше нет на Диске — выберите их заново в Настройках")
            self._event("disk", "err", "Выбранных папок больше нет на Диске — выберите папки заново в «Настройках»")
            return PREPARE_NEEDS_ACTION
        for path in missing:
            self._log(f"⚠️ Диск: папки «{folder_label(path)}» больше нет на Диске")
            self._event("disk", "warn", f"Папки «{folder_label(path)}» больше нет на Диске — "
                                        f"уберите её из выбора в «Настройках»")
        if shared:
            names = ", ".join(f"«{n}»" for n in shared)
            self._log(f"🤝 Диск: нашёл общие папки, к которым вам дали доступ: {names}")
            self._event("disk", "info", f"Нашёл общие папки, к которым вам дали доступ: {names} — скачаю и их")
        if code == 0:
            # Пропавшие папки не запоминаем: следующий запуск проверит их снова
            found = [p for p in roots or [] if p not in missing]
            if mode == "new" and rec:
                rec = dict(rec, folders=rec["folders"] + found)
            else:
                rec = {"at": time.time(), "whole": not roots, "folders": found}
            self._remember_listing("disk", rec)
            self._take_relist("disk")  # «Обновить список», нажатое во время этого сбора, исполнено
            total = int(db_stats(paths.DB_FILE, "files", "remote_path").get("total") or 0)
            self._event("disk", "ok", f"Список получен: {files_text(total)}")
        elif db_has_rows(paths.DB_FILE, "files"):
            self._log(f"⚠️ Диск: список не обновился (код {code}) — качаю по уже собранному")
            self._event("disk", "warn", "Список не обновился — качаю по уже собранному")
        else:
            return code
        return 0

    def _disk_download(self, b: Branch) -> int:
        b.state = "downloading"
        self._event("disk", "run", "Новая попытка для отложенных файлов" if b.round > 1 else "Начинаю скачивание")
        if self._folders_done is None:
            self._watch_folders(force=True)  # папки, скачанные раньше, — без событий
        workers = min(self.workers, MAX_DISK_WORKERS)
        code = self._run_step(b, SLOT_DISK, "main.py", ["download", "--workers", str(workers), "--no-scan"],
                              "скачиваю файлы")
        if code != -1:
            self._watch_folders(force=True)
        return code

    def _watch_folders(self, force: bool = False) -> None:
        """Лента: «Папка «Фото» скачана целиком» — папка верхнего уровня Диска докачалась.

        Первый подсчёт за спасение только запоминает уже скачанные папки. Не чаще раза
        в FOLDERS_SUMMARY_SEC, если не force. Никогда не бросает: лента — не скачивание.
        """
        now = time.monotonic()
        if not force and now - self._folders_at < FOLDERS_SUMMARY_SEC:
            return
        self._folders_at = now
        try:
            summary = disk_folders_summary(paths.DB_FILE, force=True)
        except Exception as e:  # noqa: BLE001
            log.warning("сводка папок Диска: %s", e)
            return
        if summary is None:
            return
        done = set(summary["names"])
        if self._folders_done is not None:
            new = sorted(done - self._folders_done, key=lambda n: (n.casefold(), n))
            if 0 < len(new) <= 3:
                for name in new:
                    self._event("disk", "ok", f"Папка «{name}» скачана целиком")
            elif new:
                shown = ", ".join(f"«{n}»" for n in new[:3])
                self._event("disk", "ok", f"Скачано целиком папок: {len(new)} — {shown} и другие. "
                                          "Список — в «Подробнее» у Диска")
        self._folders_done = done

    # ── Безлимит ──

    def _run_unlim(self, b: Branch) -> None:
        self._drive(b, self._unlim_prepare, self._unlim_download,
                    (paths.UNLIM_DB_FILE, "unlim_files", "file_id"), self._unlim_list_late)

    def _unlim_prepare(self, b: Branch) -> int:
        if not b.reset_done:
            b.reset_done = True
            n = reset_failed_files(str(paths.UNLIM_DB_FILE)) if paths.UNLIM_DB_FILE.exists() else 0
            if n:
                self._log(f"↩️ Безлимит: {n} файлов с ошибками снова в очереди")
                self._event("unlim", "info", f"Снова в очереди: {files_text(n)}, которые раньше не скачались")
        explicit = self._take_relist("unlim")
        rec = read_listing("unlim")
        if not listing_stale(rec, time.time()):
            self._log(f"📋 Безлимит: список фото и видео собран {when_text(rec['at'])} — заново не собираю")
            self._event("unlim", "info", f"Список фото и видео собран {when_text(rec['at'])} — заново не собираю")
        elif (not explicit and db_has_pending(paths.UNLIM_DB_FILE, "unlim_files")
              and not unlim_has_rows_outside(paths.UNLIM_DB_FILE)):
            self._list_after(b, rec)
        else:
            code = self._unlim_list(b)
            if code:
                return code
        self._apply_category_choice(paths.UNLIM_DB_FILE, "unlim_files", "file_id", self.unlim_categories,
                                    "Безлимит")
        return 0

    def _unlim_list_late(self, b: Branch) -> int:
        """Сбор списка, отложенный до конца первого шага скачивания (Branch.list_after)."""
        if not listing_stale(read_listing("unlim"), time.time()):
            return 0
        code = self._unlim_list(b)
        if code == 0:
            self._apply_category_choice(paths.UNLIM_DB_FILE, "unlim_files", "file_id", self.unlim_categories,
                                        "Безлимит")
        return code

    def _unlim_list(self, b: Branch) -> int:
        """Сбор списка безлимита (unlim_download.py --collect). 0 — можно качать: список получен или есть прежний."""
        b.state = "listing"
        self._event("unlim", "run", "Получаю у Яндекса список фото и видео")
        code = self._run_step(b, SLOT_UNLIM_COLLECT, "unlim_download.py", ["--collect"],
                              "получаю список файлов")
        if code == -1:
            return -1
        if code == 0:
            self._remember_listing("unlim", {"at": time.time(), "whole": True, "folders": []})
            self._take_relist("unlim")  # «Обновить список», нажатое во время этого сбора, исполнено
            total = int(db_stats(paths.UNLIM_DB_FILE, "unlim_files", "file_id").get("total") or 0)
            self._event("unlim", "ok", f"Список получен: {files_text(total)}")
        elif db_has_rows(paths.UNLIM_DB_FILE, "unlim_files"):
            self._log(f"⚠️ Безлимит: список не обновился (код {code}) — качаю по уже собранному")
            self._event("unlim", "warn", "Список не обновился — качаю по уже собранному")
        else:
            return code
        return 0

    def _unlim_download(self, b: Branch) -> int:
        b.state = "downloading"
        b.activity = "скачиваю файлы"
        # Каждый раунд даёт молчавшим серверам ещё один шанс
        clear_dead_hosts(str(paths.UNLIM_DB_FILE))
        while True:
            if self._stopped():
                return -1
            res = launch_unlim_workers(self.workers, self.batch, starter=self._own_start)
            if res["started"]:
                break
            # Все слоты заняты воркерами, запущенными вручную, — дождёмся их
            self._log("⏳ Безлимит: жду уже запущенных воркеров")
            if self._stop_evt.wait(5):
                return -1
        self._log(f"🚀 Безлимит: запущено воркеров — {len(res['started'])}")
        n = len(res["started"])
        self._event("unlim", "run", "Новая попытка для отложенных файлов" if b.round > 1 else
                    f"Начинаю скачивание в {n} {plural(n, 'поток', 'потока', 'потоков')}")
        codes = self._wait_workers(res["started"])
        if codes is None:
            return -1
        code = self._combine_worker_codes(codes)
        b.last_exit = {"code": code, "at": time.time(), "step": "скачиваю файлы"}
        if not tm.any_alive(UNLIM_WORKER_SLOT_PREFIX):
            # Воркер упал посреди пачки: его файлы остались «в работе» и без этого
            # не считались бы нескачанными — ветка объявила бы «все файлы сохранены»
            n = release_orphaned_unlim()
            if n:
                self._log(f"↩️ Безлимит: {n} файлов зависли «в работе» после воркера — снова в очереди")
        return code

    @staticmethod
    def _combine_worker_codes(codes: list[int | None]) -> int:
        real = [c for c in codes if c is not None]
        for important in (EXIT_DISK_FULL, EXIT_AUTH, EXIT_DEFERRED, EXIT_OUTAGE):
            if important in real:
                return important
        bad = [c for c in real if c != 0]
        return bad[0] if bad else 0


rescue = Rescue()
