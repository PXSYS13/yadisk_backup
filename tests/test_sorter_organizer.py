"""Юнит-тесты планировщика, исполнителя и безопасного отката сортировки."""

from __future__ import annotations

import calendar
import errno
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sorter.organizer import (
    SortOptions,
    build_sort_plan,
    check_hardlink_support,
    execute_sort_plan,
    resume_sort_run,
    rollback_sort_run,
)
from sorter.organizer import planner as planner_mod
from sorter.state import SorterState
from sorter.utils import compute_sha256


def _ts(year: int, month: int, day: int = 15, hour: int = 12) -> float:
    return float(calendar.timegm((year, month, day, hour, 0, 0)))


def _unit_vec(index: int) -> np.ndarray:
    v = np.zeros(512, dtype=np.float32)
    v[index % 512] = 1.0
    return v


def _add_media(
    state: SorterState,
    source_dir: Path,
    rel_path: str,
    content: bytes,
    *,
    media_kind: str = "photo",
    shot_ts: float | None = None,
    duration_sec: float | None = None,
    scan_status: str = "scanned",
) -> tuple[int, Path]:
    full = source_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)
    st = full.stat()
    mid, _ = state.upsert_media_file(
        local_path=str(full),
        rel_path=rel_path,
        source_root=str(source_dir),
        media_kind=media_kind,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        partial_hash=f"ph_{rel_path}",
        shot_ts=shot_ts,
        date_source="exif" if shot_ts is not None else "unknown",
        duration_sec=duration_sec,
        scan_status=scan_status,
    )
    return mid, full


def _add_faces(
    state: SorterState,
    media_id: int,
    specs: list[dict[str, Any]],
) -> list[int]:
    faces_payload: list[dict[str, Any]] = []
    for i, sp in enumerate(specs):
        faces_payload.append(
            {
                "frame_ms": int(sp.get("frame_ms", 0)),
                "bbox": sp.get("bbox", [10 + i * 50, 10, 60 + i * 50, 60]),
                "kps": [[20, 20]] * 5,
                "det_score": float(sp.get("det_score", 0.95)),
                "quality_score": float(sp.get("quality_score", 0.85)),
                "est_age": sp.get("est_age"),
                "est_gender": sp.get("est_gender"),
                "embedding": _unit_vec(i),
                "person_id": sp.get("person_id"),
                "assign_confidence": float(sp.get("assign_confidence", 0.95)),
                "assign_source": str(sp.get("assign_source", "cluster")),
                "is_rejected": bool(sp.get("is_rejected", False)),
            }
        )
    return state.replace_faces_for_media(media_id, faces_payload)


def test_sort_options_roundtrip_and_validation() -> None:
    opts = SortOptions(
        source_dir="C:/src",
        target_dir="C:/dst",
        mode="copy",
        tree_layout="dual",
        include_child_age=False,
        group_policy="shared_folder",
        no_faces_policy="skip",
        unnamed_policy="skip",
        skip_ignored=False,
        preserve_dates=False,
        video_min_frames=3,
        video_single_sharp_conf=0.7,
    )
    restored = SortOptions.from_dict(opts.to_dict())
    assert restored == opts

    with pytest.raises(ValueError):
        SortOptions(source_dir="C:/src", target_dir="C:/dst", mode="symlink")  # type: ignore[arg-type]


def test_plan_people_first_child_age_and_nodate(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True, priority=10)
        mama = st.create_person(name="Мама", birth_year=1990, is_named=True, priority=20)

        m_4y, _ = _add_media(st, src, "z_4y.jpg", b"z4", shot_ts=_ts(2023, 7, 15))
        m_0y, _ = _add_media(st, src, "z_0y.jpg", b"z0", shot_ts=_ts(2019, 3, 10))
        m_1y, _ = _add_media(st, src, "z_1y.jpg", b"z1", shot_ts=_ts(2020, 1, 5))
        m_nodate, _ = _add_media(st, src, "z_nodate.jpg", b"zn", shot_ts=None)
        m_adult, _ = _add_media(st, src, "mama.jpg", b"mm", shot_ts=_ts(2023, 7, 15))

        for mid in (m_4y, m_0y, m_1y, m_nodate):
            _add_faces(st, mid, [{"person_id": zlata}])
        _add_faces(st, m_adult, [{"person_id": mama}])

        plan = build_sort_plan(
            st,
            SortOptions(source_dir=str(src), target_dir=str(dst), include_child_age=True),
        )
        rel_by_media = {op.media_id: op.rel_dst for op in plan.ops}

        assert rel_by_media[m_4y] == "Люди/Злата/2023 (4 года)/07 Июль/z_4y.jpg"
        assert rel_by_media[m_0y] == "Люди/Злата/2019 (до 1 года)/03 Март/z_0y.jpg"
        assert rel_by_media[m_1y] == "Люди/Злата/2020 (1 год)/01 Январь/z_1y.jpg"
        assert rel_by_media[m_nodate] == "Люди/Злата/Без_даты/z_nodate.jpg"
        assert rel_by_media[m_adult] == "Люди/Мама/2023/07 Июль/mama.jpg"

        summary = plan.to_summary_dict()
        assert summary["total_ops"] == 5
        assert summary["unique_media"] == 5
        assert summary["summary_by_category"]["person"] == 5
        assert summary["tree_preview"]["count"] == 5
        assert summary["tree_preview"]["children"][0]["name"] == "Люди"

        # Без возраста ребёнка
        plan_no_age = build_sort_plan(
            st,
            SortOptions(source_dir=str(src), target_dir=str(dst), include_child_age=False),
        )
        rel_no_age = {op.media_id: op.rel_dst for op in plan_no_age.ops}
        assert rel_no_age[m_4y] == "Люди/Злата/2023/07 Июль/z_4y.jpg"


def test_plan_dual_and_group_policies(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True, priority=5)
        mama = st.create_person(name="Мама", birth_year=1990, is_named=True, priority=20)
        mid, _ = _add_media(st, src, "family.jpg", b"family", shot_ts=_ts(2023, 7, 15))
        _add_faces(st, mid, [{"person_id": mama}, {"person_id": zlata}])

        # each_person + dual
        plan_dual = build_sort_plan(
            st,
            SortOptions(
                source_dir=str(src),
                target_dir=str(dst),
                tree_layout="dual",
                group_policy="each_person",
            ),
        )
        rels = [op.rel_dst for op in plan_dual.ops]
        assert rels == [
            "Люди/Злата/2023 (4 года)/07 Июль/family.jpg",
            "Люди/Мама/2023/07 Июль/family.jpg",
            "По датам/2023/07 Июль/family.jpg",
        ]
        assert plan_dual.summary_by_category["person"] == 2
        assert plan_dual.summary_by_category["by_date"] == 1

        # shared_folder
        plan_shared = build_sort_plan(
            st,
            SortOptions(
                source_dir=str(src),
                target_dir=str(dst),
                tree_layout="people_first",
                group_policy="shared_folder",
            ),
        )
        assert len(plan_shared.ops) == 1
        assert plan_shared.ops[0].category == "shared"
        assert plan_shared.ops[0].rel_dst == "Совместные/2023/07 Июль/family.jpg"

        # priority (выигрывает меньший priority: Злата=5 < Мама=20)
        plan_prio = build_sort_plan(
            st,
            SortOptions(
                source_dir=str(src),
                target_dir=str(dst),
                tree_layout="people_first",
                group_policy="priority",
            ),
        )
        assert len(plan_prio.ops) == 1
        assert plan_prio.ops[0].person_id == zlata
        assert plan_prio.ops[0].rel_dst == "Люди/Злата/2023 (4 года)/07 Июль/family.jpg"


def test_plan_no_faces_unnamed_and_ignored(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True)
        unnamed = st.create_person(name="", is_named=False)
        ignored = st.create_person(name="Прохожий", is_named=True, is_ignored=True)

        m_nofaces, _ = _add_media(st, src, "landscape.jpg", b"land", shot_ts=_ts(2023, 8, 1))
        m_rejected, _ = _add_media(st, src, "false_det.jpg", b"rej", shot_ts=_ts(2023, 8, 2))
        m_unnamed, _ = _add_media(st, src, "who.jpg", b"who", shot_ts=_ts(2023, 8, 3))
        m_ignored_only, _ = _add_media(st, src, "stranger.jpg", b"ign", shot_ts=_ts(2023, 8, 4))
        m_mixed, _ = _add_media(st, src, "with_stranger.jpg", b"mix", shot_ts=_ts(2023, 8, 5))

        _add_faces(st, m_rejected, [{"person_id": zlata, "is_rejected": True}])
        _add_faces(st, m_unnamed, [{"person_id": unnamed}])
        _add_faces(st, m_ignored_only, [{"person_id": ignored}])
        _add_faces(st, m_mixed, [{"person_id": zlata}, {"person_id": ignored}])

        plan = build_sort_plan(
            st,
            SortOptions(
                source_dir=str(src),
                target_dir=str(dst),
                no_faces_policy="folder",
                unnamed_policy="folder",
                skip_ignored=True,
            ),
        )
        by_mid = {op.media_id: op for op in plan.ops}
        assert by_mid[m_nofaces].rel_dst == "Без_лиц/2023/08 Август/landscape.jpg"
        assert by_mid[m_rejected].rel_dst == "Без_лиц/2023/08 Август/false_det.jpg"
        assert by_mid[m_unnamed].rel_dst == "Нераспознанные/2023/08 Август/who.jpg"
        assert m_ignored_only not in by_mid
        assert by_mid[m_mixed].rel_dst == "Люди/Злата/2023 (4 года)/08 Август/with_stranger.jpg"

        # skip policies
        plan_skip = build_sort_plan(
            st,
            SortOptions(
                source_dir=str(src),
                target_dir=str(dst),
                no_faces_policy="skip",
                unnamed_policy="skip",
                skip_ignored=True,
            ),
        )
        assert [op.media_id for op in plan_skip.ops] == [m_mixed]


def test_plan_video_attribution_guard(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True)

        # 1) Длинное видео с 1 кадром (cluster) -> не атрибутируется Злате
        v_one_frame, _ = _add_media(
            st, src, "long_one.mp4", b"v1", media_kind="video", shot_ts=_ts(2023, 7, 1), duration_sec=12.0
        )
        _add_faces(
            st,
            v_one_frame,
            [{"frame_ms": 1000, "person_id": zlata, "quality_score": 0.9, "assign_confidence": 0.9}],
        )

        # 2) Длинное видео с 2 разными кадрами -> атрибутируется Злате
        v_two_frames, _ = _add_media(
            st, src, "long_two.mp4", b"v2", media_kind="video", shot_ts=_ts(2023, 7, 2), duration_sec=12.0
        )
        _add_faces(
            st,
            v_two_frames,
            [
                {"frame_ms": 1000, "person_id": zlata, "quality_score": 0.5, "assign_confidence": 0.5},
                {"frame_ms": 3000, "person_id": zlata, "quality_score": 0.5, "assign_confidence": 0.5},
            ],
        )

        # 3) Длинное видео с 1 кадром, но подтверждено пользователем (assign_source='user') -> проходит
        v_user, _ = _add_media(
            st, src, "long_user.mp4", b"v3", media_kind="video", shot_ts=_ts(2023, 7, 3), duration_sec=20.0
        )
        _add_faces(
            st,
            v_user,
            [{"frame_ms": 500, "person_id": zlata, "assign_source": "user", "quality_score": 0.3}],
        )

        # 4) Короткое видео (<= 3.5 сек) с 1 чётким и уверенным лицом -> проходит
        v_short_ok, _ = _add_media(
            st, src, "short_ok.mp4", b"v4", media_kind="video", shot_ts=_ts(2023, 7, 4), duration_sec=2.8
        )
        _add_faces(
            st,
            v_short_ok,
            [{"frame_ms": 0, "person_id": zlata, "quality_score": 0.72, "assign_confidence": 0.68}],
        )

        # 5) Короткое видео с мыльным лицом (quality < 0.60) -> не проходит
        v_short_blur, _ = _add_media(
            st, src, "short_blur.mp4", b"v5", media_kind="video", shot_ts=_ts(2023, 7, 5), duration_sec=2.0
        )
        _add_faces(
            st,
            v_short_blur,
            [{"frame_ms": 0, "person_id": zlata, "quality_score": 0.45, "assign_confidence": 0.85}],
        )

        plan = build_sort_plan(st, SortOptions(source_dir=str(src), target_dir=str(dst)))
        by_mid = {op.media_id: op for op in plan.ops}

        assert by_mid[v_one_frame].category == "unnamed"
        assert by_mid[v_two_frames].category == "person"
        assert by_mid[v_two_frames].person_id == zlata
        assert by_mid[v_user].category == "person"
        assert by_mid[v_short_ok].category == "person"
        assert by_mid[v_short_blur].category == "unnamed"


def test_plan_collisions_and_overlap_rejection(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True)
        m1, _ = _add_media(st, src, "a/IMG_0001.jpg", b"one", shot_ts=_ts(2023, 7, 10))
        m2, _ = _add_media(st, src, "b/IMG_0001.jpg", b"two", shot_ts=_ts(2023, 7, 11))
        _add_faces(st, m1, [{"person_id": zlata}])
        _add_faces(st, m2, [{"person_id": zlata}])

        # Создаём уже существующий файл на диске в целевой папке
        existing = dst / "Люди" / "Злата" / "2023 (4 года)" / "07 Июль" / "IMG_0001.jpg"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(b"already_there")

        plan = build_sort_plan(st, SortOptions(source_dir=str(src), target_dir=str(dst)))
        rels = [op.rel_dst for op in plan.ops]
        assert rels == [
            "Люди/Злата/2023 (4 года)/07 Июль/IMG_0001_2.jpg",
            "Люди/Злата/2023 (4 года)/07 Июль/IMG_0001_3.jpg",
        ]

        # Защита от пересечения source и target
        with pytest.raises(ValueError):
            build_sort_plan(st, SortOptions(source_dir=str(src), target_dir=str(src)))
        with pytest.raises(ValueError):
            build_sort_plan(st, SortOptions(source_dir=str(src), target_dir=str(src / "sub_out")))
        with pytest.raises(ValueError):
            build_sort_plan(st, SortOptions(source_dir=str(src), target_dir=str(tmp_path)))


def test_plan_move_multi_dest_ordering_and_hardlink_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    with SorterState(tmp_path / "state.db") as st:
        p1 = st.create_person(name="Злата", birth_year=2019, is_named=True, priority=1)
        p2 = st.create_person(name="Мама", is_named=True, priority=2)
        mid, _ = _add_media(st, src, "group.jpg", b"x" * 2048, shot_ts=_ts(2023, 7, 15))
        _add_faces(st, mid, [{"person_id": p1}, {"person_id": p2}])

        # 1) На том же диске с поддержкой hardlink: первые 2 -> hardlink, последний -> move
        plan_move = build_sort_plan(
            st,
            SortOptions(
                source_dir=str(src),
                target_dir=str(dst),
                mode="move",
                tree_layout="dual",
                group_policy="each_person",
            ),
        )
        assert [op.op_type for op in plan_move.ops] == ["hardlink", "hardlink", "move"]

        # 2) Симуляция FAT32 / exFAT -> fallback для hardlink в copy, а для move первые 2 -> copy
        monkeypatch.setattr(planner_mod, "_get_windows_filesystem_name", lambda _p: "exFAT")
        supported, reason = check_hardlink_support(str(src), str(dst))
        assert supported is False
        assert reason is not None and "exFAT" in reason

        plan_hl_fallback = build_sort_plan(
            st, SortOptions(source_dir=str(src), target_dir=str(dst), mode="hardlink")
        )
        assert plan_hl_fallback.effective_mode == "copy"
        assert plan_hl_fallback.hardlink_supported is False
        assert plan_hl_fallback.mode_fallback_reason is not None
        assert plan_hl_fallback.required_bytes == 2048 * 2

        plan_move_no_hl = build_sort_plan(
            st,
            SortOptions(
                source_dir=str(src),
                target_dir=str(dst),
                mode="move",
                tree_layout="dual",
                group_policy="each_person",
            ),
        )
        assert [op.op_type for op in plan_move_no_hl.ops] == ["copy", "copy", "move"]


def test_execute_hardlink_and_full_rollback(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    payload_1 = b"photo-bytes-zlata-2023" * 50
    payload_2 = b"photo-bytes-group-2023" * 40

    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True, priority=1)
        mama = st.create_person(name="Мама", is_named=True, priority=2)
        m1, f1 = _add_media(st, src, "solo.jpg", payload_1, shot_ts=_ts(2023, 7, 15))
        m2, f2 = _add_media(st, src, "both.jpg", payload_2, shot_ts=_ts(2023, 7, 16))
        _add_faces(st, m1, [{"person_id": zlata}])
        _add_faces(st, m2, [{"person_id": zlata}, {"person_id": mama}])

        plan = build_sort_plan(
            st, SortOptions(source_dir=str(src), target_dir=str(dst), mode="hardlink")
        )
        assert plan.effective_mode == "hardlink"
        assert plan.required_bytes == 0
        assert len(plan.ops) == 3

        res = execute_sort_plan(st, plan)
        assert res.status == "completed"
        assert res.done_ops == 3
        assert res.failed_ops == 0

        # Проверяем тот же inode и нетронутость исходников
        for op in plan.ops:
            src_st = os.stat(op.src_path)
            dst_st = os.stat(op.dst_path)
            assert src_st.st_ino == dst_st.st_ino
            assert src_st.st_dev == dst_st.st_dev
        assert f1.read_bytes() == payload_1
        assert f2.read_bytes() == payload_2

        # SHA-256 закеширован в media_files
        m1_row = st.get_media(m1)
        assert m1_row is not None and m1_row["sha256"] == compute_sha256(str(f1))

        # Полный откат: 0 остаточных файлов и пустых папок в target_dir, исходники целы
        rb = rollback_sort_run(st, res.run_id)
        assert rb.status == "rolled_back"
        assert rb.reverted == 3
        assert rb.skipped_modified == 0
        assert rb.failed == 0
        assert rb.removed_dirs > 0
        assert list(dst.rglob("*")) == []
        assert f1.exists() and f1.read_bytes() == payload_1
        assert f2.exists() and f2.read_bytes() == payload_2


def test_rollback_hardlink_never_deletes_last_copy_if_source_missing(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True)
        mid, src_file = _add_media(st, src, "precious.jpg", b"precious-data", shot_ts=_ts(2023, 7, 1))
        _add_faces(st, mid, [{"person_id": zlata}])

        plan = build_sort_plan(st, SortOptions(source_dir=str(src), target_dir=str(dst), mode="hardlink"))
        res = execute_sort_plan(st, plan)
        dst_file = Path(plan.ops[0].dst_path)

        # Пользователь случайно удалил исходник -> откат НЕ смеет удалять последнюю копию
        src_file.unlink()

        rb = rollback_sort_run(st, res.run_id)
        assert rb.status == "partial"
        assert rb.reverted == 0
        assert rb.skipped_modified == 1
        assert dst_file.exists()
        assert dst_file.read_bytes() == b"precious-data"


def test_execute_copy_preserves_dates_and_rollback_skips_modified(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    shot_ts = _ts(2022, 6, 15, 14)
    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True)
        m1, f1 = _add_media(st, src, "keep_clean.jpg", b"clean-bytes-123", shot_ts=shot_ts)
        m2, f2 = _add_media(st, src, "will_edit.jpg", b"original-bytes-456", shot_ts=shot_ts)
        _add_faces(st, m1, [{"person_id": zlata}])
        _add_faces(st, m2, [{"person_id": zlata}])

        plan = build_sort_plan(
            st,
            SortOptions(source_dir=str(src), target_dir=str(dst), mode="copy", preserve_dates=True),
        )
        res = execute_sort_plan(st, plan)
        assert res.status == "completed"
        assert res.done_ops == 2

        dst_by_mid = {op.media_id: Path(op.dst_path) for op in plan.ops}
        dst_clean = dst_by_mid[m1]
        dst_edited = dst_by_mid[m2]

        # Проверяем SHA-256 и дату изменения файла и родительской папки
        assert compute_sha256(str(dst_clean)) == compute_sha256(str(f1))
        assert abs(dst_clean.stat().st_mtime - shot_ts) < 2.0
        assert abs(dst_clean.parent.stat().st_mtime - shot_ts) < 2.0

        # Пользователь отредактировал второй скопированный файл
        dst_edited.write_bytes(b"user-edited-version-must-survive")

        rb = rollback_sort_run(st, res.run_id)
        assert rb.status == "partial"
        assert rb.reverted == 1
        assert rb.skipped_modified == 1
        assert rb.failed == 0
        assert not dst_clean.exists()
        assert dst_edited.exists()
        assert dst_edited.read_bytes() == b"user-edited-version-must-survive"
        assert f1.read_bytes() == b"clean-bytes-123"
        assert f2.read_bytes() == b"original-bytes-456"


def test_execute_hardlink_runtime_fallback_to_copy_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True)
        mid, src_file = _add_media(st, src, "fallback.jpg", b"fallback-payload", shot_ts=_ts(2023, 5, 1))
        _add_faces(st, mid, [{"person_id": zlata}])

        plan = build_sort_plan(st, SortOptions(source_dir=str(src), target_dir=str(dst), mode="hardlink"))

        def _fail_link(_s: str, _d: str) -> None:
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        monkeypatch.setattr(os, "link", _fail_link)

        res = execute_sort_plan(st, plan)
        assert res.status == "completed"
        assert res.done_ops == 1
        dst_file = Path(plan.ops[0].dst_path)
        assert dst_file.exists()
        assert dst_file.read_bytes() == b"fallback-payload"
        assert os.stat(src_file).st_ino != os.stat(dst_file).st_ino

        rb = rollback_sort_run(st, res.run_id)
        assert rb.status == "rolled_back"
        assert rb.reverted == 1
        assert not dst_file.exists()
        assert list(dst.rglob("*")) == []
        assert src_file.read_bytes() == b"fallback-payload"


def test_execute_move_guard_multi_dest_and_rollback_restore(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    raw_solo = b"solo-move-content" * 30
    raw_group = b"group-move-content" * 30

    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True, priority=1)
        mama = st.create_person(name="Мама", is_named=True, priority=2)
        m1, f1 = _add_media(st, src, "sub/solo.jpg", raw_solo, shot_ts=_ts(2023, 7, 10))
        m2, f2 = _add_media(st, src, "sub/group.jpg", raw_group, shot_ts=_ts(2023, 7, 11))
        _add_faces(st, m1, [{"person_id": zlata}])
        _add_faces(st, m2, [{"person_id": zlata}, {"person_id": mama}])

        sha1 = compute_sha256(str(f1))
        sha2 = compute_sha256(str(f2))

        plan = build_sort_plan(
            st,
            SortOptions(source_dir=str(src), target_dir=str(dst), mode="move", group_policy="each_person"),
        )

        # Без allow_move=True перемещение запрещено и файлы не тронуты
        with pytest.raises(ValueError):
            execute_sort_plan(st, plan, allow_move=False)
        assert f1.exists() and f2.exists()

        # С allow_move=True перемещение выполняется
        res = execute_sort_plan(st, plan, allow_move=True)
        assert res.status == "completed"
        assert res.done_ops == 3
        assert not f1.exists()
        assert not f2.exists()
        for op in plan.ops:
            assert Path(op.dst_path).exists()

        # Откат возвращает исходные файлы по точным прежним путям и удаляет все пустые папки
        rb = rollback_sort_run(st, res.run_id)
        assert rb.status == "rolled_back"
        assert rb.reverted == 3
        assert rb.skipped_modified == 0
        assert rb.failed == 0
        assert f1.exists() and compute_sha256(str(f1)) == sha1
        assert f2.exists() and compute_sha256(str(f2)) == sha2
        assert list(dst.rglob("*")) == []


def test_execute_refuses_insufficient_space(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True)
        mid, _ = _add_media(st, src, "big.jpg", b"0123456789", shot_ts=_ts(2023, 7, 1))
        _add_faces(st, mid, [{"person_id": zlata}])

        plan = build_sort_plan(st, SortOptions(source_dir=str(src), target_dir=str(dst), mode="copy"))
        plan.enough_space = False
        plan.free_bytes = 512

        with pytest.raises(RuntimeError, match="Недостаточно свободного места"):
            execute_sort_plan(st, plan)


def test_resume_sort_run_after_cancellation(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()

    with SorterState(tmp_path / "state.db") as st:
        zlata = st.create_person(name="Злата", birth_year=2019, is_named=True)
        for i in range(3):
            mid, _ = _add_media(
                st, src, f"img_{i}.jpg", f"payload-{i}".encode("utf-8"), shot_ts=_ts(2023, 7, i + 1)
            )
            _add_faces(st, mid, [{"person_id": zlata}])

        plan = build_sort_plan(st, SortOptions(source_dir=str(src), target_dir=str(dst), mode="copy"))
        stop_ev = threading.Event()

        def _stop_after_first(info: dict[str, Any]) -> None:
            if info["processed"] >= 1:
                stop_ev.set()

        res_partial = execute_sort_plan(st, plan, stop_event=stop_ev, progress_cb=_stop_after_first)
        assert res_partial.status == "cancelled"
        assert res_partial.done_ops == 1
        assert res_partial.total_ops == 3

        pending_before = st.list_run_operations(res_partial.run_id, status="pending")
        assert len(pending_before) == 2

        # Продолжаем прерванный запуск
        res_final = resume_sort_run(st, res_partial.run_id)
        assert res_final.status == "completed"
        assert res_final.done_ops == 3
        assert res_final.failed_ops == 0
        assert st.list_run_operations(res_partial.run_id, status="pending") == []
        for op in plan.ops:
            assert Path(op.dst_path).exists()
