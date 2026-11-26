"""Stage 2: Поиск возрастных пар (Child-Aging Temporal-Bridge + Family Co-occurrence Ranker)."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from sorter.clustering.prototypes import year_from_ts
from sorter.state import SorterState, decode_embedding

log = logging.getLogger("photo_sorter.clustering.age_bridge")

SECONDS_PER_YEAR = 365.2425 * 86_400.0
BOUNDARY_WINDOW_SIZE = 12
TOP_PAIRWISE_K = 5
QUALITY_FLOOR = 0.35
ANCHOR_MIN_MEDIA = 5
COOCCURRENCE_MIN_BOUNDARY_SIM = 0.25
HARD_STRANGER_BOUNDARY_FLOOR = 0.27
STRONG_CO_BOUNDARY_FLOOR = 0.24
STRONG_CO_MIN_COUNT = 3
GENDER_CONSISTENCY_MIN_RATIO = 0.80
PREFILTER_PERSON_THRESHOLD = 120
PREFILTER_CENTROID_SIM = 0.08


@dataclass
class AgeBridgeCandidate:
    """Кандидат на объединение двух возрастных эпох одного человека."""

    person_a_id: int
    person_b_id: int
    name_a: str
    name_b: str
    score: float
    boundary_sim: float
    cooccurrence_count: int
    shared_family_names: list[str]
    year_span_a: tuple[int | None, int | None]
    year_span_b: tuple[int | None, int | None]
    age_progression_bonus: float
    reason_ru: str
    sample_faces_a: list[int]
    sample_faces_b: list[int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "person_a_id": int(self.person_a_id),
            "person_b_id": int(self.person_b_id),
            "name_a": self.name_a,
            "name_b": self.name_b,
            "score": round(float(self.score), 4),
            "boundary_sim": round(float(self.boundary_sim), 4),
            "cooccurrence_count": int(self.cooccurrence_count),
            "shared_family_names": list(self.shared_family_names),
            "year_span_a": [self.year_span_a[0], self.year_span_a[1]],
            "year_span_b": [self.year_span_b[0], self.year_span_b[1]],
            "age_progression_bonus": round(float(self.age_progression_bonus), 4),
            "reason_ru": self.reason_ru,
            "sample_faces_a": list(self.sample_faces_a),
            "sample_faces_b": list(self.sample_faces_b),
        }


@dataclass
class _PersonProfile:
    person_id: int
    name: str
    is_named: bool
    birth_year: int | None
    all_faces: list[dict[str, Any]]
    q_faces: list[dict[str, Any]]
    q_embs: np.ndarray  # (K, D) в хронологическом порядке q_faces
    q_weights: np.ndarray  # (K,)
    q_ts: list[float | None]
    dated_indices: list[int]
    min_ts: float | None
    med_ts: float | None
    max_ts: float | None
    year_span: tuple[int | None, int | None]
    media_ids: set[int]
    confident_gender: int | None
    head_centroid: np.ndarray
    tail_centroid: np.ndarray
    overall_centroid: np.ndarray


def _weighted_unit_centroid(embs: np.ndarray, weights: np.ndarray) -> np.ndarray:
    w = np.maximum(0.05, weights.astype(np.float32, copy=False))
    cent = (embs * w[:, None]).sum(axis=0)
    norm = float(np.linalg.norm(cent))
    if norm > 1e-12:
        return (cent / norm).astype(np.float32, copy=False)
    best = int(np.argmax(w))
    return embs[best].copy()


def _detect_confident_gender(faces: list[dict[str, Any]]) -> int | None:
    genders = [int(f["est_gender"]) for f in faces if f.get("est_gender") is not None]
    if len(genders) < 2:
        return None
    zeros = sum(1 for g in genders if g == 0)
    ones = len(genders) - zeros
    total = len(genders)
    if zeros / total >= GENDER_CONSISTENCY_MIN_RATIO:
        return 0
    if ones / total >= GENDER_CONSISTENCY_MIN_RATIO:
        return 1
    return None


def _build_person_profile(
    person: dict[str, Any], faces: list[dict[str, Any]]
) -> _PersonProfile:
    preferred = [
        f for f in faces if float(f.get("quality_score", 0.0)) >= QUALITY_FLOOR
    ]
    q_faces = preferred if preferred else list(faces)
    q_faces.sort(
        key=lambda f: (
            0 if f.get("shot_ts") is not None else 1,
            float(f["shot_ts"]) if f.get("shot_ts") is not None else 0.0,
            int(f["id"]),
        )
    )

    q_embs = np.stack(
        [decode_embedding(f["embedding"]) for f in q_faces], axis=0
    ).astype(np.float32, copy=False)
    q_weights = np.asarray(
        [float(f.get("quality_score", 0.0)) for f in q_faces], dtype=np.float32
    )
    q_ts = [
        float(f["shot_ts"]) if f.get("shot_ts") is not None else None for f in q_faces
    ]
    dated_indices = [i for i, ts in enumerate(q_ts) if ts is not None]

    all_dated_ts = sorted(
        float(f["shot_ts"]) for f in faces if f.get("shot_ts") is not None
    )
    if all_dated_ts:
        min_ts: float | None = all_dated_ts[0]
        max_ts: float | None = all_dated_ts[-1]
        med_ts: float | None = float(np.median(all_dated_ts))
        y_min = year_from_ts(min_ts)
        y_max = year_from_ts(max_ts)
        year_span: tuple[int | None, int | None] = (
            y_min if y_min > 0 else None,
            y_max if y_max > 0 else None,
        )
    else:
        min_ts = None
        max_ts = None
        med_ts = None
        year_span = (None, None)

    n_win = min(BOUNDARY_WINDOW_SIZE, len(q_faces))
    if dated_indices:
        head_idx = dated_indices[:n_win]
        tail_idx = dated_indices[-n_win:]
    else:
        top_q = np.argsort(-q_weights)[:n_win].tolist()
        head_idx = top_q
        tail_idx = top_q

    head_centroid = _weighted_unit_centroid(q_embs[head_idx], q_weights[head_idx])
    tail_centroid = _weighted_unit_centroid(q_embs[tail_idx], q_weights[tail_idx])
    overall_centroid = _weighted_unit_centroid(q_embs, q_weights)

    media_ids = {int(f["media_id"]) for f in faces}
    gender = _detect_confident_gender(q_faces)

    return _PersonProfile(
        person_id=int(person["id"]),
        name=str(person.get("name", "") or "").strip(),
        is_named=int(person.get("is_named", 0)) != 0,
        birth_year=(
            int(person["birth_year"]) if person.get("birth_year") is not None else None
        ),
        all_faces=faces,
        q_faces=q_faces,
        q_embs=q_embs,
        q_weights=q_weights,
        q_ts=q_ts,
        dated_indices=dated_indices,
        min_ts=min_ts,
        med_ts=med_ts,
        max_ts=max_ts,
        year_span=year_span,
        media_ids=media_ids,
        confident_gender=gender,
        head_centroid=head_centroid,
        tail_centroid=tail_centroid,
        overall_centroid=overall_centroid,
    )


def _select_boundary_indices(
    earlier: _PersonProfile, later: _PersonProfile
) -> tuple[list[int], list[int]]:
    """Выбирает до 12 граничных качественных лиц каждой стороны (новейшие у earlier, старейшие у later)."""
    n_e = min(BOUNDARY_WINDOW_SIZE, len(earlier.q_faces))
    n_l = min(BOUNDARY_WINDOW_SIZE, len(later.q_faces))

    if not earlier.dated_indices or not later.dated_indices:
        idx_e = np.argsort(-earlier.q_weights)[:n_e].tolist()
        idx_l = np.argsort(-later.q_weights)[:n_l].tolist()
        return idx_e, idx_l

    assert earlier.max_ts is not None and later.min_ts is not None
    assert earlier.min_ts is not None and later.max_ts is not None

    # Если интервалы последовательны (или почти не пересекаются), берём хвост earlier и начало later.
    # Если один кластер вложен во временной интервал другого, берём ближайшие по времени к границе перекрытия.
    if earlier.max_ts <= later.min_ts or later.max_ts > earlier.max_ts:
        idx_e = earlier.dated_indices[-n_e:]
        idx_l = later.dated_indices[:n_l]
        return idx_e, idx_l

    # Случай вложенного интервала (например, earlier = 2018..2026, later = 2021..2022)
    l_min, l_max = later.min_ts, later.max_ts

    def dist_to_later(i: int) -> tuple[float, float]:
        ts = earlier.q_ts[i]
        assert ts is not None
        if ts < l_min:
            d = l_min - ts
        elif ts > l_max:
            d = ts - l_max
        else:
            d = 0.0
        return (d, -float(earlier.q_weights[i]))

    idx_e = sorted(earlier.dated_indices, key=dist_to_later)[:n_e]
    idx_l = later.dated_indices[:n_l]
    return idx_e, idx_l


def _compute_boundary_sim(
    earlier: _PersonProfile,
    later: _PersonProfile,
    idx_e: list[int],
    idx_l: list[int],
) -> float:
    embs_e = earlier.q_embs[idx_e]
    embs_l = later.q_embs[idx_l]
    cent_e = _weighted_unit_centroid(embs_e, earlier.q_weights[idx_e])
    cent_l = _weighted_unit_centroid(embs_l, later.q_weights[idx_l])
    cent_cos = float(np.dot(cent_e, cent_l))

    pair_sims = (embs_e @ embs_l.T).reshape(-1)
    k = min(TOP_PAIRWISE_K, int(pair_sims.size))
    if k == pair_sims.size:
        top_mean = float(pair_sims.mean())
    else:
        top_vals = np.partition(pair_sims, -k)[-k:]
        top_mean = float(top_vals.mean())

    return 0.6 * cent_cos + 0.4 * top_mean


def _mean_age_for_indices(profile: _PersonProfile, indices: list[int]) -> float | None:
    ages = [
        float(profile.q_faces[i]["est_age"])
        for i in indices
        if profile.q_faces[i].get("est_age") is not None
    ]
    if not ages:
        ages = [
            float(f["est_age"])
            for f in profile.q_faces
            if f.get("est_age") is not None
        ]
    return float(sum(ages) / len(ages)) if ages else None


def _compute_age_progression_bonus(
    earlier: _PersonProfile,
    later: _PersonProfile,
    idx_e: list[int],
    idx_l: list[int],
    max_pair_gap_years: float,
) -> float:
    """Оценивает хронологическую последовательность и согласованный рост возраста (особенно для детей <= 18)."""
    if (
        earlier.max_ts is None
        or earlier.med_ts is None
        or later.min_ts is None
        or later.med_ts is None
    ):
        return 0.0

    gap_years = (later.min_ts - earlier.max_ts) / SECONDS_PER_YEAR
    if gap_years < -1.5 or gap_years > max_pair_gap_years:
        return 0.0

    age_e = _mean_age_for_indices(earlier, idx_e)
    age_l = _mean_age_for_indices(later, idx_l)
    if age_e is None or age_l is None:
        # Небольшой бонус за последовательность эпох без явного разрыва даже без est_age
        return 0.02 if -0.5 <= gap_years <= max_pair_gap_years else 0.0

    ts_e = [float(t) for i in idx_e if (t := earlier.q_ts[i]) is not None]
    ts_l = [float(t) for i in idx_l if (t := later.q_ts[i]) is not None]
    mean_ts_e = (sum(ts_e) / len(ts_e)) if ts_e else earlier.med_ts
    mean_ts_l = (sum(ts_l) / len(ts_l)) if ts_l else later.med_ts
    delta_years = max(0.0, (mean_ts_l - mean_ts_e) / SECONDS_PER_YEAR)
    age_diff = age_l - age_e

    # Явное противоречие: более поздние фото выглядят существенно моложе
    if delta_years >= 2.0 and age_diff < -3.0:
        return -0.05

    err = abs(age_diff - delta_years)
    tolerance = max(2.5, 0.6 * delta_years + 1.5)
    if age_diff < -1.0 or err > tolerance:
        return 0.0

    consistency = max(0.0, 1.0 - err / max(4.0, tolerance))
    bonus = 0.05 * consistency

    # Усиленный бонус для растущих детей (est_age <= 18) с монотонным ростом возраста
    is_child_growth = age_e <= 18.0 and age_l <= 20.0
    if is_child_growth and delta_years >= 0.3 and age_diff >= -0.2:
        bonus += 0.06 * consistency
    return float(bonus)


def _pick_sample_faces(
    profile: _PersonProfile, boundary_indices: list[int], limit: int = 4
) -> list[int]:
    """Выбирает до 4 лучших по качеству лиц, включая лица на временной границе."""
    chosen: list[int] = []
    seen: set[int] = set()

    boundary_sorted = sorted(
        (profile.q_faces[i] for i in boundary_indices),
        key=lambda f: (-float(f.get("quality_score", 0.0)), int(f["id"])),
    )
    for f in boundary_sorted[:2]:
        fid = int(f["id"])
        if fid not in seen:
            chosen.append(fid)
            seen.add(fid)

    overall_sorted = sorted(
        profile.all_faces,
        key=lambda f: (-float(f.get("quality_score", 0.0)), int(f["id"])),
    )
    for f in overall_sorted:
        if len(chosen) >= limit:
            break
        fid = int(f["id"])
        if fid not in seen:
            chosen.append(fid)
            seen.add(fid)

    return chosen[:limit]


def _format_span(span: tuple[int | None, int | None]) -> str:
    y1, y2 = span
    if y1 is None and y2 is None:
        return "без даты"
    if y1 == y2 or y2 is None:
        return str(y1)
    if y1 is None:
        return str(y2)
    return f"{y1}–{y2}"


def _build_reason_ru(
    *,
    boundary_sim: float,
    span_a: tuple[int | None, int | None],
    span_b: tuple[int | None, int | None],
    shared_co: int,
    shared_names: list[str],
    age_bonus: float,
) -> str:
    pct = int(round(max(0.0, min(1.0, boundary_sim)) * 100))
    parts = [
        f"Сходство на границе эпох {pct}% ({_format_span(span_a)} и {_format_span(span_b)})"
    ]
    if shared_co > 0:
        if shared_names:
            names_txt = ", ".join(shared_names[:3])
            parts.append(f"общие фото с семьёй ({names_txt}): {shared_co}")
        else:
            parts.append(f"общие семейные фото: {shared_co}")
    if age_bonus >= 0.03:
        parts.append("возраст согласован с датами съёмки")
    return "; ".join(parts)


def find_age_bridge_candidates(
    state: SorterState,
    *,
    top_k: int = 25,
    min_score: float = 0.36,
    max_pair_gap_years: float = 4.5,
) -> list[AgeBridgeCandidate]:
    """Stage 2: ранжирует пары кластеров, вероятно принадлежащих одному человеку (включая взрослеющих детей)."""
    persons = state.list_persons(include_ignored=False)
    if len(persons) < 2:
        return []

    person_by_id = {int(p["id"]): p for p in persons}
    all_faces = state.list_faces_with_media(include_rejected=False)

    faces_by_person: dict[int, list[dict[str, Any]]] = {}
    face_to_person: dict[int, int] = {}
    frame_to_persons: dict[tuple[int, int], set[int]] = {}

    for face in all_faces:
        fpid = face.get("person_id")
        if fpid is None:
            continue
        pid = int(fpid)
        if pid not in person_by_id:
            continue
        faces_by_person.setdefault(pid, []).append(face)
        face_to_person[int(face["id"])] = pid
        frame_key = (int(face["media_id"]), int(face.get("frame_ms", 0)))
        frame_to_persons.setdefault(frame_key, set()).add(pid)

    profiles: list[_PersonProfile] = []
    for pid, p_faces in sorted(faces_by_person.items()):
        if p_faces:
            profiles.append(_build_person_profile(person_by_id[pid], p_faces))

    if len(profiles) < 2:
        return []

    # Собираем ВСЕ запрещённые пары людей (person-level cannot_link + face-level cannot_link + один кадр)
    forbidden_pairs: set[tuple[int, int]] = set(state.get_cannot_link_person_pairs())
    for fa, fb in state.get_cannot_link_face_pairs():
        pa = face_to_person.get(fa)
        pb = face_to_person.get(fb)
        if pa is not None and pb is not None and pa != pb:
            forbidden_pairs.add((pa, pb) if pa < pb else (pb, pa))
    for pids_in_frame in frame_to_persons.values():
        if len(pids_in_frame) < 2:
            continue
        ordered_pids = sorted(pids_in_frame)
        for i in range(len(ordered_pids)):
            for j in range(i + 1, len(ordered_pids)):
                forbidden_pairs.add((ordered_pids[i], ordered_pids[j]))

    # Якорные члены семьи для контекста совместного появления (>= 5 медиафайлов, приоритет именованным)
    anchors = [p for p in profiles if len(p.media_ids) >= ANCHOR_MIN_MEDIA]
    anchors.sort(key=lambda p: (not p.is_named, -len(p.media_ids), p.person_id))

    # Быстрый матричный префильтр для очень больших архивов (> 120 профилей)
    n_prof = len(profiles)
    use_prefilter = n_prof > PREFILTER_PERSON_THRESHOLD
    prefilter_ok: np.ndarray | None = None
    if use_prefilter:
        heads = np.stack([p.head_centroid for p in profiles], axis=0)
        tails = np.stack([p.tail_centroid for p in profiles], axis=0)
        overalls = np.stack([p.overall_centroid for p in profiles], axis=0)
        max_sim = np.maximum(
            np.maximum(tails @ heads.T, heads @ tails.T),
            overalls @ overalls.T,
        )
        prefilter_ok = max_sim >= PREFILTER_CENTROID_SIM

    candidates: list[AgeBridgeCandidate] = []

    for i in range(n_prof):
        prof_a = profiles[i]
        for j in range(i + 1, n_prof):
            prof_b = profiles[j]
            if prefilter_ok is not None and not bool(prefilter_ok[i, j]):
                continue

            pair_key = (
                (prof_a.person_id, prof_b.person_id)
                if prof_a.person_id < prof_b.person_id
                else (prof_b.person_id, prof_a.person_id)
            )
            if pair_key in forbidden_pairs:
                continue

            # Не предлагаем объединять двух по-разному названных пользователем людей
            if (
                prof_a.is_named
                and prof_b.is_named
                and prof_a.name
                and prof_b.name
                and prof_a.name.casefold() != prof_b.name.casefold()
            ):
                continue

            # Хронологический порядок (earlier, later) для граничного окна
            if prof_a.med_ts is not None and prof_b.med_ts is not None:
                key_a = (prof_a.med_ts, prof_a.max_ts or prof_a.med_ts, prof_a.person_id)
                key_b = (prof_b.med_ts, prof_b.max_ts or prof_b.med_ts, prof_b.person_id)
                earlier, later = (prof_a, prof_b) if key_a <= key_b else (prof_b, prof_a)
            else:
                earlier, later = (
                    (prof_a, prof_b) if prof_a.person_id < prof_b.person_id else (prof_b, prof_a)
                )

            idx_e, idx_l = _select_boundary_indices(earlier, later)
            boundary_sim = _compute_boundary_sim(earlier, later, idx_e, idx_l)

            # Жёсткий порог защиты от посторонних (ниже 0.24 отклоняем всегда)
            if boundary_sim < STRONG_CO_BOUNDARY_FLOOR:
                continue

            # Контекст совместных фото с якорными членами семьи
            shared_co = 0
            shared_anchors: list[tuple[bool, int, str]] = []
            for anc in anchors:
                if anc.person_id == prof_a.person_id or anc.person_id == prof_b.person_id:
                    continue
                co_a = len(prof_a.media_ids & anc.media_ids)
                if co_a == 0:
                    continue
                co_b = len(prof_b.media_ids & anc.media_ids)
                if co_b == 0:
                    continue
                shared = min(co_a, co_b)
                shared_co += shared
                label = anc.name if anc.name else f"Человек #{anc.person_id}"
                shared_anchors.append((anc.is_named and bool(anc.name), shared, label))

            # Жёсткий анти-странджер: 0.24..0.27 допускается только при сильном семейном контексте
            if boundary_sim < HARD_STRANGER_BOUNDARY_FLOOR and shared_co < STRONG_CO_MIN_COUNT:
                continue

            if boundary_sim >= COOCCURRENCE_MIN_BOUNDARY_SIM and shared_co > 0:
                co_bonus = min(0.14, 0.025 * math.log1p(shared_co) * 2.5)
            else:
                co_bonus = 0.0

            age_bonus = _compute_age_progression_bonus(
                earlier, later, idx_e, idx_l, max_pair_gap_years
            )

            gender_penalty = 0.0
            if (
                prof_a.confident_gender is not None
                and prof_b.confident_gender is not None
                and prof_a.confident_gender != prof_b.confident_gender
            ):
                gender_penalty = -0.14

            score = float(
                max(0.0, min(1.0, boundary_sim + co_bonus + age_bonus + gender_penalty))
            )
            if score < min_score:
                continue

            shared_anchors.sort(key=lambda x: (not x[0], -x[1], x[2]))
            shared_names = [item[2] for item in shared_anchors]

            # Порядок отображения: именованный (или более крупный / более ранний) профиль идёт первым как person_a
            prefer_a = (
                (
                    prof_a.is_named,
                    len(prof_a.all_faces),
                    -(prof_a.med_ts or 0.0),
                    -prof_a.person_id,
                )
                >= (
                    prof_b.is_named,
                    len(prof_b.all_faces),
                    -(prof_b.med_ts or 0.0),
                    -prof_b.person_id,
                )
            )
            if prefer_a:
                first, second = prof_a, prof_b
                first_idx = idx_e if prof_a.person_id == earlier.person_id else idx_l
                second_idx = idx_l if prof_b.person_id == later.person_id else idx_e
            else:
                first, second = prof_b, prof_a
                first_idx = idx_e if prof_b.person_id == earlier.person_id else idx_l
                second_idx = idx_l if prof_a.person_id == later.person_id else idx_e

            reason_ru = _build_reason_ru(
                boundary_sim=boundary_sim,
                span_a=first.year_span,
                span_b=second.year_span,
                shared_co=shared_co,
                shared_names=shared_names,
                age_bonus=age_bonus,
            )

            candidates.append(
                AgeBridgeCandidate(
                    person_a_id=first.person_id,
                    person_b_id=second.person_id,
                    name_a=first.name,
                    name_b=second.name,
                    score=score,
                    boundary_sim=boundary_sim,
                    cooccurrence_count=shared_co,
                    shared_family_names=shared_names,
                    year_span_a=first.year_span,
                    year_span_b=second.year_span,
                    age_progression_bonus=age_bonus,
                    reason_ru=reason_ru,
                    sample_faces_a=_pick_sample_faces(first, first_idx),
                    sample_faces_b=_pick_sample_faces(second, second_idx),
                )
            )

    candidates.sort(
        key=lambda c: (
            -c.score,
            -c.boundary_sim,
            -c.cooccurrence_count,
            c.person_a_id,
            c.person_b_id,
        )
    )
    return candidates[: max(0, int(top_k))]
