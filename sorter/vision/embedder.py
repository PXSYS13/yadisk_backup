"""ONNX-эмбеддер ArcFace + genderage и детерминированные тестовые двойники (Fake*)."""

from __future__ import annotations

from collections.abc import Sequence
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
from PIL import Image
from scipy import ndimage

from sorter.state import EMBEDDING_DIM
from sorter.utils import to_long_path
from sorter.vision.detector import DetectedFace, compute_face_quality
from sorter.vision.models import select_ort_providers

log = logging.getLogger("photo_sorter.vision.embedder")


@runtime_checkable
class FaceEmbedderProtocol(Protocol):
    """Структурный интерфейс извлечения 512-мерного эмбеддинга и оценки возраста/пола."""

    def embed(self, aligned_112: Image.Image) -> np.ndarray: ...

    def estimate_age_gender(
        self, image: Image.Image, bbox: Sequence[float]
    ) -> tuple[float | None, int | None]: ...


class ArcFaceEmbedder:
    """ONNX Runtime эмбеддер ArcFace (glintr100 / w600k_r50) и опционально genderage.onnx."""

    def __init__(
        self,
        embedder_path: Path | str,
        *,
        genderage_path: Path | str | None = None,
        providers: list[str] | None = None,
    ) -> None:
        import onnxruntime as ort

        self.embedder_path = Path(embedder_path)
        self.genderage_path = Path(genderage_path) if genderage_path is not None else None
        ort_providers = providers if providers is not None else select_ort_providers()

        self._emb_session = ort.InferenceSession(
            to_long_path(str(self.embedder_path)),
            providers=ort_providers,
        )
        self._emb_input_name: str = self._emb_session.get_inputs()[0].name

        self._ga_session: ort.InferenceSession | None = None
        self._ga_input_name: str | None = None
        if self.genderage_path is not None:
            if self.genderage_path.is_file():
                self._ga_session = ort.InferenceSession(
                    to_long_path(str(self.genderage_path)),
                    providers=ort_providers,
                )
                self._ga_input_name = self._ga_session.get_inputs()[0].name
            else:
                log.warning(
                    "Файл модели возраста/пола genderage не найден: %s — оценка возраста отключена",
                    self.genderage_path,
                )

    def embed(self, aligned_112: Image.Image) -> np.ndarray:
        """Вычисляет L2-нормализованный 512-мерный float32 вектор из выровненного 112x112 RGB."""
        rgb = aligned_112 if aligned_112.mode == "RGB" else aligned_112.convert("RGB")
        if rgb.size != (112, 112):
            rgb = rgb.resize((112, 112), Image.Resampling.BICUBIC)

        arr = np.asarray(rgb, dtype=np.float32)
        blob = ((arr - 127.5) / 127.5).transpose(2, 0, 1)[None, ...]
        blob = np.ascontiguousarray(blob, dtype=np.float32)

        raw = self._emb_session.run(None, {self._emb_input_name: blob})[0]
        vec = np.asarray(raw, dtype=np.float32).reshape(-1)
        if vec.size == 0 or not bool(np.all(np.isfinite(vec))):
            raise ValueError("Модель ArcFace вернула некорректный вектор эмбеддинга (NaN/Inf/пустой)")
        norm = float(np.linalg.norm(vec))
        if not np.isfinite(norm) or norm <= 1e-12:
            raise ValueError("Модель ArcFace вернула нулевой вектор эмбеддинга (L2-норма <= 1e-12)")
        vec = vec / norm
        return vec.astype(np.float32, copy=False)

    def estimate_age_gender(
        self, image: Image.Image, bbox: Sequence[float]
    ) -> tuple[float | None, int | None]:
        """Оценивает возраст [0..100] и пол (0=жен, 1=муж) через InsightFace genderage.onnx."""
        if self._ga_session is None or self._ga_input_name is None:
            return None, None

        x1, y1, x2, y2 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        cx = 0.5 * (x1 + x2)
        cy = 0.5 * (y1 + y2)
        side = max(2.0, max(x2 - x1, y2 - y1) * 1.5)
        half = 0.5 * side

        rgb = image if image.mode == "RGB" else image.convert("RGB")
        crop = rgb.crop(
            (
                int(round(cx - half)),
                int(round(cy - half)),
                int(round(cx + half)),
                int(round(cy + half)),
            )
        )
        resized = crop.resize((96, 96), Image.Resampling.BILINEAR)

        # Соглашение InsightFace genderage.onnx: BGR float32 NCHW в диапазоне [0..255]
        bgr = np.asarray(resized, dtype=np.float32)[..., ::-1]
        blob = np.ascontiguousarray(bgr.transpose(2, 0, 1)[None, ...], dtype=np.float32)

        raw = self._ga_session.run(None, {self._ga_input_name: blob})[0]
        pred = np.asarray(raw, dtype=np.float32).reshape(-1)
        if pred.size < 3:
            log.warning(
                "Модель genderage вернула неожиданный выход (размер %d < 3) — оценка возраста пропущена",
                int(pred.size),
            )
            return None, None
        if not bool(np.all(np.isfinite(pred[:3]))):
            log.warning("Модель genderage вернула NaN/Inf — оценка возраста пропущена")
            return None, None

        gender = int(np.argmax(pred[:2]))
        age = float(np.clip(round(float(pred[2]) * 100.0, 1), 0.0, 100.0))
        return age, gender


def _make_fake_basis(dim: int = EMBEDDING_DIM, n_features: int = 15) -> np.ndarray:
    """Строит фиксированный детерминированный ортонормированный базис (512, 15)."""
    rng = np.random.default_rng(202505)
    raw = rng.standard_normal((dim, n_features), dtype=np.float64)
    q, _ = np.linalg.qr(raw)
    return q[:, :n_features].astype(np.float32)


_FAKE_EMBED_BASIS: np.ndarray = _make_fake_basis()


class FakeFaceDetector:
    """Детерминированный тестовый детектор: отдаёт заданные лица или ищет цветные квадраты."""

    def __init__(
        self,
        *,
        fixed_faces: Sequence[DetectedFace] | None = None,
        min_box_side: int = 20,
        color_threshold: float = 25.0,
        default_det_score: float = 0.95,
        min_quality_floor: float = 0.78,
    ) -> None:
        self._fixed_faces = list(fixed_faces) if fixed_faces is not None else None
        self.min_box_side = int(min_box_side)
        self.color_threshold = float(color_threshold)
        self.default_det_score = float(default_det_score)
        self.min_quality_floor = float(min_quality_floor)

    def detect(self, image: Image.Image) -> list[DetectedFace]:
        if self._fixed_faces is not None:
            return [
                DetectedFace(
                    bbox=f.bbox,
                    kps=[list(pt) for pt in f.kps],
                    det_score=f.det_score,
                    quality_score=f.quality_score,
                )
                for f in self._fixed_faces
            ]

        rgb = image if image.mode == "RGB" else image.convert("RGB")
        arr = np.asarray(rgb, dtype=np.float32)
        if arr.ndim != 3 or arr.shape[0] < self.min_box_side or arr.shape[1] < self.min_box_side:
            return []

        # Оцениваем цвет фона по углам изображения
        corners = np.stack([arr[0, 0], arr[0, -1], arr[-1, 0], arr[-1, -1]], axis=0)
        bg = np.median(corners, axis=0)
        diff = np.max(np.abs(arr - bg[None, None, :]), axis=-1)
        mask = diff >= self.color_threshold

        labeled, count = ndimage.label(mask)
        if count == 0:
            return []

        rgb_u8 = np.asarray(rgb, dtype=np.uint8)
        slices = ndimage.find_objects(labeled)
        detected: list[DetectedFace] = []
        for sl in slices:
            if sl is None:
                continue
            ys, xs = sl
            x1, x2 = float(xs.start), float(xs.stop)
            y1, y2 = float(ys.start), float(ys.stop)
            w, h = x2 - x1, y2 - y1
            if w < self.min_box_side or h < self.min_box_side:
                continue
            bbox = (x1, y1, x2, y2)
            kps = [
                [x1 + 0.30 * w, y1 + 0.35 * h],
                [x1 + 0.70 * w, y1 + 0.35 * h],
                [x1 + 0.50 * w, y1 + 0.55 * h],
                [x1 + 0.35 * w, y1 + 0.78 * h],
                [x1 + 0.65 * w, y1 + 0.78 * h],
            ]
            raw_q = compute_face_quality(rgb_u8, bbox, kps, self.default_det_score)
            quality = max(self.min_quality_floor, raw_q)
            detected.append(
                DetectedFace(
                    bbox=bbox,
                    kps=kps,
                    det_score=self.default_det_score,
                    quality_score=float(min(1.0, quality)),
                )
            )

        detected.sort(key=lambda f: (f.bbox[0], f.bbox[1]))
        return detected


class FakeFaceEmbedder:
    """Детерминированный тестовый эмбеддер: устойчивые 512-d L2 векторы по цвету кропа."""

    def __init__(
        self,
        *,
        default_age: float | None = 28.0,
        default_gender: int | None = 1,
    ) -> None:
        self.default_age = default_age
        self.default_gender = default_gender

    def embed(self, aligned_112: Image.Image) -> np.ndarray:
        rgb = aligned_112 if aligned_112.mode == "RGB" else aligned_112.convert("RGB")
        if rgb.size != (112, 112):
            rgb = rgb.resize((112, 112), Image.Resampling.BILINEAR)

        # Берём центральную область кропа, чтобы не зависеть от чёрных краёв после выравнивания
        arr = np.asarray(rgb, dtype=np.float32)[20:92, 20:92, :] / 255.0
        mean_c = arr.mean(axis=(0, 1))
        centered = mean_c - 0.5
        feats = np.concatenate(
            [
                centered * 2.0,
                np.cos(np.pi * mean_c),
                np.sin(np.pi * mean_c),
                np.cos(2.0 * np.pi * mean_c),
                np.sin(2.0 * np.pi * mean_c),
            ]
        ).astype(np.float32)

        vec = _FAKE_EMBED_BASIS @ feats
        norm = float(np.linalg.norm(vec))
        if norm > 1e-12:
            vec = vec / norm
        return vec.astype(np.float32, copy=False)

    def estimate_age_gender(
        self, image: Image.Image, bbox: Sequence[float]
    ) -> tuple[float | None, int | None]:
        _ = (image, bbox)
        return self.default_age, self.default_gender
