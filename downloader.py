"""Рекурсивный обход Яндекс.Диска и многопоточное скачивание.

Архитектура:
* scan() — обходит дерево с корня `/`, регистрирует все файлы в БД как pending.
* download_all() — берёт pending из БД и скачивает в пул потоков. Каждый поток
  имеет свой `yadisk.Client` (синхронный клиент не thread-safe для шаринга).
* Resume — если локальный файл существует и совпадает по размеру → skipped.
* Корзина (`/Trash`) не трогается.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import yadisk
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

from state import State
from utils import (
    avoid_case_collision,
    ensure_dir,
    file_size_safe,
    resolve_local_path,
    to_long_path,
)

log = logging.getLogger("yadisk_backup")

# Скип-листы для путей которые НИКОГДА не качаем
SKIP_PATH_PREFIXES = ("disk:/Корзина", "disk:/Trash", "/Корзина", "/Trash")

# Какие исключения yadisk считаем "повторяемыми"
_RETRYABLE_EXC = (
    yadisk.exceptions.RetriableYaDiskError
    if hasattr(yadisk.exceptions, "RetriableYaDiskError")
    else yadisk.exceptions.YaDiskError,
)


# ── retry-обёртка для сетевых вызовов ───────────────────────────────────────


def _is_retryable(exc: BaseException) -> bool:
    """Решает, стоит ли retry-ить исключение."""
    # Эти точно не имеет смысла повторять
    non_retryable = (
        yadisk.exceptions.UnauthorizedError,
        yadisk.exceptions.ForbiddenError,
        yadisk.exceptions.PathNotFoundError,
    )
    if isinstance(exc, non_retryable):
        return False
    # 429 ловим отдельно — спим минуту
    if isinstance(exc, yadisk.exceptions.TooManyRequestsError):
        log.warning("Поймали 429 TooManyRequests — спим 60 секунд")
        time.sleep(60)
        return True
    if isinstance(exc, yadisk.exceptions.YaDiskError):
        return True
    if isinstance(exc, (OSError, ConnectionError, TimeoutError)):
        return True
    return False


_retry = retry(
    retry=retry_if_exception_type(
        (
            yadisk.exceptions.YaDiskError,
            OSError,
            ConnectionError,
            TimeoutError,
        )
    ),
    wait=wait_exponential(multiplier=1, min=1, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)


# ── обход дерева ────────────────────────────────────────────────────────────


def _normalize_remote(path: str) -> str:
    """Приводит путь к виду 'disk:/foo/bar' — так возвращает API."""
    if path.startswith("disk:"):
        return path
    if path.startswith("/"):
        return "disk:" + path
    return "disk:/" + path


def _is_skipped(path: str) -> bool:
    return any(path.startswith(p) for p in SKIP_PATH_PREFIXES)


def scan_tree(
    client: yadisk.Client,
    state: State,
    root: str = "/",
    on_file: Optional[Callable[[str, int], None]] = None,
    on_dir: Optional[Callable[[str], None]] = None,
) -> tuple[int, int]:
    """Обходит дерево начиная с root. Регистрирует файлы как pending.
    Возвращает (количество файлов, суммарный размер).
    """
    files_count = 0
    bytes_total = 0

    # Очередь путей для обхода — итеративный BFS, не плодим рекурсию на 10 уровней
    queue: list[str] = [root]

    while queue:
        cur = queue.pop()
        if _is_skipped(_normalize_remote(cur)):
            log.info("Пропускаю корзину: %s", cur)
            continue

        try:
            entries = _list_dir(client, cur)
        except yadisk.exceptions.PathNotFoundError:
            log.warning("Путь не найден: %s", cur)
            continue
        except yadisk.exceptions.UnauthorizedError:
            raise  # пробрасываем выше — токен битый
        except Exception as e:
            log.error("Не смог прочитать %s: %s", cur, e)
            continue

        for entry in entries:
            entry_path = entry.path  # вид 'disk:/foo/bar'
            if _is_skipped(entry_path):
                continue

            if entry.type == "dir":
                if on_dir:
                    on_dir(entry_path)
                queue.append(entry_path)
            elif entry.type == "file":
                state.mark_pending(
                    remote_path=entry_path,
                    size=int(entry.size) if entry.size is not None else None,
                    md5=getattr(entry, "md5", None),
                )
                files_count += 1
                if entry.size:
                    bytes_total += int(entry.size)
                if on_file:
                    on_file(entry_path, int(entry.size or 0))

    return files_count, bytes_total


@_retry
def _list_dir(client: yadisk.Client, path: str) -> list:
    """listdir с retry. Возвращает list ResourceObject."""
    # max_items=None → yadisk сам пагинирует
    return list(client.listdir(path, max_items=None))


# ── обход только медиа (фото/видео) через get_files ─────────────────────────


def scan_media(
    client: yadisk.Client,
    state: State,
    media_types: tuple[str, ...] = ("image", "video"),
    on_file: Optional[Callable[[str, int], None]] = None,
) -> tuple[int, int]:
    """Альтернатива scan_tree: использует client.get_files() с фильтром по типу.
    Возвращает все фото и видео со всего диска плоским списком.

    Это покрывает альбомы «Камера», «Видео», «Скриншоты» (это умные виды Яндекса
    над теми же файлами), но НЕ читает «Альбом семьи», «Люди на фото» и пр. —
    их API не отдаёт. Зато берёт все медиа из всех папок диска целиком.
    """
    files_count = 0
    bytes_total = 0

    for media_type in media_types:
        log.info("Запрашиваю media_type=%s...", media_type)
        for entry in _iter_files(client, media_type=media_type):
            entry_path = entry.path
            if _is_skipped(entry_path):
                continue
            if entry.type != "file":
                continue

            state.mark_pending(
                remote_path=entry_path,
                size=int(entry.size) if entry.size is not None else None,
                md5=getattr(entry, "md5", None),
            )
            files_count += 1
            if entry.size:
                bytes_total += int(entry.size)
            if on_file:
                on_file(entry_path, int(entry.size or 0))

    return files_count, bytes_total


@_retry
def _get_files_page(
    client: yadisk.Client,
    media_type: str,
    offset: int,
    limit: int,
) -> list:
    """Одна страница из get_files с retry."""
    return list(
        client.get_files(
            media_type=media_type,
            offset=offset,
            limit=limit,
        )
    )


def _iter_files(client: yadisk.Client, media_type: str, page_size: int = 200):
    """Итератор по всем файлам диска для заданного media_type.

    Сами пагинируем — yadisk.get_files() умеет итератор, но мы хотим
    обернуть КАЖДУЮ страницу в retry, а не одну общую попытку.
    """
    offset = 0
    while True:
        page = _get_files_page(client, media_type, offset, page_size)
        if not page:
            return
        for item in page:
            yield item
        if len(page) < page_size:
            return
        offset += page_size


# ── скачивание ──────────────────────────────────────────────────────────────


@dataclass
class DownloadJob:
    remote_path: str
    size: Optional[int]
    md5: Optional[str]


@dataclass
class DownloadResult:
    job: DownloadJob
    status: str  # downloaded | skipped | failed
    local_path: Optional[str]
    bytes_written: int
    error: Optional[str]
    duration_ms: int


def _ensure_local_dirs(local_path: str) -> None:
    parent = os.path.dirname(local_path)
    if parent:
        ensure_dir(to_long_path(parent))


@_retry
def _download_with_retry(client: yadisk.Client, remote: str, local: str) -> None:
    """Низкоуровневый вызов download с retry."""
    client.download(remote, to_long_path(local))


def _download_one(
    job: DownloadJob,
    client: yadisk.Client,
    download_dir: str,
) -> DownloadResult:
    start = time.monotonic()
    local_path = resolve_local_path(download_dir, job.remote_path)
    local_path = avoid_case_collision(local_path)
    _ensure_local_dirs(local_path)

    # Resume-логика: если файл уже скачан корректно — skipped
    existing_size = file_size_safe(local_path)
    if (
        existing_size is not None
        and job.size is not None
        and existing_size == job.size
    ):
        dur = int((time.monotonic() - start) * 1000)
        return DownloadResult(
            job=job,
            status="skipped",
            local_path=local_path,
            bytes_written=0,
            error=None,
            duration_ms=dur,
        )

    # Если файл частично скачан или с другим размером — перезаписываем
    if existing_size is not None:
        try:
            os.remove(to_long_path(local_path))
        except OSError:
            pass

    try:
        _download_with_retry(client, job.remote_path, local_path)
    except yadisk.exceptions.TooManyRequestsError as e:
        # Уже спали в _is_retryable, но если tenacity сдался — фиксируем как fail
        dur = int((time.monotonic() - start) * 1000)
        return DownloadResult(job, "failed", None, 0, f"429 TooManyRequests: {e}", dur)
    except yadisk.exceptions.PathNotFoundError as e:
        dur = int((time.monotonic() - start) * 1000)
        return DownloadResult(job, "failed", None, 0, f"PathNotFound: {e}", dur)
    except Exception as e:
        dur = int((time.monotonic() - start) * 1000)
        return DownloadResult(job, "failed", None, 0, f"{type(e).__name__}: {e}", dur)

    bytes_written = file_size_safe(local_path) or 0
    dur = int((time.monotonic() - start) * 1000)
    return DownloadResult(
        job=job,
        status="downloaded",
        local_path=local_path,
        bytes_written=bytes_written,
        error=None,
        duration_ms=dur,
    )


# Каждому потоку — свой клиент. Шарить yadisk.Client между потоками небезопасно.
_thread_local = threading.local()


def _get_thread_client(token: str) -> yadisk.Client:
    cli = getattr(_thread_local, "client", None)
    if cli is None:
        cli = yadisk.Client(token=token)
        _thread_local.client = cli
    return cli


def _close_thread_client() -> None:
    cli = getattr(_thread_local, "client", None)
    if cli is not None:
        try:
            cli.close()
        except Exception:
            pass
        _thread_local.client = None


def download_all(
    token: str,
    state: State,
    download_dir: str,
    workers: int = 4,
    jobs: Optional[Iterator[DownloadJob]] = None,
) -> dict[str, int]:
    """Скачивает все pending файлы из state в пул потоков.
    Возвращает счётчики: {downloaded, skipped, failed, bytes}.
    """
    if workers < 1:
        workers = 1
    if workers > 5:
        log.warning("workers=%d > 5 — Яндекс будет резать по rate limit. Снижаю до 5.", workers)
        workers = 5

    ensure_dir(download_dir)

    # Собираем список jobs из БД (или из переданного итератора — для retry-failed)
    if jobs is None:
        jobs_list = [
            DownloadJob(
                remote_path=row["remote_path"],
                size=row["size"],
                md5=row["md5"],
            )
            for row in state.get_pending()
        ]
    else:
        jobs_list = list(jobs)

    if not jobs_list:
        log.info("Нет файлов для скачивания.")
        return {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}

    total_bytes = sum(j.size for j in jobs_list if j.size)
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}

    file_bar = tqdm(total=len(jobs_list), unit="file", desc="Файлы", position=0)
    byte_bar = tqdm(
        total=total_bytes,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc="Байты",
        position=1,
    )

    def worker(job: DownloadJob) -> DownloadResult:
        client = _get_thread_client(token)
        return _download_one(job, client, download_dir)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(worker, j): j for j in jobs_list}
            try:
                for fut in as_completed(futures):
                    res = fut.result()
                    _apply_result(state, res, counters, file_bar, byte_bar)
            except KeyboardInterrupt:
                log.warning("Ctrl+C — отменяю оставшиеся задачи и закрываю пул...")
                # Отменяем то что ещё не стартовало
                for f in futures:
                    f.cancel()
                raise
    finally:
        file_bar.close()
        byte_bar.close()
        # Каждый поток должен закрыть свой клиент. Через ThreadPoolExecutor это
        # не сделаешь напрямую, но ThreadPoolExecutor умирает вместе с воркерами.

    return counters


def _apply_result(
    state: State,
    res: DownloadResult,
    counters: dict[str, int],
    file_bar: tqdm,
    byte_bar: tqdm,
) -> None:
    """Применяет результат скачивания к БД и счётчикам."""
    if res.status == "downloaded":
        state.mark_downloaded(res.job.remote_path, res.local_path or "")
        counters["downloaded"] += 1
        counters["bytes"] += res.bytes_written
        byte_bar.update(res.bytes_written)
        log.info(
            "OK | %s | %d B | %d ms | %s",
            res.status,
            res.bytes_written,
            res.duration_ms,
            res.job.remote_path,
        )
    elif res.status == "skipped":
        state.mark_skipped(res.job.remote_path, res.local_path or "")
        counters["skipped"] += 1
        if res.job.size:
            byte_bar.update(res.job.size)
        log.info(
            "SKIP | already exists | %s",
            res.job.remote_path,
        )
    else:
        state.mark_failed(res.job.remote_path, res.error or "unknown")
        counters["failed"] += 1
        log.error(
            "FAIL | %s | %s | %s",
            res.error,
            res.duration_ms,
            res.job.remote_path,
        )
    file_bar.update(1)


# ── проверка токена ─────────────────────────────────────────────────────────


def check_token(token: str) -> tuple[bool, str]:
    """Проверяет, валиден ли токен. Возвращает (ok, message)."""
    if not token or token.strip() == "" or token.startswith("AQAAA_paste"):
        return False, (
            "Токен не задан. Открой https://yandex.ru/dev/disk/poligon/, "
            "получи OAuth-токен по кнопке справа вверху и положи его в .env "
            "как YA_DISK_TOKEN=..."
        )
    try:
        cli = yadisk.Client(token=token)
        if not cli.check_token():
            return False, (
                "Токен невалиден (Яндекс ответил, что он не работает). "
                "Получи новый на https://yandex.ru/dev/disk/poligon/"
            )
        info = cli.get_disk_info()
        used = getattr(info, "used_space", 0) or 0
        total = getattr(info, "total_space", 0) or 0
        cli.close()
        return True, f"Токен OK. Использовано {used / 1e9:.2f} ГБ из {total / 1e9:.2f} ГБ."
    except yadisk.exceptions.UnauthorizedError:
        return False, (
            "Токен не принят (401 Unauthorized). Получи новый на "
            "https://yandex.ru/dev/disk/poligon/"
        )
    except Exception as e:
        return False, f"Не удалось проверить токен: {type(e).__name__}: {e}"
