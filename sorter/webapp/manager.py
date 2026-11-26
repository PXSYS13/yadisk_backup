"""Координатор фоновых задач (сканирование, кластеризация, сортировка, откат) и диалог выбора папки."""

from __future__ import annotations

import inspect
import logging
import os
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from sorter import paths
from sorter.clustering import find_age_bridge_candidates, run_initial_clustering
from sorter.media_scan import ScanSummary, scan_directory
from sorter.organizer import (
    DryRunPlan,
    SortOptions,
    build_sort_plan,
    execute_sort_plan,
    resume_sort_run,
    rollback_sort_run,
)
from sorter.organizer.executor import _check_free_space_for_ops
from sorter.organizer.planner import _validate_source_and_target
from sorter.state import SorterState
from sorter.utils import (
    IS_WINDOWS,
    CancelledError,
    hhmm,
    human_bytes,
    keep_awake,
    to_long_path,
    validate_dir_safe,
)
from sorter.vision import (
    VisionEngine,
    ensure_model_pack,
    is_model_pack_ready,
    process_pending_media,
    select_ort_providers,
)
from sorter.webapp import config as cfg_mod

log = logging.getLogger("photo_sorter.webui")


class TaskBusyError(RuntimeError):
    """Другая длительная фоновая задача уже выполняется."""


EngineFactory = Callable[..., VisionEngine]


def _invoke_engine_factory(
    factory: EngineFactory,
    *,
    pack_name: str,
    prefer_gpu: bool,
    progress_cb: Callable[[int, int | None], None] | None,
    stop_event: threading.Event,
) -> VisionEngine:
    """Вызывает пользовательскую фабрику VisionEngine с поддержкой 0..4 аргументов."""
    sig = inspect.signature(factory)
    params = list(sig.parameters.values())
    has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
    if not params:
        return factory()

    kwargs: dict[str, Any] = {}
    if has_var_kw or "progress_cb" in sig.parameters:
        kwargs["progress_cb"] = progress_cb
    if has_var_kw or "stop_event" in sig.parameters:
        kwargs["stop_event"] = stop_event

    pos_params = [
        p
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and p.name not in kwargs
    ]
    if has_var_pos or len(pos_params) >= 2:
        return factory(pack_name, prefer_gpu, **kwargs)
    if len(pos_params) == 1:
        return factory(pack_name, **kwargs)
    return factory(**kwargs)


class TaskManager:
    """Потокобезопасный менеджер фоновых задач Web UI с прогрессом и лентой событий."""

    def __init__(self, *, engine_factory: EngineFactory | None = None) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._engine_factory = engine_factory

        self._running: bool = False
        self._task: str | None = None
        self._phase: str = "idle"
        self._progress: dict[str, Any] = {
            "done": 0,
            "total": 0,
            "percent": 0.0,
            "current_file": "",
        }
        self._message: str = ""
        self._error: str | None = None
        self._events: deque[dict[str, str]] = deque(maxlen=120)
        self._age_bridge_count_cache: int | None = None

    def add_event(self, kind: str, text: str) -> None:
        with self._lock:
            self._events.appendleft({"at": hhmm(), "kind": kind, "text": text})

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def invalidate_age_bridge_cache(self) -> None:
        """Сбрасывает кеш числа кандидатов возрастных мостов (после кластеризации и правок Студии)."""
        with self._lock:
            self._age_bridge_count_cache = None

    def reconcile_stale_runs(self, state: SorterState | None = None) -> list[int]:
        """Помечает зависшие sort_runs со статусом 'running' как 'partial', когда задача не активна."""
        with self._lock:
            if self._running:
                return []
            if state is not None:
                reconciled = state.reconcile_stale_sort_runs()
            else:
                with SorterState(paths.DB_FILE) as own_state:
                    reconciled = own_state.reconcile_stale_sort_runs()
        for rid in reconciled:
            self.add_event(
                "warn",
                f"Незавершённый запуск сортировки #{rid} из прошлой сессии помечен как частичный (partial)",
            )
        return reconciled

    def _set_progress(
        self,
        done: int,
        total: int,
        current_file: str = "",
        *,
        percent: float | None = None,
        done_ops: int | None = None,
        skipped_ops: int | None = None,
        failed_ops: int | None = None,
    ) -> None:
        if percent is None:
            pct = round(min(100.0, max(0.0, 100.0 * done / total)), 1) if total > 0 else 0.0
        else:
            pct = round(min(100.0, max(0.0, float(percent))), 1)
        prog: dict[str, Any] = {
            "done": int(done),
            "total": int(total),
            "percent": pct,
            "current_file": str(current_file),
        }
        if done_ops is not None:
            prog["done_ops"] = int(done_ops)
        if skipped_ops is not None:
            prog["skipped_ops"] = int(skipped_ops)
        if failed_ops is not None:
            prog["failed_ops"] = int(failed_ops)
        with self._lock:
            self._progress = prog

    def _set_state(
        self,
        *,
        phase: str | None = None,
        message: str | None = None,
        error: str | None = None,
        clear_error: bool = False,
    ) -> None:
        with self._lock:
            if phase is not None:
                self._phase = phase
            if message is not None:
                self._message = message
            if clear_error:
                self._error = None
            elif error is not None:
                self._error = error

    def _begin_task(
        self,
        task_name: str,
        initial_phase: str,
        initial_message: str,
    ) -> threading.Event:
        stop_ev = threading.Event()
        with self._lock:
            if self._running:
                raise TaskBusyError("Сейчас уже выполняется другая задача")
            self._running = True
            self._task = task_name
            self._phase = initial_phase
            self._message = initial_message
            self._error = None
            self._stop_event = stop_ev
            self._progress = {
                "done": 0,
                "total": 0,
                "percent": 0.0,
                "current_file": "",
            }
        return stop_ev

    def _abort_unstarted_task(self, exc: BaseException) -> None:
        with self._lock:
            self._running = False
            self._thread = None
            self._phase = "error"
            self._error = str(exc)
            self._message = f"Ошибка запуска задачи: {exc}"

    def _spawn_worker(self, name: str, target: Callable[[], None]) -> None:
        try:
            thread = threading.Thread(target=target, name=name, daemon=True)
            with self._lock:
                self._thread = thread
            thread.start()
        except BaseException as exc:
            self._abort_unstarted_task(exc)
            raise

    def start_scan(
        self,
        source_dir: str,
        *,
        recluster: bool = True,
        model_pack: str | None = None,
        prefer_gpu: bool | None = None,
    ) -> str:
        """Проверяет папку, сохраняет настройки и запускает сканирование + анализ лиц + кластеризацию."""
        clean_dir = validate_dir_safe(source_dir, label="исходная папка")
        if not os.path.isdir(to_long_path(clean_dir)):
            raise ValueError(f"Папка не найдена: {clean_dir}")
        if self.is_running():
            raise TaskBusyError("Сейчас уже выполняется другая задача")

        cfg_updates: dict[str, Any] = {"source_dir": clean_dir}
        if model_pack is not None:
            cfg_updates["model_pack"] = model_pack
        if prefer_gpu is not None:
            cfg_updates["prefer_gpu"] = prefer_gpu
        # Сохраняем конфиг ДО _begin_task, чтобы сбой диска не оставил _running=True
        cfg_mod.save_config(cfg_updates)

        stop_ev = self._begin_task("scan", "scanning", f"Сканирую папку {clean_dir}…")
        try:
            self.add_event("run", f"Начинаю сканирование папки {clean_dir} (только чтение)")

            def _worker() -> None:
                try:
                    paths.ensure_runtime_dirs()
                    cfg = cfg_mod.get_config()
                    pack_name = str(cfg.get("model_pack", "antelopev2"))
                    use_gpu = bool(cfg.get("prefer_gpu", True))

                    with keep_awake():
                        with SorterState(paths.DB_FILE) as state:

                            def _on_scan_progress(summary: ScanSummary, rel_path: str) -> None:
                                self._set_progress(
                                    summary.discovered,
                                    summary.discovered,
                                    rel_path,
                                    percent=0.0,
                                )
                                self._set_state(
                                    message=(
                                        f"Найдено медиафайлов: {summary.discovered} "
                                        f"(фото: {summary.photos}, видео: {summary.videos})"
                                    )
                                )

                            scan_summary = scan_directory(
                                clean_dir,
                                state,
                                stop_event=stop_ev,
                                progress_cb=_on_scan_progress,
                            )
                            state.set_meta("source_dir", clean_dir)
                            state.set_meta("last_scan_at", str(time.time()))

                            scan_ev_text = (
                                f"Индексация завершена: {scan_summary.photos} фото, "
                                f"{scan_summary.videos} видео (новых/обновлённых: {scan_summary.new_or_changed})"
                            )
                            if scan_summary.io_errors > 0:
                                scan_ev_text += f", ошибок доступа/чтения: {scan_summary.io_errors}"
                                self.add_event("warn", scan_ev_text)
                            else:
                                self.add_event("info", scan_ev_text)

                            pending_media = state.list_media(scan_status="pending")
                            vision_stats = {
                                "total": len(pending_media),
                                "processed": 0,
                                "scanned": 0,
                                "errors": 0,
                                "faces": 0,
                            }

                            if pending_media:

                                def _on_dl_progress(downloaded: int, total: int | None) -> None:
                                    tot = total or 0
                                    self._set_progress(downloaded, tot, f"{pack_name}.zip")
                                    if total:
                                        self._set_state(
                                            message=(
                                                f"Скачиваю модели {pack_name}: "
                                                f"{human_bytes(downloaded)} / {human_bytes(total)}"
                                            )
                                        )
                                    else:
                                        self._set_state(
                                            message=f"Скачиваю модели {pack_name}: {human_bytes(downloaded)}"
                                        )

                                if self._engine_factory is not None:
                                    engine = _invoke_engine_factory(
                                        self._engine_factory,
                                        pack_name=pack_name,
                                        prefer_gpu=use_gpu,
                                        progress_cb=_on_dl_progress,
                                        stop_event=stop_ev,
                                    )
                                else:
                                    if not is_model_pack_ready(pack_name):
                                        self._set_state(
                                            phase="downloading_model",
                                            message=f"Скачиваю пакет ONNX-моделей {pack_name}…",
                                        )
                                        self.add_event(
                                            "info",
                                            f"Загрузка моделей распознавания ({pack_name})…",
                                        )
                                        ensure_model_pack(
                                            pack_name=pack_name,
                                            progress_cb=_on_dl_progress,
                                            stop_event=stop_ev,
                                        )
                                    self._set_state(
                                        phase="detecting",
                                        message=f"Загружаю модели {pack_name} в память…",
                                    )
                                    engine = VisionEngine.from_model_pack(
                                        pack_name=pack_name,
                                        prefer_gpu=use_gpu,
                                        progress_cb=_on_dl_progress,
                                        stop_event=stop_ev,
                                    )

                                self._set_state(
                                    phase="detecting",
                                    message=f"Поиск лиц: 0 из {len(pending_media)}…",
                                )
                                self._set_progress(0, len(pending_media), "")

                                def _on_vision_progress(
                                    vstats: dict[str, int], row: dict[str, Any]
                                ) -> None:
                                    rel = str(row.get("rel_path") or row.get("local_path") or "")
                                    self._set_progress(vstats["processed"], vstats["total"], rel)
                                    self._set_state(
                                        message=(
                                            f"Обработано {vstats['processed']} из {vstats['total']} "
                                            f"(найдено лиц: {vstats['faces']})"
                                        )
                                    )

                                vision_stats = process_pending_media(
                                    state,
                                    engine,
                                    stop_event=stop_ev,
                                    progress_cb=_on_vision_progress,
                                )

                            cluster_msg = ""
                            if recluster and not stop_ev.is_set():
                                self._set_state(
                                    phase="clustering",
                                    message="Кластеризация лиц по людям…",
                                )
                                self.add_event("info", "Запуск кластеризации лиц…")
                                c_stats = run_initial_clustering(state)
                                cluster_msg = f", кластеров: {c_stats['clusters_created']}"
                                self.invalidate_age_bridge_cache()

                            final_stats = state.get_stats()
                            msg = (
                                f"Готово: обработано {final_stats['scanned']} из {final_stats['media_total']} файлов, "
                                f"лиц: {final_stats['faces_active']}, профилей: {final_stats['persons_total']}"
                                f"{cluster_msg}"
                            )
                            issues: list[str] = []
                            if scan_summary.io_errors > 0:
                                issues.append(f"ошибок доступа к диску: {scan_summary.io_errors}")
                            if vision_stats["errors"] > 0:
                                issues.append(f"ошибок чтения: {vision_stats['errors']}")

                            total_items = max(vision_stats["total"], scan_summary.discovered)
                            self._set_progress(total_items, total_items, "", percent=100.0)
                            if issues:
                                msg += f" ({', '.join(issues)})"
                                self.add_event("warn", msg)
                                self._set_state(phase="done", message=msg, error="; ".join(issues))
                            else:
                                self.add_event("ok", msg)
                                self._set_state(phase="done", message=msg, clear_error=True)
                except CancelledError:
                    msg = "Сканирование остановлено пользователем"
                    self.add_event("info", msg)
                    self._set_state(phase="cancelled", message=msg, clear_error=True)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Сбой фонового сканирования")
                    self.add_event("err", f"Ошибка сканирования: {exc}")
                    self._set_state(phase="error", message=f"Ошибка: {exc}", error=str(exc))
                finally:
                    self.invalidate_age_bridge_cache()
                    with self._lock:
                        self._running = False

            self._spawn_worker("sorter-scan", _worker)
        except BaseException as exc:
            self._abort_unstarted_task(exc)
            raise
        return clean_dir

    def start_cluster(self) -> None:
        """Запускает кластеризацию и построение возрастных прототипов в фоновом потоке."""
        self._begin_task("cluster", "clustering", "Выполняю кластеризацию лиц…")
        try:
            self.add_event("run", "Запуск перекластеризации лиц")

            def _worker() -> None:
                try:
                    with keep_awake():
                        with SorterState(paths.DB_FILE) as state:
                            c_stats = run_initial_clustering(state)
                            msg = (
                                f"Кластеризация завершена: новых профилей {c_stats['clusters_created']}, "
                                f"присоединено к названным {c_stats['matched_existing']}, "
                                f"доназначено {c_stats['reassigned_low_quality']}"
                            )
                            self.add_event("ok", msg)
                            self._set_progress(1, 1, "", percent=100.0)
                            self._set_state(phase="done", message=msg, clear_error=True)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Сбой кластеризации")
                    self.add_event("err", f"Ошибка кластеризации: {exc}")
                    self._set_state(phase="error", message=f"Ошибка: {exc}", error=str(exc))
                finally:
                    self.invalidate_age_bridge_cache()
                    with self._lock:
                        self._running = False

            self._spawn_worker("sorter-cluster", _worker)
        except BaseException as exc:
            self._abort_unstarted_task(exc)
            raise

    def start_sort(self, options: SortOptions, *, allow_move: bool = False) -> DryRunPlan:
        """Синхронно строит и проверяет план сортировки, затем запускает выполнение в фоне."""
        if self.is_running():
            raise TaskBusyError("Сейчас уже выполняется другая задача")

        with SorterState(paths.DB_FILE) as state:
            self.reconcile_stale_runs(state)
            plan = build_sort_plan(state, options)

        has_move = plan.effective_mode == "move" or any(op.op_type == "move" for op in plan.ops)
        if has_move and not allow_move:
            raise ValueError(
                "Режим перемещения (move) изменяет исходную папку и требует явного "
                "подтверждения allow_move=True"
            )
        if not plan.enough_space:
            raise ValueError(
                f"Недостаточно свободного места на диске: требуется {human_bytes(plan.required_bytes)} "
                f"(+100 МБ запас), доступно {human_bytes(plan.free_bytes)}"
            )

        stop_ev = self._begin_task(
            "sort",
            "sorting",
            f"Выполняю сортировку ({plan.effective_mode}): 0 из {len(plan.ops)}…",
        )
        try:
            self._set_progress(0, len(plan.ops), "", done_ops=0, skipped_ops=0, failed_ops=0)
            self.add_event(
                "run",
                f"Запуск сортировки ({plan.effective_mode}): операций {len(plan.ops)} -> {plan.target_dir}",
            )

            def _worker() -> None:
                try:
                    with SorterState(paths.DB_FILE) as state:

                        def _on_progress(ev: dict[str, Any]) -> None:
                            done = int(ev.get("processed", 0))
                            total = int(ev.get("total", len(plan.ops)))
                            dst = os.path.basename(str(ev.get("dst_path") or ""))
                            self._set_progress(
                                done,
                                total,
                                dst,
                                done_ops=int(ev.get("done_ops", 0)),
                                skipped_ops=int(ev.get("skipped_ops", 0)),
                                failed_ops=int(ev.get("failed_ops", 0)),
                            )
                            self._set_state(message=f"Сортировка: {done} из {total}…")

                        res = execute_sort_plan(
                            state,
                            plan,
                            allow_move=allow_move,
                            stop_event=stop_ev,
                            progress_cb=_on_progress,
                        )

                    for warn_item in res.warnings[:10]:
                        self.add_event("warn", warn_item)
                    for err_item in res.errors[:10]:
                        self.add_event("err", err_item)

                    if res.status == "cancelled":
                        msg = (
                            f"Сортировка #{res.run_id} остановлена: выполнено {res.done_ops} "
                            f"из {res.total_ops}"
                            + (f", ошибок {res.failed_ops}" if res.failed_ops > 0 else "")
                        )
                        self.add_event("warn" if res.failed_ops > 0 else "info", msg)
                        self._set_progress(
                            res.done_ops + res.skipped_ops + res.failed_ops,
                            res.total_ops,
                            "",
                            done_ops=res.done_ops,
                            skipped_ops=res.skipped_ops,
                            failed_ops=res.failed_ops,
                        )
                        if res.failed_ops > 0:
                            err_detail = res.errors[0] if res.errors else msg
                            self._set_state(phase="cancelled", message=msg, error=err_detail)
                        else:
                            self._set_state(phase="cancelled", message=msg, clear_error=True)
                    elif res.status == "completed" and res.failed_ops == 0:
                        msg = f"Сортировка #{res.run_id} завершена: выполнено {res.done_ops} операций"
                        if res.fallback_copy_ops > 0:
                            msg += f" (резервных копий: {res.fallback_copy_ops})"
                        self.add_event("warn" if res.warnings else "ok", msg)
                        self._set_progress(
                            res.total_ops,
                            res.total_ops,
                            "",
                            percent=100.0,
                            done_ops=res.done_ops,
                            skipped_ops=res.skipped_ops,
                            failed_ops=res.failed_ops,
                        )
                        self._set_state(phase="done", message=msg, clear_error=True)
                    else:
                        msg = (
                            f"Сортировка #{res.run_id} завершена частично: "
                            f"выполнено {res.done_ops}, пропущено {res.skipped_ops}, ошибок {res.failed_ops}"
                        )
                        self.add_event("warn", msg)
                        self._set_progress(
                            res.total_ops,
                            res.total_ops,
                            "",
                            percent=100.0,
                            done_ops=res.done_ops,
                            skipped_ops=res.skipped_ops,
                            failed_ops=res.failed_ops,
                        )
                        err_detail = res.errors[0] if res.errors else msg
                        self._set_state(phase="done", message=msg, error=err_detail)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Сбой выполнения сортировки")
                    self.add_event("err", f"Ошибка сортировки: {exc}")
                    self._set_state(phase="error", message=f"Ошибка: {exc}", error=str(exc))
                finally:
                    with self._lock:
                        self._running = False

            self._spawn_worker("sorter-sort", _worker)
        except BaseException as exc:
            self._abort_unstarted_task(exc)
            raise
        return plan

    def start_resume(self, run_id: int, *, allow_move: bool = False) -> None:
        """Синхронно проверяет запуск и продолжает незавершённые операции в фоне."""
        if self.is_running():
            raise TaskBusyError("Сейчас уже выполняется другая задача")

        with SorterState(paths.DB_FILE) as state:
            self.reconcile_stale_runs(state)
            run = state.get_sort_run(run_id)
            if run is None:
                raise LookupError(f"Запуск сортировки #{run_id} не найден")
            all_ops = state.list_run_operations(run_id)

        if run["status"] == "rolled_back" or any(op["status"] == "rolled_back" for op in all_ops):
            raise ValueError(
                f"Запуск сортировки #{run_id} частично или полностью откачен и не может быть продолжен"
            )
        pending_ops = [op for op in all_ops if op["status"] == "pending"]
        if not pending_ops:
            raise ValueError(
                f"В запуске сортировки #{run_id} нет незавершённых операций для продолжения"
            )
        if any(str(op.get("op_type") or "") == "move" for op in pending_ops) and not allow_move:
            raise ValueError(
                "Возобновление перемещения (move) изменяет исходную папку и требует явного "
                "подтверждения allow_move=True"
            )

        src_dir, dst_dir = _validate_source_and_target(
            str(run["source_dir"]), str(run["target_dir"])
        )
        try:
            _check_free_space_for_ops(src_dir, dst_dir, pending_ops)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

        total_ops = int(run.get("total_ops", len(all_ops)))
        done_init = int(run.get("done_ops", 0))
        skipped_init = int(run.get("skipped_ops", 0))
        failed_init = int(run.get("failed_ops", 0))

        stop_ev = self._begin_task("sort", "sorting", f"Продолжаю сортировку #{run_id}…")
        try:
            self._set_progress(
                done_init + skipped_init + failed_init,
                total_ops,
                "",
                done_ops=done_init,
                skipped_ops=skipped_init,
                failed_ops=failed_init,
            )
            self.add_event("run", f"Возобновление сортировки #{run_id}")

            def _worker() -> None:
                try:
                    with SorterState(paths.DB_FILE) as state:

                        def _on_progress(ev: dict[str, Any]) -> None:
                            done = int(ev.get("processed", 0))
                            total = int(ev.get("total", total_ops))
                            dst = os.path.basename(str(ev.get("dst_path") or ""))
                            self._set_progress(
                                done,
                                total,
                                dst,
                                done_ops=int(ev.get("done_ops", 0)),
                                skipped_ops=int(ev.get("skipped_ops", 0)),
                                failed_ops=int(ev.get("failed_ops", 0)),
                            )
                            self._set_state(message=f"Довыполнение #{run_id}: {done} из {total}…")

                        res = resume_sort_run(
                            state,
                            run_id,
                            allow_move=allow_move,
                            stop_event=stop_ev,
                            progress_cb=_on_progress,
                        )

                    for warn_item in res.warnings[:10]:
                        self.add_event("warn", warn_item)
                    for err_item in res.errors[:10]:
                        self.add_event("err", err_item)

                    if res.status == "cancelled":
                        msg = (
                            f"Довыполнение #{run_id} остановлено: {res.done_ops} из {res.total_ops}"
                            + (f", ошибок {res.failed_ops}" if res.failed_ops > 0 else "")
                        )
                        self.add_event("warn" if res.failed_ops > 0 else "info", msg)
                        self._set_progress(
                            res.done_ops + res.skipped_ops + res.failed_ops,
                            res.total_ops,
                            "",
                            done_ops=res.done_ops,
                            skipped_ops=res.skipped_ops,
                            failed_ops=res.failed_ops,
                        )
                        if res.failed_ops > 0:
                            err_detail = res.errors[0] if res.errors else msg
                            self._set_state(phase="cancelled", message=msg, error=err_detail)
                        else:
                            self._set_state(phase="cancelled", message=msg, clear_error=True)
                    elif res.status == "completed" and res.failed_ops == 0:
                        msg = (
                            f"Сортировка #{run_id} завершена ({res.status}): "
                            f"выполнено {res.done_ops}, ошибок 0"
                        )
                        self.add_event("warn" if res.warnings else "ok", msg)
                        self._set_progress(
                            res.total_ops,
                            res.total_ops,
                            "",
                            percent=100.0,
                            done_ops=res.done_ops,
                            skipped_ops=res.skipped_ops,
                            failed_ops=res.failed_ops,
                        )
                        self._set_state(phase="done", message=msg, clear_error=True)
                    else:
                        msg = (
                            f"Сортировка #{run_id} завершена частично ({res.status}): "
                            f"выполнено {res.done_ops}, пропущено {res.skipped_ops}, ошибок {res.failed_ops}"
                        )
                        self.add_event("warn", msg)
                        self._set_progress(
                            res.total_ops,
                            res.total_ops,
                            "",
                            percent=100.0,
                            done_ops=res.done_ops,
                            skipped_ops=res.skipped_ops,
                            failed_ops=res.failed_ops,
                        )
                        err_detail = res.errors[0] if res.errors else msg
                        self._set_state(phase="done", message=msg, error=err_detail)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Сбой довыполнения сортировки #%d", run_id)
                    self.add_event("err", f"Ошибка довыполнения #{run_id}: {exc}")
                    self._set_state(phase="error", message=f"Ошибка: {exc}", error=str(exc))
                finally:
                    with self._lock:
                        self._running = False

            self._spawn_worker("sorter-resume", _worker)
        except BaseException as exc:
            self._abort_unstarted_task(exc)
            raise

    def start_rollback(self, run_id: int) -> None:
        """Синхронно проверяет запуск и безопасно откатывает его в фоновом потоке."""
        if self.is_running():
            raise TaskBusyError("Сейчас уже выполняется другая задача")

        with SorterState(paths.DB_FILE) as state:
            self.reconcile_stale_runs(state)
            run = state.get_sort_run(run_id)
        if run is None:
            raise LookupError(f"Запуск сортировки #{run_id} не найден")

        done_ops = int(run.get("done_ops", 0))
        stop_ev = self._begin_task("rollback", "rollback", f"Откатываю сортировку #{run_id}…")
        try:
            self._set_progress(0, done_ops, "", done_ops=0, skipped_ops=0, failed_ops=0)
            self.add_event("run", f"Начинаю безопасный откат сортировки #{run_id}")

            def _worker() -> None:
                try:
                    with keep_awake():
                        with SorterState(paths.DB_FILE) as state:

                            def _on_progress(ev: dict[str, Any]) -> None:
                                done = int(ev.get("processed", 0))
                                total = int(ev.get("total", done_ops))
                                dst = os.path.basename(str(ev.get("dst_path") or ""))
                                reverted_live = int(ev.get("done_ops", ev.get("reverted", 0)))
                                skipped_live = int(
                                    ev.get("skipped_ops", ev.get("skipped_modified", 0))
                                )
                                failed_live = int(ev.get("failed_ops", ev.get("failed", 0)))
                                self._set_progress(
                                    done,
                                    total,
                                    dst,
                                    done_ops=reverted_live,
                                    skipped_ops=skipped_live,
                                    failed_ops=failed_live,
                                )
                                self._set_state(message=f"Откат #{run_id}: {done} из {total}…")

                            res = rollback_sort_run(
                                state,
                                run_id,
                                stop_event=stop_ev,
                                progress_cb=_on_progress,
                            )

                    for warn_item in res.warnings[:10]:
                        self.add_event("warn", warn_item)

                    msg = (
                        f"Откат #{run_id} завершён ({res.status}): "
                        f"возвращено/удалено {res.reverted}, пропущено изменённых {res.skipped_modified}, "
                        f"ошибок {res.failed}, удалено пустых папок {res.removed_dirs}"
                    )
                    was_cancelled = stop_ev.is_set()
                    has_issue = res.failed > 0 or (res.status == "partial" and not was_cancelled)
                    self.add_event("warn" if (has_issue or was_cancelled) else "ok", msg)
                    self._set_progress(
                        done_ops,
                        done_ops,
                        "",
                        percent=100.0,
                        done_ops=res.reverted,
                        skipped_ops=res.skipped_modified + res.already_missing,
                        failed_ops=res.failed,
                    )
                    if was_cancelled:
                        self._set_state(phase="cancelled", message=msg, clear_error=True)
                    elif has_issue:
                        err_detail = res.warnings[0] if res.warnings else msg
                        self._set_state(phase="done", message=msg, error=err_detail)
                    else:
                        self._set_state(phase="done", message=msg, clear_error=True)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Сбой отката сортировки #%d", run_id)
                    self.add_event("err", f"Ошибка отката #{run_id}: {exc}")
                    self._set_state(phase="error", message=f"Ошибка: {exc}", error=str(exc))
                finally:
                    with self._lock:
                        self._running = False

            self._spawn_worker("sorter-rollback", _worker)
        except BaseException as exc:
            self._abort_unstarted_task(exc)
            raise

    def stop(self) -> bool:
        """Запрашивает мягкую остановку активной фоновой задачи."""
        with self._lock:
            if not self._running:
                return False
            ev = self._stop_event
        ev.set()
        self.add_event("info", "Запрошена остановка — завершаю текущий шаг…")
        return True

    def wait(self, timeout: float = 30.0) -> bool:
        """Ждёт завершения активного рабочего потока (удобно в тестах)."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def shutdown(self, timeout: float = 10.0) -> None:
        """Останавливает фоновую задачу и ждёт её завершения при закрытии сервера."""
        with self._lock:
            ev = self._stop_event
            thread = self._thread
        ev.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                log.warning(
                    "Фоновый поток сортировки по людям не завершился за %.1f с при остановке сервера",
                    timeout,
                )

    def model_status(self, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        """Возвращает состояние ONNX-пакета и доступных провайдеров ускорения."""
        active_cfg = cfg if cfg is not None else cfg_mod.get_config()
        pack = str(active_cfg.get("model_pack", "antelopev2"))
        prefer_gpu = bool(active_cfg.get("prefer_gpu", True))
        providers = select_ort_providers(prefer_gpu=prefer_gpu)
        gpu_providers = select_ort_providers(prefer_gpu=True)
        return {
            "pack": pack,
            "ready": is_model_pack_ready(pack),
            "providers": providers,
            "gpu_available": any(p != "CPUExecutionProvider" for p in gpu_providers),
        }

    def status_dict(self) -> dict[str, Any]:
        """Возвращает полную сводку состояния для GET /api/people/status."""
        cfg = cfg_mod.get_config()
        model_info = self.model_status(cfg)

        with self._lock:
            is_active = self._running
            cached_bridges = self._age_bridge_count_cache

        with SorterState(paths.DB_FILE) as state:
            if not is_active:
                self.reconcile_stale_runs(state)
            stats = state.get_stats()
            if int(stats.get("persons_total", 0)) < 2:
                age_bridge_count = 0
                if not is_active:
                    with self._lock:
                        self._age_bridge_count_cache = 0
            elif is_active or cached_bridges is not None:
                age_bridge_count = cached_bridges if cached_bridges is not None else 0
            else:
                age_bridge_count = len(find_age_bridge_candidates(state, top_k=25))
                with self._lock:
                    if not self._running and self._age_bridge_count_cache is None:
                        self._age_bridge_count_cache = age_bridge_count
            raw_undo = state.get_last_undoable()

        last_undo: dict[str, Any] | None = None
        if raw_undo is not None:
            last_undo = {
                "id": int(raw_undo["id"]),
                "action_type": str(raw_undo["action_type"]),
                "description": str(raw_undo["description"]),
                "created_at": float(raw_undo["created_at"]),
            }

        with self._lock:
            running = self._running
            task = self._task
            phase = self._phase
            progress = dict(self._progress)
            message = self._message
            error = self._error
            events = list(self._events)

        return {
            "running": running,
            "task": task,
            "phase": phase,
            "progress": progress,
            "message": message,
            "error": error,
            "config": cfg,
            "model": model_info,
            "stats": stats,
            "age_bridge_count": age_bridge_count,
            "last_undo": last_undo,
            "events": events,
        }


manager = TaskManager()


def pick_folder_dialog(initial_dir: str = "") -> tuple[str | None, str | None]:
    """Открывает системное окно выбора папки без интерполяции пользовательских строк в скрипт.

    Возвращает ``(chosen_path, error_message)``:
    - ``(path, None)`` — пользователь выбрал папку;
    - ``(None, None)`` — пользователь отменил выбор;
    - ``(None, "описание ошибки")`` — не удалось запустить системный диалог.
    """
    ps_error: str | None = None
    if IS_WINDOWS:
        ps = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = 'Выберите папку'; "
            "$p = $env:PHOTO_SORTER_INIT_DIR; "
            "if ($p -and (Test-Path -LiteralPath $p -PathType Container)) { $d.SelectedPath = $p }; "
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
        )
        ps_exe = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
        )
        env = {**os.environ, "PHOTO_SORTER_INIT_DIR": initial_dir or ""}
        try:
            proc = subprocess.run(
                [ps_exe, "-NoProfile", "-NonInteractive", "-STA", "-Command", ps],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            if proc.returncode == 0:
                chosen = (proc.stdout or "").strip()
                return (chosen, None) if chosen else (None, None)
            stderr_short = (proc.stderr or "").strip()[:200]
            ps_error = f"FolderBrowserDialog завершился с кодом {proc.returncode}: {stderr_short}"
            log.warning("%s", ps_error)
        except Exception as exc:  # noqa: BLE001
            ps_error = f"Не удалось открыть PowerShell FolderBrowserDialog: {exc}"
            log.warning("%s", ps_error)

    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            initialdir=initial_dir or None,
            title="Выберите папку",
        )
        cleaned = (chosen or "").strip() if isinstance(chosen, str) else ""
        return (cleaned, None) if cleaned else (None, None)
    except Exception as exc:  # noqa: BLE001
        tk_err = f"Не удалось открыть системный диалог выбора папки: {exc}"
        log.warning("%s", tk_err)
        if ps_error:
            return None, f"{ps_error}; {tk_err}"
        return None, tk_err
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
