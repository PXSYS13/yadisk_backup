"""Нативный роутер офлайн-сортировки фото и видео по людям (/api/people/*).

Подключается к единому FastAPI-приложению `yadisk_backup` и использует его защиту localhost/CSRF/CSP.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from sorter import paths
from sorter.clustering import (
    find_age_bridge_candidates,
    ignore_person_action,
    list_person_faces_outlier_first,
    merge_persons_action,
    reject_age_pair_action,
    reject_face_action,
    rename_person_action,
    split_faces_action,
    undo_last_action,
    year_from_ts,
)
from sorter.clustering.studio import _UNSET as _STUDIO_UNSET
from sorter.organizer import SortOptions, build_sort_plan
from sorter.state import SorterState
from sorter.utils import (
    is_path_inside,
    is_symlink_or_junction,
    path_key,
    to_long_path,
    validate_dir_safe,
)
import sorter.webapp.config as cfg_mod
import sorter.webapp.manager as mgr_mod

log = logging.getLogger("webui.people")
router = APIRouter(prefix="/api/people", tags=["people"])

manager: mgr_mod.TaskManager = mgr_mod.manager
_studio_lock = threading.RLock()


class SettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: str | None = None
    target_dir: str | None = None
    model_pack: str | None = None
    prefer_gpu: bool | None = None
    mode: str | None = None
    tree_layout: str | None = None
    include_child_age: bool | None = None
    group_policy: str | None = None
    no_faces_policy: str | None = None
    unnamed_policy: str | None = None
    skip_ignored: bool | None = None
    preserve_dates: bool | None = None


class FolderPickPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal["source_dir", "target_dir"] = "source_dir"


class ScanStartPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: str | None = None
    recluster: bool = True
    model_pack: str | None = None
    prefer_gpu: bool | None = None


class PersonPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    birth_year: int | None = None
    is_ignored: bool | None = None
    priority: int | None = None


class MergePersonsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_person_id: int
    source_person_id: int
    force: bool = False


class SplitFacesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    face_ids: list[int] = Field(..., min_length=1)
    new_name: str = ""


class RejectFacePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rejected: bool = True


class AgeBridgeDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_a_id: int
    person_b_id: int
    decision: Literal["merge", "reject"]
    merged_name: str | None = None
    birth_year: int | None = None


class PlanPreviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: str | None = None
    target_dir: str | None = None
    mode: str | None = None
    tree_layout: str | None = None
    include_child_age: bool | None = None
    group_policy: str | None = None
    no_faces_policy: str | None = None
    unnamed_policy: str | None = None
    skip_ignored: bool | None = None
    preserve_dates: bool | None = None
    video_min_frames: int | None = None
    video_single_sharp_conf: float | None = None


class SortStartPayload(PlanPreviewPayload):
    model_config = ConfigDict(extra="forbid")

    allow_move: bool = False


class ResumeRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_move: bool = False


def _serialize_prototypes(raw_protos: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    by_pid: dict[int, list[dict[str, Any]]] = {}
    for pr in raw_protos:
        pid = int(pr["person_id"])
        by_pid.setdefault(pid, []).append(
            {
                "year_bucket": int(pr["year_bucket"]),
                "face_count": int(pr["face_count"]),
                "mean_est_age": pr.get("mean_est_age"),
            }
        )
    return by_pid


def _format_person_row(
    row: dict[str, Any], prototypes: list[dict[str, Any]]
) -> dict[str, Any]:
    out = dict(row)
    out["id"] = int(out["id"])
    out["is_named"] = bool(int(out.get("is_named", 0)))
    out["is_ignored"] = bool(int(out.get("is_ignored", 0)))
    out["priority"] = int(out.get("priority", 100))
    out["prototypes"] = list(prototypes)
    return out


def _get_formatted_person(state: SorterState, person_id: int) -> dict[str, Any] | None:
    for p in state.list_persons(include_ignored=True):
        if int(p["id"]) == int(person_id):
            protos = _serialize_prototypes(state.list_prototypes(person_id=person_id)).get(
                int(person_id), []
            )
            return _format_person_row(p, protos)
    return None


def _serialize_face_row(face: dict[str, Any]) -> dict[str, Any]:
    out = dict(face)
    out.pop("embedding", None)
    out.pop("local_path", None)
    out["id"] = int(out["id"])
    out["media_id"] = int(out["media_id"])
    out["is_rejected"] = bool(int(out.get("is_rejected", 0)))
    yr = year_from_ts(out.get("shot_ts"))
    out["year"] = yr if yr > 0 else None
    return out


def _safe_cache_file(root_dir: Path, rel_path: str) -> Path | None:
    """Проверяет, что файл кеша — обычный не-симлинк/не-junction строго внутри root_dir."""
    rel = (rel_path or "").strip()
    if not rel or "\x00" in rel or os.path.isabs(rel):
        return None
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or ":" in norm:
        return None
    parts = [part for part in norm.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None

    try:
        root_resolved = root_dir.resolve()
        if not root_resolved.is_dir():
            return None
        cur = root_resolved
        for seg in parts:
            cur = cur / seg
            st = os.lstat(to_long_path(str(cur)))
            if is_symlink_or_junction(cur, st):
                return None
        resolved = cur.resolve()
        if not is_path_inside(str(resolved), str(root_resolved)):
            return None
        if path_key(str(resolved)) == path_key(str(root_resolved)):
            return None
        final_st = os.lstat(to_long_path(str(resolved)))
        if is_symlink_or_junction(resolved, final_st) or not stat.S_ISREG(final_st.st_mode):
            return None
        return resolved
    except OSError:
        return None


def _clear_cache_dir_files(root_dir: Path) -> None:
    """Безопасно удаляет обычные файлы внутри каталога кеша без перехода по симлинкам/junction."""
    if not root_dir.is_dir():
        return
    root_resolved = root_dir.resolve()
    try:
        entries = list(os.scandir(root_resolved))
    except OSError as exc:
        log.warning("Не удалось прочитать каталог кеша %s: %s", root_resolved, exc)
        return
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
            if is_symlink_or_junction(entry.path, st):
                continue
            resolved = Path(entry.path).resolve()
            if not is_path_inside(str(resolved), str(root_resolved)):
                continue
            os.unlink(to_long_path(str(resolved)))
        except OSError as exc:
            log.debug("Не удалось удалить файл кеша %s: %s", entry.name, exc)


def _build_sort_options_from_payload(payload: PlanPreviewPayload) -> SortOptions:
    cfg = cfg_mod.get_config()
    merged: dict[str, Any] = dict(cfg)
    for key, val in payload.model_dump(exclude_none=True).items():
        merged[key] = val
    return SortOptions.from_dict(merged)


def _ensure_not_running_for_studio() -> None:
    if manager.is_running():
        raise HTTPException(status_code=409, detail="Сначала остановите текущую операцию")


@router.get("/status")
def api_people_status():
    return manager.status_dict()


@router.post("/settings")
def api_people_settings(payload: SettingsPayload):
    updates = payload.model_dump(exclude_none=True)
    for field_name, label in (
        ("source_dir", "исходная папка"),
        ("target_dir", "целевая папка"),
    ):
        if field_name in updates:
            raw_dir = str(updates[field_name]).strip()
            if raw_dir:
                try:
                    clean_dir = validate_dir_safe(raw_dir, label=label)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                if os.path.isfile(to_long_path(clean_dir)):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Указанный путь является файлом, а не папкой: {clean_dir}",
                    )
                updates[field_name] = clean_dir
            else:
                updates[field_name] = ""
    try:
        saved = cfg_mod.save_config(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "config": saved}


@router.post("/folder/pick")
def api_people_pick_folder(payload: FolderPickPayload | None = None):
    if manager.is_running():
        raise HTTPException(status_code=409, detail="Сначала остановите текущую операцию")
    field = payload.field if payload is not None else "source_dir"
    label = "исходная папка" if field == "source_dir" else "целевая папка"
    cur = str(cfg_mod.get_config().get(field, "")).strip()
    safe_init = ""
    if cur:
        try:
            safe_init = validate_dir_safe(cur, label=label)
        except ValueError:
            safe_init = ""

    raw_pick = mgr_mod.pick_folder_dialog(safe_init)
    if isinstance(raw_pick, tuple):
        chosen, pick_err = raw_pick
    else:
        chosen, pick_err = raw_pick, None

    if pick_err:
        raise HTTPException(status_code=500, detail=str(pick_err))
    if not chosen:
        return {"picked": False, "path": cur}
    try:
        clean = validate_dir_safe(chosen, label=label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cfg_mod.save_config({field: clean})
    return {"picked": True, "path": clean}


@router.post("/scan/start")
def api_people_scan_start(payload: ScanStartPayload | None = None):
    req = payload if payload is not None else ScanStartPayload()
    src = (req.source_dir or "").strip() or str(cfg_mod.get_config().get("source_dir", ""))
    try:
        manager.start_scan(
            src,
            recluster=req.recluster,
            model_pack=req.model_pack,
            prefer_gpu=req.prefer_gpu,
        )
    except mgr_mod.TaskBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/cluster/run")
def api_people_cluster_run():
    try:
        manager.start_cluster()
    except mgr_mod.TaskBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/task/stop")
def api_people_task_stop():
    return {"ok": True, "stopping": manager.stop()}


@router.post("/reset")
def api_people_reset():
    with _studio_lock:
        _ensure_not_running_for_studio()
        with SorterState(paths.DB_FILE) as state:
            state.clear_all()
        _clear_cache_dir_files(paths.CROPS_DIR)
        _clear_cache_dir_files(paths.THUMBS_DIR)
        manager.invalidate_age_bridge_cache()
        manager.add_event("info", "База индекса и кеш кропов очищены (исходные фото не затронуты)")
    return {"ok": True}


@router.get("/persons")
def api_people_list_persons(include_ignored: Annotated[bool, Query()] = True):
    with SorterState(paths.DB_FILE) as state:
        persons = state.list_persons(include_ignored=include_ignored)
        protos_by_pid = _serialize_prototypes(state.list_prototypes())
    items = [
        _format_person_row(p, protos_by_pid.get(int(p["id"]), []))
        for p in persons
    ]
    return {"items": items}


@router.get("/persons/{person_id}/faces")
def api_people_person_faces(person_id: int):
    with SorterState(paths.DB_FILE) as state:
        person = _get_formatted_person(state, person_id)
        if person is None:
            raise HTTPException(status_code=404, detail=f"Профиль #{person_id} не найден")
        faces = list_person_faces_outlier_first(state, person_id)
    return {
        "person": person,
        "items": [_serialize_face_row(f) for f in faces],
    }


@router.patch("/persons/{person_id}")
def api_people_patch_person(person_id: int, payload: PersonPatchPayload):
    _ensure_not_running_for_studio()
    fields_set = payload.model_fields_set
    if "birth_year" in fields_set and payload.birth_year is not None:
        if payload.birth_year < 1850 or payload.birth_year > 2100:
            raise HTTPException(
                status_code=400,
                detail=f"Некорректный год рождения: {payload.birth_year}",
            )

    with _studio_lock:
        _ensure_not_running_for_studio()
        with SorterState(paths.DB_FILE) as state:
            person = state.get_person(person_id)
            if person is None:
                raise HTTPException(status_code=404, detail=f"Профиль #{person_id} не найден")

            merged_into: int | None = None
            active_pid = person_id

            if "name" in fields_set:
                clean_name = (payload.name or "").strip()
                existing_match: dict[str, Any] | None = None
                if clean_name:
                    target_key = clean_name.casefold()
                    for cand in state.list_persons(include_ignored=False):
                        if int(cand["id"]) == person_id:
                            continue
                        if int(cand.get("is_named", 0)) and (
                            str(cand.get("name") or "").strip().casefold() == target_key
                        ):
                            existing_match = cand
                            break

                if existing_match is not None:
                    existing_id = int(existing_match["id"])
                    try:
                        merge_persons_action(state, existing_id, person_id, force=False)
                    except ValueError as exc:
                        if "cannot_link" in str(exc).lower():
                            raise HTTPException(
                                status_code=409,
                                detail=(
                                    f"Нельзя автоматически объединить с профилем «{clean_name}»: "
                                    "профили встречались на одном фото или были разделены пользователем. "
                                    "Если это точно один человек, объедините их вручную с подтверждением."
                                ),
                            ) from exc
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                    merged_into = existing_id
                    active_pid = existing_id
                    if "birth_year" in fields_set and payload.birth_year is not None:
                        target_after = state.get_person(existing_id)
                        if target_after is not None and target_after.get("birth_year") is None:
                            rename_person_action(
                                state,
                                existing_id,
                                str(target_after.get("name") or clean_name),
                                birth_year=payload.birth_year,
                            )
                else:
                    try:
                        rename_person_action(
                            state,
                            person_id,
                            clean_name,
                            birth_year=(
                                payload.birth_year
                                if "birth_year" in fields_set
                                else _STUDIO_UNSET
                            ),
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
            elif "birth_year" in fields_set:
                try:
                    rename_person_action(
                        state,
                        person_id,
                        str(person.get("name") or ""),
                        birth_year=payload.birth_year,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

            if "is_ignored" in fields_set and payload.is_ignored is not None:
                try:
                    ignore_person_action(state, active_pid, ignored=bool(payload.is_ignored))
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

            if "priority" in fields_set and payload.priority is not None:
                state.update_person(active_pid, priority=int(payload.priority))

            updated = _get_formatted_person(state, active_pid)
        manager.invalidate_age_bridge_cache()
    return {"ok": True, "person": updated, "merged_into": merged_into}


@router.post("/persons/merge")
def api_people_merge_persons(payload: MergePersonsPayload):
    _ensure_not_running_for_studio()
    if payload.target_person_id == payload.source_person_id:
        raise HTTPException(status_code=400, detail="Нельзя объединить профиль с самим собой")
    with _studio_lock:
        _ensure_not_running_for_studio()
        with SorterState(paths.DB_FILE) as state:
            if (
                state.get_person(payload.target_person_id) is None
                or state.get_person(payload.source_person_id) is None
            ):
                raise HTTPException(status_code=404, detail="Один из выбранных профилей не найден")
            try:
                res = merge_persons_action(
                    state,
                    payload.target_person_id,
                    payload.source_person_id,
                    force=payload.force,
                )
            except ValueError as exc:
                if "cannot_link" in str(exc).lower():
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            person = _get_formatted_person(state, payload.target_person_id)
        manager.invalidate_age_bridge_cache()
    return {
        "ok": True,
        "person": person,
        "reassigned": int(res["reassigned_faces"]),
    }


@router.post("/persons/split")
def api_people_split_persons(payload: SplitFacesPayload):
    _ensure_not_running_for_studio()
    with _studio_lock:
        _ensure_not_running_for_studio()
        with SorterState(paths.DB_FILE) as state:
            try:
                res = split_faces_action(state, payload.face_ids, new_name=payload.new_name)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            new_pid = int(res["person"]["id"])
            new_person = _get_formatted_person(state, new_pid)
        manager.invalidate_age_bridge_cache()
    return {"ok": True, "new_person": new_person}


@router.post("/faces/{face_id}/reject")
def api_people_reject_face(face_id: int, payload: RejectFacePayload | None = None):
    _ensure_not_running_for_studio()
    rejected = payload.rejected if payload is not None else True
    with _studio_lock:
        _ensure_not_running_for_studio()
        with SorterState(paths.DB_FILE) as state:
            if state.get_face(face_id) is None:
                raise HTTPException(status_code=404, detail=f"Лицо #{face_id} не найдено")
            try:
                reject_face_action(state, face_id, rejected=rejected)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        manager.invalidate_age_bridge_cache()
    return {"ok": True}


@router.get("/age-bridges")
def api_people_age_bridges(top_k: Annotated[int, Query(ge=1, le=200)] = 25):
    with SorterState(paths.DB_FILE) as state:
        candidates = find_age_bridge_candidates(state, top_k=top_k)
    return {"items": [c.to_dict() for c in candidates]}


@router.post("/age-bridges/decision")
def api_people_age_bridge_decision(payload: AgeBridgeDecisionPayload):
    _ensure_not_running_for_studio()
    if payload.person_a_id == payload.person_b_id:
        raise HTTPException(status_code=400, detail="Указан один и тот же профиль")

    fields_set = payload.model_fields_set
    # Валидируем birth_year ДО merge_persons_action, чтобы некорректный ввод не оставил частичный merge
    if "birth_year" in fields_set and payload.birth_year is not None:
        if not (1850 <= payload.birth_year <= 2100):
            raise HTTPException(
                status_code=400,
                detail=f"Некорректный год рождения: {payload.birth_year} (допустимо 1850–2100)",
            )

    with _studio_lock:
        _ensure_not_running_for_studio()
        with SorterState(paths.DB_FILE) as state:
            persons_map = {int(p["id"]): p for p in state.list_persons(include_ignored=True)}
            p_a = persons_map.get(payload.person_a_id)
            p_b = persons_map.get(payload.person_b_id)
            if p_a is None or p_b is None:
                raise HTTPException(status_code=404, detail="Один из выбранных профилей не найден")

            if payload.decision == "reject":
                try:
                    reject_age_pair_action(state, payload.person_a_id, payload.person_b_id)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                manager.invalidate_age_bridge_cache()
                return {"ok": True, "decision": "reject"}

            a_named = bool(int(p_a.get("is_named", 0))) and bool(str(p_a.get("name") or "").strip())
            b_named = bool(int(p_b.get("is_named", 0))) and bool(str(p_b.get("name") or "").strip())
            if b_named and not a_named:
                target_id, source_id = payload.person_b_id, payload.person_a_id
            elif a_named and not b_named:
                target_id, source_id = payload.person_a_id, payload.person_b_id
            elif int(p_b.get("face_count", 0)) > int(p_a.get("face_count", 0)):
                target_id, source_id = payload.person_b_id, payload.person_a_id
            else:
                target_id, source_id = payload.person_a_id, payload.person_b_id

            try:
                res = merge_persons_action(state, target_id, source_id, force=False)
            except ValueError as exc:
                if "cannot_link" in str(exc).lower():
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            merged_name = (payload.merged_name or "").strip() if payload.merged_name else ""
            has_birth_year = "birth_year" in fields_set
            if merged_name or has_birth_year:
                cur_person = state.get_person(target_id) or {}
                cur_name = str(cur_person.get("name") or "")
                cur_birth = cur_person.get("birth_year")
                effective_name = merged_name or cur_name
                name_changed = bool(merged_name) and effective_name != cur_name
                birth_changed = has_birth_year and payload.birth_year != cur_birth
                if name_changed or birth_changed:
                    try:
                        rename_person_action(
                            state,
                            target_id,
                            effective_name,
                            birth_year=payload.birth_year if has_birth_year else _STUDIO_UNSET,
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc

            final_person = _get_formatted_person(state, target_id)
        manager.invalidate_age_bridge_cache()
    return {
        "ok": True,
        "decision": "merge",
        "person": final_person,
        "reassigned": int(res["reassigned_faces"]),
    }


@router.post("/studio/undo")
def api_people_studio_undo():
    _ensure_not_running_for_studio()
    with _studio_lock:
        _ensure_not_running_for_studio()
        with SorterState(paths.DB_FILE) as state:
            undone = undo_last_action(state)
        manager.invalidate_age_bridge_cache()
    return {"ok": True, "undone": undone}


@router.get("/faces/{face_id}/crop")
def api_people_face_crop(face_id: int):
    with SorterState(paths.DB_FILE) as state:
        face = state.get_face(face_id)
    if face is None:
        raise HTTPException(status_code=404, detail="Кроп лица не найден")
    resolved = _safe_cache_file(paths.CROPS_DIR, str(face.get("crop_rel_path") or ""))
    if resolved is None:
        raise HTTPException(status_code=404, detail="Файл кропа недоступен")
    return FileResponse(os.fspath(resolved), media_type="image/jpeg")


@router.get("/media/{media_id}/thumb")
def api_people_media_thumb(media_id: int):
    with SorterState(paths.DB_FILE) as state:
        media = state.get_media(media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="Превью медиафайла не найдено")
    resolved = _safe_cache_file(paths.THUMBS_DIR, str(media.get("thumb_rel_path") or ""))
    if resolved is None:
        raise HTTPException(status_code=404, detail="Файл превью недоступен")
    return FileResponse(os.fspath(resolved), media_type="image/jpeg")


@router.post("/plan/preview")
def api_people_plan_preview(payload: PlanPreviewPayload | None = None):
    req = payload if payload is not None else PlanPreviewPayload()
    try:
        options = _build_sort_options_from_payload(req)
        with SorterState(paths.DB_FILE) as state:
            plan = build_sort_plan(state, options)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    summary = plan.to_summary_dict()
    summary["sample_ops"] = [op.to_dict() for op in plan.ops[:80]]
    return summary


@router.post("/sort/start")
def api_people_sort_start(payload: SortStartPayload | None = None):
    req = payload if payload is not None else SortStartPayload()
    try:
        options = _build_sort_options_from_payload(req)
        manager.start_sort(options, allow_move=req.allow_move)
    except mgr_mod.TaskBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/runs")
def api_people_list_runs():
    with SorterState(paths.DB_FILE) as state:
        manager.reconcile_stale_runs(state)
        items = state.list_sort_runs(limit=30)
    return {"items": items}


@router.post("/runs/{run_id}/resume")
def api_people_resume_run(run_id: int, payload: ResumeRunPayload | None = None):
    req = payload if payload is not None else ResumeRunPayload()
    try:
        manager.start_resume(run_id, allow_move=req.allow_move)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except mgr_mod.TaskBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/runs/{run_id}/rollback")
def api_people_rollback_run(run_id: int):
    try:
        manager.start_rollback(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except mgr_mod.TaskBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}
