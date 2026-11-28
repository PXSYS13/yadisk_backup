"""Извлечение ключевых кадров из видео через PyAV с учётом поворота (rotate / DISPLAYMATRIX)."""

from __future__ import annotations

import logging
import math
import struct
import threading
from typing import Any

from PIL import Image

from sorter.utils import CancelledError, to_long_path

log = logging.getLogger("photo_sorter.vision.video")

try:
    import av

    VIDEO_AVAILABLE = True
except ImportError as _av_exc:
    av = None  # type: ignore[assignment]
    VIDEO_AVAILABLE = False
    log.warning("PyAV (av) недоступен (%s) — обработка видео пропущена, фото работают штатно", _av_exc)

_FALLBACK_MAX_FRAMES = 4
_FALLBACK_DECODE_LIMIT = 300


def _snap_rotation_cw(angle_deg: float) -> int:
    """Приводит угол поворота по часовой стрелке к {0, 90, 180, 270}."""
    snapped = int(round(angle_deg / 90.0) * 90) % 360
    return snapped if snapped in (0, 90, 180, 270) else 0


def parse_display_matrix_rotation_cw(raw: bytes | bytearray | memoryview) -> int:
    """Вычисляет угол поворота по часовой стрелке (0/90/180/270) из 36-байтной DISPLAYMATRIX."""
    buf = bytes(raw)
    if len(buf) < 36:
        return 0
    m = struct.unpack("<9i", buf[:36])
    scale0 = math.hypot(m[0] / 65536.0, m[3] / 65536.0)
    scale1 = math.hypot(m[1] / 65536.0, m[4] / 65536.0)
    if scale0 <= 1e-6 or scale1 <= 1e-6:
        return 0
    # Соглашение FFmpeg av_display_rotation_get возвращает угол против часовой стрелки;
    # инвертируем знак, чтобы получить поворот по часовой стрелке для приведения к upright.
    ccw_deg = -math.degrees(math.atan2(m[1] / (65536.0 * scale1), m[0] / (65536.0 * scale0)))
    return _snap_rotation_cw(-ccw_deg)


def _extract_rotation_from_side_data_map(sd_obj: Any) -> int:
    if not sd_obj:
        return 0
    if isinstance(sd_obj, dict):
        for key, val in sd_obj.items():
            key_norm = str(key).lower().replace("_", "")
            if "displaymatrix" not in key_norm:
                continue
            if isinstance(val, (bytes, bytearray, memoryview)):
                rot = parse_display_matrix_rotation_cw(val)
                if rot:
                    return rot
            elif isinstance(val, (int, float)):
                rot = _snap_rotation_cw(-float(val))
                if rot:
                    return rot
    else:
        try:
            for item in sd_obj:
                item_type = str(getattr(item, "type", item)).lower().replace("_", "")
                if "displaymatrix" not in item_type:
                    continue
                try:
                    raw = bytes(item)
                except TypeError:
                    raw = b""
                if len(raw) >= 36:
                    rot = parse_display_matrix_rotation_cw(raw)
                    if rot:
                        return rot
        except TypeError:
            pass
    return 0


def get_stream_rotation_cw(stream: Any, frame: Any = None) -> int:
    """Определяет поворот видео по часовой стрелке (0/90/180/270) из rotate/DISPLAYMATRIX/frame."""
    metadata = getattr(stream, "metadata", None)
    if isinstance(metadata, dict):
        for k, v in metadata.items():
            if str(k).lower() == "rotate":
                try:
                    rot = _snap_rotation_cw(float(str(v).strip()))
                    if rot:
                        return rot
                except ValueError:
                    pass

    # Поддержка и stream.side_data (старый PyAV), и codec_context.coded_side_data (PyAV 14+)
    rot = _extract_rotation_from_side_data_map(getattr(stream, "side_data", None))
    if rot:
        return rot

    codec_ctx = getattr(stream, "codec_context", None)
    if codec_ctx is not None:
        rot = _extract_rotation_from_side_data_map(getattr(codec_ctx, "coded_side_data", None))
        if rot:
            return rot

    if frame is not None:
        frame_rot = getattr(frame, "rotation", None)
        if isinstance(frame_rot, (int, float)) and frame_rot != 0:
            # frame.rotation в PyAV/FFmpeg следует av_display_rotation_get (CCW)
            return _snap_rotation_cw(-float(frame_rot))
        rot = _extract_rotation_from_side_data_map(getattr(frame, "side_data", None))
        if rot:
            return rot

    return 0


def apply_rotation_cw(image: Image.Image, deg_cw: int) -> Image.Image:
    """Поворачивает изображение по часовой стрелке на 0/90/180/270 градусов."""
    norm = deg_cw % 360
    if norm == 90:
        return image.transpose(Image.Transpose.ROTATE_270)
    if norm == 180:
        return image.transpose(Image.Transpose.ROTATE_180)
    if norm == 270:
        return image.transpose(Image.Transpose.ROTATE_90)
    return image


def _extract_duration_sec(container: Any, stream: Any) -> float | None:
    stream_dur = getattr(stream, "duration", None)
    time_base = getattr(stream, "time_base", None)
    if stream_dur is not None and time_base is not None:
        val = float(stream_dur * time_base)
        if math.isfinite(val) and val > 0.0:
            return val
    cont_dur = getattr(container, "duration", None)
    if cont_dur is not None:
        val = float(cont_dur) / float(av.time_base)
        if math.isfinite(val) and val > 0.0:
            return val
    return None


def _frame_time_sec(frame: Any, stream: Any, fallback_idx: int, fps: float) -> float:
    ftime = getattr(frame, "time", None)
    if isinstance(ftime, (int, float)) and math.isfinite(ftime) and ftime >= 0.0:
        return float(ftime)
    pts = getattr(frame, "pts", None)
    tb = getattr(stream, "time_base", None)
    if pts is not None and tb is not None:
        val = float(pts * tb)
        if math.isfinite(val) and val >= 0.0:
            return val
    return float(fallback_idx) / max(fps, 1.0)


def _stream_fps(stream: Any) -> float:
    for attr in ("average_rate", "guessed_rate", "base_rate"):
        rate = getattr(stream, attr, None)
        if rate is not None:
            try:
                val = float(rate)
                if math.isfinite(val) and val > 0.0:
                    return val
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    return 25.0


def _decode_pass(
    video_path: str,
    *,
    nonkey_only: bool,
    step_sec: float,
    max_frames: int,
    max_decoded_frames: int | None,
    stop_event: threading.Event | None,
) -> tuple[list[tuple[int, Image.Image]], float | None, bool]:
    frames: list[tuple[int, Image.Image]] = []
    duration_sec: float | None = None
    last_ts: float | None = None
    max_seen_ts: float = 0.0

    with av.open(to_long_path(video_path)) as container:
        if not container.streams.video:
            return [], None, False
        stream = container.streams.video[0]
        duration_sec = _extract_duration_sec(container, stream)
        fps = _stream_fps(stream)
        stream_rot = get_stream_rotation_cw(stream)

        if nonkey_only:
            try:
                stream.codec_context.skip_frame = "NONKEY"
            except Exception as exc:  # noqa: BLE001
                log.debug("skip_frame=NONKEY unavailable for %s: %s", video_path, exc)

        for idx, frame in enumerate(container.decode(stream)):
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Извлечение кадров видео остановлено пользователем")
            if max_decoded_frames is not None and idx >= max_decoded_frames:
                break

            ts = _frame_time_sec(frame, stream, idx, fps)
            if ts > max_seen_ts:
                max_seen_ts = ts

            if last_ts is not None and (ts - last_ts) < (step_sec - 1e-6):
                continue

            rot_cw = stream_rot if stream_rot else get_stream_rotation_cw(stream, frame)
            img = frame.to_image()
            if img.mode != "RGB":
                img = img.convert("RGB")
            if rot_cw:
                img = apply_rotation_cw(img, rot_cw)

            frame_ms = max(0, int(round(ts * 1000.0)))
            if frames and frame_ms <= frames[-1][0]:
                frame_ms = frames[-1][0] + 1

            frames.append((frame_ms, img))
            last_ts = ts
            if len(frames) >= max_frames:
                break

    if duration_sec is None and max_seen_ts > 0.0:
        duration_sec = max_seen_ts
    return frames, duration_sec, True


def extract_video_keyframes(
    video_path: str,
    *,
    step_sec: float = 1.5,
    max_frames: int = 60,
    stop_event: threading.Event | None = None,
) -> tuple[list[tuple[int, Image.Image]], float | None]:
    """Извлекает ключевые RGB-кадры (frame_ms, PIL.Image) с учётом поворота и длительность видео."""
    if stop_event is not None and stop_event.is_set():
        raise CancelledError("Извлечение кадров видео остановлено пользователем")
    if not VIDEO_AVAILABLE or av is None:
        return [], None

    eff_max = max(1, int(max_frames))
    eff_step = max(0.05, float(step_sec))

    frames, duration_sec, has_video_stream = _decode_pass(
        video_path,
        nonkey_only=True,
        step_sec=eff_step,
        max_frames=eff_max,
        max_decoded_frames=None,
        stop_event=stop_event,
    )
    if not has_video_stream:
        return [], None

    if len(frames) <= 1 and duration_sec is not None and duration_sec > 5.0:
        log.warning(
            "Декодирование с skip_frame=NONKEY извлекло только %d кадр(ов) для видео длительностью "
            "%.1f с (%s) — выполняется резервный проход",
            len(frames),
            duration_sec,
            video_path,
        )

    # Если ролик короткий или содержит только 1 keyframe — делаем лёгкий fallback без NONKEY,
    # чтобы набрать до 4 равномерных кадров для многокадрового подтверждения лиц.
    if len(frames) <= 1 and eff_max > 1:
        fb_target = min(_FALLBACK_MAX_FRAMES, eff_max)
        if duration_sec is not None and duration_sec > 0.0:
            fb_step = max(0.05, min(eff_step, duration_sec / float(fb_target)))
        else:
            fb_step = min(eff_step, 0.35)
        try:
            fb_frames, fb_dur, _ = _decode_pass(
                video_path,
                nonkey_only=False,
                step_sec=fb_step,
                max_frames=fb_target,
                max_decoded_frames=_FALLBACK_DECODE_LIMIT,
                stop_event=stop_event,
            )
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if frames:
                log.warning(
                    "Резервный проход декодирования видео %s завершился ошибкой (%s) — "
                    "использую %d кадр(ов) первого прохода",
                    video_path,
                    exc,
                    len(frames),
                )
                return frames, duration_sec
            raise
        if len(fb_frames) > len(frames):
            frames = fb_frames
        if duration_sec is None and fb_dur is not None:
            duration_sec = fb_dur

    return frames, duration_sec
