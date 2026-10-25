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

import requests
import yadisk
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm

from state import DEFERRED_MARKER, State
from storage_fetch import DeadHosts, FetchFailure, fetch_preview, fetch_to_file, preview_failure_text
from utils import (
    APP_VERSION,
    EXIT_AUTH,
    EXIT_DISK_FULL,
    EXIT_OUTAGE,
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
    is_sqlite_disk_error,
    remove_preview,
    remove_quiet,
    resolve_local_path,
    short_hash,
    to_disk_path,
    to_long_path,
    too_big_for_folder,
)

log = logging.getLogger("yadisk_backup")

# Сколько файлов берём за один запрос get_files и кладём в БД одной пачкой
SCAN_PAGE_SIZE = 200
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


# ── клиент и retry-политика ─────────────────────────────────────────────────


# Ошибки, повторять которые бессмысленно: токен не станет валидным, удалённый файл
# не появится, папка не станет файлом, а исчерпанный лимит скачиваний файла не вернётся
# через минуту, сколько ни жди.
NON_RETRYABLE = (
    yadisk.exceptions.UnauthorizedError,
    yadisk.exceptions.ForbiddenError,
    yadisk.exceptions.NotFoundError,
    yadisk.exceptions.WrongResourceTypeError,
    yadisk.exceptions.BadRequestError,
    yadisk.exceptions.ResourceDownloadLimitExceededError,
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
# Через клиент идут только запросы к API (ссылки, списки, сведения о файле), сами файлы
# качает storage_fetch. Ответ API дольше минуты — уже сбой: ждать по 5 минут и повторять
# его 4 раза внутри yadisk, да ещё 7 раз через tenacity, значило висеть часами
DEFAULT_TIMEOUT = (15.0, 60.0)
# Свои повторы yadisk для одиночных запросов (ссылка на файл, сведения о файле): один. Файл,
# которому Яндекс так и не выдал ссылку, ждёт следующего раунда, а не висит здесь
DEFAULT_N_RETRIES = 1
# Для списков — три, как до 2.8: listdir листает папку страницами по 500, и страница, которой
# не хватило повторов, начинает всю папку заново. С одним повтором при ~20% сбоев большая
# папка почти никогда не дочитывалась и выпадала из списка, а сбор кончался кодом 3
LIST_N_RETRIES = 3


def make_client(token: str) -> yadisk.Client:
    """Клиент REST API с единым таймаутом. Закрывать через .close() или contextlib.closing."""
    return yadisk.Client(token=token, default_args={"timeout": DEFAULT_TIMEOUT, "n_retries": DEFAULT_N_RETRIES})


# Дерево папок в Настройках: человек ждёт ответа, долгие таймауты и повторы ему не нужны
BROWSE_TIMEOUT = (10.0, 20.0)
# Сколько элементов папки читаем ради подпапок: API не обещает «сначала папки»
BROWSE_CAP = 5000


def make_browse_client(token: str) -> yadisk.Client:
    """Клиент для дерева папок: короткий таймаут и один повтор."""
    return yadisk.Client(token=token, default_args={"timeout": BROWSE_TIMEOUT, "n_retries": 1})


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


_normalize_remote = to_disk_path  # прежнее имя: 'disk:/foo/bar', как возвращает API
_is_skipped = in_trash  # прежнее имя: путь в корзине


class RepeatedPages(Exception):
    """Плоский список Диска прислал уже присланную страницу снова — дальше по нему не дойти."""

    def __init__(self, offset: int):
        super().__init__(f"Яндекс повторяет страницы плоского списка файлов (с {offset}-го файла)")
        self.offset = offset


class RootNotFound(Exception):
    """Выбранной папки больше нет на Диске: её переименовали, перенесли или удалили."""

    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


# on_note(вид, имена): сбор списка сообщает о находках, которые стоит показать человеку
OnNote = Callable[[str, list[str]], None]


def _modified_ts(entry) -> float | None:
    """Когда файл менялся на Диске — секунды эпохи; None, если API не сказал."""
    when = getattr(entry, "modified", None)
    try:
        return when.timestamp() if when is not None else None
    except (AttributeError, OverflowError, OSError, ValueError):
        return None


def _register_file(entry, batch: list, on_file: OnFile | None) -> int:
    """Кладёт файл API в пачку для БД и дёргает колбэк. Возвращает размер в байтах."""
    size = int(entry.size) if entry.size is not None else None
    batch.append((entry.path, size, getattr(entry, "md5", None), _modified_ts(entry)))
    if on_file:
        on_file(entry.path, size or 0)
    return size or 0


def _top_folder(path: str) -> str | None:
    """'disk:/Фото/2020/a.jpg' → 'Фото'; файл в корне Диска → None."""
    rest = to_disk_path(path).removeprefix("disk:/")
    return rest.split("/", 1)[0] if "/" in rest else None


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
        try:
            files, total = scan_all_files(client, state, on_file=on_file, seen_top=seen_top)
        except Exception as e:
            log.warning("get_files недоступен (%s), переключаюсь на обход дерева папок", e)
        else:
            more_files, more_bytes = _scan_shared_folders(client, state, seen_top, on_file, on_dir, on_note)
            return files + more_files, total + more_bytes
        return _walk(client, state, root, on_file, on_dir, on_note=on_note)
    return _walk(client, state, root, on_file, on_dir, strict_root=True, on_note=on_note)


def scan_exit_code(e: BaseException) -> int | None:
    """Код выхода для ошибки, оборвавшей сбор списка; None — неизвестная ошибка.

    2 — Яндекс не принял токен, 3 — Яндекс недоступен или просит подождать (повторить
    позже), 4 — базу не записать на локальный диск. Без этого любая такая ошибка давала
    код 1, который Web UI читал как «скачивание закончилось с ошибками».
    """
    if isinstance(e, yadisk.exceptions.UnauthorizedError):
        return EXIT_AUTH
    if isinstance(e, (yadisk.exceptions.RequestError, yadisk.exceptions.TooManyRequestsError,
                      requests.RequestException, RepeatedPages, *SERVER_ERRORS, *TIMEOUT_ERRORS)):
        return EXIT_OUTAGE
    if is_sqlite_disk_error(e):
        return EXIT_DISK_FULL
    return None


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
        except Exception as e:
            log.error("Не смог прочитать %s: %s", cur, e)
            if on_note:
                on_note("unreadable_folders", [cur])
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
    # max_items=None → yadisk сам пагинирует; сбой страницы он повторяет сам, не с начала папки
    return list(client.listdir(path, max_items=None, n_retries=LIST_N_RETRIES))


# ── общие папки и дерево папок для Настроек ─────────────────────────────────

_FOLDER_FIELDS = ["name", "path", "type", "share"]


@_retry
def _list_root_with_share(client: yadisk.Client) -> list:
    """Корень Диска с признаком общего доступа (share есть только у общих папок)."""
    return list(client.listdir("disk:/", max_items=None, fields=_FOLDER_FIELDS, n_retries=LIST_N_RETRIES))


def _share_flags(entry) -> tuple[bool, bool | None]:
    """(общая ли папка, своя ли она). У чужой общей папки is_owned == False."""
    share = getattr(entry, "share", None)
    if share is None:
        return False, None
    return True, getattr(share, "is_owned", None)


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
        shared = shared_with_me_dirs(client)
    except yadisk.exceptions.UnauthorizedError:
        raise
    except Exception as e:  # noqa: BLE001 — список уже собран, это только дополнение
        log.warning("Не смог проверить общие папки в корне Диска: %s", e)
        return 0, 0
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


def list_disk_folders(
    client: yadisk.Client, path: str = "disk:/", cap: int = BROWSE_CAP,
) -> tuple[list[dict], bool]:
    """Подпапки path для дерева в Настройках и признак «показаны не все».

    Элементы: {name, path, shared, owned}. Порядок «сначала папки» API не обещает,
    поэтому читаем до cap элементов папки. Корзину не показываем.
    """
    folders: list[dict] = []
    seen = 0
    for entry in client.listdir(to_disk_path(path), limit=1000, max_items=cap + 1,
                                fields=_FOLDER_FIELDS):
        seen += 1
        if seen > cap:
            break
        if entry.type != "dir" or _is_skipped(entry.path):
            continue
        shared, owned = _share_flags(entry)
        folders.append({"name": entry.name, "path": entry.path,
                        "shared": shared, "owned": bool(owned)})
    folders.sort(key=lambda f: (f["name"].casefold(), f["name"]))
    return folders, seen > cap


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


def _iter_files(client: yadisk.Client, media_type: str | None = None, page_size: int = SCAN_PAGE_SIZE):
    """Итератор по файлам диска для заданного media_type (или всех файлов, если media_type=None).

    Сами пагинируем — yadisk.get_files() умеет итератор, но мы хотим
    обернуть КАЖДУЮ страницу в retry, а не одну общую попытку.

    Полная страница, которая повторяет первую или предыдущую, — RepeatedPages: без этой
    проверки сбор шёл бы бесконечно, а scan_tree тогда обходит Диск по папкам.
    """
    offset = 0
    first: set[str] = set()
    prev: set[str] = set()
    while True:
        page = _get_files_page(client, media_type, offset, page_size)
        if not page:
            return
        paths = {entry.path for entry in page}
        if offset and len(page) >= page_size and (paths <= prev or paths <= first):
            raise RepeatedPages(offset)
        yield from page
        if len(page) < page_size:
            return
        first = first or paths
        prev = paths
        offset += page_size


def _scan_flat(
    client: yadisk.Client,
    state: State,
    media_type: str | None,
    on_file: OnFile | None,
    seen_top: set[str] | None = None,
) -> tuple[int, int]:
    """Плоский обход get_files() с регистрацией в БД пачками по SCAN_PAGE_SIZE.

    seen_top собирает верхние папки увиденных файлов — по ним видно, каких
    общих папок в плоском списке не оказалось.
    """
    files_count = 0
    bytes_total = 0
    batch: list = []

    for entry in _iter_files(client, media_type=media_type):
        if entry.type != "file" or _is_skipped(entry.path):
            continue
        if seen_top is not None and (top := _top_folder(entry.path)):
            seen_top.add(top)
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
    seen_top: set[str] | None = None,
) -> tuple[int, int]:
    """Быстрое сканирование всего диска плоским get_files() вместо рекурсии по папкам."""
    return _scan_flat(client, state, None, on_file, seen_top)


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


class StorageUnavailable(Exception):
    """Сервер хранения файла молчит — файл откладывается, а не считается ошибкой."""

    def __init__(self, failure: FetchFailure):
        super().__init__(str(failure))
        self.failure = failure


class FetchError(Exception):
    """Файл не скачался после повторов; failure.kind — категория для текста ошибки."""

    def __init__(self, failure: FetchFailure):
        super().__init__(failure.text)
        self.failure = failure


def _ensure_local_dirs(local_path: str) -> None:
    parent = os.path.dirname(local_path)
    if parent:
        ensure_dir(to_long_path(parent))


@_retry
def _download_link(client: yadisk.Client, remote: str) -> str:
    """Ссылка на скачивание с повторами API (429, 50x, таймауты)."""
    return client.get_download_link(remote)


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
        token = getattr(client, "token", "")
        failure = fetch_preview(_thread_http(), url, to_long_path(dst),
                                headers={"Authorization": f"OAuth {token}"} if token else None)
    except Exception as e:  # noqa: BLE001 — превью — бонус, скачивание не должно падать из-за него
        log.warning("превью %s: %s", job.remote_path, e)
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
    if isinstance(e, (yadisk.exceptions.UnauthorizedError, yadisk.exceptions.ForbiddenError)):
        return f"auth_expired | {name}: {e}{api}"
    if isinstance(e, TIMEOUT_ERRORS):
        return f"download_timeout | {name}: {e}{api}"
    if classify_yandex_error(e)["is_yandex_server"]:
        return f"server_50x | {name} [СБОЙ СЕРВЕРА ЯНДЕКСА]: {e}{api}"
    return f"{name}: {e}{api}"


def _set_mtime(local_path: str, ts: float | None) -> None:
    """Копии — время изменения файла на Диске, а не время скачивания: так его видят проводник,
    Finder и сортировка по датам. Не получилось — не беда, файл уже на месте."""
    if not ts:
        return
    try:
        os.utime(to_long_path(local_path), (ts, ts))
    except (OSError, OverflowError, ValueError):
        pass


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
               error: str | None = None, storage_host: str = "") -> DownloadResult:
        return DownloadResult(job, status, local, bytes_written, error,
                              int((time.monotonic() - start) * 1000), storage_host)

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
            local_path = beside
        os.replace(part_path, to_long_path(local_path))
        _set_mtime(local_path, job.modified)
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

    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "deferred": 0, "bytes": 0}
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
                continue
            futures[pool.submit(worker, job)] = job
            return True
        return False

    def should_abort(res: DownloadResult) -> str | None:
        error = res.error or ""
        if res.status != "failed":
            streaks["local_disk"] = streaks["auth"] = 0
            return None
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
    """Состояние токена: ('ok' | 'invalid' | 'unreachable', сообщение).

    unreachable — Яндекс не ответил на проверку (сеть, 50x, таймаут). Это не повод
    просить у человека новый токен: во время сбоев Яндекса старый токен жив.
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
