"""Безопасный откат запуска сортировки с проверкой inode/SHA-256 и очисткой пустых папок."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sorter.state import SorterState
from sorter.utils import (
    CancelledError,
    compute_sha256,
    is_fatal_storage_error,
    is_path_inside,
    is_symlink_or_junction,
    path_key,
    to_long_path,
    validate_dir_safe,
)

log = logging.getLogger("photo_sorter.organizer.rollback")

_RESTORE_CHUNK_SIZE = 1024 * 1024  # 1 МиБ


@dataclass
class RollbackResult:
    """Подробный отчёт об откате запуска сортировки."""

    run_id: int
    status: str  # 'rolled_back' | 'partial'
    reverted: int = 0
    skipped_modified: int = 0
    already_missing: int = 0
    failed: int = 0
    removed_dirs: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "reverted": self.reverted,
            "skipped_modified": self.skipped_modified,
            "already_missing": self.already_missing,
            "failed": self.failed,
            "removed_dirs": self.removed_dirs,
            "warnings": list(self.warnings),
        }


def _remove_part_quietly(part_path: str) -> None:
    try:
        lp = to_long_path(part_path)
        if os.path.lexists(lp):
            os.unlink(lp)
    except OSError as exc:
        log.debug("Failed to cleanup temp restore file %s: %s", part_path, exc)


def _restore_moved_file(
    dst_path: str,
    src_path: str,
    expected_sha256: str,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Атомарно возвращает перемещённый файл из dst_path обратно в src_path."""
    src_parent = os.path.dirname(src_path)
    os.makedirs(to_long_path(src_parent), exist_ok=True)

    dst_lp = to_long_path(dst_path)
    src_lp = to_long_path(src_path)

    dst_dir_st = os.stat(to_long_path(os.path.dirname(dst_path)))
    src_dir_st = os.stat(to_long_path(src_parent))
    if dst_dir_st.st_dev == src_dir_st.st_dev:
        try:
            os.replace(dst_lp, src_lp)
            return
        except OSError as exc:
            winerr = getattr(exc, "winerror", None)
            if exc.errno != errno.EXDEV and winerr != 17:
                raise

    # Межтомовое восстановление: потоковая копия в случайный .part + сверка SHA-256 + replace + unlink dst
    fd, part_path = tempfile.mkstemp(
        dir=to_long_path(src_parent),
        prefix=".sorter.",
        suffix=".part",
    )
    part_lp = to_long_path(part_path)
    hasher = hashlib.sha256()
    try:
        with open(dst_lp, "rb") as rf, os.fdopen(fd, "wb") as wf:
            fd = -1
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise CancelledError("Откат остановлен пользователем")
                chunk = rf.read(_RESTORE_CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                wf.write(chunk)
            wf.flush()
        actual_sha = hasher.hexdigest()
        if actual_sha != expected_sha256:
            raise OSError("Контрольная сумма SHA-256 не совпала при возврате файла")
        written_sha = compute_sha256(part_path, stop_event=stop_event)
        if written_sha != expected_sha256:
            raise OSError("Контрольная сумма SHA-256 временного файла не совпала при возврате")
        os.replace(part_lp, src_lp)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        _remove_part_quietly(part_path)
        raise
    os.unlink(dst_lp)


def _collect_parent_dirs(dst_path: str, target_dir: str, out_dirs: dict[str, str]) -> None:
    target_key = path_key(target_dir)
    cur = os.path.dirname(dst_path)
    while cur:
        cur_key = path_key(cur)
        if cur_key == target_key:
            break
        if not is_path_inside(cur, target_dir):
            break
        out_dirs.setdefault(cur_key, cur)
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent


def _prune_empty_dirs(candidate_dirs: dict[str, str], target_dir: str) -> int:
    """Удаляет пустые подпапки снизу вверх строго внутри target_dir (без симлинков/junction)."""
    target_key = path_key(target_dir)
    ordered = sorted(
        candidate_dirs.values(),
        key=lambda p: (len(Path(p).parts), len(p)),
        reverse=True,
    )
    removed = 0
    for folder in ordered:
        if path_key(folder) == target_key or not is_path_inside(folder, target_dir):
            continue
        lp = to_long_path(folder)
        try:
            st = os.lstat(lp)
            if is_symlink_or_junction(folder, st) or not stat.S_ISDIR(st.st_mode):
                continue
            os.rmdir(lp)
            removed += 1
        except OSError:
            continue
    return removed


def rollback_sort_run(
    state: SorterState,
    run_id: int,
    *,
    stop_event: threading.Event | None = None,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> RollbackResult:
    """Безопасно откатывает выполненные операции запуска в обратном порядке (id DESC)."""
    run = state.get_sort_run(run_id)
    if run is None:
        raise ValueError(f"Запуск сортировки #{run_id} не найден")

    source_dir = validate_dir_safe(str(run["source_dir"]), label="исходная папка")
    target_dir = validate_dir_safe(str(run["target_dir"]), label="целевая папка")

    done_ops = state.list_run_operations(run_id, status="done")
    done_ops.reverse()  # Строго обратный порядок (id DESC)

    has_move_ops = str(run.get("effective_mode") or "") == "move" or any(
        str(op.get("op_type") or "") == "move" for op in done_ops
    )
    if not os.path.isdir(to_long_path(target_dir)):
        raise RuntimeError(f"Папка недоступна (диск отключён?): {target_dir}")
    if has_move_ops and not os.path.isdir(to_long_path(source_dir)):
        raise RuntimeError(f"Папка недоступна (диск отключён?): {source_dir}")

    reverted = 0
    skipped_modified = 0
    already_missing = 0
    failed = 0
    warnings: list[str] = []
    candidate_dirs: dict[str, str] = {}
    cancelled = False
    finalized = False

    total = len(done_ops)

    def _emit_progress(idx: int, dst_path: str) -> None:
        if progress_cb is None:
            return
        progress_cb(
            {
                "run_id": run_id,
                "processed": idx,
                "total": total,
                "done_ops": reverted,
                "skipped_ops": skipped_modified + already_missing,
                "failed_ops": failed,
                "reverted": reverted,
                "skipped_modified": skipped_modified,
                "failed": failed,
                "dst_path": dst_path,
            }
        )

    try:
        for idx, op in enumerate(done_ops, start=1):
            if stop_event is not None and stop_event.is_set():
                cancelled = True
                warnings.append("Откат остановлен пользователем до завершения")
                break

            op_id = int(op["id"])
            raw_op_type = str(op["op_type"])
            src_path = str(op["src_path"])
            dst_path = str(op["dst_path"])
            expected_sha = str(op.get("src_sha256") or "")
            expected_inode = str(op.get("dst_inode") or "")
            op_note = str(op.get("error") or "")

            # Обязательная проверка: не удаляем ничего вне целевой папки
            if not is_path_inside(dst_path, target_dir) or path_key(dst_path) == path_key(
                target_dir
            ):
                failed += 1
                warnings.append(f"Отказано в удалении файла вне целевой папки: {dst_path}")
                _emit_progress(idx, dst_path)
                continue

            _collect_parent_dirs(dst_path, target_dir, candidate_dirs)

            effective_type = (
                "copy"
                if raw_op_type == "hardlink" and op_note.startswith("fallback:copy")
                else raw_op_type
            )

            dst_lp = to_long_path(dst_path)
            src_lp = to_long_path(src_path)

            try:
                if effective_type == "hardlink":
                    try:
                        dst_st = os.lstat(dst_lp)
                    except FileNotFoundError:
                        already_missing += 1
                        state.update_operation_status(op_id, "rolled_back")
                        _emit_progress(idx, dst_path)
                        continue

                    if is_symlink_or_junction(dst_path, dst_st) or not stat.S_ISREG(dst_st.st_mode):
                        skipped_modified += 1
                        warnings.append(f"Пропущено при откате (не обычный файл): {dst_path}")
                        _emit_progress(idx, dst_path)
                        continue

                    if not is_path_inside(src_path, source_dir) or path_key(src_path) == path_key(
                        source_dir
                    ):
                        skipped_modified += 1
                        warnings.append(
                            f"Исходный путь вне исходной папки, жёсткая ссылка сохранена: {dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    try:
                        src_st = os.lstat(src_lp)
                    except FileNotFoundError:
                        skipped_modified += 1
                        warnings.append(
                            f"Исходный файл отсутствует, жёсткая ссылка сохранена для защиты данных: "
                            f"{dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    if is_symlink_or_junction(src_path, src_st) or not stat.S_ISREG(src_st.st_mode):
                        skipped_modified += 1
                        warnings.append(
                            f"Исходный путь не является обычным файлом, ссылка сохранена: {dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    if path_key(os.path.realpath(src_lp)) == path_key(os.path.realpath(dst_lp)):
                        skipped_modified += 1
                        warnings.append(
                            f"Исходный и целевой пути совпадают, удаление отменено для защиты данных: "
                            f"{dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    actual_dst_inode = f"{dst_st.st_dev}:{dst_st.st_ino}"
                    actual_src_inode = f"{src_st.st_dev}:{src_st.st_ino}"

                    if actual_dst_inode == actual_src_inode and (
                        src_st.st_nlink < 2 or dst_st.st_nlink < 2
                    ):
                        skipped_modified += 1
                        warnings.append(
                            f"Обнаружена единственная ссылка на файл (nlink < 2) — удаление отменено: "
                            f"{dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    inode_ok = (bool(expected_inode) and actual_dst_inode == expected_inode) or (
                        actual_dst_inode == actual_src_inode
                    )
                    if not inode_ok:
                        skipped_modified += 1
                        warnings.append(
                            f"Файл изменён или заменён пользователем после сортировки, сохранён: "
                            f"{dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    same_hardlink = (
                        actual_dst_inode == actual_src_inode
                        and src_st.st_nlink >= 2
                        and dst_st.st_nlink >= 2
                    )
                    if not same_hardlink:
                        if not expected_sha:
                            skipped_modified += 1
                            warnings.append(
                                f"Не удалось подтвердить целостность копии исходника, файл сохранён: "
                                f"{dst_path}"
                            )
                            _emit_progress(idx, dst_path)
                            continue
                        dst_sha = compute_sha256(dst_path, stop_event=stop_event)
                        src_sha = compute_sha256(src_path, stop_event=stop_event)
                        if dst_sha != expected_sha or src_sha != expected_sha:
                            skipped_modified += 1
                            warnings.append(
                                f"Файл или исходник изменён после сортировки, сохранён: {dst_path}"
                            )
                            _emit_progress(idx, dst_path)
                            continue

                    os.unlink(dst_lp)
                    state.update_operation_status(op_id, "rolled_back")
                    reverted += 1

                elif effective_type == "copy":
                    try:
                        dst_st = os.lstat(dst_lp)
                    except FileNotFoundError:
                        already_missing += 1
                        state.update_operation_status(op_id, "rolled_back")
                        _emit_progress(idx, dst_path)
                        continue

                    if is_symlink_or_junction(dst_path, dst_st) or not stat.S_ISREG(dst_st.st_mode):
                        skipped_modified += 1
                        warnings.append(f"Пропущено при откате (не обычный файл): {dst_path}")
                        _emit_progress(idx, dst_path)
                        continue

                    if not expected_sha:
                        skipped_modified += 1
                        warnings.append(
                            f"Отсутствует эталонный SHA-256, копия сохранена для безопасности: "
                            f"{dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    # Проверяем, что оригинал в source_dir цел и совпадает по SHA-256 до удаления копии
                    if not is_path_inside(src_path, source_dir) or path_key(src_path) == path_key(
                        source_dir
                    ):
                        skipped_modified += 1
                        warnings.append(
                            f"Исходный путь вне исходной папки, копия сохранена для защиты данных: "
                            f"{dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    try:
                        src_st = os.lstat(src_lp)
                    except FileNotFoundError:
                        skipped_modified += 1
                        warnings.append(
                            f"Исходный файл отсутствует, единственная копия сохранена: {dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    if is_symlink_or_junction(src_path, src_st) or not stat.S_ISREG(src_st.st_mode):
                        skipped_modified += 1
                        warnings.append(
                            f"Исходный путь не является обычным файлом, копия сохранена: {dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    if path_key(os.path.realpath(src_lp)) == path_key(os.path.realpath(dst_lp)) or (
                        src_st.st_dev == dst_st.st_dev and src_st.st_ino == dst_st.st_ino
                    ):
                        skipped_modified += 1
                        warnings.append(
                            f"Исходник и копия указывают на один файл, удаление отменено: {dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    actual_sha = compute_sha256(dst_path, stop_event=stop_event)
                    if actual_sha != expected_sha:
                        skipped_modified += 1
                        warnings.append(
                            f"Скопированный файл изменён пользователем после сортировки и сохранён: "
                            f"{dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    src_actual_sha = compute_sha256(src_path, stop_event=stop_event)
                    if src_actual_sha != expected_sha:
                        skipped_modified += 1
                        warnings.append(
                            f"Исходный файл был изменён после сортировки, копия сохранена: {dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    os.unlink(dst_lp)
                    state.update_operation_status(op_id, "rolled_back")
                    reverted += 1

                elif effective_type == "move":
                    if not is_path_inside(src_path, source_dir) or path_key(src_path) == path_key(
                        source_dir
                    ):
                        failed += 1
                        warnings.append(
                            f"Отказано в возврате файла за пределы исходной папки: {src_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    try:
                        dst_st = os.lstat(dst_lp)
                    except FileNotFoundError:
                        if not os.path.lexists(src_lp):
                            failed += 1
                            warnings.append(
                                f"Перемещённый файл отсутствует в целевой папке и не найден в исходной: "
                                f"{dst_path}"
                            )
                        else:
                            already_missing += 1
                            state.update_operation_status(op_id, "rolled_back")
                        _emit_progress(idx, dst_path)
                        continue

                    if is_symlink_or_junction(dst_path, dst_st) or not stat.S_ISREG(dst_st.st_mode):
                        skipped_modified += 1
                        warnings.append(f"Пропущено при откате (не обычный файл): {dst_path}")
                        _emit_progress(idx, dst_path)
                        continue

                    if not expected_sha:
                        skipped_modified += 1
                        warnings.append(
                            f"Отсутствует эталонный SHA-256, перемещённый файл оставлен на месте: "
                            f"{dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    actual_sha = compute_sha256(dst_path, stop_event=stop_event)
                    if actual_sha != expected_sha:
                        skipped_modified += 1
                        warnings.append(
                            f"Перемещённый файл изменён пользователем после сортировки, оставлен на месте: "
                            f"{dst_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    if os.path.lexists(src_lp):
                        skipped_modified += 1
                        warnings.append(
                            f"По исходному пути уже существует файл, возврат пропущен: {src_path}"
                        )
                        _emit_progress(idx, dst_path)
                        continue

                    _restore_moved_file(dst_path, src_path, expected_sha, stop_event=stop_event)
                    state.update_operation_status(op_id, "rolled_back")
                    reverted += 1

                else:
                    failed += 1
                    warnings.append(f"Неизвестный тип операции при откате: {effective_type!r}")

            except CancelledError:
                cancelled = True
                warnings.append("Откат остановлен пользователем во время проверки файла")
                break
            except Exception as exc:  # noqa: BLE001
                if is_fatal_storage_error(exc):
                    raise
                failed += 1
                msg = f"Ошибка отката {dst_path}: {exc}"
                log.warning(msg)
                warnings.append(msg)

            _emit_progress(idx, dst_path)

        removed_dirs = _prune_empty_dirs(candidate_dirs, target_dir)

        final_status = (
            "rolled_back"
            if (not cancelled and failed == 0 and skipped_modified == 0)
            else "partial"
        )
        state.refresh_sort_run_counts(run_id, status=final_status, finished=True)
        finalized = True

        return RollbackResult(
            run_id=run_id,
            status=final_status,
            reverted=reverted,
            skipped_modified=skipped_modified,
            already_missing=already_missing,
            failed=failed,
            removed_dirs=removed_dirs,
            warnings=warnings,
        )
    finally:
        if not finalized:
            try:
                state.refresh_sort_run_counts(run_id, status="partial", finished=True)
            except Exception as fin_exc:  # noqa: BLE001
                log.error(
                    "Не удалось перевести sort_run #%d в статус 'partial' после фатального сбоя отката: %s",
                    run_id,
                    fin_exc,
                )
