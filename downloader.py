"""Рекурсивный обход Яндекс.Диска и многопоточное скачивание.

Архитектура:
* scan_tree() / scan_all_files() / scan_media() — регистрируют файлы в БД как pending.
  Для корня диска используется плоский get_files() (одна страница = 200 файлов),
  обход папок — запасной вариант.
* download_all() — берёт pending из БД и скачивает в пул потоков. Каждый поток
  имеет свой `yadisk.Client` (синхронный клиент не thread-safe для шаринга).
* Resume — если локальный файл существует и совпадает по размеру (и MD5) → skipped.
* Корзина (`/Trash`) не трогается.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

import yadisk
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

from state import State
from utils import (
    MAX_DISK_WORKERS,
    TELEGRAM_CHANNEL,
    avoid_case_collision,
    classify_yandex_error,
    compute_md5,
    ensure_dir,
    file_size_safe,
    human_bytes,
    is_local_disk_error,
    remove_quiet,
    resolve_local_path,
    to_long_path,
)

log = logging.getLogger("yadisk_backup")

# Скип-листы для путей которые НИКОГДА не качаем (полное совпадение или как родительская папка)
SKIP_PATH_PREFIXES = ("disk:/Корзина", "disk:/Trash")

# Сколько файлов берём за один запрос get_files и кладём в БД одной пачкой
SCAN_PAGE_SIZE = 200
# Как часто печатать строку прогресса, когда вывод не терминал (Web UI)
PLAIN_PROGRESS_EVERY_SEC = 5.0

OnFile = Callable[[str, int], None]


# ── клиент и retry-политика ─────────────────────────────────────────────────


# Ошибки, повторять которые бессмысленно: токен не станет валидным,
# а удалённый файл не появится, сколько ни жди.
NON_RETRYABLE = (
    yadisk.exceptions.UnauthorizedError,
    yadisk.exceptions.ForbiddenError,
    yadisk.exceptions.PathNotFoundError,
)
# Сбои на стороне Яндекса (50x) — короткая фиксированная пауза
SERVER_ERRORS = (
    yadisk.exceptions.InternalServerError,
    yadisk.exceptions.BadGatewayError,
    yadisk.exceptions.GatewayTimeoutError,
    yadisk.exceptions.UnavailableError,
)
# Таймауты: yadisk заворачивает сетевые таймауты в свой RequestTimeoutError
TIMEOUT_ERRORS = (TimeoutError, yadisk.exceptions.RequestTimeoutError)

# Пауза при 429 — Яндекс просит подождать, экспонента с 1 секунды тут не поможет
RATE_LIMIT_WAIT_SEC = 60.0
SERVER_ERROR_WAIT_SEC = 5.0
# (connect 30s, read 300s — защита от Read timed out на медленной сети и больших файлах)
DEFAULT_TIMEOUT = (30.0, 300.0)


def make_client(token: str) -> yadisk.Client:
    """Клиент REST API с единым таймаутом. Закрывать через .close() или contextlib.closing."""
    return yadisk.Client(token=token, default_args={"timeout": DEFAULT_TIMEOUT})


def account_info(client: yadisk.Client) -> dict:
    """Всё полезное, что API Яндекса сообщает об аккаунте: логин, место, безлимит, корзина, тариф."""
    info = client.get_disk_info()
    user = getattr(info, "user", None)

    def g(obj, name, default=None):
        v = getattr(obj, name, None) if obj is not None else None
        return default if v is None else v

    used = int(g(info, "used_space", 0))
    total = int(g(info, "total_space", 0))
    reg = g(user, "reg_time") or g(info, "reg_time")
    return {
        "login": g(user, "login", "?"),
        "display_name": g(user, "display_name", ""),
        "uid": str(g(user, "uid", "")),
        "country": g(user, "country", ""),
        "used": used,
        "total": total,
        "free": max(0, total - used),
        "trash": int(g(info, "trash_size", 0)),
        "photo_unlim": int(g(info, "photounlim_size", 0)),
        "is_paid": bool(g(info, "is_paid", False)),
        "unlimited_autoupload_enabled": bool(g(info, "unlimited_autoupload_enabled", False)),
        "max_file_size": int(g(info, "max_file_size", 0)),
        "reg_time": reg.isoformat() if hasattr(reg, "isoformat") else (str(reg) if reg else None),
        "photo_unlim_until": g(info, "free_photounlim_end_date"),
    }


def describe_account(acc: dict) -> str:
    """Одна строка про аккаунт — для консоли и логов."""
    name = f" ({acc['display_name']})" if acc.get("display_name") else ""
    tariff = "платный" if acc.get("is_paid") else "бесплатный"
    return (
        f"Аккаунт {acc.get('login', '?')}{name}: занято {human_bytes(acc.get('used', 0))} "
        f"из {human_bytes(acc.get('total', 0))}, безлимит {human_bytes(acc.get('photo_unlim', 0))}, "
        f"корзина {human_bytes(acc.get('trash', 0))}, {tariff} тариф"
    )


def api_error_details(e: BaseException) -> str:
    """Что ещё сообщил API: код ошибки yadisk и HTTP-статус. Пусто, если ничего."""
    parts = []
    error_type = getattr(e, "error_type", None)
    if error_type:
        parts.append(f"error_type={error_type}")
    status = getattr(getattr(e, "response", None), "status", None)
    if status:
        parts.append(f"http={status}")
    return f" [{', '.join(parts)}]" if parts else ""


def _is_retryable(exc: BaseException) -> bool:
    """Решает, стоит ли retry-ить исключение."""
    if isinstance(exc, NON_RETRYABLE) or is_local_disk_error(exc):
        return False
    if isinstance(exc, yadisk.exceptions.YaDiskError):
        return True
    return isinstance(exc, OSError)  # ConnectionError и TimeoutError — его подклассы


_wait_exponential = wait_exponential(multiplier=1, min=1, max=60)


def _wait_policy(retry_state) -> float:
    """429 → ждём минуту, 50x ошибки хранилища Яндекса → 5 сек, остальное → экспонента."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, yadisk.exceptions.TooManyRequestsError):
        log.warning(
            "Яндекс вернул 429 TooManyRequests (лимит запросов). Жду %.0f сек... Новости: %s",
            RATE_LIMIT_WAIT_SEC,
            TELEGRAM_CHANNEL,
        )
        return RATE_LIMIT_WAIT_SEC
    if isinstance(exc, SERVER_ERRORS):
        log.warning(
            "Сбой серверов Яндекса (%s, дата-центр). Проблема на стороне Яндекса. Пауза %.0f сек... Статус: %s",
            type(exc).__name__,
            SERVER_ERROR_WAIT_SEC,
            TELEGRAM_CHANNEL,
        )
        return SERVER_ERROR_WAIT_SEC
    return _wait_exponential(retry_state)


_retry = retry(
    retry=retry_if_exception(_is_retryable),
    wait=_wait_policy,
    stop=stop_after_attempt(7),
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
    """Проверяет, лежит ли путь в корзине (не задевая папки вроде Корзина2)."""
    norm = _normalize_remote(path)
    for p in SKIP_PATH_PREFIXES:
        if norm == p or norm.startswith(p + "/"):
            return True
    return False


def _register_file(entry, batch: list, on_file: OnFile | None) -> int:
    """Кладёт файл API в пачку для БД и дёргает колбэк. Возвращает размер в байтах."""
    size = int(entry.size) if entry.size is not None else None
    batch.append((entry.path, size, getattr(entry, "md5", None)))
    if on_file:
        on_file(entry.path, size or 0)
    return size or 0


def scan_tree(
    client: yadisk.Client,
    state: State,
    root: str = "/",
    on_file: OnFile | None = None,
    on_dir: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Обходит дерево начиная с root. Регистрирует файлы как pending.
    Возвращает (количество файлов, суммарный размер).
    Для корня диска использует быстрый плоский scan_all_files.
    """
    if _normalize_remote(root) in ("disk:/", "disk:"):
        try:
            return scan_all_files(client, state, on_file=on_file)
        except Exception as e:
            log.warning("get_files недоступен (%s), переключаюсь на обход дерева папок", e)

    files_count = 0
    bytes_total = 0
    queue: deque[str] = deque([root])  # BFS

    while queue:
        cur = queue.popleft()
        if _is_skipped(cur):
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

        dir_files: list = []
        for entry in entries:
            if _is_skipped(entry.path):  # вид 'disk:/foo/bar'
                continue
            if entry.type == "dir":
                if on_dir:
                    on_dir(entry.path)
                queue.append(entry.path)
            elif entry.type == "file":
                files_count += 1
                bytes_total += _register_file(entry, dir_files, on_file)

        if dir_files:
            state.mark_pending_batch(dir_files)

    return files_count, bytes_total


@_retry
def _list_dir(client: yadisk.Client, path: str) -> list:
    """listdir с retry. Возвращает list ResourceObject."""
    # max_items=None → yadisk сам пагинирует
    return list(client.listdir(path, max_items=None))


# ── плоский обход через get_files ───────────────────────────────────────────


@_retry
def _get_files_page(
    client: yadisk.Client,
    media_type: str | None,
    offset: int,
    limit: int,
) -> list:
    """Одна страница из get_files с retry.

    max_items обязателен: без него yadisk сам пагинирует до конца диска и
    возвращает ВЕСЬ хвост от offset — при ручной пагинации это O(N²) запросов.
    """
    kwargs = {"offset": offset, "limit": limit, "max_items": limit}
    if media_type:
        kwargs["media_type"] = media_type
    return list(client.get_files(**kwargs))


def _iter_files(client: yadisk.Client, media_type: str | None = None, page_size: int = SCAN_PAGE_SIZE):
    """Итератор по файлам диска для заданного media_type (или всех файлов, если media_type=None).

    Сами пагинируем — yadisk.get_files() умеет итератор, но мы хотим
    обернуть КАЖДУЮ страницу в retry, а не одну общую попытку.
    """
    offset = 0
    while True:
        page = _get_files_page(client, media_type, offset, page_size)
        if not page:
            return
        yield from page
        if len(page) < page_size:
            return
        offset += page_size


def _scan_flat(
    client: yadisk.Client,
    state: State,
    media_type: str | None,
    on_file: OnFile | None,
) -> tuple[int, int]:
    """Плоский обход get_files() с регистрацией в БД пачками по SCAN_PAGE_SIZE."""
    files_count = 0
    bytes_total = 0
    batch: list = []

    for entry in _iter_files(client, media_type=media_type):
        if entry.type != "file" or _is_skipped(entry.path):
            continue
        files_count += 1
        bytes_total += _register_file(entry, batch, on_file)
        if len(batch) >= SCAN_PAGE_SIZE:
            state.mark_pending_batch(batch)
            batch.clear()

    if batch:
        state.mark_pending_batch(batch)

    return files_count, bytes_total


def scan_media(
    client: yadisk.Client,
    state: State,
    media_types: tuple[str, ...] = ("image", "video"),
    on_file: OnFile | None = None,
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
        n, b = _scan_flat(client, state, media_type, on_file)
        files_count += n
        bytes_total += b
    return files_count, bytes_total


def scan_all_files(
    client: yadisk.Client,
    state: State,
    on_file: OnFile | None = None,
) -> tuple[int, int]:
    """Быстрое сканирование всего диска плоским get_files() вместо рекурсии по папкам."""
    return _scan_flat(client, state, None, on_file)


# ── скачивание ──────────────────────────────────────────────────────────────


@dataclass
class DownloadJob:
    remote_path: str
    size: int | None
    md5: str | None
    local_path: str | None = None


@dataclass
class DownloadResult:
    job: DownloadJob
    status: str  # downloaded | skipped | failed
    local_path: str | None
    bytes_written: int
    error: str | None
    duration_ms: int


def _ensure_local_dirs(local_path: str) -> None:
    parent = os.path.dirname(local_path)
    if parent:
        ensure_dir(to_long_path(parent))


@_retry
def _download_with_retry(client: yadisk.Client, remote: str, local: str) -> None:
    """Низкоуровневый вызов download с retry (таймаут берётся из default_args клиента)."""
    client.download(remote, to_long_path(local))


def _describe_download_error(e: BaseException) -> str:
    """Текст ошибки для БД и логов. Префикс до ' | ' — категория, её читает диагностика Web UI."""
    name = type(e).__name__
    api = api_error_details(e)
    if is_local_disk_error(e):
        # Проверяем ПЕРВЫМ: текст OSError содержит путь, а в пути бывает «500» или «notfound»
        return f"local_disk | {name}: {e} [ПРОБЛЕМА ЛОКАЛЬНОГО ДИСКА: место, права или отключённый диск]"
    if isinstance(e, yadisk.exceptions.TooManyRequestsError):
        return f"rate_limit | 429 TooManyRequests (лимит Яндекса): {e}{api}"
    if isinstance(e, yadisk.exceptions.PathNotFoundError):
        return f"PathNotFound: {e}{api}"
    if isinstance(e, (yadisk.exceptions.UnauthorizedError, yadisk.exceptions.ForbiddenError)):
        return f"auth_expired | {name}: {e}{api}"
    if isinstance(e, TIMEOUT_ERRORS):
        return f"download_timeout | {name}: {e}{api}"
    if classify_yandex_error(e)["is_yandex_server"]:
        return f"server_50x | {name} [СБОЙ СЕРВЕРА ЯНДЕКСА]: {e}{api}"
    return f"{name}: {e}{api}"


def _already_downloaded(job: DownloadJob, local_path: str) -> bool:
    """Resume: локальный файл того же размера (и MD5, если API его отдал) — качать не надо."""
    existing_size = file_size_safe(local_path)
    if existing_size is None or job.size is None or existing_size != job.size:
        return False
    if not job.md5:
        return True
    try:
        actual_md5 = compute_md5(local_path)
    except OSError as e:
        log.warning("Не смог прочитать %s для проверки MD5 (%s). Перекачиваю.", local_path, e)
        return False
    if actual_md5.lower() == job.md5.lower():
        return True
    log.warning(
        "MD5 mismatch для %s (локальный=%s, удалённый=%s). Перекачиваю.",
        local_path, actual_md5, job.md5,
    )
    return False


def _download_one(
    job: DownloadJob,
    client: yadisk.Client,
    download_dir: str,
) -> DownloadResult:
    start = time.monotonic()

    def finish(status: str, local: str | None = None, bytes_written: int = 0,
               error: str | None = None) -> DownloadResult:
        return DownloadResult(job, status, local, bytes_written, error,
                              int((time.monotonic() - start) * 1000))

    part_path: str | None = None
    try:
        local_path = job.local_path or resolve_local_path(download_dir, job.remote_path)
        local_path = avoid_case_collision(local_path)
        _ensure_local_dirs(local_path)

        if _already_downloaded(job, local_path):
            return finish("skipped", local_path)

        # Качаем во временный .part и атомарно переименовываем — недокачанный файл
        # никогда не выглядит как готовый.
        part_path = to_long_path(local_path + ".part")
        remove_quiet(part_path)
        _download_with_retry(client, job.remote_path, part_path)
        os.replace(part_path, to_long_path(local_path))
    except Exception as e:
        # Любая ошибка (сеть, Яндекс, локальный диск, makedirs) — это failed-результат,
        # а не исключение: один битый файл не должен ронять весь download_all.
        if part_path:
            remove_quiet(part_path)
        return finish("failed", error=_describe_download_error(e))

    return finish("downloaded", local_path, file_size_safe(local_path) or 0)


# Каждому потоку — свой клиент. Шарить yadisk.Client между потоками небезопасно.
_thread_local = threading.local()
# Реестр всех созданных клиентов — чтобы закрыть сокеты после остановки пула
_all_clients: list[yadisk.Client] = []
_all_clients_lock = threading.Lock()


def _get_thread_client(token: str) -> yadisk.Client:
    cli = getattr(_thread_local, "client", None)
    if cli is None:
        cli = make_client(token)
        _thread_local.client = cli
        with _all_clients_lock:
            _all_clients.append(cli)
    return cli


def _close_all_clients() -> None:
    """Закрывает клиентов всех воркеров. Вызывать ПОСЛЕ остановки пула —
    потоки уже мертвы, их thread-local исчез вместе с ними.
    """
    with _all_clients_lock:
        clients = list(_all_clients)
        _all_clients.clear()
    for cli in clients:
        try:
            cli.close()
        except Exception:  # noqa: BLE001 — закрытие не должно ронять выход
            pass


def _jobs_from_state(state: State) -> Iterator[DownloadJob]:
    for row in state.get_pending():
        yield DownloadJob(
            remote_path=row["remote_path"],
            size=row["size"],
            md5=row["md5"],
            local_path=row["local_path"] if "local_path" in row.keys() else None,
        )


def _job_source(
    state: State, jobs: Iterable[DownloadJob] | None,
) -> tuple[Iterator[DownloadJob], int | None, int | None]:
    """Поток задач + известные итоги (файлов, байт) для прогресс-баров, если их можно посчитать."""
    if jobs is None:
        pstats = state.get_pending_stats()
        return _jobs_from_state(state), pstats["count"], pstats["bytes"]
    if isinstance(jobs, (list, tuple)):
        return iter(jobs), len(jobs), sum(j.size for j in jobs if j.size)
    return iter(jobs), None, None


def download_all(
    token: str,
    state: State,
    download_dir: str,
    workers: int = 4,
    jobs: Iterable[DownloadJob] | None = None,
) -> dict[str, int]:
    """Скачивает pending файлы из state в пул потоков с ограниченной очередью в памяти.
    Возвращает счётчики: {downloaded, skipped, failed, bytes}.
    """
    workers = max(workers, 1)
    if workers > MAX_DISK_WORKERS:
        log.warning(
            "workers=%d > %d — Яндекс будет резать по rate limit. Снижаю до %d.",
            workers, MAX_DISK_WORKERS, MAX_DISK_WORKERS,
        )
        workers = MAX_DISK_WORKERS

    ensure_dir(download_dir)

    # Детерминированно сохраняем local_path в state, чтобы исключить коллизии между потоками
    state.assign_local_paths(download_dir)

    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
    jobs_iter, total_files, total_bytes = _job_source(state, jobs)
    if total_files == 0:
        log.info("Нет файлов для скачивания.")
        return counters
    started = time.monotonic()
    log.info(
        "Старт download: workers=%d | папка=%s | в очереди %s файлов, %s",
        workers, download_dir,
        "?" if total_files is None else total_files,
        "?" if total_bytes is None else human_bytes(total_bytes),
    )

    # В терминале — tqdm-бары; в subprocess под Web UI (не TTY) они превращаются
    # в мусор из \r и escape-кодов, поэтому там печатаем простую строку прогресса.
    is_tty = sys.stderr.isatty()
    file_bar = tqdm(total=total_files, unit="file", desc="Файлы", position=0, disable=not is_tty)
    byte_bar = tqdm(
        total=total_bytes,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc="Байты",
        position=1,
        disable=not is_tty,
    )
    last_plain_report = time.monotonic()

    def plain_report(force: bool = False) -> None:
        nonlocal last_plain_report
        if is_tty:
            return
        now = time.monotonic()
        if not force and now - last_plain_report < PLAIN_PROGRESS_EVERY_SEC:
            return
        last_plain_report = now
        done_n = counters["downloaded"] + counters["skipped"] + counters["failed"]
        total_str = f"/{total_files}" if total_files is not None else ""
        print(
            f"Прогресс: {done_n}{total_str} файлов | скачано {counters['downloaded']} "
            f"| пропущено {counters['skipped']} | ошибок {counters['failed']} "
            f"| {human_bytes(counters['bytes'])}",
            flush=True,
        )

    def worker(job: DownloadJob) -> DownloadResult:
        return _download_one(job, _get_thread_client(token), download_dir)

    _close_all_clients()
    pool = ThreadPoolExecutor(max_workers=workers)
    # Не более max_in_flight задач в памяти — генератор из БД читается лениво
    max_in_flight = max(workers * 2, 4)
    futures: dict = {}

    try:
        for job in jobs_iter:
            futures[pool.submit(worker, job)] = job
            if len(futures) >= max_in_flight:
                break

        if not futures:
            log.info("Нет файлов для скачивания.")
            pool.shutdown(wait=False)
            return counters

        try:
            while futures:
                # timeout обязателен: на Windows ожидание без таймаута не прерывается Ctrl+C,
                # пока не завершится очередной файл (хоть 4 ГБ)
                done, _ = wait(futures.keys(), timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    job = futures.pop(fut)
                    try:
                        res = fut.result()
                    except Exception as e:  # защитный барьер — _download_one сам ловит всё
                        res = DownloadResult(job, "failed", None, 0, _describe_download_error(e), 0)
                    _apply_result(state, res, counters, file_bar, byte_bar)
                    # Подкладываем следующую задачу из стрима
                    next_job = next(jobs_iter, None)
                    if next_job is not None:
                        futures[pool.submit(worker, next_job)] = next_job
                plain_report()
            plain_report(force=True)
        except KeyboardInterrupt:
            print("\nCtrl+C — отменяю очередь, дожидаюсь только текущих файлов...", file=sys.stderr)
            log.warning("Ctrl+C — отменяю оставшиеся задачи и закрываю пул...")
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
    finally:
        _close_all_clients()
        file_bar.close()
        byte_bar.close()
        elapsed = max(0.001, time.monotonic() - started)
        log.info(
            "Итог download: скачано %d (%s), пропущено %d, ошибок %d за %.0f с, средняя %s/с",
            counters["downloaded"], human_bytes(counters["bytes"]), counters["skipped"],
            counters["failed"], elapsed, human_bytes(counters["bytes"] / elapsed),
        )

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
        log.info("SKIP | already exists | %s", res.job.remote_path)
    else:
        state.mark_failed(res.job.remote_path, res.error or "unknown")
        counters["failed"] += 1
        diag = classify_yandex_error(res.error)
        if diag["is_yandex_server"]:
            log.warning(
                "FAIL (Сервер Яндекса) | %s | %s ms | %s | Инфо: %s | Канал: %s",
                res.error,
                res.duration_ms,
                res.job.remote_path,
                diag["message"],
                TELEGRAM_CHANNEL,
            )
        else:
            log.error("FAIL | %s | %s | %s", res.error, res.duration_ms, res.job.remote_path)
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
    cli = None
    try:
        cli = make_client(token)
        if not cli.check_token():
            return False, (
                "Токен невалиден (Яндекс ответил, что он не работает). "
                "Получи новый на https://yandex.ru/dev/disk/poligon/"
            )
        acc = account_info(cli)
        log.info("Токен OK. %s", describe_account(acc))
        return True, f"Токен OK. {describe_account(acc)}."
    except yadisk.exceptions.UnauthorizedError as e:
        return False, (
            "Токен не принят (401 Unauthorized). Получи новый на "
            f"https://yandex.ru/dev/disk/poligon/{api_error_details(e)}"
        )
    except Exception as e:
        return False, f"Не удалось проверить токен: {type(e).__name__}: {e}{api_error_details(e)}"
    finally:
        if cli is not None:
            try:
                cli.close()
            except Exception:
                pass
