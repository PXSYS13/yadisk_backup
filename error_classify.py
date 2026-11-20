"""Очистка URL от секретов и классификация ошибок Яндекса для человека."""

from __future__ import annotations

import errno
import re

TELEGRAM_CHANNEL = "https://t.me/davronsv"
TELEGRAM_HANDLE = "@davronsv"
YOUTUBE_VIDEO_URL = "https://www.youtube.com/watch?v=XAiJxqHkEUA"
DONATION_URL = "https://dalink.to/davronsvv"

_URL_TAIL_RE = re.compile(
    r"(https?://[^/\s'\"?#]+|//[A-Za-z0-9.-]+\.yandex\.[a-z.]+)[/?#](?:[^\s'\")]|\)(?=[^\s'\",;]))*"
)
_URL_PATH_RE = re.compile(r"(\burl: )/(?:[^\s'\")]|\)(?=[^\s'\",;]))*")

_HTTP_429_RE = re.compile(r"\b429\b")
_HTTP_50X_RE = re.compile(r"\b50[0234]\b")
_HTTP_AUTH_RE = re.compile(r"\b40[13]\b")
_HTTP_404_RE = re.compile(r"\b404\b")

_LOCAL_FILE_ERRNOS = frozenset({errno.ENAMETOOLONG, errno.EINVAL, errno.EFBIG})
_LOCAL_DISK_ERRNOS = frozenset({
    errno.ENOSPC, errno.EACCES, errno.EPERM, errno.EROFS, errno.EEXIST,
    errno.EISDIR, errno.ENOTDIR, errno.ENOENT, errno.EIO,
    *(e for e in (getattr(errno, "EDQUOT", None),) if e is not None),
}) | _LOCAL_FILE_ERRNOS


def scrub_urls(text: object) -> str:
    """Текст без путей и параметров ссылок: «…for url: https://downloader.disk.yandex.ru/…».

    Подписанная ссылка на файл Яндекса — сама по себе доступ к нему (и uid владельца): в журналы,
    базу и отчёт для поддержки она попадать не должна. Сервер в тексте остаётся.
    """
    return _URL_PATH_RE.sub(r"\1/…", _URL_TAIL_RE.sub(r"\1/…", str(text)))


def _is_local_disk_oserror(exc: BaseException) -> bool:
    return (
        isinstance(exc, OSError)
        and not isinstance(exc, (ConnectionError, TimeoutError))
        and getattr(exc, "errno", None) in _LOCAL_DISK_ERRNOS
    )


def classify_yandex_error(exc: BaseException | str | None) -> dict:
    """Анализирует ошибку и определяет, возникла ли она на стороне серверов Яндекса."""
    if exc is None:
        return {
            "is_yandex_server": False,
            "category": "none",
            "title": "Ошибок нет",
            "message": "Всё работает штатно",
            "action": "",
        }

    err_str = str(exc).lower()
    type_name = type(exc).__name__.lower() if isinstance(exc, BaseException) else ""

    if (
        err_str.startswith(("local_disk |", "local_file |"))
        or (isinstance(exc, BaseException) and _is_local_disk_oserror(exc))
    ):
        return {
            "is_yandex_server": False,
            "category": "local_file" if err_str.startswith("local_file |") else "local_disk",
            "title": "Проблема с локальным диском",
            "message": "Нет места, нет прав на запись или папка скачивания недоступна (отключён диск?).",
            "action": "Освободите место или проверьте папку скачивания в настройках, затем повторите.",
        }

    if err_str.startswith("overdraft |"):
        return {
            "is_yandex_server": False,
            "category": "overdraft",
            "title": "Диск переполнен",
            "message": "Яндекс закрыл программам доступ к Диску, пока не продлена подписка.",
            "action": "Продлите подписку на место и повторите скачивание.",
        }

    if err_str.startswith("bad_file |"):
        return {
            "is_yandex_server": False,
            "category": "bad_file",
            "title": "Файл пришёл повреждённым",
            "message": str(exc),
            "action": "Повторите скачивание позже.",
        }

    if _HTTP_429_RE.search(err_str) or "toomanyrequests" in err_str or "toomanyrequests" in type_name:
        return {
            "is_yandex_server": True,
            "category": "rate_limit",
            "title": "Лимит запросов Яндекса (HTTP 429 Too Many Requests)",
            "message": "Серверы Яндекса временно ограничили частоту скачивания. Скрипт ожидает паузу.",
            "action": f"Рекомендуется снизить число параллельных воркеров до 2–3. Новости: {TELEGRAM_CHANNEL}",
        }

    server_types = ("badgateway", "gatewaytimeout", "internalserver", "unavailableerror", "servererror")
    if (
        err_str.startswith("server_50x |")
        or _HTTP_50X_RE.search(err_str)
        or any(st in type_name for st in server_types)
        or "bad gateway" in err_str
        or "service unavailable" in err_str
        or "gateway timeout" in err_str
        or "prepare failed" in err_str
    ):
        return {
            "is_yandex_server": True,
            "category": "server_down",
            "title": "Сбой дата-центра Яндекса (HTTP 50x / шторм серверов)",
            "message": "Проблема на стороне серверов Яндекса (перегрузка или сбой узлов). Скрипт НЕ сломался!",
            "action": f"Скрипт автоматически подождёт и продолжит. Проверьте статус в Telegram: {TELEGRAM_CHANNEL}",
        }

    conn_indicators = (
        "connectionreset", "connection reset", "remotedisconnected",
        "connection aborted", "read timed out", "connect timed out",
        "connectionerror", "timeouterror",
    )
    if any(ci in err_str for ci in conn_indicators) or isinstance(exc, (ConnectionError, TimeoutError)):
        return {
            "is_yandex_server": True,
            "category": "connection_drop",
            "title": "Разрыв связи с сервером Яндекса",
            "message": "CDN-узел Яндекса сбросил TCP-соединение или задержал ответ по таймауту.",
            "action": "Скрипт автоматически восстановит соединение и повторит скачивание файла.",
        }

    if (
        err_str.startswith("auth_expired |")
        or _HTTP_AUTH_RE.search(err_str)
        or "unauthorized" in err_str
        or "forbidden" in err_str
        or "passport.yandex" in err_str
    ):
        return {
            "is_yandex_server": False,
            "category": "auth_expired",
            "title": "Срок действия токена или cookies истёк (401/403)",
            "message": "Яндекс отклонил ключ доступа. Токен или cookies.json устарели.",
            "action": f"Обновите токен или куки во вкладке «Настройки». Инструкция: {YOUTUBE_VIDEO_URL}",
        }

    if _HTTP_404_RE.search(err_str) or "pathnotfound" in err_str or "notfound" in err_str:
        return {
            "is_yandex_server": False,
            "category": "not_found",
            "title": "Файл не найден на Яндекс.Диске (404)",
            "message": "Файл был удалён или перемещён в корзину на Диске.",
            "action": "Файл безопасно пропущен в очереди.",
        }

    return {
        "is_yandex_server": False,
        "category": "generic",
        "title": "Ошибка скачивания",
        "message": str(exc),
        "action": f"При повторении проблемы напишите в Telegram: {TELEGRAM_CHANNEL}",
    }
