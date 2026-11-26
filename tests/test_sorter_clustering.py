"""Юнит- и интеграционные тесты Stages 1–3: кластеризация, взросление детей, видео, Studio Undo и бенчмарк."""

from __future__ import annotations

import calendar
import math
from pathlib import Path

import numpy as np
import pytest

from evals.run_family_benchmark import run_family_benchmark
from sorter.clustering.age_bridge import find_age_bridge_candidates
from sorter.clustering.engine import ClusterConfig, run_initial_clustering
from sorter.clustering.prototypes import (
    assign_unassigned_faces,
    list_person_faces_outlier_first,
    rebuild_person_prototypes,
)
from sorter.clustering.studio import (
    ignore_person_action,
    merge_persons_action,
    reject_age_pair_action,
    reject_face_action,
    rename_person_action,
    split_faces_action,
    undo_last_action,
)
from sorter.state import EMBEDDING_DIM, SorterState, encode_embedding


def _ts(year: int, month: int = 6, day: int = 15) -> float:
    return float(calendar.timegm((year, month, day, 12, 0, 0, 0, 0, 0)))


def _unit(rng: np.random.Generator) -> np.ndarray:
    v = rng.normal(0.0, 1.0, size=EMBEDDING_DIM).astype(np.float32)
    return (v / float(np.linalg.norm(v))).astype(np.float32)


def _orth_unit(rng: np.random.Generator, base: np.ndarray) -> np.ndarray:
    v = _unit(rng)
    v = v - float(np.dot(v, base)) * base
    return (v / float(np.linalg.norm(v))).astype(np.float32)


def _perturb(rng: np.random.Generator, vec: np.ndarray, target_cos: float) -> np.ndarray:
    orth = _orth_unit(rng, vec)
    c = max(-1.0, min(1.0, float(target_cos)))
    s = math.sqrt(max(0.0, 1.0 - c * c))
    out = c * vec + s * orth
    return (out / float(np.linalg.norm(out))).astype(np.float32)


def _add_photo(
    state: SorterState,
    name: str,
    shot_ts: float,
    faces: list[dict[str, object]],
) -> tuple[int, list[int]]:
    mid, _ = state.upsert_media_file(
        local_path=f"/photos/{name}",
        rel_path=name,
        source_root="/photos",
        media_kind="photo",
        size=500_000,
        mtime_ns=int(shot_ts * 1_000_000_000),
        shot_ts=shot_ts,
        date_source="exif",
        scan_status="scanned",
    )
    fids = state.replace_faces_for_media(mid, faces)
    return mid, fids


def _face_dict(
    emb: np.ndarray,
    *,
    quality: float = 0.85,
    det: float = 0.95,
    est_age: float | None = None,
    est_gender: int | None = None,
    frame_ms: int = 0,
    slot: int = 0,
) -> dict[str, object]:
    x = 50 + slot * 200
    return {
        "frame_ms": frame_ms,
        "bbox": [x, 50, x + 150, 200],
        "kps": [[x + 40, 100], [x + 110, 100]],
        "det_score": det,
        "quality_score": quality,
        "est_age": est_age,
        "est_gender": est_gender,
        "embedding": encode_embedding(emb),
    }


def test_cannot_link_blocks_twins_stage1_and_stage2(tmp_path: Path) -> None:
    """Близнецы с почти идентичными векторами на одном фото не сливаются ни в Stage 1, ни в Stage 2."""
    rng = np.random.default_rng(101)
    twin_base = _unit(rng)

    with SorterState(tmp_path / "twins.db") as state:
        # Совместное фото близнецов (создаёт same_photo cannot_link)
        _, joint_fids = _add_photo(
            state,
            "joint.jpg",
            _ts(2022, 5, 1),
            [
                _face_dict(_perturb(rng, twin_base, 0.995), est_age=6.0, est_gender=0, slot=0),
                _face_dict(_perturb(rng, twin_base, 0.995), est_age=6.0, est_gender=0, slot=1),
            ],
        )
        # Ещё несколько сольных фото каждого близнеца
        for idx in range(4):
            _add_photo(
                state,
                f"solo_{idx}.jpg",
                _ts(2022, 6, idx + 1),
                [_face_dict(_perturb(rng, twin_base, 0.99), est_age=6.1, est_gender=0)],
            )

        stats = run_initial_clustering(state)
        assert stats["clusters_created"] >= 2

        f0 = state.get_face(joint_fids[0])
        f1 = state.get_face(joint_fids[1])
        assert f0 is not None and f1 is not None
        assert f0["person_id"] is not None and f1["person_id"] is not None
        assert f0["person_id"] != f1["person_id"]

        # В Stage 2 эти двое никогда не должны предлагаться к объединению
        cands = find_age_bridge_candidates(state, top_k=25, min_score=0.10)
        pair_set = {
            tuple(sorted((c.person_a_id, c.person_b_id))) for c in cands
        }
        assert tuple(sorted((int(f0["person_id"]), int(f1["person_id"])))) not in pair_set

        # И принудительно без force=True merge_persons_action тоже должен отказать
        with pytest.raises(ValueError, match="cannot_link"):
            merge_persons_action(state, int(f0["person_id"]), int(f1["person_id"]))


def test_child_aging_three_eras_bridge_prototypes_and_blurry_match(tmp_path: Path) -> None:
    """Три эпохи взросления ребёнка (2019, 2022, 2025): Stage 1 -> Stage 2 -> Stage 3."""
    rng = np.random.default_rng(202)
    mama_vec = _unit(rng)
    child_2019 = _orth_unit(rng, mama_vec)
    drift = _orth_unit(rng, child_2019)
    drift = drift - float(np.dot(drift, mama_vec)) * mama_vec
    drift = (drift / float(np.linalg.norm(drift))).astype(np.float32)

    # 3 эпохи с шагом ~0.92 рад между соседними (cos ~ 0.60 на границе, но 2019 vs 2025 cos < 0.20)
    def era_vec(angle: float) -> np.ndarray:
        v = math.cos(angle) * child_2019 + math.sin(angle) * drift
        return (v / float(np.linalg.norm(v))).astype(np.float32)

    v_2019 = era_vec(0.0)
    v_2022 = era_vec(0.90)  # cos(v_2019, v_2022) ~= 0.62
    v_2025 = era_vec(1.75)  # cos(v_2022, v_2025) ~= 0.66; cos(v_2019, v_2025) ~= -0.18

    with SorterState(tmp_path / "aging.db") as state:
        # Создаём по 6 качественных фото в каждой эпохе + общие фото с Мамой (семейный якорь)
        era_specs = [
            (2019, v_2019, 1.5),
            (2022, v_2022, 4.5),
            (2025, v_2025, 7.5),
        ]
        era_face_ids: dict[int, list[int]] = {2019: [], 2022: [], 2025: []}
        for year, vec, age in era_specs:
            for k in range(6):
                faces = [
                    _face_dict(_perturb(rng, vec, 0.90), est_age=age, est_gender=0, slot=0)
                ]
                # На половине фото рядом Мама (якорь >= 5 медиафайлов)
                if k < 3:
                    faces.append(
                        _face_dict(
                            _perturb(rng, mama_vec, 0.92), est_age=33.0, est_gender=0, slot=1
                        )
                    )
                _, fids = _add_photo(state, f"era_{year}_{k}.jpg", _ts(year, k + 1, 10), faces)
                era_face_ids[year].append(fids[0])

        # Stage 1: держит чистые эпохи раздельно (2019, 2022, 2025 не сливаются в одну кучу)
        run_initial_clustering(state, ClusterConfig())

        p_2019 = {state.get_face(fid)["person_id"] for fid in era_face_ids[2019]}  # type: ignore[index]
        p_2022 = {state.get_face(fid)["person_id"] for fid in era_face_ids[2022]}  # type: ignore[index]
        p_2025 = {state.get_face(fid)["person_id"] for fid in era_face_ids[2025]}  # type: ignore[index]
        assert len(p_2019) == 1 and len(p_2022) == 1 and len(p_2025) == 1
        pid_19 = int(next(iter(p_2019)))
        pid_22 = int(next(iter(p_2022)))
        pid_25 = int(next(iter(p_2025)))
        assert len({pid_19, pid_22, pid_25}) == 3

        # Назовём Маму (якорь) и стартовую эпоху ребёнка (2019)
        all_persons = state.list_persons(include_ignored=False)
        mama_pid = next(
            int(p["id"])
            for p in all_persons
            if int(p["id"]) not in {pid_19, pid_22, pid_25}
        )
        rename_person_action(state, mama_pid, "Мама", birth_year=1988)
        rename_person_action(state, pid_19, "Злата", birth_year=2018)

        # Stage 2: age_bridge находит связь 2019 <-> 2022 с семейным контекстом («Мама»)
        cands_1 = find_age_bridge_candidates(state, top_k=10, min_score=0.36)
        assert cands_1
        pair_19_22 = next(
            (
                c
                for c in cands_1
                if {c.person_a_id, c.person_b_id} == {pid_19, pid_22}
            ),
            None,
        )
        assert pair_19_22 is not None
        assert pair_19_22.cooccurrence_count >= 3
        assert "Мама" in pair_19_22.shared_family_names
        assert pair_19_22.age_progression_bonus > 0.0
        assert "Сходство" in pair_19_22.reason_ru
        # JSON-сериализация кандидата
        cand_dict = pair_19_22.to_dict()
        assert cand_dict["person_a_id"] == pair_19_22.person_a_id

        # Объединяем 2019 + 2022 -> теперь должна подняться связь (Злата 2019-2022) <-> 2025
        merge_persons_action(state, pid_19, pid_22)
        cands_2 = find_age_bridge_candidates(state, top_k=10, min_score=0.36)
        pair_zlata_25 = next(
            (
                c
                for c in cands_2
                if {c.person_a_id, c.person_b_id} == {pid_19, pid_25}
            ),
            None,
        )
        assert pair_zlata_25 is not None
        merge_persons_action(state, pid_19, pid_25)

        # Проверяем мульти-прототипы по годам (2019, 2022, 2025)
        protos = state.list_prototypes(person_id=pid_19)
        buckets = {int(p["year_bucket"]) for p in protos}
        assert buckets == {2019, 2022, 2025}

        # Stage 3: добавляем новое размытое фото 2025 года и новое фото 2020 года (промежуточное)
        v_2020 = era_vec(0.30)
        _, f_blur_2025 = _add_photo(
            state,
            "blur_2025.jpg",
            _ts(2025, 11, 1),
            [_face_dict(_perturb(rng, v_2025, 0.72), quality=0.32, est_age=7.5)],
        )
        _, f_new_2020 = _add_photo(
            state,
            "new_2020.jpg",
            _ts(2020, 7, 1),
            [_face_dict(_perturb(rng, v_2020, 0.85), quality=0.75, est_age=2.5)],
        )
        assigned = assign_unassigned_faces(state)
        assert assigned == 2
        assert state.get_face(f_blur_2025[0])["person_id"] == pid_19  # type: ignore[index]
        assert state.get_face(f_new_2020[0])["person_id"] == pid_19  # type: ignore[index]

        # Проверяем outlier-first сортировку: размытое лицо 2025 должно быть ближе к началу (ниже сходство)
        ordered = list_person_faces_outlier_first(state, pid_19)
        assert len(ordered) == 20
        sims = [float(f["proto_similarity"]) for f in ordered]
        assert sims == sorted(sims)


def test_rerun_clustering_preserves_user_naming_and_assignments(tmp_path: Path) -> None:
    """Повторный запуск Stage 1 после добавления новых фото не разрушает разметку пользователя."""
    rng = np.random.default_rng(303)
    v_mama = _unit(rng)
    v_new_person = _orth_unit(rng, v_mama)

    with SorterState(tmp_path / "incremental.db") as state:
        for i in range(4):
            _add_photo(
                state, f"mama_{i}.jpg", _ts(2023, 1, i + 1), [_face_dict(_perturb(rng, v_mama, 0.90))]
            )
        run_initial_clustering(state)
        persons = state.list_persons()
        assert len(persons) == 1
        mama_pid = int(persons[0]["id"])
        rename_person_action(state, mama_pid, "Мама", birth_year=1988)

        # Добавляем новые фото Мамы и нового человека
        _, new_mama_fids = _add_photo(
            state, "mama_new.jpg", _ts(2024, 2, 1), [_face_dict(_perturb(rng, v_mama, 0.85))]
        )
        _, guest_fids = _add_photo(
            state, "guest_1.jpg", _ts(2024, 2, 2), [_face_dict(_perturb(rng, v_new_person, 0.92))]
        )

        stats = run_initial_clustering(state)
        assert stats["matched_existing"] == 1
        mama_after = state.get_person(mama_pid)
        assert mama_after is not None
        assert mama_after["name"] == "Мама"
        assert mama_after["birth_year"] == 1988
        assert state.get_face(new_mama_fids[0])["person_id"] == mama_pid  # type: ignore[index]
        guest_face = state.get_face(guest_fids[0])
        assert guest_face is not None and guest_face["person_id"] not in (None, mama_pid)


def test_video_multiframe_assignment_rules(tmp_path: Path) -> None:
    """Видео-правило Stage 3: >=2 кадра на 0.50 -> да; 1 шумный кадр на 0.52 -> нет; короткий ролик 0.65 -> да."""
    rng = np.random.default_rng(404)
    hero_vec = _unit(rng)

    with SorterState(tmp_path / "video.db") as state:
        # Создаём эталонного человека и его прототип
        _, seed_fids = _add_photo(
            state,
            "hero_seed.jpg",
            _ts(2024, 5, 1),
            [_face_dict(hero_vec, quality=0.95)],
        )
        hero_pid = state.create_person(name="Герой", is_named=True, cover_face_id=seed_fids[0])
        state.set_face_assignments([(seed_fids[0], hero_pid, hero_pid, 1.0, "user")])
        rebuild_person_prototypes(state, hero_pid)

        # Видео 1 (10с): 2 разных ключевых кадра со сходством ~0.53 -> ПРИНЯТЬ
        v1_id, _ = state.upsert_media_file(
            local_path="/videos/v_multi.mp4",
            rel_path="v_multi.mp4",
            source_root="/videos",
            media_kind="video",
            size=5_000_000,
            mtime_ns=int(_ts(2024, 5, 2) * 1e9),
            shot_ts=_ts(2024, 5, 2),
            duration_sec=10.0,
            scan_status="scanned",
        )
        v1_fids = state.replace_faces_for_media(
            v1_id,
            [
                _face_dict(_perturb(rng, hero_vec, 0.53), quality=0.55, frame_ms=1000),
                _face_dict(_perturb(rng, hero_vec, 0.52), quality=0.55, frame_ms=4000),
            ],
        )

        # Видео 2 (10с): 1 шумный кадр со сходством 0.52 (и другой посторонний кадр) -> ОТКЛОНИТЬ
        v2_id, _ = state.upsert_media_file(
            local_path="/videos/v_noisy_one.mp4",
            rel_path="v_noisy_one.mp4",
            source_root="/videos",
            media_kind="video",
            size=5_000_000,
            mtime_ns=int(_ts(2024, 5, 3) * 1e9),
            shot_ts=_ts(2024, 5, 3),
            duration_sec=10.0,
            scan_status="scanned",
        )
        v2_fids = state.replace_faces_for_media(
            v2_id,
            [
                _face_dict(_perturb(rng, hero_vec, 0.52), quality=0.50, frame_ms=1000),
                _face_dict(_orth_unit(rng, hero_vec), quality=0.70, frame_ms=5000),
            ],
        )

        # Видео 3 (2.5с <= 3.5с): 1 чёткий кадр (quality=0.75 >= 0.60, cos=0.65 >= 0.62) -> ПРИНЯТЬ
        v3_id, _ = state.upsert_media_file(
            local_path="/videos/v_short_sharp.mp4",
            rel_path="v_short_sharp.mp4",
            source_root="/videos",
            media_kind="video",
            size=1_500_000,
            mtime_ns=int(_ts(2024, 5, 4) * 1e9),
            shot_ts=_ts(2024, 5, 4),
            duration_sec=2.5,
            scan_status="scanned",
        )
        v3_fids = state.replace_faces_for_media(
            v3_id,
            [
                _face_dict(_perturb(rng, hero_vec, 0.65), quality=0.75, frame_ms=500),
            ],
        )

        assigned = assign_unassigned_faces(state)
        assert assigned == 3  # 2 кадра из v1 + 1 кадр из v3

        assert state.get_face(v1_fids[0])["person_id"] == hero_pid  # type: ignore[index]
        assert state.get_face(v1_fids[1])["person_id"] == hero_pid  # type: ignore[index]
        assert state.get_face(v2_fids[0])["person_id"] is None  # type: ignore[index]
        assert state.get_face(v3_fids[0])["person_id"] == hero_pid  # type: ignore[index]


def test_all_studio_actions_and_undo_roundtrip(tmp_path: Path) -> None:
    """Полная проверка всех 6 действий Студии + точный откат каждого через undo_last_action."""
    rng = np.random.default_rng(505)
    v_a = _unit(rng)
    v_b = _orth_unit(rng, v_a)
    v_c = _orth_unit(rng, v_a)

    with SorterState(tmp_path / "studio.db") as state:
        _, f_a1 = _add_photo(state, "a1.jpg", _ts(2022, 1, 1), [_face_dict(v_a)])
        _, f_a2 = _add_photo(
            state, "a2.jpg", _ts(2022, 2, 1), [_face_dict(_perturb(rng, v_a, 0.92))]
        )
        _, f_b1 = _add_photo(state, "b1.jpg", _ts(2023, 1, 1), [_face_dict(v_b)])
        _, f_c1 = _add_photo(state, "c1.jpg", _ts(2024, 1, 1), [_face_dict(v_c)])

        pid_a = state.create_person(name="", is_named=False, cover_face_id=f_a1[0])
        pid_b = state.create_person(name="", is_named=False, cover_face_id=f_b1[0])
        pid_c = state.create_person(name="Гость", is_named=True, cover_face_id=f_c1[0])
        state.set_face_assignments(
            [
                (f_a1[0], pid_a, pid_a, 0.99, "cluster"),
                (f_a2[0], pid_a, pid_a, 0.95, "cluster"),
                (f_b1[0], pid_b, pid_b, 0.99, "cluster"),
                (f_c1[0], pid_c, pid_c, 0.99, "cluster"),
            ]
        )
        rebuild_person_prototypes(state)

        # 1. rename + undo
        rename_person_action(state, pid_a, "Алиса", birth_year=2017)
        p = state.get_person(pid_a)
        assert p is not None and p["name"] == "Алиса" and p["birth_year"] == 2017
        undone = undo_last_action(state)
        assert undone is not None and undone["action_type"] == "rename"
        p = state.get_person(pid_a)
        assert p is not None and p["name"] == "" and p["birth_year"] is None and p["is_named"] == 0

        # 2. reject_pair + undo
        reject_age_pair_action(state, pid_a, pid_b)
        assert tuple(sorted((pid_a, pid_b))) in state.get_cannot_link_person_pairs()
        undone = undo_last_action(state)
        assert undone is not None and undone["action_type"] == "reject_pair"
        assert tuple(sorted((pid_a, pid_b))) not in state.get_cannot_link_person_pairs()

        # 3. merge + undo (включая сохранение персональных ограничений и прототипов)
        reject_age_pair_action(state, pid_b, pid_c)
        merge_persons_action(state, pid_a, pid_b)
        assert state.get_person(pid_b) is None
        assert state.get_face(f_b1[0])["person_id"] == pid_a  # type: ignore[index]
        assert tuple(sorted((pid_a, pid_c))) in state.get_cannot_link_person_pairs()

        undone = undo_last_action(state)
        assert undone is not None and undone["action_type"] == "merge"
        assert state.get_person(pid_b) is not None
        assert state.get_face(f_b1[0])["person_id"] == pid_b  # type: ignore[index]
        assert tuple(sorted((pid_a, pid_c))) not in state.get_cannot_link_person_pairs()
        assert tuple(sorted((pid_b, pid_c))) in state.get_cannot_link_person_pairs()
        assert len(state.list_prototypes(person_id=pid_b)) == 1

        # Откатываем и reject_age_pair(pid_b, pid_c)
        undo_last_action(state)

        # 4. split + undo
        split_res = split_faces_action(state, [f_a2[0]], new_name="Выделенный")
        new_pid = int(split_res["person"]["id"])
        assert state.get_face(f_a2[0])["person_id"] == new_pid  # type: ignore[index]
        assert state.get_person(new_pid)["name"] == "Выделенный"  # type: ignore[index]
        undone = undo_last_action(state)
        assert undone is not None and undone["action_type"] == "split"
        assert state.get_person(new_pid) is None
        assert state.get_face(f_a2[0])["person_id"] == pid_a  # type: ignore[index]
        assert state.get_face(f_a2[0])["assign_source"] == "cluster"  # type: ignore[index]

        # 5. ignore + undo
        ignore_person_action(state, pid_c, ignored=True)
        assert state.get_person(pid_c)["is_ignored"] == 1  # type: ignore[index]
        assert state.list_prototypes(person_id=pid_c) == []
        undone = undo_last_action(state)
        assert undone is not None and undone["action_type"] == "ignore"
        assert state.get_person(pid_c)["is_ignored"] == 0  # type: ignore[index]
        assert len(state.list_prototypes(person_id=pid_c)) == 1

        # 6. reject_face + undo
        reject_face_action(state, f_a2[0], rejected=True)
        f_after = state.get_face(f_a2[0])
        assert f_after is not None and f_after["is_rejected"] == 1 and f_after["person_id"] is None
        undone = undo_last_action(state)
        assert undone is not None and undone["action_type"] == "reject_face"
        f_restored = state.get_face(f_a2[0])
        assert (
            f_restored is not None
            and f_restored["is_rejected"] == 0
            and f_restored["person_id"] == pid_a
        )

        # Пустой стек Undo возвращает None
        assert undo_last_action(state) is None

        # 7. force=True при merge через cannot_link + откат восстанавливает ограничение
        reject_age_pair_action(state, pid_a, pid_b)
        with pytest.raises(ValueError, match="cannot_link"):
            merge_persons_action(state, pid_a, pid_b)
        merge_persons_action(state, pid_a, pid_b, force=True)
        assert state.get_person(pid_b) is None
        undone = undo_last_action(state)
        assert undone is not None and undone["action_type"] == "merge"
        assert state.get_person(pid_b) is not None
        assert tuple(sorted((pid_a, pid_b))) in state.get_cannot_link_person_pairs()


def test_age_bridge_anti_stranger_and_different_names_guard(tmp_path: Path) -> None:
    """Посторонний с сильным семейным фоном и два по-разному названных профиля не попадают в age_bridge."""
    rng = np.random.default_rng(606)
    mama_vec = _unit(rng)
    child_vec = _orth_unit(rng, mama_vec)
    stranger_vec = _orth_unit(rng, child_vec)
    stranger_vec = stranger_vec - float(np.dot(stranger_vec, mama_vec)) * mama_vec
    stranger_vec = (stranger_vec / float(np.linalg.norm(stranger_vec))).astype(np.float32)

    with SorterState(tmp_path / "guards.db") as state:
        # 6 общих фото Мамы с ребёнком (2020) и 6 общих фото Мамы с посторонним на фоне (2022)
        for i in range(6):
            _add_photo(
                state,
                f"mc_{i}.jpg",
                _ts(2020, i + 1, 10),
                [
                    _face_dict(_perturb(rng, mama_vec, 0.93), est_age=34.0, est_gender=0, slot=0),
                    _face_dict(_perturb(rng, child_vec, 0.93), est_age=3.0, est_gender=0, slot=1),
                ],
            )
            _add_photo(
                state,
                f"ms_{i}.jpg",
                _ts(2022, i + 1, 10),
                [
                    _face_dict(_perturb(rng, mama_vec, 0.93), est_age=36.0, est_gender=0, slot=0),
                    _face_dict(
                        _perturb(rng, stranger_vec, 0.93), est_age=5.0, est_gender=0, slot=1
                    ),
                ],
            )
        run_initial_clustering(state)
        cands = find_age_bridge_candidates(state, top_k=25, min_score=0.20)
        assert cands == []

        # Два похожих кластера с РАЗНЫМИ именами пропускаются, с ОДИНАКОВЫМ именем допускаются
        near_child = _perturb(rng, child_vec, 0.50)
        _, f_near = _add_photo(
            state,
            "near_2023.jpg",
            _ts(2023, 3, 1),
            [_face_dict(near_child, est_age=6.0, est_gender=0)],
        )
        p_near = state.create_person(name="Другая", is_named=True, cover_face_id=f_near[0])
        state.set_face_assignments([(f_near[0], p_near, p_near, 1.0, "user")])
        # Находим кластер ребёнка и даём другое имя
        child_pid = next(
            int(p["id"])
            for p in state.list_persons()
            if int(p["id"]) != p_near and int(p["face_count"]) == 6
        )
        # Найдём именно ребёнка (не Маму с 12 лицами и не постороннего)
        for p in state.list_persons():
            pid = int(p["id"])
            if pid == p_near or int(p["face_count"]) == 12:
                continue
            faces_p = state.list_faces_with_media(person_id=pid)
            if faces_p and faces_p[0]["rel_path"].startswith("mc_"):
                child_pid = pid
                break
        rename_person_action(state, child_pid, "Злата")

        cands_diff = find_age_bridge_candidates(state, top_k=25, min_score=0.30)
        assert not any({c.person_a_id, c.person_b_id} == {child_pid, p_near} for c in cands_diff)

        rename_person_action(state, p_near, "злата")
        cands_same = find_age_bridge_candidates(state, top_k=25, min_score=0.30)
        assert any({c.person_a_id, c.person_b_id} == {child_pid, p_near} for c in cands_same)


def test_family_benchmark_strict_passes_fast() -> None:
    """Синтетический бенчмарк «Цифровая семья» (2 000 фото) проходит все строгие пороги быстро (< 15 с)."""
    result = run_family_benchmark(strict=True, seed=42, num_photos=2000)
    assert result["passed"] is True
    assert result["cluster_purity"] >= 0.98
    assert result["child_cross_age_recall_at_5"] >= 0.90
    assert result["stranger_false_merge_rate"] <= 0.01
    assert result["cannot_link_violations"] == 0
    assert result["post_merge_child_recall"] >= 0.90
    assert result["elapsed_sec"] < 15.0
