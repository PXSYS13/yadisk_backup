"""Синтетический бенчмарк «Цифровая семья» (2 000 фото, 2018–2026) для проверки Stages 1–3."""

from __future__ import annotations

import argparse
import calendar
import json
import math
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from sorter.clustering.age_bridge import find_age_bridge_candidates
from sorter.clustering.engine import ClusterConfig, run_initial_clustering
from sorter.clustering.prototypes import assign_unassigned_faces, rebuild_person_prototypes
from sorter.clustering.studio import (
    merge_persons_action,
    reject_age_pair_action,
    rename_person_action,
)
from sorter.state import EMBEDDING_DIM, SorterState, encode_embedding
from sorter.utils import ensure_utf8_stdio

STRICT_THRESHOLDS: dict[str, tuple[str, float]] = {
    "cluster_purity": (">=", 0.98),
    "child_cross_age_recall_at_5": (">=", 0.90),
    "stranger_false_merge_rate": ("<=", 0.01),
    "cannot_link_violations": ("==", 0.0),
    "post_merge_child_recall": (">=", 0.90),
}


@dataclass(frozen=True)
class _SynthIdentity:
    key: str
    display_name: str
    birth_year: int
    gender: int  # 0 = жен, 1 = муж
    is_child: bool
    is_stranger: bool
    base_vec: np.ndarray
    drift_vec_1: np.ndarray
    drift_vec_2: np.ndarray


def _unit_vec(rng: np.random.Generator, dim: int = EMBEDDING_DIM) -> np.ndarray:
    v = rng.normal(0.0, 1.0, size=dim).astype(np.float32)
    return (v / float(np.linalg.norm(v))).astype(np.float32)


def _orthogonal_unit(
    rng: np.random.Generator, basis: list[np.ndarray], dim: int = EMBEDDING_DIM
) -> np.ndarray:
    v = rng.normal(0.0, 1.0, size=dim).astype(np.float32)
    for b in basis:
        v = (v - float(np.dot(v, b)) * b).astype(np.float32, copy=False)
    norm = float(np.linalg.norm(v))
    if norm <= 1e-8:
        return _orthogonal_unit(rng, basis, dim)
    return (v / norm).astype(np.float32)


def _year_frac_to_ts(year_frac: float) -> float:
    year = int(math.floor(year_frac))
    frac = max(0.0, min(0.999, year_frac - year))
    start_ts = float(calendar.timegm((year, 1, 1, 12, 0, 0, 0, 0, 0)))
    end_ts = float(calendar.timegm((year + 1, 1, 1, 12, 0, 0, 0, 0, 0)))
    return start_ts + frac * (end_ts - start_ts)


def _identity_vec_at_time(identity: _SynthIdentity, year_frac: float) -> np.ndarray:
    if identity.is_stranger:
        return identity.base_vec

    if not identity.is_child:
        # Взрослые: плавный слабый дрейф 0.012 рад/год относительно 2018 года
        dt = max(0.0, year_frac - 2018.0)
        theta = 0.012 * dt
        v = math.cos(theta) * identity.base_vec + math.sin(theta) * identity.drift_vec_1
        return (v / float(np.linalg.norm(v))).astype(np.float32)

    # Дети: нелинейное взросление (~быстрее в 0–3 года, плавнее к 6–8 годам),
    # прямой cosine 2018 vs 2025 ~0.30-0.40, соседние 1–2 года ~0.52-0.65
    age = max(0.0, year_frac - float(identity.birth_year))
    theta = 0.11 * age + 0.30 * (1.0 - math.exp(-age / 2.2))
    curve = 0.35 * math.sin(0.28 * age)
    drift_dir = math.cos(curve) * identity.drift_vec_1 + math.sin(curve) * identity.drift_vec_2
    v = math.cos(theta) * identity.base_vec + math.sin(theta) * drift_dir
    return (v / float(np.linalg.norm(v))).astype(np.float32)


def _sample_face_embedding(
    rng: np.random.Generator,
    clean_vec: np.ndarray,
    *,
    is_blurry: bool,
) -> tuple[np.ndarray, float, float]:
    """Генерирует зашумлённый 512-мерный эмбеддинг + (quality_score, det_score)."""
    noise = _unit_vec(rng)
    noise = noise - float(np.dot(noise, clean_vec)) * clean_vec
    noise = noise / float(np.linalg.norm(noise))

    if is_blurry:
        quality = float(rng.uniform(0.25, 0.42))
        det_score = float(rng.uniform(0.62, 0.80))
        # Размытое лицо: больше шума по ракурсу/движению, но сохраняет сходство с прототипом эпохи
        alpha = float(rng.uniform(0.65, 0.78))
    else:
        quality = float(rng.uniform(0.50, 0.94))
        det_score = float(rng.uniform(0.82, 0.99))
        alpha = float(rng.uniform(0.80, 0.88))

    beta = math.sqrt(max(0.0, 1.0 - alpha * alpha))
    emb = alpha * clean_vec + beta * noise
    emb = (emb / float(np.linalg.norm(emb))).astype(np.float32)
    return emb, quality, det_score


def _build_synthetic_identities(rng: np.random.Generator) -> dict[str, _SynthIdentity]:
    mama_base = _unit_vec(rng)
    papa_base = _orthogonal_unit(rng, [mama_base])
    zlata_base = _orthogonal_unit(rng, [mama_base, papa_base])
    # Небольшое семейное сходство брата и сестры (~0.14), чтобы проверить защиту cannot_link и пола
    timur_orth = _orthogonal_unit(rng, [mama_base, papa_base, zlata_base])
    timur_base = (0.14 * zlata_base + math.sqrt(1.0 - 0.14**2) * timur_orth).astype(np.float32)
    timur_base = (timur_base / float(np.linalg.norm(timur_base))).astype(np.float32)

    identities: dict[str, _SynthIdentity] = {}
    family_specs = [
        ("mama", "Мама", 1988, 0, False, mama_base),
        ("papa", "Папа", 1986, 1, False, papa_base),
        ("zlata", "Злата", 2018, 0, True, zlata_base),
        ("timur", "Тимур", 2020, 1, True, timur_base),
    ]
    for key, name, by, gender, is_child, base in family_specs:
        d1 = _orthogonal_unit(rng, [base])
        d2 = _orthogonal_unit(rng, [base, d1])
        identities[key] = _SynthIdentity(
            key=key,
            display_name=name,
            birth_year=by,
            gender=gender,
            is_child=is_child,
            is_stranger=False,
            base_vec=base,
            drift_vec_1=d1,
            drift_vec_2=d2,
        )

    for idx in range(1, 31):
        s_key = f"stranger_{idx:02d}"
        s_base = _unit_vec(rng)
        s_d1 = _orthogonal_unit(rng, [s_base])
        s_d2 = _orthogonal_unit(rng, [s_base, s_d1])
        identities[s_key] = _SynthIdentity(
            key=s_key,
            display_name=s_key,
            birth_year=int(rng.integers(1975, 2005)),
            gender=int(rng.integers(0, 2)),
            is_child=False,
            is_stranger=True,
            base_vec=s_base,
            drift_vec_1=s_d1,
            drift_vec_2=s_d2,
        )
    return identities


def _generate_photo_specs(
    rng: np.random.Generator, num_photos: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Генерирует 2 000 фото (2018–2025 основной архив + 2026 отложенная выборка)."""
    main_count = max(100, int(round(num_photos * 0.90)))
    heldout_count = max(20, num_photos - main_count)

    # Распределение 30 посторонних (по 1–4 фото каждому)
    stranger_appearances: list[tuple[str, bool]] = []
    for idx in range(1, 31):
        s_key = f"stranger_{idx:02d}"
        cnt = int(rng.integers(1, 5))
        for j in range(cnt):
            # Часть посторонних попадает на фон семейных праздников
            in_bg = j == 0 and (idx % 2 == 0)
            stranger_appearances.append((s_key, in_bg))
    rng.shuffle(stranger_appearances)

    main_specs: list[dict[str, Any]] = []
    stranger_ptr = 0
    for i in range(main_count):
        # Равномерное покрытие 2018.0 .. 2025.95
        year_frac = 2018.0 + (7.95 * (i + float(rng.uniform(0.0, 1.0))) / main_count)
        timur_born = year_frac >= 2020.05
        is_group = float(rng.random()) < 0.17

        # Слот для отдельного фото постороннего (около 3% архива)
        if (
            stranger_ptr < len(stranger_appearances)
            and not stranger_appearances[stranger_ptr][1]
            and float(rng.random()) < 0.045
        ):
            s_key, _ = stranger_appearances[stranger_ptr]
            stranger_ptr += 1
            people = [s_key]
        elif is_group:
            if timur_born:
                group_templates = [
                    ["mama", "zlata"],
                    ["papa", "zlata"],
                    ["mama", "timur"],
                    ["papa", "timur"],
                    ["zlata", "timur"],
                    ["mama", "zlata", "timur"],
                    ["papa", "zlata", "timur"],
                    ["mama", "papa", "zlata", "timur"],
                    ["mama", "papa"],
                ]
            else:
                group_templates = [
                    ["mama", "zlata"],
                    ["papa", "zlata"],
                    ["mama", "papa", "zlata"],
                    ["mama", "papa"],
                ]
            people = list(group_templates[int(rng.integers(0, len(group_templates)))])
            if (
                stranger_ptr < len(stranger_appearances)
                and stranger_appearances[stranger_ptr][1]
                and len(people) <= 3
            ):
                s_key, _ = stranger_appearances[stranger_ptr]
                stranger_ptr += 1
                people.append(s_key)
        else:
            if timur_born:
                choices = ["mama", "papa", "zlata", "timur"]
                probs = [0.22, 0.20, 0.30, 0.28]
            else:
                choices = ["mama", "papa", "zlata"]
                probs = [0.30, 0.25, 0.45]
            pick = str(rng.choice(choices, p=probs))
            people = [pick]

        main_specs.append({"year_frac": year_frac, "people": people, "heldout": False})

    # Гарантируем, что все оставшиеся запланированные появления посторонних попадут в основной архив
    while stranger_ptr < len(stranger_appearances):
        s_key, in_bg = stranger_appearances[stranger_ptr]
        stranger_ptr += 1
        target_idx = int(rng.integers(0, len(main_specs)))
        if in_bg and s_key not in main_specs[target_idx]["people"]:
            main_specs[target_idx]["people"].append(s_key)
        else:
            main_specs[target_idx]["people"] = [s_key]

    # Отложенная выборка 2026 года (новые фото семьи, пришедшие после обучения прототипов)
    heldout_specs: list[dict[str, Any]] = []
    for i in range(heldout_count):
        year_frac = 2026.02 + (0.90 * i / max(1, heldout_count))
        if float(rng.random()) < 0.18:
            group_templates_26 = [
                ["mama", "zlata"],
                ["papa", "timur"],
                ["zlata", "timur"],
                ["mama", "papa", "zlata", "timur"],
            ]
            people = list(group_templates_26[int(rng.integers(0, len(group_templates_26)))])
        else:
            pick = str(
                rng.choice(["mama", "papa", "zlata", "timur"], p=[0.20, 0.20, 0.30, 0.30])
            )
            people = [pick]
        heldout_specs.append({"year_frac": year_frac, "people": people, "heldout": True})

    return main_specs, heldout_specs


def _insert_photo_batch(
    state: SorterState,
    rng: np.random.Generator,
    identities: dict[str, _SynthIdentity],
    specs: list[dict[str, Any]],
    start_index: int,
    face_gt: dict[int, str],
) -> None:
    for offset, spec in enumerate(specs):
        idx = start_index + offset
        year_frac = float(spec["year_frac"])
        shot_ts = _year_frac_to_ts(year_frac)
        rel = f"archive/{int(year_frac)}/IMG_{idx:05d}.jpg"
        media_id, _ = state.upsert_media_file(
            local_path=f"/synthetic/{rel}",
            rel_path=rel,
            source_root="/synthetic",
            media_kind="photo",
            size=1_200_000,
            mtime_ns=int(shot_ts * 1_000_000_000),
            shot_ts=shot_ts,
            date_source="exif",
            width=4000,
            height=3000,
            scan_status="scanned",
        )
        face_rows: list[dict[str, Any]] = []
        gt_keys: list[str] = []
        for f_slot, person_key in enumerate(spec["people"]):
            ident = identities[person_key]
            # ~10% размытых/сложных лиц (у посторонних не делаем все лица размытыми, чтобы они кластеризовались)
            is_blurry = (not ident.is_stranger) and (float(rng.random()) < 0.10)
            clean_vec = _identity_vec_at_time(ident, year_frac)
            emb, quality, det_score = _sample_face_embedding(
                rng, clean_vec, is_blurry=is_blurry
            )
            true_age = max(0.3, year_frac - float(ident.birth_year))
            est_age = max(0.5, true_age + float(rng.normal(0.0, 1.1 if ident.is_child else 2.2)))
            est_gender = (
                ident.gender
                if float(rng.random()) < (0.90 if true_age >= 3.0 else 0.75)
                else (1 - ident.gender)
            )
            x0 = 100 + f_slot * 320
            face_rows.append(
                {
                    "frame_ms": 0,
                    "bbox": [x0, 100, x0 + 240, 340],
                    "kps": [[x0 + 60, 170], [x0 + 180, 170], [x0 + 120, 230]],
                    "det_score": det_score,
                    "quality_score": quality,
                    "est_age": round(est_age, 2),
                    "est_gender": est_gender,
                    "embedding": encode_embedding(emb),
                }
            )
            gt_keys.append(person_key)

        inserted_ids = state.replace_faces_for_media(media_id, face_rows)
        for fid, gt_key in zip(inserted_ids, gt_keys, strict=True):
            face_gt[fid] = gt_key


def _count_cannot_link_violations(state: SorterState) -> int:
    faces = state.list_faces(include_rejected=False)
    f_to_p = {int(f["id"]): f.get("person_id") for f in faces}
    violations = 0
    for fa, fb in state.get_cannot_link_face_pairs():
        pa = f_to_p.get(fa)
        pb = f_to_p.get(fb)
        if pa is not None and pb is not None and int(pa) == int(pb):
            violations += 1
    return violations


def _measure_purity_and_stranger_merge(
    state: SorterState,
    face_gt: dict[int, str],
    identities: dict[str, _SynthIdentity],
) -> tuple[float, float, dict[int, str]]:
    faces = state.list_faces(include_rejected=False)
    by_person: dict[int, list[str]] = {}
    for f in faces:
        pid = f.get("person_id")
        if pid is None:
            continue
        fid = int(f["id"])
        gt = face_gt.get(fid)
        if gt is not None:
            by_person.setdefault(int(pid), []).append(gt)

    total_assigned = 0
    total_majority = 0
    person_majority_gt: dict[int, str] = {}
    stranger_total = 0
    stranger_false_merged = 0

    for pid, gts in by_person.items():
        counts = Counter(gts)
        maj_gt, maj_cnt = counts.most_common(1)[0]
        person_majority_gt[pid] = maj_gt
        total_assigned += len(gts)
        total_majority += maj_cnt

        unique_identities = set(gts)
        for gt in gts:
            if identities[gt].is_stranger:
                stranger_total += 1
                # Ложное слияние постороннего: оказался в одном кластере с кем-то ещё
                if len(unique_identities) > 1:
                    stranger_false_merged += 1

    purity = float(total_majority / total_assigned) if total_assigned > 0 else 1.0
    stranger_rate = (
        float(stranger_false_merged / stranger_total) if stranger_total > 0 else 0.0
    )
    return purity, stranger_rate, person_majority_gt


def run_family_benchmark(
    *,
    strict: bool = False,
    seed: int = 42,
    num_photos: int = 2000,
) -> dict[str, Any]:
    """Запускает полный бенчмарк «Цифровая семья» (Stages 1–3) и возвращает словарь метрик."""
    t0 = time.perf_counter()
    rng = np.random.default_rng(seed)
    identities = _build_synthetic_identities(rng)
    main_specs, heldout_specs = _generate_photo_specs(rng, num_photos)

    with tempfile.TemporaryDirectory(prefix="family_bench_") as tmp_dir:
        db_path = Path(tmp_dir) / "benchmark.db"
        with SorterState(db_path) as state:
            face_gt: dict[int, str] = {}
            _insert_photo_batch(state, rng, identities, main_specs, 1, face_gt)

            # --- Stage 1: High-Precision Constrained Clustering ---
            stage1_stats = run_initial_clustering(state, ClusterConfig())
            cluster_purity, stage1_stranger_merge_rate, person_maj = (
                _measure_purity_and_stranger_merge(state, face_gt, identities)
            )
            stage1_cl_violations = _count_cannot_link_violations(state)

            # Симулируем разметку пользователя: называем крупнейший кластер каждого члена семьи
            persons = state.list_persons(include_ignored=False)
            clusters_by_gt: dict[str, list[dict[str, Any]]] = {}
            for p in persons:
                pid = int(p["id"])
                gt = person_maj.get(pid)
                if gt is not None and not identities[gt].is_stranger:
                    clusters_by_gt.setdefault(gt, []).append(p)

            named_primary_ids: dict[str, int] = {}
            for key in ("mama", "papa", "zlata", "timur"):
                group = clusters_by_gt.get(key, [])
                if not group:
                    continue
                group.sort(key=lambda p: (-int(p["face_count"]), int(p["id"])))
                primary = group[0]
                primary_pid = int(primary["id"])
                ident = identities[key]
                rename_person_action(
                    state, primary_pid, ident.display_name, birth_year=ident.birth_year
                )
                named_primary_ids[key] = primary_pid

            # Считаем вторичные возрастные кластеры детей (>= 2 лиц, чтобы отделить реальные эпохи)
            secondary_child_clusters: dict[int, str] = {}
            for child_key in ("zlata", "timur"):
                group = clusters_by_gt.get(child_key, [])
                child_primary_pid = named_primary_ids.get(child_key)
                for p in group:
                    pid = int(p["id"])
                    if pid != child_primary_pid:
                        secondary_child_clusters[pid] = child_key

            total_secondary_child_clusters = len(secondary_child_clusters)

            # --- Stage 2: Итеративный поиск возрастных мостов (Top-5) ---
            recalled_secondary_at_5 = 0
            total_top5_evaluated = 0
            stranger_false_suggestions = 0
            max_rounds = max(20, total_secondary_child_clusters * 3 + 5)

            for _round in range(max_rounds):
                cands = find_age_bridge_candidates(state, top_k=25, min_score=0.36)
                top5 = cands[:5]
                if not top5:
                    break

                acted = False
                for cand in top5:
                    total_top5_evaluated += 1
                    gt_a = person_maj.get(cand.person_a_id)
                    gt_b = person_maj.get(cand.person_b_id)
                    if (
                        gt_a is not None
                        and gt_a == gt_b
                        and gt_a in ("zlata", "timur", "mama", "papa")
                    ):
                        # Выбираем целью именованный (или более крупный) кластер
                        p_a = state.get_person(cand.person_a_id)
                        p_b = state.get_person(cand.person_b_id)
                        if p_a is None or p_b is None:
                            continue
                        if int(p_b.get("is_named", 0)) and not int(p_a.get("is_named", 0)):
                            target_id, source_id = cand.person_b_id, cand.person_a_id
                        else:
                            target_id, source_id = cand.person_a_id, cand.person_b_id

                        is_child_secondary = (
                            source_id in secondary_child_clusters
                            or target_id in secondary_child_clusters
                        )
                        merge_persons_action(state, target_id, source_id)
                        if is_child_secondary and gt_a in ("zlata", "timur"):
                            recalled_secondary_at_5 += 1
                            secondary_child_clusters.pop(source_id, None)
                            if target_id == named_primary_ids.get(gt_a):
                                secondary_child_clusters.pop(target_id, None)
                        acted = True
                        break
                    else:
                        if (
                            (gt_a is not None and identities[gt_a].is_stranger)
                            or (gt_b is not None and identities[gt_b].is_stranger)
                        ):
                            stranger_false_suggestions += 1
                        reject_age_pair_action(state, cand.person_a_id, cand.person_b_id)
                        acted = True
                        break

                if not acted:
                    break
                if not secondary_child_clusters:
                    # Все вторичные детские эпохи уже успешно склеены
                    break

            child_cross_age_recall_at_5 = (
                float(recalled_secondary_at_5 / total_secondary_child_clusters)
                if total_secondary_child_clusters > 0
                else 1.0
            )
            stranger_suggestion_fp_rate = (
                float(stranger_false_suggestions / total_top5_evaluated)
                if total_top5_evaluated > 0
                else 0.0
            )

            # --- Stage 3: Перестройка возрастных прототипов + отложенные фото 2026 года ---
            rebuild_person_prototypes(state)
            _insert_photo_batch(
                state,
                rng,
                identities,
                heldout_specs,
                start_index=len(main_specs) + 1,
                face_gt=face_gt,
            )
            reassigned_stage3 = assign_unassigned_faces(state)

            final_cl_violations = max(
                stage1_cl_violations, _count_cannot_link_violations(state)
            )
            _final_purity, final_stranger_merge_rate, _ = (
                _measure_purity_and_stranger_merge(state, face_gt, identities)
            )
            stranger_false_merge_rate = max(
                stage1_stranger_merge_rate, final_stranger_merge_rate
            )

            # Полнота распознавания детей после склейки и Stage 3 (включая размытые и 2026 год)
            all_final_faces = state.list_faces(include_rejected=False)
            child_total = 0
            child_correct = 0
            for f in all_final_faces:
                fid = int(f["id"])
                gt = face_gt.get(fid)
                if gt in ("zlata", "timur"):
                    child_total += 1
                    expected_pid = named_primary_ids.get(gt)
                    if expected_pid is not None and f.get("person_id") == expected_pid:
                        child_correct += 1

            post_merge_child_recall = (
                float(child_correct / child_total) if child_total > 0 else 0.0
            )

    elapsed_sec = time.perf_counter() - t0

    metrics: dict[str, Any] = {
        "seed": seed,
        "num_photos": len(main_specs) + len(heldout_specs),
        "main_photos_2018_2025": len(main_specs),
        "heldout_photos_2026": len(heldout_specs),
        "stage1_clusters_created": stage1_stats["clusters_created"],
        "secondary_child_eras_found": total_secondary_child_clusters,
        "secondary_child_eras_merged_top5": recalled_secondary_at_5,
        "stage3_reassigned_faces": reassigned_stage3,
        "cluster_purity": round(cluster_purity, 4),
        "child_cross_age_recall_at_5": round(child_cross_age_recall_at_5, 4),
        "stranger_false_merge_rate": round(stranger_false_merge_rate, 4),
        "stranger_suggestion_false_positive_rate": round(stranger_suggestion_fp_rate, 4),
        "cannot_link_violations": int(final_cl_violations),
        "post_merge_child_recall": round(post_merge_child_recall, 4),
        "elapsed_sec": round(elapsed_sec, 3),
    }

    checks: dict[str, bool] = {}
    for metric_name, (op, target) in STRICT_THRESHOLDS.items():
        val = float(metrics[metric_name])
        if op == ">=":
            checks[metric_name] = val >= target
        elif op == "<=":
            checks[metric_name] = val <= target
        elif op == "==":
            checks[metric_name] = val == target
        else:
            checks[metric_name] = False
    # Бенчмарк должен реально разбить взросление детей на >= 2 вторичные эпохи
    checks["child_eras_exercised"] = total_secondary_child_clusters >= 2

    passed = all(checks.values())
    metrics["checks"] = checks
    metrics["passed"] = passed

    if strict and not passed:
        failed = [k for k, ok in checks.items() if not ok]
        raise AssertionError(
            f"Бенчмарк «Цифровая семья» не прошёл строгие пороги: {failed}; метрики={metrics}"
        )

    return metrics


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Синтетический бенчмарк «Цифровая семья» (Stages 1–3)"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Завершить с кодом 1, если не выполнен хотя бы один целевой порог",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Вывести метрики в формате JSON",
    )
    parser.add_argument("--seed", type=int, default=42, help="Seed генератора (по умолчанию 42)")
    parser.add_argument(
        "--photos", type=int, default=2000, help="Число фото в архиве (по умолчанию 2000)"
    )
    args = parser.parse_args(argv)

    result = run_family_benchmark(strict=False, seed=args.seed, num_photos=args.photos)
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=== Бенчмарк «Цифровая семья» (2018–2026) ===")
        print(
            f"Фото: {result['num_photos']} "
            f"(2018–2025: {result['main_photos_2018_2025']}, 2026 held-out: {result['heldout_photos_2026']})"
        )
        print(f"Stage 1 кластеров: {result['stage1_clusters_created']}")
        print(
            f"Вторичных детских эпох: {result['secondary_child_eras_found']}, "
            f"склеено через Top-5: {result['secondary_child_eras_merged_top5']}"
        )
        print(f"cluster_purity:                  {result['cluster_purity']:.4f} (порог >= 0.98)")
        print(
            f"child_cross_age_recall_at_5:     {result['child_cross_age_recall_at_5']:.4f} (порог >= 0.90)"
        )
        print(
            f"stranger_false_merge_rate:       {result['stranger_false_merge_rate']:.4f} (порог <= 0.01)"
        )
        print(
            f"stranger_suggestion_fp_rate:     {result['stranger_suggestion_false_positive_rate']:.4f}"
        )
        print(
            f"cannot_link_violations:          {result['cannot_link_violations']} (порог == 0)"
        )
        print(
            f"post_merge_child_recall:         {result['post_merge_child_recall']:.4f} (порог >= 0.90)"
        )
        print(f"Время прогона:                   {result['elapsed_sec']:.3f} с")
        print(f"Итог:                            {'PASS' if result['passed'] else 'FAIL'}")

    if args.strict and not result["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
