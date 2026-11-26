"""Подсистема компьютерного зрения: ONNX-модели, выравнивание лиц, детекция, эмбеддинги и видео."""

from __future__ import annotations

from sorter.vision.align import (
    ARC_FACE_DST_112,
    align_face_112,
    estimate_similarity_transform,
    save_face_crop,
    save_media_thumb,
)
from sorter.vision.detector import (
    DetectedFace,
    FaceDetectorProtocol,
    ScrfdDetector,
    compute_face_quality,
    decode_scrfd_outputs,
    distance2bbox,
    distance2kps,
    nms,
)
from sorter.vision.embedder import (
    ArcFaceEmbedder,
    FaceEmbedderProtocol,
    FakeFaceDetector,
    FakeFaceEmbedder,
)
from sorter.vision.models import (
    MODEL_PACKS,
    ModelPackPaths,
    ModelPackSpec,
    download_file,
    ensure_model_pack,
    is_model_pack_ready,
    safe_extract_zip,
    select_ort_providers,
)
from sorter.vision.pipeline import VisionEngine, process_pending_media
from sorter.vision.video import (
    apply_rotation_cw,
    extract_video_keyframes,
    get_stream_rotation_cw,
    parse_display_matrix_rotation_cw,
)

__all__ = [
    "ARC_FACE_DST_112",
    "ArcFaceEmbedder",
    "DetectedFace",
    "FaceDetectorProtocol",
    "FaceEmbedderProtocol",
    "FakeFaceDetector",
    "FakeFaceEmbedder",
    "MODEL_PACKS",
    "ModelPackPaths",
    "ModelPackSpec",
    "ScrfdDetector",
    "VisionEngine",
    "align_face_112",
    "apply_rotation_cw",
    "compute_face_quality",
    "decode_scrfd_outputs",
    "distance2bbox",
    "distance2kps",
    "download_file",
    "ensure_model_pack",
    "estimate_similarity_transform",
    "extract_video_keyframes",
    "get_stream_rotation_cw",
    "is_model_pack_ready",
    "nms",
    "parse_display_matrix_rotation_cw",
    "process_pending_media",
    "safe_extract_zip",
    "save_face_crop",
    "save_media_thumb",
    "select_ort_providers",
]
