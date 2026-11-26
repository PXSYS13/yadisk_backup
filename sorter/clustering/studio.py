"""Обратимые действия Студии (переименование, объединение, разделение, игнор, отклонение) с Undo Ctrl+Z."""

from __future__ import annotations

import logging
from typing import Any

from sorter.clustering.prototypes import (
    assign_unassigned_faces,
    rebuild_person_prototypes,
)
from sorter.state import SorterState

log = logging.getLogger("photo_sorter.clustering.studio")

_UNSET: Any = object()


def _persons_have_cannot_link(
    state: SorterState, person_a_id: int, person_b_id: int
) -> bool:
    """Проверяет person-level и face-level cannot_link (включая один кадр) между двумя людьми."""
    pa, pb = sorted((int(person_a_id), int(person_b_id)))
    if (pa, pb) in state.get_cannot_link_person_pairs():
        return True

    faces_a = state.list_faces(person_id=pa, include_rejected=False)
    faces_b = state.list_faces(person_id=pb, include_rejected=False)
    ids_a = {int(f["id"]) for f in faces_a}
    ids_b = {int(f["id"]) for f in faces_b}
    if not ids_a or not ids_b:
        return False

    frames_a = {(int(f["media_id"]), int(f.get("frame_ms", 0))) for f in faces_a}
    for fb in faces_b:
        if (int(fb["media_id"]), int(fb.get("frame_ms", 0))) in frames_a:
            return True

    for cl_a, cl_b in state.get_cannot_link_face_pairs():
        if (cl_a in ids_a and cl_b in ids_b) or (cl_a in ids_b and cl_b in ids_a):
            return True
    return False


def _person_constraints_for(state: SorterState, person_id: int) -> list[dict[str, Any]]:
    pid = int(person_id)
    return [
        {
            "kind": str(c["kind"]),
            "entity_type": str(c["entity_type"]),
            "entity_a": int(c["entity_a"]),
            "entity_b": int(c["entity_b"]),
            "source": str(c["source"]),
        }
        for c in state.list_constraints(entity_type="person")
        if int(c["entity_a"]) == pid or int(c["entity_b"]) == pid
    ]


def rename_person_action(
    state: SorterState,
    person_id: int,
    name: str,
    birth_year: Any = _UNSET,
) -> dict[str, Any]:
    """Переименовывает человека (и опционально обновляет год рождения) с записью в журнал Undo."""
    pid = int(person_id)
    person = state.get_person(pid)
    if person is None:
        raise ValueError(f"Профиль #{pid} не найден")

    clean_name = str(name or "").strip()
    kwargs: dict[str, Any] = {"name": clean_name, "is_named": bool(clean_name)}
    if birth_year is not _UNSET:
        if birth_year is None:
            kwargs["birth_year"] = None
        else:
            by = int(birth_year)
            if by < 1850 or by > 2100:
                raise ValueError(f"Некорректный год рождения: {by}")
            kwargs["birth_year"] = by

    updated = state.update_person(pid, **kwargs)
    assert updated is not None

    snapshot = {
        "person_id": pid,
        "before": {
            "name": str(person.get("name", "")),
            "birth_year": person.get("birth_year"),
            "is_named": int(person.get("is_named", 0)),
        },
        "after": {
            "name": str(updated.get("name", "")),
            "birth_year": updated.get("birth_year"),
            "is_named": int(updated.get("is_named", 0)),
        },
    }
    old_label = str(person.get("name") or f"Человек #{pid}")
    new_label = str(updated.get("name") or f"Человек #{pid}")
    history_id = state.push_history(
        "rename", f"Переименование «{old_label}» -> «{new_label}»", snapshot
    )
    return {"action": "rename", "history_id": history_id, "person": updated}


def merge_persons_action(
    state: SorterState,
    target_person_id: int,
    source_person_id: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Объединяет source_person_id в target_person_id, перестраивает прототипы и доназначает лица."""
    tid, sid = int(target_person_id), int(source_person_id)
    if tid == sid:
        raise ValueError("Нельзя объединить профиль с самим собой")

    target = state.get_person(tid)
    source = state.get_person(sid)
    if target is None or source is None:
        raise ValueError("Один из выбранных профилей не найден")

    if not force and _persons_have_cannot_link(state, tid, sid):
        raise ValueError(
            "Профили имеют ограничение cannot_link (были на одном фото или разделены пользователем); "
            "передайте force=True для принудительного объединения"
        )

    target_constraints_before = _person_constraints_for(state, tid)
    source_faces_cluster_ids = {
        int(f["id"]): f.get("cluster_id")
        for f in state.list_faces(person_id=sid, include_rejected=True)
    }
    unassigned_before = {
        int(f["id"]): {
            "id": int(f["id"]),
            "cluster_id": f.get("cluster_id"),
            "assign_confidence": float(f.get("assign_confidence", 1.0)),
            "assign_source": str(f.get("assign_source", "cluster")),
        }
        for f in state.list_faces(include_rejected=False)
        if f.get("person_id") is None
    }

    merge_snap = state.merge_persons(tid, sid)
    rebuild_person_prototypes(state, tid)
    reassigned_count = assign_unassigned_faces(state)

    auto_assigned_faces: list[dict[str, Any]] = []
    if unassigned_before and reassigned_count > 0:
        for f in state.list_faces(include_rejected=False):
            fid = int(f["id"])
            if fid in unassigned_before and f.get("person_id") is not None:
                auto_assigned_faces.append(unassigned_before[fid])

    snapshot = {
        **merge_snap,
        "source_faces_cluster_ids": {
            str(k): v for k, v in source_faces_cluster_ids.items()
        },
        "target_constraints_before": target_constraints_before,
        "auto_assigned_faces": auto_assigned_faces,
    }

    t_label = str(target.get("name") or f"Человек #{tid}")
    s_label = str(source.get("name") or f"Человек #{sid}")
    history_id = state.push_history(
        "merge", f"Объединение «{s_label}» в «{t_label}»", snapshot
    )
    return {
        "action": "merge",
        "history_id": history_id,
        "person": state.get_person(tid),
        "merged_faces": len(merge_snap["moved_faces"]),
        "reassigned_faces": reassigned_count,
    }


def split_faces_action(
    state: SorterState, face_ids: list[int], new_name: str = ""
) -> dict[str, Any]:
    """Выделяет указанные лица в новый профиль с полной поддержкой отката Undo."""
    unique_ids: list[int] = []
    seen: set[int] = set()
    for raw_fid in face_ids:
        fid = int(raw_fid)
        if fid not in seen:
            unique_ids.append(fid)
            seen.add(fid)
    if not unique_ids:
        raise ValueError("Не указаны лица для выделения")

    faces_before: list[dict[str, Any]] = []
    affected_old_pids: set[int] = set()
    for fid in unique_ids:
        face = state.get_face(fid)
        if face is None:
            raise ValueError(f"Лицо #{fid} не найдено")
        if int(face.get("is_rejected", 0)) != 0:
            raise ValueError(f"Лицо #{fid} помечено как отклонённое")
        faces_before.append(
            {
                "id": fid,
                "cluster_id": face.get("cluster_id"),
                "person_id": face.get("person_id"),
                "assign_confidence": float(face.get("assign_confidence", 1.0)),
                "assign_source": str(face.get("assign_source", "cluster")),
                "quality_score": float(face.get("quality_score", 0.0)),
            }
        )
        if face.get("person_id") is not None:
            affected_old_pids.add(int(face["person_id"]))

    best_cover = max(faces_before, key=lambda f: (f["quality_score"], -f["id"]))["id"]
    clean_name = str(new_name or "").strip()
    new_pid = state.create_person(
        name=clean_name,
        is_named=bool(clean_name),
        cover_face_id=int(best_cover),
    )

    old_covers: dict[str, int | None] = {}
    for old_pid in sorted(affected_old_pids):
        old_p = state.get_person(old_pid)
        if old_p is not None and old_p.get("cover_face_id") in seen:
            old_covers[str(old_pid)] = int(old_p["cover_face_id"])
            state.update_person(old_pid, cover_face_id=None)

    state.set_face_assignments(
        [(fid, new_pid, new_pid, 1.0, "user") for fid in unique_ids]
    )

    for old_pid in sorted(affected_old_pids):
        state.add_constraint("cannot_link", "person", old_pid, new_pid, source="user")
        rebuild_person_prototypes(state, old_pid)
    rebuild_person_prototypes(state, new_pid)

    snapshot = {
        "new_person_id": new_pid,
        "faces_before": [
            {
                "id": f["id"],
                "cluster_id": f["cluster_id"],
                "person_id": f["person_id"],
                "assign_confidence": f["assign_confidence"],
                "assign_source": f["assign_source"],
            }
            for f in faces_before
        ],
        "old_covers": old_covers,
    }
    label = clean_name or f"Человек #{new_pid}"
    history_id = state.push_history(
        "split", f"Выделение {len(unique_ids)} лиц в «{label}»", snapshot
    )
    return {
        "action": "split",
        "history_id": history_id,
        "person": state.get_person(new_pid),
        "moved_faces": len(unique_ids),
    }


def ignore_person_action(
    state: SorterState, person_id: int, ignored: bool = True
) -> dict[str, Any]:
    """Переключает флаг игнорирования профиля и обновляет его прототипы."""
    pid = int(person_id)
    person = state.get_person(pid)
    if person is None:
        raise ValueError(f"Профиль #{pid} не найден")

    before_ignored = bool(int(person.get("is_ignored", 0)))
    after_ignored = bool(ignored)
    updated = state.update_person(pid, is_ignored=after_ignored)
    rebuild_person_prototypes(state, pid)

    snapshot = {
        "person_id": pid,
        "before_ignored": before_ignored,
        "after_ignored": after_ignored,
    }
    label = str(person.get("name") or f"Человек #{pid}")
    verb = "Скрыт профиль" if after_ignored else "Возвращён профиль"
    history_id = state.push_history("ignore", f"{verb} «{label}»", snapshot)
    return {"action": "ignore", "history_id": history_id, "person": updated}


def reject_face_action(
    state: SorterState, face_id: int, rejected: bool = True
) -> dict[str, Any]:
    """Помечает лицо как ложное срабатывание (не лицо) или снимает отметку."""
    fid = int(face_id)
    face = state.get_face(fid)
    if face is None:
        raise ValueError(f"Лицо #{fid} не найдено")

    old_pid = face.get("person_id")
    old_cover_face_id: int | None = None
    if rejected and old_pid is not None:
        old_person = state.get_person(int(old_pid))
        if old_person is not None and old_person.get("cover_face_id") == fid:
            old_cover_face_id = fid
            state.update_person(int(old_pid), cover_face_id=None)

    snapshot = {
        "face_id": fid,
        "before": {
            "is_rejected": bool(int(face.get("is_rejected", 0))),
            "cluster_id": face.get("cluster_id"),
            "person_id": int(old_pid) if old_pid is not None else None,
            "assign_confidence": float(face.get("assign_confidence", 1.0)),
            "assign_source": str(face.get("assign_source", "cluster")),
        },
        "after_rejected": bool(rejected),
        "old_cover_face_id": old_cover_face_id,
    }

    state.set_face_rejected(fid, rejected=bool(rejected))
    if old_pid is not None:
        rebuild_person_prototypes(state, int(old_pid))

    verb = "Отклонено лицо" if rejected else "Восстановлено лицо"
    history_id = state.push_history("reject_face", f"{verb} #{fid}", snapshot)
    return {
        "action": "reject_face",
        "history_id": history_id,
        "face": state.get_face(fid),
    }


def reject_age_pair_action(
    state: SorterState, person_a_id: int, person_b_id: int
) -> dict[str, Any]:
    """Добавляет постоянное ограничение cannot_link между двумя профилями (пользователь нажал «Нет»)."""
    pa, pb = sorted((int(person_a_id), int(person_b_id)))
    if pa == pb:
        raise ValueError("Нельзя задать ограничение профиля на самого себя")
    if state.get_person(pa) is None or state.get_person(pb) is None:
        raise ValueError("Один из выбранных профилей не найден")

    existed_before = (pa, pb) in state.get_cannot_link_person_pairs()
    state.add_constraint("cannot_link", "person", pa, pb, source="user")

    snapshot = {
        "person_a_id": pa,
        "person_b_id": pb,
        "existed_before": existed_before,
    }
    history_id = state.push_history(
        "reject_pair",
        f"Пара профилей #{pa} и #{pb} помечена как разные люди",
        snapshot,
    )
    return {
        "action": "reject_pair",
        "history_id": history_id,
        "person_a_id": pa,
        "person_b_id": pb,
    }


def undo_last_action(state: SorterState) -> dict[str, Any] | None:
    """Откатывает последнее неотменённое действие Студии и перестраивает затронутые прототипы."""
    entry = state.get_last_undoable()
    if entry is None:
        return None

    action_type = str(entry["action_type"])
    snap = entry.get("snapshot") or {}

    if action_type == "rename":
        pid = int(snap["person_id"])
        before = snap["before"]
        if state.get_person(pid) is not None:
            state.update_person(
                pid,
                name=str(before.get("name", "")),
                birth_year=before.get("birth_year"),
                is_named=bool(before.get("is_named", 0)),
            )

    elif action_type == "merge":
        target_before = snap["target_before"]
        source_before = snap["source_before"]
        tid = int(target_before["id"])
        sid = int(source_before["id"])

        state.restore_person_from_snapshot(source_before)
        if state.get_person(tid) is None:
            state.restore_person_from_snapshot(target_before)
        else:
            state.update_person(
                tid,
                name=str(target_before.get("name", "")),
                birth_year=target_before.get("birth_year"),
                is_named=bool(target_before.get("is_named", 0)),
                is_ignored=bool(target_before.get("is_ignored", 0)),
                priority=int(target_before.get("priority", 100)),
                cover_face_id=target_before.get("cover_face_id"),
            )

        restored_sid: int | None = sid if state.get_person(sid) is not None else None
        cluster_map = snap.get("source_faces_cluster_ids") or {}
        restore_updates: list[tuple[int, int | None, int | None, float, str]] = []
        for item in snap.get("moved_faces", []):
            fid = int(item["id"])
            cid = cluster_map.get(str(fid))
            restore_updates.append(
                (
                    fid,
                    int(cid) if cid is not None else None,
                    restored_sid,
                    float(item.get("assign_confidence", 1.0)),
                    str(item.get("assign_source", "cluster")),
                )
            )
        for item in snap.get("auto_assigned_faces", []):
            fid = int(item["id"])
            cid = item.get("cluster_id")
            restore_updates.append(
                (
                    fid,
                    int(cid) if cid is not None else None,
                    None,
                    float(item.get("assign_confidence", 1.0)),
                    str(item.get("assign_source", "cluster")),
                )
            )
        if restore_updates:
            state.set_face_assignments(restore_updates)

        # Восстанавливаем персональные ограничения точно к состоянию до слияния
        before_target_set = {
            (str(c["kind"]), int(c["entity_a"]), int(c["entity_b"]))
            for c in snap.get("target_constraints_before", [])
        }
        for current_c in _person_constraints_for(state, tid):
            key = (
                str(current_c["kind"]),
                int(current_c["entity_a"]),
                int(current_c["entity_b"]),
            )
            if key not in before_target_set:
                state.remove_constraint(
                    key[0], "person", int(current_c["entity_a"]), int(current_c["entity_b"])
                )
        for src_c in snap.get("source_constraints", []):
            state.add_constraint(
                str(src_c["kind"]),
                "person",
                int(src_c["entity_a"]),
                int(src_c["entity_b"]),
                source=str(src_c.get("source", "user")),
            )

        rebuild_person_prototypes(state, tid)
        if restored_sid is not None:
            rebuild_person_prototypes(state, restored_sid)

    elif action_type == "split":
        new_pid = int(snap["new_person_id"])
        affected_old: set[int] = set()
        pid_exists_cache: dict[int, bool] = {}
        split_updates: list[tuple[int, int | None, int | None, float, str]] = []
        for item in snap.get("faces_before", []):
            old_pid_raw = item.get("person_id")
            restored_old_pid: int | None = None
            if old_pid_raw is not None:
                old_pid_int = int(old_pid_raw)
                if old_pid_int not in pid_exists_cache:
                    pid_exists_cache[old_pid_int] = state.get_person(old_pid_int) is not None
                if pid_exists_cache[old_pid_int]:
                    restored_old_pid = old_pid_int
                    affected_old.add(old_pid_int)
            cid = item.get("cluster_id")
            split_updates.append(
                (
                    int(item["id"]),
                    int(cid) if cid is not None else None,
                    restored_old_pid,
                    float(item.get("assign_confidence", 1.0)),
                    str(item.get("assign_source", "cluster")),
                )
            )
        if split_updates:
            state.set_face_assignments(split_updates)
        state.delete_person(new_pid)

        for old_pid_str, cover_fid in (snap.get("old_covers") or {}).items():
            old_pid = int(old_pid_str)
            if state.get_person(old_pid) is not None:
                state.update_person(old_pid, cover_face_id=cover_fid)

        for old_pid in sorted(affected_old):
            rebuild_person_prototypes(state, old_pid)

    elif action_type == "ignore":
        pid = int(snap["person_id"])
        if state.get_person(pid) is not None:
            state.update_person(pid, is_ignored=bool(snap.get("before_ignored", False)))
            rebuild_person_prototypes(state, pid)

    elif action_type == "reject_face":
        fid = int(snap["face_id"])
        before = snap["before"]
        state.set_face_rejected(fid, rejected=bool(before.get("is_rejected", False)))
        old_pid = before.get("person_id")
        restored_pid: int | None = None
        if old_pid is not None and state.get_person(int(old_pid)) is not None:
            restored_pid = int(old_pid)
        cid = before.get("cluster_id")
        state.set_face_assignments(
            [
                (
                    fid,
                    int(cid) if cid is not None else None,
                    restored_pid,
                    float(before.get("assign_confidence", 1.0)),
                    str(before.get("assign_source", "cluster")),
                )
            ]
        )
        if restored_pid is not None:
            if snap.get("old_cover_face_id") is not None:
                state.update_person(restored_pid, cover_face_id=int(snap["old_cover_face_id"]))
            rebuild_person_prototypes(state, restored_pid)

    elif action_type == "reject_pair":
        if not bool(snap.get("existed_before", False)):
            state.remove_constraint(
                "cannot_link",
                "person",
                int(snap["person_a_id"]),
                int(snap["person_b_id"]),
            )
    else:
        raise ValueError(f"Неизвестный тип действия в истории Undo: {action_type}")

    state.mark_history_undone(int(entry["id"]))
    return {
        "undone": True,
        "history_id": int(entry["id"]),
        "action_type": action_type,
        "description": str(entry.get("description", "")),
    }
