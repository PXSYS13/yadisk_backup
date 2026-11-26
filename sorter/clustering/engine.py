"""Stage 1: Высокоточная кластеризация с жёсткими ограничениями Cannot-Link и сохранением разметки пользователя."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

from sorter.clustering.prototypes import (
    assign_unassigned_faces,
    rebuild_person_prototypes,
)
from sorter.state import SorterState, decode_embedding

log = logging.getLogger("photo_sorter.clustering.engine")

EXISTING_NAMED_MATCH_THRESHOLD = 0.58
LARGE_CLUSTER_MIN_SIZE = 10
MAX_PAIRWISE_SAMPLE = 256


@dataclass(frozen=True)
class ClusterConfig:
    """Параметры Stage 1 высокоточной кластеризации."""

    min_seed_quality: float = 0.45
    merge_threshold: float = 0.56
    min_link_threshold: float = 0.42
    mutual_knn_k: int = 25
    max_cluster_merge_batch: int = 5000


def _build_cl_adjacency(pairs: set[tuple[int, int]]) -> dict[int, set[int]]:
    adj: dict[int, set[int]] = {}
    for a, b in pairs:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return adj


def _find_mutual_knn_edges(
    embeddings: np.ndarray,
    sim_matrix: np.ndarray | None,
    face_ids: list[int],
    frame_keys: list[tuple[int, int]],
    cl_face_pairs: set[tuple[int, int]],
    *,
    merge_threshold: float,
    mutual_knn_k: int,
    batch_size: int = 2048,
) -> list[tuple[float, int, int]]:
    """Находит взаимные top-k рёбра выше merge_threshold с исключением cannot_link и одного кадра."""
    n = embeddings.shape[0]
    if n < 2:
        return []
    k = min(max(1, int(mutual_knn_k)), n - 1)

    topk_sets: list[set[int]] = [set() for _ in range(n)]
    cand_sims: dict[tuple[int, int], float] = {}

    if sim_matrix is not None:
        # Быстрый путь с готовой матрицей N x N
        if k < n - 1:
            idx_part = np.argpartition(sim_matrix, -k, axis=1)[:, -k:]
        else:
            idx_part = np.broadcast_to(np.arange(n, dtype=np.intp), (n, n))
        for i in range(n):
            row = sim_matrix[i]
            nbrs = idx_part[i]
            s_i = topk_sets[i]
            for j_raw in nbrs:
                j = int(j_raw)
                if j == i:
                    continue
                val = float(row[j])
                if val >= merge_threshold:
                    s_i.add(j)
                    a, b = (i, j) if i < j else (j, i)
                    cand_sims[(a, b)] = val
    else:
        # Блочный путь для больших архивов (> max_cluster_merge_batch)
        step = max(256, min(batch_size, n))
        for start in range(0, n, step):
            end = min(n, start + step)
            block = embeddings[start:end] @ embeddings.T
            for local_r in range(end - start):
                global_i = start + local_r
                block[local_r, global_i] = -2.0
            if k < n - 1:
                idx_part = np.argpartition(block, -k, axis=1)[:, -k:]
            else:
                idx_part = np.broadcast_to(np.arange(n, dtype=np.intp), (end - start, n))
            for local_r in range(end - start):
                i = start + local_r
                row = block[local_r]
                s_i = topk_sets[i]
                for j_raw in idx_part[local_r]:
                    j = int(j_raw)
                    if j == i:
                        continue
                    val = float(row[j])
                    if val >= merge_threshold:
                        s_i.add(j)
                        a, b = (i, j) if i < j else (j, i)
                        cand_sims[(a, b)] = val

    edges: list[tuple[float, int, int]] = []
    for (i, j), sim_val in cand_sims.items():
        if j not in topk_sets[i] or i not in topk_sets[j]:
            continue
        if frame_keys[i] == frame_keys[j]:
            continue
        fa, fb = sorted((face_ids[i], face_ids[j]))
        if (fa, fb) in cl_face_pairs:
            continue
        edges.append((sim_val, i, j))

    edges.sort(key=lambda item: (-item[0], item[1], item[2]))
    return edges


def _pair_linkage_ok(
    members_a: list[int],
    members_b: list[int],
    sum_vec_a: np.ndarray,
    sum_vec_b: np.ndarray,
    embeddings: np.ndarray,
    sim_matrix: np.ndarray | None,
    *,
    merge_threshold: float,
    min_link_threshold: float,
) -> bool:
    """Проверяет среднее сходство >= merge_threshold и минимум (или 5-й перцентиль) >= min_link_threshold."""
    len_a = len(members_a)
    len_b = len(members_b)
    avg_sim = float(np.dot(sum_vec_a, sum_vec_b)) / float(len_a * len_b)
    if avg_sim < merge_threshold:
        return False

    # Детерминированная выборка для очень больших пар, чтобы проверка оставалась O(1) мс
    idx_a = members_a
    idx_b = members_b
    if len_a > MAX_PAIRWISE_SAMPLE:
        step_a = max(1, len_a // MAX_PAIRWISE_SAMPLE)
        idx_a = members_a[::step_a][:MAX_PAIRWISE_SAMPLE]
    if len_b > MAX_PAIRWISE_SAMPLE:
        step_b = max(1, len_b // MAX_PAIRWISE_SAMPLE)
        idx_b = members_b[::step_b][:MAX_PAIRWISE_SAMPLE]

    if sim_matrix is not None:
        block = sim_matrix[np.ix_(idx_a, idx_b)]
    else:
        block = embeddings[idx_a] @ embeddings[idx_b].T

    if len_a + len_b <= LARGE_CLUSTER_MIN_SIZE:
        return float(block.min()) >= min_link_threshold
    return float(np.percentile(block, 5)) >= min_link_threshold


def _constrained_agglomerative_cluster(
    embeddings: np.ndarray,
    face_ids: list[int],
    frame_keys: list[tuple[int, int]],
    cl_face_pairs: set[tuple[int, int]],
    config: ClusterConfig,
) -> list[list[int]]:
    """Жадная агломеративная кластеризация по убыванию сходства с жёстким cannot_link и защитой от цепочек."""
    n = embeddings.shape[0]
    if n == 0:
        return []
    if n == 1:
        return [[0]]

    sim_matrix: np.ndarray | None = None
    if n <= config.max_cluster_merge_batch:
        sim_matrix = embeddings @ embeddings.T
        np.fill_diagonal(sim_matrix, -2.0)

    edges = _find_mutual_knn_edges(
        embeddings,
        sim_matrix,
        face_ids,
        frame_keys,
        cl_face_pairs,
        merge_threshold=config.merge_threshold,
        mutual_knn_k=config.mutual_knn_k,
    )

    # Локальные cannot_link индексы внутри seed-набора
    fid_to_local = {fid: idx for idx, fid in enumerate(face_ids)}
    local_cl: list[set[int]] = [set() for _ in range(n)]
    for fa, fb in cl_face_pairs:
        la = fid_to_local.get(fa)
        lb = fid_to_local.get(fb)
        if la is not None and lb is not None and la != lb:
            local_cl[la].add(lb)
            local_cl[lb].add(la)

    # Компоненты связности (union-find с малым->большим слиянием)
    parent = list(range(n))
    members: dict[int, list[int]] = {i: [i] for i in range(n)}
    member_sets: dict[int, set[int]] = {i: {i} for i in range(n)}
    forbidden: dict[int, set[int]] = {i: set(local_cl[i]) for i in range(n)}
    comp_frames: dict[int, set[tuple[int, int]]] = {i: {frame_keys[i]} for i in range(n)}
    sum_vecs: dict[int, np.ndarray] = {i: embeddings[i].copy() for i in range(n)}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def try_merge(u: int, v: int) -> bool:
        ra = find(u)
        rb = find(v)
        if ra == rb:
            return False

        # Гарантируем |A| <= |B| для быстрой проверки пересечений
        if len(members[ra]) > len(members[rb]):
            ra, rb = rb, ra

        # 1. Жёсткое правило Cannot-Link + запрет двух лиц из одного кадра
        if not member_sets[ra].isdisjoint(forbidden[rb]):
            return False
        if not comp_frames[ra].isdisjoint(comp_frames[rb]):
            return False

        # 2. Критерий связности: среднее >= merge_threshold и минимум/p05 >= min_link_threshold
        if not _pair_linkage_ok(
            members[ra],
            members[rb],
            sum_vecs[ra],
            sum_vecs[rb],
            embeddings,
            sim_matrix,
            merge_threshold=config.merge_threshold,
            min_link_threshold=config.min_link_threshold,
        ):
            return False

        # Сливаем ra -> rb
        parent[ra] = rb
        members[rb].extend(members[ra])
        member_sets[rb].update(member_sets[ra])
        forbidden[rb].update(forbidden[ra])
        comp_frames[rb].update(comp_frames[ra])
        sum_vecs[rb] = sum_vecs[rb] + sum_vecs[ra]
        del members[ra]
        del member_sets[ra]
        del forbidden[ra]
        del comp_frames[ra]
        del sum_vecs[ra]
        return True

    # Проход 1: жадное объединение по отсортированным взаимным kNN-рёбрам
    for _sim_val, u, v in edges:
        try_merge(u, v)

    # Проход 2: дослияние плотных кластеров (если mutual_knn_k < размера большого кластера)
    # Использует те же жёсткие критерии cannot_link + avg_sim >= merge_threshold + min/p05 >= min_link_threshold
    active_roots = sorted(members.keys())
    if 2 <= len(active_roots) <= 2000:
        cent_list: list[np.ndarray] = []
        for root in active_roots:
            sv = sum_vecs[root]
            norm = float(np.linalg.norm(sv))
            cent_list.append(sv / norm if norm > 1e-12 else sv)
        cent_mat = np.stack(cent_list, axis=0).astype(np.float32, copy=False)
        c_sims = cent_mat @ cent_mat.T
        np.fill_diagonal(c_sims, -2.0)

        rows, cols = np.where(np.triu(c_sims, k=1) >= config.merge_threshold)
        cand_pairs = [
            (float(c_sims[r, c]), active_roots[int(r)], active_roots[int(c)])
            for r, c in zip(rows, cols, strict=True)
        ]
        cand_pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
        for _csim, ra, rb in cand_pairs:
            try_merge(ra, rb)

    clusters = sorted(
        members.values(),
        key=lambda cl: (-len(cl), min(face_ids[i] for i in cl)),
    )
    return clusters


def run_initial_clustering(
    state: SorterState, config: ClusterConfig | None = None
) -> dict[str, int]:
    """Stage 1: высокоточная кластеризация с сохранением пользовательской разметки и прототипным доназначением."""
    cfg = config or ClusterConfig()
    all_faces = state.list_faces_with_media(include_rejected=False)
    if not all_faces:
        return {
            "clusters_created": 0,
            "seed_faces": 0,
            "matched_existing": 0,
            "reassigned_low_quality": 0,
            "unassigned_faces": 0,
        }

    all_persons = state.list_persons(include_ignored=True)
    person_constraints = state.get_cannot_link_person_pairs()
    constrained_person_ids: set[int] = set()
    for pa, pb in person_constraints:
        constrained_person_ids.add(pa)
        constrained_person_ids.add(pb)

    # Определяем профили, которые нельзя трогать:
    # именованные, игнорируемые, имеющие пользовательские лица или персональные ограничения
    preserved_person_ids: set[int] = set()
    named_active_person_ids: set[int] = set()
    for p in all_persons:
        pid = int(p["id"])
        is_named = int(p.get("is_named", 0)) != 0
        is_ignored = int(p.get("is_ignored", 0)) != 0
        if is_named or is_ignored or pid in constrained_person_ids:
            preserved_person_ids.add(pid)
        if is_named and not is_ignored:
            named_active_person_ids.add(pid)

    for face in all_faces:
        fpid = face.get("person_id")
        if fpid is not None and str(face.get("assign_source", "")) == "user":
            preserved_person_ids.add(int(fpid))

    # Сбрасываем только чисто автоматические безымянные кластеры без правок пользователя
    reset_updates: list[tuple[int, int | None, int | None, float, str]] = []
    obsolete_person_ids: set[int] = set()
    for p in all_persons:
        pid = int(p["id"])
        if pid not in preserved_person_ids:
            obsolete_person_ids.add(pid)

    for face in all_faces:
        fpid = face.get("person_id")
        if fpid is not None and int(fpid) in obsolete_person_ids:
            reset_updates.append((int(face["id"]), None, None, 1.0, "cluster"))
            face["person_id"] = None
            face["cluster_id"] = None
            face["assign_source"] = "cluster"

    if reset_updates:
        state.set_face_assignments(reset_updates)
    for pid in sorted(obsolete_person_ids):
        state.delete_person(pid)
    if obsolete_person_ids:
        state.mark_all_history_undone()

    cl_face_pairs = state.get_cannot_link_face_pairs()
    cl_adj = _build_cl_adjacency(cl_face_pairs)

    face_to_person: dict[int, int] = {}
    frame_persons: dict[tuple[int, int], set[int]] = {}
    unassigned_seed_faces: list[dict[str, Any]] = []

    for face in all_faces:
        fid = int(face["id"])
        fpid = face.get("person_id")
        frame_key = (int(face["media_id"]), int(face.get("frame_ms", 0)))
        if fpid is not None:
            pid_int = int(fpid)
            face_to_person[fid] = pid_int
            frame_persons.setdefault(frame_key, set()).add(pid_int)
        elif float(face.get("quality_score", 0.0)) >= cfg.min_seed_quality:
            unassigned_seed_faces.append(face)

    total_seed_faces = len(unassigned_seed_faces)

    # Шаг 1: высокоточное присоединение новых качественных лиц к уже именованным людям (>= 0.58)
    matched_existing = 0
    remaining_seed_faces = unassigned_seed_faces
    if named_active_person_ids and unassigned_seed_faces:
        for pid in sorted(named_active_person_ids):
            rebuild_person_prototypes(state, pid)
        named_protos = [
            p
            for p in state.list_prototypes()
            if int(p["person_id"]) in named_active_person_ids
        ]
        if named_protos:
            proto_mat = np.stack(
                [decode_embedding(p["centroid"]) for p in named_protos], axis=0
            ).astype(np.float32, copy=False)
            proto_pids = np.asarray(
                [int(p["person_id"]) for p in named_protos], dtype=np.int64
            )
            seed_mat = np.stack(
                [decode_embedding(f["embedding"]) for f in unassigned_seed_faces], axis=0
            ).astype(np.float32, copy=False)
            sims = seed_mat @ proto_mat.T

            # Собираем кандидатов >= 0.58 и применяем от большего сходства к меньшему
            candidates: list[tuple[float, int, list[tuple[float, int]]]] = []
            for idx in range(len(unassigned_seed_faces)):
                row = sims[idx]
                valid_cols = np.flatnonzero(row >= EXISTING_NAMED_MATCH_THRESHOLD)
                if valid_cols.size == 0:
                    continue
                best_per_pid: dict[int, float] = {}
                for col in valid_cols:
                    pid = int(proto_pids[col])
                    val = float(row[col])
                    if val > best_per_pid.get(pid, -2.0):
                        best_per_pid[pid] = val
                ranked = sorted(
                    ((val, pid) for pid, val in best_per_pid.items()),
                    key=lambda item: (-item[0], item[1]),
                )
                candidates.append((ranked[0][0], idx, ranked))

            candidates.sort(key=lambda item: (-item[0], item[1]))
            matched_indices: set[int] = set()
            existing_updates: list[tuple[int, int | None, int | None, float, str]] = []

            for _top_val, idx, ranked in candidates:
                face = unassigned_seed_faces[idx]
                fid = int(face["id"])
                frame_key = (int(face["media_id"]), int(face.get("frame_ms", 0)))
                for sim_val, pid in ranked:
                    if pid in frame_persons.get(frame_key, ()):
                        continue
                    if any(
                        face_to_person.get(other_fid) == pid
                        for other_fid in cl_adj.get(fid, ())
                    ):
                        continue
                    conf = float(min(1.0, max(0.0, sim_val)))
                    existing_updates.append((fid, pid, pid, conf, "cluster"))
                    face_to_person[fid] = pid
                    frame_persons.setdefault(frame_key, set()).add(pid)
                    matched_indices.add(idx)
                    break

            if existing_updates:
                state.set_face_assignments(existing_updates)
                matched_existing = len(existing_updates)
            remaining_seed_faces = [
                f for idx, f in enumerate(unassigned_seed_faces) if idx not in matched_indices
            ]

    # Шаг 2: Constrained High-Precision Clustering на оставшихся качественных лицах
    clusters_created = 0
    if remaining_seed_faces:
        embeddings = np.stack(
            [decode_embedding(f["embedding"]) for f in remaining_seed_faces], axis=0
        ).astype(np.float32, copy=False)
        face_ids = [int(f["id"]) for f in remaining_seed_faces]
        frame_keys = [
            (int(f["media_id"]), int(f.get("frame_ms", 0))) for f in remaining_seed_faces
        ]

        clusters = _constrained_agglomerative_cluster(
            embeddings,
            face_ids,
            frame_keys,
            cl_face_pairs,
            cfg,
        )

        cluster_updates: list[tuple[int, int | None, int | None, float, str]] = []
        for comp in clusters:
            comp_faces = [remaining_seed_faces[i] for i in comp]
            best_face = max(
                comp_faces,
                key=lambda f: (
                    float(f.get("quality_score", 0.0)),
                    float(f.get("det_score", 0.0)),
                    -int(f["id"]),
                ),
            )
            pid = state.create_person(
                name="",
                is_named=False,
                cover_face_id=int(best_face["id"]),
            )
            clusters_created += 1

            comp_embs = embeddings[comp]
            centroid = comp_embs.mean(axis=0)
            norm = float(np.linalg.norm(centroid))
            if norm > 1e-12:
                centroid = centroid / norm
                confs = comp_embs @ centroid
            else:
                confs = np.ones(len(comp), dtype=np.float32)

            for local_idx, conf_val in zip(comp, confs, strict=True):
                fid = face_ids[local_idx]
                conf = 1.0 if len(comp) == 1 else float(min(1.0, max(0.0, conf_val)))
                cluster_updates.append((fid, pid, pid, conf, "cluster"))

        if cluster_updates:
            state.set_face_assignments(cluster_updates)

    # Шаг 3: перестраиваем возрастные прототипы и прикрепляем размытые / неразмеченные лица
    rebuild_person_prototypes(state)
    reassigned_low_quality = assign_unassigned_faces(state)

    final_faces = state.list_faces_with_media(include_rejected=False)
    unassigned_count = sum(1 for f in final_faces if f.get("person_id") is None)

    return {
        "clusters_created": clusters_created,
        "seed_faces": total_seed_faces,
        "matched_existing": matched_existing,
        "reassigned_low_quality": reassigned_low_quality,
        "unassigned_faces": unassigned_count,
    }
