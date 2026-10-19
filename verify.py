"""Проверка скачанного: все ли файлы на месте и совпадают ли они с Яндексом.

Общая для обычного Диска (main.py verify) и Безлимита (unlim_download.py --verify).
Сначала размеры всех файлов — это быстро даже на 250 000 файлов. Потом MD5, но только
у файлов, чей размер совпал и чей MD5 Яндекс сообщил: MD5 читает каждый файл целиком,
и на большой папке это часы.

Для Web UI печатаются строки `verify_progress | {json}` (не чаще раза в 2 секунды) и в
самом конце `verify_summary | {json}` — экран «Подробнее» читает их из журнала задачи.
Проблемные файлы (нет в папке, другой размер, другой MD5) вызывающий запоминает в базе:
кнопка «Скачать заново» возвращает их в очередь.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from typing import NamedTuple

from tqdm import tqdm

from storage_fetch import is_md5
from utils import files_text, human_bytes, human_duration_ru, keep_awake, to_long_path, under_webui

# Примеров каждого вида в строке verify_summary: интерфейс показывает их списком
EXAMPLES_PER_KIND = 50
# Строку прогресса для Web UI печатаем не чаще, чем раз в столько секунд
PROGRESS_EVERY_SEC = 2.0
# Нет файла, другой размер, другой MD5 — такие файлы можно скачать заново.
# «Не прочитать» (файл открыт в другой программе, нет прав) — не порча: новая копия
# его тоже не заменит, поэтому в очередь он не возвращается
REQUEUE_KINDS = ("missing", "size", "md5")
KINDS = (*REQUEUE_KINDS, "unreadable")
_CHUNK = 1024 * 1024
_TITLES = {"missing": "Нет в папке", "size": "Размер не совпал", "md5": "MD5 не совпал",
           "unreadable": "Не удалось прочитать"}


class Item(NamedTuple):
    """Скачанный файл из базы: ключ (remote_path или file_id), где лежит, что о нём знает Яндекс."""

    key: str
    local_path: str | None
    size: int | None
    md5: str | None


def _error_text(e: OSError) -> str:
    return (e.strerror or str(e))[:200]


def _md5(path: str, on_chunk: Callable[[int], None]) -> str:
    h = hashlib.md5()
    with open(to_long_path(path), "rb") as f:
        while block := f.read(_CHUNK):
            h.update(block)
            on_chunk(len(block))
    return h.hexdigest()


def check_files(items: Iterable[Item],
                on_progress: Callable[[dict], None] | None = None) -> tuple[dict, list[tuple[str, str]]]:
    """Проверяет скачанные файлы. Возвращает (сводку для verify_summary, проблемы [(ключ, вид)]).

    on_progress получает {"stage": "size"|"md5", "done", "total", "found"}, для MD5 ещё
    bytes_done, bytes_total и eta_sec: после каждого файла и каждого прочитанного мегабайта.
    """
    t0 = time.monotonic()
    items = list(items)
    todo = [it for it in items if it.local_path]
    counts = dict.fromkeys((*KINDS, "ok", "no_md5"), 0)
    examples: dict[str, list[dict]] = {k: [] for k in KINDS}
    problems: list[tuple[str, str]] = []
    bad_bytes = 0
    reachable = 0

    def found(kind: str, it: Item, **extra) -> None:
        nonlocal bad_bytes
        counts[kind] += 1
        if kind in REQUEUE_KINDS:
            problems.append((it.key, kind))
            bad_bytes += it.size or 0
        if len(examples[kind]) < EXAMPLES_PER_KIND:
            examples[kind].append({"path": it.key, "size": it.size, **extra})

    def report(**p) -> None:
        if on_progress is not None:
            on_progress({**p, "found": sum(counts[k] for k in KINDS)})

    # 1. Размеры: по одному обращению к диску на файл
    to_hash: list[tuple[Item, int]] = []
    for i, it in enumerate(todo, 1):
        try:
            got = os.stat(to_long_path(it.local_path)).st_size
        except (FileNotFoundError, NotADirectoryError):
            found("missing", it)
        except OSError as e:  # нет прав, диск не готов
            found("unreadable", it, error=_error_text(e))
        else:
            reachable += 1
            if it.size is not None and got != it.size:
                found("size", it, local_size=got)
            elif is_md5(it.md5):
                to_hash.append((it, got))
            else:
                counts["ok"] += 1
                counts["no_md5"] += 1
        report(stage="size", done=i, total=len(todo))

    # 2. MD5 — только у файлов, чей размер совпал
    bytes_total = sum(got for _, got in to_hash)
    bytes_done = 0
    hashed = 0
    t_md5 = time.monotonic()

    def md5_progress() -> None:
        spent = time.monotonic() - t_md5
        eta = round((bytes_total - bytes_done) * spent / bytes_done) if spent >= 5 and bytes_done else None
        report(stage="md5", done=hashed, total=len(to_hash), bytes_done=bytes_done,
               bytes_total=bytes_total, eta_sec=eta)

    def on_chunk(n: int) -> None:
        nonlocal bytes_done
        bytes_done += n
        md5_progress()

    for it, got in to_hash:
        start = bytes_done
        try:
            actual = _md5(it.local_path, on_chunk)
        except (FileNotFoundError, NotADirectoryError):  # удалили, пока шла проверка
            found("missing", it)
        except OSError as e:
            found("unreadable", it, error=_error_text(e))
        else:
            if actual == it.md5.lower():
                counts["ok"] += 1
            else:
                found("md5", it)
        bytes_done = start + got
        hashed += 1
        md5_progress()

    summary = {
        "checked": len(todo),
        "ok": counts["ok"],
        **{k: counts[k] for k in KINDS},
        "no_md5": counts["no_md5"],
        # У строк безлимита из старых версий не записано, где лежит файл: проверить нечем
        "unchecked": len(items) - len(todo),
        "bytes": sum(it.size or 0 for it in todo),
        "bad_bytes": bad_bytes,
        # Ни одного файла не нашлось: отключён диск с папкой или папку перенесли целиком
        "all_missing": bool(todo) and not reachable,
        "seconds": round(time.monotonic() - t0),
        "examples": {k: v for k, v in examples.items() if v},
    }
    return summary, problems


def progress_line(p: dict) -> str:
    return f"verify_progress | {json.dumps(p, ensure_ascii=False)}"


def summary_line(summary: dict) -> str:
    return f"verify_summary | {json.dumps(summary, ensure_ascii=False)}"


class Progress:
    """Прогресс проверки: полоса tqdm в терминале, строка verify_progress для Web UI."""

    def __init__(self, tty: bool | None = None, every: float = PROGRESS_EVERY_SEC,
                 clock: Callable[[], float] = time.monotonic):
        self.tty = sys.stderr.isatty() if tty is None else tty
        self.every = every
        self.clock = clock
        self._stage: str | None = None
        self._last = 0.0
        self._bar: tqdm | None = None

    def __call__(self, p: dict) -> None:
        if self.tty:
            self._draw(p)
            return
        now = self.clock()
        # Начало этапа — сразу: интерфейс должен узнать, что пошла долгая сверка MD5
        if p["stage"] != self._stage or now - self._last >= self.every:
            self._stage, self._last = p["stage"], now
            print(progress_line(p), flush=True)

    def _draw(self, p: dict) -> None:
        md5 = p["stage"] == "md5"
        if p["stage"] != self._stage:
            self.close()
            self._stage = p["stage"]
            self._bar = (tqdm(total=p["bytes_total"], desc="Сверяю MD5", unit="B", unit_scale=True,
                              unit_divisor=1024, leave=False) if md5 else
                         tqdm(total=p["total"], desc="Все ли файлы на месте", unit="file", leave=False))
        if self._bar is not None:
            self._bar.update((p["bytes_done"] if md5 else p["done"]) - self._bar.n)

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None


def print_report(summary: dict) -> None:
    """Итог для человека (консоль, журнал в «Подробнее»), последней строкой — verify_summary."""
    s = summary
    print(f"\nПроверено: {files_text(s['checked'])} ({human_bytes(s['bytes'])}) "
          f"за {human_duration_ru(s['seconds'])}")
    print(f"  В порядке:                  {s['ok']}")
    for kind in KINDS:
        if kind != "unreadable" or s[kind]:
            print(f"  {_TITLES[kind] + ':':<27} {s[kind]}")
    if s["no_md5"]:
        print(f"  Без MD5 от Яндекса, проверен только размер: {s['no_md5']}")
    if s["unchecked"]:
        print(f"  Не записано, куда сохранены, — не проверены: {s['unchecked']}")
    if s["all_missing"]:
        print("  Ни одного файла нет на месте: диск с папкой отключён или папку перенесли?")
    if not under_webui():
        # В Web UI примеры показывает карточка проверки, журнал ими не засоряем
        for kind in KINDS:
            shown = s["examples"].get(kind, [])[:20]
            if shown:
                print(f"\n{_TITLES[kind]} (первые {len(shown)}):")
                for e in shown:
                    extra = (f" (на диске {e['local_size']}, у Яндекса {e['size']})" if kind == "size"
                             else f" ({e['error']})" if kind == "unreadable" else "")
                    print(f"  {e['path']}{extra}")
    # Последней строкой: интерфейс ищет её в хвосте журнала
    print(summary_line(s), flush=True)


def run_check(items: list[Item], save_problems: Callable[[list[tuple[str, str]]], None]) -> int:
    """Проверка для CLI: прогресс, запись проблем в базу, итог. 0 — всё в порядке, 1 — есть проблемы.

    save_problems(пары (ключ, вид)) заменяет в базе список для «Скачать заново».
    """
    if items:
        total = sum(it.size or 0 for it in items)
        print(f"Проверяю скачанные файлы: {files_text(len(items))} ({human_bytes(total)}). Сначала смотрю, "
              f"все ли на месте, потом сверяю MD5 — это долго: каждый файл читается целиком.", flush=True)
    else:
        print("Скачанных файлов пока нет — проверять нечего.", flush=True)
    # Прошлый итог больше не верен; остановленная проверка не должна оставить старый список
    save_problems([])
    progress = Progress()
    # Сверка MD5 большой папки идёт часами: компьютер не должен уснуть посреди неё
    with keep_awake():
        try:
            summary, problems = check_files(items, progress)
        finally:
            progress.close()
    # Сначала база, потом итог: «Скачать заново» в интерфейсе появляется по строке итога
    save_problems(problems)
    print_report(summary)
    return 0 if not any(summary[k] for k in KINDS) else 1
