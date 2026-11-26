"""SQLite WAL хранилище очереди загрузки (upload_state.db).

Статусы файла в upload_files:
  pending             — ждёт загрузки
  uploading           — взят воркером прямо сейчас
  uploaded            — успешно загружен в /photounlim (или hardlinked)
  skipped_remote      — такой же файл (size + md5) уже есть в /photounlim
  skipped_dup_local   — точный дубль другого уже загруженного файла из этой же папки
  skipped_limit       — размер файла превышает лимит тарифа (filesize_limit)
  skipped_unsupported — не фото/видео, RAW/DNG, 0 байт или повреждённый заголовок
  failed              — исчерпаны попытки загрузки или ошибка чтения локального файла
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from uploader import paths
from uploader.utils import name_with_tag, path_key, sanitize_remote_name, sqlite_tx

log = logging.getLogger("unlim_uploader")

MAX_ATTEMPTS = 5

_INIT_LOCK = threading.Lock()
_INITIALIZED_DBS: dict[str, tuple[int, int]] = {}  # abs_path -> (st_dev, st_ino)

SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    local_path    TEXT NOT NULL,
    path_key      TEXT UNIQUE NOT NULL,
    rel_path      TEXT NOT NULL,
    name          TEXT NOT NULL,
    media_kind    TEXT,
    mime          TEXT,
    size          INTEGER NOT NULL DEFAULT 0,
    mtime_ns      INTEGER NOT NULL DEFAULT 0,
    md5           TEXT,
    sha256        TEXT,
    shot_time     REAL,
    remote_name   TEXT,
    remote_id     TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    uploaded_at   REAL,
    updated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_upload_status ON upload_files(status);
CREATE INDEX IF NOT EXISTS idx_upload_md5_size ON upload_files(md5, size);
CREATE INDEX IF NOT EXISTS idx_upload_remote_name ON upload_files(remote_name);

CREATE TABLE IF NOT EXISTS remote_unlim_index (
    file_id       TEXT PRIMARY KEY,
    name          TEXT,
    size          INTEGER NOT NULL DEFAULT 0,
    md5           TEXT,
    file_time     REAL
);
CREATE INDEX IF NOT EXISTS idx_remote_md5_size ON remote_unlim_index(md5, size);
CREATE INDEX IF NOT EXISTS idx_remote_name ON remote_unlim_index(name);

CREATE TABLE IF NOT EXISTS upload_meta (
    key           TEXT PRIMARY KEY,
    value         TEXT
);
"""


@dataclass
class UploadRow:
    id: int
    local_path: str
    rel_path: str
    name: str
    media_kind: str
    mime: str
    size: int
    mtime_ns: int
    md5: str | None
    sha256: str | None
    shot_time: float | None
    remote_name: str
    remote_id: str | None
    status: str
    error: str | None
    attempts: int


def connect_db(
    db_path: str | os.PathLike | None = None, timeout: float = 60.0
) -> sqlite3.Connection:
    p = os.fspath(db_path or paths.DB_FILE)
    parent = os.path.dirname(os.path.abspath(p))
    if parent:
        os.makedirs(parent, exist_ok=True)
    c = sqlite3.connect(p, timeout=timeout, isolation_level=None)
    try:
        c.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        c.execute("PRAGMA synchronous=NORMAL")
        c.row_factory = sqlite3.Row
    except BaseException:
        c.close()
        raise
    return c


class UploadState:
    """Потокобезопасная работа с upload_state.db через короткие BEGIN IMMEDIATE транзакции."""

    def __init__(self, db_path: str | os.PathLike | None = None):
        self.db_path = os.path.abspath(os.fspath(db_path or paths.DB_FILE))
        self.init_schema()

    def _conn(self) -> sqlite3.Connection:
        return connect_db(self.db_path)

    def init_schema(self, *, force: bool = False) -> None:
        with _INIT_LOCK:
            if not force and os.path.exists(self.db_path):
                try:
                    st = os.stat(self.db_path)
                    if _INITIALIZED_DBS.get(self.db_path) == (st.st_dev, st.st_ino):
                        return
                except OSError:
                    pass
            last_err: sqlite3.Error | None = None
            for attempt in range(5):
                c = self._conn()
                try:
                    c.execute("PRAGMA journal_mode=WAL")
                    c.executescript(SCHEMA)
                    if not os.name == "nt":
                        for candidate in (
                            self.db_path,
                            f"{self.db_path}-wal",
                            f"{self.db_path}-shm",
                        ):
                            if os.path.exists(candidate):
                                try:
                                    os.chmod(candidate, 0o600)
                                except OSError:
                                    pass
                    try:
                        st = os.stat(self.db_path)
                        _INITIALIZED_DBS[self.db_path] = (st.st_dev, st.st_ino)
                    except OSError:
                        pass
                    return
                except sqlite3.OperationalError as e:
                    last_err = e
                    if "locked" in str(e).lower() and attempt < 4:
                        time.sleep(0.05 * (attempt + 1))
                        continue
                    raise
                finally:
                    c.close()
            if last_err is not None:
                raise last_err

    # ── Метаданные ──────────────────────────────────────────────────────────

    def get_meta(self, key: str, default: Any = None) -> Any:
        c = self._conn()
        try:
            row = c.execute("SELECT value FROM upload_meta WHERE key = ?", (key,)).fetchone()
            if row is None or row["value"] is None:
                return default
            try:
                return json.loads(row["value"])
            except ValueError as e:
                log.debug("upload_meta[%s] не JSON (%s) — возвращаю строку", key, e)
                return row["value"]
        finally:
            c.close()

    def set_meta(self, key: str, value: Any) -> None:
        raw = json.dumps(value, ensure_ascii=False)
        c = self._conn()
        try:
            with sqlite_tx(c):
                c.execute(
                    "INSERT OR REPLACE INTO upload_meta (key, value) VALUES (?, ?)",
                    (key, raw),
                )
        finally:
            c.close()

    # ── Облачный индекс /photounlim ─────────────────────────────────────────

    def replace_remote_index(
        self,
        items: Iterable[tuple[str, str, int, str | None, float | None]],
        *,
        complete: bool = True,
    ) -> int:
        """Сохраняет снимок /photounlim в remote_unlim_index. При complete=False не перезатирает больший кеш."""
        rows = list(items)
        c = self._conn()
        try:
            with sqlite_tx(c):
                if not complete:
                    cur = int(c.execute("SELECT COUNT(*) FROM remote_unlim_index").fetchone()[0])
                    if cur > len(rows):
                        # Дополняем существующий индекс без стирания ранее собранных записей
                        c.executemany(
                            "INSERT OR REPLACE INTO remote_unlim_index (file_id, name, size, md5, file_time) "
                            "VALUES (?, ?, ?, ?, ?)",
                            rows,
                        )
                        return cur
                c.execute("DELETE FROM remote_unlim_index")
                c.executemany(
                    "INSERT OR REPLACE INTO remote_unlim_index (file_id, name, size, md5, file_time) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                if complete:
                    c.execute(
                        "INSERT OR REPLACE INTO upload_meta (key, value) VALUES ('remote_indexed_at', ?)",
                        (json.dumps(time.time()),),
                    )
            return len(rows)
        finally:
            c.close()

    def add_remote_entry(
        self,
        file_id: str,
        name: str,
        size: int,
        md5: str | None,
        file_time: float | None = None,
    ) -> None:
        c = self._conn()
        try:
            with sqlite_tx(c):
                c.execute(
                    "INSERT OR REPLACE INTO remote_unlim_index (file_id, name, size, md5, file_time) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (file_id, name, int(size), md5.lower() if md5 else None, file_time),
                )
        finally:
            c.close()

    def find_remote_by_hash(self, md5: str, size: int) -> str | None:
        c = self._conn()
        try:
            row = c.execute(
                "SELECT file_id FROM remote_unlim_index WHERE md5 = ? AND size = ? LIMIT 1",
                (md5.lower(), int(size)),
            ).fetchone()
            return row["file_id"] if row else None
        finally:
            c.close()

    def remote_count(self) -> int:
        c = self._conn()
        try:
            return int(c.execute("SELECT COUNT(*) FROM remote_unlim_index").fetchone()[0])
        finally:
            c.close()

    # ── Локальное сканирование и дедупликация ───────────────────────────────

    def upsert_scanned_files(
        self,
        records: list[dict[str, Any]],
        source_dir: str,
        *,
        prune_missing: bool = True,
    ) -> dict[str, int]:
        """Обновляет очередь по результатам сканирования папки.

        - Уже обработанные файлы с тем же (size, mtime_ns) не перехешируются и не перезагружаются.
        - Изменившиеся локально файлы (другой size или mtime_ns) возвращаются в pending.
        - При prune_missing=True (полный скан без нечитаемых подпапок) устаревшие pending-строки
          от старой папки или удалённых файлов убираются из очереди.
        """
        now = time.time()
        added = updated = unchanged = unsupported = pruned = 0
        seen_keys: set[str] = set()
        c = self._conn()
        try:
            with sqlite_tx(c):
                existing = {
                    r["path_key"]: dict(r)
                    for r in c.execute(
                        "SELECT id, path_key, size, mtime_ns, status, md5, sha256, remote_name "
                        "FROM upload_files"
                    )
                }
                used_remote_names = {
                    (r["remote_name"] or "").casefold()
                    for r in existing.values()
                    if r["remote_name"]
                }

                for rec in records:
                    pk = path_key(rec["local_path"])
                    seen_keys.add(pk)
                    prev = existing.get(pk)
                    accepted = bool(rec["accepted"])
                    io_error = bool(rec.get("io_error"))
                    size = int(rec["size"])
                    mtime_ns = int(rec["mtime_ns"])

                    if io_error:
                        desired_status = "failed"
                    elif accepted:
                        desired_status = "pending"
                    else:
                        desired_status = "skipped_unsupported"

                    if prev is not None:
                        same_file = prev["size"] == size and prev["mtime_ns"] == mtime_ns
                        if same_file:
                            prev_st = prev["status"]
                            # Не сбрасываем skipped_limit (файл > filesize_limit) обратно в pending на каждом скане
                            if accepted and prev_st == "skipped_limit":
                                unchanged += 1
                                continue
                            if accepted and prev_st == "skipped_unsupported":
                                pass
                            elif not accepted and not io_error and prev_st != "skipped_unsupported":
                                pass
                            elif io_error and prev_st != "failed":
                                pass
                            else:
                                unchanged += 1
                                continue

                        rname = prev["remote_name"] or (
                            self._pick_unique_name(rec["name"], pk, used_remote_names)
                            if accepted
                            else None
                        )
                        c.execute(
                            "UPDATE upload_files SET local_path=?, rel_path=?, name=?, media_kind=?, "
                            "mime=?, size=?, mtime_ns=?, md5=NULL, sha256=NULL, shot_time=?, "
                            "remote_name=?, remote_id=NULL, status=?, error=?, attempts=0, "
                            "uploaded_at=NULL, updated_at=? WHERE id=?",
                            (
                                rec["local_path"],
                                rec["rel_path"],
                                rec["name"],
                                rec.get("media_kind"),
                                rec.get("mime"),
                                size,
                                mtime_ns,
                                rec.get("shot_time"),
                                rname,
                                desired_status,
                                rec.get("reason"),
                                now,
                                prev["id"],
                            ),
                        )
                        if accepted:
                            updated += 1
                        else:
                            unsupported += 1
                        continue

                    rname = (
                        self._pick_unique_name(rec["name"], pk, used_remote_names)
                        if accepted
                        else None
                    )
                    c.execute(
                        "INSERT INTO upload_files "
                        "(local_path, path_key, rel_path, name, media_kind, mime, size, mtime_ns, "
                        "shot_time, remote_name, status, error, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            rec["local_path"],
                            pk,
                            rec["rel_path"],
                            rec["name"],
                            rec.get("media_kind"),
                            rec.get("mime"),
                            size,
                            mtime_ns,
                            rec.get("shot_time"),
                            rname,
                            desired_status,
                            rec.get("reason"),
                            now,
                        ),
                    )
                    if accepted:
                        added += 1
                    else:
                        unsupported += 1

                if prune_missing:
                    stale_ids = [
                        (row["id"],)
                        for pk, row in existing.items()
                        if row["status"] == "pending" and pk not in seen_keys
                    ]
                    if stale_ids:
                        c.executemany(
                            "DELETE FROM upload_files WHERE id=? AND status='pending'", stale_ids
                        )
                        pruned = len(stale_ids)

                c.execute(
                    "INSERT OR REPLACE INTO upload_meta (key, value) VALUES ('source_dir', ?)",
                    (json.dumps(source_dir, ensure_ascii=False),),
                )
                c.execute(
                    "INSERT OR REPLACE INTO upload_meta (key, value) VALUES ('last_scan_at', ?)",
                    (json.dumps(now),),
                )
        finally:
            c.close()
        return {
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "unsupported": unsupported,
            "pruned": pruned,
        }

    @staticmethod
    def _pick_unique_name(raw_name: str, seed: str, taken: set[str]) -> str:
        base = sanitize_remote_name(raw_name)
        if base.casefold() not in taken:
            taken.add(base.casefold())
            return base
        short = hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:6]
        cand = name_with_tag(base, f"_{short}")
        idx = 2
        while cand.casefold() in taken:
            cand = name_with_tag(base, f"_{short}_{idx}")
            idx += 1
        taken.add(cand.casefold())
        return cand

    def update_file_stat(self, row_id: int, size: int, mtime_ns: int) -> None:
        """Обновляет size/mtime_ns и сбрасывает кешированные хеши, если файл изменился после скана."""
        c = self._conn()
        try:
            with sqlite_tx(c):
                c.execute(
                    "UPDATE upload_files SET size=?, mtime_ns=?, md5=NULL, sha256=NULL, updated_at=? "
                    "WHERE id=?",
                    (int(size), int(mtime_ns), time.time(), row_id),
                )
        finally:
            c.close()

    def apply_remote_dedup(self) -> int:
        now = time.time()
        c = self._conn()
        try:
            with sqlite_tx(c):
                rows = c.execute(
                    "SELECT u.id, r.file_id FROM upload_files u "
                    "JOIN remote_unlim_index r ON r.md5 = u.md5 AND r.size = u.size "
                    "WHERE u.status = 'pending' AND u.md5 IS NOT NULL"
                ).fetchall()
                if not rows:
                    return 0
                c.executemany(
                    "UPDATE upload_files SET status='skipped_remote', remote_id=?, "
                    "error='уже есть в Безлимите', updated_at=? WHERE id=? AND status='pending'",
                    [(r["file_id"], now, r["id"]) for r in rows],
                )
                return len(rows)
        finally:
            c.close()

    def mark_hashes_and_check_dup(
        self,
        row_id: int,
        md5: str,
        sha256: str,
        size: int,
    ) -> tuple[str, str | None]:
        """Записывает MD5/SHA256 и атомарно проверяет дубли.

        Возвращает:
          ('ok', None)                       — можно загружать
          ('skipped_dup_local', other_rel)   — такой же файл из этой папки уже загружен / найден в облаке
          ('skipped_remote', remote_file_id) — такой же файл уже в /photounlim
        """
        md5_low = md5.lower()
        sha_low = sha256.lower()
        now = time.time()
        c = self._conn()
        try:
            with sqlite_tx(c):
                c.execute(
                    "UPDATE upload_files SET md5=?, sha256=?, size=?, updated_at=? WHERE id=?",
                    (md5_low, sha_low, int(size), now, row_id),
                )
                # 1. Уже успешно обработанный локальный дубль
                done_dup = c.execute(
                    "SELECT rel_path, remote_id FROM upload_files "
                    "WHERE id != ? AND md5 = ? AND size = ? "
                    "AND status IN ('uploaded', 'skipped_remote') "
                    "ORDER BY id ASC LIMIT 1",
                    (row_id, md5_low, int(size)),
                ).fetchone()
                if done_dup is not None:
                    c.execute(
                        "UPDATE upload_files SET status='skipped_dup_local', remote_id=?, "
                        "error=?, updated_at=? WHERE id=?",
                        (
                            done_dup["remote_id"],
                            f"дубль локального файла {done_dup['rel_path']}",
                            now,
                            row_id,
                        ),
                    )
                    return "skipped_dup_local", done_dup["rel_path"]

                # 2. Уже существует в /photounlim
                rem = c.execute(
                    "SELECT file_id FROM remote_unlim_index WHERE md5=? AND size=? LIMIT 1",
                    (md5_low, int(size)),
                ).fetchone()
                if rem is not None:
                    c.execute(
                        "UPDATE upload_files SET status='skipped_remote', remote_id=?, "
                        "error='уже есть в Безлимите', updated_at=? WHERE id=?",
                        (rem["file_id"], now, row_id),
                    )
                    return "skipped_remote", rem["file_id"]
                return "ok", None
        finally:
            c.close()

    # ── Очередь воркеров ────────────────────────────────────────────────────

    def release_in_progress(self) -> int:
        c = self._conn()
        try:
            with sqlite_tx(c):
                return c.execute(
                    "UPDATE upload_files SET status='pending', updated_at=? WHERE status='uploading'",
                    (time.time(),),
                ).rowcount
        finally:
            c.close()

    def retry_failed(self) -> int:
        c = self._conn()
        try:
            with sqlite_tx(c):
                return c.execute(
                    "UPDATE upload_files SET status='pending', attempts=0, error=NULL, updated_at=? "
                    "WHERE status='failed'",
                    (time.time(),),
                ).rowcount
        finally:
            c.close()

    def clear_queue(self) -> None:
        c = self._conn()
        try:
            with sqlite_tx(c):
                c.execute("DELETE FROM upload_files")
                c.execute("DELETE FROM remote_unlim_index")
                c.execute("DELETE FROM upload_meta")
        finally:
            c.close()

    def take_next_pending(self, exclude_ids: set[int] | None = None) -> UploadRow | None:
        c = self._conn()
        try:
            with sqlite_tx(c):
                excl = sorted(int(x) for x in exclude_ids) if exclude_ids else []
                if excl:
                    marks = ",".join("?" * len(excl))
                    sql = (
                        "SELECT id, local_path, rel_path, name, media_kind, mime, size, mtime_ns, "
                        "md5, sha256, shot_time, remote_name, remote_id, status, error, attempts "
                        f"FROM upload_files WHERE status = 'pending' AND id NOT IN ({marks}) "
                        "ORDER BY attempts ASC, COALESCE(shot_time, 0) ASC, id ASC LIMIT 1"
                    )
                    row = c.execute(sql, excl).fetchone()
                else:
                    row = c.execute(
                        "SELECT id, local_path, rel_path, name, media_kind, mime, size, mtime_ns, "
                        "md5, sha256, shot_time, remote_name, remote_id, status, error, attempts "
                        "FROM upload_files WHERE status = 'pending' "
                        "ORDER BY attempts ASC, COALESCE(shot_time, 0) ASC, id ASC LIMIT 1"
                    ).fetchone()
                if row is None:
                    return None
                c.execute(
                    "UPDATE upload_files SET status='uploading', updated_at=? WHERE id=?",
                    (time.time(), row["id"]),
                )
                return UploadRow(
                    id=int(row["id"]),
                    local_path=row["local_path"],
                    rel_path=row["rel_path"],
                    name=row["name"],
                    media_kind=row["media_kind"] or "photo",
                    mime=row["mime"] or "application/octet-stream",
                    size=int(row["size"]),
                    mtime_ns=int(row["mtime_ns"]),
                    md5=row["md5"],
                    sha256=row["sha256"],
                    shot_time=row["shot_time"],
                    remote_name=row["remote_name"] or sanitize_remote_name(row["name"]),
                    remote_id=row["remote_id"],
                    status="uploading",
                    error=row["error"],
                    attempts=int(row["attempts"]),
                )
        finally:
            c.close()

    def mark_uploaded(
        self,
        row_id: int,
        remote_id: str,
        remote_name: str,
        note: str | None = None,
        *,
        index_size: int | None = None,
        index_md5: str | None = None,
        index_time: float | None = None,
    ) -> None:
        """Атомарно отмечает файл загруженным и (при наличии size/md5) добавляет в remote_unlim_index."""
        now = time.time()
        c = self._conn()
        try:
            with sqlite_tx(c):
                c.execute(
                    "UPDATE upload_files SET status='uploaded', remote_id=?, remote_name=?, "
                    "error=?, uploaded_at=?, updated_at=? WHERE id=?",
                    (remote_id, remote_name, note, now, now, row_id),
                )
                if index_size is not None and index_md5:
                    c.execute(
                        "INSERT OR REPLACE INTO remote_unlim_index (file_id, name, size, md5, file_time) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (remote_id, remote_name, int(index_size), index_md5.lower(), index_time),
                    )
        finally:
            c.close()

    def mark_skipped_remote(
        self,
        row_id: int,
        remote_id: str | None,
        note: str = "уже есть в Безлимите",
        *,
        index_name: str | None = None,
        index_size: int | None = None,
        index_md5: str | None = None,
        index_time: float | None = None,
    ) -> None:
        now = time.time()
        c = self._conn()
        try:
            with sqlite_tx(c):
                c.execute(
                    "UPDATE upload_files SET status='skipped_remote', remote_id=?, error=?, updated_at=? "
                    "WHERE id=?",
                    (remote_id, note, now, row_id),
                )
                if remote_id and index_size is not None and index_md5:
                    c.execute(
                        "INSERT OR REPLACE INTO remote_unlim_index (file_id, name, size, md5, file_time) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            remote_id,
                            index_name or remote_id.rsplit("/", 1)[-1],
                            int(index_size),
                            index_md5.lower(),
                            index_time,
                        ),
                    )
        finally:
            c.close()

    def mark_skipped_limit(self, row_id: int, reason: str) -> None:
        c = self._conn()
        try:
            with sqlite_tx(c):
                c.execute(
                    "UPDATE upload_files SET status='skipped_limit', error=?, updated_at=? WHERE id=?",
                    (reason, time.time(), row_id),
                )
        finally:
            c.close()

    def mark_skipped_unsupported(self, row_id: int, reason: str) -> None:
        c = self._conn()
        try:
            with sqlite_tx(c):
                c.execute(
                    "UPDATE upload_files SET status='skipped_unsupported', error=?, updated_at=? WHERE id=?",
                    (reason, time.time(), row_id),
                )
        finally:
            c.close()

    def mark_retry_or_fail(
        self,
        row_id: int,
        error: str,
        *,
        burn_attempt: bool = True,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> str:
        now = time.time()
        c = self._conn()
        try:
            with sqlite_tx(c):
                row = c.execute(
                    "SELECT attempts FROM upload_files WHERE id=?", (row_id,)
                ).fetchone()
                cur = int(row["attempts"]) if row else 0
                nxt = cur + 1 if burn_attempt else cur
                status = "failed" if (burn_attempt and nxt >= max_attempts) else "pending"
                c.execute(
                    "UPDATE upload_files SET status=?, attempts=?, error=?, updated_at=? WHERE id=?",
                    (status, nxt, error[:500], now, row_id),
                )
                return status
        finally:
            c.close()

    def mark_failed_permanent(self, row_id: int, error: str) -> None:
        c = self._conn()
        try:
            with sqlite_tx(c):
                c.execute(
                    "UPDATE upload_files SET status='failed', error=?, updated_at=? WHERE id=?",
                    (error[:500], time.time(), row_id),
                )
        finally:
            c.close()

    # ── Статистика и выборки для UI/CLI ─────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        c = self._conn()
        try:
            by_status: dict[str, tuple[int, int]] = {}
            for r in c.execute(
                "SELECT status, COUNT(*) AS cnt, COALESCE(SUM(size), 0) AS total_bytes "
                "FROM upload_files GROUP BY status"
            ):
                by_status[r["status"]] = (int(r["cnt"]), int(r["total_bytes"]))

            by_kind: dict[str, tuple[int, int]] = {}
            for r in c.execute(
                "SELECT media_kind, COUNT(*) AS cnt, COALESCE(SUM(size), 0) AS total_bytes "
                "FROM upload_files WHERE media_kind IS NOT NULL AND status != 'skipped_unsupported' "
                "GROUP BY media_kind"
            ):
                by_kind[r["media_kind"]] = (int(r["cnt"]), int(r["total_bytes"]))

            remote_cnt = int(c.execute("SELECT COUNT(*) FROM remote_unlim_index").fetchone()[0])

            def pair(st: str) -> tuple[int, int]:
                return by_status.get(st, (0, 0))

            pend_n, pend_b = pair("pending")
            up_n, up_b = pair("uploading")
            done_n, done_b = pair("uploaded")
            rem_n, rem_b = pair("skipped_remote")
            dup_n, dup_b = pair("skipped_dup_local")
            lim_n, lim_b = pair("skipped_limit")
            uns_n, uns_b = pair("skipped_unsupported")
            fail_n, fail_b = pair("failed")

            media_total = pend_n + up_n + done_n + rem_n + dup_n + lim_n + fail_n
            media_bytes = pend_b + up_b + done_b + rem_b + dup_b + lim_b + fail_b
            return {
                "total_files": media_total + uns_n,
                "media_files": media_total,
                "media_bytes": media_bytes,
                "photos": by_kind.get("photo", (0, 0))[0],
                "photos_bytes": by_kind.get("photo", (0, 0))[1],
                "videos": by_kind.get("video", (0, 0))[0],
                "videos_bytes": by_kind.get("video", (0, 0))[1],
                "pending": pend_n,
                "pending_bytes": pend_b,
                "uploading": up_n,
                "uploading_bytes": up_b,
                "uploaded": done_n,
                "uploaded_bytes": done_b,
                "skipped_remote": rem_n,
                "skipped_remote_bytes": rem_b,
                "skipped_dup_local": dup_n,
                "skipped_dup_local_bytes": dup_b,
                "skipped_limit": lim_n,
                "skipped_limit_bytes": lim_b,
                "skipped_unsupported": uns_n + lim_n,
                "skipped_unsupported_bytes": uns_b + lim_b,
                "failed": fail_n,
                "failed_bytes": fail_b,
                "remote_index_count": remote_cnt,
            }
        finally:
            c.close()

    @staticmethod
    def _build_filter(status: str | None, search: str | None) -> tuple[str, list[Any]]:
        where: list[str] = []
        args: list[Any] = []
        if status and status != "all":
            if status == "skipped":
                where.append(
                    "status IN ('skipped_remote', 'skipped_dup_local', 'skipped_limit', 'skipped_unsupported')"
                )
            else:
                where.append("status = ?")
                args.append(status)
        if search:
            where.append("(rel_path LIKE ? OR name LIKE ? OR COALESCE(error, '') LIKE ?)")
            pat = f"%{search}%"
            args.extend([pat, pat, pat])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return clause, args

    def list_files(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], int]:
        clause, args = self._build_filter(status, search)
        c = self._conn()
        try:
            total = int(c.execute(f"SELECT COUNT(*) FROM upload_files{clause}", args).fetchone()[0])
            rows = c.execute(
                f"SELECT id, rel_path, name, media_kind, size, shot_time, remote_name, remote_id, "
                f"status, error, attempts, uploaded_at FROM upload_files{clause} "
                f"ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                [*args, int(limit), int(offset)],
            ).fetchall()
            return [dict(r) for r in rows], total
        finally:
            c.close()

    def iter_files_for_csv(
        self,
        *,
        status: str | None = None,
        batch_size: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        """Потоковая выборка строк порциями без загрузки всей таблицы в память."""
        clause, args = self._build_filter(status, None)
        last_id = 0
        while True:
            c = self._conn()
            try:
                and_id = f"{clause} AND id > ?" if clause else " WHERE id > ?"
                rows = c.execute(
                    f"SELECT id, rel_path, media_kind, size, status, remote_id, error "
                    f"FROM upload_files{and_id} ORDER BY id ASC LIMIT ?",
                    [*args, last_id, int(batch_size)],
                ).fetchall()
            finally:
                c.close()
            if not rows:
                break
            for r in rows:
                last_id = int(r["id"])
                yield dict(r)
