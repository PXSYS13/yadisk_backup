"""Утилиты: санитизация путей, MD5, длинные пути Windows, общие лимиты и хелперы."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

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

APP_VERSION = "2.2.0"

# ── Общие лимиты (единое место для CLI, Web UI и pipeline) ──────────────────

# Больше 5 параллельных скачиваний через REST API — Яндекс режет по rate limit.
MAX_DISK_WORKERS = 5
# Воркеры безлимита качают ZIP-пачки — это тяжелее для серверов архивации.
MAX_UNLIM_WORKERS = 6
DEFAULT_UNLIM_BATCH = 25
MAX_UNLIM_BATCH = 80

# ── Страховочные превью ─────────────────────────────────────────────────────
# Пока оригинал фото лежит на молчащем сервере Яндекса, сохраняем его превью
# (JPEG до 1280 px) — чтобы у человека было хоть что-то уже сейчас.
PREVIEW_DIRNAME = "_превью"


def previews_enabled() -> bool:
    """Превью включены, пока YADISK_PREVIEWS не выключен явно (0/false/no/off)."""
    return os.environ.get("YADISK_PREVIEWS", "1").strip().lower() not in ("0", "false", "no", "off")


def preview_path_for(download_dir: str, original_path: str) -> str:
    """<папка>/_превью/<путь оригинала относительно папки>.jpg — рядом не перепутать с оригиналом."""
    try:
        rel = os.path.relpath(original_path, download_dir)
    except ValueError:  # другой диск на Windows
        rel = os.path.basename(original_path)
    if rel.startswith(".."):
        rel = os.path.basename(original_path)
    return os.path.join(download_dir, PREVIEW_DIRNAME, rel + ".jpg")


@contextmanager
def keep_awake() -> Iterator[None]:
    """Не даёт компьютеру уснуть, пока идёт скачивание (Windows и macOS).

    На Windows флаг действует для вызывающего потока — звать из потока, который живёт
    всё время скачивания. Экран при этом гаснуть может, засыпать система — нет.
    """
    caffeinate = None
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM
        except Exception:  # noqa: BLE001 — не получилось — просто качаем дальше
            pass
    elif sys.platform == "darwin":
        try:
            caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            caffeinate = None
    try:
        yield
    finally:
        if IS_WINDOWS:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:  # noqa: BLE001
                pass
        if caffeinate is not None:
            caffeinate.terminate()


# ── Выбор папок Диска ───────────────────────────────────────────────────────

# Корзину не сканируем и выбрать не даём
SKIP_PATH_PREFIXES = ("disk:/Корзина", "disk:/Trash")
# Все выбранные папки уходят аргументами в `main.py scan`, а командная строка
# Windows ограничена 32 767 символами: 50 × 400 с запасом в неё помещаются
MAX_DISK_FOLDERS = 50
MAX_DISK_FOLDER_LEN = 400


def to_disk_path(path: str) -> str:
    """Приводит путь к виду 'disk:/foo/bar' — так пути возвращает API."""
    if path.startswith("disk:"):
        return path
    if path.startswith("/"):
        return "disk:" + path
    return "disk:/" + path


def in_trash(path: str) -> bool:
    """Путь лежит в корзине (папки вроде «Корзина2» не задевает)."""
    norm = to_disk_path(path)
    return any(norm == p or norm.startswith(p + "/") for p in SKIP_PATH_PREFIXES)


def path_in_folders(path: str, folders: Iterable[str]) -> bool:
    """Путь совпадает с одной из папок или лежит внутри неё. «Фото2» не входит в «Фото»."""
    return any(path == f or path.startswith(f + "/") for f in folders)


def normalize_disk_folders(paths: Iterable[str]) -> list[str]:
    """Чистит выбор папок: вид 'disk:/…', без хвостового '/', дублей и вложенных.

    Папка внутри уже выбранной лишняя: родитель и так скачается целиком.
    Ошибки — ValueError с текстом, который можно показать человеку.
    """
    clean: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str):
            raise ValueError("Непонятный путь к папке")
        text = raw.strip()
        if not text:
            continue
        if any(ord(ch) < 32 for ch in text):
            raise ValueError("Непонятный путь к папке")
        parts = [p for p in to_disk_path(text).removeprefix("disk:").split("/") if p]
        if not parts:
            raise ValueError("Чтобы скачать весь Диск, выберите «Весь Диск»")
        path = "disk:/" + "/".join(parts)
        if in_trash(path):
            raise ValueError("Корзину скачать нельзя")
        if len(path) > MAX_DISK_FOLDER_LEN:
            raise ValueError(f"Слишком длинный путь к папке: {path.removeprefix('disk:')[:60]}…")
        clean.add(path)
    kept: list[str] = []
    for path in sorted(clean, key=lambda p: (len(p), p)):
        if not path_in_folders(path, kept):
            kept.append(path)
    if len(kept) > MAX_DISK_FOLDERS:
        raise ValueError(f"Можно выбрать не больше {MAX_DISK_FOLDERS} папок — "
                         f"отметьте папки уровнем выше")
    return sorted(kept, key=lambda p: (p.casefold(), p))


def encode_disk_folders(paths: Iterable[str]) -> str:
    r"""Выбор папок → строка для .env.

    JSON, а не запятые: в именах папок бывают запятые. '$' прячем в $ —
    python-dotenv подставляет ${…} даже внутри одинарных кавычек.
    """
    return json.dumps(list(paths), ensure_ascii=False).replace("$", "\\u0024")


def decode_disk_folders(raw: str | None) -> list[str] | None:
    """Строка из .env → список папок. Пусто или испорчено — None: качаем весь Диск."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if not isinstance(data, list):
            return None
        return normalize_disk_folders(data) or None
    except ValueError:
        return None


# ── Коды выхода скриптов (их читают Rescue в webui.py и фронтенд) ──────────
EXIT_OK = 0
EXIT_PARTIAL = 1      # скачивание закончилось, часть файлов упала
EXIT_AUTH = 2         # токен или cookies действительно недействительны
EXIT_OUTAGE = 3       # Яндекс не отвечает или отказывает — повторить позже
EXIT_DISK_FULL = 4    # локальный диск переполнен или недоступен для записи
EXIT_DEFERRED = 5     # всё доступное скачано, остальное лежит на молчащих серверах Яндекса
EXIT_INTERRUPTED = 130

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


def classify_dir(path: str) -> str:
    """Определяет целевую директорию (Фото, Видео, ...) для файла по расширению."""
    return _CATEGORY_TO_DIR.get(categorize(path), DIR_OTHER)


# ── Санитизация путей ────────────────────────────────────────────────────────


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
    p = remote_path.removeprefix("disk:").lstrip("/")
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


def _short_hash(remote_path: str) -> str:
    return hashlib.sha256(remote_path.encode("utf-8", errors="replace")).hexdigest()[:6]


def resolve_local_path(
    download_dir: str,
    remote_path: str,
    existing_paths: set[str] | None = None,
) -> str:
    """Собирает локальный путь для удалённого файла с санитизацией и поддержкой длинных путей.

    Если при санитизации имя изменилось (были запрещённые символы вроде : ? * или зарезервированные имена),
    добавляет короткий детерминированный хеш remote_path для исключения коллизий (a:b.jpg vs a?b.jpg vs a_b.jpg).
    """
    p = remote_path.removeprefix("disk:").lstrip("/")
    raw_parts = [part for part in p.split("/") if part]
    clean_parts = [sanitize_component(part) for part in raw_parts]

    if clean_parts and raw_parts != clean_parts:
        stem, ext = os.path.splitext(clean_parts[-1])
        clean_parts[-1] = f"{stem}_{_short_hash(remote_path)}{ext}"

    rel = "/".join(clean_parts)
    local = os.path.join(download_dir, rel.replace("/", os.sep))

    if existing_paths is not None:
        check_key = local.lower() if IS_WINDOWS else local
        if check_key in existing_paths:
            stem, ext = os.path.splitext(local)
            local = f"{stem}_{_short_hash(remote_path)}{ext}"
        existing_paths.add(local.lower() if IS_WINDOWS else local)

    return local


def safe_int(val: object, default: int = 4, min_val: int = 1, max_val: int = 16) -> int:
    """Безопасный парсер целых чисел с дефолтом и ограничением диапазона."""
    try:
        n = int(val)  # type: ignore[arg-type]
        return max(min_val, min(max_val, n))
    except (ValueError, TypeError):
        return default


# ── Проверка безопасности целевой папки ─────────────────────────────────────

_WIN_SYSTEM_DIR_RE = re.compile(
    r"^[a-z]:\\(windows|program files|program files \(x86\)|programdata"
    r"|\$recycle\.bin|users\\all users)(\\|$)"
)
_WIN_SYSTEM_ENV_VARS = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
_POSIX_SYSTEM_DIRS = ("/etc", "/usr", "/bin", "/sbin", "/var", "/lib", "/system", "/library")


def _is_drive_root(path: str) -> bool:
    return len(path) <= 3 and path.endswith((":\\", ":/"))


def is_safe_dir(path: str, *, must_exist: bool = False) -> tuple[bool, str]:
    r"""Проверяет, можно ли использовать папку как цель скачивания или сортировки.

    Запрещены:
    - пустые или относительные пути
    - корни дисков (C:\, D:\, /)
    - домашняя папка пользователя целиком (C:\Users\username или /home/user)
    - системные папки (Windows, Program Files, ProgramData, Recycle Bin, /etc, /usr, ...)

    must_exist=False — папку можно создать: достаточно, чтобы существовал
    ближайший существующий предок (целевая папка скачивания).
    must_exist=True — папка обязана существовать (сортировка organize).
    """
    if not path or not path.strip():
        return False, "Пустой путь"
    p_str = path.strip()
    # Префикс длинных путей Windows не должен обходить проверки: \\?\C:\Windows — это C:\Windows
    if p_str.startswith("\\\\?\\UNC\\"):
        p_str = "\\\\" + p_str[8:]
    elif p_str.startswith("\\\\?\\"):
        p_str = p_str[4:]
    raw_norm = p_str.lower().replace("/", "\\")

    # Текстовые проверки Windows-путей — до is_absolute(), чтобы 'C:\' и 'C:\Windows'
    # отклонялись одинаково на любой платформе (CI гоняет тесты и на Linux)
    if _is_drive_root(raw_norm):
        return False, "Нельзя использовать корень диска целиком (укажите конкретную папку)"
    if _WIN_SYSTEM_DIR_RE.match(raw_norm):
        return False, f"Системная папка запрещена: {p_str}"

    try:
        if not Path(p_str).is_absolute():
            return False, "Путь должен быть абсолютным"
        abs_path = os.path.abspath(p_str)
    except (OSError, ValueError):
        return False, "Невалидный путь"

    norm = abs_path.lower().replace("/", "\\")

    # Корень диска (C:\, D:\, /, \)
    if _is_drive_root(abs_path) or norm in ("\\", "/"):
        return False, "Нельзя использовать корень диска целиком (укажите конкретную папку)"

    # Домашняя папка пользователя целиком
    home_dir = os.path.expanduser("~")
    if home_dir and home_dir != "~":
        home_norm = os.path.abspath(home_dir).lower().replace("/", "\\")
        if norm == home_norm or raw_norm == home_norm:
            return False, (
                "Нельзя использовать домашнюю папку пользователя целиком "
                "(укажите конкретную папку бэкапа)"
            )

    # Системные папки Windows по переменным окружения (учитывают нестандартный диск)
    for var in _WIN_SYSTEM_ENV_VARS:
        val = os.environ.get(var)
        if val:
            val_norm = os.path.abspath(val).lower().replace("/", "\\")
            if norm == val_norm or norm.startswith(val_norm + "\\"):
                return False, f"Системная папка запрещена: {val}"

    # Любой диск с системными папками Windows (C:\Windows, D:\Windows, ...)
    if _WIN_SYSTEM_DIR_RE.match(norm):
        return False, f"Системная папка запрещена: {abs_path}"

    # Unix системные пути
    norm_posix = abs_path.replace("\\", "/").lower()
    for bad in _POSIX_SYSTEM_DIRS:
        if norm_posix == bad or norm_posix.startswith(bad + "/"):
            return False, f"Системная папка запрещена: {bad}"

    if must_exist:
        if not os.path.isdir(abs_path):
            return False, "Папка не существует"
        return True, "OK"

    # Папки ещё нет — должен существовать ближайший предок
    parent = Path(abs_path).parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.exists():
        return False, "Родительская директория недоступна"
    return True, "OK"


def is_safe_download_dir(path: str) -> tuple[bool, str]:
    """Проверка целевой папки скачивания (может ещё не существовать)."""
    return is_safe_dir(path, must_exist=False)


# ── Коллизии регистра на Windows ────────────────────────────────────────────


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


# ── SQLite ──────────────────────────────────────────────────────────────────


def sqlite_ro_uri(db_path: str | os.PathLike) -> str:
    """URI для read-only подключения к SQLite.

    Экранирует спецсимволы пути: '#' в URI начинает фрагмент, и sqlite молча
    открыл бы ПУСТУЮ базу вместо нашей (проверено на Windows-пути с решёткой).
    """
    return Path(db_path).resolve().as_uri() + "?mode=ro"


@contextmanager
def sqlite_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Явная транзакция: BEGIN IMMEDIATE → COMMIT, при любой ошибке — ROLLBACK.

    BEGIN IMMEDIATE сразу берёт блокировку на запись — в WAL-режиме это
    спасает от `database is locked` при нескольких писателях.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


# ── Файлы и форматирование ──────────────────────────────────────────────────


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


def remove_quiet(path: str) -> None:
    """Удаляет файл, если он есть. Ошибки глотает — это уборка, не бизнес-логика."""
    try:
        os.remove(path)
    except OSError:
        pass


# Ошибки локального диска: кончилось место, нет прав, путь занят файлом, диск отключён.
# Сколько ни повторяй запрос к Яндексу — они не пройдут.
LOCAL_DISK_ERRNOS = frozenset({
    errno.ENOSPC, errno.EACCES, errno.EPERM, errno.EROFS, errno.EEXIST,
    errno.EISDIR, errno.ENOTDIR, errno.ENAMETOOLONG, errno.ENOENT,
})


def is_local_disk_error(exc: BaseException) -> bool:
    """OSError локальной файловой системы (не сеть): место, права, отсутствующий диск."""
    return (
        isinstance(exc, OSError)
        and not isinstance(exc, (ConnectionError, TimeoutError))
        and getattr(exc, "errno", None) in LOCAL_DISK_ERRNOS
    )


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


def ensure_utf8_stdio() -> None:
    """Переключает stdout/stderr в UTF-8 — кириллица в консоли Windows без крякозябр."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


# ── Ссылки сообщества ────────────────────────────────────────────────────────
TELEGRAM_CHANNEL = "https://t.me/davronsv"
TELEGRAM_HANDLE = "@davronsv"
YOUTUBE_VIDEO_URL = "https://www.youtube.com/watch?v=XAiJxqHkEUA"
DONATION_URL = "https://dalink.to/davronsvv"


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
    type_name = type(exc).__name__.lower() if isinstance(exc, BaseException) else ""

    # 0. Локальный диск — проверяем ПЕРВЫМ: в тексте OSError есть путь к файлу,
    #    а в пути бывают «500» и «notfound», которые ниже приняли бы за сбой Яндекса
    if err_str.startswith("local_disk |") or (isinstance(exc, BaseException) and is_local_disk_error(exc)):
        return {
            "is_yandex_server": False,
            "category": "local_disk",
            "title": "Проблема с локальным диском",
            "message": "Нет места, нет прав на запись или папка скачивания недоступна (отключён диск?).",
            "action": "Освободите место или проверьте папку скачивания в настройках, затем повторите.",
        }

    # 1. 429 Too Many Requests (Rate limit Яндекса)
    if "429" in err_str or "toomanyrequests" in err_str or "toomanyrequests" in type_name:
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
