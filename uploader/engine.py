"""Движок сканирования папки и параллельной загрузки фото и видео в /photounlim.

Принципы работы:
- Исходная папка на компьютере открывается строго на чтение — файлы никогда не удаляются и не меняются.
- NTFS-junction и симлинки не обходятся, чтобы исключить выход за пределы выбранной папки и зацикливание.
- Дедупликация тройная: локальные дубли (size+md5 с координацией in-flight), индекс /photounlim и HTTP 409 code 72.
- При 429 / SmartCaptcha все воркеры уходят в общую паузу без сжигания попыток файла.
- Quota Guard проверяет storage_type='photounlim' и контролирует, что обычное место Диска не расходуется.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from uploader.cookies import (
    AuthExpiredError,
    CaptchaError,
    CookiesFormatError,
    RateLimitError,
    UnlimApiError,
    YandexUnavailableError,
    get_sk_uid,
    make_session,
)
from uploader.media import classify_local_file, extract_shot_time, should_skip_dir
from uploader.quota_guard import QuotaGuard, QuotaGuardError, QuotaSnapshot, fetch_quota_snapshot
from uploader.remote_index import sync_remote_index
from uploader.state import MAX_ATTEMPTS, UploadRow, UploadState
from uploader.transport_base import UploadTransport
from uploader.transport_web import SessionContext, WebModelsTransport
from uploader.utils import (
    DEFAULT_CHUNK_MB,
    DEFAULT_WORKERS,
    EXIT_AUTH,
    EXIT_INTERRUPTED,
    EXIT_LOCAL_IO,
    EXIT_OK,
    EXIT_OUTAGE,
    EXIT_PARTIAL,
    EXIT_QUOTA_DRIFT,
    IS_WINDOWS,
    MAX_WORKERS,
    CancelledError,
    compute_hashes_with_size,
    files_text,
    human_bytes,
    is_path_inside,
    is_sqlite_disk_error,
    keep_awake,
    redact_urls,
    strip_long_prefix,
    to_long_path,
)

log = logging.getLogger("unlim_uploader")

REMOTE_INDEX_TTL_SEC = 6 * 3600
QUOTA_CHECK_EVERY_FILES = 15
QUOTA_CHECK_EVERY_BYTES = 200 * 1024 * 1024
MAX_CONSECUTIVE_QUOTA_ERRORS = 3
OUTAGE_STREAK_LIMIT = 6
MAX_TAIL_OUTAGE_PASSES = 2
WATCH_SETTLE_SEC = 5.0

EventCallback = Callable[[str, str], None]


@dataclass
class ScanSummary:
    source_dir: str
    scanned_total: int = 0
    photos: int = 0
    videos: int = 0
    unsupported: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    pruned: int = 0
    unreadable_dirs: int = 0
    unreadable_files: int = 0


@dataclass
class RunSummary:
    exit_code: int
    uploaded: int = 0
    hardlinked: int = 0
    skipped_remote: int = 0
    skipped_dup_local: int = 0
    skipped_limit: int = 0
    failed: int = 0
    bytes_uploaded: int = 0
    message: str = ""
    quota_before: QuotaSnapshot | None = None
    quota_after: QuotaSnapshot | None = None


@dataclass
class LiveProgress:
    """Потокобезопасные метрики текущей загрузки для Web UI и CLI."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    phase: str = "idle"
    active_files: dict[int, dict[str, Any]] = field(default_factory=dict)
    bytes_samples: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=120))
    session_bytes: int = 0
    session_uploaded: int = 0
    cooldown_until: float = 0.0
    indexed_remote: int = 0
    scanned_files: int = 0

    def set_phase(self, phase: str) -> None:
        with self.lock:
            self.phase = phase

    def set_active(self, worker_id: int, name: str, sent: int, total: int, stage: str) -> None:
        with self.lock:
            prev = self.active_files.get(worker_id)
            prev_sent = int(prev["sent"]) if prev and prev["name"] == name else 0
            delta = max(0, sent - prev_sent)
            if delta > 0:
                self.session_bytes += delta
                self.bytes_samples.append((time.monotonic(), self.session_bytes))
            self.active_files[worker_id] = {
                "name": name,
                "sent": sent,
                "total": total,
                "stage": stage,
            }

    def clear_active(self, worker_id: int) -> None:
        with self.lock:
            self.active_files.pop(worker_id, None)

    def speed_bps(self, window_sec: float = 15.0) -> float:
        now = time.monotonic()
        with self.lock:
            if len(self.bytes_samples) < 2:
                return 0.0
            cutoff = now - window_sec
            oldest = self.bytes_samples[0]
            for sample in self.bytes_samples:
                if sample[0] >= cutoff:
                    oldest = sample
                    break
            newest = self.bytes_samples[-1]
            dt = newest[0] - oldest[0]
            if dt < 0.5:
                return 0.0
            return max(0.0, (newest[1] - oldest[1]) / dt)

    def snapshot(self) -> dict[str, Any]:
        speed = self.speed_bps()
        with self.lock:
            return {
                "phase": self.phase,
                "active_files": list(self.active_files.values()),
                "speed_bps": int(speed),
                "session_bytes": self.session_bytes,
                "session_uploaded": self.session_uploaded,
                "cooldown_sec": max(0, int(self.cooldown_until - time.time())),
                "indexed_remote": self.indexed_remote,
                "scanned_files": self.scanned_files,
            }


def _is_reparse_or_symlink(entry: os.DirEntry[str], st: os.stat_result | None = None) -> bool:
    if entry.is_symlink():
        return True
    is_junc = getattr(entry, "is_junction", None)
    if callable(is_junc):
        try:
            if is_junc():
                return True
        except OSError:
            return True
    if IS_WINDOWS:
        try:
            info = st if st is not None else entry.stat(follow_symlinks=False)
            attrs = getattr(info, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
            if attrs & reparse:
                return True
        except OSError:
            return True
    return False


def scan_source_folder(
    source_dir: str,
    state: UploadState,
    *,
    verify_magic: bool = True,
    settle_sec: float = 0.0,
    stop_event: threading.Event | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ScanSummary:
    """Рекурсивно сканирует локальную папку (только чтение) и обновляет очередь в SQLite."""
    root = os.path.abspath(strip_long_prefix(source_dir))
    if not os.path.isdir(to_long_path(root)):
        raise FileNotFoundError(f"Папка не найдена или недоступна: {root}")

    summary = ScanSummary(source_dir=root)
    records: list[dict[str, Any]] = []
    now = time.time()
    visited_dirs: set[tuple[int, int]] = set()
    try:
        root_st = os.stat(to_long_path(root))
        if root_st.st_ino:
            visited_dirs.add((root_st.st_dev, root_st.st_ino))
    except OSError:
        pass

    stack = [root]
    while stack:
        if stop_event is not None and stop_event.is_set():
            raise CancelledError("Сканирование папки остановлено")
        current = stack.pop()
        try:
            with os.scandir(to_long_path(current)) as it:
                entries = list(it)
        except OSError as e:
            log.warning("Не удалось прочитать папку %s: %s", current, e)
            summary.unreadable_dirs += 1
            continue

        for entry in entries:
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Сканирование папки остановлено")
            try:
                st = entry.stat(follow_symlinks=False)
                if _is_reparse_or_symlink(entry, st):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if should_skip_dir(entry.name):
                        continue
                    dir_key = (st.st_dev, st.st_ino)
                    if st.st_ino and dir_key in visited_dirs:
                        continue
                    if st.st_ino:
                        visited_dirs.add(dir_key)
                    stack.append(strip_long_prefix(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError as e:
                log.warning("Не удалось получить сведения о %s: %s", entry.path, e)
                summary.unreadable_files += 1
                continue

            if settle_sec > 0:
                age = now - st.st_mtime
                # Пропускаем только файлы, записанные в последние settle_sec секунд
                # (будущие даты камеры с age < -2.0 не блокируются)
                if -2.0 <= age < settle_sec:
                    continue

            full_path = strip_long_prefix(entry.path)
            rel_path = os.path.relpath(full_path, root)
            verdict = classify_local_file(full_path, st.st_size, verify_magic=verify_magic)
            if verdict.io_error:
                summary.unreadable_files += 1
            shot_time = (
                extract_shot_time(full_path, verdict.media_kind, st.st_mtime)
                if verdict.accepted and verdict.media_kind
                else None
            )
            records.append(
                {
                    "local_path": full_path,
                    "rel_path": rel_path,
                    "name": entry.name,
                    "accepted": verdict.accepted,
                    "io_error": verdict.io_error,
                    "media_kind": verdict.media_kind,
                    "mime": verdict.mime,
                    "reason": verdict.reason,
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "shot_time": shot_time,
                }
            )
            summary.scanned_total += 1
            if verdict.accepted:
                if verdict.media_kind == "video":
                    summary.videos += 1
                else:
                    summary.photos += 1
            elif not verdict.io_error:
                summary.unsupported += 1
            if on_progress is not None and (summary.scanned_total % 50 == 0):
                on_progress(summary.scanned_total)

    if on_progress is not None:
        on_progress(summary.scanned_total)

    diff = state.upsert_scanned_files(
        records,
        root,
        prune_missing=(summary.unreadable_dirs == 0 and settle_sec == 0.0),
    )
    summary.added = diff["added"]
    summary.updated = diff["updated"]
    summary.unchanged = diff["unchanged"]
    summary.pruned = diff["pruned"]
    return summary


class UploadEngine:
    """Координирует preflight, индексацию /photounlim, скан, параллельную загрузку и watch-режим."""

    def __init__(
        self,
        state: UploadState,
        *,
        workers: int = DEFAULT_WORKERS,
        chunk_mb: int = DEFAULT_CHUNK_MB,
        strict_quota_guard: bool = True,
        allow_quota_count: bool = False,
        refresh_remote_index: bool = False,
        cookies_file: str | os.PathLike | None = None,
        transport: UploadTransport | None = None,
        session_ctx: SessionContext | None = None,
        quota_fetcher: Callable[[], QuotaSnapshot] | None = None,
        remote_indexer: Callable[[], int] | None = None,
        on_event: EventCallback | None = None,
        progress: LiveProgress | None = None,
        stop_event: threading.Event | None = None,
    ):
        self.state = state
        self.workers = max(1, min(MAX_WORKERS, int(workers)))
        self.chunk_mb = chunk_mb
        self.strict_quota_guard = strict_quota_guard
        self.allow_quota_count = allow_quota_count
        self.refresh_remote_index = refresh_remote_index
        self.cookies_file = cookies_file
        self._custom_transport = transport
        self._session_ctx = session_ctx
        self._owns_session_ctx = session_ctx is None
        self._quota_fetcher = quota_fetcher
        self._remote_indexer = remote_indexer
        self._on_event = on_event
        self.progress = progress or LiveProgress()
        self.stop_event = stop_event or threading.Event()

        self._lock = threading.Lock()
        self._dup_cond = threading.Condition()
        self._inflight_hashes: set[tuple[str, int]] = set()
        self._source_root: str = ""
        self._fatal_code: int | None = None
        self._fatal_message: str = ""
        self._outage_files: set[int] = set()
        self._cooldown_until: float = 0.0
        self._files_since_quota: int = 0
        self._bytes_since_quota: int = 0
        self._quota_check_failures: int = 0
        self._summary = RunSummary(exit_code=EXIT_OK)

    def close(self) -> None:
        if self._owns_session_ctx and self._session_ctx is not None:
            self._session_ctx.close()
            self._session_ctx = None

    def _emit(self, kind: str, text: str) -> None:
        clean = redact_urls(text)
        log.info("[%s] %s", kind, clean)
        if self._on_event is not None:
            try:
                self._on_event(kind, clean)
            except Exception as e:  # noqa: BLE001
                log.warning("Ошибка обработчика событий UI/CLI: %s", e)

    def _init_session(self) -> SessionContext:
        if self._session_ctx is not None:
            return self._session_ctx
        sess = make_session(cookies_file=self.cookies_file)
        try:
            sk, uid = get_sk_uid(sess)
        except BaseException:
            sess.close()
            raise
        self._session_ctx = SessionContext(sess, sk, uid, owns_session=True)
        self._owns_session_ctx = True
        return self._session_ctx

    def _fetch_quota(self) -> QuotaSnapshot:
        if self._quota_fetcher is not None:
            return self._quota_fetcher()
        ctx = self._init_session()
        sk, uid, gen = ctx.get()
        try:
            return fetch_quota_snapshot(ctx.thread_session(), sk, uid)
        except AuthExpiredError:
            sk, uid, _ = ctx.refresh_if_stale(gen)
            return fetch_quota_snapshot(ctx.thread_session(), sk, uid)

    def _sync_remote_if_needed(self) -> None:
        last_indexed = self.state.get_meta("remote_indexed_at")
        stale = (
            self.refresh_remote_index
            or not isinstance(last_indexed, (int, float))
            or (time.time() - float(last_indexed)) > REMOTE_INDEX_TTL_SEC
        )
        if not stale and self.state.remote_count() > 0:
            self._emit(
                "info",
                f"Использую сохранённый список Безлимита ({files_text(self.state.remote_count())})",
            )
            return

        self.progress.set_phase("indexing")
        self._emit(
            "run", "Читаю список файлов в Безлимите (/photounlim), чтобы пропустить дубликаты…"
        )

        def _on_prog(count: int) -> None:
            with self.progress.lock:
                self.progress.indexed_remote = count

        if self._remote_indexer is not None:
            cnt = self._remote_indexer()
        else:
            ctx = self._init_session()
            cnt = sync_remote_index(
                ctx,
                None,
                None,
                self.state,
                stop_event=self.stop_event,
                on_progress=_on_prog,
            )
        self._emit("ok", f"В Безлимите уже есть {files_text(cnt)} — их повторно грузить не буду")

    def _set_fatal(self, code: int, message: str) -> None:
        with self._lock:
            if self._fatal_code is None:
                self._fatal_code = code
                self._fatal_message = redact_urls(message)
        with self._dup_cond:
            self._dup_cond.notify_all()

    def _has_fatal(self) -> bool:
        with self._lock:
            return self._fatal_code is not None

    def _set_cooldown(self, seconds: float) -> None:
        until = time.time() + seconds
        with self._lock:
            if until > self._cooldown_until:
                self._cooldown_until = until
        with self.progress.lock:
            if until > self.progress.cooldown_until:
                self.progress.cooldown_until = until

    def _wait_cooldown(self) -> None:
        while not self.stop_event.is_set() and not self._has_fatal():
            with self._lock:
                wait = self._cooldown_until - time.time()
            if wait <= 0:
                return
            self.stop_event.wait(min(wait, 1.0))

    def _safe_db_mark(self, fn: Callable[[], Any], context: str) -> Any:
        try:
            return fn()
        except sqlite3.Error as db_err:
            msg = f"Ошибка базы состояния SQLite ({context}): {db_err}"
            log.exception(msg)
            self._emit("err", msg)
            self._set_fatal(EXIT_LOCAL_IO, msg)
            return None

    def _maybe_check_quota_drift(self, guard: QuotaGuard, added_bytes: int) -> None:
        need_check = False
        delta_files = 0
        delta_bytes = 0
        with self._lock:
            self._files_since_quota += 1
            self._bytes_since_quota += added_bytes
            if (
                self._files_since_quota >= QUOTA_CHECK_EVERY_FILES
                or self._bytes_since_quota >= QUOTA_CHECK_EVERY_BYTES
            ):
                need_check = True
                delta_files = self._files_since_quota
                delta_bytes = self._bytes_since_quota
                self._files_since_quota = 0
                self._bytes_since_quota = 0
        if not need_check:
            return
        try:
            snap = self._fetch_quota()
            guard.check_drift(snap, delta_bytes)
            with self._lock:
                self._quota_check_failures = 0
        except QuotaGuardError as e:
            self._emit("err", str(e))
            self._set_fatal(EXIT_QUOTA_DRIFT, str(e))
        except AuthExpiredError as e:
            self._emit("err", f"Данные входа истекли при проверке квоты: {e}")
            self._set_fatal(EXIT_AUTH, str(e))
        except (CaptchaError, RateLimitError) as e:
            with self._lock:
                self._files_since_quota += delta_files
                self._bytes_since_quota += delta_bytes
            self._emit("warn", f"Пауза при проверке квоты: {e}")
            self._set_cooldown(35.0)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._files_since_quota += delta_files
                self._bytes_since_quota += delta_bytes
                self._quota_check_failures += 1
                fails = self._quota_check_failures
            log.warning(
                "Не удалось перепроверить квоту (%d/%d): %s", fails, MAX_CONSECUTIVE_QUOTA_ERRORS, e
            )
            if (
                self.strict_quota_guard
                and guard.baseline.unlimited_free
                and fails >= MAX_CONSECUTIVE_QUOTA_ERRORS
            ):
                msg = f"Не удаётся проверить квоту Диска {fails} раза подряд — останавливаю загрузку ради безопасности"
                self._emit("err", msg)
                self._set_fatal(EXIT_OUTAGE, msg)

    def _worker_loop(self, worker_id: int, transport: UploadTransport, guard: QuotaGuard) -> None:
        try:
            while not self.stop_event.is_set() and not self._has_fatal():
                self._wait_cooldown()
                if self.stop_event.is_set() or self._has_fatal():
                    break

                with self._lock:
                    skip_ids = set(self._outage_files)
                row = self.state.take_next_pending(exclude_ids=skip_ids or None)
                if row is None:
                    break

                try:
                    self._process_one(worker_id, row, transport, guard)
                except CancelledError:
                    self._safe_db_mark(
                        partial(
                            self.state.mark_retry_or_fail,
                            row.id,
                            "остановлено пользователем",
                            burn_attempt=False,
                        ),
                        "cancel",
                    )
                    break
                except AuthExpiredError as e:
                    self._set_fatal(EXIT_AUTH, str(e))
                    self._emit("err", f"Данные входа истекли: {e}")
                    self._safe_db_mark(
                        partial(
                            self.state.mark_retry_or_fail,
                            row.id,
                            str(e),
                            burn_attempt=False,
                        ),
                        "auth_expired",
                    )
                    break
                except QuotaGuardError as e:
                    self._set_fatal(EXIT_QUOTA_DRIFT, str(e))
                    self._emit("err", str(e))
                    self._safe_db_mark(
                        partial(
                            self.state.mark_retry_or_fail,
                            row.id,
                            str(e),
                            burn_attempt=False,
                        ),
                        "quota_guard",
                    )
                    break
                except (CaptchaError, RateLimitError) as e:
                    pause = 60.0 if isinstance(e, CaptchaError) else 35.0
                    self._set_cooldown(pause)
                    self._emit("warn", f"{e} — пауза {int(pause)} сек")
                    self._safe_db_mark(
                        partial(
                            self.state.mark_retry_or_fail,
                            row.id,
                            str(e),
                            burn_attempt=False,
                        ),
                        "rate_limit",
                    )
                except YandexUnavailableError as e:
                    with self._lock:
                        self._outage_files.add(row.id)
                        outage_cnt = len(self._outage_files)
                    if outage_cnt >= OUTAGE_STREAK_LIMIT:
                        msg = "Яндекс не отвечает на загрузку нескольких файлов подряд — попробуйте позже"
                        self._set_fatal(EXIT_OUTAGE, msg)
                        self._emit("err", msg)
                    else:
                        self._set_cooldown(10.0)
                        self._emit("warn", f"Яндекс временно не ответил ({row.name}): {e}")
                    self._safe_db_mark(
                        partial(
                            self.state.mark_retry_or_fail,
                            row.id,
                            str(e),
                            burn_attempt=False,
                        ),
                        "outage",
                    )
                    if outage_cnt >= OUTAGE_STREAK_LIMIT:
                        break
                except FileNotFoundError as e:
                    parent_dir = os.path.dirname(row.local_path)
                    drive_gone = (
                        not os.path.isdir(to_long_path(self._source_root))
                        if self._source_root
                        else not os.path.isdir(to_long_path(parent_dir))
                    )
                    if drive_gone:
                        msg = f"Папка на компьютере стала недоступна (отключён диск?): {self._source_root or parent_dir}"
                        self._set_fatal(EXIT_LOCAL_IO, msg)
                        self._emit("err", msg)
                        self._safe_db_mark(
                            partial(
                                self.state.mark_retry_or_fail,
                                row.id,
                                msg,
                                burn_attempt=False,
                            ),
                            "drive_gone",
                        )
                        break
                    self._safe_db_mark(
                        partial(self.state.mark_failed_permanent, row.id, str(e)),
                        "file_missing",
                    )
                    with self._lock:
                        self._summary.failed += 1
                    self._emit("warn", f"Файл пропал с диска: {row.rel_path}")
                except OSError as e:
                    parent_dir = os.path.dirname(row.local_path)
                    drive_gone = (
                        not os.path.isdir(to_long_path(self._source_root))
                        if self._source_root
                        else not os.path.isdir(to_long_path(parent_dir))
                    )
                    if drive_gone:
                        msg = f"Папка на компьютере стала недоступна (отключён диск?): {self._source_root or parent_dir}"
                        self._set_fatal(EXIT_LOCAL_IO, msg)
                        self._emit("err", msg)
                        self._safe_db_mark(
                            partial(
                                self.state.mark_retry_or_fail,
                                row.id,
                                msg,
                                burn_attempt=False,
                            ),
                            "oserror_drive_gone",
                        )
                        break
                    new_st = self._safe_db_mark(
                        partial(
                            self.state.mark_retry_or_fail,
                            row.id,
                            f"ошибка чтения файла: {e}",
                            burn_attempt=True,
                        ),
                        "oserror_read",
                    )
                    if new_st == "failed":
                        with self._lock:
                            self._summary.failed += 1
                    self._emit("warn", f"Не удалось прочитать {row.rel_path}: {e}")
                except UnlimApiError as e:
                    clean_err = redact_urls(str(e))
                    new_status = self._safe_db_mark(
                        partial(
                            self.state.mark_retry_or_fail,
                            row.id,
                            clean_err,
                            burn_attempt=True,
                            max_attempts=MAX_ATTEMPTS,
                        ),
                        "api_error",
                    )
                    if new_status == "failed":
                        with self._lock:
                            self._summary.failed += 1
                        self._emit("err", f"Не удалось загрузить {row.rel_path}: {clean_err}")
                    else:
                        self._emit("warn", f"Повтор позже для {row.name}: {clean_err}")
                except sqlite3.Error as e:
                    msg = f"Ошибка базы состояния SQLite: {e}"
                    log.exception(msg)
                    self._emit("err", msg)
                    self._set_fatal(EXIT_LOCAL_IO, msg)
                    break
                except Exception as e:  # noqa: BLE001
                    if is_sqlite_disk_error(e):
                        self._emit("err", f"Нет места на локальном диске под базу состояния: {e}")
                        self._set_fatal(EXIT_LOCAL_IO, str(e))
                        break
                    clean_err = redact_urls(f"{type(e).__name__}: {e}")
                    new_status = self._safe_db_mark(
                        partial(
                            self.state.mark_retry_or_fail,
                            row.id,
                            clean_err,
                            burn_attempt=True,
                        ),
                        "unexpected_file_error",
                    )
                    if new_status == "failed":
                        with self._lock:
                            self._summary.failed += 1
                    self._emit("err", f"Ошибка для {row.rel_path}: {clean_err}")
                finally:
                    self.progress.clear_active(worker_id)
        except Exception as fatal_err:  # noqa: BLE001 — воркер никогда не должен умирать молча
            log.exception("Критическая ошибка потока загрузки w%d", worker_id)
            code = (
                EXIT_LOCAL_IO
                if isinstance(fatal_err, sqlite3.Error) or is_sqlite_disk_error(fatal_err)
                else EXIT_PARTIAL
            )
            msg = f"Сбой воркера загрузки: {redact_urls(str(fatal_err))}"
            self._emit("err", msg)
            self._set_fatal(code, msg)

    def _process_one(
        self,
        worker_id: int,
        row: UploadRow,
        transport: UploadTransport,
        guard: QuotaGuard,
    ) -> None:
        # Проверка принадлежности файла исходной папке (защита от подмены симлинком после скана)
        if self._source_root and not is_path_inside(row.local_path, self._source_root):
            self.state.mark_failed_permanent(row.id, "файл указывает за пределы выбранной папки")
            with self._lock:
                self._summary.failed += 1
            self._emit("warn", f"Пропущен небезопасный путь: {row.rel_path}")
            return

        # Актуальный stat перед хешированием/загрузкой (ловим изменение размера после скана)
        st = os.stat(to_long_path(row.local_path))
        if st.st_size <= 0:
            self.state.mark_skipped_unsupported(row.id, "пустой файл (0 байт)")
            return
        if st.st_size != row.size or st.st_mtime_ns != row.mtime_ns:
            row.size = st.st_size
            row.mtime_ns = st.st_mtime_ns
            row.md5 = None
            row.sha256 = None
            self.state.update_file_stat(row.id, st.st_size, st.st_mtime_ns)

        size_err = guard.check_file_size(row.size)
        if size_err:
            self.state.mark_skipped_limit(row.id, size_err)
            with self._lock:
                self._summary.skipped_limit += 1
            self._emit("warn", f"Пропущен {row.rel_path}: {size_err}")
            return

        md5, sha256 = row.md5, row.sha256
        if not md5 or not sha256:
            self.progress.set_active(worker_id, row.name, 0, row.size, "hashing")
            md5, sha256, bytes_read = compute_hashes_with_size(
                row.local_path, stop_event=self.stop_event
            )
            if bytes_read != row.size:
                row.size = bytes_read
                self.state.update_file_stat(
                    row.id, bytes_read, os.stat(to_long_path(row.local_path)).st_mtime_ns
                )
                size_err2 = guard.check_file_size(row.size)
                if size_err2:
                    self.state.mark_skipped_limit(row.id, size_err2)
                    with self._lock:
                        self._summary.skipped_limit += 1
                    return

        hash_key = (md5.lower(), int(row.size))

        # Атомарное владение (md5, size): одновременно этот хеш обрабатывает только один воркер.
        # Второй воркер ждёт завершения первого, затем проверяет БД — если первый загрузился,
        # второй отметится skipped_dup_local с его remote_id; если первый упал, второй загрузит файл.
        with self._dup_cond:
            while hash_key in self._inflight_hashes:
                if self.stop_event.is_set() or self._has_fatal():
                    raise CancelledError("Остановлено во время ожидания дубля")
                self._dup_cond.wait(timeout=0.5)
            self._inflight_hashes.add(hash_key)

        try:
            verdict, _ref = self.state.mark_hashes_and_check_dup(row.id, md5, sha256, row.size)
            if verdict == "skipped_remote":
                with self._lock:
                    self._summary.skipped_remote += 1
                return
            if verdict == "skipped_dup_local":
                with self._lock:
                    self._summary.skipped_dup_local += 1
                return

            self.progress.set_active(worker_id, row.name, 0, row.size, "uploading")

            def _on_prog(sent: int, total: int) -> None:
                self.progress.set_active(worker_id, row.name, sent, total, "uploading")

            result = transport.upload_file(
                row,
                md5=md5,
                sha256=sha256,
                on_progress=_on_prog,
                stop_event=self.stop_event,
            )

            with self._lock:
                self._outage_files.clear()

            if result.outcome == "duplicate_remote":
                self.state.mark_skipped_remote(
                    row.id,
                    result.remote_id,
                    result.note or "уже есть в Безлимите",
                    index_name=result.remote_name,
                    index_size=row.size,
                    index_md5=md5,
                    index_time=row.shot_time,
                )
                with self._lock:
                    self._summary.skipped_remote += 1
                return

            self.state.mark_uploaded(
                row.id,
                result.remote_id,
                result.remote_name,
                result.note,
                index_size=row.size,
                index_md5=md5,
                index_time=row.shot_time,
            )
            with self._lock:
                if result.outcome == "hardlinked":
                    self._summary.hardlinked += 1
                else:
                    self._summary.uploaded += 1
                    self._summary.bytes_uploaded += row.size
            with self.progress.lock:
                self.progress.session_uploaded += 1

            self._maybe_check_quota_drift(guard, row.size)
        finally:
            with self._dup_cond:
                self._inflight_hashes.discard(hash_key)
                self._dup_cond.notify_all()

    def run_once(
        self, source_dir: str, *, settle_sec: float = 0.0, _close_session: bool = True
    ) -> RunSummary:
        """Выполняет один полный проход: preflight -> индекс -> скан -> загрузка."""
        self._summary = RunSummary(exit_code=EXIT_OK)
        self._fatal_code = None
        self._fatal_message = ""
        self._outage_files.clear()
        self._quota_check_failures = 0
        self._source_root = os.path.abspath(strip_long_prefix(source_dir))

        own_transport: WebModelsTransport | None = None
        try:
            with keep_awake():
                # 1. Preflight и Quota Guard
                self.progress.set_phase("preflight")
                try:
                    quota_before = self._fetch_quota()
                    self._summary.quota_before = quota_before
                    guard = QuotaGuard(
                        quota_before,
                        strict=self.strict_quota_guard,
                        allow_quota_count=self.allow_quota_count,
                    )
                    guard.check_preflight()
                    if quota_before.warning:
                        self._emit("warn", quota_before.warning)
                    else:
                        self._emit(
                            "ok",
                            f"Доступ подтверждён: в Безлимите уже {human_bytes(quota_before.actual_photounlim_size)}, "
                            f"лимит файла {human_bytes(quota_before.filesize_limit)}",
                        )
                except (CookiesFormatError, AuthExpiredError) as e:
                    self.progress.set_phase("error")
                    self._emit("err", str(e))
                    self._summary.exit_code = EXIT_AUTH
                    self._summary.message = str(e)
                    return self._summary
                except QuotaGuardError as e:
                    self.progress.set_phase("error")
                    self._emit("err", str(e))
                    self._summary.exit_code = EXIT_QUOTA_DRIFT
                    self._summary.message = str(e)
                    return self._summary
                except (YandexUnavailableError, UnlimApiError, RuntimeError) as e:
                    self.progress.set_phase("error")
                    self._emit("err", str(e))
                    self._summary.exit_code = EXIT_OUTAGE
                    self._summary.message = str(e)
                    return self._summary
                except sqlite3.Error as e:
                    self.progress.set_phase("error")
                    self._emit("err", f"Ошибка базы состояния: {e}")
                    self._summary.exit_code = EXIT_LOCAL_IO
                    self._summary.message = str(e)
                    return self._summary

                # 2. Восстановление после обрыва + облачный индекс
                try:
                    released = self.state.release_in_progress()
                except sqlite3.Error as e:
                    self.progress.set_phase("error")
                    self._emit("err", f"Ошибка базы состояния: {e}")
                    self._summary.exit_code = EXIT_LOCAL_IO
                    self._summary.message = str(e)
                    return self._summary
                if released:
                    self._emit(
                        "info",
                        f"Вернул в очередь {files_text(released)} после незавершённого запуска",
                    )

                try:
                    self._sync_remote_if_needed()
                except CancelledError:
                    self.progress.set_phase("idle")
                    self._summary.exit_code = EXIT_INTERRUPTED
                    self._summary.message = "Остановлено пользователем"
                    return self._summary
                except AuthExpiredError as e:
                    self.progress.set_phase("error")
                    self._emit("err", str(e))
                    self._summary.exit_code = EXIT_AUTH
                    self._summary.message = str(e)
                    return self._summary
                except (YandexUnavailableError, UnlimApiError, RuntimeError) as e:
                    self.progress.set_phase("error")
                    self._emit("err", f"Не удалось получить список Безлимита: {e}")
                    self._summary.exit_code = EXIT_OUTAGE
                    self._summary.message = str(e)
                    return self._summary
                except sqlite3.Error as e:
                    self.progress.set_phase("error")
                    self._emit("err", f"Ошибка сохранения индекса Безлимита: {e}")
                    self._summary.exit_code = EXIT_LOCAL_IO
                    self._summary.message = str(e)
                    return self._summary

                # 3. Сканирование локальной папки
                self.progress.set_phase("scanning")
                self._emit("run", f"Сканирую папку {source_dir}…")
                try:

                    def _on_scan(n: int) -> None:
                        with self.progress.lock:
                            self.progress.scanned_files = n

                    scan = scan_source_folder(
                        source_dir,
                        self.state,
                        settle_sec=settle_sec,
                        stop_event=self.stop_event,
                        on_progress=_on_scan,
                    )
                    deduped = self.state.apply_remote_dedup()
                    st_before = self.state.stats()
                except CancelledError:
                    self.progress.set_phase("idle")
                    self._summary.exit_code = EXIT_INTERRUPTED
                    self._summary.message = "Остановлено пользователем"
                    return self._summary
                except (OSError, sqlite3.Error) as e:
                    self.progress.set_phase("error")
                    self._emit("err", str(e))
                    self._summary.exit_code = EXIT_LOCAL_IO
                    self._summary.message = str(e)
                    return self._summary

                self._summary.skipped_remote += deduped
                if scan.unreadable_dirs or scan.unreadable_files:
                    self._emit(
                        "warn",
                        f"Не удалось прочитать часть локальных данных: папок {scan.unreadable_dirs}, "
                        f"файлов {scan.unreadable_files} (проверьте права доступа)",
                    )
                self._emit(
                    "info",
                    f"Найдено: {scan.photos} фото, {scan.videos} видео "
                    f"(к загрузке {files_text(st_before['pending'])}, "
                    f"пропущено неподходящих: {scan.unsupported})",
                )

                if st_before["pending"] == 0:
                    if st_before["failed"] > 0 or scan.unreadable_dirs > 0:
                        self.progress.set_phase("done")
                        self._summary.exit_code = EXIT_PARTIAL
                        self._summary.message = (
                            f"Новых файлов к загрузке нет, но есть ошибки ({st_before['failed']}) — "
                            "нажмите «Повторить ошибки»"
                        )
                        self._emit("warn", self._summary.message)
                        return self._summary
                    self.progress.set_phase("done")
                    self._summary.message = "Все фото и видео из папки уже загружены в Безлимит"
                    self._emit("ok", self._summary.message)
                    return self._summary

                # 4. Запуск воркеров (с повторным проходом для хвоста <= OUTAGE_STREAK_LIMIT)
                self.progress.set_phase("uploading")
                if self._custom_transport is not None:
                    transport: UploadTransport = self._custom_transport
                else:
                    own_transport = WebModelsTransport(self._init_session(), chunk_mb=self.chunk_mb)
                    transport = own_transport

                for tail_pass in range(MAX_TAIL_OUTAGE_PASSES):
                    threads = [
                        threading.Thread(
                            target=self._worker_loop,
                            args=(idx + 1, transport, guard),
                            name=f"upload-w{idx + 1}",
                            daemon=True,
                        )
                        for idx in range(self.workers)
                    ]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join()

                    if self._has_fatal() or self.stop_event.is_set() or not self._outage_files:
                        break
                    if len(self._outage_files) >= OUTAGE_STREAK_LIMIT:
                        break
                    if tail_pass + 1 < MAX_TAIL_OUTAGE_PASSES:
                        self._emit(
                            "info",
                            f"Повторяю {files_text(len(self._outage_files))}, на которых Яндекс временно не ответил…",
                        )
                        self._outage_files.clear()
                        self._wait_cooldown()

                # Финальная сверка квоты
                if (
                    not self._has_fatal()
                    and not self.stop_event.is_set()
                    and (self._summary.bytes_uploaded > 0 or self._summary.hardlinked > 0)
                ):
                    try:
                        quota_after = self._fetch_quota()
                        self._summary.quota_after = quota_after
                        guard.check_drift(quota_after, self._bytes_since_quota)
                    except QuotaGuardError as e:
                        self._emit("err", str(e))
                        self._set_fatal(EXIT_QUOTA_DRIFT, str(e))
                    except AuthExpiredError as e:
                        self._emit("err", f"Данные входа истекли при финальной проверке: {e}")
                        self._set_fatal(EXIT_AUTH, str(e))
                    except Exception as e:  # noqa: BLE001
                        self._emit("warn", f"Финальная проверка квоты не удалась: {e}")

            if self._fatal_code is not None:
                self.progress.set_phase("error")
                self._summary.exit_code = self._fatal_code
                self._summary.message = self._fatal_message
                return self._summary

            if self._outage_files and not self.stop_event.is_set():
                msg = "Яндекс временно не принял оставшиеся файлы — попробуйте снова чуть позже"
                self.progress.set_phase("error")
                self._emit("warn", msg)
                self._summary.exit_code = EXIT_OUTAGE
                self._summary.message = msg
                return self._summary

            if self.stop_event.is_set():
                self.progress.set_phase("idle")
                self._summary.exit_code = EXIT_INTERRUPTED
                self._summary.message = (
                    "Остановлено пользователем — при следующем запуске продолжу с того же места"
                )
                self._emit("info", self._summary.message)
                return self._summary

            final_stats = self.state.stats()
            if final_stats["failed"] > 0 or scan.unreadable_dirs > 0:
                self.progress.set_phase("done")
                self._summary.exit_code = EXIT_PARTIAL
                self._summary.message = (
                    f"Загрузка завершена с ошибками: загружено {self._summary.uploaded + self._summary.hardlinked}, "
                    f"ошибок {final_stats['failed']}"
                )
                self._emit("warn", self._summary.message)
                return self._summary

            self.progress.set_phase("done")
            done_now = self._summary.uploaded + self._summary.hardlinked
            self._summary.exit_code = EXIT_OK
            self._summary.message = (
                f"Готово: загружено {files_text(done_now)} ({human_bytes(self._summary.bytes_uploaded)}), "
                f"дубликатов пропущено {self._summary.skipped_remote + self._summary.skipped_dup_local}"
            )
            self._emit("ok", self._summary.message)
            return self._summary
        finally:
            if own_transport is not None:
                own_transport.close()
            if _close_session:
                self.close()

    def run_watch(self, source_dir: str, *, interval_sec: int = 60) -> RunSummary:
        """Непрерывное наблюдение за папкой: подгружает новые фото и видео по мере появления."""
        interval = max(10, int(interval_sec))
        try:
            last_res = self.run_once(source_dir, settle_sec=WATCH_SETTLE_SEC, _close_session=False)
            if last_res.exit_code in (EXIT_AUTH, EXIT_QUOTA_DRIFT, EXIT_INTERRUPTED):
                return last_res

            self._emit("info", f"Включён режим наблюдения за папкой (проверка каждые {interval} с)")
            while not self.stop_event.is_set():
                self.progress.set_phase("watching")
                if self.stop_event.wait(interval):
                    break
                self.refresh_remote_index = False
                last_res = self.run_once(
                    source_dir, settle_sec=WATCH_SETTLE_SEC, _close_session=False
                )
                if last_res.exit_code in (EXIT_AUTH, EXIT_QUOTA_DRIFT, EXIT_INTERRUPTED):
                    return last_res

            self.progress.set_phase("idle")
            last_res.exit_code = EXIT_INTERRUPTED
            last_res.message = "Наблюдение остановлено"
            return last_res
        finally:
            self.close()
