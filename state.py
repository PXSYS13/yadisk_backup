"""SQLite-трекер состояния скачивания.

Каждый файл с Яндекс.Диска регистрируется в таблице `files` с одним из статусов:
pending / downloaded / failed / skipped. WAL-режим позволяет писать из нескольких
потоков без блокировок.

Отложенный файл (его сервер хранения Яндекса молчит) остаётся pending с ошибкой
`storage_down | …` и именем сервера в storage_host — отдельного статуса нет, чтобы
retry-failed, фильтр категорий и Web UI работали как раньше.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from utils import (
    legacy_local_path,
    path_key,
    remote_path_is_clean,
    resolve_local_path,
    sqlite_tx,
    to_long_path,
)


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
-- Служебные записи о самой базе: например, когда последний раз собирали список
-- (webui.py, ключ listing). Удаление базы («Забыть список файлов») стирает и их
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Маркер отложенного файла в колонке error (его же считает db_stats в webui.py)
DEFERRED_MARKER = "storage_down | "

# Маркеры в колонке error: файл снят выбором человека, и это обратимо. Ставит и
# снимает их webui.py; сбор списка обновляет у таких строк размер и MD5, но не статус
CATEGORY_SKIP_MARKER = "not in selected categories"
FOLDER_SKIP_MARKER = "not in selected folders"
SELECTION_MARKERS = (CATEGORY_SKIP_MARKER, FOLDER_SKIP_MARKER)


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
            conn = self._conn()
            conn.executescript(SCHEMA)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
            for col in ("storage_host", "preview_path"):
                if col in cols:
                    continue
                try:
                    conn.execute(f"ALTER TABLE files ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError as e:  # колонку успел добавить другой процесс
                    if "duplicate column" not in str(e).lower():
                        raise

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with sqlite_tx(self._conn()) as conn:
            yield conn

    # ── публичное API ─────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        row = self._conn().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        """Записывает служебное значение; None — удаляет его."""
        with self._tx() as conn:
            if value is None:
                conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def mark_pending(
        self,
        remote_path: str,
        size: int | None,
        md5: str | None,
    ) -> None:
        """Регистрирует файл как ожидающий скачивания. Не перетирает уже завершённые."""
        self.mark_pending_batch([(remote_path, size, md5)])

    def mark_pending_batch(
        self,
        items: Iterable[tuple[str, int | None, str | None]],
    ) -> None:
        """Пакетная регистрация файлов — значительно ускоряет scan_tree и scan_media.

        Уже скачанный файл, у которого на Диске изменился MD5, возвращается в pending:
        бэкап должен догнать новую версию. Статус пропущенных (skipped) не трогаем —
        это выбор пользователя (категории, папки) или дубликат. У снятых выбором
        обновляем размер и MD5: файл мог измениться, пока его папка была не выбрана,
        и после возврата в очередь проверка скачанного сравнит его со старыми данными.
        """
        items_list = [(*item, *SELECTION_MARKERS) for item in items]
        if not items_list:
            return
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO files (remote_path, size, md5, status)
                VALUES (?, ?, ?, 'pending')
                ON CONFLICT(remote_path) DO UPDATE SET
                    size = excluded.size,
                    md5 = COALESCE(excluded.md5, files.md5),
                    status = CASE
                        WHEN files.status = 'downloaded'
                             AND excluded.md5 IS NOT NULL AND files.md5 IS NOT NULL
                             AND excluded.md5 <> files.md5
                        THEN 'pending'
                        ELSE files.status
                    END
                WHERE files.status <> 'skipped' OR files.error IN (?, ?)
                """,
                items_list,
            )

    def mark_downloaded(self, remote_path: str, local_path: str) -> str | None:
        """Файл скачан. Возвращает путь его страховочного превью, если оно записано:
        оригинал теперь на месте, превью пора убрать (downloader удаляет файл и
        зовёт clear_previews)."""
        with self._tx() as conn:
            preview = self._preview_of(conn, remote_path)
            conn.execute(
                """
                UPDATE files
                   SET status = 'downloaded',
                       local_path = ?,
                       error = NULL,
                       storage_host = NULL,
                       downloaded_at = ?
                 WHERE remote_path = ?
                """,
                (local_path, _utc_now(), remote_path),
            )
        return preview

    def mark_skipped(self, remote_path: str, local_path: str) -> str | None:
        """Файл уже лежит в папке (докачка). Возвращает превью, как mark_downloaded."""
        with self._tx() as conn:
            preview = self._preview_of(conn, remote_path)
            conn.execute(
                """
                UPDATE files
                   SET status = 'skipped',
                       local_path = ?,
                       error = NULL,
                       storage_host = NULL,
                       downloaded_at = ?
                 WHERE remote_path = ?
                """,
                (local_path, _utc_now(), remote_path),
            )
        return preview

    @staticmethod
    def _preview_of(conn: sqlite3.Connection, remote_path: str) -> str | None:
        row = conn.execute("SELECT preview_path FROM files WHERE remote_path = ?", (remote_path,)).fetchone()
        return row["preview_path"] if row else None

    def clear_previews(self, items: Iterable[tuple[str, str]]) -> None:
        """Забывает удалённые превью: пары (remote_path, путь превью).

        Строку, где за это время записали другое превью, не трогает.
        """
        with self._tx() as conn:
            conn.executemany(
                "UPDATE files SET preview_path = NULL WHERE remote_path = ? AND preview_path = ?",
                items,
            )

    def stale_previews(self) -> list[tuple[str, str]]:
        """Превью файлов, чьи оригиналы уже на месте: (remote_path, путь превью).

        Скачанные и найденные при докачке (skipped без маркера). Такие превью остались
        от прошлых версий или не удалились в прошлый раз. Снятые выбором (skipped с
        маркером) сюда не входят: их оригиналов в папке нет, превью — единственная копия.
        """
        return [(r["remote_path"], r["preview_path"]) for r in self._conn().execute(
            "SELECT remote_path, preview_path FROM files WHERE preview_path IS NOT NULL "
            "AND (status = 'downloaded' OR (status = 'skipped' AND error IS NULL))"
        )]

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

    def mark_deferred(self, remote_path: str, host: str, error: str,
                      preview_path: str | None = None) -> None:
        """Файл на молчащем сервере хранения: остаётся в очереди, попробуем в следующий раунд.

        preview_path — куда сохранено страховочное превью (если сохранено).
        """
        text = error if error.startswith(DEFERRED_MARKER) else DEFERRED_MARKER + error
        with self._tx() as conn:
            conn.execute(
                "UPDATE files SET status = 'pending', error = ?, storage_host = ?, "
                "preview_path = COALESCE(?, preview_path) WHERE remote_path = ?",
                (text[:2000], host or None, preview_path or None, remote_path),
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
            "SELECT remote_path, size, md5, local_path, error, storage_host, preview_path "
            "FROM files WHERE status='pending' ORDER BY size ASC"
        )

    def get_pending_stats(self) -> dict[str, int]:
        """Быстро возвращает количество и сумму байт pending файлов."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes FROM files WHERE status='pending'"
        ).fetchone()
        return {"count": int(row["n"] if row else 0), "bytes": int(row["bytes"] if row else 0)}

    def assign_local_paths(self, download_dir: str) -> None:
        """Детерминированно назначает и сохраняет local_path для pending/failed файлов.

        Учитывает существующие пути, санитизацию и коллизии регистра (Windows, macOS).
        Если пользователь сменил DOWNLOAD_DIR, старые назначения вне новой папки
        пересчитываются — иначе остаток очереди молча уехал бы в прежнюю папку.
        Назначенные пути внутри папки не пересчитываются; новые строки получают имена
        по правилу 2.3.1 (см. resolve_local_path).
        """
        root = os.path.normcase(os.path.abspath(download_dir)).rstrip("\\/")

        def under_root(path: str) -> bool:
            n = os.path.normcase(os.path.abspath(path))
            return n == root or n.startswith(root + os.sep)

        with self._tx() as conn:
            # Занятые пути — только у файлов, которые реально лежат на диске
            existing_paths: set[str] = {
                path_key(r["local_path"])
                for r in conn.execute(
                    "SELECT local_path FROM files "
                    "WHERE status IN ('downloaded', 'skipped') "
                    "AND local_path IS NOT NULL AND local_path != ''"
                )
            }

            rows = conn.execute(
                "SELECT remote_path, local_path FROM files "
                "WHERE status IN ('pending', 'failed') ORDER BY remote_path ASC"
            ).fetchall()

            # Сначала регистрируем те, чьи назначения остаются в силе,
            # потом считаем новые — чтобы новые не столкнулись со старыми.
            to_assign: list[str] = []
            for r in rows:
                lp = r["local_path"]
                if lp and under_root(lp):
                    existing_paths.add(path_key(lp))
                else:
                    to_assign.append(r["remote_path"])

            if not to_assign:
                return

            # Сначала пути, которые не пришлось чистить: «a_b/x.txt» получит чистое имя,
            # а «a:b/x.txt» (после чистки тоже a_b/x.txt) — хеш. Сортировка устойчивая:
            # внутри групп порядок remote_path сохраняется.
            order = sorted(((not remote_path_is_clean(rp), rp) for rp in to_assign),
                           key=lambda t: t[0])
            dir_exists: dict[str, bool] = {}
            updates: list[tuple[str, str]] = []
            for dirty, rp in order:
                old = legacy_local_path(download_dir, rp) if dirty else None
                if old and path_key(old) not in existing_paths:
                    folder = os.path.dirname(old)
                    if folder not in dir_exists:
                        dir_exists[folder] = os.path.isdir(to_long_path(folder))
                    # Файл уже лежит под именем до 2.3.1 (например, после «Забыть список
                    # файлов») — оставляем это имя, а не качаем его второй раз под новым
                    if dir_exists[folder] and os.path.isfile(to_long_path(old)):
                        existing_paths.add(path_key(old))
                        updates.append((old, rp))
                        continue
                updates.append((resolve_local_path(download_dir, rp, existing_paths), rp))
            conn.executemany(
                "UPDATE files SET local_path = ? WHERE remote_path = ?",
                updates,
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

    def __enter__(self) -> State:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

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
