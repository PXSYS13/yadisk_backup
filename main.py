"""CLI для бэкапа Яндекс.Диска.

Команды:
    python main.py scan                 # обход дерева, заполнить state без скачивания
    python main.py scan --root /Фото --root "/Работа/2024"   # только эти папки
    python main.py download             # скачать всё pending
    python main.py download --workers 4 # с заданным числом потоков
    python main.py download --wait      # не выходить: отложенные повторять каждые 30 минут
    python main.py retry-failed         # сбросить failed → pending и скачать
    python main.py stats                # статистика по БД
    python main.py verify               # проверить md5 локальных файлов
    python main.py check                # проверить токен и доступ к диску
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import sqlite3
import sys
import time
from contextlib import closing
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

from downloader import (
    DEAD_HOSTS,
    RootNotFound,
    download_all,
    make_client,
    scan_exit_code,
    scan_media,
    scan_tree,
    token_state,
)
from state import State
from utils import (
    APP_VERSION,
    EXIT_AUTH,
    EXIT_DEFERRED,
    EXIT_DISK_FULL,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_OUTAGE,
    EXIT_PARTIAL,
    MAX_DISK_WORKERS,
    TELEGRAM_CHANNEL,
    YOUTUBE_VIDEO_URL,
    compute_md5,
    deferred_round_text,
    ensure_dir,
    ensure_utf8_stdio,
    file_size_safe,
    files_text,
    hhmm,
    human_bytes,
    human_duration,
    is_sqlite_disk_error,
    keep_awake,
    plural,
    retry_every_sec,
    safe_int,
    under_webui,
    utc_ts,
)

# Где лежит проект — все относительные пути считаем отсюда
PROJECT_DIR = Path(__file__).resolve().parent
LOG_DIR = PROJECT_DIR / "logs"
LOG_FILE = LOG_DIR / "backup.log"
DB_FILE = PROJECT_DIR / "state.db"

EMPTY_COUNTERS = {"downloaded": 0, "skipped": 0, "failed": 0, "deferred": 0, "bytes": 0}

log = logging.getLogger("yadisk_backup")


def setup_logging(level: str = "INFO") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_h = RotatingFileHandler(
        LOG_FILE, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_h.setFormatter(fmt)

    # Консольный хендлер пишет только WARNING+ — чтобы не мешать tqdm-барам
    console_h = logging.StreamHandler(stream=sys.stderr)
    console_h.setLevel(logging.WARNING)
    console_h.setFormatter(fmt)

    root = logging.getLogger("yadisk_backup")
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        print(f"LOG_LEVEL={level!r} не распознан, использую INFO", file=sys.stderr)
        numeric = logging.INFO
    root.setLevel(numeric)
    # Сбрасываем хендлеры на случай повторного вызова
    root.handlers.clear()
    root.addHandler(file_h)
    root.addHandler(console_h)
    root.propagate = False


def load_config(require_token: bool = True) -> dict:
    """Грузит .env из директории проекта.

    Если require_token=True и токен не задан — выходим с понятной ошибкой.
    Для команд stats/verify токен не нужен, можно работать только с БД.
    """
    dotenv_path = PROJECT_DIR / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path)
    elif require_token:
        sys.exit(
            f"Не найден файл {dotenv_path}.\n"
            f"Скопируй .env.example в .env и пропиши YA_DISK_TOKEN.\n"
            f"Токен взять тут: https://yandex.ru/dev/disk/poligon/"
        )

    token = os.getenv("YA_DISK_TOKEN", "").strip()
    download_dir = os.getenv("DOWNLOAD_DIR", str(PROJECT_DIR / "downloads")).strip()
    max_workers = safe_int(os.getenv("MAX_WORKERS"), default=4, min_val=1, max_val=16)
    log_level = os.getenv("LOG_LEVEL", "INFO").strip() or "INFO"

    return {
        "token": token,
        "download_dir": download_dir,
        "max_workers": max_workers,
        "log_level": log_level,
    }


# ── общие шаги команд ───────────────────────────────────────────────────────


def _token_exit_code(state: str) -> int:
    """Недействительный токен — код 2 (нужен новый), недоступный Яндекс — код 3 (повторить позже)."""
    return EXIT_OK if state == "ok" else EXIT_AUTH if state == "invalid" else EXIT_OUTAGE


def _require_token(cfg: dict) -> int | None:
    """Проверяет токен и печатает результат. Возвращает код выхода при ошибке, иначе None."""
    state, msg = token_state(cfg["token"])
    if state != "ok":
        print(msg, file=sys.stderr)
        return _token_exit_code(state)
    print(msg)
    return None


def _download_exit_code(counters: dict) -> int:
    """Код выхода по итогам download_all (см. EXIT_* в utils.py).

    Отложенные файлы важнее упавших: их Rescue повторит сам в следующий раунд,
    а упавшие видны в статистике и повторяются кнопкой.
    """
    aborted = counters.get("aborted")
    if aborted == "local_disk":
        return EXIT_DISK_FULL
    if aborted == "auth":
        return EXIT_AUTH
    if counters.get("deferred", 0) > 0:
        return EXIT_DEFERRED
    return EXIT_OK if counters.get("failed", 0) == 0 else EXIT_PARTIAL


def _run_download(cfg: dict, state: State, workers: int, wait: bool = False) -> int:
    """Запускает download_all и печатает итог. Коды выхода — _download_exit_code, 130 — Ctrl+C.

    wait — идёт main.py download --wait: о следующей попытке скажет он сам.
    """
    print(f"Старт скачивания в {workers} {plural(workers, 'поток', 'потока', 'потоков')} → {cfg['download_dir']}")
    try:
        ensure_dir(cfg["download_dir"])
    except OSError as e:
        # Диск отключён, путь неверный или нет прав: без этого процесс падал с кодом 1,
        # и интерфейс принимал падение за «скачано, часть не скачалась»
        print(f"local_disk | Не получается создать папку {cfg['download_dir']}: {e}. "
              f"Проверьте, что диск подключён, или выберите другую папку.", flush=True)
        return EXIT_DISK_FULL
    t0 = time.time()
    counters = dict(EMPTY_COUNTERS)
    try:
        counters = download_all(
            token=cfg["token"],
            state=state,
            download_dir=cfg["download_dir"],
            workers=workers,
        )
    except sqlite3.OperationalError as e:
        if not is_sqlite_disk_error(e):
            raise
        # База лежит на том же диске, что и файлы: он кончился — это код 4, а не падение
        print(f"local_disk | Не получается записать базу state.db: {e}. Освободите место "
              f"или проверьте, что диск подключён.", flush=True)
        return EXIT_DISK_FULL
    except KeyboardInterrupt:
        _print_summary(counters, time.time() - t0)
        print("\nПрервано пользователем. Прогресс сохранён в state.db.", flush=True)
        # Потоки пула не остановить посреди файла — они держат сокеты до конца
        # скачивания, и обычный выход ждал бы их минуты. Состояние в БД уже
        # согласовано (WAL), недокачанные .part уберутся при следующем запуске.
        logging.shutdown()
        os._exit(130)
    _print_summary(counters, time.time() - t0)
    aborted = counters.get("aborted")
    if aborted == "local_disk":
        print("local_disk | Остановлено: локальный диск не принимает файлы (нет места, нет прав "
              "или диск отключён). Освободите место или выберите другую папку.", flush=True)
    elif aborted == "auth":
        print("auth_expired | Остановлено: Яндекс перестал принимать токен. Получите новый.", flush=True)
    elif counters.get("deferred", 0) > 0:
        print(f"storage_down | Отложено {files_text(counters['deferred'])}: их серверы хранения Яндекса "
              f"сейчас не отвечают. Файлы не потеряны — их скачает следующая попытка.", flush=True)
        if not wait and not under_webui():
            _print_deferred_hint(state)
    return _download_exit_code(counters)


def _deferred_text(state: State) -> str:
    """«1 234 файла на 301 сервере хранения» — сколько файлов ждёт молчащих серверов Яндекса."""
    d = state.get_deferred_stats()
    hosts = d["hosts"]
    where = f" на {hosts} {plural(hosts, 'сервере', 'серверах', 'серверах')} хранения" if hosts else ""
    return f"{files_text(d['files'])}{where}"


def _print_deferred_hint(state: State) -> None:
    """Консоль: сама программа отложенные не повторит, если её не попросить.

    В Web UI повторы каждые 30 минут ведёт интерфейс, поэтому там подсказка не печатается.
    """
    print(f"\n  Скачано всё, что Яндекс отдаёт сейчас. Ждут Яндекса: {_deferred_text(state)}.")
    print("  Повторить их: python main.py download. С ключом --wait программа будет пробовать их сама")
    print("  каждые 30 минут, пока не скачает всё: python main.py download --wait", flush=True)


class _ScanProgress:
    """Счётчик найденных файлов: tqdm в терминале, простая строка раз в 2 с под Web UI."""

    def __init__(self, label: str):
        self.label = label
        self.n = 0
        self._tty = sys.stderr.isatty()
        self._bar = tqdm(unit="file", desc=label, leave=False) if self._tty else None
        self._last = time.monotonic()

    def on_file(self, _path: str, _size: int) -> None:
        self.n += 1
        if self._bar is not None:
            self._bar.update(1)
            return
        now = time.monotonic()
        if now - self._last >= 2.0:
            self._last = now
            print(f"{self.label}: {self.n}", flush=True)

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
        else:
            print(f"{self.label}: {self.n}", flush=True)


def _print_summary(counters: dict, elapsed: float) -> None:
    print(
        f"\nИтого: скачано {counters['downloaded']} ({human_bytes(counters['bytes'])}) | "
        f"пропущено {counters['skipped']} | упало {counters['failed']} | "
        f"отложено {counters.get('deferred', 0)} | время {human_duration(elapsed)}"
    )
    if counters.get("failed", 0) > 0:
        print("  💡 Если файлы упали из-за сбоев серверов Яндекса (50x/429) — запустите: python main.py retry-failed")
        print(f"     Новости и статус работы серверов Яндекса: {TELEGRAM_CHANNEL}")


# ── команды ─────────────────────────────────────────────────────────────────


def cmd_check(cfg: dict, args: argparse.Namespace) -> int:
    state, msg = token_state(cfg["token"])
    print(msg)
    return _token_exit_code(state)


def cmd_scan(cfg: dict, args: argparse.Namespace) -> int:
    if (code := _require_token(cfg)) is not None:
        return code

    roots = args.root or (
        [r.strip() for r in args.roots.split(",") if r.strip()]
        if args.roots
        else ["/"]
    )

    total_files = 0
    total_bytes = 0
    missing: list[str] = []
    notes: dict[str, list[str]] = {}
    # Один счётчик на все корни: Web UI показывает «Найдено: N» по последней строке
    progress = _ScanProgress("Найдено")
    try:
        with State(str(DB_FILE)) as state, closing(make_client(cfg["token"])) as client:
            for root in roots:
                print(f"Обхожу {root}...")
                try:
                    files, bytes_ = scan_tree(
                        client=client,
                        state=state,
                        root=root,
                        on_file=progress.on_file,
                        on_note=lambda kind, names: notes.setdefault(kind, []).extend(names),
                    )
                except RootNotFound as e:
                    # Папку выбрали в Настройках, а на Диске её уже нет — Web UI скажет об этом
                    missing.append(e.path)
                    print(f"  → {root}: этой папки больше нет на Диске")
                    log.warning("scan: выбранной папки нет на Диске")
                    continue
                print(f"  → {root}: {files} файлов, {human_bytes(bytes_)}")
                log.info("scan: %d файлов, %s", files, human_bytes(bytes_))
                total_files += files
                total_bytes += bytes_
    except Exception as e:  # noqa: BLE001 — ниже решаем, известная ли это ошибка
        code = scan_exit_code(e)
        if code is None:
            raise
        return _scan_failed(code, e)
    finally:
        progress.close()

    # Находки — в самом конце: Web UI читает их из хвоста журнала, когда сбор закончен,
    # а за долгий сбор ранние строки вытеснили бы строки прогресса
    unreadable = notes.pop("unreadable_folders", [])
    for path in missing:
        print(f"folder_missing | {json.dumps({'path': path}, ensure_ascii=False)}", flush=True)
    for kind, names in notes.items():
        _print_note(kind, names)
    print(f"\nИтого: {total_files} файлов, {human_bytes(total_bytes)}")
    log.info("scan итого: %d файлов, %s; в БД %s", total_files, human_bytes(total_bytes), _stats_line())
    if unreadable:
        # Список без этих папок неполный: код 3, чтобы Web UI не запомнил его как собранный,
        # а найденные файлы всё равно уже в базе и скачаются
        print(f"server_50x | Не удалось прочитать {len(unreadable)} папок — Яндекс не ответил. "
              f"Список файлов неполный, соберу его заново при следующей попытке.", flush=True)
        log.warning("scan: не прочитано папок: %d", len(unreadable))
        return EXIT_OUTAGE
    return 0


def _scan_failed(code: int, e: BaseException) -> int:
    """Сбор списка оборвался известной ошибкой: строка с префиксом для Web UI и код выхода."""
    if code == EXIT_AUTH:
        print(f"auth_expired | Яндекс перестал принимать ключ доступа: {e}. Получите новый.", flush=True)
    elif code == EXIT_DISK_FULL:
        print(f"local_disk | Не получается записать базу state.db: {e}. Освободите место "
              f"или проверьте, что диск подключён.", flush=True)
    else:
        print(f"server_50x | Яндекс не отвечает, список файлов собрать не удалось: {e}. "
              f"Повторю позже — уже собранное не теряется.", flush=True)
    log.error("scan оборван: %s: %s", type(e).__name__, e)
    return code


def _print_note(kind: str, names: list[str]) -> None:
    """Находка сбора списка для Web UI: `kind | {"names": [...]}` (например, общие папки)."""
    print(f"{kind} | {json.dumps({'names': names}, ensure_ascii=False)}", flush=True)


def _stats_line() -> str:
    """Короткая сводка по БД для логов."""
    try:
        with State(str(DB_FILE)) as state:
            s = state.get_stats()
        return (f"pending={s['pending']} downloaded={s['downloaded']} skipped={s['skipped']} "
                f"failed={s['failed']} всего={human_bytes(s['bytes_total'])}")
    except Exception as e:  # noqa: BLE001 — сводка для лога не должна ронять команду
        return f"нет данных ({e})"


def cmd_scan_media(cfg: dict, args: argparse.Namespace) -> int:
    """Сканирует ТОЛЬКО фото и видео со всего диска (через media_type)."""
    if (code := _require_token(cfg)) is not None:
        return code

    media_types = tuple(t.strip() for t in args.types.split(",") if t.strip())
    print(f"Сканирую медиа: {', '.join(media_types)}")

    progress = _ScanProgress("Найдено медиа")
    with State(str(DB_FILE)) as state, closing(make_client(cfg["token"])) as client:
        try:
            files, total_bytes = scan_media(
                client=client,
                state=state,
                media_types=media_types,
                on_file=progress.on_file,
            )
        finally:
            progress.close()

    print(f"Найдено медиа-файлов: {files}")
    print(f"Суммарный объём: {human_bytes(total_bytes)}")
    return 0


def _scan_if_empty(cfg: dict, state: State, args: argparse.Namespace) -> None:
    if state.get_stats().get("pending", 0) == 0:
        print("В БД нет pending-файлов. Сначала выполни: python main.py scan")
        # Но всё-равно попробуем — может пользователь сначала запустил download
        if not args.no_scan:
            print("Запускаю scan автоматически (--no-scan чтобы отключить)...")
            with closing(make_client(cfg["token"])) as client:
                scan_tree(client, state, root=args.root)


def cmd_download(cfg: dict, args: argparse.Namespace) -> int:
    workers = args.workers or cfg["max_workers"]
    if not args.wait:
        if (code := _require_token(cfg)) is not None:
            return code
        with State(str(DB_FILE)) as state:
            _scan_if_empty(cfg, state, args)
            return _run_download(cfg, state, workers)
    # Компьютер не засыпает и между попытками: серверы Яндекса оживают когда угодно
    with keep_awake():
        return _download_rounds(cfg, args, workers)


def _download_rounds(cfg: dict, args: argparse.Namespace, workers: int) -> int:
    """--wait: раунды, как у Web UI. Отложенные файлы пробуются каждые retry_every_sec(),
    пока не скачаются. Выход — когда ждать нечего, нужен человек (код 2 или 4) или Ctrl+C.
    """
    every = retry_every_sec()
    round_no = 1
    while True:
        waiting = ""
        code = _require_token(cfg)
        if code is None:
            with State(str(DB_FILE)) as state:
                if round_no == 1:
                    _scan_if_empty(cfg, state, args)
                deferred_before = state.get_deferred_stats()["files"]
                downloaded_before = state.get_stats()["downloaded"]
                code = _run_download(cfg, state, workers, wait=True)
                if round_no > 1:
                    got = state.get_stats()["downloaded"] - downloaded_before
                    _print_round_result(round_no, deferred_before, got)
                if code == EXIT_DEFERRED:
                    waiting = _deferred_text(state)
        if code not in (EXIT_DEFERRED, EXIT_OUTAGE):
            if round_no > 1 and code in (EXIT_OK, EXIT_PARTIAL):
                print("Отложенных файлов больше нет — повторять нечего.", flush=True)
            return code
        next_at = time.time() + every
        why = f"Ждут Яндекса: {waiting}" if waiting else "Яндекс сейчас не отвечает"
        print(f"\n{why}. Следующая попытка в {hhmm(next_at)}. "
              f"Остановить — Ctrl+C, скачанное сохранится.", flush=True)
        if not _sleep_until(next_at):
            return EXIT_INTERRUPTED
        # Молчавшим серверам — новый шанс: файлы каждого пробуются хотя бы раз
        DEAD_HOSTS.clear()
        round_no += 1


def _print_round_result(round_no: int, deferred_before: int, downloaded: int) -> None:
    """Итог повторной попытки: сколько отложенных отдал Яндекс — видно, оживают ли его серверы."""
    if deferred_before:
        print(f"Попытка №{round_no}: {deferred_round_text(deferred_before, downloaded)}.", flush=True)


def _sleep_until(ts: float) -> bool:
    """Ждёт до ts. False — человек нажал Ctrl+C."""
    try:
        while (left := ts - time.time()) > 0:
            time.sleep(min(left, 5.0))
    except KeyboardInterrupt:
        print("\nОстановлено. Скачанное сохранено, отложенные файлы остались в очереди.", flush=True)
        return False
    return True


def cmd_retry_failed(cfg: dict, args: argparse.Namespace) -> int:
    if (code := _require_token(cfg)) is not None:
        return code

    with State(str(DB_FILE)) as state:
        n = state.reset_failed_to_pending()
        print(f"Сброшено failed → pending: {n}")
        if n == 0:
            return 0
        return _run_download(cfg, state, args.workers or cfg["max_workers"])


def cmd_stats(cfg: dict, args: argparse.Namespace) -> int:
    with State(str(DB_FILE)) as state:
        s = state.get_stats()
        d = state.get_deferred_stats()
        last = utc_ts(state.last_downloaded_at())

    print("Статистика по БД state.db:")
    print(f"  Всего файлов:        {s['total']}")
    print(f"    pending:           {s['pending']}")
    print(f"      ещё не пробовали:  {s['pending'] - d['files']}")
    print(f"      ждут Яндекса:      {d['files']} ({human_bytes(d['bytes'])}, серверов хранения: {d['hosts']})")
    print(f"    downloaded:        {s['downloaded']}")
    print(f"    skipped:           {s['skipped']}")
    print(f"    failed:            {s['failed']}")
    print(f"  Объём всего:         {human_bytes(s['bytes_total'])}")
    print(f"  Объём скачано/skip:  {human_bytes(s['bytes_downloaded'])}")
    if last:
        when = time.strftime("%d.%m %H:%M", time.localtime(last))
        print(f"  Последний файл скачан: {when} (по часам этого компьютера)")
    if d["files"] and s["pending"] == d["files"]:
        # Ровно об этом спрашивают чаще всего: «упала доля успешных скачиваний — у всех так?»
        print("\n  Всё, что Яндекс отдаёт сейчас, уже скачано: в очереди только файлы, чьи серверы")
        print("  хранения молчат. Поэтому новые попытки почти все неудачные — это не поломка.")
        print("  Пусть программа повторяет их сама: python main.py download --wait")
    return 0


def cmd_verify(cfg: dict, args: argparse.Namespace) -> int:
    """Проверяет MD5 локальных файлов против того что хранит API."""
    with State(str(DB_FILE)) as state:
        rows = list(state.get_all_downloaded())
    if not rows:
        print("Нет скачанных файлов в БД для проверки.")
        return 0

    bad: list[tuple[str, str]] = []
    missing: list[str] = []
    size_mismatch: list[str] = []

    bar = tqdm(rows, desc="Проверка MD5", unit="file", disable=not sys.stderr.isatty())
    for row in bar:
        remote = row["remote_path"]
        local = row["local_path"]
        expected_md5 = row["md5"]
        expected_size = row["size"]

        size = file_size_safe(local)
        if size is None:
            missing.append(remote)
            continue
        if expected_size is not None and expected_size != size:
            size_mismatch.append(f"{remote} (local={size}, remote={expected_size})")
            continue
        if not expected_md5:
            continue  # API не вернул MD5 — проверить нечем
        try:
            actual = compute_md5(local)
        except OSError as e:
            bad.append((remote, f"read error: {e}"))
            continue
        if actual.lower() != expected_md5.lower():
            bad.append((remote, f"md5 mismatch: local={actual}, remote={expected_md5}"))

    bar.close()

    print(f"\nПроверено: {len(rows)}")
    print(f"  Отсутствует локально:    {len(missing)}")
    print(f"  Размер не совпал:        {len(size_mismatch)}")
    print(f"  MD5 не совпал:           {len(bad)}")

    if missing:
        print("\nОтсутствующие файлы (первые 20):")
        for p in missing[:20]:
            print(f"  {p}")
    if size_mismatch:
        print("\nРасхождение по размеру (первые 20):")
        for p in size_mismatch[:20]:
            print(f"  {p}")
    if bad:
        print("\nРасхождение по MD5 (первые 20):")
        for p, msg in bad[:20]:
            print(f"  {p}: {msg}")

    return 0 if not (missing or size_mismatch or bad) else 1


# ── argparse ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yadisk_backup",
        description="Полный бэкап Яндекс.Диска через официальный REST API.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Проверить токен и доступ к диску")
    p_check.set_defaults(func=cmd_check)

    p_scan = sub.add_parser("scan", help="Обойти дерево и заполнить БД (без скачивания)")
    p_scan.add_argument(
        "--root",
        action="append",
        default=None,
        help="Папка для обхода; можно указать несколько раз. Без неё — весь Диск",
    )
    p_scan.add_argument(
        "--roots",
        default=None,
        help="Несколько корней через запятую, напр.: /Фотокамера,/Видео,/Скриншоты",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_scan_media = sub.add_parser(
        "scan-media",
        help="Сканировать только фото и видео на основном диске (для безлимита используйте unlim_download.py / Web UI)",
    )
    p_scan_media.add_argument(
        "--types",
        default="image,video",
        help="Типы медиа через запятую: image,video (по умолчанию)",
    )
    p_scan_media.set_defaults(func=cmd_scan_media)

    p_dl = sub.add_parser("download", help="Скачать все pending файлы")
    p_dl.add_argument("--workers", type=int, default=None, help=f"Число потоков (макс {MAX_DISK_WORKERS})")
    p_dl.add_argument("--root", default="/", help="Корень для авто-scan если БД пуста")
    p_dl.add_argument("--no-scan", action="store_true", help="Не делать авто-scan если БД пуста")
    p_dl.add_argument("--wait", action="store_true",
                      help="Не выходить, пока есть отложенные файлы: повторять их каждые 30 минут, как Web UI")
    p_dl.set_defaults(func=cmd_download)

    p_retry = sub.add_parser("retry-failed", help="Перезапустить failed -> pending")
    p_retry.add_argument("--workers", type=int, default=None)
    p_retry.set_defaults(func=cmd_retry_failed)

    p_stats = sub.add_parser("stats", help="Показать статистику по БД")
    p_stats.set_defaults(func=cmd_stats)

    p_verify = sub.add_parser("verify", help="Проверить MD5 скачанных файлов")
    p_verify.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()

    parser = build_parser()
    args = parser.parse_args(argv)

    print("=" * 65)
    print("  Yandex.Disk Backup by Davron (PXSYS)")
    print(f"  Telegram: {TELEGRAM_CHANNEL} (новости, помощь, статус Яндекса)")
    print(f"  YouTube: {YOUTUBE_VIDEO_URL}")
    print("=" * 65)

    # stats и verify читают только локальную БД — токен не нужен
    require_token = args.cmd not in ("stats", "verify")
    cfg = load_config(require_token=require_token)
    setup_logging(cfg["log_level"])
    log.info(
        "yadisk_backup %s | python %s | %s | команда: %s | БД: %s | папка: %s | workers=%s",
        APP_VERSION, platform.python_version(), platform.platform(), args.cmd,
        DB_FILE, cfg["download_dir"], cfg["max_workers"],
    )

    code = args.func(cfg, args)
    log.info("команда %s завершена с кодом %s", args.cmd, code)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
