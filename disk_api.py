"""Клиент REST API Яндекс.Диска, политика повторов, сведения об аккаунте и разбор ошибок."""

from __future__ import annotations

import logging

import requests
import yadisk
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from storage_fetch import FetchFailure
from utils import (
    EXIT_AUTH,
    EXIT_DISK_FULL,
    EXIT_OUTAGE,
    EXIT_OVERDRAFT,
    TELEGRAM_CHANNEL,
    human_bytes,
    in_trash,
    is_local_disk_error,
    is_sqlite_disk_error,
    to_disk_path,
)

log = logging.getLogger("yadisk_backup")

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
# качает storage_fetch.
DEFAULT_TIMEOUT = (15.0, 60.0)
DEFAULT_N_RETRIES = 1
LIST_N_RETRIES = 3

# Дерево папок в Настройках: человек ждёт ответа, долгие таймауты и повторы ему не нужны
BROWSE_TIMEOUT = (10.0, 20.0)
BROWSE_CAP = 5000

OVERDRAFT_ERROR = "DiskAPIDisabledForOverdraftUserError"
OVERDRAFT_DAYS = 44

_FOLDER_FIELDS = ["name", "path", "type", "share", "created", "modified"]
_FOLDER_DATE_FIELDS = ["path", "type", "created", "modified"]


def make_client(token: str) -> yadisk.Client:
    """Клиент REST API с единым таймаутом. Закрывать через .close() или contextlib.closing."""
    return yadisk.Client(token=token, default_args={"timeout": DEFAULT_TIMEOUT, "n_retries": DEFAULT_N_RETRIES})


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


def is_overdraft(e: BaseException) -> bool:
    """Яндекс закрыл программам доступ к Диску: он переполнен."""
    return isinstance(e, yadisk.exceptions.ForbiddenError) and getattr(e, "error_type", None) == OVERDRAFT_ERROR


def overdraft_account(client: yadisk.Client) -> dict | None:
    """Сколько занято и сколько есть — Яндекс говорит это и переполненному Диску. Никогда не бросает."""
    try:
        return account_info(client)
    except Exception as e:  # noqa: BLE001 — цифры только поясняют, без них текст тоже верен
        log.warning("сведения о переполненном Диске: %s%s", type(e).__name__, api_error_details(e))
        return None


def overdraft_message(acc: dict | None) -> str:
    """Что случилось и что делать — для консоли и журнала."""
    used = (f" (занято {human_bytes(acc['used'])} из {human_bytes(acc['total'])})"
            if acc and acc.get("total") else "")
    return (f"Диск переполнен{used}: Яндекс закрыл программам доступ к нему. Так бывает, когда "
            f"закончилась подписка на место. Скачивать файлы Яндекс разрешает {OVERDRAFT_DAYS} дня "
            f"после переполнения, потом блокирует Диск. Продлите подписку хотя бы на месяц — "
            f"программа продолжит с того же места. Новый ключ доступа не поможет.")


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


def _entry_ts(entry, attr: str) -> float | None:
    """Временная метка ресурса Диска (modified/created) в секундах эпохи или None."""
    when = getattr(entry, attr, None)
    try:
        return when.timestamp() if when is not None else None
    except (AttributeError, OverflowError, OSError, ValueError):
        return None


def _modified_ts(entry) -> float | None:
    return _entry_ts(entry, "modified")


def _created_ts(entry) -> float | None:
    return _entry_ts(entry, "created")


def _top_folder(path: str) -> str | None:
    """'disk:/Фото/2020/a.jpg' → 'Фото'; файл в корне Диска → None."""
    rest = to_disk_path(path).removeprefix("disk:/")
    return rest.split("/", 1)[0] if "/" in rest else None


def _parent_remote_dir(remote_dir: str) -> str:
    """Родитель папки: 'disk:/Фото/2023' → 'disk:/Фото', 'disk:/Фото' → 'disk:/'."""
    cur = remote_dir.rstrip("/")
    idx = cur.rfind("/")
    return cur[:idx] if idx > 5 else "disk:/"


def _share_flags(entry) -> tuple[bool, bool | None]:
    """(общая ли папка, своя ли она). У чужой общей папки is_owned == False."""
    share = getattr(entry, "share", None)
    if share is None:
        return False, None
    return True, getattr(share, "is_owned", None)


def list_disk_folders(
    client: yadisk.Client, path: str = "disk:/", cap: int = BROWSE_CAP,
) -> tuple[list[dict], bool]:
    """Подпапки path для дерева в Настройках и признак «показаны не все»."""
    folders: list[dict] = []
    seen = 0
    for entry in client.listdir(to_disk_path(path), limit=1000, max_items=cap + 1,
                                fields=_FOLDER_FIELDS):
        seen += 1
        if seen > cap:
            break
        if entry.type != "dir" or in_trash(entry.path):
            continue
        shared, owned = _share_flags(entry)
        folders.append({"name": entry.name, "path": entry.path,
                        "shared": shared, "owned": bool(owned)})
    folders.sort(key=lambda f: (f["name"].casefold(), f["name"]))
    return folders, seen > cap


def scan_exit_code(e: BaseException) -> int | None:
    """Код выхода для ошибки, оборвавшей сбор списка; None — неизвестная ошибка."""
    if is_overdraft(e):
        return EXIT_OVERDRAFT
    if isinstance(e, yadisk.exceptions.UnauthorizedError):
        return EXIT_AUTH
    if isinstance(e, (yadisk.exceptions.RequestError, yadisk.exceptions.TooManyRequestsError,
                      requests.RequestException, RepeatedPages, *SERVER_ERRORS, *TIMEOUT_ERRORS)):
        return EXIT_OUTAGE
    if is_sqlite_disk_error(e):
        return EXIT_DISK_FULL
    return None
