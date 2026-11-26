"""Общие утилиты: хеширование, длинные пути, санитизация имён, SQLite-транзакции и коды выхода."""

from __future__ import annotations

import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger("unlim_uploader")

IS_WINDOWS = sys.platform.startswith("win")
CASE_INSENSITIVE_PATHS = IS_WINDOWS or sys.platform == "darwin"
WIN_LONG_PATH_THRESHOLD = 240
MAX_REMOTE_NAME_BYTES = 240  # запас до лимита 255 байт UTF-8 на стороне MPFS

# Коды выхода CLI (единые для консоли и Web UI)
EXIT_OK = 0
EXIT_PARTIAL = 1  # проход завершён, но по отдельным файлам есть ошибки
EXIT_AUTH = 2  # cookies.json отсутствует или сессия истекла
EXIT_OUTAGE = 3  # Яндекс недоступен / 429 / капча — повторить позже
EXIT_LOCAL_IO = 4  # локальный диск/папка недоступны или кончилось место под БД
EXIT_QUOTA_DRIFT = 6  # нет активного безлимита или начало расходоваться обычное место Диска
EXIT_INTERRUPTED = 130

# Ограничения конкурентности (бережный режим по умолчанию, чтобы не ловить 429/капчу)
DEFAULT_WORKERS = 2
MAX_WORKERS = 3
DEFAULT_CHUNK_MB = 16
MIN_CHUNK_MB = 4
MAX_CHUNK_MB = 64

_FORBIDDEN_NAME_CHARS = re.compile(r'[\x00-\x1f<>:"|?*\\/]+')
_URL_REDACT_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def ensure_utf8_stdio() -> None:
    """Переводит stdout/stderr в UTF-8 на Windows, чтобы кириллица не падала в cp1251/cp866."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except (OSError, ValueError, AttributeError) as e:
                log.debug("ensure_utf8_stdio: %s", e)


def redact_urls(text: str) -> str:
    """Скрывает query-параметры и токены подписанных URL в сообщениях об ошибках и логах."""
    if not text:
        return ""

    def _mask(m: re.Match[str]) -> str:
        raw = m.group(0)
        head = raw.split("?", 1)[0]
        parts = head.split("/", 4)
        if len(parts) >= 4:
            return "/".join(parts[:3]) + "/<redacted>"
        return head

    return _URL_REDACT_RE.sub(_mask, text)


_EXT_DRIVE_RE = re.compile(r"^\\\\\?\\[A-Za-z]:\\")


def validate_source_dir_safe(raw_path: str, *, allow_unc: bool = False) -> str:
    """Проверяет, что путь папки-источника безопасен (без control chars, UNC/device путей)."""
    if not isinstance(raw_path, str):
        raise ValueError("Некорректный путь к папке")
    cleaned = raw_path.strip()
    if not cleaned:
        raise ValueError("Не указана папка с фото и видео")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("Путь к папке содержит недопустимые управляющие символы")
    if not allow_unc:
        norm_slash = cleaned.replace("/", "\\")
        upper = norm_slash.upper()
        if upper.startswith(("\\\\.\\", "\\\\??\\", "\\\\?\\UNC\\")):
            raise ValueError(
                "Сетевые UNC-пути и пути устройств отключены из соображений безопасности"
            )
        if upper.startswith("\\\\?\\"):
            if not _EXT_DRIVE_RE.match(norm_slash) or ".." in norm_slash.split("\\"):
                raise ValueError(
                    "Сетевые UNC-пути и пути устройств отключены из соображений безопасности"
                )
        elif norm_slash.startswith("\\\\"):
            raise ValueError(
                "Сетевые UNC-пути (\\\\сервер\\папка) отключены; подключите папку как локальный диск"
            )
        try:
            resolved = os.path.realpath(cleaned)
        except OSError:
            resolved = cleaned
        res_slash = resolved.replace("/", "\\")
        res_upper = res_slash.upper()
        if res_upper.startswith(("\\\\.\\", "\\\\??\\", "\\\\?\\UNC\\")) or (
            res_slash.startswith("\\\\") and not _EXT_DRIVE_RE.match(res_slash)
        ):
            raise ValueError(
                "Сетевые UNC-пути и пути устройств отключены из соображений безопасности"
            )
    return cleaned


def strip_long_prefix(path: str) -> str:
    """Снимает Windows-префикс длинного пути (\\\\?\\ или \\\\?\\UNC\\)."""
    if path[:8].upper() == "\\\\?\\UNC\\":
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def to_long_path(path: str) -> str:
    """Добавляет \\\\?\\ на Windows для длинных путей, чтобы os.stat/open не падали с ENOENT."""
    if not IS_WINDOWS or not path:
        return path
    if path.startswith("\\\\?\\"):
        return path
    abs_p = os.path.abspath(path)
    if len(abs_p) < WIN_LONG_PATH_THRESHOLD:
        return abs_p
    if abs_p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_p[2:]
    return "\\\\?\\" + abs_p


def path_key(path: str) -> str:
    """Ключ нормализованного пути для словарей и БД с учётом регистронезависимости ОС."""
    norm = os.path.normpath(strip_long_prefix(path))
    return norm.casefold() if CASE_INSENSITIVE_PATHS else norm


def is_path_inside(child_path: str, root_path: str) -> bool:
    """Проверяет после разрешения симлинков/junction, что child_path находится внутри root_path."""
    try:
        root_real = path_key(os.path.realpath(to_long_path(root_path)))
        child_real = path_key(os.path.realpath(to_long_path(child_path)))
        return os.path.commonpath([root_real, child_real]) == root_real
    except (OSError, ValueError):
        return False


def sanitize_remote_name(name: str, fallback_stem: str = "media") -> str:
    """Чистит имя файла для плоского раздела /photounlim/<имя> (UTF-8 <= MAX_REMOTE_NAME_BYTES)."""
    cleaned = unicodedata.normalize("NFC", name or "").strip()
    cleaned = _FORBIDDEN_NAME_CHARS.sub("_", cleaned)
    cleaned = cleaned.strip(". ")
    if not cleaned:
        cleaned = fallback_stem
    stem, ext = os.path.splitext(cleaned)
    ext = ext[:20]
    if not stem:
        stem = fallback_stem
    return truncate_utf8_name(stem, ext, MAX_REMOTE_NAME_BYTES)


def truncate_utf8_name(stem: str, ext: str, max_bytes: int = MAX_REMOTE_NAME_BYTES) -> str:
    """Укорачивает основу имени так, чтобы (stem + ext) в UTF-8 не превышал max_bytes."""
    ext_bytes = len(ext.encode("utf-8"))
    budget = max(1, max_bytes - ext_bytes)
    raw = stem.encode("utf-8")
    if len(raw) <= budget:
        return stem + ext
    cut = raw[:budget]
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    safe_stem = cut.decode("utf-8", errors="ignore").rstrip(". ") or "f"
    return safe_stem + ext


def name_with_tag(filename: str, tag: str, max_bytes: int = MAX_REMOTE_NAME_BYTES) -> str:
    """Вставляет суффикс перед расширением: IMG_0001.JPG + '_a1b2c3' -> IMG_0001_a1b2c3.JPG."""
    stem, ext = os.path.splitext(filename)
    ext_and_tag = tag + ext
    return truncate_utf8_name(stem, ext_and_tag, max_bytes)


class CancelledError(Exception):
    """Операция прервана по запросу остановки."""


def compute_hashes_with_size(
    local_path: str,
    *,
    chunk_size: int = 1024 * 1024,
    stop_event: threading.Event | None = None,
) -> tuple[str, str, int]:
    """Считает MD5, SHA-256 и точное число прочитанных байт за ОДИН потоковый проход (read-only)."""
    h_md5 = hashlib.md5()
    h_sha256 = hashlib.sha256()
    total = 0
    with open(to_long_path(local_path), "rb") as f:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Остановлено пользователем во время хеширования")
            block = f.read(chunk_size)
            if not block:
                break
            total += len(block)
            h_md5.update(block)
            h_sha256.update(block)
    return h_md5.hexdigest(), h_sha256.hexdigest(), total


def compute_hashes(
    local_path: str,
    *,
    chunk_size: int = 1024 * 1024,
    stop_event: threading.Event | None = None,
) -> tuple[str, str]:
    """Считает MD5 и SHA-256 за один проход."""
    md5, sha256, _ = compute_hashes_with_size(
        local_path, chunk_size=chunk_size, stop_event=stop_event
    )
    return md5, sha256


@contextmanager
def sqlite_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Явная короткая транзакция BEGIN IMMEDIATE -> COMMIT / ROLLBACK."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as rb_err:
            log.debug("sqlite_tx rollback error: %s", rb_err)
        raise


def is_sqlite_disk_error(exc: BaseException) -> bool:
    """Фатальные ошибки SQLite (переполнение диска, I/O, повреждение БД, блокировка)."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    low = str(exc).lower()
    return any(
        t in low
        for t in (
            "disk is full",
            "disk i/o error",
            "readonly database",
            "unable to open database",
            "database disk image is malformed",
            "database is locked",
            "not a database",
        )
    )


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= (n % 100) <= 19:
        return many
    r = n % 10
    if r == 1:
        return one
    if 2 <= r <= 4:
        return few
    return many


def files_text(n: int) -> str:
    return f"{int(n):,}".replace(",", " ") + " " + plural(n, "файл", "файла", "файлов")


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "0 Б"
    val = float(n)
    sign = "-" if val < 0 else ""
    val = abs(val)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if val < 1024.0 or unit == "ТБ":
            if unit == "Б":
                return f"{sign}{int(val)} {unit}"
            return f"{sign}{val:.1f} {unit}" if val < 100 else f"{sign}{val:.0f} {unit}"
        val /= 1024.0
    return f"{sign}{val:.1f} ТБ"


def hhmm(ts: float | None = None) -> str:
    t = time.time() if ts is None else ts
    return time.strftime("%H:%M:%S", time.localtime(t))


@contextmanager
def keep_awake() -> Iterator[None]:
    """Не даёт компьютеру уснуть во время загрузки (Windows и macOS)."""
    caffeinate = None
    if IS_WINDOWS:
        try:
            import ctypes

            res = ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
            if not res:
                log.debug("SetThreadExecutionState вернул 0")
        except Exception as e:  # noqa: BLE001
            log.debug("keep_awake Windows error: %s", e)
    elif sys.platform == "darwin":
        try:
            caffeinate = subprocess.Popen(
                ["caffeinate", "-i", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            log.debug("keep_awake caffeinate unavailable: %s", e)
            caffeinate = None
    try:
        yield
    finally:
        if IS_WINDOWS:
            try:
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception as e:  # noqa: BLE001
                log.debug("keep_awake Windows reset error: %s", e)
        elif caffeinate is not None:
            try:
                caffeinate.terminate()
                try:
                    caffeinate.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    caffeinate.kill()
                    caffeinate.wait(timeout=2)
            except OSError as e:
                log.debug("keep_awake caffeinate stop error: %s", e)


class WindowsSafeRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler, не теряющий записи и бэкапы при WinError 32 (файл открыт на чтение)."""

    def __init__(
        self,
        filename: str | os.PathLike[str],
        maxBytes: int = 5 * 1024 * 1024,
        backupCount: int = 3,
        encoding: str = "utf-8",
        delay: bool = True,
    ) -> None:
        super().__init__(
            os.fspath(filename),
            mode="a",
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay,
        )

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        if self.backupCount > 0:
            try:
                for i in range(self.backupCount - 1, 0, -1):
                    sfn = self.rotation_filename(f"{self.baseFilename}.{i}")
                    dfn = self.rotation_filename(f"{self.baseFilename}.{i + 1}")
                    if os.path.exists(sfn):
                        os.replace(sfn, dfn)
                dfn = self.rotation_filename(f"{self.baseFilename}.1")
                if os.path.exists(self.baseFilename):
                    os.replace(self.baseFilename, dfn)
            except OSError:
                # Файл временно занят (например, чтением support-bundle на Windows):
                # не удаляем бэкап .1 и продолжаем дописывать, повторим ротацию позже.
                pass
        if not self.delay:
            self.stream = self._open()
