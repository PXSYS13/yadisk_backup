"""Рекурсивный read-only сканер фото и видео, проверка сигнатур и извлечение даты съёмки."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import re
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image

from sorter.state import SorterState
from sorter.utils import (
    CancelledError,
    compute_partial_hash,
    is_path_inside,
    is_symlink_or_junction,
    path_key,
    plausible_ts,
    to_long_path,
    validate_dir_safe,
)

log = logging.getLogger("photo_sorter.scan")

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    HEIF_AVAILABLE = False
    log.warning(
        "Модуль pillow_heif недоступен (%s) — чтение HEIC/HEIF может быть ограничено",
        exc,
    )

PHOTO_EXTS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".jpe",
        ".png",
        ".heic",
        ".heif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
    }
)

VIDEO_EXTS = frozenset(
    {
        ".mp4",
        ".mov",
        ".m4v",
        ".3gp",
        ".3g2",
        ".mkv",
        ".webm",
        ".avi",
        ".mts",
        ".m2ts",
        ".mpg",
        ".mpeg",
        ".wmv",
        ".flv",
    }
)

SKIP_DIR_NAMES = frozenset(
    {
        "$recycle.bin",
        "system volume information",
        ".trash",
        ".trashes",
        ".spotlight-v100",
        ".fseventsd",
        "@eadir",
        "#recycle",
        ".git",
        "__pycache__",
        ".venv",
        "node_modules",
        ".models",
        ".cache",
        ".sorter_data",
    }
)

SKIP_SUFFIXES = (".part", ".tmp", ".crdownload", ".download", ".db", ".db-wal", ".db-shm")

_MP4_EPOCH_OFFSET = 2_082_844_800
_FILENAME_DATE_PATTERNS = (
    re.compile(
        r"(?<!\d)(19\d{2}|20\d{2})[-_.](\d{2})[-_.](\d{2})(?:[-_ T](\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2}))?(?!\d)"
    ),
    re.compile(
        r"(?<!\d)(19\d{2}|20\d{2})(\d{2})(\d{2})(?:[-_ T](\d{2})(\d{2})(\d{2}))?(?!\d)"
    ),
)


@dataclass(frozen=True)
class MediaVerdict:
    accepted: bool
    media_kind: str | None  # 'photo' | 'video' | None
    reason: str | None = None
    io_error: bool = False


@dataclass
class ScanSummary:
    source_dir: str
    discovered: int = 0
    photos: int = 0
    videos: int = 0
    new_or_changed: int = 0
    unchanged_skipped: int = 0
    unsupported_skipped: int = 0
    io_errors: int = 0


def should_skip_dir(name: str) -> bool:
    return name.casefold() in SKIP_DIR_NAMES


def _has_valid_signature(ext: str, header: bytes) -> bool:
    if len(header) < 4:
        return False
    if ext in (".jpg", ".jpeg", ".jpe"):
        return header[:3] == b"\xff\xd8\xff"
    if ext == ".png":
        return header[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == ".bmp":
        return header[:2] == b"BM"
    if ext == ".webp":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if ext in (".tif", ".tiff"):
        return header[:4] in (b"II\x2a\x00", b"MM\x00\x2a", b"II\x2b\x00", b"MM\x00\x2b")
    if ext in (".heic", ".heif", ".mp4", ".m4v", ".mov", ".3gp", ".3g2"):
        return len(header) >= 8 and header[4:8] in (
            b"ftyp",
            b"moov",
            b"mdat",
            b"wide",
            b"free",
            b"skip",
            b"pnot",
        )
    if ext in (".mkv", ".webm"):
        return header[:4] == b"\x1a\x45\xdf\xa3"
    if ext == ".avi":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"AVI "
    if ext == ".wmv":
        return len(header) >= 16 and header[:4] == b"\x30\x26\xb2\x75"
    if ext == ".flv":
        return header[:3] == b"FLV"
    if ext in (".mpg", ".mpeg"):
        return header[:3] == b"\x00\x00\x01"
    if ext in (".mts", ".m2ts"):
        return header[0] == 0x47 or (len(header) >= 5 and header[4] == 0x47)
    return True


def classify_media_file(local_path: str, size: int, *, verify_magic: bool = True) -> MediaVerdict:
    """Классифицирует файл как photo/video с проверкой сигнатуры."""
    low_name = os.path.basename(local_path).lower()
    if low_name.endswith(SKIP_SUFFIXES):
        return MediaVerdict(False, None, "временный или служебный файл")
    ext = os.path.splitext(low_name)[1]
    if ext in PHOTO_EXTS:
        kind = "photo"
    elif ext in VIDEO_EXTS:
        kind = "video"
    else:
        return MediaVerdict(False, None, f"не фото и не видео ({ext or 'без расширения'})")
    if size <= 0:
        return MediaVerdict(False, None, "пустой файл (0 байт)")
    if verify_magic:
        try:
            with open(to_long_path(local_path), "rb") as f:
                head = f.read(32)
        except OSError as e:
            return MediaVerdict(
                False, kind, f"не удалось прочитать файл: {e.strerror or e}", io_error=True
            )
        if not _has_valid_signature(ext, head):
            return MediaVerdict(False, None, f"повреждён или не соответствует формату {ext}")
    return MediaVerdict(True, kind, None)


def _parse_exif_dt_str(text: str) -> float | None:
    cleaned = text.split("\x00", 1)[0].strip()
    m = re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[\sT]+(\d{2}):(\d{2}):(\d{2})", cleaned)
    if not m:
        return None
    try:
        y, mo, d, hh, mm, ss = (int(g) for g in m.groups())
        ts = datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return plausible_ts(ts)


def _parse_exif_dt(raw: bytes) -> float | None:
    return _parse_exif_dt_str(raw.decode("ascii", errors="ignore"))


def _read_tiff_exif_time(tiff: bytes) -> float | None:
    if len(tiff) < 8:
        return None
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        return None
    magic, ifd0_off = struct.unpack_from(endian + "HI", tiff, 2)
    if magic != 42:
        return None

    found: dict[int, float] = {}

    def walk_ifd(offset: int, depth: int = 0) -> None:
        if depth > 2 or offset <= 0 or offset + 2 > len(tiff):
            return
        count = struct.unpack_from(endian + "H", tiff, offset)[0]
        pos = offset + 2
        for _ in range(min(count, 256)):
            if pos + 12 > len(tiff):
                break
            tag, typ, cnt, val_off = struct.unpack_from(endian + "HHII", tiff, pos)
            pos += 12
            if tag == 0x8769 and typ == 4 and cnt == 1:
                walk_ifd(val_off, depth + 1)
            elif tag in (0x9003, 0x9004, 0x0132) and typ == 2 and 19 <= cnt <= 64:
                if val_off + cnt <= len(tiff):
                    dt = _parse_exif_dt(tiff[val_off : val_off + cnt])
                    if dt is not None and tag not in found:
                        found[tag] = dt

    walk_ifd(ifd0_off)
    for tag in (0x9003, 0x9004, 0x0132):
        if tag in found:
            return found[tag]
    return None


def _read_jpeg_exif_time(local_path: str) -> float | None:
    try:
        with open(to_long_path(local_path), "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            for _ in range(64):
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                while marker[1] == 0xFF:
                    nxt = f.read(1)
                    if not nxt:
                        return None
                    marker = b"\xff" + nxt
                code = marker[1]
                if code in (0xDA, 0xD9):
                    return None
                if 0xD0 <= code <= 0xD8 or code == 0x01:
                    continue
                seg_len_raw = f.read(2)
                if len(seg_len_raw) < 2:
                    return None
                seg_len = struct.unpack(">H", seg_len_raw)[0]
                if seg_len < 2:
                    return None
                if code == 0xE1:
                    payload = f.read(min(seg_len - 2, 131072))
                    if payload.startswith(b"Exif\x00\x00"):
                        ts = _read_tiff_exif_time(payload[6:])
                        if ts is not None:
                            return ts
                    rem = (seg_len - 2) - len(payload)
                    if rem > 0:
                        f.seek(rem, os.SEEK_CUR)
                else:
                    f.seek(seg_len - 2, os.SEEK_CUR)
    except OSError as e:
        log.debug("EXIF JPEG read failed for %s: %s", local_path, e)
        return None
    return None


def _read_pillow_exif_time(local_path: str) -> float | None:
    """Читает EXIF через Pillow/pillow-heif для HEIC/HEIF/WebP/PNG."""
    try:
        with Image.open(to_long_path(local_path)) as img:
            exif = img.getexif()
            if not exif:
                return None
            # Проверяем Exif sub-IFD (0x8769) и основной IFD
            candidates: list[str] = []
            try:
                sub = exif.get_ifd(0x8769)
            except Exception:  # noqa: BLE001
                sub = {}
            for src in (sub, exif):
                for tag in (0x9003, 0x9004, 0x0132):
                    val = src.get(tag)
                    if isinstance(val, str):
                        candidates.append(val)
                    elif isinstance(val, bytes):
                        candidates.append(val.decode("ascii", errors="ignore"))
            for text in candidates:
                ts = _parse_exif_dt_str(text)
                if ts is not None:
                    return ts
    except Exception as e:  # noqa: BLE001
        log.debug("Pillow EXIF read failed for %s: %s", local_path, e)
    return None


def _read_mp4_mvhd_time(local_path: str) -> float | None:
    try:
        with open(to_long_path(local_path), "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            f.seek(0)
            pos = 0
            while pos + 8 <= file_size:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    return None
                size, btype = struct.unpack(">I4s", hdr)
                hdr_len = 8
                if size == 1:
                    ext_raw = f.read(8)
                    if len(ext_raw) < 8:
                        return None
                    size = struct.unpack(">Q", ext_raw)[0]
                    hdr_len = 16
                elif size == 0:
                    size = file_size - pos
                if size < hdr_len:
                    return None
                if btype == b"moov":
                    end_moov = min(file_size, pos + size)
                    sub = pos + hdr_len
                    while sub + 8 <= end_moov:
                        f.seek(sub)
                        sh = f.read(8)
                        if len(sh) < 8:
                            break
                        ssize, stype = struct.unpack(">I4s", sh)
                        shdr = 8
                        if ssize == 1:
                            er = f.read(8)
                            if len(er) < 8:
                                break
                            ssize = struct.unpack(">Q", er)[0]
                            shdr = 16
                        elif ssize == 0:
                            ssize = end_moov - sub
                        if ssize < shdr:
                            break
                        if stype == b"mvhd":
                            data = f.read(min(32, ssize - shdr))
                            if len(data) >= 12:
                                ver = data[0]
                                if ver == 0 and len(data) >= 8:
                                    ctime = struct.unpack_from(">I", data, 4)[0]
                                elif ver == 1 and len(data) >= 12:
                                    ctime = struct.unpack_from(">Q", data, 4)[0]
                                else:
                                    ctime = 0
                                if ctime > _MP4_EPOCH_OFFSET:
                                    return plausible_ts(ctime - _MP4_EPOCH_OFFSET)
                            return None
                        sub += ssize
                    return None
                pos += size
    except OSError as e:
        log.debug("MP4 mvhd read failed for %s: %s", local_path, e)
        return None
    return None


def date_from_filename(name: str) -> float | None:
    stem = os.path.splitext(name)[0]
    for rx in _FILENAME_DATE_PATTERNS:
        m = rx.search(stem)
        if not m:
            continue
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1980 <= y <= 2035 and 1 <= mo <= 12 and 1 <= d <= 31):
            continue
        hh = int(m.group(4)) if m.group(4) else 12
        mm = int(m.group(5)) if m.group(5) else 0
        ss = int(m.group(6)) if m.group(6) else 0
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            hh, mm, ss = 12, 0, 0
        try:
            ts = datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc).timestamp()
        except (ValueError, OverflowError, OSError):
            continue
        valid = plausible_ts(ts)
        if valid is not None:
            return valid
    return None


def extract_shot_time_with_source(
    local_path: str, media_kind: str, fallback_mtime: float | None = None
) -> tuple[float | None, str]:
    """Возвращает (timestamp, источник): exif / mvhd / filename / mtime / unknown."""
    ext = os.path.splitext(local_path)[1].lower()
    if media_kind == "photo":
        if ext in (".jpg", ".jpeg", ".jpe"):
            ts = _read_jpeg_exif_time(local_path)
            if ts is not None:
                return ts, "exif"
        elif ext in (".tif", ".tiff"):
            try:
                with open(to_long_path(local_path), "rb") as f:
                    ts = _read_tiff_exif_time(f.read(131072))
                if ts is not None:
                    return ts, "exif"
            except OSError as e:
                log.debug("TIFF EXIF read failed for %s: %s", local_path, e)
        elif ext in (".heic", ".heif", ".webp", ".png"):
            ts = _read_pillow_exif_time(local_path)
            if ts is not None:
                return ts, "exif"
    elif media_kind == "video" and ext in (".mp4", ".mov", ".m4v", ".3gp", ".3g2"):
        ts = _read_mp4_mvhd_time(local_path)
        if ts is not None:
            return ts, "mvhd"

    by_name = date_from_filename(os.path.basename(local_path))
    if by_name is not None:
        return by_name, "filename"

    mt = plausible_ts(fallback_mtime)
    if mt is not None:
        return mt, "mtime"
    return None, "unknown"


def scan_directory(
    source_dir: str,
    state: SorterState,
    *,
    stop_event: threading.Event | None = None,
    progress_cb: Callable[[ScanSummary, str], None] | None = None,
) -> ScanSummary:
    """Рекурсивно индексирует фото и видео (строго read-only) в SQLite без симлинк-петель."""
    clean_root = validate_dir_safe(source_dir, label="папка с фото и видео")
    if not os.path.isdir(to_long_path(clean_root)):
        raise ValueError(f"Папка не найдена: {clean_root}")

    summary = ScanSummary(source_dir=clean_root)
    visited_dirs: set[str] = set()
    stack: list[str] = [clean_root]

    while stack:
        if stop_event is not None and stop_event.is_set():
            raise CancelledError("Сканирование остановлено пользователем")
        cur_dir = stack.pop()
        try:
            real_key = path_key(os.path.realpath(to_long_path(cur_dir)))
        except OSError as e:
            log.warning("Не удалось открыть каталог %s: %s", cur_dir, e)
            summary.io_errors += 1
            continue
        if real_key in visited_dirs:
            continue
        if not is_path_inside(cur_dir, clean_root):
            continue
        visited_dirs.add(real_key)

        try:
            with os.scandir(to_long_path(cur_dir)) as entries:
                items = list(entries)
        except OSError as e:
            log.warning("Не удалось прочитать каталог %s: %s", cur_dir, e)
            summary.io_errors += 1
            continue

        items.sort(key=lambda e: e.name.casefold())
        subdirs: list[str] = []
        for entry in items:
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Сканирование остановлено пользователем")
            full_path = os.path.join(cur_dir, entry.name)
            try:
                if entry.is_symlink():
                    continue
                st = entry.stat(follow_symlinks=False)
                if is_symlink_or_junction(full_path, st):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not should_skip_dir(entry.name):
                        subdirs.append(full_path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError as e:
                log.warning("Ошибка доступа к %s: %s", full_path, e)
                summary.io_errors += 1
                continue

            rel_path = os.path.relpath(full_path, clean_root)
            verdict = classify_media_file(full_path, st.st_size, verify_magic=True)
            if not verdict.accepted or verdict.media_kind is None:
                if verdict.io_error:
                    log.warning(
                        "Ошибка чтения медиафайла %s: %s",
                        full_path,
                        verdict.reason or "ошибка ввода-вывода",
                    )
                    summary.io_errors += 1
                else:
                    summary.unsupported_skipped += 1
                continue

            summary.discovered += 1
            if verdict.media_kind == "photo":
                summary.photos += 1
            else:
                summary.videos += 1

            # Быстрая проверка, не изменился ли файл с прошлого сканирования
            existing = state.get_media_by_path_key(path_key(full_path))
            if (
                existing is not None
                and int(existing["size"]) == int(st.st_size)
                and int(existing["mtime_ns"]) == int(st.st_mtime_ns)
                and existing["scan_status"] == "scanned"
            ):
                summary.unchanged_skipped += 1
                continue

            try:
                p_hash = compute_partial_hash(full_path, size=st.st_size)
            except OSError as e:
                log.warning("Не удалось посчитать отпечаток %s: %s", full_path, e)
                summary.io_errors += 1
                continue

            shot_ts, date_src = extract_shot_time_with_source(
                full_path, verdict.media_kind, fallback_mtime=st.st_mtime
            )
            _, changed = state.upsert_media_file(
                local_path=full_path,
                rel_path=rel_path,
                source_root=clean_root,
                media_kind=verdict.media_kind,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                partial_hash=p_hash,
                shot_ts=shot_ts,
                date_source=date_src,
                scan_status="pending",
            )
            if changed:
                summary.new_or_changed += 1
            else:
                summary.unchanged_skipped += 1

            if progress_cb is not None and (summary.discovered % 20 == 0 or summary.discovered == 1):
                progress_cb(summary, rel_path)

        # Добавляем подкаталоги в обратном порядке, чтобы обходить алфавитно
        for sd in reversed(subdirs):
            stack.append(sd)

    if progress_cb is not None:
        progress_cb(summary, "")
    return summary
