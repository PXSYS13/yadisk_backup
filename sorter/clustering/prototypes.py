"""Возрастные мульти-прототипы людей и Stage 3 доназначение неразмеченных/размытых лиц и видео."""

from __future__ import annotations

import logging
import math
import time
from typing import Any

import numpy as np

from sorter.state import SorterState, decode_embedding

log = logging.getLogger("photo_sorter.clustering.prototypes")

PREFERRED_PROTO_QUALITY = 0.35
MIN_WEIGHT = 0.05
TEMPORAL_BONUS_MAX = 0.03
TEMPORAL_DECAY_YEARS = 2.0
VIDEO_SHORT_DURATION_SEC = 3.5
VIDEO_SHARP_MIN_QUALITY = 0.60


def year_from_ts(shot_ts: float | None) -> int:
    """Извлекает календарный год (UTC) из timestamp или 0, если дата отсутствует."""
    if shot_ts is None:
        return 0
    try:
        val = float(shot_ts)
        if not math.isfinite(val):
            return 0
        return int(time.gmtime(val).tm_year)
    except (TypeError, ValueError, OverflowError, OSError):
        return 0


def _compute_weighted_centroid(
    embeddings: list[np.ndarray], weights: list[float]
) -> np.ndarray:
    """Считает L2-нормализованный центроид, взвешенный по качеству лиц."""
    mat = np.stack(embeddings, axis=0).astype(np.float32, copy=False)
    w = np.asarray([max(MIN_WEIGHT, float(val)) for val in weights], dtype=np.float32)
    centroid = (mat * w[:, None]).sum(axis=0)
    norm = float(np.linalg.norm(centroid))
    if norm > 1e-12:
        centroid = centroid / norm
        return centroid.astype(np.float32, copy=False)
    best_idx = int(np.argmax(w))
    return mat[best_idx].copy()


def _build_person_prototypes_from_faces(
    faces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Формирует список возрастных прототипов (по годам) для одного человека."""
    by_year: dict[int, list[dict[str, Any]]] = {}
    for face in faces:
        yb = year_from_ts(face.get("shot_ts"))
        by_year.setdefault(yb, []).append(face)

    prototypes: list[dict[str, Any]] = []
    for year_bucket in sorted(by_year):
        bucket_faces = by_year[year_bucket]
        preferred = [
            f for f in bucket_faces if float(f.get("quality_score", 0.0)) >= PREFERRED_PROTO_QUALITY
        ]
        selected = preferred if preferred else bucket_faces

        embeddings = [decode_embedding(f["embedding"]) for f in selected]
        weights = [float(f.get("quality_score", 0.0)) for f in selected]
        centroid = _compute_weighted_centroid(embeddings, weights)

        ages = [float(f["est_age"]) for f in selected if f.get("est_age") is not None]
        if not ages:
            ages = [float(f["est_age"]) for f in bucket_faces if f.get("est_age") is not None]
        mean_est_age = float(sum(ages) / len(ages)) if ages else None

        prototypes.append(
            {
                "year_bucket": int(year_bucket),
                "face_count": len(bucket_faces),
                "mean_est_age": mean_est_age,
                "centroid": centroid,
            }
        )
    return prototypes


def rebuild_person_prototypes(state: SorterState, person_id: int | None = None) -> int:
    """Перестраивает возрастные мульти-прототипы (по годам) для одного или всех неигнорируемых людей."""
    if person_id is not None:
        pid = int(person_id)
        person = state.get_person(pid)
        if person is None or int(person.get("is_ignored", 0)) != 0:
            state.replace_person_prototypes(pid, [])
            return 0
        faces = state.list_faces_with_media(person_id=pid, include_rejected=False)
        protos = _build_person_prototypes_from_faces(faces)
        state.replace_person_prototypes(pid, protos)
        return len(protos)

    persons = state.list_persons(include_ignored=True)
    active_ids = {int(p["id"]) for p in persons if int(p.get("is_ignored", 0)) == 0}

    all_faces = state.list_faces_with_media(include_rejected=False)
    by_person: dict[int, list[dict[str, Any]]] = {}
    for face in all_faces:
        fpid = face.get("person_id")
        if fpid is not None and int(fpid) in active_ids:
            by_person.setdefault(int(fpid), []).append(face)

    state.clear_all_prototypes()
    total = 0
    for pid in sorted(by_person):
        protos = _build_person_prototypes_from_faces(by_person[pid])
        state.replace_person_prototypes(pid, protos)
        total += len(protos)
    return total


def _build_cannot_link_adj(pairs: set[tuple[int, int]]) -> dict[int, set[int]]:
    adj: dict[int, set[int]] = {}
    for a, b in pairs:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return adj


def _is_person_forbidden_for_face(
    face: dict[str, Any],
    person_id: int,
    face_to_person: dict[int, int],
    frame_persons: dict[tuple[int, int], set[int]],
    cl_adj: dict[int, set[int]],
) -> bool:
    frame_key = (int(face["media_id"]), int(face.get("frame_ms", 0)))
    if person_id in frame_persons.get(frame_key, ()):
        return True
    fid = int(face["id"])
    for other_fid in cl_adj.get(fid, ()):
        if face_to_person.get(other_fid) == person_id:
            return True
    return False


def assign_unassigned_faces(
    state: SorterState,
    *,
    photo_threshold: float = 0.48,
    video_threshold: float = 0.50,
    video_single_sharp_threshold: float = 0.62,
) -> int:
    """Stage 3: прикрепляет неразмеченные/размытые лица и кадры видео к лучшим возрастным прототипам."""
    active_persons = state.list_persons(include_ignored=False)
    active_person_ids = {int(p["id"]) for p in active_persons}
    if not active_person_ids:
        return 0

    raw_protos = [
        p for p in state.list_prototypes() if int(p["person_id"]) in active_person_ids
    ]
    if not raw_protos:
        return 0

    all_faces = state.list_faces_with_media(include_rejected=False)
    unassigned_faces: list[dict[str, Any]] = []
    face_to_person: dict[int, int] = {}
    frame_persons: dict[tuple[int, int], set[int]] = {}
    existing_video_person_frames: dict[tuple[int, int], set[int]] = {}

    for face in all_faces:
        fid = int(face["id"])
        mid = int(face["media_id"])
        frame_ms = int(face.get("frame_ms", 0))
        pid = face.get("person_id")
        if pid is None:
            unassigned_faces.append(face)
        else:
            pid_int = int(pid)
            face_to_person[fid] = pid_int
            frame_persons.setdefault((mid, frame_ms), set()).add(pid_int)
            if face.get("media_kind") == "video":
                existing_video_person_frames.setdefault((mid, pid_int), set()).add(frame_ms)

    if not unassigned_faces:
        return 0

    cl_adj = _build_cannot_link_adj(state.get_cannot_link_face_pairs())

    proto_mat = np.stack(
        [decode_embedding(p["centroid"]) for p in raw_protos], axis=0
    ).astype(np.float32, copy=False)
    proto_pids = np.asarray([int(p["person_id"]) for p in raw_protos], dtype=np.int64)
    proto_years = np.asarray([int(p["year_bucket"]) for p in raw_protos], dtype=np.int32)

    face_mat = np.stack(
        [decode_embedding(f["embedding"]) for f in unassigned_faces], axis=0
    ).astype(np.float32, copy=False)
    face_years = np.asarray(
        [year_from_ts(f.get("shot_ts")) for f in unassigned_faces], dtype=np.int32
    )

    raw_sims = face_mat @ proto_mat.T  # (N_unassigned, M_protos)

    # Временной бонус 0.03 * exp(-|face_year - proto_year| / 2.0), если оба года известны (> 0)
    both_dated = (face_years[:, None] > 0) & (proto_years[None, :] > 0)
    year_diff = np.abs(face_years[:, None] - proto_years[None, :]).astype(np.float32)
    bonus = np.where(
        both_dated,
        TEMPORAL_BONUS_MAX * np.exp(-year_diff / TEMPORAL_DECAY_YEARS),
        0.0,
    ).astype(np.float32)
    weighted_sims = raw_sims + bonus

    raw_photo_floor = float(photo_threshold) - 0.02

    # --- 1. Фотографии ---
    photo_candidates: list[tuple[float, int, list[tuple[float, float, int]]]] = []
    # --- 2. Видео ---
    # (media_id, person_id) -> list of (weighted_sim, raw_sim, face_idx)
    video_groups: dict[tuple[int, int], list[tuple[float, float, int]]] = {}

    for idx, face in enumerate(unassigned_faces):
        w_row = weighted_sims[idx]
        r_row = raw_sims[idx]
        is_video = face.get("media_kind") == "video"

        if not is_video:
            valid_mask = (r_row >= raw_photo_floor) & (w_row >= photo_threshold)
            valid_indices = np.flatnonzero(valid_mask)
            if valid_indices.size == 0:
                continue
            best_per_person: dict[int, tuple[float, float]] = {}
            for p_idx in valid_indices:
                pid = int(proto_pids[p_idx])
                ws = float(w_row[p_idx])
                rs = float(r_row[p_idx])
                prev = best_per_person.get(pid)
                if prev is None or ws > prev[0]:
                    best_per_person[pid] = (ws, rs)
            ranked = sorted(
                ((ws, rs, pid) for pid, (ws, rs) in best_per_person.items()),
                key=lambda x: (-x[0], -x[1], x[2]),
            )
            photo_candidates.append((ranked[0][0], idx, ranked))
        else:
            valid_indices = np.flatnonzero(r_row >= video_threshold)
            if valid_indices.size == 0:
                continue
            best_per_person_v: dict[int, tuple[float, float]] = {}
            for p_idx in valid_indices:
                pid = int(proto_pids[p_idx])
                ws = float(w_row[p_idx])
                rs = float(r_row[p_idx])
                prev = best_per_person_v.get(pid)
                if prev is None or ws > prev[0]:
                    best_per_person_v[pid] = (ws, rs)
            ranked_v = sorted(
                ((ws, rs, pid) for pid, (ws, rs) in best_per_person_v.items()),
                key=lambda x: (-x[0], -x[1], x[2]),
            )
            # Берём лучшего не запрещённого заранее кандидата для группировки по (media_id, person_id)
            mid = int(face["media_id"])
            for ws, rs, pid in ranked_v:
                if not _is_person_forbidden_for_face(
                    face, pid, face_to_person, frame_persons, cl_adj
                ):
                    video_groups.setdefault((mid, pid), []).append((ws, rs, idx))
                    break

    updates: list[tuple[int, int | None, int | None, float, str]] = []

    # Применяем фото-назначения от максимального сходства к меньшему с жёсткой проверкой cannot_link
    photo_candidates.sort(key=lambda item: (-item[0], item[1]))
    for _top_score, idx, ranked in photo_candidates:
        face = unassigned_faces[idx]
        fid = int(face["id"])
        frame_key = (int(face["media_id"]), int(face.get("frame_ms", 0)))
        for ws, _rs, pid in ranked:
            if _is_person_forbidden_for_face(face, pid, face_to_person, frame_persons, cl_adj):
                continue
            conf = float(min(1.0, max(0.0, ws)))
            updates.append((fid, face.get("cluster_id"), pid, conf, "prototype"))
            face_to_person[fid] = pid
            frame_persons.setdefault(frame_key, set()).add(pid)
            break

    # Применяем видео-назначения по многокадровому правилу
    if video_groups:
        video_durations: dict[int, float | None] = {
            int(m["id"]): (
                float(m["duration_sec"]) if m.get("duration_sec") is not None else None
            )
            for m in state.list_media(media_kind="video")
        }
        ordered_groups = sorted(
            video_groups.items(),
            key=lambda kv: (-max(item[0] for item in kv[1]), kv[0][0], kv[0][1]),
        )
        for (mid, pid), entries in ordered_groups:
            # В каждом ключевом кадре (frame_ms) оставляем не более одного лучшего лица для pid
            best_by_frame: dict[int, tuple[float, float, int]] = {}
            for ws, rs, idx in entries:
                face = unassigned_faces[idx]
                if _is_person_forbidden_for_face(face, pid, face_to_person, frame_persons, cl_adj):
                    continue
                fms = int(face.get("frame_ms", 0))
                prev_frame = best_by_frame.get(fms)
                if prev_frame is None or ws > prev_frame[0]:
                    best_by_frame[fms] = (ws, rs, idx)

            if not best_by_frame:
                continue

            distinct_frames = len(
                set(best_by_frame.keys()) | existing_video_person_frames.get((mid, pid), set())
            )
            accepted = False
            if distinct_frames >= 2:
                accepted = True
            else:
                duration = video_durations.get(mid)
                is_short = (
                    duration is not None
                    and math.isfinite(duration)
                    and duration <= VIDEO_SHORT_DURATION_SEC
                )
                if is_short:
                    best_ws, best_rs, best_idx = next(iter(best_by_frame.values()))
                    best_quality = float(unassigned_faces[best_idx].get("quality_score", 0.0))
                    if (
                        best_quality >= VIDEO_SHARP_MIN_QUALITY
                        and best_rs >= video_single_sharp_threshold
                    ):
                        accepted = True

            if not accepted:
                continue

            for fms, (ws, _rs, idx) in sorted(best_by_frame.items()):
                face = unassigned_faces[idx]
                if _is_person_forbidden_for_face(face, pid, face_to_person, frame_persons, cl_adj):
                    continue
                fid = int(face["id"])
                conf = float(min(1.0, max(0.0, ws)))
                updates.append((fid, face.get("cluster_id"), pid, conf, "prototype"))
                face_to_person[fid] = pid
                frame_persons.setdefault((mid, fms), set()).add(pid)
                existing_video_person_frames.setdefault((mid, pid), set()).add(fms)

    if updates:
        state.set_face_assignments(updates)
    return len(updates)


def list_person_faces_outlier_first(
    state: SorterState, person_id: int
) -> list[dict[str, Any]]:
    """Возвращает лица человека, отсортированные по возрастанию сходства с прототипами (выбросы первыми)."""
    pid = int(person_id)
    faces = state.list_faces_with_media(person_id=pid, include_rejected=False)
    if not faces:
        return []

    protos = state.list_prototypes(person_id=pid)
    if protos:
        proto_mat = np.stack(
            [decode_embedding(p["centroid"]) for p in protos], axis=0
        ).astype(np.float32, copy=False)
    else:
        computed = _build_person_prototypes_from_faces(faces)
        proto_mat = np.stack(
            [np.asarray(p["centroid"], dtype=np.float32) for p in computed], axis=0
        ).astype(np.float32, copy=False)

    face_mat = np.stack(
        [decode_embedding(f["embedding"]) for f in faces], axis=0
    ).astype(np.float32, copy=False)
    sims = face_mat @ proto_mat.T
    max_sims = sims.max(axis=1)

    enriched: list[dict[str, Any]] = []
    for face, sim_val in zip(faces, max_sims, strict=True):
        item = dict(face)
        item["proto_similarity"] = float(sim_val)
        enriched.append(item)

    enriched.sort(
        key=lambda f: (
            float(f["proto_similarity"]),
            float(f.get("quality_score", 0.0)),
            int(f["id"]),
        )
    )
    return enriched
