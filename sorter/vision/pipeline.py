"""Конвейер извлечения лиц, эмбеддингов, кропов и превью из фото и видео в SorterState."""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
from pathlib import Path
import stat
import threading
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from sorter import paths
from sorter.state import SorterState
from sorter.utils import (
    CancelledError,
    is_fatal_storage_error,
    is_path_inside,
    is_symlink_or_junction,
    to_long_path,
)
from sorter.vision.align import align_face_112, save_face_crop, save_media_thumb
from sorter.vision.detector import DetectedFace, FaceDetectorProtocol, ScrfdDetector
from sorter.vision.embedder import ArcFaceEmbedder, FaceEmbedderProtocol
from sorter.vision.models import ensure_model_pack, select_ort_providers
from sorter.vision.video import extract_video_keyframes

log = logging.getLogger("photo_sorter.vision.pipeline")

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


class _FatalInferenceError(Exception):
    """Внутренняя обёртка для немедленного проброса фатального сбоя ONNX/инференса."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


def _is_ort_exception(exc: BaseException) -> bool:
    mod = type(exc).__module__ or ""
    return mod == "onnxruntime" or mod.startswith("onnxruntime.")


def _select_video_face_candidates(
    candidates: list[tuple[int, Image.Image, DetectedFace]],
    max_faces: int,
) -> list[tuple[int, Image.Image, DetectedFace]]:
    """Отбирает до max_faces лучших лиц, равномерно сохраняя разные frame_ms для подтверждения."""
    if max_faces <= 0 or len(candidates) <= max_faces:
        return candidates

    by_frame: dict[int, list[tuple[int, Image.Image, DetectedFace]]] = {}
    for item in candidates:
        by_frame.setdefault(item[0], []).append(item)

    for frame_items in by_frame.values():
        frame_items.sort(key=lambda x: (x[2].quality_score, x[2].det_score), reverse=True)

    # Сортируем кадры по качеству лучшего лица в кадре, затем берём round-robin по раундам
    ordered_frames = sorted(
        by_frame.keys(),
        key=lambda ms: (by_frame[ms][0][2].quality_score, by_frame[ms][0][2].det_score),
        reverse=True,
    )

    selected: list[tuple[int, Image.Image, DetectedFace]] = []
    round_idx = 0
    while len(selected) < max_faces:
        added_any = False
        for ms in ordered_frames:
            bucket = by_frame[ms]
            if round_idx < len(bucket):
                selected.append(bucket[round_idx])
                added_any = True
                if len(selected) >= max_faces:
                    break
        if not added_any:
            break
        round_idx += 1

    selected.sort(key=lambda x: (x[0], -x[2].quality_score))
    return selected


class VisionEngine:
    """Обёртка над детектором и эмбеддером для обработки фото/видео и записи лиц в БД."""

    def __init__(
        self,
        detector: FaceDetectorProtocol,
        embedder: FaceEmbedderProtocol,
        *,
        crops_dir: Path | str | None = None,
        thumbs_dir: Path | str | None = None,
        video_step_sec: float = 1.5,
        video_max_frames: int = 60,
        max_faces_per_video: int = 40,
    ) -> None:
        self.detector = detector
        self.embedder = embedder
        self._crops_dir = Path(crops_dir) if crops_dir is not None else None
        self._thumbs_dir = Path(thumbs_dir) if thumbs_dir is not None else None
        self.video_step_sec = float(video_step_sec)
        self.video_max_frames = int(video_max_frames)
        self.max_faces_per_video = int(max_faces_per_video)

    @property
    def crops_dir(self) -> Path:
        return self._crops_dir if self._crops_dir is not None else paths.CROPS_DIR

    @property
    def thumbs_dir(self) -> Path:
        return self._thumbs_dir if self._thumbs_dir is not None else paths.THUMBS_DIR

    @classmethod
    def from_model_pack(
        cls,
        pack_name: str = "antelopev2",
        *,
        models_dir: Path | str | None = None,
        crops_dir: Path | str | None = None,
        thumbs_dir: Path | str | None = None,
        prefer_gpu: bool = True,
        score_thresh: float = 0.50,
        iou_thresh: float = 0.40,
        progress_cb: Callable[[int, int | None], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> VisionEngine:
        """Загружает (при необходимости скачивает) ONNX-пакет и создаёт готовый VisionEngine."""
        pack = ensure_model_pack(
            pack_name=pack_name,
            models_dir=models_dir,
            progress_cb=progress_cb,
            stop_event=stop_event,
        )
        providers = select_ort_providers(prefer_gpu=prefer_gpu)
        detector = ScrfdDetector(
            pack.detector_path,
            providers=providers,
            score_thresh=score_thresh,
            iou_thresh=iou_thresh,
        )
        embedder = ArcFaceEmbedder(
            pack.embedder_path,
            genderage_path=pack.genderage_path,
            providers=providers,
        )
        return cls(
            detector=detector,
            embedder=embedder,
            crops_dir=crops_dir,
            thumbs_dir=thumbs_dir,
        )

    def _detect_faces(self, image: Image.Image) -> list[DetectedFace]:
        try:
            return self.detector.detect(image)
        except Exception as exc:  # noqa: BLE001
            if _is_ort_exception(exc) or isinstance(exc, RuntimeError):
                raise _FatalInferenceError(exc) from exc
            raise

    def _build_face_record(
        self,
        *,
        media_id: int,
        face_idx: int,
        frame_ms: int,
        image: Image.Image,
        face: DetectedFace,
    ) -> dict[str, Any]:
        aligned = align_face_112(image, face.kps)
        try:
            embedding = self.embedder.embed(aligned)
            est_age, est_gender = self.embedder.estimate_age_gender(image, face.bbox)
        except Exception as exc:  # noqa: BLE001
            if _is_ort_exception(exc) or isinstance(exc, RuntimeError):
                raise _FatalInferenceError(exc) from exc
            raise

        crop_rel_path = f"m_{media_id}_f_{face_idx}.jpg"
        save_face_crop(image, face.bbox, self.crops_dir / crop_rel_path)

        return {
            "frame_ms": int(frame_ms),
            "bbox": [float(v) for v in face.bbox],
            "kps": [[float(pt[0]), float(pt[1])] for pt in face.kps],
            "det_score": float(face.det_score),
            "quality_score": float(face.quality_score),
            "est_age": est_age,
            "est_gender": est_gender,
            "embedding": embedding,
            "crop_rel_path": crop_rel_path,
        }

    def _process_photo(
        self,
        media_id: int,
        local_path: str,
        state: SorterState,
        stop_event: threading.Event | None,
    ) -> int:
        with Image.open(to_long_path(local_path)) as raw_img:
            rgb = ImageOps.exif_transpose(raw_img).convert("RGB")

        width, height = rgb.size
        thumb_rel_path = f"m_{media_id}.jpg"
        save_media_thumb(rgb, self.thumbs_dir / thumb_rel_path)

        detected = self._detect_faces(rgb)
        records: list[dict[str, Any]] = []
        for idx, face in enumerate(detected):
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Обработка фото остановлена пользователем")
            try:
                records.append(
                    self._build_face_record(
                        media_id=media_id,
                        face_idx=idx,
                        frame_ms=0,
                        image=rgb,
                        face=face,
                    )
                )
            except (ValueError, np.linalg.LinAlgError) as face_exc:
                log.warning(
                    "Пропущено лицо #%d в фото #%d (%s): %s",
                    idx,
                    media_id,
                    local_path,
                    face_exc,
                )

        state.replace_faces_for_media(media_id, records)
        state.mark_media_scanned(
            media_id,
            width=width,
            height=height,
            thumb_rel_path=thumb_rel_path,
        )
        return len(records)

    def _process_video(
        self,
        media_id: int,
        local_path: str,
        state: SorterState,
        stop_event: threading.Event | None,
    ) -> int:
        keyframes, duration_sec = extract_video_keyframes(
            local_path,
            step_sec=self.video_step_sec,
            max_frames=self.video_max_frames,
            stop_event=stop_event,
        )
        if not keyframes:
            raise ValueError("Не удалось извлечь ни одного кадра из видео")

        first_img = keyframes[0][1]
        width, height = first_img.size
        thumb_rel_path = f"m_{media_id}.jpg"
        save_media_thumb(first_img, self.thumbs_dir / thumb_rel_path)

        candidates: list[tuple[int, Image.Image, DetectedFace]] = []
        for frame_ms, frame_img in keyframes:
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Обработка видео остановлена пользователем")
            detected = self._detect_faces(frame_img)
            for face in detected:
                candidates.append((frame_ms, frame_img, face))

        kept = _select_video_face_candidates(candidates, self.max_faces_per_video)
        records: list[dict[str, Any]] = []
        for idx, (frame_ms, frame_img, face) in enumerate(kept):
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Обработка видео остановлена пользователем")
            try:
                records.append(
                    self._build_face_record(
                        media_id=media_id,
                        face_idx=idx,
                        frame_ms=frame_ms,
                        image=frame_img,
                        face=face,
                    )
                )
            except (ValueError, np.linalg.LinAlgError) as face_exc:
                log.warning(
                    "Пропущено лицо #%d в видео #%d (%s): %s",
                    idx,
                    media_id,
                    local_path,
                    face_exc,
                )

        state.replace_faces_for_media(media_id, records)
        state.mark_media_scanned(
            media_id,
            width=width,
            height=height,
            duration_sec=duration_sec,
            thumb_rel_path=thumb_rel_path,
        )
        return len(records)

    def process_media_row(
        self,
        media_row: dict[str, Any],
        state: SorterState,
        *,
        stop_event: threading.Event | None = None,
    ) -> int:
        """Обрабатывает один медиафайл из media_files; при ошибке файла помечает его без падения."""
        if stop_event is not None and stop_event.is_set():
            raise CancelledError("Обработка остановлена пользователем")

        media_id = int(media_row["id"])
        local_path = str(media_row["local_path"])
        media_kind = str(media_row["media_kind"])
        source_root = str(media_row.get("source_root") or "").strip()

        try:
            if source_root and not is_path_inside(local_path, source_root):
                raise ValueError(
                    f"Путь медиафайла выходит за пределы исходной папки: {local_path}"
                )
            st = os.lstat(to_long_path(local_path))
            if is_symlink_or_junction(local_path, st) or not stat.S_ISREG(st.st_mode):
                raise ValueError(
                    f"Медиафайл не является обычным файлом (симлинк/junction запрещены): {local_path}"
                )
            if media_kind == "photo":
                return self._process_photo(media_id, local_path, state, stop_event)
            if media_kind == "video":
                return self._process_video(media_id, local_path, state, stop_event)
            raise ValueError(f"Неподдерживаемый тип медиа: {media_kind}")
        except CancelledError:
            raise
        except _FatalInferenceError as fatal:
            raise fatal.original from None
        except Exception as exc:  # noqa: BLE001
            if is_fatal_storage_error(exc) or _is_ort_exception(exc):
                raise
            log.warning("Ошибка обработки медиа #%d (%s): %s", media_id, local_path, exc)
            state.mark_media_error(media_id, f"{type(exc).__name__}: {exc}")
            return 0

    def process_pending_media(
        self,
        state: SorterState,
        *,
        stop_event: threading.Event | None = None,
        progress_cb: Callable[[dict[str, int], dict[str, Any]], None] | None = None,
    ) -> dict[str, int]:
        """Обрабатывает все ожидающие (scan_status='pending') медиафайлы в базе."""
        return process_pending_media(
            state,
            self,
            stop_event=stop_event,
            progress_cb=progress_cb,
        )


def process_pending_media(
    state: SorterState,
    engine: VisionEngine,
    *,
    stop_event: threading.Event | None = None,
    progress_cb: Callable[[dict[str, int], dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Последовательно прогоняет все медиа со статусом 'pending' через VisionEngine."""
    pending = state.list_media(scan_status="pending")
    stats: dict[str, int] = {
        "total": len(pending),
        "processed": 0,
        "scanned": 0,
        "errors": 0,
        "faces": 0,
    }

    for row in pending:
        if stop_event is not None and stop_event.is_set():
            raise CancelledError("Анализ лиц остановлен пользователем")

        media_id = int(row["id"])
        faces_count = engine.process_media_row(row, state, stop_event=stop_event)
        updated = state.get_media(media_id)

        stats["processed"] += 1
        if updated is not None and updated.get("scan_status") == "scanned":
            stats["scanned"] += 1
            stats["faces"] += faces_count
        else:
            stats["errors"] += 1

        if progress_cb is not None:
            progress_cb(dict(stats), row)

    return stats
