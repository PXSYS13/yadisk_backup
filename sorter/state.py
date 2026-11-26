"""SQLite WAL хранилище состояния сканирования, лиц, людей, возрастных прототипов и журнала сортировки."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from sorter.utils import IS_WINDOWS, path_key, sqlite_tx

log = logging.getLogger("photo_sorter.state")

SCHEMA_VERSION = 1
EMBEDDING_DIM = 512
_UNSET = object()


def encode_embedding(vec: np.ndarray) -> bytes:
    """Нормализует 512-мерный вектор по L2 и упаковывает в float32 bytes."""
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        raise ValueError("Пустой вектор эмбеддинга")
    if not bool(np.all(np.isfinite(arr))):
        raise ValueError("Вектор эмбеддинга содержит NaN или Inf")
    norm = float(np.linalg.norm(arr))
    if not np.isfinite(norm) or norm <= 1e-12:
        raise ValueError("Нулевой вектор эмбеддинга (L2-норма <= 1e-12)")
    arr = arr / norm
    return arr.astype("<f4", copy=False).tobytes()


def decode_embedding(blob: bytes | memoryview) -> np.ndarray:
    """Распаковывает float32 bytes в L2-нормализованный 1D numpy-массив."""
    arr = np.frombuffer(blob, dtype="<f4").astype(np.float32, copy=True)
    norm = float(np.linalg.norm(arr))
    if norm > 1e-12:
        arr /= norm
    return arr


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path_key TEXT NOT NULL UNIQUE,
    local_path TEXT NOT NULL,
    rel_path TEXT NOT NULL DEFAULT '',
    source_root TEXT NOT NULL DEFAULT '',
    media_kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    partial_hash TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    shot_ts REAL,
    date_source TEXT NOT NULL DEFAULT 'unknown',
    width INTEGER,
    height INTEGER,
    duration_sec REAL,
    thumb_rel_path TEXT NOT NULL DEFAULT '',
    scan_status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_status ON media_files(scan_status);
CREATE INDEX IF NOT EXISTS idx_media_shot_ts ON media_files(shot_ts);

CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    birth_year INTEGER,
    is_named INTEGER NOT NULL DEFAULT 0,
    is_ignored INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 100,
    cover_face_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    frame_ms INTEGER NOT NULL DEFAULT 0,
    bbox_json TEXT NOT NULL,
    kps_json TEXT NOT NULL,
    det_score REAL NOT NULL,
    quality_score REAL NOT NULL,
    est_age REAL,
    est_gender INTEGER,
    embedding BLOB NOT NULL,
    crop_rel_path TEXT NOT NULL DEFAULT '',
    cluster_id INTEGER,
    person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    assign_confidence REAL NOT NULL DEFAULT 1.0,
    assign_source TEXT NOT NULL DEFAULT 'cluster',
    is_rejected INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_faces_media ON faces(media_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);

CREATE TABLE IF NOT EXISTS person_prototypes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    year_bucket INTEGER NOT NULL,
    face_count INTEGER NOT NULL,
    mean_est_age REAL,
    centroid BLOB NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(person_id, year_bucket)
);
CREATE INDEX IF NOT EXISTS idx_proto_person ON person_prototypes(person_id);

CREATE TABLE IF NOT EXISTS face_constraints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_a INTEGER NOT NULL,
    entity_b INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL,
    UNIQUE(kind, entity_type, entity_a, entity_b)
);
CREATE INDEX IF NOT EXISTS idx_constraints_kind ON face_constraints(kind, entity_type);

CREATE TABLE IF NOT EXISTS studio_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    description TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sort_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_dir TEXT NOT NULL,
    target_dir TEXT NOT NULL,
    mode TEXT NOT NULL,
    effective_mode TEXT NOT NULL,
    options_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    total_ops INTEGER NOT NULL DEFAULT 0,
    done_ops INTEGER NOT NULL DEFAULT 0,
    skipped_ops INTEGER NOT NULL DEFAULT 0,
    failed_ops INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS run_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES sort_runs(id) ON DELETE CASCADE,
    media_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    op_type TEXT NOT NULL,
    src_path TEXT NOT NULL,
    dst_path TEXT NOT NULL,
    src_size INTEGER NOT NULL DEFAULT 0,
    src_sha256 TEXT NOT NULL DEFAULT '',
    dst_inode TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ops_run ON run_operations(run_id, status);
"""


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class SorterState:
    """Потокобезопасный доступ к SQLite WAL базе состояния программы."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(
                os.fspath(self.db_path),
                timeout=15.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            self._conn = conn
            if not IS_WINDOWS:
                for suffix in ("", "-wal", "-shm"):
                    p = f"{os.fspath(self.db_path)}{suffix}"
                    if os.path.exists(p):
                        try:
                            os.chmod(p, 0o600)
                        except OSError:
                            pass
        return self._conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is not None and str(row["value"]) == str(SCHEMA_VERSION):
                    return
            except sqlite3.OperationalError:
                pass
            conn.executescript(_SCHEMA_SQL)
            with sqlite_tx(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def __enter__(self) -> SorterState:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- Meta ---

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._get_conn().execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value)
                )

    # --- Media files ---

    def upsert_media_file(
        self,
        *,
        local_path: str,
        rel_path: str,
        source_root: str,
        media_kind: str,
        size: int,
        mtime_ns: int,
        partial_hash: str = "",
        shot_ts: float | None = None,
        date_source: str = "unknown",
        width: int | None = None,
        height: int | None = None,
        duration_sec: float | None = None,
        thumb_rel_path: str = "",
        scan_status: str = "pending",
        error: str = "",
    ) -> tuple[int, bool]:
        """Добавляет или обновляет запись о файле; возвращает (media_id, needs_processing)."""
        p_key = path_key(local_path)
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            existing = conn.execute(
                "SELECT id, size, mtime_ns, partial_hash, scan_status, shot_ts, date_source "
                "FROM media_files WHERE path_key = ?",
                (p_key,),
            ).fetchone()
            if existing is not None:
                unchanged = (
                    int(existing["size"]) == int(size)
                    and int(existing["mtime_ns"]) == int(mtime_ns)
                    and (not partial_hash or existing["partial_hash"] == partial_hash)
                )
                if unchanged and existing["scan_status"] == "scanned":
                    # Обновим только метаданные пути/даты, не сбрасывая найденные лица
                    with sqlite_tx(conn):
                        conn.execute(
                            "UPDATE media_files SET local_path = ?, rel_path = ?, source_root = ?, "
                            "shot_ts = COALESCE(?, shot_ts), "
                            "date_source = CASE WHEN ? != 'unknown' THEN ? ELSE date_source END, "
                            "updated_at = ? WHERE id = ?",
                            (
                                local_path,
                                rel_path,
                                source_root,
                                shot_ts,
                                date_source,
                                date_source,
                                now,
                                int(existing["id"]),
                            ),
                        )
                    return int(existing["id"]), False

                with sqlite_tx(conn):
                    conn.execute(
                        "UPDATE media_files SET local_path = ?, rel_path = ?, source_root = ?, "
                        "media_kind = ?, size = ?, mtime_ns = ?, partial_hash = ?, shot_ts = ?, "
                        "date_source = ?, width = COALESCE(?, width), height = COALESCE(?, height), "
                        "duration_sec = COALESCE(?, duration_sec), "
                        "thumb_rel_path = CASE WHEN ? != '' THEN ? ELSE thumb_rel_path END, "
                        "scan_status = ?, error = ?, updated_at = ? WHERE id = ?",
                        (
                            local_path,
                            rel_path,
                            source_root,
                            media_kind,
                            int(size),
                            int(mtime_ns),
                            partial_hash,
                            shot_ts,
                            date_source,
                            width,
                            height,
                            duration_sec,
                            thumb_rel_path,
                            thumb_rel_path,
                            scan_status,
                            error,
                            now,
                            int(existing["id"]),
                        ),
                    )
                return int(existing["id"]), True

            with sqlite_tx(conn):
                cur = conn.execute(
                    "INSERT INTO media_files("
                    "path_key, local_path, rel_path, source_root, media_kind, size, mtime_ns, "
                    "partial_hash, shot_ts, date_source, width, height, duration_sec, "
                    "thumb_rel_path, scan_status, error, updated_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        p_key,
                        local_path,
                        rel_path,
                        source_root,
                        media_kind,
                        int(size),
                        int(mtime_ns),
                        partial_hash,
                        shot_ts,
                        date_source,
                        width,
                        height,
                        duration_sec,
                        thumb_rel_path,
                        scan_status,
                        error,
                        now,
                    ),
                )
                return int(cur.lastrowid or 0), True

    def get_media(self, media_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._get_conn().execute(
                "SELECT * FROM media_files WHERE id = ?", (int(media_id),)
            ).fetchone()
            return _row_to_dict(row)

    def get_media_by_path_key(self, p_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._get_conn().execute(
                "SELECT * FROM media_files WHERE path_key = ?", (p_key,)
            ).fetchone()
            return _row_to_dict(row)

    def list_media(
        self,
        *,
        scan_status: str | None = None,
        media_kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scan_status is not None:
            clauses.append("scan_status = ?")
            params.append(scan_status)
        if media_kind is not None:
            clauses.append("media_kind = ?")
            params.append(media_kind)
        sql = "SELECT * FROM media_files"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(shot_ts, 0) ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
        with self._lock:
            rows = self._get_conn().execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def mark_media_scanned(
        self,
        media_id: int,
        *,
        width: int | None = None,
        height: int | None = None,
        duration_sec: float | None = None,
        thumb_rel_path: str | None = None,
        error: str = "",
    ) -> None:
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(
                    "UPDATE media_files SET "
                    "width = COALESCE(?, width), height = COALESCE(?, height), "
                    "duration_sec = COALESCE(?, duration_sec), "
                    "thumb_rel_path = COALESCE(?, thumb_rel_path), "
                    "scan_status = 'scanned', error = ?, updated_at = ? WHERE id = ?",
                    (width, height, duration_sec, thumb_rel_path, error, now, int(media_id)),
                )

    def mark_media_error(self, media_id: int, error: str) -> None:
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(
                    "UPDATE media_files SET scan_status = 'error', error = ?, updated_at = ? "
                    "WHERE id = ?",
                    (error[:500], now, int(media_id)),
                )

    def update_media_sha256(self, media_id: int, sha256: str) -> None:
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(
                    "UPDATE media_files SET sha256 = ?, updated_at = ? WHERE id = ?",
                    (sha256, time.time(), int(media_id)),
                )

    # --- Faces ---

    def replace_faces_for_media(
        self, media_id: int, faces: list[dict[str, Any]]
    ) -> list[int]:
        """Атомарно заменяет лица для media_id и добавляет same_photo cannot_link между ними."""
        now = time.time()
        inserted_ids: list[int] = []
        by_frame: dict[int, list[int]] = {}
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                old_ids = [
                    int(r["id"])
                    for r in conn.execute(
                        "SELECT id FROM faces WHERE media_id = ?", (int(media_id),)
                    ).fetchall()
                ]
                if old_ids:
                    placeholders = ",".join("?" for _ in old_ids)
                    conn.execute(
                        f"DELETE FROM face_constraints WHERE entity_type = 'face' "
                        f"AND (entity_a IN ({placeholders}) OR entity_b IN ({placeholders}))",
                        old_ids + old_ids,
                    )
                    conn.execute("DELETE FROM faces WHERE media_id = ?", (int(media_id),))

                for item in faces:
                    emb = item["embedding"]
                    blob = emb if isinstance(emb, (bytes, bytearray)) else encode_embedding(emb)
                    bbox_val = item["bbox"]
                    kps_val = item.get("kps") or []
                    bbox_json = (
                        bbox_val if isinstance(bbox_val, str) else json.dumps(bbox_val)
                    )
                    kps_json = kps_val if isinstance(kps_val, str) else json.dumps(kps_val)
                    frame_ms = int(item.get("frame_ms", 0))
                    cur = conn.execute(
                        "INSERT INTO faces("
                        "media_id, frame_ms, bbox_json, kps_json, det_score, quality_score, "
                        "est_age, est_gender, embedding, crop_rel_path, cluster_id, person_id, "
                        "assign_confidence, assign_source, is_rejected, created_at"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            int(media_id),
                            frame_ms,
                            bbox_json,
                            kps_json,
                            float(item.get("det_score", 0.0)),
                            float(item.get("quality_score", 0.0)),
                            item.get("est_age"),
                            item.get("est_gender"),
                            blob,
                            str(item.get("crop_rel_path", "")),
                            item.get("cluster_id"),
                            item.get("person_id"),
                            float(item.get("assign_confidence", 1.0)),
                            str(item.get("assign_source", "cluster")),
                            int(bool(item.get("is_rejected", False))),
                            now,
                        ),
                    )
                    fid = int(cur.lastrowid or 0)
                    inserted_ids.append(fid)
                    by_frame.setdefault(frame_ms, []).append(fid)

                # Жёсткие связи Cannot-Link для всех пар лиц на одном фото / видеокадре
                for frame_faces in by_frame.values():
                    if len(frame_faces) < 2:
                        continue
                    for i in range(len(frame_faces)):
                        for j in range(i + 1, len(frame_faces)):
                            a, b = sorted((frame_faces[i], frame_faces[j]))
                            conn.execute(
                                "INSERT OR IGNORE INTO face_constraints("
                                "kind, entity_type, entity_a, entity_b, source, created_at"
                                ") VALUES('cannot_link', 'face', ?, ?, 'same_photo', ?)",
                                (a, b, now),
                            )
        return inserted_ids

    def get_face(self, face_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._get_conn().execute(
                "SELECT * FROM faces WHERE id = ?", (int(face_id),)
            ).fetchone()
            return _row_to_dict(row)

    def list_faces(
        self,
        *,
        media_id: int | None = None,
        person_id: int | None = None,
        include_rejected: bool = False,
        min_quality: float | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if media_id is not None:
            clauses.append("media_id = ?")
            params.append(int(media_id))
        if person_id is not None:
            clauses.append("person_id = ?")
            params.append(int(person_id))
        if not include_rejected:
            clauses.append("is_rejected = 0")
        if min_quality is not None:
            clauses.append("quality_score >= ?")
            params.append(float(min_quality))
        sql = "SELECT * FROM faces"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id ASC"
        with self._lock:
            rows = self._get_conn().execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def list_faces_with_media(
        self,
        *,
        person_id: int | None = None,
        include_rejected: bool = False,
    ) -> list[dict[str, Any]]:
        """Возвращает лица вместе с датой съёмки и типом медиафайла."""
        clauses: list[str] = []
        params: list[Any] = []
        if person_id is not None:
            clauses.append("f.person_id = ?")
            params.append(int(person_id))
        if not include_rejected:
            clauses.append("f.is_rejected = 0")
        sql = (
            "SELECT f.*, m.local_path, m.rel_path, m.media_kind, m.shot_ts, m.date_source "
            "FROM faces f JOIN media_files m ON m.id = f.media_id"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(m.shot_ts, 0) ASC, f.id ASC"
        with self._lock:
            rows = self._get_conn().execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def set_face_assignments(
        self,
        updates: list[tuple[int, int | None, int | None, float, str]],
    ) -> None:
        """Пакетно обновляет (face_id, cluster_id, person_id, assign_confidence, assign_source)."""
        if not updates:
            return
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.executemany(
                    "UPDATE faces SET cluster_id = ?, person_id = ?, "
                    "assign_confidence = ?, assign_source = ? WHERE id = ?",
                    [(cid, pid, float(conf), src, int(fid)) for fid, cid, pid, conf, src in updates],
                )

    def set_face_rejected(self, face_id: int, rejected: bool = True) -> None:
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(
                    "UPDATE faces SET is_rejected = ?, person_id = CASE WHEN ? = 1 THEN NULL ELSE person_id END "
                    "WHERE id = ?",
                    (int(bool(rejected)), int(bool(rejected)), int(face_id)),
                )

    # --- Persons ---

    def create_person(
        self,
        *,
        name: str = "",
        birth_year: int | None = None,
        is_named: bool = False,
        is_ignored: bool = False,
        priority: int = 100,
        cover_face_id: int | None = None,
    ) -> int:
        now = time.time()
        clean_name = name.strip()
        named_flag = int(bool(is_named or clean_name))
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                cur = conn.execute(
                    "INSERT INTO persons("
                    "name, birth_year, is_named, is_ignored, priority, cover_face_id, "
                    "created_at, updated_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        clean_name,
                        birth_year,
                        named_flag,
                        int(bool(is_ignored)),
                        int(priority),
                        cover_face_id,
                        now,
                        now,
                    ),
                )
                return int(cur.lastrowid or 0)

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._get_conn().execute(
                "SELECT * FROM persons WHERE id = ?", (int(person_id),)
            ).fetchone()
            return _row_to_dict(row)

    def list_persons(self, *, include_ignored: bool = True) -> list[dict[str, Any]]:
        """Список людей со счётчиками лиц/медиа, диапазоном годов и лучшим cover_face_id."""
        where = "" if include_ignored else "WHERE p.is_ignored = 0"
        sql = f"""
        SELECT
            p.*,
            COUNT(CASE WHEN f.is_rejected = 0 THEN f.id END) AS face_count,
            COUNT(DISTINCT CASE WHEN f.is_rejected = 0 THEN f.media_id END) AS media_count,
            MIN(CASE WHEN f.is_rejected = 0 THEN m.shot_ts END) AS min_shot_ts,
            MAX(CASE WHEN f.is_rejected = 0 THEN m.shot_ts END) AS max_shot_ts,
            (
                SELECT f2.id FROM faces f2
                WHERE f2.person_id = p.id AND f2.is_rejected = 0
                ORDER BY f2.quality_score DESC, f2.det_score DESC, f2.id ASC
                LIMIT 1
            ) AS best_face_id
        FROM persons p
        LEFT JOIN faces f ON f.person_id = p.id
        LEFT JOIN media_files m ON m.id = f.media_id
        {where}
        GROUP BY p.id
        ORDER BY p.is_ignored ASC, p.is_named DESC, p.priority ASC, media_count DESC, p.id ASC
        """
        with self._lock:
            rows = self._get_conn().execute(sql).fetchall()
            result: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                if d.get("cover_face_id") is None:
                    d["cover_face_id"] = d.get("best_face_id")
                d["min_year"] = (
                    int(time.gmtime(d["min_shot_ts"]).tm_year)
                    if d.get("min_shot_ts") is not None
                    else None
                )
                d["max_year"] = (
                    int(time.gmtime(d["max_shot_ts"]).tm_year)
                    if d.get("max_shot_ts") is not None
                    else None
                )
                result.append(d)
            return result

    def update_person(
        self,
        person_id: int,
        *,
        name: Any = _UNSET,
        birth_year: Any = _UNSET,
        is_named: Any = _UNSET,
        is_ignored: Any = _UNSET,
        priority: Any = _UNSET,
        cover_face_id: Any = _UNSET,
    ) -> dict[str, Any] | None:
        sets: list[str] = []
        params: list[Any] = []
        if name is not _UNSET:
            clean = str(name or "").strip()
            sets.append("name = ?")
            params.append(clean)
            if is_named is _UNSET:
                sets.append("is_named = ?")
                params.append(1 if clean else 0)
        if birth_year is not _UNSET:
            sets.append("birth_year = ?")
            params.append(int(birth_year) if birth_year is not None else None)
        if is_named is not _UNSET:
            sets.append("is_named = ?")
            params.append(int(bool(is_named)))
        if is_ignored is not _UNSET:
            sets.append("is_ignored = ?")
            params.append(int(bool(is_ignored)))
        if priority is not _UNSET:
            sets.append("priority = ?")
            params.append(int(priority))
        if cover_face_id is not _UNSET:
            sets.append("cover_face_id = ?")
            params.append(int(cover_face_id) if cover_face_id is not None else None)
        if not sets:
            return self.get_person(person_id)
        sets.append("updated_at = ?")
        params.append(time.time())
        params.append(int(person_id))
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(f"UPDATE persons SET {', '.join(sets)} WHERE id = ?", params)
            return self.get_person(person_id)

    def delete_person(self, person_id: int) -> None:
        pid = int(person_id)
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(
                    "DELETE FROM face_constraints WHERE entity_type = 'person' "
                    "AND (entity_a = ? OR entity_b = ?)",
                    (pid, pid),
                )
                conn.execute("DELETE FROM persons WHERE id = ?", (pid,))

    def merge_persons(self, target_person_id: int, source_person_id: int) -> dict[str, Any]:
        """Объединяет source_person_id в target_person_id и возвращает снимок для отката Undo."""
        tid, sid = int(target_person_id), int(source_person_id)
        if tid == sid:
            raise ValueError("Нельзя объединить человека с самим собой")
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            target = conn.execute("SELECT * FROM persons WHERE id = ?", (tid,)).fetchone()
            source = conn.execute("SELECT * FROM persons WHERE id = ?", (sid,)).fetchone()
            if target is None or source is None:
                raise ValueError("Один из выбранных профилей не найден")

            moved_faces = [
                {
                    "id": int(r["id"]),
                    "assign_source": str(r["assign_source"]),
                    "assign_confidence": float(r["assign_confidence"]),
                }
                for r in conn.execute(
                    "SELECT id, assign_source, assign_confidence FROM faces WHERE person_id = ?",
                    (sid,),
                ).fetchall()
            ]
            old_constraints = [
                dict(r)
                for r in conn.execute(
                    "SELECT kind, entity_type, entity_a, entity_b, source FROM face_constraints "
                    "WHERE entity_type = 'person' AND (entity_a = ? OR entity_b = ?)",
                    (sid, sid),
                ).fetchall()
            ]
            snapshot = {
                "target_before": dict(target),
                "source_before": dict(source),
                "moved_faces": moved_faces,
                "source_constraints": old_constraints,
            }

            with sqlite_tx(conn):
                conn.execute(
                    "UPDATE faces SET person_id = ?, assign_source = 'user' WHERE person_id = ?",
                    (tid, sid),
                )
                # Переносим person-level constraints на target (избегая self-loop)
                for c in old_constraints:
                    other = c["entity_b"] if c["entity_a"] == sid else c["entity_a"]
                    if other != tid:
                        a, b = sorted((tid, int(other)))
                        conn.execute(
                            "INSERT OR IGNORE INTO face_constraints("
                            "kind, entity_type, entity_a, entity_b, source, created_at"
                            ") VALUES(?, 'person', ?, ?, ?, ?)",
                            (c["kind"], a, b, c["source"], now),
                        )
                conn.execute(
                    "DELETE FROM face_constraints WHERE entity_type = 'person' "
                    "AND (entity_a = ? OR entity_b = ?)",
                    (sid, sid),
                )
                new_birth = (
                    target["birth_year"]
                    if target["birth_year"] is not None
                    else source["birth_year"]
                )
                new_name = target["name"] or source["name"]
                new_named = 1 if (target["is_named"] or source["is_named"] or new_name) else 0
                conn.execute(
                    "UPDATE persons SET name = ?, birth_year = ?, is_named = ?, updated_at = ? "
                    "WHERE id = ?",
                    (new_name, new_birth, new_named, now, tid),
                )
                conn.execute("DELETE FROM persons WHERE id = ?", (sid,))
            return snapshot

    # --- Prototypes ---

    def replace_person_prototypes(
        self, person_id: int, prototypes: list[dict[str, Any]]
    ) -> None:
        now = time.time()
        pid = int(person_id)
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute("DELETE FROM person_prototypes WHERE person_id = ?", (pid,))
                for proto in prototypes:
                    cent = proto["centroid"]
                    blob = (
                        cent
                        if isinstance(cent, (bytes, bytearray))
                        else encode_embedding(cent)
                    )
                    conn.execute(
                        "INSERT INTO person_prototypes("
                        "person_id, year_bucket, face_count, mean_est_age, centroid, updated_at"
                        ") VALUES(?, ?, ?, ?, ?, ?)",
                        (
                            pid,
                            int(proto["year_bucket"]),
                            int(proto["face_count"]),
                            proto.get("mean_est_age"),
                            blob,
                            now,
                        ),
                    )

    def list_prototypes(self, person_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if person_id is not None:
                rows = self._get_conn().execute(
                    "SELECT * FROM person_prototypes WHERE person_id = ? ORDER BY year_bucket ASC",
                    (int(person_id),),
                ).fetchall()
            else:
                rows = self._get_conn().execute(
                    "SELECT * FROM person_prototypes ORDER BY person_id ASC, year_bucket ASC"
                ).fetchall()
            return [dict(r) for r in rows]

    def clear_all_prototypes(self) -> None:
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute("DELETE FROM person_prototypes")

    # --- Constraints ---

    def add_constraint(
        self,
        kind: str,
        entity_type: str,
        entity_a: int,
        entity_b: int,
        *,
        source: str = "user",
    ) -> None:
        a, b = sorted((int(entity_a), int(entity_b)))
        if a == b:
            return
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO face_constraints("
                    "kind, entity_type, entity_a, entity_b, source, created_at"
                    ") VALUES(?, ?, ?, ?, ?, ?)",
                    (kind, entity_type, a, b, source, time.time()),
                )

    def remove_constraint(
        self, kind: str, entity_type: str, entity_a: int, entity_b: int
    ) -> None:
        a, b = sorted((int(entity_a), int(entity_b)))
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(
                    "DELETE FROM face_constraints WHERE kind = ? AND entity_type = ? "
                    "AND entity_a = ? AND entity_b = ?",
                    (kind, entity_type, a, b),
                )

    def list_constraints(
        self, *, kind: str | None = None, entity_type: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        sql = "SELECT * FROM face_constraints"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._get_conn().execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_cannot_link_face_pairs(self) -> set[tuple[int, int]]:
        rows = self.list_constraints(kind="cannot_link", entity_type="face")
        return {(int(r["entity_a"]), int(r["entity_b"])) for r in rows}

    def get_cannot_link_person_pairs(self) -> set[tuple[int, int]]:
        rows = self.list_constraints(kind="cannot_link", entity_type="person")
        return {(int(r["entity_a"]), int(r["entity_b"])) for r in rows}

    # --- Studio history (Undo Ctrl+Z) ---

    def push_history(
        self, action_type: str, description: str, snapshot: dict[str, Any]
    ) -> int:
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                cur = conn.execute(
                    "INSERT INTO studio_history(action_type, description, snapshot_json, undone, created_at) "
                    "VALUES(?, ?, ?, 0, ?)",
                    (action_type, description, json.dumps(snapshot, ensure_ascii=False), time.time()),
                )
                return int(cur.lastrowid or 0)

    def list_history(
        self, *, limit: int = 20, include_undone: bool = False
    ) -> list[dict[str, Any]]:
        where = "" if include_undone else "WHERE undone = 0"
        with self._lock:
            rows = self._get_conn().execute(
                f"SELECT * FROM studio_history {where} ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                try:
                    d["snapshot"] = json.loads(d["snapshot_json"])
                except (ValueError, TypeError) as exc:
                    log.error(
                        "Повреждённый JSON в studio_history.snapshot_json (id=%s): %s",
                        d.get("id"),
                        exc,
                    )
                    d["snapshot"] = {}
                out.append(d)
            return out

    def get_last_undoable(self) -> dict[str, Any] | None:
        items = self.list_history(limit=1, include_undone=False)
        return items[0] if items else None

    def mark_history_undone(self, history_id: int) -> None:
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(
                    "UPDATE studio_history SET undone = 1 WHERE id = ?", (int(history_id),)
                )

    def mark_all_history_undone(self) -> int:
        """Помечает все неотменённые записи studio_history как undone (после удаления профилей)."""
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                cur = conn.execute("UPDATE studio_history SET undone = 1 WHERE undone = 0")
                return int(cur.rowcount or 0)

    def restore_person_from_snapshot(self, person_data: dict[str, Any]) -> None:
        """Восстанавливает удалённого человека с прежним ID (для Undo объединения)."""
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO persons("
                    "id, name, birth_year, is_named, is_ignored, priority, cover_face_id, "
                    "created_at, updated_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(person_data["id"]),
                        str(person_data.get("name", "")),
                        person_data.get("birth_year"),
                        int(bool(person_data.get("is_named", 0))),
                        int(bool(person_data.get("is_ignored", 0))),
                        int(person_data.get("priority", 100)),
                        person_data.get("cover_face_id"),
                        float(person_data.get("created_at", time.time())),
                        time.time(),
                    ),
                )

    # --- Sort runs & operations ---

    def create_sort_run(
        self,
        *,
        source_dir: str,
        target_dir: str,
        mode: str,
        effective_mode: str,
        options: dict[str, Any],
        ops: list[dict[str, Any]],
    ) -> int:
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                cur = conn.execute(
                    "INSERT INTO sort_runs("
                    "source_dir, target_dir, mode, effective_mode, options_json, status, "
                    "total_ops, done_ops, skipped_ops, failed_ops, created_at"
                    ") VALUES(?, ?, ?, ?, ?, 'running', ?, 0, 0, 0, ?)",
                    (
                        source_dir,
                        target_dir,
                        mode,
                        effective_mode,
                        json.dumps(options, ensure_ascii=False),
                        len(ops),
                        now,
                    ),
                )
                run_id = int(cur.lastrowid or 0)
                if ops:
                    conn.executemany(
                        "INSERT INTO run_operations("
                        "run_id, media_id, person_id, op_type, src_path, dst_path, "
                        "src_size, src_sha256, dst_inode, status, error, updated_at"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, '', 'pending', '', ?)",
                        [
                            (
                                run_id,
                                op.get("media_id"),
                                op.get("person_id"),
                                str(op["op_type"]),
                                str(op["src_path"]),
                                str(op["dst_path"]),
                                int(op.get("src_size", 0)),
                                str(op.get("src_sha256", "")),
                                now,
                            )
                            for op in ops
                        ],
                    )
                return run_id

    def get_sort_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._get_conn().execute(
                "SELECT r.*, "
                "COALESCE((SELECT COUNT(*) FROM run_operations o WHERE o.run_id = r.id AND o.status = 'pending'), 0) AS pending_ops, "
                "COALESCE((SELECT COUNT(*) FROM run_operations o WHERE o.run_id = r.id AND o.status = 'rolled_back'), 0) AS rolled_back_ops "
                "FROM sort_runs r WHERE r.id = ?",
                (int(run_id),),
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["pending_ops"] = int(d.get("pending_ops") or 0)
            d["rolled_back_ops"] = int(d.get("rolled_back_ops") or 0)
            try:
                d["options"] = json.loads(d["options_json"])
            except (ValueError, TypeError) as exc:
                log.error(
                    "Повреждённый JSON в sort_runs.options_json (run_id=%s): %s",
                    d.get("id"),
                    exc,
                )
                d["options"] = {}
            return d

    def list_sort_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._get_conn().execute(
                "SELECT r.*, "
                "COALESCE((SELECT COUNT(*) FROM run_operations o WHERE o.run_id = r.id AND o.status = 'pending'), 0) AS pending_ops, "
                "COALESCE((SELECT COUNT(*) FROM run_operations o WHERE o.run_id = r.id AND o.status = 'rolled_back'), 0) AS rolled_back_ops "
                "FROM sort_runs r ORDER BY r.id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["pending_ops"] = int(d.get("pending_ops") or 0)
                d["rolled_back_ops"] = int(d.get("rolled_back_ops") or 0)
                try:
                    d["options"] = json.loads(d["options_json"])
                except (ValueError, TypeError) as exc:
                    log.error(
                        "Повреждённый JSON в sort_runs.options_json (run_id=%s): %s",
                        d.get("id"),
                        exc,
                    )
                    d["options"] = {}
                out.append(d)
            return out

    def reconcile_stale_sort_runs(self) -> list[int]:
        """Переводит все зависшие записи sort_runs со статусом 'running' в 'partial'."""
        with self._lock:
            rows = self._get_conn().execute(
                "SELECT id FROM sort_runs WHERE status = 'running' ORDER BY id ASC"
            ).fetchall()
            stale_ids = [int(r["id"]) for r in rows]
            for rid in stale_ids:
                log.warning(
                    "Обнаружен незавершённый sort_run #%d в статусе 'running' — перевожу в 'partial'",
                    rid,
                )
                self.refresh_sort_run_counts(rid, status="partial", finished=True)
            return stale_ids

    def list_run_operations(
        self, run_id: int, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if status is not None:
                rows = self._get_conn().execute(
                    "SELECT * FROM run_operations WHERE run_id = ? AND status = ? ORDER BY id ASC",
                    (int(run_id), status),
                ).fetchall()
            else:
                rows = self._get_conn().execute(
                    "SELECT * FROM run_operations WHERE run_id = ? ORDER BY id ASC",
                    (int(run_id),),
                ).fetchall()
            return [dict(r) for r in rows]

    def update_operation_status(
        self,
        op_id: int,
        status: str,
        *,
        src_sha256: str | None = None,
        dst_inode: str | None = None,
        error: str = "",
    ) -> None:
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute(
                    "UPDATE run_operations SET status = ?, "
                    "src_sha256 = COALESCE(?, src_sha256), "
                    "dst_inode = COALESCE(?, dst_inode), "
                    "error = ?, updated_at = ? WHERE id = ?",
                    (status, src_sha256, dst_inode, error[:500], now, int(op_id)),
                )

    def refresh_sort_run_counts(
        self, run_id: int, *, status: str | None = None, finished: bool = False
    ) -> dict[str, Any] | None:
        rid = int(run_id)
        with self._lock:
            conn = self._get_conn()
            counts = {
                str(r["status"]): int(r["c"])
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS c FROM run_operations WHERE run_id = ? GROUP BY status",
                    (rid,),
                ).fetchall()
            }
            done_ops = counts.get("done", 0)
            skipped_ops = counts.get("skipped", 0)
            failed_ops = counts.get("failed", 0)
            total_ops = sum(counts.values())
            with sqlite_tx(conn):
                if status is not None:
                    conn.execute(
                        "UPDATE sort_runs SET total_ops = ?, done_ops = ?, skipped_ops = ?, "
                        "failed_ops = ?, status = ?, "
                        "finished_at = CASE WHEN ? THEN ? ELSE finished_at END WHERE id = ?",
                        (
                            total_ops,
                            done_ops,
                            skipped_ops,
                            failed_ops,
                            status,
                            int(finished),
                            time.time(),
                            rid,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE sort_runs SET total_ops = ?, done_ops = ?, skipped_ops = ?, "
                        "failed_ops = ? WHERE id = ?",
                        (total_ops, done_ops, skipped_ops, failed_ops, rid),
                    )
            return self.get_sort_run(rid)

    # --- Summary stats & reset ---

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            conn = self._get_conn()
            m_row = conn.execute(
                "SELECT "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN media_kind = 'photo' THEN 1 ELSE 0 END) AS photos, "
                "SUM(CASE WHEN media_kind = 'video' THEN 1 ELSE 0 END) AS videos, "
                "SUM(CASE WHEN scan_status = 'scanned' THEN 1 ELSE 0 END) AS scanned, "
                "SUM(CASE WHEN scan_status = 'pending' THEN 1 ELSE 0 END) AS pending, "
                "SUM(CASE WHEN scan_status = 'error' THEN 1 ELSE 0 END) AS errors, "
                "COALESCE(SUM(size), 0) AS total_bytes "
                "FROM media_files"
            ).fetchone()
            f_row = conn.execute(
                "SELECT "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN is_rejected = 0 THEN 1 ELSE 0 END) AS active, "
                "SUM(CASE WHEN is_rejected = 0 AND person_id IS NOT NULL THEN 1 ELSE 0 END) AS assigned, "
                "SUM(CASE WHEN is_rejected = 1 THEN 1 ELSE 0 END) AS rejected "
                "FROM faces"
            ).fetchone()
            p_row = conn.execute(
                "SELECT "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN is_named = 1 AND is_ignored = 0 THEN 1 ELSE 0 END) AS named, "
                "SUM(CASE WHEN is_named = 0 AND is_ignored = 0 THEN 1 ELSE 0 END) AS unnamed, "
                "SUM(CASE WHEN is_ignored = 1 THEN 1 ELSE 0 END) AS ignored "
                "FROM persons"
            ).fetchone()
            r_count = conn.execute("SELECT COUNT(*) AS c FROM sort_runs").fetchone()
            return {
                "media_total": int(m_row["total"] or 0),
                "photos": int(m_row["photos"] or 0),
                "videos": int(m_row["videos"] or 0),
                "scanned": int(m_row["scanned"] or 0),
                "pending": int(m_row["pending"] or 0),
                "errors": int(m_row["errors"] or 0),
                "total_bytes": int(m_row["total_bytes"] or 0),
                "faces_total": int(f_row["total"] or 0),
                "faces_active": int(f_row["active"] or 0),
                "faces_assigned": int(f_row["assigned"] or 0),
                "faces_rejected": int(f_row["rejected"] or 0),
                "persons_total": int(p_row["total"] or 0),
                "persons_named": int(p_row["named"] or 0),
                "persons_unnamed": int(p_row["unnamed"] or 0),
                "persons_ignored": int(p_row["ignored"] or 0),
                "sort_runs": int(r_count["c"] or 0),
            }

    def clear_all(self) -> None:
        """Очищает индексированные медиафайлы, лица, людей и прототипы (журнал sort_runs оставляет)."""
        with self._lock:
            conn = self._get_conn()
            with sqlite_tx(conn):
                conn.execute("DELETE FROM studio_history")
                conn.execute("DELETE FROM face_constraints")
                conn.execute("DELETE FROM person_prototypes")
                conn.execute("DELETE FROM faces")
                conn.execute("DELETE FROM persons")
                conn.execute("DELETE FROM media_files")
