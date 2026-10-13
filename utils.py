"""Утилиты: санитизация путей, MD5, работа с длинными путями Windows."""

from __future__ import annotations

import hashlib
import math
import os
import sys

# Символы, запрещённые в именах файлов на Windows.
# Слеши тоже сюда: имя файла с '\' или '/' на Яндекс.Диске легально, а локально
# превратилось бы в разделитель пути и увело файл мимо DOWNLOAD_DIR.
_WIN_FORBIDDEN = '<>:"|?*\\/'
# Зарезервированные имена Windows
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

IS_WINDOWS = sys.platform.startswith("win")
# Порог, после которого включаем префикс \\?\ для длинных путей
WIN_LONG_PATH_THRESHOLD = 240

# ── Расширения файлов по категориям ─────────────────────────────────────────

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif",
    ".bmp", ".tiff", ".tif", ".raw", ".arw", ".cr2", ".nef", ".dng", ".orf",
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".3gp", ".3g2",
    ".mpg", ".mpeg", ".wmv", ".flv", ".vob", ".mts", ".m2ts",
}
AUDIO_EXTS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".aac", ".wma",
    ".opus", ".aiff", ".alac",
}
DOC_EXTS = {
    ".doc", ".docx", ".pdf", ".txt", ".rtf", ".odt",
    ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx", ".odp",
}
ARCHIVE_EXTS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
    ".cab", ".tgz", ".tbz", ".lz", ".lzma",
}
PROGRAM_EXTS = {
    ".exe", ".dll", ".msi", ".bat", ".cmd", ".sys", ".com",
    ".scr", ".vbs", ".ps1", ".sh", ".jar",
}
MEDIA_EXTS = PHOTO_EXTS | VIDEO_EXTS

CATEGORIES = {
    "photos": PHOTO_EXTS,
    "videos": VIDEO_EXTS,
    "audio": AUDIO_EXTS,
    "documents": DOC_EXTS,
    "archives": ARCHIVE_EXTS,
    "programs": PROGRAM_EXTS,
}

CATEGORY_LABELS = {
    "photos": "Фото",
    "videos": "Видео",
    "audio": "Аудио",
    "documents": "Документы",
    "archives": "Архивы",
    "programs": "Программы",
    "other": "Прочее",
}

# Директории-назначения для organize.py
DIR_PHOTO = "Фото"
DIR_VIDEO = "Видео"
DIR_DOC = "Документы"
DIR_ARCHIVE = "Архивы"
DIR_PROGRAM = "Программы"
DIR_AUDIO = "Аудио"
DIR_OTHER = "Прочее"
DIR_DUP = "_duplicates"


def categorize(path: str) -> str:
    """Определяет категорию файла по расширению."""
    ext = os.path.splitext(path)[1].lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "other"


def is_media(path: str) -> bool:
    """Проверяет, является ли файл медиа (фото или видео) по расширению."""
    ext = os.path.splitext(path)[1].lower()
    return ext in MEDIA_EXTS


# Маппинг категория → имя DIR_*
_CATEGORY_TO_DIR = {
    "photos": DIR_PHOTO,
    "videos": DIR_VIDEO,
    "audio": DIR_AUDIO,
    "documents": DIR_DOC,
    "archives": DIR_ARCHIVE,
    "programs": DIR_PROGRAM,
    "other": DIR_OTHER,
}


def classify_dir(path: str) -> str:
    """Определяет целевую директорию (Фото, Видео, ...) для файла по расширению."""
    return _CATEGORY_TO_DIR.get(categorize(path), DIR_OTHER)


def sanitize_component(name: str) -> str:
    """Чистит ОДИН компонент пути (имя файла или папки) от запрещённых символов."""
    if not name:
        return "_"
    cleaned = []
    for ch in name:
        if ch in _WIN_FORBIDDEN or ord(ch) < 32:
            cleaned.append("_")
        else:
            cleaned.append(ch)
    out = "".join(cleaned).rstrip(" .")  # Windows не любит хвостовые пробелы и точки
    if not out or out in (".", ".."):
        out = "_"
    # Зарезервированные имена
    stem = out.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        out = "_" + out
    return out


def sanitize_remote_path(remote_path: str) -> str:
    """Превращает /disk/foo:bar/file?.jpg → foo_bar/file_.jpg (относительный путь)."""
    # Убираем префикс disk: если есть
    p = remote_path
    p = p.removeprefix("disk:")
    p = p.lstrip("/")
    parts = [sanitize_component(part) for part in p.split("/") if part]
    return "/".join(parts)


def ensure_dir(path: str | os.PathLike) -> None:
    """Создаёт директорию и все родительские, если их нет."""
    os.makedirs(path, exist_ok=True)


def to_long_path(path: str) -> str:
    r"""Для Windows — добавляет префикс \\?\ если путь длинный.
    На других платформах возвращает как есть.
    """
    if not IS_WINDOWS:
        return path
    abs_path = os.path.abspath(path)
    if len(abs_path) < WIN_LONG_PATH_THRESHOLD:
        return abs_path
    if abs_path.startswith("\\\\?\\"):
        return abs_path
    # UNC-пути (\\server\share\...) требуют другой префикс
    if abs_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_path[2:]
    return "\\\\?\\" + abs_path


def resolve_local_path(download_dir: str, remote_path: str) -> str:
    """Собирает локальный путь для удалённого файла с санитизацией и поддержкой длинных путей."""
    rel = sanitize_remote_path(remote_path)
    local = os.path.join(download_dir, rel.replace("/", os.sep))
    return local


def avoid_case_collision(local_path: str) -> str:
    """На Windows два файла foo.txt и FOO.txt — одно и то же.
    Если файл с таким именем (без учёта регистра) уже занят другим путём — добавляем суффикс _1, _2 и т.д.
    Учитываем только реально существующие файлы.
    """
    if not IS_WINDOWS:
        return local_path
    parent = os.path.dirname(local_path)
    name = os.path.basename(local_path)
    if not parent or not os.path.isdir(parent):
        return local_path
    try:
        existing = os.listdir(parent)
    except OSError:
        return local_path
    if name in existing:
        return local_path
    existing_lower = {e.lower(): e for e in existing}
    if name.lower() in existing_lower:
        return _suffix_path(local_path, existing_lower)
    return local_path


def _suffix_path(local_path: str, existing_lower: dict[str, str]) -> str:
    base, ext = os.path.splitext(local_path)
    i = 1
    while True:
        candidate = f"{base}_{i}{ext}"
        if os.path.basename(candidate).lower() not in existing_lower:
            return candidate
        i += 1


def sqlite_ro_uri(db_path: str | os.PathLike) -> str:
    """URI для read-only подключения к SQLite.

    Экранирует спецсимволы пути: '#' в URI начинает фрагмент, и sqlite молча
    открыл бы ПУСТУЮ базу вместо нашей (проверено на Windows-пути с решёткой).
    """
    from pathlib import Path

    return Path(db_path).resolve().as_uri() + "?mode=ro"


def compute_md5(local_path: str, chunk: int = 1024 * 1024) -> str:
    """Считает MD5 локального файла стримом — не грузит файл целиком в память."""
    h = hashlib.md5()
    path = to_long_path(local_path)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def file_size_safe(local_path: str) -> int | None:
    """Размер локального файла или None если файла нет."""
    try:
        return os.path.getsize(to_long_path(local_path))
    except OSError:
        return None


def human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def human_duration(seconds: float | None) -> str:
    """Форматирует длительность в формат HH:MM:SS."""
    if seconds is None or seconds < 0 or math.isnan(seconds):
        return "00:00:00"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def human_duration_ru(seconds: float | None) -> str:
    """Форматирует длительность в читаемый русский вид (1ч 02м 03с)."""
    if seconds is None or seconds < 0 or math.isnan(seconds):
        return "—"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}ч {m:02d}м {s:02d}с"
    if m > 0:
        return f"{m}м {s:02d}с"
    return f"{s}с"


# ── Ссылки сообщества ────────────────────────────────────────────────────────
TELEGRAM_CHANNEL = "https://t.me/davronsv"
TELEGRAM_HANDLE = "@davronsv"
YOUTUBE_VIDEO_URL = "https://www.youtube.com/watch?v=XAiJxqHkEUA"


def classify_yandex_error(exc: BaseException | str | None) -> dict:
    """Анализирует ошибку и определяет, возникла ли она на стороне серверов Яндекса.

    Возвращает словарь:
    {
        "is_yandex_server": bool,
        "category": str,
        "title": str,
        "message": str,
        "action": str,
    }
    """
    if exc is None:
        return {
            "is_yandex_server": False,
            "category": "none",
            "title": "Ошибок нет",
            "message": "Всё работает штатно",
            "action": "",
        }

    err_str = str(exc).lower()
    type_name = type(exc).__name__ if isinstance(exc, BaseException) else ""

    # 1. 429 Too Many Requests (Rate limit Яндекса)
    if "429" in err_str or "toomanyrequests" in err_str or "toomanyrequests" in type_name.lower():
        return {
            "is_yandex_server": True,
            "category": "rate_limit",
            "title": "Лимит запросов Яндекса (HTTP 429 Too Many Requests)",
            "message": "Серверы Яндекса временно ограничили частоту скачивания. Скрипт ожидает паузу.",
            "action": f"Рекомендуется снизить число параллельных воркеров до 2–3. Новости: {TELEGRAM_CHANNEL}",
        }

    # 2. Сбои оборудования / дата-центров Яндекса (500, 502 Bad Gateway, 503 Service Unavailable, 504 Gateway Timeout)
    server_codes = ("500", "502", "503", "504")
    server_types = ("badgateway", "gatewaytimeout", "internalserver", "unavailableerror", "servererror")
    if (
        any(code in err_str for code in server_codes)
        or any(st in type_name.lower() for st in server_types)
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

    # 3. Обрывы связи и сбросы TCP-соединений CDN Яндекса
    conn_indicators = (
        "connectionreset", "connection reset", "remotedisconnected",
        "connection aborted", "read timed out", "connect timed out",
        "connectionerror", "timeouterror"
    )
    if any(ci in err_str for ci in conn_indicators) or isinstance(exc, (ConnectionError, TimeoutError)):
        return {
            "is_yandex_server": True,
            "category": "connection_drop",
            "title": "Разрыв связи с сервером Яндекса",
            "message": "CDN-узел Яндекса сбросил TCP-соединение или задержал ответ по таймауту.",
            "action": "Скрипт автоматически восстановит соединение и повторит скачивание файла.",
        }

    # 4. Проблемы с авторизацией (клиентские: 401, 403, протухли куки/токен)
    if "401" in err_str or "unauthorized" in err_str or "403" in err_str or "forbidden" in err_str or "passport.yandex" in err_str:
        return {
            "is_yandex_server": False,
            "category": "auth_expired",
            "title": "Срок действия токена или cookies истёк (401/403)",
            "message": "Яндекс отклонил ключ доступа. Токен или cookies.json устарели.",
            "action": f"Обновите токен или куки во вкладке 'Настройка'. Инструкция: {YOUTUBE_VIDEO_URL}",
        }

    # 5. Файл не найден (404)
    if "404" in err_str or "pathnotfound" in err_str or "notfound" in err_str:
        return {
            "is_yandex_server": False,
            "category": "not_found",
            "title": "Файл не найден на Яндекс.Диске (404)",
            "message": "Файл был удалён или перемещён в корзину на Диске.",
            "action": "Файл безопасно пропущен в очереди.",
        }

    # 6. Прочие ошибки
    return {
        "is_yandex_server": False,
        "category": "generic",
        "title": "Ошибка скачивания",
        "message": str(exc),
        "action": f"При повторении проблемы напишите в Telegram: {TELEGRAM_CHANNEL}",
    }


