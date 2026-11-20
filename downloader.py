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
import sqlite3
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

import requests
import yadisk
from urllib.parse import urlsplit
from tqdm import tqdm

from disk_api import (  # noqa: F401 — реэкспорт для обратной совместимости
    BROWSE_CAP,
    BROWSE_TIMEOUT,
    DEFAULT_N_RETRIES,
    DEFAULT_TIMEOUT,
    LIST_N_RETRIES,
    NON_RETRYABLE,
    OVERDRAFT_DAYS,
    OVERDRAFT_ERROR,
    RATE_LIMIT_WAIT_SEC,
    SERVER_ERRORS,
    SERVER_ERROR_WAIT_SEC,
    TIMEOUT_ERRORS,
    FetchError,
    RepeatedPages,
    RootNotFound,
    StorageUnavailable,
    _FOLDER_DATE_FIELDS,
    _FOLDER_FIELDS,
    _created_ts,
    _entry_ts,
    _is_retryable,
    _modified_ts,
    _parent_remote_dir,
    _retry,
    _share_flags,
    _top_folder,
    _wait_policy,
    account_info,
    api_error_details,
    describe_account,
    is_overdraft,
    list_disk_folders,
    make_browse_client,
    make_client,
    overdraft_account,
    overdraft_message,
    scan_exit_code,
)
from file_dates import apply_folder_dates, restore_all, set_file_time
from state import DEFERRED_MARKER, State
from storage_fetch import DeadHosts, FetchFailure, fetch_preview, fetch_to_file, preview_failure_text
from utils import (
    APP_VERSION,
    MAX_DISK_WORKERS,
    SKIP_PATH_PREFIXES,  # noqa: F401 — реэкспорт: корзину никогда не качаем
    categorize,
    in_trash,
    local_error_kind,
    name_with_tag,
    part_path_for,
    path_key,
    preview_path_for,
    previews_enabled,
    TELEGRAM_CHANNEL,
    avoid_case_collision,
    classify_yandex_error,
    compute_md5,
    ensure_dir,
    file_size_safe,
    human_bytes,
    is_local_disk_error,
    remove_preview,
    remove_quiet,
    resolve_local_path,
    scrub_urls,
    short_hash,
    to_disk_path,
    to_long_path,
    too_big_for_folder,
)

log = logging.getLogger("yadisk_backup")

# Сколько файлов берём за один запрос get_files и кладём в БД одной пачкой
SCAN_PAGE_SIZE = 200
# Столько полных страниц подряд почти без новых файлов (меньше SCAN_MIN_FRESH_SHARE страницы) —
# плоский список повторяет присланное, и сбор переходит на обход папок
SCAN_STALE_PAGES = 5
SCAN_MIN_FRESH_SHARE = 0.1
# Как часто печатать строку прогресса, когда вывод не терминал (Web UI)
PLAIN_PROGRESS_EVERY_SEC = 5.0
# Сколько раз берём свежую ссылку, если сервер хранения оборвал файл или ответил 50x
FRESH_LINK_RETRIES = 2
# Столько ошибок локального диска подряд — дальше качать бессмысленно (код 4)
LOCAL_DISK_GIVE_UP = 10
# Столько ответов 401/403 подряд — перепроверяем сам токен (код 2, только если он мёртв)
AUTH_FAILS_BEFORE_RECHECK = 5

# Молчащие серверы хранения: все потоки download_all живут в одном процессе
DEAD_HOSTS = DeadHosts()

OnFile = Callable[[str, int], None]


# ── обход дерева (клиент и retry-политика вынесены в disk_api.py) ──────────


_normalize_remote = to_disk_path  # прежнее имя: 'disk:/foo/bar', как возвращает API
_is_skipped = in_trash  # прежнее имя: путь в корзине


# on_note(вид, имена): сбор списка сообщает о находках, которые стоит показать человеку
OnNote = Callable[[str, list[str]], None]


def _register_file(entry, batch: list, on_file: OnFile | None) -> int:
    """Кладёт файл API в пачку для БД и дёргает колбэк. Возвращает размер в байтах."""
    size = int(entry.size) if entry.size is not None else None
    batch.append((entry.path, size, getattr(entry, "md5", None), _modified_ts(entry), _created_ts(entry)))
    if on_file:
        on_file(entry.path, size or 0)
    return size or 0


def scan_tree(
    client: yadisk.Client,
    state: State,
    root: str = "/",
    on_file: OnFile | None = None,
    on_dir: Callable[[str], None] | None = None,
    on_note: OnNote | None = None,
) -> tuple[int, int]:
    """Обходит дерево начиная с root. Регистрирует файлы как pending.
    Возвращает (количество файлов, суммарный размер).

    Для корня диска использует быстрый плоский scan_all_files, а потом обходит
    общие папки, которые открыли этому аккаунту, если их файлов в плоском списке
    не оказалось. Выбранной папки нет на Диске — RootNotFound.
    """
    if to_disk_path(root) in ("disk:/", "disk:"):
        seen_top: set[str] = set()
        listed: set[str] = set()
        try:
            files, total = scan_all_files(client, state, on_file=on_file, seen_top=seen_top, known=listed)
        except (yadisk.exceptions.UnauthorizedError, sqlite3.Error):
            raise
        except Exception as e:
            if is_overdraft(e):
                raise  # папки ответят тем же: Диск переполнен
            log.warning("get_files недоступен (%s), переключаюсь на обход дерева папок", e)
        else:
            listed.clear()  # больше не нужен, а на большом Диске это сотни мегабайт
            more_files, more_bytes = _scan_shared_folders(client, state, seen_top, on_file, on_dir, on_note)
            collect_folder_dates(client, state)
            return files + more_files, total + more_bytes
        return _walk(client, state, root, _new_files_only(on_file, listed), on_dir, on_note=on_note)
    return _walk(client, state, root, on_file, on_dir, strict_root=True, on_note=on_note)


def _new_files_only(on_file: OnFile | None, counted: set[str]) -> OnFile | None:
    """on_file, который молчит о файлах из counted.

    Обход папок после оборванного плоского списка проходит и уже посчитанные файлы:
    без этого «Найдено» показало бы их дважды.
    """
    if on_file is None or not counted:
        return on_file

    def notify(path: str, size: int) -> None:
        if path not in counted:
            on_file(path, size)

    return notify


def _walk(
    client: yadisk.Client,
    state: State,
    root: str,
    on_file: OnFile | None,
    on_dir: Callable[[str], None] | None,
    strict_root: bool = False,
    on_note: OnNote | None = None,
) -> tuple[int, int]:
    """Обход папок в ширину от root. strict_root — пропавший root это RootNotFound, а не пропуск.

    Папка, которую не удалось прочитать и после повторов, уходит в on_note("unreadable_folders"):
    список без неё неполный, и scan не должен закончиться кодом 0.
    """
    files_count = 0
    bytes_total = 0
    queue: deque[str] = deque([root])  # BFS
    first = True

    while queue:
        cur = queue.popleft()
        is_root, first = first, False
        if _is_skipped(cur):
            log.info("Пропускаю корзину: %s", cur)
            continue

        try:
            entries = _list_dir(client, cur)
        except (yadisk.exceptions.PathNotFoundError, yadisk.exceptions.WrongResourceTypeError):
            if is_root and strict_root:
                raise RootNotFound(to_disk_path(root).rstrip("/")) from None
            log.warning("Путь не найден: %s", cur)
            continue
        except yadisk.exceptions.UnauthorizedError:
            raise  # пробрасываем выше — токен битый
        except yadisk.exceptions.ForbiddenError as e:
            if is_overdraft(e):
                raise
            # Отозванный доступ к отдельной общей папке — не сбой Яндекса, повтор не поможет
            log.warning("Нет доступа к папке %s (403): %s — пропускаю", cur, e)
            continue
        except Exception as e:
            if is_overdraft(e):
                # Не «папка не прочиталась»: так непрочитанным стал бы весь Диск, а сбор — кодом 3
                # «Яндекс не отвечает» по кругу каждые 30 минут
                raise
            log.error("Не смог прочитать %s: %s", cur, e)
            if on_note:
                on_note("unreadable_folders", [cur])
            continue

        dir_files: list = []
        dir_folders: list[tuple[str, float | None, float | None]] = []
        for entry in entries:
            if _is_skipped(entry.path):  # вид 'disk:/foo/bar'
                continue
            if entry.type == "dir":
                if on_dir:
                    on_dir(entry.path)
                dir_folders.append((to_disk_path(entry.path).rstrip("/"),
                                    _created_ts(entry), _modified_ts(entry)))
                queue.append(entry.path)
            elif entry.type == "file":
                files_count += 1
                bytes_total += _register_file(entry, dir_files, on_file)

        if dir_files:
            state.mark_pending_batch(dir_files)
        if dir_folders and hasattr(state, "save_folders_batch"):
            state.save_folders_batch(dir_folders)

    return files_count, bytes_total


@_retry
def _list_dir(client: yadisk.Client, path: str) -> list:
    """listdir с retry. Возвращает list ResourceObject."""
    # max_items=None → yadisk сам пагинирует; сбой страницы он повторяет сам, не с начала папки
    return list(client.listdir(path, max_items=None, n_retries=LIST_N_RETRIES))


# ── общие папки и дерево папок для Настроек ─────────────────────────────────

# За один проход после плоского сканирования дозапрашиваем даты не более чем для 500 родительских папок;
# остальные папки получают даты из запасного расчёта по файлам (derive_folder_dates_from_files)
FOLDER_DATES_MAX_PARENTS = 500


def collect_folder_dates(
    client: yadisk.Client,
    state: State,
    max_parents: int = FOLDER_DATES_MAX_PARENTS,
) -> int:
    """Дособирает точные даты (created, modified) папок Диска после плоского get_files().

    Одно обращение к родителю (listdir с минимальным набором fields) отдаёт даты всех его подпапок.
    Ошибки сети/лимита не фатальны: для оставшихся папок есть запасной расчёт по датам файлов.
    """
    if not (hasattr(state, "unique_parent_folders") and hasattr(state, "known_folder_paths")
            and hasattr(state, "save_folders_batch")):
        return 0
    all_dirs = state.unique_parent_folders()
    if not all_dirs:
        return 0
    known = state.known_folder_paths()
    missing = all_dirs - known
    if not missing:
        return 0
    parents = sorted({_parent_remote_dir(d) for d in missing})[:max_parents]
    saved = 0
    batch: list[tuple[str, float | None, float | None]] = []
    for parent in parents:
        try:
            entries = list(client.listdir(parent, max_items=None, fields=_FOLDER_DATE_FIELDS,
                                          n_retries=LIST_N_RETRIES))
        except yadisk.exceptions.UnauthorizedError:
            raise
        except Exception as e:  # noqa: BLE001 — сбор дат папок не должен ронять скан файлов
            if is_overdraft(e):
                break
            log.debug("даты папок: пропуск %s (%s)", parent, e)
            continue
        for entry in entries:
            if getattr(entry, "type", None) != "dir" or _is_skipped(entry.path):
                continue
            cr, mod = _created_ts(entry), _modified_ts(entry)
            if cr is not None or mod is not None:
                batch.append((to_disk_path(entry.path).rstrip("/"), cr, mod))
        if len(batch) >= 200:
            state.save_folders_batch(batch)
            saved += len(batch)
            batch.clear()
    if batch:
        state.save_folders_batch(batch)
        saved += len(batch)
    return saved


@_retry
def _list_root_with_share(client: yadisk.Client) -> list:
    """Корень Диска с признаком общего доступа (share есть только у общих папок)."""
    return list(client.listdir("disk:/", max_items=None, fields=_FOLDER_FIELDS, n_retries=LIST_N_RETRIES))


def shared_with_me_dirs(client: yadisk.Client) -> list:
    """Папки в корне Диска, к которым этому аккаунту дал доступ другой человек."""
    out = []
    for entry in _list_root_with_share(client):
        shared, owned = _share_flags(entry)
        # owned is None — Яндекс не сказал, чья папка: не рискуем лишним обходом
        if entry.type == "dir" and shared and owned is False:
            out.append(entry)
    return out


def _scan_shared_folders(
    client: yadisk.Client,
    state: State,
    seen_top: set[str],
    on_file: OnFile | None,
    on_dir: Callable[[str], None] | None,
    on_note: OnNote | None,
) -> tuple[int, int]:
    """Обходит общие папки, которых не оказалось в плоском списке get_files.

    Документация Яндекса не говорит, попадают ли файлы чужих общих папок в плоский
    список. Если попадают — их верхняя папка уже в seen_top и лишнего обхода нет.
    """
    try:
        root_entries = _list_root_with_share(client)
    except yadisk.exceptions.UnauthorizedError:
        raise
    except Exception as e:  # noqa: BLE001 — список уже собран, это только дополнение
        log.warning("Не смог проверить общие папки в корне Диска: %s", e)
        return 0, 0
    # Попутно сохраняем даты всех корневых папок Диска — без единого лишнего запроса
    if hasattr(state, "save_folders_batch"):
        top_folders = [
            (to_disk_path(e.path).rstrip("/"), _created_ts(e), _modified_ts(e))
            for e in root_entries
            if getattr(e, "type", None) == "dir" and not _is_skipped(e.path)
            and (_created_ts(e) is not None or _modified_ts(e) is not None)
        ]
        if top_folders:
            state.save_folders_batch(top_folders)
    shared = []
    for entry in root_entries:
        is_shared, owned = _share_flags(entry)
        if entry.type == "dir" and is_shared and owned is False:
            shared.append(entry)
    # Папка лежит в корне Диска, поэтому её имя и есть верхняя папка её файлов
    missing = [d for d in shared if d.name not in seen_top]
    if not missing:
        return 0, 0
    names = [d.name for d in missing]
    log.info("Общие папки вне плоского списка: %d", len(names))
    if on_note:
        on_note("shared_folders", names)
    files_count = 0
    bytes_total = 0
    for d in missing:
        n, b = _walk(client, state, d.path, on_file, on_dir, on_note=on_note)
        files_count += n
        bytes_total += b
    return files_count, bytes_total


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
    kwargs = {"offset": offset, "limit": limit, "max_items": limit, "n_retries": LIST_N_RETRIES}
    if media_type:
        kwargs["media_type"] = media_type
    return list(client.get_files(**kwargs))


def _iter_files(
    client: yadisk.Client,
    media_type: str | None = None,
    page_size: int = SCAN_PAGE_SIZE,
    known: set[str] | None = None,
):
    """Итератор по файлам диска для заданного media_type (или всех файлов, если media_type=None).

    Сами пагинируем — yadisk.get_files() умеет итератор, но мы хотим
    обернуть КАЖДУЮ страницу в retry, а не одну общую попытку.

    Отдаёт только файлы, которых в этом сборе ещё не было (known — их пути): страница может
    задеть часть предыдущей. Полная страница без единого нового пути — RepeatedPages: Яндекс
    повторяет уже присланное. Сравнивать только с первой и предыдущей страницей мало — по кругу
    из других кусков сбор у человека шёл 70 часов и «нашёл» 10 млн файлов. Страницы, где новых
    почти нет, SCAN_STALE_PAGES раз подряд — тоже RepeatedPages: по файлу за запрос до конца шли
    бы сутками. scan_tree после RepeatedPages обходит Диск по папкам.
    """
    known = set() if known is None else known
    offset = 0
    stale = 0
    while True:
        page = _get_files_page(client, media_type, offset, page_size)
        if not page:
            return
        fresh = []
        for entry in page:
            if entry.path not in known:
                known.add(entry.path)
                fresh.append(entry)
        # Сначала отдаём новое: путь уже в known, и обход папок его второй раз не посчитает
        yield from fresh
        if len(page) < page_size:
            return
        stale = stale + 1 if len(fresh) < page_size * SCAN_MIN_FRESH_SHARE else 0
        if not fresh or stale >= SCAN_STALE_PAGES:
            raise RepeatedPages(offset)
        offset += page_size


def _scan_flat(
    client: yadisk.Client,
    state: State,
    media_type: str | None,
    on_file: OnFile | None,
    seen_top: set[str] | None = None,
    known: set[str] | None = None,
) -> tuple[int, int]:
    """Плоский обход get_files() с регистрацией в БД пачками по SCAN_PAGE_SIZE.

    seen_top собирает верхние папки увиденных файлов — по ним видно, каких
    общих папок в плоском списке не оказалось. known собирает пути присланных файлов.
    """
    files_count = 0
    bytes_total = 0
    batch: list = []

    db_failed = False
    try:
        for entry in _iter_files(client, media_type=media_type, known=known):
            if entry.type != "file" or _is_skipped(entry.path):
                continue
            if seen_top is not None and (top := _top_folder(entry.path)):
                seen_top.add(top)
            files_count += 1
            bytes_total += _register_file(entry, batch, on_file)
            if len(batch) >= SCAN_PAGE_SIZE:
                try:
                    state.mark_pending_batch(batch)
                except sqlite3.Error:
                    db_failed = True
                    raise
                batch.clear()
    finally:
        # Оборвался список — уже присланное всё равно в базу: эти файлы скачаются
        if batch and not db_failed:
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
    seen_top: set[str] | None = None,
    known: set[str] | None = None,
) -> tuple[int, int]:
    """Быстрое сканирование всего диска плоским get_files() вместо рекурсии по папкам."""
    return _scan_flat(client, state, None, on_file, seen_top, known)


# ── скачивание ──────────────────────────────────────────────────────────────


@dataclass
class DownloadJob:
    remote_path: str
    size: int | None
    md5: str | None
    local_path: str | None = None
    error: str | None = None          # последняя ошибка из БД (для отложенных — маркер)
    storage_host: str | None = None   # сервер хранения, на котором файл был отложен
    preview_path: str | None = None   # уже сохранённое страховочное превью
    modified: float | None = None     # когда файл менялся на Диске — это время получит копия
    created: float | None = None      # когда файл создан на Диске — CreationTime / birthtime
    downloaded_at: str | None = None  # когда файл скачивали раньше: значит, на его месте — наша копия
    legacy_part: str | None = None    # «<файл>.part» версий до 2.8: путь не занят файлом Диска, можно убрать


@dataclass
class DownloadResult:
    job: DownloadJob
    status: str  # downloaded | skipped | failed | deferred
    local_path: str | None
    bytes_written: int
    error: str | None
    duration_ms: int
    storage_host: str = ""
    preview_path: str = ""  # превью, сохранённое для отложенного фото
    dated: bool = False     # дата файла уже выставлена сразу после скачивания


def _ensure_local_dirs(local_path: str) -> None:
    parent = os.path.dirname(local_path)
    if parent:
        ensure_dir(to_long_path(parent))


@_retry
def _download_link(client: yadisk.Client, remote: str) -> str:
    """Ссылка на скачивание с повторами API (429, 50x, таймауты)."""
    return client.get_download_link(remote)


# Ссылку на превью даёт ответ API, и к ней прикладывается ключ доступа: только Яндексу и только по https
PREVIEW_HOSTS = (".yandex.ru", ".yandex.net")


def _token_host_ok(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").lower().endswith(PREVIEW_HOSTS)


def _save_preview(client: yadisk.Client, job: DownloadJob, local_path: str, download_dir: str) -> str:
    """Страховочное превью отложенного фото. Возвращает путь или "" — никогда не бросает.

    Превью отдаёт отдельный сервис Яндекса, оно доступно и при молчащем сервере
    оригинала. Ссылка на него требует токена, в отличие от ссылки на сам файл.
    """
    if not previews_enabled() or categorize(job.remote_path) != "photos":
        return ""
    dst = preview_path_for(download_dir, local_path)
    if job.preview_path and os.path.exists(to_long_path(job.preview_path)):
        return ""  # уже есть — базу не трогаем
    try:
        meta = client.get_meta(job.remote_path, preview_size="XXXL", preview_crop=False, fields=["preview"])
        url = getattr(meta, "preview", None)
        if not url:
            return ""
        if not _token_host_ok(url):
            log.warning("превью %s: ссылка ведёт не на Яндекс (%s) — ключ доступа туда не отправляю",
                        job.remote_path, urlsplit(url).hostname)
            return ""
        token = getattr(client, "token", "")
        failure = fetch_preview(_thread_http(), url, to_long_path(dst),
                                headers={"Authorization": f"OAuth {token}"} if token else None)
    except Exception as e:  # noqa: BLE001 — превью — бонус, скачивание не должно падать из-за него
        log.warning("превью %s: %s", job.remote_path, scrub_urls(e))
        return ""
    if failure is not None:
        missing, text = preview_failure_text(failure)
        (log.info if missing else log.warning)("превью %s: %s", job.remote_path, text)
        return ""
    log.info("PREVIEW | %s | %s", job.remote_path, dst)
    return dst


def _drop_preview(state: State, remote_path: str, preview: str, download_dir: str) -> None:
    """Оригинал фото на месте — его страховочное превью больше не нужно. Никогда не бросает.

    Иначе в папке рядом с оригиналом навсегда оставалась уменьшенная копия, и люди
    принимали её за «испорченный» оригинал.
    """
    try:
        if remove_preview(preview, download_dir):
            state.clear_previews([(remote_path, preview)])
            log.info("PREVIEW-DEL | %s | %s", remote_path, preview)
        else:
            log.warning("превью %s не удалено (файл занят или лежит не в «_превью»): %s",
                        remote_path, preview)
    except Exception as e:  # noqa: BLE001 — уборка не должна ронять скачивание
        log.warning("превью %s: %s", remote_path, e)


def _sweep_previews(state: State, download_dir: str) -> None:
    """Убирает превью фото, оригиналы которых уже на месте: они остались от прошлых
    версий или не удалились в прошлый раз (файл был открыт). Никогда не бросает."""
    try:
        stale = state.stale_previews()
        gone = [item for item in stale if remove_preview(item[1], download_dir)]
        if gone:
            state.clear_previews(gone)
            log.info("Убрано %d превью: оригиналы этих фото уже скачаны", len(gone))
        if len(gone) < len(stale):
            log.warning("не удалено превью: %d (файл занят или лежит не в «_превью»)", len(stale) - len(gone))
    except Exception as e:  # noqa: BLE001 — уборка не должна мешать скачиванию
        log.warning("уборка превью: %s", e)


def _thread_http() -> requests.Session:
    """HTTP-сессия потока для самих файлов (ссылки подписаны, токен не нужен)."""
    http = getattr(_thread_local, "http", None)
    if http is None:
        http = requests.Session()
        http.headers["User-Agent"] = f"yadisk-backup/{APP_VERSION}"
        _thread_local.http = http
        with _all_clients_lock:
            _all_clients.append(http)
    return http


def _download_with_retry(client: yadisk.Client, remote: str, local: str,
                         job: DownloadJob | None = None) -> None:
    """Скачивает файл с Диска в local через ссылку и сервер хранения.

    Сервер хранения, который молчит, даёт StorageUnavailable сразу (без 7 повторов по
    5 минут, как было с client.download). Обрыв, 50x и испорченный файл повторяются со
    свежей ссылкой; остальное — FetchError.
    """
    size = job.size if job else None
    md5 = job.md5 if job else None
    # Уже отложенный файл в новом раунде пробуем один раз: не ожил — ждёт следующего
    was_deferred = bool(job and (job.error or "").startswith(DEFERRED_MARKER))
    tries = 1 if was_deferred else FRESH_LINK_RETRIES + 1
    failure: FetchFailure | None = None
    for attempt in range(tries):
        link = _download_link(client, remote)
        _written, _host, failure = fetch_to_file(
            _thread_http(), link, to_long_path(local),
            expected_size=size, expected_md5=md5,
            host_is_dead=DEAD_HOSTS.is_dead, reject_pages=False,
        )
        if failure is None:
            return
        if failure.is_storage:
            if failure.kind == "storage_down":
                DEAD_HOSTS.mark(failure.host)
            raise StorageUnavailable(failure)
        if failure.is_local or failure.is_local_file:
            break
        if attempt + 1 >= tries:
            break
        if failure.kind == "rate_limit":
            time.sleep(RATE_LIMIT_WAIT_SEC)
        elif failure.kind in ("server_50x", "timeout", "connection", "refused", "bad_file"):
            time.sleep(2 * (attempt + 1))
        else:
            break
    if failure is not None and failure.kind in _DEFER_AFTER_RETRIES:
        # Яндекс стабильно не отдаёт файл («no more reliable hosts», обрывы) — это не
        # вина файла: откладываем до следующего раунда, а не пишем «ошибка»
        raise StorageUnavailable(FetchFailure(
            "storage_down", f"Яндекс не отдал файл после {tries} попыток: {failure}", failure.host))
    raise FetchError(failure)


# Эти сбои после всех повторов откладывают файл, остальные — ошибка файла
_DEFER_AFTER_RETRIES = ("server_50x", "timeout", "connection", "rate_limit")


# Категория сбоя скачивания → префикс ошибки, который читает диагностика Web UI
_FETCH_ERROR_PREFIX = {
    "rate_limit": "rate_limit",
    "server_50x": "server_50x",
    "timeout": "download_timeout",
    "connection": "download_timeout",
    "local_disk": "local_disk",
    "local_file": "local_file",
    "bad_file": "bad_file",
    "refused": "http_error",
}


def _describe_download_error(e: BaseException) -> str:
    """Текст ошибки для БД и логов. Префикс до ' | ' — категория, её читает диагностика Web UI."""
    name = type(e).__name__
    api = api_error_details(e)
    if is_local_disk_error(e):
        # Проверяем ПЕРВЫМ: текст OSError содержит путь, а в пути бывает «500» или «notfound»
        if local_error_kind(e) == "local_file":
            return (f"local_file | {name}: {e} [ЭТОТ ФАЙЛ НЕ ЗАПИСАТЬ В ПАПКУ: слишком длинное "
                    f"или недопустимое имя, или файл слишком большой для диска]")
        return f"local_disk | {name}: {e} [ПРОБЛЕМА ЛОКАЛЬНОГО ДИСКА: место, права или отключённый диск]"
    if isinstance(e, FetchError):
        prefix = _FETCH_ERROR_PREFIX.get(e.failure.kind, e.failure.kind)
        return f"{prefix} | {e.failure.text}"
    if isinstance(e, yadisk.exceptions.TooManyRequestsError):
        return f"rate_limit | 429 TooManyRequests (лимит Яндекса): {e}{api}"
    if isinstance(e, yadisk.exceptions.PathNotFoundError):
        return f"PathNotFound: {e}{api}"
    if is_overdraft(e):
        return f"overdraft | {name} [ДИСК ПЕРЕПОЛНЕН, ЯНДЕКС ЗАКРЫЛ ДОСТУП ПРОГРАММАМ]: {e}{api}"
    if isinstance(e, (yadisk.exceptions.UnauthorizedError, yadisk.exceptions.ForbiddenError)):
        return f"auth_expired | {name}: {e}{api}"
    if isinstance(e, TIMEOUT_ERRORS):
        return f"download_timeout | {name}: {e}{api}"
    if classify_yandex_error(e)["is_yandex_server"]:
        return f"server_50x | {name} [СБОЙ СЕРВЕРА ЯНДЕКСА]: {e}{api}"
    return f"{name}: {e}{api}"


def restore_dates(state: State, download_dir: str, say: Callable[[str], None] | None = None) -> int:
    """Возвращает дату с Яндекса скачанным раньше файлам, у которых стоит дата скачивания.

    Так их скачивали версии до 2.8.0 и 2.8.x до первого нового сбора списка (даты в базе
    ещё не было). Возвращает, сколько файлов получили дату. Папки скачивания нет или она
    пустая (диск отключён) — ничего не делает (см. file_dates.restore_all). Никогда не бросает.
    """
    try:
        return restore_all(state.dates_to_check(), state.mark_dated, download_dir, say)
    except Exception as e:  # noqa: BLE001 — даты не должны ломать шаг скачивания
        log.warning("даты файлов: %s", e)
        return 0


def restore_folder_dates(state: State, download_dir: str) -> int:
    """Синхронизирует даты создания и изменения локальных папок Диска снизу вверх.

    Использует точные даты папок из таблицы folders, а для отсутствующих — запасной расчёт
    по датам файлов внутри папки. Пустые папки не создаются; ошибки не прерывают скачивание.
    """
    if not os.path.isdir(to_long_path(download_dir)):
        return 0
    try:
        exact = state.get_folder_dates()
        fallback = state.derive_folder_dates_from_files()
        merged: list[tuple[str, float | None, float | None]] = []
        for remote_dir in set(exact) | set(fallback):
            ec, em = exact.get(remote_dir, (None, None))
            fc, fm = fallback.get(remote_dir, (None, None))
            created = ec if ec is not None else fc
            modified = em if em is not None else fm
            if created is None and modified is None:
                continue
            local_dir = resolve_local_path(download_dir, remote_dir)
            merged.append((local_dir, created, modified))
        updated = apply_folder_dates(merged)
        if updated:
            log.info("даты папок Диска синхронизированы: %d", updated)
        return updated
    except Exception as e:  # noqa: BLE001 — даты папок не должны ломать скачивание
        log.warning("даты папок: %s", e)
        return 0


def _free_beside(local_path: str, remote_path: str) -> str:
    """Свободный путь рядом, не затирающий уже существующие посторонние файлы или папки."""
    folder, name = os.path.split(local_path)
    h = short_hash(remote_path)
    cand = avoid_case_collision(os.path.join(folder, name_with_tag(name, f"_{h}")))
    n = 1
    while os.path.lexists(to_long_path(cand)):
        n += 1
        cand = avoid_case_collision(os.path.join(folder, name_with_tag(name, f"_{h}_{n}")))
    return cand


def _beside(local_path: str, remote_path: str) -> str:
    """Свободное имя рядом: «Договор.pdf» → «Договор_a1b2c3.pdf» (хеш пути на Диске)."""
    folder, name = os.path.split(local_path)
    return os.path.join(folder, name_with_tag(name, f"_{short_hash(remote_path)}"))


def _named_for(local_path: str, remote_path: str) -> bool:
    """В имени файла — хеш его пути на Диске: так его назвала сама программа (правило до 2.3.1,
    занятое имя, копия «рядом»). Файл по такому пути — наша копия этого же файла Диска."""
    return f"_{short_hash(remote_path)}" in os.path.basename(local_path)


def _is_leftover(existing: str, fresh: str) -> bool:
    """На месте лежит начало только что скачанного файла (или он весь) — его можно заменить.

    Версии до 12 октября качали прямо в настоящее имя: обрыв или молчащий сервер оставляли
    там пустой или недокачанный файл, а в базе — ни отметки о скачивании. Такой файл байт
    в байт совпадает с началом скачанного. Файл, который положил человек или переложила
    «Сортировка», не совпадёт, даже если он меньше. Не прочитать — не затираем.
    """
    try:
        path = to_long_path(existing)
        if os.path.islink(path) or not os.path.isfile(path):
            return False
        left = os.path.getsize(path)
        if left > os.path.getsize(fresh):
            return False
        with open(path, "rb") as old, open(fresh, "rb") as new:
            while left > 0:
                block = old.read(min(left, 1024 * 1024))
                if not block or block != new.read(len(block)):
                    return False
                left -= len(block)
        return True
    except OSError:
        return False


def _drop_legacy_part(job: DownloadJob) -> None:
    """Убирает «<файл>.part», брошенный версией до 2.8 при остановке или сбое. Никогда не бросает.

    Такие файлы бывают по нескольку гигабайт, а новая версия качает во временный файл с другим
    именем и их не трогала. Что путь не занят файлом Диска, решено по базе (_jobs_from_state).
    Файл больше самого файла Диска — не его недокачанная копия: его не трогаем.
    """
    if not job.legacy_part:
        return
    path = to_long_path(job.legacy_part)
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return
        if job.size is not None and os.path.getsize(path) > job.size:
            return
        os.remove(path)
        log.info("Убран временный файл прежней версии: %s", job.legacy_part)
    except OSError as e:
        log.warning("не удалён временный файл прежней версии %s: %s", job.legacy_part, e)


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


# Сбои API, после которых повторы кончились, а Яндекс так и не выдал ссылку: он недоступен
# или перегружен, файл не виноват — откладываем до следующего раунда, а не пишем «ошибка»
_API_UNAVAILABLE = (yadisk.exceptions.RequestError, yadisk.exceptions.TooManyRequestsError,
                    *SERVER_ERRORS, *TIMEOUT_ERRORS, requests.RequestException)


def _download_one(
    job: DownloadJob,
    client: yadisk.Client,
    download_dir: str,
) -> DownloadResult:
    start = time.monotonic()

    def finish(status: str, local: str | None = None, bytes_written: int = 0,
               error: str | None = None, storage_host: str = "", dated: bool = False) -> DownloadResult:
        return DownloadResult(job, status, local, bytes_written, error,
                              int((time.monotonic() - start) * 1000), storage_host, dated=dated)

    part_path: str | None = None
    try:
        local_path = job.local_path or resolve_local_path(download_dir, job.remote_path)
        local_path = avoid_case_collision(local_path)
        _ensure_local_dirs(local_path)
        _drop_legacy_part(job)

        if _already_downloaded(job, local_path):
            return finish("skipped", local_path)
        beside = ""
        if (not job.downloaded_at and not _named_for(local_path, job.remote_path)
                and os.path.lexists(to_long_path(local_path))):
            # На этом месте лежит файл, который программа туда не записывала: его мог положить
            # человек или переложить сортировка. Затираем его, только если это оборванное начало
            # этого же файла (решится после скачивания), иначе наш файл ляжет рядом
            beside = _beside(local_path, job.remote_path)
            if _already_downloaded(job, beside):
                return finish("skipped", beside)
        if why := too_big_for_folder(os.path.dirname(local_path), job.size):
            return finish("failed", error=f"local_file | {why}")

        # Качаем во временный файл и атомарно переименовываем — недокачанный файл
        # никогда не выглядит как готовый.
        part_path = to_long_path(part_path_for(local_path))
        remove_quiet(part_path)
        _download_with_retry(client, job.remote_path, part_path, job)
        if beside and not _is_leftover(local_path, part_path):
            local_path = _free_beside(local_path, job.remote_path)
        os.replace(part_path, to_long_path(local_path))
        # Копии — дата изменения (и создания) файла на Диске, а не время скачивания: так его видят
        # проводник, Finder и сортировка по датам. Не получилось — не беда, файл на месте
        dated = set_file_time(local_path, job.modified, created=job.created)
    except StorageUnavailable as e:
        if part_path:
            remove_quiet(part_path)
        res = finish("deferred", error=str(e.failure), storage_host=e.failure.host)
        res.preview_path = _save_preview(client, job, local_path, download_dir)
        return res
    except _API_UNAVAILABLE as e:
        if part_path:
            remove_quiet(part_path)
        return finish("deferred", error=f"{DEFERRED_MARKER}Яндекс не выдал ссылку на файл: "
                                        f"{_describe_download_error(e)}")
    except Exception as e:
        # Любая ошибка (сеть, Яндекс, локальный диск, makedirs) — это failed-результат,
        # а не исключение: один битый файл не должен ронять весь download_all.
        if part_path:
            remove_quiet(part_path)
        return finish("failed", error=_describe_download_error(e))

    return finish("downloaded", local_path, file_size_safe(local_path) or 0, dated=dated)


# Каждому потоку — свой клиент. Шарить yadisk.Client между потоками небезопасно.
_thread_local = threading.local()
# Реестр всех созданных клиентов — чтобы закрыть сокеты после остановки пула
_all_clients: list = []
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
    _thread_local.client = None
    _thread_local.http = None
    for cli in clients:
        try:
            cli.close()
        except Exception:  # noqa: BLE001 — закрытие не должно ронять выход
            pass


def _legacy_part(remote_path: str, local_path: str | None,
                 taken: tuple[set[str], set[str]]) -> str | None:
    """«<файл>.part», куда качали версии до 2.8, — если его путь не занят файлом Диска.

    На Диске бывает и свой «видео.mp4.part» (недокачанная загрузка браузера): его копию,
    скачанную или ещё нет, не трогаем. taken — State.part_named_paths().
    """
    if not local_path:
        return None
    part = local_path + ".part"
    taken_local, taken_remote = taken
    if path_key(part) in taken_local or (remote_path + ".part").lower() in taken_remote:
        return None
    return part


def _jobs_from_state(state: State) -> Iterator[DownloadJob]:
    taken = state.part_named_paths()
    for row in state.get_pending():
        keys = row.keys()
        local_path = row["local_path"] if "local_path" in keys else None
        yield DownloadJob(
            remote_path=row["remote_path"],
            size=row["size"],
            md5=row["md5"],
            local_path=local_path,
            error=row["error"] if "error" in keys else None,
            storage_host=row["storage_host"] if "storage_host" in keys else None,
            preview_path=row["preview_path"] if "preview_path" in keys else None,
            modified=row["modified"] if "modified" in keys else None,
            created=row["created"] if "created" in keys else None,
            downloaded_at=row["downloaded_at"] if "downloaded_at" in keys else None,
            legacy_part=_legacy_part(row["remote_path"], local_path, taken),
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
    counters: dict[str, int] | None = None,
) -> dict[str, int]:
    """Скачивает pending файлы из state в пул потоков с ограниченной очередью в памяти.

    Возвращает счётчики: {downloaded, skipped, failed, deferred, bytes} и, если пришлось
    остановиться раньше, aborted = 'local_disk' (диск не принимает файлы) или 'auth'
    (токен действительно мёртв). Файлы на молчащих серверах хранения откладываются:
    остаются в очереди до следующего раунда.
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
    _sweep_previews(state, download_dir)

    if counters is None:
        counters = {"downloaded": 0, "skipped": 0, "failed": 0, "deferred": 0, "bytes": 0}
    else:
        for k in ("downloaded", "skipped", "failed", "deferred", "bytes"):
            counters.setdefault(k, 0)
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
        done_n = (counters["downloaded"] + counters["skipped"] + counters["failed"]
                  + counters["deferred"])
        total_str = f"/{total_files}" if total_files is not None else ""
        # Строку видит человек в журнале задачи; порядок полей прежний
        print(
            f"Прогресс: {done_n}{total_str} файлов | скачано {counters['downloaded']} "
            f"| пропущено {counters['skipped']} | ошибок {counters['failed']} "
            f"| отложено {counters['deferred']} | {human_bytes(counters['bytes'])}",
            flush=True,
        )

    def worker(job: DownloadJob) -> DownloadResult:
        return _download_one(job, _get_thread_client(token), download_dir)

    _close_all_clients()
    pool = ThreadPoolExecutor(max_workers=workers)
    # Не более max_in_flight задач в памяти — генератор из БД читается лениво
    max_in_flight = max(workers * 2, 4)
    futures: dict = {}
    streaks = {"local_disk": 0, "auth": 0}

    def submit_next() -> bool:
        # Файлы уже известного молчащего сервера откладываем сразу, без запроса к Яндексу
        for job in jobs_iter:
            if job.storage_host and DEAD_HOSTS.is_dead(job.storage_host):
                _apply_result(state, DownloadResult(
                    job, "deferred", None, 0,
                    f"{DEFERRED_MARKER}сервер хранения {job.storage_host} недавно не отвечал",
                    0, job.storage_host,
                ), counters, file_bar, byte_bar, download_dir)
                plain_report()
                continue
            futures[pool.submit(worker, job)] = job
            return True
        return False

    def should_abort(res: DownloadResult) -> str | None:
        error = res.error or ""
        if res.status != "failed":
            streaks["local_disk"] = streaks["auth"] = 0
            return None
        if error.startswith("overdraft |"):
            # Переполнен весь Диск, а не этот файл: остальные ответят так же
            log.error("Диск переполнен: Яндекс закрыл программам доступ к нему — останавливаюсь")
            return "overdraft"
        streaks["local_disk"] = streaks["local_disk"] + 1 if error.startswith("local_disk |") else 0
        streaks["auth"] = streaks["auth"] + 1 if error.startswith("auth_expired |") else 0
        if streaks["local_disk"] >= LOCAL_DISK_GIVE_UP:
            log.error("Локальный диск %d раз подряд не принял файл — останавливаюсь", LOCAL_DISK_GIVE_UP)
            return "local_disk"
        if streaks["auth"] >= AUTH_FAILS_BEFORE_RECHECK:
            streaks["auth"] = 0
            # 403 на отдельные файлы бывает и с живым токеном — проверяем сам токен
            state_now, msg = token_state(token)
            if state_now == "invalid":
                log.error("Токен перестал действовать: %s", msg)
                return "auth"
        return None

    pool_joined = True
    try:
        while len(futures) < max_in_flight and submit_next():
            pass

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
                    _apply_result(state, res, counters, file_bar, byte_bar, download_dir)
                    abort = should_abort(res)
                    if abort:
                        counters["aborted"] = abort
                        break
                    # Подкладываем следующую задачу из стрима
                    submit_next()
                if counters.get("aborted"):
                    pool.shutdown(wait=True, cancel_futures=True)
                    # Файлы, которые успели докачаться, пока пул останавливался, — в базу:
                    # иначе они остались бы pending, а их превью — навсегда в «_превью»
                    for fut, job in futures.items():
                        if fut.cancelled():
                            continue
                        try:
                            res = fut.result()
                        except Exception as e:  # noqa: BLE001 — защитный барьер, как выше
                            res = DownloadResult(job, "failed", None, 0, _describe_download_error(e), 0)
                        _apply_result(state, res, counters, file_bar, byte_bar, download_dir)
                    futures.clear()
                    break
                plain_report()
            plain_report(force=True)
        except KeyboardInterrupt:
            print("\nCtrl+C — отменяю очередь, дожидаюсь только текущих файлов...", file=sys.stderr)
            log.warning("Ctrl+C — отменяю оставшиеся задачи и закрываю пул...")
            pool_joined = False
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        except BaseException:
            pool_joined = False
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
    finally:
        if pool_joined:
            _close_all_clients()
        file_bar.close()
        byte_bar.close()
        elapsed = max(0.001, time.monotonic() - started)
        log.info(
            "Итог download: скачано %d (%s), пропущено %d, ошибок %d, отложено %d "
            "(молчащих серверов %d) за %.0f с, средняя %s/с",
            counters["downloaded"], human_bytes(counters["bytes"]), counters["skipped"],
            counters["failed"], counters["deferred"], DEAD_HOSTS.count(), elapsed,
            human_bytes(counters["bytes"] / elapsed),
        )

    return counters


def _apply_result(
    state: State,
    res: DownloadResult,
    counters: dict[str, int],
    file_bar: tqdm,
    byte_bar: tqdm,
    download_dir: str,
) -> None:
    """Применяет результат скачивания к БД и счётчикам.

    Оригинал на месте (скачан или уже лежал) — его страховочное превью удаляется.
    """
    preview = None
    if res.status == "downloaded":
        dated_val = res.job.modified if res.dated and res.job.modified is not None else None
        if dated_val is not None:
            try:
                preview = state.mark_downloaded(res.job.remote_path, res.local_path or "", dated=dated_val)
            except TypeError:
                preview = state.mark_downloaded(res.job.remote_path, res.local_path or "")
        else:
            preview = state.mark_downloaded(res.job.remote_path, res.local_path or "")
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
        preview = state.mark_skipped(res.job.remote_path, res.local_path or "")
        counters["skipped"] += 1
        if res.job.size:
            byte_bar.update(res.job.size)
        log.info("SKIP | already exists | %s", res.job.remote_path)
    elif res.status == "deferred":
        already = (res.job.storage_host == res.storage_host
                   and (res.job.error or "").startswith(DEFERRED_MARKER))
        if not already or res.preview_path:  # тот же сервер уже записан — базу лишний раз не трогаем
            state.mark_deferred(res.job.remote_path, res.storage_host, res.error or "",
                                preview_path=res.preview_path or None)
        counters["deferred"] += 1
        if res.preview_path:
            counters["previews"] = counters.get("previews", 0) + 1
        log.info("DEFER | %s | %s", res.storage_host or "?", res.job.remote_path)
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
    file_bar.set_postfix_str(f"скачано {counters['downloaded']}, ждут Яндекса {counters['deferred']}",
                             refresh=False)
    file_bar.update(1)
    if preview:
        _drop_preview(state, res.job.remote_path, preview, download_dir)


# ── проверка токена ─────────────────────────────────────────────────────────


def check_token(token: str) -> tuple[bool, str]:
    """Проверяет, валиден ли токен. Возвращает (ok, message)."""
    state, msg = token_state(token)
    return state == "ok", msg


def token_state(token: str) -> tuple[str, str]:
    """Состояние токена: ('ok' | 'invalid' | 'unreachable' | 'overdraft', сообщение).

    unreachable — Яндекс не ответил на проверку (сеть, 50x, таймаут). Это не повод
    просить у человека новый токен: во время сбоев Яндекса старый токен жив.
    overdraft — токен жив, но Диск переполнен и Яндекс закрыл по нему доступ.
    """
    if not token or token.strip() == "" or token.startswith("AQAAA_paste"):
        return "invalid", (
            "Токен не задан. Открой https://yandex.ru/dev/disk/poligon/, "
            "получи OAuth-токен по кнопке справа вверху и положи его в .env "
            "как YA_DISK_TOKEN=..."
        )
    cli = None
    try:
        cli = make_client(token)
        if not cli.check_token():
            return "invalid", (
                "Токен невалиден (Яндекс ответил, что он не работает). "
                "Получи новый на https://yandex.ru/dev/disk/poligon/"
            )
        acc = account_info(cli)
        log.info("Токен OK. %s", describe_account(acc))
        return "ok", f"Токен OK. {describe_account(acc)}."
    except (yadisk.exceptions.UnauthorizedError, yadisk.exceptions.ForbiddenError) as e:
        if is_overdraft(e):
            return "overdraft", overdraft_message(overdraft_account(cli))
        return "invalid", (
            f"Токен не принят ({type(e).__name__}). Получи новый на "
            f"https://yandex.ru/dev/disk/poligon/{api_error_details(e)}"
        )
    except Exception as e:
        return "unreachable", (
            f"server_50x | Яндекс не ответил на проверку токена: {type(e).__name__}: {e}"
            f"{api_error_details(e)}. Токен, скорее всего, в порядке — повторите позже."
        )
    finally:
        if cli is not None:
            try:
                cli.close()
            except Exception:
                pass
