"""Детектор лиц SCRFD (ONNX Runtime) и оценка качества лица на чистом NumPy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
from PIL import Image

from sorter.utils import to_long_path
from sorter.vision.models import select_ort_providers


@dataclass
class DetectedFace:
    """Результат детекции одного лица в координатах исходного изображения."""

    bbox: tuple[float, float, float, float]
    kps: list[list[float]]
    det_score: float
    quality_score: float


@runtime_checkable
class FaceDetectorProtocol(Protocol):
    """Структурный интерфейс детектора лиц."""

    def detect(self, image: Image.Image) -> list[DetectedFace]: ...


def compute_face_quality(
    image_rgb: np.ndarray,
    bbox: Sequence[float],
    kps: np.ndarray | Sequence[Sequence[float]],
    det_score: float,
) -> float:
    """Оценивает качество лица в [0.0, 1.0]: det_score, размер, резкость (Лапласиан) и ракурс."""
    if image_rgb.ndim < 2 or image_rgb.shape[0] == 0 or image_rgb.shape[1] == 0:
        return 0.0

    img_h, img_w = int(image_rgb.shape[0]), int(image_rgb.shape[1])
    x1, y1, x2, y2 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    bw = max(0.0, x2 - x1)
    bh = max(0.0, y2 - y1)
    min_side = min(bw, bh)
    if min_side <= 1.0:
        return 0.0

    # 1. Уверенность детектора
    det_s = float(np.clip(float(det_score), 0.0, 1.0))

    # 2. Размер лица (< 48 px жёстко штрафуем ниже 0.35; >= 112 px -> 1.0)
    if min_side < 48.0:
        size_s = 0.34 * ((min_side / 48.0) ** 1.2)
    elif min_side < 112.0:
        size_s = 0.34 + 0.66 * (((min_side - 48.0) / (112.0 - 48.0)) ** 0.6)
    else:
        size_s = 1.0

    # 3. Резкость по дисперсии конечного 3x3 Лапласиана на оттенках серого
    ix1 = max(0, min(img_w - 1, int(math.floor(x1))))
    iy1 = max(0, min(img_h - 1, int(math.floor(y1))))
    ix2 = max(ix1 + 1, min(img_w, int(math.ceil(x2))))
    iy2 = max(iy1 + 1, min(img_h, int(math.ceil(y2))))
    crop = image_rgb[iy1:iy2, ix1:ix2]

    if crop.ndim == 3 and crop.shape[2] >= 3:
        gray = (
            0.299 * crop[..., 0].astype(np.float32)
            + 0.587 * crop[..., 1].astype(np.float32)
            + 0.114 * crop[..., 2].astype(np.float32)
        )
    else:
        gray = crop.astype(np.float32)

    if gray.shape[0] >= 3 and gray.shape[1] >= 3:
        lap = (
            -4.0 * gray[1:-1, 1:-1]
            + gray[:-2, 1:-1]
            + gray[2:, 1:-1]
            + gray[1:-1, :-2]
            + gray[1:-1, 2:]
        )
        lap_var = float(np.var(lap))
        sharp_s = float(lap_var / (lap_var + 100.0)) if lap_var > 0.0 else 0.0
    else:
        sharp_s = 0.0

    # 4. Фронтальность по симметрии 5 ключевых точек (поворот по рысканью и крен линии глаз)
    kps_arr = np.asarray(kps, dtype=np.float64)
    if kps_arr.shape == (5, 2):
        le = kps_arr[0]
        re = kps_arr[1]
        nose = kps_arr[2]
        eye_dx = float(re[0] - le[0])
        eye_dy = float(re[1] - le[1])
        eye_dist = math.hypot(eye_dx, eye_dy)
        if eye_dist < 1e-3:
            pose_s = 0.0
        else:
            ux = eye_dx / eye_dist
            uy = eye_dy / eye_dist
            # Проекция носа на ось между глазами
            d_left = float((nose[0] - le[0]) * ux + (nose[1] - le[1]) * uy)
            d_right = float((re[0] - nose[0]) * ux + (re[1] - nose[1]) * uy)
            if d_left <= 0.0 or d_right <= 0.0:
                yaw_s = 0.05
            else:
                yaw_s = float(min(d_left, d_right) / max(d_left, d_right, 1e-6))

            # Крен головы (угол линии глаз к горизонту)
            angle_deg = abs(math.degrees(math.atan2(eye_dy, eye_dx)))
            roll_s = max(0.0, 1.0 - (angle_deg / 45.0))

            # Отношение межзрачкового расстояния к ширине bbox (сужается в профиль)
            eye_span_s = min(1.0, (eye_dist / max(bw, 1.0)) / 0.28)
            pose_s = yaw_s * (0.65 + 0.35 * roll_s) * (0.50 + 0.50 * eye_span_s)
    else:
        pose_s = 0.70

    content = 0.20 * det_s + 0.40 * sharp_s + 0.40 * pose_s
    score = content * size_s * (0.50 + 0.50 * pose_s)
    return float(np.clip(score, 0.0, 1.0))


def distance2bbox(
    points: np.ndarray,
    distance: np.ndarray,
    max_shape: tuple[int, int] | None = None,
) -> np.ndarray:
    """Декодирует (left, top, right, bottom) расстояния от центров якорей в [x1, y1, x2, y2]."""
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    if max_shape is not None:
        max_h, max_w = max_shape
        x1 = np.clip(x1, 0.0, float(max_w))
        y1 = np.clip(y1, 0.0, float(max_h))
        x2 = np.clip(x2, 0.0, float(max_w))
        y2 = np.clip(y2, 0.0, float(max_h))
    return np.stack([x1, y1, x2, y2], axis=-1).astype(np.float32, copy=False)


def distance2kps(
    points: np.ndarray,
    distance: np.ndarray,
    max_shape: tuple[int, int] | None = None,
) -> np.ndarray:
    """Декодирует смещения 5 ключевых точек (N, 10) от центров якорей в (N, 5, 2)."""
    preds: list[np.ndarray] = []
    for i in range(0, distance.shape[1], 2):
        px = points[:, 0] + distance[:, i]
        py = points[:, 1] + distance[:, i + 1]
        if max_shape is not None:
            max_h, max_w = max_shape
            px = np.clip(px, 0.0, float(max_w))
            py = np.clip(py, 0.0, float(max_h))
        preds.append(px)
        preds.append(py)
    stacked = np.stack(preds, axis=-1).astype(np.float32, copy=False)
    return stacked.reshape((-1, distance.shape[1] // 2, 2))


def nms(dets: np.ndarray, iou_thresh: float = 0.40) -> list[int]:
    """Жадное подавление немаксимумов (NMS) по массиву [x1, y1, x2, y2, score]."""
    if dets.size == 0:
        return []
    x1 = dets[:, 0]
    y1 = dets[:, 1]
    x2 = dets[:, 2]
    y2 = dets[:, 3]
    scores = dets[:, 4]

    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]

    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])

        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        union = areas[i] + areas[rest] - inter
        iou = np.where(union > 0.0, inter / union, 0.0)
        inds = np.where(iou <= iou_thresh)[0]
        order = rest[inds]
    return keep


def _approximate_kps_from_bbox(bbox: np.ndarray) -> np.ndarray:
    """Синтезирует 5 ключевых точек из bbox для моделей без ветки kps (6 выходов)."""
    x1, y1, x2, y2 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
    w = x2 - x1
    h = y2 - y1
    return np.array(
        [
            [x1 + 0.30 * w, y1 + 0.35 * h],
            [x1 + 0.70 * w, y1 + 0.35 * h],
            [x1 + 0.50 * w, y1 + 0.55 * h],
            [x1 + 0.35 * w, y1 + 0.78 * h],
            [x1 + 0.65 * w, y1 + 0.78 * h],
        ],
        dtype=np.float32,
    )


def _generate_anchor_centers(
    input_w: int, input_h: int, stride: int, num_anchors: int
) -> np.ndarray:
    fw = input_w // stride
    fh = input_h // stride
    grid_y, grid_x = np.mgrid[:fh, :fw]
    grid = np.stack([grid_x, grid_y], axis=-1).astype(np.float32)
    centers = (grid * float(stride)).reshape((-1, 2))
    if num_anchors > 1:
        centers = np.stack([centers] * num_anchors, axis=1).reshape((-1, 2))
    return centers


def decode_scrfd_outputs(
    outputs: Sequence[np.ndarray],
    input_size: tuple[int, int],
    *,
    det_scale: float = 1.0,
    score_thresh: float = 0.50,
    iou_thresh: float = 0.40,
    max_faces: int = 0,
    anchor_cache: dict[tuple[int, int, int, int], np.ndarray] | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Декодирует выходы InsightFace SCRFD (6 или 9 тензоров) в (boxes, scores, kps)."""
    n_out = len(outputs)
    strides: tuple[int, ...]
    if n_out == 6:
        fmc, strides, num_anchors, use_kps = 3, (8, 16, 32), 2, False
    elif n_out == 9:
        fmc, strides, num_anchors, use_kps = 3, (8, 16, 32), 2, True
    elif n_out == 10:
        fmc, strides, num_anchors, use_kps = 5, (8, 16, 32, 64, 128), 1, False
    elif n_out == 15:
        fmc, strides, num_anchors, use_kps = 5, (8, 16, 32, 64, 128), 1, True
    else:
        raise ValueError(f"Неподдерживаемое число выходов SCRFD: {n_out} (ожидалось 6, 9, 10 или 15)")

    in_w, in_h = int(input_size[0]), int(input_size[1])
    scores_list: list[np.ndarray] = []
    bboxes_list: list[np.ndarray] = []
    kps_list: list[np.ndarray] = []

    for idx, stride in enumerate(strides):
        scores = np.asarray(outputs[idx], dtype=np.float32).reshape(-1)
        bbox_preds = np.asarray(outputs[idx + fmc], dtype=np.float32).reshape((-1, 4)) * float(
            stride
        )

        pos_inds = np.where(scores >= score_thresh)[0]
        if pos_inds.size == 0:
            continue

        cache_key = (in_w, in_h, stride, num_anchors)
        if anchor_cache is not None and cache_key in anchor_cache:
            centers = anchor_cache[cache_key]
        else:
            centers = _generate_anchor_centers(in_w, in_h, stride, num_anchors)
            if anchor_cache is not None:
                anchor_cache[cache_key] = centers

        sel_centers = centers[pos_inds]
        sel_scores = scores[pos_inds]
        sel_bboxes = distance2bbox(sel_centers, bbox_preds[pos_inds])

        scores_list.append(sel_scores)
        bboxes_list.append(sel_bboxes)

        if use_kps:
            kps_preds = np.asarray(
                outputs[idx + fmc * 2], dtype=np.float32
            ).reshape((-1, 10)) * float(stride)
            kps_list.append(distance2kps(sel_centers, kps_preds[pos_inds]))

    if not scores_list:
        return (
            np.zeros((0, 4), dtype=np.float32),
            np.zeros((0,), dtype=np.float32),
            np.zeros((0, 5, 2), dtype=np.float32),
        )

    all_scores = np.concatenate(scores_list, axis=0)
    all_bboxes = np.concatenate(bboxes_list, axis=0)
    all_kps = (
        np.concatenate(kps_list, axis=0)
        if use_kps
        else np.stack([_approximate_kps_from_bbox(b) for b in all_bboxes], axis=0)
    )

    scale = float(det_scale) if det_scale > 0.0 else 1.0
    all_bboxes = all_bboxes / scale
    all_kps = all_kps / scale

    dets = np.hstack([all_bboxes, all_scores[:, None]]).astype(np.float32, copy=False)
    keep = nms(dets, iou_thresh=iou_thresh)
    if max_faces > 0 and len(keep) > max_faces:
        keep = keep[:max_faces]

    return all_bboxes[keep], all_scores[keep], all_kps[keep]


class ScrfdDetector:
    """ONNX Runtime детектор лиц InsightFace SCRFD (scrfd_10g_bnkps.onnx / det_10g.onnx)."""

    def __init__(
        self,
        model_path: Path | str,
        *,
        providers: list[str] | None = None,
        input_size: tuple[int, int] = (640, 640),
        score_thresh: float = 0.50,
        iou_thresh: float = 0.40,
        max_faces: int = 0,
    ) -> None:
        import onnxruntime as ort

        self.model_path = Path(model_path)
        self.input_size = (int(input_size[0]), int(input_size[1]))
        self.score_thresh = float(score_thresh)
        self.iou_thresh = float(iou_thresh)
        self.max_faces = int(max_faces)
        self._anchor_cache: dict[tuple[int, int, int, int], np.ndarray] = {}

        ort_providers = providers if providers is not None else select_ort_providers()
        self._session = ort.InferenceSession(
            to_long_path(str(self.model_path)),
            providers=ort_providers,
        )
        self._input_name: str = self._session.get_inputs()[0].name

    def _preprocess(self, rgb_img: Image.Image) -> tuple[np.ndarray, float]:
        in_w, in_h = self.input_size
        orig_w, orig_h = rgb_img.size
        im_ratio = float(orig_h) / float(max(orig_w, 1))
        model_ratio = float(in_h) / float(max(in_w, 1))

        if im_ratio > model_ratio:
            new_h = in_h
            new_w = max(1, int(round(new_h / im_ratio)))
        else:
            new_w = in_w
            new_h = max(1, int(round(new_w * im_ratio)))

        det_scale = float(new_h) / float(max(orig_h, 1))
        resized = rgb_img.resize((new_w, new_h), Image.Resampling.BILINEAR)
        canvas = Image.new("RGB", (in_w, in_h), color=(0, 0, 0))
        canvas.paste(resized, (0, 0))

        arr = np.asarray(canvas, dtype=np.float32)
        blob = ((arr - 127.5) / 128.0).transpose(2, 0, 1)[None, ...]
        return np.ascontiguousarray(blob, dtype=np.float32), det_scale

    def detect(self, image: Image.Image) -> list[DetectedFace]:
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        orig_w, orig_h = rgb.size
        if orig_w == 0 or orig_h == 0:
            return []

        blob, det_scale = self._preprocess(rgb)
        raw_outputs = self._session.run(None, {self._input_name: blob})
        outputs = [np.asarray(out) for out in raw_outputs]

        boxes, scores, kps_arr = decode_scrfd_outputs(
            outputs,
            self.input_size,
            det_scale=det_scale,
            score_thresh=self.score_thresh,
            iou_thresh=self.iou_thresh,
            max_faces=self.max_faces,
            anchor_cache=self._anchor_cache,
        )
        if boxes.shape[0] == 0:
            return []

        rgb_np = np.asarray(rgb, dtype=np.uint8)
        results: list[DetectedFace] = []
        for idx in range(boxes.shape[0]):
            x1 = float(np.clip(boxes[idx, 0], 0.0, float(orig_w)))
            y1 = float(np.clip(boxes[idx, 1], 0.0, float(orig_h)))
            x2 = float(np.clip(boxes[idx, 2], 0.0, float(orig_w)))
            y2 = float(np.clip(boxes[idx, 3], 0.0, float(orig_h)))
            if x2 - x1 < 2.0 or y2 - y1 < 2.0:
                continue
            bbox = (x1, y1, x2, y2)
            kps = [[float(pt[0]), float(pt[1])] for pt in kps_arr[idx]]
            det_score = float(scores[idx])
            quality = compute_face_quality(rgb_np, bbox, kps, det_score)
            results.append(
                DetectedFace(
                    bbox=bbox,
                    kps=kps,
                    det_score=det_score,
                    quality_score=quality,
                )
            )
        return results
