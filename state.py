"""SQLite-трекер состояния скачивания.

Каждый файл с Яндекс.Диска регистрируется в таблице `files` с одним из статусов:
pending / downloaded / failed / skipped. WAL-режим позволяет писать из нескольких
потоков без блокировок.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional


def _utc_now() -> str:
    """Текущее время UTC в том же формате, что писали раньше (utcnow() устарел)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    remote_path TEXT PRIMARY KEY,
    size INTEGER,
    md5 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    local_path TEXT,
    error TEXT,
    downloaded_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_status ON files(status);
"""


class State:
    """Тонкая обёртка над SQLite. Создаёт по соединению на поток (через threading.local).

    SQLite в WAL умеет конкурентные читатели + один писатель. Чтобы не ловить
    `database is locked`, у каждого потока своё соединение.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._tls = threading.local()
        self._lock = threading.Lock()  # для редких операций над schema
        self._all_conns: list[sqlite3.Connection] = []
        self._all_conns_lock = threading.Lock()
        self._init_schema()

    # ── низкоуровневое ────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                timeout=30,
                isolation_level=None,  # autocommit; явные транзакции через BEGIN
                check_same_thread=False,
            )
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.row_factory = sqlite3.Row
            self._tls.conn = conn
            with self._all_conns_lock:
                self._all_conns.append(conn)
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            self._conn().executescript(SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ── публичное API ─────────────────────────────────────────────────

    def mark_pending(
        self,
        remote_path: str,
        size: Optional[int],
        md5: Optional[str],
    ) -> None:
        """Регистрирует файл как ожидающий скачивания. Не перетирает уже завершённые."""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO files (remote_path, size, md5, status)
                VALUES (?, ?, ?, 'pending')
                ON CONFLICT(remote_path) DO UPDATE SET
                    size = excluded.size,
                    md5 = COALESCE(excluded.md5, files.md5)
                WHERE files.status NOT IN ('downloaded', 'skipped')
                """,
                (remote_path, size, md5),
            )

    def mark_downloaded(self, remote_path: str, local_path: str) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE files
                   SET status = 'downloaded',
                       local_path = ?,
                       error = NULL,
                       downloaded_at = ?
                 WHERE remote_path = ?
                """,
                (local_path, _utc_now(), remote_path),
            )

    def mark_skipped(self, remote_path: str, local_path: str) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE files
                   SET status = 'skipped',
                       local_path = ?,
                       error = NULL,
                       downloaded_at = ?
                 WHERE remote_path = ?
                """,
                (local_path, _utc_now(), remote_path),
            )

    def mark_failed(self, remote_path: str, error: str) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE files
                   SET status = 'failed',
                       error = ?
                 WHERE remote_path = ?
                """,
                (error[:2000], remote_path),
            )

    def reset_failed_to_pending(self) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE files SET status='pending', error=NULL WHERE status='failed'"
            )
            return cur.rowcount

    def get_pending(self) -> Iterable[sqlite3.Row]:
        """Возвращает курсор с pending-файлами. Читается лениво, без загрузки всего в RAM."""
        return self._conn().execute(
            "SELECT remote_path, size, md5 FROM files WHERE status='pending' ORDER BY size ASC"
        )

    def get_failed(self) -> Iterable[sqlite3.Row]:
        return self._conn().execute(
            "SELECT remote_path, size, md5, error FROM files WHERE status='failed'"
        )

    def get_all_downloaded(self) -> Iterable[sqlite3.Row]:
        return self._conn().execute(
            "SELECT remote_path, local_path, md5, size FROM files "
            "WHERE status IN ('downloaded', 'skipped') AND local_path IS NOT NULL"
        )

    def get_stats(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM files GROUP BY status"
        ).fetchall()
        stats: dict[str, int] = {
            "pending": 0,
            "downloaded": 0,
            "failed": 0,
            "skipped": 0,
            "total": 0,
            "bytes_total": 0,
            "bytes_downloaded": 0,
        }
        for r in rows:
            stats[r["status"]] = r["n"]
            stats["total"] += r["n"]
            stats["bytes_total"] += r["bytes"] or 0
            if r["status"] in ("downloaded", "skipped"):
                stats["bytes_downloaded"] += r["bytes"] or 0
        return stats

    def get_existing_remote_paths(self) -> set[str]:
        """Все уже зарегистрированные пути — для инкрементального scan."""
        return {
            row["remote_path"]
            for row in self._conn().execute("SELECT remote_path FROM files")
        }

    def close(self) -> None:
        """Закрывает все соединения, открытые в любых потоках."""
        with self._all_conns_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._tls.conn = None
