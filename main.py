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
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

import yadisk
from downloader import (
    DownloadJob,
    check_token,
    download_all,
    scan_media,
    scan_tree,
)
from state import State
from utils import (
    compute_md5,
    file_size_safe,
    human_bytes,
    human_duration,
    to_long_path,
)

# Где лежит проект — все относительные пути считаем отсюда
PROJECT_DIR = Path(__file__).resolve().parent
LOG_DIR = PROJECT_DIR / "logs"
LOG_FILE = LOG_DIR / "backup.log"
DB_FILE = PROJECT_DIR / "state.db"


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
    root.setLevel(level.upper())
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
    try:
        max_workers = int(os.getenv("MAX_WORKERS", "4"))
    except ValueError:
        max_workers = 4
    log_level = os.getenv("LOG_LEVEL", "INFO").strip() or "INFO"

    return {
        "token": token,
        "download_dir": download_dir,
        "max_workers": max_workers,
        "log_level": log_level,
    }


# ── команды ─────────────────────────────────────────────────────────────────


def cmd_check(cfg: dict, args: argparse.Namespace) -> int:
    ok, msg = check_token(cfg["token"])
    print(msg)
    return 0 if ok else 2


def cmd_scan(cfg: dict, args: argparse.Namespace) -> int:
    ok, msg = check_token(cfg["token"])
    if not ok:
        print(msg, file=sys.stderr)
        return 2
    print(msg)

    state = State(str(DB_FILE))
    client = yadisk.Client(token=cfg["token"])

    roots = (
        [r.strip() for r in args.roots.split(",") if r.strip()]
        if args.roots
        else [args.root]
    )

    total_files = 0
    total_bytes = 0
    try:
        for root in roots:
            print(f"Обхожу {root}...")
            progress = tqdm(unit="file", desc=f"Найдено [{root}]", leave=False)

            def on_file(path: str, size: int) -> None:
                progress.update(1)

            files, bytes_ = scan_tree(
                client=client,
                state=state,
                root=root,
                on_file=on_file,
            )
            progress.close()
            print(f"  → {root}: {files} файлов, {human_bytes(bytes_)}")
            total_files += files
            total_bytes += bytes_
    finally:
        client.close()
        state.close()

    print(f"\nИтого: {total_files} файлов, {human_bytes(total_bytes)}")
    return 0


def cmd_scan_media(cfg: dict, args: argparse.Namespace) -> int:
    """Сканирует ТОЛЬКО фото и видео со всего диска (через media_type)."""
    ok, msg = check_token(cfg["token"])
    if not ok:
        print(msg, file=sys.stderr)
        return 2
    print(msg)

    media_types = tuple(t.strip() for t in args.types.split(",") if t.strip())
    print(f"Сканирую медиа: {', '.join(media_types)}")

    state = State(str(DB_FILE))
    client = yadisk.Client(token=cfg["token"])

    progress = tqdm(unit="file", desc="Найдено медиа")

    def on_file(path: str, size: int) -> None:
        progress.update(1)

    try:
        files, total_bytes = scan_media(
            client=client,
            state=state,
            media_types=media_types,
            on_file=on_file,
        )
    finally:
        progress.close()
        client.close()
        state.close()

    print(f"Найдено медиа-файлов: {files}")
    print(f"Суммарный объём: {human_bytes(total_bytes)}")
    return 0


def cmd_download(cfg: dict, args: argparse.Namespace) -> int:
    ok, msg = check_token(cfg["token"])
    if not ok:
        print(msg, file=sys.stderr)
        return 2
    print(msg)

    state = State(str(DB_FILE))
    pending_count = state.get_stats().get("pending", 0)

    # Если ничего pending — предложить сделать scan
    if pending_count == 0:
        print("В БД нет pending-файлов. Сначала выполни: python main.py scan")
        # Но всё-равно попробуем — может пользователь сначала запустил download
        if not args.no_scan:
            print("Запускаю scan автоматически (--no-scan чтобы отключить)...")
            client = yadisk.Client(token=cfg["token"])
            try:
                scan_tree(client, state, root=args.root)
            finally:
                client.close()

    workers = args.workers or cfg["max_workers"]
    print(f"Старт скачивания в {workers} потоков → {cfg['download_dir']}")

    t0 = time.time()
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
    try:
        counters = download_all(
            token=cfg["token"],
            state=state,
            download_dir=cfg["download_dir"],
            workers=workers,
        )
    except KeyboardInterrupt:
        print("\nПрервано пользователем. Прогресс сохранён в state.db.")
        state.close()
        return 130
    finally:
        elapsed = time.time() - t0
        _print_summary(counters, elapsed)
        state.close()

    return 0 if counters["failed"] == 0 else 1


def cmd_retry_failed(cfg: dict, args: argparse.Namespace) -> int:
    ok, msg = check_token(cfg["token"])
    if not ok:
        print(msg, file=sys.stderr)
        return 2

    state = State(str(DB_FILE))
    n = state.reset_failed_to_pending()
    print(f"Сброшено failed → pending: {n}")
    if n == 0:
        state.close()
        return 0

    workers = args.workers or cfg["max_workers"]
    t0 = time.time()
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
    try:
        counters = download_all(
            token=cfg["token"],
            state=state,
            download_dir=cfg["download_dir"],
            workers=workers,
        )
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        state.close()
        return 130
    finally:
        elapsed = time.time() - t0
        _print_summary(counters, elapsed)
        state.close()

    return 0 if counters["failed"] == 0 else 1


def cmd_stats(cfg: dict, args: argparse.Namespace) -> int:
    state = State(str(DB_FILE))
    s = state.get_stats()
    state.close()

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
    state = State(str(DB_FILE))
    rows = list(state.get_all_downloaded())
    if not rows:
        print("Нет скачанных файлов в БД для проверки.")
        state.close()
        return 0

    bad: list[tuple[str, str]] = []
    missing: list[str] = []
    size_mismatch: list[str] = []

    bar = tqdm(rows, desc="Проверка MD5", unit="file")
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
    state.close()

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


def _print_summary(counters: dict, elapsed: float) -> None:
    print(
        f"\nИтого: скачано {counters['downloaded']} ({human_bytes(counters['bytes'])}) | "
        f"пропущено {counters['skipped']} | упало {counters['failed']} | "
        f"время {human_duration(elapsed)}"
    )


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
        help="Сканировать только фото и видео (включая безлимитное фото-хранилище)",
    )
    p_scan_media.add_argument(
        "--types",
        default="image,video",
        help="Типы медиа через запятую: image,video (по умолчанию)",
    )
    p_scan_media.set_defaults(func=cmd_scan_media)

    p_dl = sub.add_parser("download", help="Скачать все pending файлы")
    p_dl.add_argument("--workers", type=int, default=None, help="Число потоков (макс 5)")
    p_dl.add_argument("--root", default="/", help="Корень для авто-scan если БД пуста")
    p_dl.add_argument("--no-scan", action="store_true", help="Не делать авто-scan если БД пуста")
    p_dl.set_defaults(func=cmd_download)

    p_retry = sub.add_parser("retry-failed", help="Перезапустить failed → pending")
    p_retry.add_argument("--workers", type=int, default=None)
    p_retry.set_defaults(func=cmd_retry_failed)

    p_stats = sub.add_parser("stats", help="Показать статистику по БД")
    p_stats.set_defaults(func=cmd_stats)

    p_verify = sub.add_parser("verify", help="Проверить MD5 скачанных файлов")
    p_verify.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # stats и verify читают только локальную БД — токен не нужен
    require_token = args.cmd not in ("stats", "verify")
    cfg = load_config(require_token=require_token)
    setup_logging(cfg["log_level"])

    return args.func(cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
