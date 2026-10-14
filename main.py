"""CLI для бэкапа Яндекс.Диска.

Команды:
    python main.py scan                 # обход дерева, заполнить state без скачивания
    python main.py download             # скачать всё pending
    python main.py download --workers 4 # с заданным числом потоков
    python main.py retry-failed         # сбросить failed → pending и скачать
    python main.py stats                # статистика по БД
    python main.py verify               # проверить md5 локальных файлов
    python main.py check                # проверить токен и доступ к диску
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
import time
from contextlib import closing
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

from downloader import (
    download_all,
    make_client,
    scan_media,
    scan_tree,
    token_state,
)
from state import State
from utils import (
    APP_VERSION,
    EXIT_AUTH,
    EXIT_OK,
    EXIT_OUTAGE,
    EXIT_PARTIAL,
    MAX_DISK_WORKERS,
    TELEGRAM_CHANNEL,
    YOUTUBE_VIDEO_URL,
    compute_md5,
    ensure_utf8_stdio,
    file_size_safe,
    human_bytes,
    human_duration,
    safe_int,
)

# Где лежит проект — все относительные пути считаем отсюда
PROJECT_DIR = Path(__file__).resolve().parent
LOG_DIR = PROJECT_DIR / "logs"
LOG_FILE = LOG_DIR / "backup.log"
DB_FILE = PROJECT_DIR / "state.db"

EMPTY_COUNTERS = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}

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


def _run_download(cfg: dict, state: State, workers: int) -> int:
    """Запускает download_all и печатает итог. Код выхода: 0 — всё ок, 1 — были ошибки, 130 — Ctrl+C."""
    print(f"Старт скачивания в {workers} потоков → {cfg['download_dir']}")
    t0 = time.time()
    counters = dict(EMPTY_COUNTERS)
    try:
        counters = download_all(
            token=cfg["token"],
            state=state,
            download_dir=cfg["download_dir"],
            workers=workers,
        )
    except KeyboardInterrupt:
        _print_summary(counters, time.time() - t0)
        print("\nПрервано пользователем. Прогресс сохранён в state.db.", flush=True)
        # Потоки пула не остановить посреди файла — они держат сокеты до конца
        # скачивания, и обычный выход ждал бы их минуты. Состояние в БД уже
        # согласовано (WAL), недокачанные .part уберутся при следующем запуске.
        logging.shutdown()
        os._exit(130)
    _print_summary(counters, time.time() - t0)
    return EXIT_OK if counters["failed"] == 0 else EXIT_PARTIAL


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
            print(f"Найдено: {self.n} ({self.label})", flush=True)

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
        else:
            print(f"Найдено: {self.n} ({self.label})", flush=True)


def _print_summary(counters: dict, elapsed: float) -> None:
    print(
        f"\nИтого: скачано {counters['downloaded']} ({human_bytes(counters['bytes'])}) | "
        f"пропущено {counters['skipped']} | упало {counters['failed']} | "
        f"время {human_duration(elapsed)}"
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

    roots = (
        [r.strip() for r in args.roots.split(",") if r.strip()]
        if args.roots
        else [args.root]
    )

    total_files = 0
    total_bytes = 0
    with State(str(DB_FILE)) as state, closing(make_client(cfg["token"])) as client:
        for root in roots:
            print(f"Обхожу {root}...")
            progress = _ScanProgress(f"Найдено [{root}]")
            try:
                files, bytes_ = scan_tree(
                    client=client,
                    state=state,
                    root=root,
                    on_file=progress.on_file,
                )
            finally:
                progress.close()
            print(f"  → {root}: {files} файлов, {human_bytes(bytes_)}")
            log.info("scan %s: %d файлов, %s", root, files, human_bytes(bytes_))
            total_files += files
            total_bytes += bytes_

    print(f"\nИтого: {total_files} файлов, {human_bytes(total_bytes)}")
    log.info("scan итого: %d файлов, %s; в БД %s", total_files, human_bytes(total_bytes), _stats_line())
    return 0


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


def cmd_download(cfg: dict, args: argparse.Namespace) -> int:
    if (code := _require_token(cfg)) is not None:
        return code

    with State(str(DB_FILE)) as state:
        if state.get_stats().get("pending", 0) == 0:
            print("В БД нет pending-файлов. Сначала выполни: python main.py scan")
            # Но всё-равно попробуем — может пользователь сначала запустил download
            if not args.no_scan:
                print("Запускаю scan автоматически (--no-scan чтобы отключить)...")
                with closing(make_client(cfg["token"])) as client:
                    scan_tree(client, state, root=args.root)

        return _run_download(cfg, state, args.workers or cfg["max_workers"])


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

    print("Статистика по БД state.db:")
    print(f"  Всего файлов:        {s['total']}")
    print(f"    pending:           {s['pending']}")
    print(f"    downloaded:        {s['downloaded']}")
    print(f"    skipped:           {s['skipped']}")
    print(f"    failed:            {s['failed']}")
    print(f"  Объём всего:         {human_bytes(s['bytes_total'])}")
    print(f"  Объём скачано/skip:  {human_bytes(s['bytes_downloaded'])}")
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
    p_scan.add_argument("--root", default="/", help="Корневой путь обхода (по умолчанию /)")
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
