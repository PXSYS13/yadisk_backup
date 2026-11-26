"""Безопасное выполнение плана сортировки с журналированием в SQLite, кешем SHA-256 и resume."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sorter.organizer.planner import (
    DISK_HEADROOM_BYTES,
    DryRunPlan,
    _are_same_volume,
    _nearest_existing_dir,
    _validate_source_and_target,
    check_hardlink_support,
)
from sorter.state import SorterState
from sorter.utils import (
    CancelledError,
    apply_folder_dates,
    compute_sha256,
    human_bytes,
    is_fatal_storage_error,
    is_path_inside,
    is_symlink_or_junction,
    keep_awake,
    path_key,
    plausible_ts,
    set_file_time,
    to_long_path,
)

log = logging.getLogger("photo_sorter.organizer.executor")

_COPY_CHUNK_SIZE = 1024 * 1024  # 1 МиБ

_HARDLINK_FALLBACK_ERRNOS: frozenset[int] = frozenset(
    e
    for e in (
        errno.EXDEV,
        errno.EPERM,
        errno.EOPNOTSUPP,
        errno.EMLINK,
        getattr(errno, "ENOTSUP", None),
    )
    if e is not None
)
_HARDLINK_FALLBACK_WINERRORS: frozenset[int] = frozenset({1, 17, 50, 1142})


@dataclass
class ExecutionResult:
    """Итоги выполнения или довыполнения запуска сортировки."""

    run_id: int
    status: str  # 'completed' | 'partial' | 'cancelled'
    total_ops: int
    done_ops: int
    skipped_ops: int
    failed_ops: int
    effective_mode: str
    errors: list[str] = field(default_factory=list)
    fallback_copy_ops: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "total_ops": self.total_ops,
            "done_ops": self.done_ops,
            "skipped_ops": self.skipped_ops,
            "failed_ops": self.failed_ops,
            "effective_mode": self.effective_mode,
            "errors": list(self.errors),
            "fallback_copy_ops": self.fallback_copy_ops,
            "warnings": list(self.warnings),
        }


def _should_fallback_hardlink_to_copy(
    exc: OSError,
    *,
    source_dir: str = "",
    target_dir: str = "",
) -> bool:
    winerr = getattr(exc, "winerror", None)
    if winerr is not None and int(winerr) in _HARDLINK_FALLBACK_WINERRORS:
        return True
    err_no = getattr(exc, "errno", None)
    if err_no is not None and int(err_no) in _HARDLINK_FALLBACK_ERRNOS:
        return True
    is_access_denied = (winerr is not None and int(winerr) == 5) or (
        err_no is not None and int(err_no) == errno.EACCES
    )
    if is_access_denied and source_dir and target_dir:
        supported, _reason = check_hardlink_support(source_dir, target_dir)
        return not supported
    return False


def _remove_part_quietly(part_path: str) -> None:
    try:
        lp = to_long_path(part_path)
        if os.path.lexists(lp):
            os.unlink(lp)
    except OSError as exc:
        log.debug("Failed to cleanup temp file %s: %s", part_path, exc)


def _atomic_copy_and_hash(
    src_path: str,
    dst_path: str,
    *,
    expected_sha256: str | None = None,
    stop_event: threading.Event | None = None,
) -> str:
    """Потоково копирует src_path во временный .part с проверкой SHA-256 и атомарно заменяет dst_path."""
    dst_parent = os.path.dirname(dst_path)
    src_lp = to_long_path(src_path)
    dst_lp = to_long_path(dst_path)

    fd, part_path = tempfile.mkstemp(
        dir=to_long_path(dst_parent),
        prefix=".sorter.",
        suffix=".part",
    )
    part_lp = to_long_path(part_path)
    hasher = hashlib.sha256()
    try:
        with open(src_lp, "rb") as rf, os.fdopen(fd, "wb") as wf:
            fd = -1
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise CancelledError("Остановлено пользователем во время копирования")
                chunk = rf.read(_COPY_CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                wf.write(chunk)
            wf.flush()

        copied_sha = hasher.hexdigest()
        if expected_sha256 is not None:
            if copied_sha != expected_sha256:
                raise OSError(
                    f"Контрольная сумма SHA-256 не совпала при копировании {src_path} -> {dst_path}"
                )
            # Повторно сверяем записанный .part файл на диске ДО публикации в dst_path
            written_sha = compute_sha256(part_path, stop_event=stop_event)
            if written_sha != expected_sha256:
                raise OSError(
                    f"Контрольная сумма SHA-256 временного файла не совпала при перемещении "
                    f"{src_path} -> {dst_path}"
                )

        os.replace(part_lp, dst_lp)
        return copied_sha
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        _remove_part_quietly(part_path)
        raise


def _stat_inode_str(path: str) -> str:
    st = os.lstat(to_long_path(path))
    return f"{st.st_dev}:{st.st_ino}"


def _record_folder_dates(
    folder_bounds: dict[str, tuple[str, float, float]],
    dst_path: str,
    target_dir: str,
    source_dir: str,
    shot_ts: float | None,
) -> None:
    valid_ts = plausible_ts(shot_ts)
    if valid_ts is None:
        return
    target_key = path_key(target_dir)
    source_key = path_key(source_dir)
    cur = os.path.dirname(dst_path)
    while cur:
        cur_key = path_key(cur)
        if cur_key == target_key or cur_key == source_key:
            break
        if not is_path_inside(cur, target_dir) or is_path_inside(cur, source_dir):
            break
        prev = folder_bounds.get(cur_key)
        if prev is None:
            folder_bounds[cur_key] = (cur, valid_ts, valid_ts)
        else:
            folder_bounds[cur_key] = (cur, min(prev[1], valid_ts), max(prev[2], valid_ts))
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent


def _build_media_lookup(
    state: SorterState,
    extra_shot_ts: dict[int, float | None] | None = None,
) -> dict[int, dict[str, Any]]:
    lookup: dict[int, dict[str, Any]] = {}
    for row in state.list_media():
        mid = int(row["id"])
        lookup[mid] = {
            "size": int(row.get("size") or 0),
            "mtime_ns": int(row.get("mtime_ns") or 0),
            "sha256": str(row.get("sha256") or ""),
            "shot_ts": float(row["shot_ts"]) if row.get("shot_ts") is not None else None,
        }
    if extra_shot_ts:
        for mid, ts in extra_shot_ts.items():
            entry = lookup.get(mid)
            if entry is None:
                lookup[mid] = {"size": 0, "mtime_ns": 0, "sha256": "", "shot_ts": ts}
            elif entry["shot_ts"] is None and ts is not None:
                entry["shot_ts"] = ts
    return lookup


def _get_or_compute_sha256(
    state: SorterState,
    media_id: int | None,
    src_path: str,
    src_st: os.stat_result,
    sha_cache: dict[int, str],
    media_lookup: dict[int, dict[str, Any]],
    *,
    stop_event: threading.Event | None = None,
) -> str:
    if media_id is not None and media_id in sha_cache:
        return sha_cache[media_id]

    if media_id is not None:
        meta = media_lookup.get(media_id)
        if (
            meta is not None
            and meta["sha256"]
            and meta["size"] == int(src_st.st_size)
            and meta["mtime_ns"] == int(src_st.st_mtime_ns)
        ):
            sha_cache[media_id] = str(meta["sha256"])
            return sha_cache[media_id]

    digest = compute_sha256(src_path, stop_event=stop_event)
    if media_id is not None:
        sha_cache[media_id] = digest
        state.update_media_sha256(media_id, digest)
    return digest


def _move_file_safely(
    src_path: str,
    dst_path: str,
    expected_sha256: str,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Атомарно перемещает файл: rename в пределах тома или copy+SHA256(.part)+replace+unlink между томами."""
    src_lp = to_long_path(src_path)
    dst_lp = to_long_path(dst_path)
    src_dir_st = os.stat(to_long_path(os.path.dirname(src_path)))
    dst_dir_st = os.stat(to_long_path(os.path.dirname(dst_path)))

    if src_dir_st.st_dev == dst_dir_st.st_dev:
        try:
            os.replace(src_lp, dst_lp)
            return
        except OSError as exc:
            winerr = getattr(exc, "winerror", None)
            if exc.errno != errno.EXDEV and winerr != 17:
                raise

    # Межтомовое перемещение: копируем в .part, сверяем SHA-256 на .part ДО replace, затем удаляем src.
    _atomic_copy_and_hash(
        src_path,
        dst_path,
        expected_sha256=expected_sha256,
        stop_event=stop_event,
    )
    try:
        os.unlink(src_lp)
    except BaseException:
        try:
            os.unlink(dst_lp)
        except OSError as cleanup_exc:
            log.warning(
                "Не удалось удалить целевую копию %s после сбоя удаления исходника %s: %s",
                dst_path,
                src_path,
                cleanup_exc,
            )
        raise


def _check_free_space_for_ops(
    source_dir: str,
    target_dir: str,
    ops: list[dict[str, Any]],
) -> None:
    target_ancestor = _nearest_existing_dir(target_dir)
    same_volume = _are_same_volume(source_dir, target_ancestor)

    required_bytes = 0
    for op in ops:
        op_type = str(op.get("op_type") or "")
        size = int(op.get("src_size") or 0)
        if op_type == "copy":
            required_bytes += size
        elif op_type == "move" and not same_volume:
            required_bytes += size

    if required_bytes <= 0:
        return

    if target_ancestor is None:
        raise RuntimeError(
            f"Целевой диск недоступен для проверки свободного места (требуется {human_bytes(required_bytes)})"
        )
    try:
        free_bytes = int(shutil.disk_usage(to_long_path(target_ancestor)).free)
    except OSError as exc:
        raise RuntimeError(
            f"Не удалось определить свободное место на целевом диске ({exc})"
        ) from exc

    if free_bytes < required_bytes + DISK_HEADROOM_BYTES:
        raise RuntimeError(
            f"Недостаточно свободного места на диске: требуется {human_bytes(required_bytes)} "
            f"(+100 МБ запас), доступно {human_bytes(free_bytes)}"
        )


def _execute_run_operations(
    state: SorterState,
    run_id: int,
    *,
    extra_shot_ts: dict[int, float | None] | None = None,
    stop_event: threading.Event | None = None,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> ExecutionResult:
    run = state.get_sort_run(run_id)
    if run is None:
        raise ValueError(f"Запуск сортировки #{run_id} не найден")

    source_dir = str(run["source_dir"])
    target_dir = str(run["target_dir"])
    effective_mode = str(run["effective_mode"])
    raw_options = run.get("options")
    options: dict[str, Any] = raw_options if isinstance(raw_options, dict) else {}
    preserve_dates = bool(options.get("preserve_dates", True))

    all_ops = state.list_run_operations(run_id)
    total_ops = len(all_ops)
    pending_ops = [op for op in all_ops if op["status"] == "pending"]
    failed_media_ids: set[int] = {
        int(op["media_id"])
        for op in all_ops
        if op["status"] == "failed" and op.get("media_id") is not None
    }
    errors: list[str] = [
        str(op["error"]) for op in all_ops if op["status"] == "failed" and op.get("error")
    ]
    warnings: list[str] = []

    media_lookup = _build_media_lookup(state, extra_shot_ts)
    sha_cache: dict[int, str] = {}
    folder_bounds: dict[str, tuple[str, float, float]] = {}

    done_live = sum(1 for op in all_ops if op["status"] == "done")
    skipped_live = sum(1 for op in all_ops if op["status"] == "skipped")
    failed_live = sum(1 for op in all_ops if op["status"] == "failed")
    processed = done_live + skipped_live + failed_live

    cancelled = False
    finalized = False

    def _emit_progress(op_type: str, dst_path: str, error_text: str | None = None) -> None:
        if progress_cb is None:
            return
        payload: dict[str, Any] = {
            "run_id": run_id,
            "processed": processed,
            "total": total_ops,
            "done_ops": done_live,
            "skipped_ops": skipped_live,
            "failed_ops": failed_live,
            "op_type": op_type,
            "dst_path": dst_path,
        }
        if error_text is not None:
            payload["error"] = error_text
        progress_cb(payload)

    try:
        with keep_awake():
            for op in pending_ops:
                if stop_event is not None and stop_event.is_set():
                    cancelled = True
                    break

                op_id = int(op["id"])
                op_type = str(op["op_type"])
                media_id = int(op["media_id"]) if op.get("media_id") is not None else None
                src_path = str(op["src_path"])
                dst_path = str(op["dst_path"])
                shot_ts: float | None = (
                    media_lookup[media_id]["shot_ts"]
                    if media_id is not None and media_id in media_lookup
                    else None
                )

                try:
                    if not is_path_inside(dst_path, target_dir) or path_key(dst_path) == path_key(
                        target_dir
                    ):
                        raise ValueError(
                            f"Целевой путь выходит за пределы целевой папки: {dst_path}"
                        )
                    if not is_path_inside(src_path, source_dir) or path_key(src_path) == path_key(
                        source_dir
                    ):
                        raise ValueError(
                            f"Исходный путь выходит за пределы исходной папки: {src_path}"
                        )

                    if op_type == "move" and media_id is not None and media_id in failed_media_ids:
                        raise RuntimeError(
                            "Перемещение пропущено из-за ошибки предыдущей операции для этого файла"
                        )

                    src_lp = to_long_path(src_path)
                    try:
                        src_st = os.lstat(src_lp)
                    except FileNotFoundError as exc:
                        raise FileNotFoundError(f"Исходный файл не найден: {src_path}") from exc
                    if is_symlink_or_junction(src_path, src_st) or not stat.S_ISREG(src_st.st_mode):
                        raise OSError(
                            f"Исходный путь не является обычным файлом (симлинк/junction): {src_path}"
                        )

                    dst_parent = os.path.dirname(dst_path)
                    os.makedirs(to_long_path(dst_parent), exist_ok=True)
                    dst_lp = to_long_path(dst_path)

                    if op_type == "hardlink":
                        src_sha = _get_or_compute_sha256(
                            state,
                            media_id,
                            src_path,
                            src_st,
                            sha_cache,
                            media_lookup,
                            stop_event=stop_event,
                        )
                        fallback_note = ""
                        fell_back_to_copy = False
                        if os.path.lexists(dst_lp):
                            existing_st = os.lstat(dst_lp)
                            if (
                                not is_symlink_or_junction(dst_path, existing_st)
                                and stat.S_ISREG(existing_st.st_mode)
                                and existing_st.st_dev == src_st.st_dev
                                and existing_st.st_ino == src_st.st_ino
                            ):
                                dst_inode = f"{existing_st.st_dev}:{existing_st.st_ino}"
                                if preserve_dates and shot_ts is not None:
                                    _record_folder_dates(
                                        folder_bounds, dst_path, target_dir, source_dir, shot_ts
                                    )
                                state.update_operation_status(
                                    op_id, "done", src_sha256=src_sha, dst_inode=dst_inode
                                )
                                done_live += 1
                                processed += 1
                                _emit_progress(op_type, dst_path)
                                continue
                            raise FileExistsError(f"Целевой файл уже существует: {dst_path}")

                        try:
                            os.link(src_lp, dst_lp)
                        except OSError as link_exc:
                            if not _should_fallback_hardlink_to_copy(
                                link_exc, source_dir=source_dir, target_dir=target_dir
                            ):
                                raise
                            try:
                                free_bytes = int(shutil.disk_usage(to_long_path(dst_parent)).free)
                            except OSError as du_exc:
                                raise OSError(
                                    errno.ENOSPC,
                                    f"Не удалось проверить свободное место на диске перед копированием: {du_exc}",
                                ) from du_exc
                            needed_bytes = int(src_st.st_size) + DISK_HEADROOM_BYTES
                            if free_bytes < needed_bytes:
                                raise OSError(
                                    errno.ENOSPC,
                                    f"Недостаточно свободного места для резервного копирования "
                                    f"(требуется {human_bytes(src_st.st_size)} + 100 МБ запас, "
                                    f"доступно {human_bytes(free_bytes)})",
                                ) from link_exc

                            log.warning(
                                "Hardlink fallback to copy for %s -> %s: %s",
                                src_path,
                                dst_path,
                                link_exc,
                            )
                            copied_sha = _atomic_copy_and_hash(
                                src_path, dst_path, stop_event=stop_event
                            )
                            src_sha = copied_sha
                            if media_id is not None:
                                sha_cache[media_id] = copied_sha
                                state.update_media_sha256(media_id, copied_sha)
                            fell_back_to_copy = True
                            fallback_note = f"fallback:copy ({link_exc.strerror or link_exc})"

                        if preserve_dates and shot_ts is not None:
                            if fell_back_to_copy:
                                set_file_time(dst_path, shot_ts, created=shot_ts)
                            _record_folder_dates(
                                folder_bounds, dst_path, target_dir, source_dir, shot_ts
                            )

                        dst_inode = _stat_inode_str(dst_path)
                        state.update_operation_status(
                            op_id,
                            "done",
                            src_sha256=src_sha,
                            dst_inode=dst_inode,
                            error=fallback_note,
                        )

                    elif op_type == "copy":
                        if os.path.lexists(dst_lp):
                            raise FileExistsError(f"Целевой файл уже существует: {dst_path}")
                        copied_sha = _atomic_copy_and_hash(
                            src_path, dst_path, stop_event=stop_event
                        )
                        if media_id is not None:
                            sha_cache[media_id] = copied_sha
                            state.update_media_sha256(media_id, copied_sha)
                        if preserve_dates and shot_ts is not None:
                            set_file_time(dst_path, shot_ts, created=shot_ts)
                            _record_folder_dates(
                                folder_bounds, dst_path, target_dir, source_dir, shot_ts
                            )
                        dst_inode = _stat_inode_str(dst_path)
                        state.update_operation_status(
                            op_id, "done", src_sha256=copied_sha, dst_inode=dst_inode
                        )

                    elif op_type == "move":
                        if os.path.lexists(dst_lp):
                            raise FileExistsError(f"Целевой файл уже существует: {dst_path}")
                        src_sha = _get_or_compute_sha256(
                            state,
                            media_id,
                            src_path,
                            src_st,
                            sha_cache,
                            media_lookup,
                            stop_event=stop_event,
                        )
                        _move_file_safely(src_path, dst_path, src_sha, stop_event=stop_event)
                        if preserve_dates and shot_ts is not None:
                            set_file_time(dst_path, shot_ts, created=shot_ts)
                            _record_folder_dates(
                                folder_bounds, dst_path, target_dir, source_dir, shot_ts
                            )
                        dst_inode = _stat_inode_str(dst_path)
                        state.update_operation_status(
                            op_id, "done", src_sha256=src_sha, dst_inode=dst_inode
                        )

                    else:
                        raise ValueError(f"Неизвестный тип операции: {op_type!r}")

                    done_live += 1
                    processed += 1
                    _emit_progress(op_type, dst_path)

                except CancelledError:
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001
                    if is_fatal_storage_error(exc):
                        raise
                    err_msg = f"{os.path.basename(src_path)}: {exc}"
                    log.warning("Sort op #%d failed (%s -> %s): %s", op_id, src_path, dst_path, exc)
                    errors.append(err_msg)
                    if media_id is not None:
                        failed_media_ids.add(media_id)
                    state.update_operation_status(op_id, "failed", error=err_msg)
                    failed_live += 1
                    processed += 1
                    _emit_progress(op_type, dst_path, error_text=err_msg)

        if preserve_dates and folder_bounds:
            safe_folder_dates = [
                (folder_path, min_ts, max_ts)
                for folder_path, min_ts, max_ts in folder_bounds.values()
                if is_path_inside(folder_path, target_dir)
                and not is_path_inside(folder_path, source_dir)
                and path_key(folder_path) != path_key(target_dir)
            ]
            if safe_folder_dates:
                apply_folder_dates(safe_folder_dates)

        # Перечитываем итоговые счётчики из БД
        refreshed_ops = state.list_run_operations(run_id)
        done_cnt = sum(1 for op in refreshed_ops if op["status"] == "done")
        skipped_cnt = sum(1 for op in refreshed_ops if op["status"] == "skipped")
        failed_cnt = sum(1 for op in refreshed_ops if op["status"] == "failed")
        pending_cnt = sum(1 for op in refreshed_ops if op["status"] == "pending")
        rolled_back_cnt = sum(1 for op in refreshed_ops if op["status"] == "rolled_back")
        fallback_copy_ops = sum(
            1
            for op in refreshed_ops
            if op["status"] == "done" and str(op.get("error") or "").startswith("fallback:copy")
        )
        if fallback_copy_ops > 0:
            warnings.append(
                f"Для {fallback_copy_ops} опер. выполнен резервный переход с жёсткой ссылки на копирование"
            )

        if cancelled:
            final_status = "cancelled"
        elif failed_cnt == 0 and pending_cnt == 0 and rolled_back_cnt == 0:
            final_status = "completed"
        else:
            final_status = "partial"

        state.refresh_sort_run_counts(run_id, status=final_status, finished=not cancelled)
        finalized = True

        return ExecutionResult(
            run_id=run_id,
            status=final_status,
            total_ops=len(refreshed_ops),
            done_ops=done_cnt,
            skipped_ops=skipped_cnt,
            failed_ops=failed_cnt,
            effective_mode=effective_mode,
            errors=errors,
            fallback_copy_ops=fallback_copy_ops,
            warnings=warnings,
        )
    finally:
        if not finalized:
            try:
                state.refresh_sort_run_counts(run_id, status="partial", finished=True)
            except Exception as fin_exc:  # noqa: BLE001
                log.error(
                    "Не удалось перевести sort_run #%d в статус 'partial' после фатальной ошибки: %s",
                    run_id,
                    fin_exc,
                )


def execute_sort_plan(
    state: SorterState,
    plan: DryRunPlan,
    *,
    allow_move: bool = False,
    stop_event: threading.Event | None = None,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> ExecutionResult:
    """Создаёт журнал sort_run в SQLite и выполняет все запланированные операции."""
    has_move = plan.effective_mode == "move" or any(op.op_type == "move" for op in plan.ops)
    if has_move and not allow_move:
        raise ValueError(
            "Режим перемещения (move) изменяет исходную папку и требует явного "
            "подтверждения allow_move=True"
        )

    if not plan.enough_space:
        raise RuntimeError(
            f"Недостаточно свободного места на диске: требуется {human_bytes(plan.required_bytes)} "
            f"(+100 МБ запас), доступно {human_bytes(plan.free_bytes)}"
        )

    _validate_source_and_target(plan.source_dir, plan.target_dir)

    options_dict = (
        plan.options.to_dict()
        if plan.options is not None
        else {
            "source_dir": plan.source_dir,
            "target_dir": plan.target_dir,
            "mode": plan.requested_mode,
            "preserve_dates": True,
        }
    )

    db_ops = [
        {
            "media_id": op.media_id,
            "person_id": op.person_id,
            "op_type": op.op_type,
            "src_path": op.src_path,
            "dst_path": op.dst_path,
            "src_size": op.src_size,
            "src_sha256": "",
        }
        for op in plan.ops
    ]

    run_id = state.create_sort_run(
        source_dir=plan.source_dir,
        target_dir=plan.target_dir,
        mode=plan.requested_mode,
        effective_mode=plan.effective_mode,
        options=options_dict,
        ops=db_ops,
    )

    extra_shot_ts = {op.media_id: op.shot_ts for op in plan.ops}
    return _execute_run_operations(
        state,
        run_id,
        extra_shot_ts=extra_shot_ts,
        stop_event=stop_event,
        progress_cb=progress_cb,
    )


def resume_sort_run(
    state: SorterState,
    run_id: int,
    *,
    allow_move: bool = False,
    stop_event: threading.Event | None = None,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> ExecutionResult:
    """Продолжает выполнение незавершённых ('pending') операций прерванного запуска."""
    run = state.get_sort_run(run_id)
    if run is None:
        raise ValueError(f"Запуск сортировки #{run_id} не найден")
    if run["status"] == "rolled_back":
        raise ValueError(f"Запуск сортировки #{run_id} уже откачен и не может быть продолжен")

    all_ops = state.list_run_operations(run_id)
    if any(op["status"] == "rolled_back" for op in all_ops):
        raise ValueError(
            f"Запуск сортировки #{run_id} содержит откаченные операции и не может быть продолжен"
        )
    pending_ops = [op for op in all_ops if op["status"] == "pending"]
    if not pending_ops:
        raise ValueError(f"В запуске сортировки #{run_id} нет незавершённых операций для продолжения")

    has_pending_move = any(str(op.get("op_type") or "") == "move" for op in pending_ops)
    if has_pending_move and not allow_move:
        raise ValueError(
            "Возобновление операций перемещения (move) изменяет исходную папку и требует явного "
            "подтверждения allow_move=True"
        )

    source_dir, target_dir = _validate_source_and_target(
        str(run["source_dir"]), str(run["target_dir"])
    )
    _check_free_space_for_ops(source_dir, target_dir, pending_ops)

    state.refresh_sort_run_counts(run_id, status="running", finished=False)
    return _execute_run_operations(
        state,
        run_id,
        extra_shot_ts=None,
        stop_event=stop_event,
        progress_cb=progress_cb,
    )
