"""Офлайн юнит- и интеграционные тесты подсистемы компьютерного зрения (Lane 1A)."""

from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import stat
import struct
import threading
from types import SimpleNamespace
from typing import Any
import zipfile

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
import pytest

from sorter.media_scan import scan_directory
from sorter.state import SorterState, decode_embedding
from sorter.utils import CancelledError
from sorter.vision.align import (
    ARC_FACE_DST_112,
    align_face_112,
    estimate_similarity_transform,
    save_face_crop,
    save_media_thumb,
)
from sorter.vision.detector import (
    DetectedFace,
    ScrfdDetector,
    compute_face_quality,
    decode_scrfd_outputs,
    distance2bbox,
    distance2kps,
    nms,
)
from sorter.vision.embedder import ArcFaceEmbedder, FakeFaceDetector, FakeFaceEmbedder
from sorter.vision.models import (
    MODEL_PACKS,
    ensure_model_pack,
    is_model_pack_ready,
    safe_extract_zip,
    select_ort_providers,
)
from sorter.vision.pipeline import VisionEngine, process_pending_media
from sorter.vision.video import (
    extract_video_keyframes,
    get_stream_rotation_cw,
    parse_display_matrix_rotation_cw,
)


# ---------------------------------------------------------------------------
# 1. Выравнивание Umeyama + align_face_112 (< 0.5 px) + кропы/превью
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("angle_deg", "scale", "tx", "ty"),
    [
        (27.0, 1.8, 180.0, 110.0),
        (-34.0, 0.85, 220.0, 190.0),
        (180.0, 1.4, 340.0, 320.0),
    ],
)
def test_umeyama_and_align_face_112_subpixel_accuracy(
    angle_deg: float, scale: float, tx: float, ty: float
) -> None:
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    # Прямое отображение dst -> src (поворот, масштаб и сдвиг на исходном полотне)
    rot_s = scale * np.array([[cos_a, -sin_a], [sin_a, cos_a]], dtype=np.float64)
    src_kps = (ARC_FACE_DST_112 @ rot_s.T) + np.array([tx, ty], dtype=np.float64)

    # Проверяем точность самой оценки матрицы Umeyama
    mat = estimate_similarity_transform(src_kps, ARC_FACE_DST_112)
    assert mat.shape == (2, 3)
    assert mat.dtype == np.float64
    assert float(np.linalg.det(mat[:, :2])) > 0.0

    ones = np.ones((5, 1), dtype=np.float64)
    projected = np.hstack([src_kps, ones]) @ mat.T
    assert float(np.max(np.linalg.norm(projected - ARC_FACE_DST_112, axis=1))) < 1e-6

    # Рисуем 5 субпиксельных гауссовых пятен в точных координатах src_kps на 512x512 RGB
    w, h = 512, 512
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    canvas = np.zeros((h, w, 3), dtype=np.float64)
    sigma = 2.0 * max(1.0, scale)
    for px, py in src_kps:
        gauss = np.exp(-((xx - px) ** 2 + (yy - py) ** 2) / (2.0 * sigma * sigma))
        canvas[..., 0] = np.maximum(canvas[..., 0], 255.0 * gauss)
        canvas[..., 1] = np.maximum(canvas[..., 1], 255.0 * gauss)
        canvas[..., 2] = np.maximum(canvas[..., 2], 255.0 * gauss)

    src_img = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), mode="RGB")
    aligned = align_face_112(src_img, src_kps)
    assert aligned.size == (112, 112)
    assert aligned.mode == "RGB"

    aligned_gray = np.asarray(aligned, dtype=np.float64).mean(axis=2)
    for idx, (dx, dy) in enumerate(ARC_FACE_DST_112):
        r = 6
        x0 = max(0, int(math.floor(dx)) - r)
        x1 = min(112, int(math.ceil(dx)) + r + 1)
        y0 = max(0, int(math.floor(dy)) - r)
        y1 = min(112, int(math.ceil(dy)) + r + 1)
        patch = aligned_gray[y0:y1, x0:x1]
        # Порог отсечения шума бикубического звона вокруг пика
        weights = np.maximum(0.0, patch - 0.35 * float(patch.max()))
        py_grid, px_grid = np.mgrid[y0:y1, x0:x1].astype(np.float64)
        total_w = float(weights.sum())
        assert total_w > 0.0, f"Не найдено пятно для точки {idx}"
        cx = float((px_grid * weights).sum() / total_w)
        cy = float((py_grid * weights).sum() / total_w)
        err = math.hypot(cx - dx, cy - dy)
        assert err < 0.5, f"Точка {idx}: ошибка выравнивания {err:.3f} px >= 0.5 px"


def test_umeyama_prevents_reflection() -> None:
    reflected = ARC_FACE_DST_112.copy()
    reflected[:, 0] = 112.0 - reflected[:, 0]
    mat = estimate_similarity_transform(reflected, ARC_FACE_DST_112)
    # Определитель линейной части обязан быть положительным (никаких зеркальных отражений)
    assert float(np.linalg.det(mat[:, :2])) > 0.0


def test_save_face_crop_and_media_thumb(tmp_path: Path) -> None:
    img = Image.new("RGBA", (800, 600), color=(40, 120, 200, 255))
    crop_path = tmp_path / "crops" / "face.jpg"
    thumb_path = tmp_path / "thumbs" / "thumb.jpg"

    save_face_crop(img, [10.0, 20.0, 110.0, 120.0], crop_path, size=192, margin=0.35)
    save_media_thumb(img, thumb_path, max_side=480)

    with Image.open(crop_path) as c:
        assert c.format == "JPEG"
        assert c.mode == "RGB"
        assert c.size == (192, 192)

    with Image.open(thumb_path) as t:
        assert t.format == "JPEG"
        assert t.mode == "RGB"
        assert max(t.size) == 480
        assert t.size == (480, 360)


# ---------------------------------------------------------------------------
# 2. Оценка качества лица (compute_face_quality)
# ---------------------------------------------------------------------------


def _draw_sharp_synthetic_face(size: int = 320, box: tuple[int, int, int, int] = (80, 80, 240, 240)) -> Image.Image:
    img = Image.new("RGB", (size, size), color=(30, 35, 40))
    draw = ImageDraw.Draw(img)
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    draw.ellipse([x1, y1, x2, y2], fill=(215, 175, 145), outline=(40, 30, 20), width=2)
    # Резкие контрастные черты лица (глаза, брови, нос, рот, текстура)
    for ex in (x1 + int(0.32 * w), x1 + int(0.68 * w)):
        ey = y1 + int(0.36 * h)
        r = max(2, int(0.06 * w))
        draw.ellipse([ex - r, ey - r, ex + r, ey + r], fill=(15, 15, 15), outline=(255, 255, 255))
        draw.line([ex - r * 2, ey - r * 2, ex + r * 2, ey - r * 2], fill=(35, 25, 20), width=2)
    nx, ny = x1 + int(0.50 * w), y1 + int(0.55 * h)
    draw.polygon([(nx, ny - int(0.09 * h)), (nx - int(0.05 * w), ny + int(0.05 * h)), (nx + int(0.05 * w), ny + int(0.05 * h))], fill=(160, 110, 90), outline=(50, 30, 20))
    my = y1 + int(0.76 * h)
    draw.line([x1 + int(0.34 * w), my, x1 + int(0.66 * w), my], fill=(120, 30, 35), width=3)
    # Тонкая шахматная текстура кожи внутри лба для высокой резкости
    for dy in range(int(0.14 * h), int(0.26 * h), 4):
        for dx in range(int(0.25 * w), int(0.75 * w), 4):
            draw.rectangle([x1 + dx, y1 + dy, x1 + dx + 1, y1 + dy + 1], fill=(150, 110, 85))
    return img


def test_compute_face_quality_sharp_blur_tiny_and_profile() -> None:
    box = (80.0, 80.0, 240.0, 240.0)
    frontal_kps = [
        [80.0 + 0.32 * 160, 80.0 + 0.36 * 160],
        [80.0 + 0.68 * 160, 80.0 + 0.36 * 160],
        [80.0 + 0.50 * 160, 80.0 + 0.55 * 160],
        [80.0 + 0.35 * 160, 80.0 + 0.76 * 160],
        [80.0 + 0.65 * 160, 80.0 + 0.76 * 160],
    ]
    sharp_img = _draw_sharp_synthetic_face(320, (80, 80, 240, 240))
    blurry_img = sharp_img.filter(ImageFilter.GaussianBlur(radius=4.5))

    q_sharp = compute_face_quality(np.asarray(sharp_img), box, frontal_kps, det_score=0.96)
    q_blur = compute_face_quality(np.asarray(blurry_img), box, frontal_kps, det_score=0.96)
    assert q_sharp > 0.7
    assert q_blur < q_sharp - 0.15

    # Крошечное лицо (< 48 px) жёстко штрафуется (< 0.35)
    tiny_box = (40.0, 40.0, 76.0, 76.0)
    tiny_kps = [
        [40.0 + 0.32 * 36, 40.0 + 0.36 * 36],
        [40.0 + 0.68 * 36, 40.0 + 0.36 * 36],
        [40.0 + 0.50 * 36, 40.0 + 0.55 * 36],
        [40.0 + 0.35 * 36, 40.0 + 0.76 * 36],
        [40.0 + 0.65 * 36, 40.0 + 0.76 * 36],
    ]
    tiny_img = _draw_sharp_synthetic_face(160, (40, 40, 76, 76))
    q_tiny = compute_face_quality(np.asarray(tiny_img), tiny_box, tiny_kps, det_score=0.98)
    assert q_tiny < 0.35

    # Сильный профиль (нос смещён за край глаза, узкий межзрачковый интервал)
    profile_kps = [
        [80.0 + 0.22 * 160, 80.0 + 0.36 * 160],
        [80.0 + 0.38 * 160, 80.0 + 0.42 * 160],
        [80.0 + 0.20 * 160, 80.0 + 0.55 * 160],
        [80.0 + 0.24 * 160, 80.0 + 0.76 * 160],
        [80.0 + 0.36 * 160, 80.0 + 0.76 * 160],
    ]
    q_profile = compute_face_quality(np.asarray(sharp_img), box, profile_kps, det_score=0.96)
    assert q_profile < 0.45
    assert q_profile < q_sharp - 0.35


# ---------------------------------------------------------------------------
# 3. SCRFD декодирование bbox/kps и NMS
# ---------------------------------------------------------------------------


def test_scrfd_helpers_distance_nms_and_decode_9_and_6_outputs() -> None:
    pts = np.array([[100.0, 200.0]], dtype=np.float32)
    b_dist = np.array([[20.0, 30.0, 40.0, 50.0]], dtype=np.float32)
    boxes = distance2bbox(pts, b_dist)
    np.testing.assert_allclose(boxes, [[80.0, 170.0, 140.0, 250.0]])

    k_dist = np.array([[-10, -5, 10, -5, 0, 5, -8, 15, 8, 15]], dtype=np.float32)
    kps = distance2kps(pts, k_dist)
    assert kps.shape == (1, 5, 2)
    np.testing.assert_allclose(kps[0, 0], [90.0, 195.0])
    np.testing.assert_allclose(kps[0, 2], [100.0, 205.0])

    # NMS подавляет сильно перекрывающийся дубль, оставляя независимый бокс
    dets = np.array(
        [
            [10.0, 10.0, 110.0, 110.0, 0.92],
            [14.0, 14.0, 112.0, 112.0, 0.80],
            [200.0, 200.0, 300.0, 300.0, 0.85],
        ],
        dtype=np.float32,
    )
    assert nms(dets, iou_thresh=0.40) == [0, 2]

    # Синтетические 9 выходов SCRFD для input_size=(64, 64), strides=(8, 16, 32), 2 якоря
    in_w, in_h = 64, 64
    strides = (8, 16, 32)
    score_tensors: list[np.ndarray] = []
    bbox_tensors: list[np.ndarray] = []
    kps_tensors: list[np.ndarray] = []
    for s in strides:
        n_cells = (in_h // s) * (in_w // s) * 2
        score_tensors.append(np.zeros((n_cells, 1), dtype=np.float32))
        bbox_tensors.append(np.zeros((n_cells, 4), dtype=np.float32))
        kps_tensors.append(np.zeros((n_cells, 10), dtype=np.float32))

    # На stride=16 (4x4 сетка, 2 якоря на ячейку): ячейка (gx=2, gy=1) -> центр (32, 16), индекс (1*4 + 2)*2 = 12
    idx_a = (1 * 4 + 2) * 2
    idx_dup = idx_a + 1  # второй якорь в той же точке, должен быть подавлен NMS
    score_tensors[1][idx_a, 0] = 0.95
    bbox_tensors[1][idx_a] = [1.0, 0.5, 1.0, 1.5]  # *16 -> [16, 8, 48, 40]
    kps_tensors[1][idx_a] = [-0.5, -0.25, 0.5, -0.25, 0.0, 0.25, -0.4, 0.75, 0.4, 0.75]

    score_tensors[1][idx_dup, 0] = 0.75
    bbox_tensors[1][idx_dup] = [0.95, 0.5, 1.05, 1.5]
    kps_tensors[1][idx_dup] = kps_tensors[1][idx_a]

    out_boxes, out_scores, out_kps = decode_scrfd_outputs(
        score_tensors + bbox_tensors + kps_tensors,
        (in_w, in_h),
        det_scale=0.5,
        score_thresh=0.50,
        iou_thresh=0.40,
    )
    assert out_boxes.shape == (1, 4)
    assert out_scores.shape == (1,)
    assert out_kps.shape == (1, 5, 2)
    assert float(out_scores[0]) == pytest.approx(0.95, abs=1e-5)
    # [16, 8, 48, 40] / 0.5 = [32, 16, 96, 80]
    np.testing.assert_allclose(out_boxes[0], [32.0, 16.0, 96.0, 80.0], atol=1e-4)
    # Нос (32 + 0, 16 + 4) / 0.5 = (64, 40)
    np.testing.assert_allclose(out_kps[0, 2], [64.0, 40.0], atol=1e-4)

    # Вариант на 6 выходов (без kps) должен синтезировать 5 точек внутри bbox
    b6, s6, k6 = decode_scrfd_outputs(
        score_tensors + bbox_tensors,
        (in_w, in_h),
        det_scale=0.5,
        score_thresh=0.50,
        iou_thresh=0.40,
    )
    assert b6.shape == (1, 4)
    assert s6.shape == (1,)
    assert k6.shape == (1, 5, 2)
    assert np.all(k6[0, :, 0] >= b6[0, 0]) and np.all(k6[0, :, 0] <= b6[0, 2])


# ---------------------------------------------------------------------------
# 4. Безопасная распаковка ZIP, манифест, SHA-256 и выбор провайдеров ORT
# ---------------------------------------------------------------------------


def test_safe_extract_zip_rejects_zip_slip_abs_and_symlink(tmp_path: Path) -> None:
    dest = tmp_path / "out"

    # 1. Zip-slip (../evil.onnx)
    slip_zip = tmp_path / "slip.zip"
    with zipfile.ZipFile(slip_zip, "w") as zf:
        zf.writestr("../evil.onnx", b"bad")
    with pytest.raises(ValueError, match="zip-slip"):
        safe_extract_zip(slip_zip, dest)
    assert not (tmp_path / "evil.onnx").exists()

    # 2. Абсолютный путь
    abs_zip = tmp_path / "abs.zip"
    with zipfile.ZipFile(abs_zip, "w") as zf:
        zf.writestr("/etc/evil.onnx", b"bad")
    with pytest.raises(ValueError):
        safe_extract_zip(abs_zip, dest)

    # 3. Симлинк в ZIP (Unix S_IFLNK)
    sym_zip = tmp_path / "sym.zip"
    with zipfile.ZipFile(sym_zip, "w") as zf:
        info = zipfile.ZipInfo("antelopev2/link.onnx")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError, match="Символические ссылки"):
        safe_extract_zip(sym_zip, dest)


def test_ensure_model_pack_mocked_download_manifest_and_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = MODEL_PACKS["antelopev2"]
    zip_buf = io.BytesIO()
    payloads = {
        spec.detector_file: b"detector-onnx-payload-bytes",
        spec.embedder_file: b"embedder-onnx-payload-bytes",
        spec.genderage_file: b"genderage-onnx-payload-bytes",
    }
    with zipfile.ZipFile(zip_buf, "w") as zf:
        for fname, content in payloads.items():
            zf.writestr(f"antelopev2/{fname}", content)
    zip_bytes = zip_buf.getvalue()
    zip_sha = hashlib.sha256(zip_bytes).hexdigest()

    class _FakeResp:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self.status_code = 200
            self.headers = {"Content-Length": str(len(data))}

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int = 65536) -> Any:
            for off in range(0, len(self._data), 16):
                yield self._data[off : off + 16]

    monkeypatch.setattr("sorter.vision.models.requests.get", lambda *a, **kw: _FakeResp(zip_bytes))
    expected_files = {fname: hashlib.sha256(content).hexdigest() for fname, content in payloads.items()}
    patched_spec = type(spec)(
        name=spec.name,
        url=spec.url,
        detector_file=spec.detector_file,
        embedder_file=spec.embedder_file,
        genderage_file=spec.genderage_file,
        zip_sha256=zip_sha,
        file_sha256s=expected_files,
    )
    monkeypatch.setitem(MODEL_PACKS, "antelopev2", patched_spec)

    models_dir = tmp_path / ".models"
    assert not is_model_pack_ready("antelopev2", models_dir)

    # Проверка отмены через stop_event
    stop = threading.Event()
    stop.set()
    with pytest.raises(CancelledError):
        ensure_model_pack("antelopev2", models_dir, stop_event=stop)

    # Проверка несовпадения SHA-256
    with pytest.raises(ValueError, match="SHA-256"):
        ensure_model_pack("antelopev2", models_dir, expected_zip_sha256="0" * 64)

    # Успешная загрузка + распаковка с выравниванием вложенной папки + manifest.json
    progress_calls: list[tuple[int, int | None]] = []
    pack = ensure_model_pack(
        "antelopev2",
        models_dir,
        expected_zip_sha256=zip_sha,
        expected_file_sha256s=expected_files,
        progress_cb=lambda done, total: progress_calls.append((done, total)),
    )
    assert progress_calls and progress_calls[-1][0] == len(zip_bytes)
    assert pack.detector_path.is_file()
    assert pack.embedder_path.is_file()
    assert pack.genderage_path.is_file()
    assert is_model_pack_ready("antelopev2", models_dir, verify_manifest=True)

    # Повреждённый ONNX файл на диске не проходит проверку SHA-256
    pack.detector_path.write_bytes(b"corrupted")
    assert not is_model_pack_ready("antelopev2", models_dir, verify_manifest=True)

    manifest = json.loads((pack.pack_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["zip_sha256"] == zip_sha
    for fname, content in payloads.items():
        assert manifest["files"][fname] == hashlib.sha256(content).hexdigest()


def test_select_ort_providers_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ort = SimpleNamespace(
        get_available_providers=lambda: [
            "CPUExecutionProvider",
            "CUDAExecutionProvider",
            "DmlExecutionProvider",
        ]
    )
    monkeypatch.setitem(__import__("sys").modules, "onnxruntime", fake_ort)
    assert select_ort_providers(prefer_gpu=True) == [
        "DmlExecutionProvider",
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]
    assert select_ort_providers(prefer_gpu=False) == ["CPUExecutionProvider"]


# ---------------------------------------------------------------------------
# 5. Видео: реальный PyAV MP4 с поворотом 90° + метаданные rotate/DISPLAYMATRIX
# ---------------------------------------------------------------------------


def _make_test_mp4(
    out_path: Path,
    *,
    width: int = 160,
    height: int = 96,
    fps: int = 10,
    num_frames: int = 20,
    display_rotation_ccw: int = 0,
    draw_fn: Any = None,
) -> None:
    container = av.open(str(out_path), mode="w")
    try:
        stream = container.add_stream("mpeg4", rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        if display_rotation_ccw != 0:
            stream.set_display_rotation(display_rotation_ccw)
        for i in range(num_frames):
            if draw_fn is not None:
                img = draw_fn(i, width, height)
            else:
                img = Image.new("RGB", (width, height), color=(20, 20, 20))
                draw = ImageDraw.Draw(img)
                # Ярко-красная полоса у левого края закодированного кадра (x < 30).
                # После поворота на 90° по часовой стрелке она обязана оказаться СВЕРХУ (y < 30)!
                draw.rectangle([0, 0, 28, height - 1], fill=(230, 30, 30))
                draw.rectangle([width - 28, 0, width - 1, height - 1], fill=(30, 30, 230))
            vf = av.VideoFrame.from_image(img)
            for pkt in stream.encode(vf):
                container.mux(pkt)
        for pkt in stream.encode():
            container.mux(pkt)
    finally:
        container.close()


def test_video_extract_keyframes_with_rotation_and_duration(tmp_path: Path) -> None:
    mp4_path = tmp_path / "rotated_portrait.mp4"
    # -90° CCW в display_matrix = 90° CW поворот до вертикальной ориентации (как на iPhone/Android)
    _make_test_mp4(mp4_path, width=160, height=96, fps=10, num_frames=20, display_rotation_ccw=-90)

    frames, duration = extract_video_keyframes(str(mp4_path), step_sec=0.5, max_frames=10)
    assert duration is not None and duration >= 1.5
    assert len(frames) >= 2

    timestamps = [ms for ms, _ in frames]
    assert len(set(timestamps)) == len(timestamps)
    assert timestamps == sorted(timestamps)

    _, first_img = frames[0]
    # После поворота на 90° CW закодированный кадр 160x96 становится портретным 96x160
    assert first_img.size == (96, 160)
    # Красная полоса с левого края (x < 28) после 90° CW должна оказаться вверху (y < 28)
    arr = np.asarray(first_img, dtype=np.float32)
    top_mean = arr[:20, :, :].mean(axis=(0, 1))
    bottom_mean = arr[-20:, :, :].mean(axis=(0, 1))
    assert top_mean[0] > 160 and top_mean[0] > top_mean[2] + 80
    assert bottom_mean[2] > 160 and bottom_mean[2] > bottom_mean[0] + 80


def test_video_rotation_parsing_metadata_and_display_matrix() -> None:
    # Проверка чтения legacy metadata["rotate"]
    s_meta_90 = SimpleNamespace(metadata={"rotate": "90"}, side_data=None, codec_context=None)
    assert get_stream_rotation_cw(s_meta_90) == 90

    s_meta_270 = SimpleNamespace(metadata={"Rotate": "270"}, side_data=None, codec_context=None)
    assert get_stream_rotation_cw(s_meta_270) == 270

    # Проверка чтения DISPLAYMATRIX (матрица поворота 90° CW: m[0]=0, m[1]=+65536, m[3]=-65536, m[4]=0)
    dm_cw_90 = struct.pack("<9i", 0, 65536, 0, -65536, 0, 0, 0, 0, 1 << 30)
    assert parse_display_matrix_rotation_cw(dm_cw_90) == 90

    s_sd = SimpleNamespace(metadata={}, side_data={"DISPLAYMATRIX": dm_cw_90}, codec_context=None)
    assert get_stream_rotation_cw(s_sd) == 90


# ---------------------------------------------------------------------------
# 6. Интеграция VisionEngine.process_pending_media (фото + видео + битый файл)
# ---------------------------------------------------------------------------


def test_vision_engine_process_pending_media_photo_video_and_corrupt(tmp_path: Path) -> None:
    src = tmp_path / "media"
    src.mkdir()

    # 1. Фото с двумя «людьми» (красный и синий квадраты) + EXIF Orientation=6 (поворот 90° CW)
    photo_path = src / "20240501_family.jpg"
    raw_photo = Image.new("RGB", (260, 400), color=(25, 25, 25))
    draw = ImageDraw.Draw(raw_photo)
    draw.rectangle([50, 40, 170, 160], fill=(220, 45, 45))
    draw.rectangle([50, 220, 170, 340], fill=(45, 70, 220))
    exif = raw_photo.getexif()
    exif[0x0112] = 6  # EXIF Orientation: Rotate 90 CW -> итоговый размер 400x260
    raw_photo.save(photo_path, format="JPEG", exif=exif)

    # 2. Видео с красным «человеком» на протяжении 2 секунд
    video_path = src / "20240502_clip.mp4"

    def _draw_video_frame(idx: int, w: int, h: int) -> Image.Image:
        frame = Image.new("RGB", (w, h), color=(25, 25, 25))
        d = ImageDraw.Draw(frame)
        shift = (idx % 4) * 2
        d.rectangle([40 + shift, 30, 130 + shift, 120], fill=(220, 45, 45))
        return frame

    _make_test_mp4(video_path, width=200, height=160, fps=10, num_frames=20, draw_fn=_draw_video_frame)

    # 3. Повреждённый JPEG (валидный заголовок, но битое тело) — не должен ронять пакет
    broken_path = src / "20240503_corrupt.jpg"
    broken_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF\x00" + b"\xde\xad\xbe\xef" * 10)

    crops_dir = tmp_path / "cache" / "crops"
    thumbs_dir = tmp_path / "cache" / "thumbs"

    with SorterState(tmp_path / "vision.db") as state:
        scan_summary = scan_directory(str(src), state)
        assert scan_summary.discovered == 3

        engine = VisionEngine(
            detector=FakeFaceDetector(),
            embedder=FakeFaceEmbedder(default_age=7.0, default_gender=0),
            crops_dir=crops_dir,
            thumbs_dir=thumbs_dir,
            video_step_sec=0.5,
            video_max_frames=10,
            max_faces_per_video=40,
        )
        progress_events: list[dict[str, int]] = []
        summary = engine.process_pending_media(
            state,
            progress_cb=lambda stats, _row: progress_events.append(stats),
        )

        assert summary["total"] == 3
        assert summary["processed"] == 3
        assert summary["scanned"] == 2
        assert summary["errors"] == 1
        assert summary["faces"] >= 4
        assert len(progress_events) == 3

        # Проверяем фото: EXIF применён (400x260), 2 лица, cannot_link, превью и кропы на диске
        photo_rows = [r for r in state.list_media() if r["rel_path"].endswith(".jpg") and "family" in r["rel_path"]]
        assert len(photo_rows) == 1
        photo_row = photo_rows[0]
        assert photo_row["scan_status"] == "scanned"
        assert photo_row["width"] == 400 and photo_row["height"] == 260
        assert (thumbs_dir / photo_row["thumb_rel_path"]).is_file()

        photo_faces = state.list_faces(media_id=int(photo_row["id"]))
        assert len(photo_faces) == 2
        f0_id, f1_id = int(photo_faces[0]["id"]), int(photo_faces[1]["id"])
        assert (min(f0_id, f1_id), max(f0_id, f1_id)) in state.get_cannot_link_face_pairs()
        for f in photo_faces:
            assert (crops_dir / f["crop_rel_path"]).is_file()
            assert f["est_age"] == pytest.approx(7.0)
            assert f["est_gender"] == 0

        # Проверяем видео: >= 2 лица с разными frame_ms, длительность заполнена, эмбеддинги совпадают с красным
        video_rows = [r for r in state.list_media(media_kind="video")]
        assert len(video_rows) == 1
        video_row = video_rows[0]
        assert video_row["scan_status"] == "scanned"
        assert video_row["duration_sec"] is not None and video_row["duration_sec"] >= 1.5
        assert (thumbs_dir / video_row["thumb_rel_path"]).is_file()

        video_faces = state.list_faces(media_id=int(video_row["id"]))
        assert len(video_faces) >= 2
        distinct_frames = {int(f["frame_ms"]) for f in video_faces}
        assert len(distinct_frames) >= 2

        # Сравнение эмбеддингов: лицо из видео должно совпасть с одним из двух лиц на фото и отличаться от другого
        v_emb = decode_embedding(video_faces[0]["embedding"])
        p_embs = [decode_embedding(f["embedding"]) for f in photo_faces]
        sims = sorted(float(np.dot(v_emb, pe)) for pe in p_embs)
        assert sims[1] > 0.95
        assert sims[0] < 0.50

        # Битый файл помечен ошибкой
        corrupt_rows = [r for r in state.list_media(scan_status="error")]
        assert len(corrupt_rows) == 1
        assert "corrupt.jpg" in corrupt_rows[0]["rel_path"]

        # Повторный вызов process_pending_media ничего не обрабатывает (0 pending)
        again = process_pending_media(state, engine)
        assert again == {"total": 0, "processed": 0, "scanned": 0, "errors": 0, "faces": 0}


def test_video_face_cap_preserves_distinct_frame_timestamps(tmp_path: Path) -> None:
    video_path = tmp_path / "many_faces.mp4"
    _make_test_mp4(video_path, width=160, height=120, fps=10, num_frames=20)

    # По 3 лица в каждом кадре, но лимит max_faces_per_video=4 -> обязаны остаться разные frame_ms
    fixed = [
        DetectedFace(
            bbox=(10.0 + i * 45.0, 20.0, 50.0 + i * 45.0, 60.0),
            kps=[[20.0 + i * 45.0, 32.0], [40.0 + i * 45.0, 32.0], [30.0 + i * 45.0, 42.0], [22.0 + i * 45.0, 50.0], [38.0 + i * 45.0, 50.0]],
            det_score=0.95 - i * 0.05,
            quality_score=0.90 - i * 0.05,
        )
        for i in range(3)
    ]

    with SorterState(tmp_path / "cap.db") as state:
        mid, _ = state.upsert_media_file(
            local_path=str(video_path),
            rel_path="many_faces.mp4",
            source_root=str(tmp_path),
            media_kind="video",
            size=video_path.stat().st_size,
            mtime_ns=video_path.stat().st_mtime_ns,
        )
        engine = VisionEngine(
            detector=FakeFaceDetector(fixed_faces=fixed),
            embedder=FakeFaceEmbedder(),
            crops_dir=tmp_path / "crops",
            thumbs_dir=tmp_path / "thumbs",
            video_step_sec=0.4,
            max_faces_per_video=4,
        )
        count = engine.process_media_row(state.get_media(mid) or {}, state)
        assert count == 4
        faces = state.list_faces(media_id=mid)
        assert len({int(f["frame_ms"]) for f in faces}) >= 2


def test_onnx_wrappers_preprocessing_and_output_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    det_file = tmp_path / "scrfd_10g_bnkps.onnx"
    emb_file = tmp_path / "glintr100.onnx"
    ga_file = tmp_path / "genderage.onnx"
    for f in (det_file, emb_file, ga_file):
        f.write_bytes(b"stub")

    captured: dict[str, np.ndarray] = {}

    class _MockSession:
        def __init__(self, model_path: str, providers: list[str] | None = None) -> None:
            self.model_path = model_path
            self.providers = providers

        def get_inputs(self) -> list[Any]:
            return [SimpleNamespace(name="input")]

        def run(self, _out: Any, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
            blob = feed["input"]
            name = Path(self.model_path).name
            captured[name] = blob
            if name == "glintr100.onnx":
                vec = np.arange(1, 513, dtype=np.float32)[None, :]
                return [vec]
            if name == "genderage.onnx":
                # [gender_f, gender_m, age_scaled] -> female (0), 6.5 years
                return [np.array([[2.5, -1.0, 0.065]], dtype=np.float32)]
            # SCRFD 9-output для input_size=(64, 64)
            outs_s: list[np.ndarray] = []
            outs_b: list[np.ndarray] = []
            outs_k: list[np.ndarray] = []
            for s in (8, 16, 32):
                n = (64 // s) * (64 // s) * 2
                outs_s.append(np.zeros((n, 1), dtype=np.float32))
                outs_b.append(np.zeros((n, 4), dtype=np.float32))
                outs_k.append(np.zeros((n, 10), dtype=np.float32))
            idx = (2 * 4 + 2) * 2  # stride 16 -> центр (32, 32)
            outs_s[1][idx, 0] = 0.91
            outs_b[1][idx] = [1.0, 1.0, 1.0, 1.0]
            outs_k[1][idx] = [-0.4, -0.3, 0.4, -0.3, 0.0, 0.1, -0.3, 0.5, 0.3, 0.5]
            return outs_s + outs_b + outs_k

    fake_ort = SimpleNamespace(
        InferenceSession=_MockSession,
        get_available_providers=lambda: ["CPUExecutionProvider"],
    )
    monkeypatch.setitem(__import__("sys").modules, "onnxruntime", fake_ort)

    detector = ScrfdDetector(det_file, input_size=(64, 64))
    img = Image.new("RGB", (128, 64), color=(255, 127, 0))
    faces = detector.detect(img)
    assert len(faces) == 1
    assert captured["scrfd_10g_bnkps.onnx"].shape == (1, 3, 64, 64)
    # Проверка нормализации (rgb - 127.5) / 128.0 на верхнем левом пикселе (255, 127, 0)
    assert float(captured["scrfd_10g_bnkps.onnx"][0, 0, 0, 0]) == pytest.approx(
        (255.0 - 127.5) / 128.0, abs=1e-4
    )
    assert float(captured["scrfd_10g_bnkps.onnx"][0, 2, 0, 0]) == pytest.approx(
        (0.0 - 127.5) / 128.0, abs=1e-4
    )

    embedder = ArcFaceEmbedder(emb_file, genderage_path=ga_file)
    vec = embedder.embed(Image.new("RGB", (112, 112), color=(255, 0, 127)))
    assert vec.shape == (512,)
    assert vec.dtype == np.float32
    assert float(np.linalg.norm(vec)) == pytest.approx(1.0, abs=1e-5)
    assert captured["glintr100.onnx"].shape == (1, 3, 112, 112)
    assert float(captured["glintr100.onnx"][0, 0, 0, 0]) == pytest.approx(1.0, abs=1e-5)

    age, gender = embedder.estimate_age_gender(
        Image.new("RGB", (100, 100), color=(200, 100, 50)),
        [20.0, 20.0, 80.0, 80.0],
    )
    assert age == pytest.approx(6.5)
    assert gender == 0
    # Проверка BGR [0..255] в genderage.onnx: R=200 -> канал 2, B=50 -> канал 0
    ga_blob = captured["genderage.onnx"]
    assert ga_blob.shape == (1, 3, 96, 96)
    assert float(ga_blob[0, 0, 48, 48]) == pytest.approx(50.0, abs=1.0)
    assert float(ga_blob[0, 2, 48, 48]) == pytest.approx(200.0, abs=1.0)

