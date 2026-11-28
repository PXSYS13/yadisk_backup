"""Фильтрация фото и видео для безлимита (/photounlim), проверка сигнатур и чтение дат съёмки.

По правилам Яндекс 360:
- в безлимит попадают фото (JPEG/PNG/HEIC/WebP/GIF/BMP/TIFF) и видео (MP4/MOV/MKV/AVI/3GP и др.);
- RAW-форматы (.dng, .cr2, .nef, .arw, .raw и т.д.) НЕ попадают в безлимит и расходуют обычное место,
  поэтому они явно исключаются с понятной причиной пропуска.
"""

from __future__ import annotations

import calendar
import logging
import math
import mimetypes
import os
import re
import struct
import time
from dataclasses import dataclass

from uploader.utils import to_long_path

log = logging.getLogger("unlim_uploader")

UNLIM_PHOTO_EXTS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".jpe",
        ".png",
        ".heic",
        ".heif",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
    }
)

UNLIM_VIDEO_EXTS = frozenset(
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

RAW_EXCLUDED_EXTS = frozenset(
    {
        ".dng",
        ".raw",
        ".arw",
        ".cr2",
        ".cr3",
        ".nef",
        ".nrw",
        ".orf",
        ".rw2",
        ".pef",
        ".srw",
        ".raf",
        ".x3f",
        ".3fr",
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
        # Служебные папки бэкапа (utils.PREVIEW_DIRNAME, LINKS_DIRNAME, SET_ASIDE_DIRNAME):
        # превью, чужие файлы по ссылкам и отложенные повреждённые копии в Безлимит не грузим.
        "_превью",
        "_ссылки",
        "_заменённые",
        "_unlim_tmp_zips",
    }
)

SKIP_SUFFIXES = (".part", ".tmp", ".crdownload", ".download", ".db", ".db-wal", ".db-shm")

MIN_PLAUSIBLE_TS = 315_532_800.0  # 1980-01-01 UTC
MAX_AHEAD_SEC = 86_400.0
_MP4_EPOCH_OFFSET = 2_082_844_800

_MIME_OVERRIDES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".png": "image/png",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".3gp": "video/3gpp",
    ".3g2": "video/3gpp2",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
}

_FILENAME_DATE_PATTERNS = (
    re.compile(
        r"(?<!\d)(19\d{2}|20\d{2})[-_.](\d{2})[-_.](\d{2})(?:[-_ T](\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2}))?(?!\d)"
    ),
    re.compile(r"(?<!\d)(19\d{2}|20\d{2})(\d{2})(\d{2})(?:[-_ T](\d{2})(\d{2})(\d{2}))?(?!\d)"),
)


@dataclass(frozen=True)
class MediaVerdict:
    accepted: bool
    media_kind: str | None  # 'photo' | 'video' | None
    reason: str | None  # причина пропуска или ошибки
    mime: str | None = None
    io_error: bool = False  # True — файл подходит по формату, но не читается (права/отключён диск)


def plausible_ts(ts: float | int | None, now: float | None = None) -> float | None:
    """Возвращает timestamp, если дата между 1980 и (сейчас + 1 сутки), иначе None."""
    if ts is None:
        return None
    try:
        val = float(ts)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(val) or val < MIN_PLAUSIBLE_TS:
        return None
    ceiling = (time.time() if now is None else now) + MAX_AHEAD_SEC
    if val > ceiling:
        return None
    return val


def should_skip_dir(name: str) -> bool:
    return name.casefold() in SKIP_DIR_NAMES


def guess_mime(path: str, media_kind: str | None = None) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    if media_kind == "video":
        return "video/mp4"
    return "image/jpeg"


def _has_valid_signature(ext: str, header: bytes) -> bool:
    """Проверяет заголовок (magic bytes), отсекая переименованный текст и повреждённые заглушки."""
    if len(header) < 4:
        return False
    if ext in (".jpg", ".jpeg", ".jpe"):
        return header[:3] == b"\xff\xd8\xff"
    if ext == ".png":
        return header[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == ".gif":
        return header[:6] in (b"GIF87a", b"GIF89a")
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


def classify_local_file(local_path: str, size: int, *, verify_magic: bool = True) -> MediaVerdict:
    """Решает, подходит ли файл для загрузки в /photounlim."""
    name = os.path.basename(local_path)
    low_name = name.lower()
    if low_name.endswith(SKIP_SUFFIXES):
        return MediaVerdict(False, None, "временный или служебный файл")
    ext = os.path.splitext(low_name)[1]
    if ext in RAW_EXCLUDED_EXTS:
        return MediaVerdict(False, None, f"RAW-формат ({ext}) не входит в безлимит Яндекса")
    if ext in UNLIM_PHOTO_EXTS:
        kind = "photo"
    elif ext in UNLIM_VIDEO_EXTS:
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
    return MediaVerdict(True, kind, None, guess_mime(local_path, kind))


def _parse_exif_dt(raw: bytes) -> float | None:
    text = raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
    m = re.match(r"^(\d{4}):(\d{2}):(\d{2})\s+(\d{2}):(\d{2}):(\d{2})", text)
    if not m:
        return None
    try:
        vals = [int(g) for g in m.groups()]
        ts = float(calendar.timegm((vals[0], vals[1], vals[2], vals[3], vals[4], vals[5])))
    except (ValueError, OverflowError):
        return None
    return plausible_ts(ts)


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


def _date_from_filename(name: str) -> float | None:
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
            ts = float(calendar.timegm((y, mo, d, hh, mm, ss)))
        except (ValueError, OverflowError):
            continue
        valid = plausible_ts(ts)
        if valid is not None:
            return valid
    return None


def extract_shot_time(
    local_path: str, media_kind: str, fallback_mtime: float | None = None
) -> float | None:
    """Дата съёмки файла: EXIF (фото) / mvhd (MP4/MOV) -> имя файла -> mtime."""
    ext = os.path.splitext(local_path)[1].lower()
    if media_kind == "photo":
        if ext in (".jpg", ".jpeg", ".jpe"):
            ts = _read_jpeg_exif_time(local_path)
            if ts is not None:
                return ts
        elif ext in (".tif", ".tiff"):
            try:
                with open(to_long_path(local_path), "rb") as f:
                    ts = _read_tiff_exif_time(f.read(131072))
                if ts is not None:
                    return ts
            except OSError as e:
                log.debug("TIFF EXIF read failed for %s: %s", local_path, e)
    elif media_kind == "video" and ext in (".mp4", ".mov", ".m4v", ".3gp", ".3g2"):
        ts = _read_mp4_mvhd_time(local_path)
        if ts is not None:
            return ts

    by_name = _date_from_filename(os.path.basename(local_path))
    if by_name is not None:
        return by_name
    return plausible_ts(fallback_mtime)
