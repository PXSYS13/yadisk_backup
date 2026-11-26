"""Построение плана сортировки (Dry-Run) без изменения файлов на диске."""

from __future__ import annotations

import ctypes
import logging
import math
import os
import shutil
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from sorter.state import SorterState
from sorter.utils import (
    RUSSIAN_MONTHS,
    format_age_ru,
    human_bytes,
    is_path_inside,
    name_with_tag,
    path_key,
    sanitize_filename,
    sanitize_segment,
    strip_long_prefix,
    to_long_path,
    validate_dir_safe,
)

log = logging.getLogger("photo_sorter.organizer.planner")

SortMode = Literal["hardlink", "copy", "move"]
TreeLayout = Literal["people_first", "dual"]
GroupPolicy = Literal["each_person", "shared_folder", "priority"]
FallbackPolicy = Literal["folder", "skip"]

_VALID_MODES: frozenset[str] = frozenset({"hardlink", "copy", "move"})
_VALID_LAYOUTS: frozenset[str] = frozenset({"people_first", "dual"})
_VALID_GROUP_POLICIES: frozenset[str] = frozenset({"each_person", "shared_folder", "priority"})
_VALID_FALLBACK_POLICIES: frozenset[str] = frozenset({"folder", "skip"})

_TOP_LEVEL_ORDER: dict[str, int] = {
    "Люди": 0,
    "Совместные": 1,
    "По датам": 2,
    "Нераспознанные": 3,
    "Без_лиц": 4,
}

DISK_HEADROOM_BYTES = 100 * 1024 * 1024  # 100 МБ страховочный запас
SHORT_VIDEO_MAX_SEC = 3.5
VIDEO_SHARP_QUALITY_MIN = 0.60
MAX_CHILD_AGE_YEARS = 18
TREE_MAX_CHILDREN = 60


@dataclass
class SortOptions:
    """Параметры построения плана сортировки."""

    source_dir: str
    target_dir: str
    mode: SortMode = "hardlink"
    tree_layout: TreeLayout = "people_first"
    include_child_age: bool = True
    group_policy: GroupPolicy = "each_person"
    no_faces_policy: FallbackPolicy = "folder"
    unnamed_policy: FallbackPolicy = "folder"
    skip_ignored: bool = True
    preserve_dates: bool = True
    video_min_frames: int = 2
    video_single_sharp_conf: float = 0.62

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(f"Недопустимый режим сортировки: {self.mode!r}")
        if self.tree_layout not in _VALID_LAYOUTS:
            raise ValueError(f"Недопустимая структура дерева: {self.tree_layout!r}")
        if self.group_policy not in _VALID_GROUP_POLICIES:
            raise ValueError(f"Недопустимая политика групповых фото: {self.group_policy!r}")
        if self.no_faces_policy not in _VALID_FALLBACK_POLICIES:
            raise ValueError(f"Недопустимая политика для файлов без лиц: {self.no_faces_policy!r}")
        if self.unnamed_policy not in _VALID_FALLBACK_POLICIES:
            raise ValueError(f"Недопустимая политика для нераспознанных: {self.unnamed_policy!r}")
        self.video_min_frames = max(1, int(self.video_min_frames))
        self.video_single_sharp_conf = float(self.video_single_sharp_conf)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_dir": self.source_dir,
            "target_dir": self.target_dir,
            "mode": self.mode,
            "tree_layout": self.tree_layout,
            "include_child_age": bool(self.include_child_age),
            "group_policy": self.group_policy,
            "no_faces_policy": self.no_faces_policy,
            "unnamed_policy": self.unnamed_policy,
            "skip_ignored": bool(self.skip_ignored),
            "preserve_dates": bool(self.preserve_dates),
            "video_min_frames": int(self.video_min_frames),
            "video_single_sharp_conf": float(self.video_single_sharp_conf),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SortOptions:
        mode_raw = str(data.get("mode", "hardlink"))
        layout_raw = str(data.get("tree_layout", "people_first"))
        group_raw = str(data.get("group_policy", "each_person"))
        no_faces_raw = str(data.get("no_faces_policy", "folder"))
        unnamed_raw = str(data.get("unnamed_policy", "folder"))
        if mode_raw not in _VALID_MODES:
            raise ValueError(f"Недопустимый режим сортировки: {mode_raw!r}")
        if layout_raw not in _VALID_LAYOUTS:
            raise ValueError(f"Недопустимая структура дерева: {layout_raw!r}")
        if group_raw not in _VALID_GROUP_POLICIES:
            raise ValueError(f"Недопустимая политика групповых фото: {group_raw!r}")
        if no_faces_raw not in _VALID_FALLBACK_POLICIES:
            raise ValueError(f"Недопустимая политика для файлов без лиц: {no_faces_raw!r}")
        if unnamed_raw not in _VALID_FALLBACK_POLICIES:
            raise ValueError(f"Недопустимая политика для нераспознанных: {unnamed_raw!r}")
        return cls(
            source_dir=str(data.get("source_dir", "")),
            target_dir=str(data.get("target_dir", "")),
            mode=cast(SortMode, mode_raw),
            tree_layout=cast(TreeLayout, layout_raw),
            include_child_age=bool(data.get("include_child_age", True)),
            group_policy=cast(GroupPolicy, group_raw),
            no_faces_policy=cast(FallbackPolicy, no_faces_raw),
            unnamed_policy=cast(FallbackPolicy, unnamed_raw),
            skip_ignored=bool(data.get("skip_ignored", True)),
            preserve_dates=bool(data.get("preserve_dates", True)),
            video_min_frames=int(data.get("video_min_frames", 2)),
            video_single_sharp_conf=float(data.get("video_single_sharp_conf", 0.62)),
        )


@dataclass
class PlannedOp:
    """Одна запланированная файловая операция."""

    media_id: int
    person_id: int | None
    person_label: str
    op_type: str
    src_path: str
    dst_path: str
    rel_dst: str
    src_size: int
    shot_ts: float | None
    category: str  # 'person' | 'shared' | 'no_faces' | 'unnamed' | 'by_date'

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "person_id": self.person_id,
            "person_label": self.person_label,
            "op_type": self.op_type,
            "src_path": self.src_path,
            "dst_path": self.dst_path,
            "rel_dst": self.rel_dst,
            "src_size": self.src_size,
            "shot_ts": self.shot_ts,
            "category": self.category,
        }


@dataclass
class DryRunPlan:
    """Полный результат планирования (Dry-Run) с оценкой места и превью дерева."""

    source_dir: str
    target_dir: str
    requested_mode: str
    effective_mode: str
    mode_fallback_reason: str | None
    hardlink_supported: bool
    required_bytes: int
    free_bytes: int | None
    enough_space: bool
    ops: list[PlannedOp] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary_by_category: dict[str, int] = field(default_factory=dict)
    tree_preview: dict[str, Any] = field(default_factory=dict)
    options: SortOptions | None = None

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "source_dir": self.source_dir,
            "target_dir": self.target_dir,
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "mode_fallback_reason": self.mode_fallback_reason,
            "hardlink_supported": self.hardlink_supported,
            "total_ops": len(self.ops),
            "unique_media": len({op.media_id for op in self.ops}),
            "required_bytes": self.required_bytes,
            "free_bytes": self.free_bytes,
            "enough_space": self.enough_space,
            "warnings": list(self.warnings),
            "summary_by_category": dict(self.summary_by_category),
            "tree_preview": self.tree_preview,
            "options": self.options.to_dict() if self.options is not None else {},
        }


def _nearest_existing_dir(path: str) -> str | None:
    """Находит ближайшего существующего предка-каталога (для проверки тома и места)."""
    cur = os.path.abspath(strip_long_prefix(path))
    while True:
        lp = to_long_path(cur)
        try:
            if os.path.isdir(lp):
                return cur
        except OSError:
            pass
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            return None
        cur = parent


def _get_windows_filesystem_name(path: str) -> str | None:
    """Определяет имя файловой системы Windows (NTFS, FAT32, exFAT и т.д.) через WinAPI."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # Получаем точку монтирования тома (работает и для букв дисков, и для mount points)
        resolved = os.path.realpath(strip_long_prefix(path))
        vol_buf = ctypes.create_unicode_buffer(1024)
        get_vol_path = kernel32.GetVolumePathNameW
        get_vol_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_vol_path.restype = wintypes.BOOL
        if get_vol_path(resolved, vol_buf, len(vol_buf)):
            root_path = vol_buf.value
        else:
            drive, _ = os.path.splitdrive(resolved)
            if not drive:
                log.warning("_get_windows_filesystem_name(%s): не удалось определить том", path)
                return None
            root_path = drive + "\\"

        fs_buf = ctypes.create_unicode_buffer(256)
        get_vol_info = kernel32.GetVolumeInformationW
        get_vol_info.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        get_vol_info.restype = wintypes.BOOL
        ok = get_vol_info(root_path, None, 0, None, None, None, fs_buf, len(fs_buf))
        if not ok:
            log.warning(
                "_get_windows_filesystem_name(%s): GetVolumeInformationW failed (winerror=%s)",
                path,
                ctypes.get_last_error(),
            )
            return None
        fs_name = fs_buf.value.strip()
        if not fs_name:
            log.warning("_get_windows_filesystem_name(%s): пустое имя файловой системы", path)
            return None
        return fs_name
    except Exception as exc:  # noqa: BLE001
        log.warning("_get_windows_filesystem_name(%s) failed: %s", path, exc)
        return None


def check_hardlink_support(source_dir: str, target_dir: str) -> tuple[bool, str | None]:
    """Проверяет, поддерживаются ли жёсткие ссылки между source_dir и target_dir (read-only)."""
    src_ancestor = _nearest_existing_dir(source_dir)
    if src_ancestor is None:
        return False, "Исходная папка недоступна — включено безопасное копирование"
    dst_ancestor = _nearest_existing_dir(target_dir)
    if dst_ancestor is None:
        return False, "Целевой диск недоступен — включено безопасное копирование"

    try:
        src_st = os.stat(to_long_path(src_ancestor))
        dst_st = os.stat(to_long_path(dst_ancestor))
    except OSError as exc:
        return (
            False,
            f"Не удалось проверить файловую систему ({exc.strerror or exc}) — "
            "включено безопасное копирование",
        )

    if src_st.st_dev != dst_st.st_dev:
        return (
            False,
            "Исходная и целевая папки на разных дисках — включено безопасное копирование",
        )

    fs_name = _get_windows_filesystem_name(dst_ancestor)
    if fs_name is not None:
        upper = fs_name.upper()
        if upper in {"FAT", "FAT32", "EXFAT"} or upper not in {"NTFS", "UDF"}:
            return (
                False,
                f"Файловая система {fs_name} не поддерживает жёсткие ссылки — "
                "включено безопасное копирование",
            )

    return True, None


def _are_same_volume(source_dir: str, target_ancestor: str | None) -> bool:
    if target_ancestor is None:
        return False
    try:
        src_st = os.stat(to_long_path(source_dir))
        dst_st = os.stat(to_long_path(target_ancestor))
        return src_st.st_dev == dst_st.st_dev
    except OSError:
        return False


def _extract_year_month(shot_ts: float | None) -> tuple[int, int] | None:
    if shot_ts is None:
        return None
    try:
        val = float(shot_ts)
        if not math.isfinite(val):
            return None
        tm = time.gmtime(val)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if tm.tm_year < 1900 or tm.tm_year > 2200:
        return None
    return tm.tm_year, tm.tm_mon


def _date_segments(
    shot_ts: float | None,
    *,
    birth_year: int | None = None,
    include_child_age: bool = False,
) -> list[str]:
    ym = _extract_year_month(shot_ts)
    if ym is None:
        return ["Без_даты"]
    shot_year, shot_month = ym
    month_seg = RUSSIAN_MONTHS[shot_month]
    if include_child_age and birth_year is not None:
        age = shot_year - int(birth_year)
        if 0 <= age <= MAX_CHILD_AGE_YEARS:
            year_seg = f"{shot_year} ({format_age_ru(age)})"
        else:
            year_seg = str(shot_year)
    else:
        year_seg = str(shot_year)
    return [year_seg, month_seg]


def _video_person_passes(
    media: Mapping[str, Any],
    person_faces: list[dict[str, Any]],
    options: SortOptions,
) -> bool:
    """Фильтр достоверности атрибуции человека в видео (отсекает единичные ложные кадры)."""
    if not person_faces:
        return False
    if any(str(f.get("assign_source", "")) == "user" for f in person_faces):
        return True
    distinct_frames = {int(f.get("frame_ms", 0)) for f in person_faces}
    if len(distinct_frames) >= options.video_min_frames:
        return True
    duration = media.get("duration_sec")
    is_short = False
    if duration is not None:
        try:
            dur_val = float(duration)
            is_short = math.isfinite(dur_val) and dur_val <= SHORT_VIDEO_MAX_SEC
        except (TypeError, ValueError):
            is_short = False
    if is_short:
        for f in person_faces:
            quality = float(f.get("quality_score", 0.0))
            conf = float(f.get("assign_confidence", 0.0))
            if quality >= VIDEO_SHARP_QUALITY_MIN and conf >= options.video_single_sharp_conf:
                return True
    return False


def _is_named_person(person: Mapping[str, Any]) -> bool:
    return bool(person.get("is_named")) and bool(str(person.get("name") or "").strip())


def _build_person_folder_names(
    persons_by_id: Mapping[int, dict[str, Any]],
    warnings: list[str],
) -> dict[int, str]:
    """Сопоставляет person_id безопасное имя папки без коллизий между разными людьми."""
    ordered = sorted(
        persons_by_id.values(),
        key=lambda p: (int(p.get("priority", 100)), int(p["id"])),
    )
    used_keys: dict[str, int] = {}
    folder_names: dict[int, str] = {}
    for p in ordered:
        pid = int(p["id"])
        if not _is_named_person(p) or bool(p.get("is_ignored")):
            continue
        base = sanitize_segment(str(p.get("name") or ""), fallback=f"Человек_{pid}")
        candidate = base
        suffix = 2
        while candidate.casefold() in used_keys:
            candidate = sanitize_segment(f"{base}_{suffix}", fallback=f"Человек_{pid}")
            suffix += 1
        if candidate != base:
            warnings.append(
                f"Найдено совпадение имени папки «{base}» — для профиля #{pid} использована папка «{candidate}»"
            )
        used_keys[candidate.casefold()] = pid
        folder_names[pid] = candidate
    return folder_names


def _dst_exists_on_disk(dst_path: str) -> bool:
    try:
        return os.path.lexists(to_long_path(dst_path))
    except OSError:
        return True


_MAX_DEST_COLLISION_ATTEMPTS = 100_000


def _allocate_destination(
    target_dir: str,
    rel_dir_parts: list[str],
    base_filename: str,
    allocated_keys: set[str],
    media_id: int,
) -> tuple[str, str]:
    """Выбирает уникальный путь назначения с суффиксами _2, _3, ... при коллизиях."""
    candidate = base_filename
    counter = 2
    for _attempt in range(_MAX_DEST_COLLISION_ATTEMPTS):
        dst_path = os.path.join(target_dir, *rel_dir_parts, candidate)
        key = path_key(dst_path)
        if key not in allocated_keys and not _dst_exists_on_disk(dst_path):
            allocated_keys.add(key)
            rel_dst = "/".join([*rel_dir_parts, candidate])
            return dst_path, rel_dst
        if counter > 10_000:
            candidate = name_with_tag(base_filename, f"_{media_id}_{counter}")
        else:
            candidate = name_with_tag(base_filename, f"_{counter}")
        counter += 1
    raise RuntimeError(
        f"Превышен лимит попыток ({_MAX_DEST_COLLISION_ATTEMPTS}) подбора уникального имени "
        f"для файла {base_filename!r} в папке {'/'.join(rel_dir_parts) or target_dir}"
    )


@dataclass
class _TreeNode:
    name: str
    count: int = 0
    total_bytes: int = 0
    children: dict[str, _TreeNode] = field(default_factory=dict)


def _tree_node_to_dict(node: _TreeNode, *, depth: int = 0) -> dict[str, Any]:
    children_list = list(node.children.values())
    if depth == 0:
        children_list.sort(key=lambda c: (_TOP_LEVEL_ORDER.get(c.name, 99), c.name.casefold()))
    elif depth == 1 and node.name == "Люди":
        children_list.sort(key=lambda c: (-c.count, c.name.casefold()))
    else:
        children_list.sort(key=lambda c: c.name.casefold())

    serialized: list[dict[str, Any]] = []
    if len(children_list) > TREE_MAX_CHILDREN:
        visible = children_list[: TREE_MAX_CHILDREN - 1]
        overflow = children_list[TREE_MAX_CHILDREN - 1 :]
        for ch in visible:
            serialized.append(_tree_node_to_dict(ch, depth=depth + 1))
        serialized.append(
            {
                "name": f"... ещё {len(overflow)} папок",
                "count": sum(c.count for c in overflow),
                "bytes": sum(c.total_bytes for c in overflow),
                "children": [],
            }
        )
    else:
        for ch in children_list:
            serialized.append(_tree_node_to_dict(ch, depth=depth + 1))

    return {
        "name": node.name,
        "count": node.count,
        "bytes": node.total_bytes,
        "children": serialized,
    }


def _build_tree_preview(target_dir: str, ops: list[PlannedOp]) -> dict[str, Any]:
    root_name = os.path.basename(target_dir.rstrip("\\/")) or target_dir
    root = _TreeNode(name=root_name)
    for op in ops:
        root.count += 1
        root.total_bytes += op.src_size
        parts = op.rel_dst.split("/")[:-1]  # только папки без имени файла
        cur = root
        for seg in parts:
            child = cur.children.get(seg)
            if child is None:
                child = _TreeNode(name=seg)
                cur.children[seg] = child
            child.count += 1
            child.total_bytes += op.src_size
            cur = child
    return _tree_node_to_dict(root, depth=0)


def _validate_source_and_target(source_raw: str, target_raw: str) -> tuple[str, str]:
    source_dir = validate_dir_safe(source_raw, label="исходная папка")
    target_dir = validate_dir_safe(target_raw, label="целевая папка")

    if not os.path.isdir(to_long_path(source_dir)):
        raise ValueError(f"Исходная папка не существует или недоступна: {source_dir}")

    target_lp = to_long_path(target_dir)
    if os.path.exists(target_lp) and not os.path.isdir(target_lp):
        raise ValueError(f"Целевой путь указывает на файл, а не на папку: {target_dir}")

    src_real = path_key(os.path.realpath(to_long_path(source_dir)))
    dst_real = path_key(os.path.realpath(target_lp))
    if src_real == dst_real:
        raise ValueError(
            "Целевая папка совпадает с исходной — выберите отдельную папку для результата"
        )
    if is_path_inside(target_dir, source_dir):
        raise ValueError(
            "Целевая папка находится внутри исходной — это запрещено для защиты от рекурсии"
        )
    if is_path_inside(source_dir, target_dir):
        raise ValueError(
            "Исходная папка находится внутри целевой — выберите независимую целевую папку"
        )
    return source_dir, target_dir


def build_sort_plan(state: SorterState, options: SortOptions) -> DryRunPlan:
    """Строит полный детерминированный план сортировки без модификации диска."""
    source_dir, target_dir = _validate_source_and_target(options.source_dir, options.target_dir)
    normalized_options = SortOptions.from_dict(
        {**options.to_dict(), "source_dir": source_dir, "target_dir": target_dir}
    )

    warnings: list[str] = []
    requested_mode: str = normalized_options.mode
    hardlink_supported, hl_reason = check_hardlink_support(source_dir, target_dir)

    effective_mode = requested_mode
    mode_fallback_reason: str | None = None
    if requested_mode == "hardlink" and not hardlink_supported:
        effective_mode = "copy"
        mode_fallback_reason = (
            hl_reason or "Жёсткие ссылки не поддерживаются — включено безопасное копирование"
        )
        warnings.append(mode_fallback_reason)

    persons_list = state.list_persons(include_ignored=True)
    persons_by_id: dict[int, dict[str, Any]] = {int(p["id"]): p for p in persons_list}
    person_folder_names = _build_person_folder_names(persons_by_id, warnings)

    all_faces = state.list_faces(include_rejected=False)
    faces_by_media: dict[int, list[dict[str, Any]]] = {}
    for face in all_faces:
        faces_by_media.setdefault(int(face["media_id"]), []).append(face)

    all_media = state.list_media()
    allocated_keys: set[str] = set()
    ops: list[PlannedOp] = []
    summary_by_category: dict[str, int] = {
        "person": 0,
        "shared": 0,
        "no_faces": 0,
        "unnamed": 0,
        "by_date": 0,
    }

    missing_on_disk = 0
    scan_error_skipped = 0
    unscanned_no_faces = 0

    for media in all_media:
        src_path = str(media["local_path"])
        if not is_path_inside(src_path, source_dir):
            continue

        scan_status = str(media.get("scan_status") or "")
        if scan_status == "error":
            scan_error_skipped += 1
            continue

        src_lp = to_long_path(src_path)
        if not os.path.isfile(src_lp):
            missing_on_disk += 1
            continue

        media_id = int(media["id"])
        shot_ts = float(media["shot_ts"]) if media.get("shot_ts") is not None else None
        src_size = int(media.get("size") or 0)
        if src_size <= 0:
            try:
                src_size = int(os.stat(src_lp).st_size)
            except OSError:
                src_size = 0

        active_faces = faces_by_media.get(media_id, [])
        if scan_status == "pending" and not active_faces:
            unscanned_no_faces += 1

        # Определяем целевые папки (rel_dir_parts, person_id, person_label, category)
        destinations: list[tuple[list[str], int | None, str, str]] = []

        if not active_faces:
            if normalized_options.no_faces_policy == "folder":
                date_segs = _date_segments(shot_ts)
                destinations.append((["Без_лиц", *date_segs], None, "Без лиц", "no_faces"))
        else:
            media_kind = str(media.get("media_kind") or "photo")
            faces_by_pid: dict[int, list[dict[str, Any]]] = {}
            for face in active_faces:
                pid_raw = face.get("person_id")
                if pid_raw is not None:
                    pid = int(pid_raw)
                    if pid in persons_by_id:
                        faces_by_pid.setdefault(pid, []).append(face)

            attributed_pids: list[int] = []
            for pid, p_faces in faces_by_pid.items():
                if media_kind == "video":
                    if not _video_person_passes(media, p_faces, normalized_options):
                        continue
                attributed_pids.append(pid)

            named_persons = [
                persons_by_id[pid]
                for pid in attributed_pids
                if _is_named_person(persons_by_id[pid])
                and not bool(persons_by_id[pid].get("is_ignored"))
            ]
            named_persons.sort(key=lambda p: (int(p.get("priority", 100)), int(p["id"])))

            if named_persons:
                if normalized_options.group_policy == "shared_folder" and len(named_persons) >= 2:
                    date_segs = _date_segments(shot_ts)
                    destinations.append((["Совместные", *date_segs], None, "Совместные", "shared"))
                else:
                    chosen = (
                        [named_persons[0]]
                        if normalized_options.group_policy == "priority"
                        else named_persons
                    )
                    for person in chosen:
                        pid = int(person["id"])
                        folder_name = person_folder_names[pid]
                        birth_year = (
                            int(person["birth_year"])
                            if person.get("birth_year") is not None
                            else None
                        )
                        date_segs = _date_segments(
                            shot_ts,
                            birth_year=birth_year,
                            include_child_age=normalized_options.include_child_age,
                        )
                        destinations.append(
                            (
                                ["Люди", folder_name, *date_segs],
                                pid,
                                str(person.get("name") or folder_name),
                                "person",
                            )
                        )
            else:
                all_faces_ignored = all(
                    f.get("person_id") is not None
                    and int(f["person_id"]) in persons_by_id
                    and bool(persons_by_id[int(f["person_id"])].get("is_ignored"))
                    for f in active_faces
                )
                if all_faces_ignored and normalized_options.skip_ignored:
                    continue
                if normalized_options.unnamed_policy == "folder":
                    date_segs = _date_segments(shot_ts)
                    destinations.append(
                        (["Нераспознанные", *date_segs], None, "Нераспознанные", "unnamed")
                    )

        if not destinations:
            continue

        if normalized_options.tree_layout == "dual":
            date_segs = _date_segments(shot_ts)
            destinations.append((["По датам", *date_segs], None, "По датам", "by_date"))

        base_filename = sanitize_filename(
            os.path.basename(src_path), fallback_stem=f"media_{media_id}"
        )

        # Защита для move с несколькими назначениями:
        # первые k-1 назначений создаём через hardlink (если доступен) или copy,
        # и только ПОСЛЕДНЕЕ — через move, чтобы исходник не исчез раньше времени.
        total_dests = len(destinations)
        for idx, (rel_dir_parts, person_id, person_label, category) in enumerate(destinations):
            if effective_mode == "move" and total_dests > 1 and idx < total_dests - 1:
                op_type = "hardlink" if hardlink_supported else "copy"
            else:
                op_type = effective_mode

            dst_path, rel_dst = _allocate_destination(
                target_dir, rel_dir_parts, base_filename, allocated_keys, media_id
            )
            ops.append(
                PlannedOp(
                    media_id=media_id,
                    person_id=person_id,
                    person_label=person_label,
                    op_type=op_type,
                    src_path=src_path,
                    dst_path=dst_path,
                    rel_dst=rel_dst,
                    src_size=src_size,
                    shot_ts=shot_ts,
                    category=category,
                )
            )
            summary_by_category[category] = summary_by_category.get(category, 0) + 1

    if missing_on_disk > 0:
        warnings.append(f"Пропущено файлов, отсутствующих на диске: {missing_on_disk}")
    if scan_error_skipped > 0:
        warnings.append(f"Пропущено файлов с ошибкой сканирования: {scan_error_skipped}")
    if unscanned_no_faces > 0:
        warnings.append(
            f"Файлов без завершённого поиска лиц: {unscanned_no_faces} "
            "(они распределены по политике «Без_лиц»)"
        )

    target_ancestor = _nearest_existing_dir(target_dir)
    same_volume = _are_same_volume(source_dir, target_ancestor)

    required_bytes = 0
    for op in ops:
        if op.op_type == "copy":
            required_bytes += op.src_size
        elif op.op_type == "move" and not same_volume:
            required_bytes += op.src_size

    free_bytes: int | None = None
    if target_ancestor is not None:
        try:
            free_bytes = int(shutil.disk_usage(to_long_path(target_ancestor)).free)
        except OSError as exc:
            log.debug("disk_usage(%s) failed: %s", target_ancestor, exc)
            free_bytes = None

    if required_bytes == 0:
        enough_space = free_bytes is None or free_bytes > 0
    elif free_bytes is None:
        enough_space = False
        warnings.append(
            f"Не удалось определить свободное место на целевом диске "
            f"(требуется {human_bytes(required_bytes)})"
        )
    else:
        enough_space = free_bytes >= (required_bytes + DISK_HEADROOM_BYTES)
        if not enough_space:
            warnings.append(
                f"Недостаточно свободного места на диске: требуется {human_bytes(required_bytes)} "
                f"(+100 МБ запас), доступно {human_bytes(free_bytes)}"
            )

    tree_preview = _build_tree_preview(target_dir, ops)

    return DryRunPlan(
        source_dir=source_dir,
        target_dir=target_dir,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        mode_fallback_reason=mode_fallback_reason,
        hardlink_supported=hardlink_supported,
        required_bytes=required_bytes,
        free_bytes=free_bytes,
        enough_space=enough_space,
        ops=ops,
        warnings=warnings,
        summary_by_category=summary_by_category,
        tree_preview=tree_preview,
        options=normalized_options,
    )
