"""Выравнивание лиц 112x112 по 5 точкам (Umeyama на чистом NumPy + Pillow) и сохранение кропов/превью."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image

from sorter.utils import to_long_path

# Стандартный 5-точечный шаблон ArcFace для выровненного лица 112x112
# Порядок точек: левый глаз, правый глаз, кончик носа, левый уголок рта, правый уголок рта.
ARC_FACE_DST_112: np.ndarray = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float64,
)


def estimate_similarity_transform(
    src_pts: np.ndarray | Sequence[Sequence[float]],
    dst_pts: np.ndarray | Sequence[Sequence[float]],
) -> np.ndarray:
    """Оценивает 2D-преобразование подобия (2x3 float64) методом Umeyama без отражений.

    Возвращает матрицу M формы (2, 3), такую что dst ≈ M @ [x, y, 1]^T.
    """
    src = np.asarray(src_pts, dtype=np.float64)
    dst = np.asarray(dst_pts, dtype=np.float64)
    if src.ndim != 2 or dst.ndim != 2 or src.shape != dst.shape or src.shape[1] != 2:
        raise ValueError(
            f"Ожидались массивы точек формы (N, 2), получено src={src.shape}, dst={dst.shape}"
        )
    num = src.shape[0]
    if num < 2:
        raise ValueError("Для оценки преобразования подобия нужно минимум 2 точки")

    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_demean = src - src_mean
    dst_demean = dst - dst_mean

    cov = (dst_demean.T @ src_demean) / float(num)
    d = np.ones((2,), dtype=np.float64)
    if float(np.linalg.det(cov)) < 0.0:
        d[1] = -1.0

    u, s, vt = np.linalg.svd(cov)
    rank = int(np.linalg.matrix_rank(cov))
    if rank == 0:
        raise ValueError("Вырожденные ключевые точки: нулевой ранг ковариационной матрицы")

    if rank == 1:
        if float(np.linalg.det(u) * np.linalg.det(vt)) > 0.0:
            rot = u @ vt
        else:
            prev_d1 = d[1]
            d[1] = -1.0
            rot = u @ np.diag(d) @ vt
            d[1] = prev_d1
    else:
        rot = u @ np.diag(d) @ vt

    src_var = float(np.sum(src_demean**2) / float(num))
    if src_var <= 1e-12:
        raise ValueError("Вырожденные исходные ключевые точки (нулевая дисперсия)")

    scale = float(np.dot(s, d) / src_var)
    if not np.isfinite(scale) or abs(scale) <= 1e-12:
        raise ValueError("Некорректный масштаб преобразования подобия")

    trans = dst_mean - scale * (rot @ src_mean)
    mat = np.zeros((2, 3), dtype=np.float64)
    mat[:, :2] = scale * rot
    mat[:, 2] = trans
    return mat


def align_face_112(
    image: Image.Image,
    kps: np.ndarray | Sequence[Sequence[float]],
) -> Image.Image:
    """Выравнивает лицо в 112x112 RGB по 5 ключевым точкам через Pillow AFFINE + BICUBIC."""
    src_pts = np.asarray(kps, dtype=np.float64)
    if src_pts.shape != (5, 2):
        raise ValueError(f"Ожидались 5 ключевых точек формы (5, 2), получено {src_pts.shape}")

    fwd = estimate_similarity_transform(src_pts, ARC_FACE_DST_112)
    fwd_3x3 = np.eye(3, dtype=np.float64)
    fwd_3x3[:2, :] = fwd
    inv = np.linalg.inv(fwd_3x3)[:2, :]

    # Pillow Image.Transform.AFFINE применяет матрицу к координатам центров пикселей (x + 0.5, y + 0.5)
    # и ожидает на выходе координаты в той же системе (u + 0.5, v + 0.5).
    a, b, c = float(inv[0, 0]), float(inv[0, 1]), float(inv[0, 2])
    d, e, f = float(inv[1, 0]), float(inv[1, 1]), float(inv[1, 2])
    c_pil = c + 0.5 - 0.5 * a - 0.5 * b
    f_pil = f + 0.5 - 0.5 * d - 0.5 * e
    coeffs = (a, b, c_pil, d, e, f_pil)

    rgb = image if image.mode == "RGB" else image.convert("RGB")
    return rgb.transform(
        (112, 112),
        Image.Transform.AFFINE,
        coeffs,
        resample=Image.Resampling.BICUBIC,
    )


def save_face_crop(
    image: Image.Image,
    bbox: Sequence[float],
    out_path: Path,
    size: int = 192,
    margin: float = 0.35,
) -> None:
    """Сохраняет квадратный RGB-кроп лица с отступом margin в JPEG (качество 88)."""
    x1, y1, x2, y2 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    cx = 0.5 * (x1 + x2)
    cy = 0.5 * (y1 + y2)
    side = max(1.0, max(x2 - x1, y2 - y1) * (1.0 + 2.0 * max(0.0, float(margin))))
    half = 0.5 * side

    left = int(round(cx - half))
    top = int(round(cy - half))
    right = int(round(cx + half))
    bottom = int(round(cy + half))
    if right <= left:
        right = left + 1
    if bottom <= top:
        bottom = top + 1

    rgb = image if image.mode == "RGB" else image.convert("RGB")
    cropped = rgb.crop((left, top, right, bottom))
    resized = cropped.resize((int(size), int(size)), Image.Resampling.LANCZOS)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    resized.save(to_long_path(str(out_path)), format="JPEG", quality=88, optimize=True)


def save_media_thumb(
    image: Image.Image,
    out_path: Path,
    max_side: int = 480,
) -> None:
    """Сохраняет уменьшенную RGB-миниатюру фото/кадра в JPEG (качество 82)."""
    thumb = image.convert("RGB") if image.mode != "RGB" else image.copy()
    thumb.thumbnail((int(max_side), int(max_side)), Image.Resampling.LANCZOS)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    thumb.save(to_long_path(str(out_path)), format="JPEG", quality=82, optimize=True)
