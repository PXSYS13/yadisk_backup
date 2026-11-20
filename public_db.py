"""База данных публичных ссылок (links_state.db): схема, метаданные, уборка превью и даты папок."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

from file_dates import apply_folder_dates, plausible
from utils import (
    LINKS_DIRNAME,
    remove_preview,
    resolve_local_path,
    sanitize_component,
    sqlite_tx,
    to_long_path,
)

log = logging.getLogger("public_download")

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    public_url TEXT NOT NULL,
    root_path TEXT NOT NULL DEFAULT '/',
    name TEXT,
    kind TEXT,
    local_dir TEXT,
    added_at REAL,
    listed_at REAL,
    gen INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'active',
    error_kind TEXT,
    error TEXT,
    limit_until REAL
);
CREATE TABLE IF NOT EXISTS link_files (
    link_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    size INTEGER,
    md5 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    local_path TEXT,
    error TEXT,
    storage_host TEXT,
    preview_path TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    gen INTEGER NOT NULL DEFAULT 0,
    downloaded_at TEXT,
    file_time REAL,
    created REAL,
    PRIMARY KEY (link_id, path)
);
CREATE INDEX IF NOT EXISTS idx_link_files_status ON link_files(link_id, status);
CREATE TABLE IF NOT EXISTS link_folders (
    link_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    created REAL,
    modified REAL,
    PRIMARY KEY (link_id, path)
);
CREATE TABLE IF NOT EXISTS links_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def link_key(public_url: str, sub_path: str) -> str:
    """Уникальный адрес ссылки в базе: публичная ссылка плюс подпапка."""
    return public_url + (sub_path if sub_path not in ("", "/") else "")


def _parse_ts(val) -> float | None:
    """ISO-дата публичного API или unix-время альбома → секунды эпохи, иначе None."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return plausible(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        num = plausible(s)
        if num is not None:
            return num
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return plausible(dt.timestamp())
        except (ValueError, OverflowError, OSError):
            return None
    return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(link_files)")}
    for col, kind in (("file_time", "REAL"), ("created", "REAL")):
        if col in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE link_files ADD COLUMN {col} {kind}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM links_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM links_meta WHERE key=?", (key,))
    else:
        conn.execute("INSERT INTO links_meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def poke(conn: sqlite3.Connection, wake: bool = False) -> None:
    """Сообщить идущему скачиванию: список ссылок изменился (и, если wake, — не ждать раунда)."""
    meta_set(conn, "changed", str(time.time()))
    if wake:
        meta_set(conn, "wake", str(time.time()))


def link_folder_name(conn: sqlite3.Connection, name: str) -> str:
    """Папка ссылки внутри папки скачивания: '_ссылки/<имя>', уникальная без учёта регистра."""
    text = (name or "").strip()
    base = (sanitize_component(text)[:80].rstrip(" .") if text else "") or "Ссылка"
    taken = {r[0].casefold() for r in conn.execute("SELECT local_dir FROM links WHERE local_dir IS NOT NULL")}
    candidate, n = base, 2
    while f"{LINKS_DIRNAME}/{candidate}".casefold() in taken:
        candidate = f"{base} ({n})"
        n += 1
    return f"{LINKS_DIRNAME}/{candidate}"


def _fallback_name(public_url: str, sub_path: str) -> str:
    tail = [p for p in sub_path.split("/") if p]
    return tail[-1] if tail else public_url.rstrip("/").rsplit("/", 1)[-1]


def add_link(conn: sqlite3.Connection, public_url: str, sub_path: str = "/",
             meta: dict | None = None) -> tuple[int, bool]:
    """Добавляет ссылку или возвращает убранную из списка. (id, появилась ли она в списке)."""
    url = link_key(public_url, sub_path)
    name = (meta or {}).get("name") or _fallback_name(public_url, sub_path)
    kind = (meta or {}).get("type")
    with sqlite_tx(conn):
        row = conn.execute("SELECT id, state FROM links WHERE url=?", (url,)).fetchone()
        if row:
            if row["state"] != "removed":
                return row["id"], False
            conn.execute("UPDATE links SET state='active', error_kind=NULL, error=NULL, limit_until=NULL, "
                         "listed_at=NULL WHERE id=?", (row["id"],))
            return row["id"], True
        cur = conn.execute(
            "INSERT INTO links (url, public_url, root_path, name, kind, local_dir, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, public_url, sub_path or "/", name, kind, link_folder_name(conn, name), time.time()),
        )
    return int(cur.lastrowid), True


def remove_link(conn: sqlite3.Connection, link_id: int, purge: bool) -> bool:
    """Убирает ссылку из списка. Файлы на диске остаются. purge — сразу забыть её файлы."""
    with sqlite_tx(conn):
        n = conn.execute("UPDATE links SET state='removed' WHERE id=? AND state<>'removed'", (link_id,)).rowcount
        if purge:
            conn.execute("DELETE FROM link_files WHERE link_id=?", (link_id,))
            conn.execute("DELETE FROM link_folders WHERE link_id=?", (link_id,))
    return bool(n)


def purge_removed(conn: sqlite3.Connection) -> None:
    with sqlite_tx(conn):
        conn.execute("DELETE FROM link_files WHERE link_id IN (SELECT id FROM links WHERE state='removed')")
        conn.execute("DELETE FROM link_folders WHERE link_id IN (SELECT id FROM links WHERE state='removed')")


def _upsert_files(conn: sqlite3.Connection, link_id: int, gen: int, items: list[tuple]) -> None:
    if not items:
        return
    rows = [
        (link_id, it[0], it[1], it[2], gen,
         it[3] if len(it) > 3 else None,
         it[4] if len(it) > 4 else None)
        for it in items
    ]
    with sqlite_tx(conn):
        conn.executemany(
            """
            INSERT INTO link_files (link_id, path, size, md5, status, gen, file_time, created)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            ON CONFLICT(link_id, path) DO UPDATE SET
                size = excluded.size,
                md5 = COALESCE(excluded.md5, link_files.md5),
                gen = excluded.gen,
                file_time = COALESCE(excluded.file_time, link_files.file_time),
                created = COALESCE(excluded.created, link_files.created),
                status = CASE
                    WHEN link_files.status = 'gone' THEN 'pending'
                    WHEN link_files.status = 'downloaded'
                         AND excluded.md5 IS NOT NULL AND link_files.md5 IS NOT NULL
                         AND excluded.md5 <> link_files.md5
                    THEN 'pending'
                    ELSE link_files.status
                END
            """,
            rows,
        )


def _upsert_folders(conn: sqlite3.Connection, link_id: int, items: list[tuple[str, float | None, float | None]]) -> None:
    rows = [(link_id, p, c, m) for p, c, m in items if p and (c is not None or m is not None)]
    if not rows:
        return
    with sqlite_tx(conn):
        conn.executemany(
            """
            INSERT INTO link_folders (link_id, path, created, modified) VALUES (?, ?, ?, ?)
            ON CONFLICT(link_id, path) DO UPDATE SET
                created = COALESCE(excluded.created, link_folders.created),
                modified = COALESCE(excluded.modified, link_folders.modified)
            """,
            rows,
        )


def drop_previews(conn: sqlite3.Connection, download_dir: str, previews: list[tuple[int, str, str]]) -> int:
    """Удаляет превью файлов, чьи оригиналы скачаны: (link_id, path, путь превью)."""
    try:
        gone = [p for p in previews if remove_preview(p[2], download_dir)]
        if gone:
            with sqlite_tx(conn):
                conn.executemany("UPDATE link_files SET preview_path=NULL "
                                 "WHERE link_id=? AND path=? AND preview_path=?", gone)
        if len(gone) < len(previews):
            log.warning("не удалено превью: %d (файл занят или лежит не в «_превью»)", len(previews) - len(gone))
        return len(gone)
    except Exception as e:  # noqa: BLE001 — уборка не должна ронять скачивание
        log.warning("уборка превью: %s", type(e).__name__)
        return 0


def sweep_previews(conn: sqlite3.Connection, download_dir: str) -> int:
    """Превью уже скачанных файлов, которые не удалились в прошлый раз, — удалить."""
    try:
        rows = conn.execute("SELECT link_id, path, preview_path FROM link_files "
                            "WHERE status='downloaded' AND preview_path IS NOT NULL").fetchall()
    except sqlite3.Error as e:
        log.warning("уборка превью: %s", e)
        return 0
    return drop_previews(conn, download_dir, [(r[0], r[1], r[2]) for r in rows])


def _apply_link_folder_dates(conn: sqlite3.Connection, download_dir: str) -> int:
    """Синхронизирует даты создания и изменения папок публичных ссылок снизу вверх."""
    if not os.path.isdir(to_long_path(download_dir)):
        return 0
    try:
        links = conn.execute(
            "SELECT id, root_path, local_dir, kind FROM links WHERE state='active' AND local_dir IS NOT NULL"
        ).fetchall()
        pairs: list[tuple[str, float | None, float | None]] = []
        for lk in links:
            if lk["kind"] == "file":
                continue
            base = os.path.join(download_dir, *lk["local_dir"].split("/"))
            root_path = (lk["root_path"] or "/").rstrip("/") or "/"
            exact: dict[str, tuple[float | None, float | None]] = {}
            for r in conn.execute("SELECT path, created, modified FROM link_folders WHERE link_id=?", (lk["id"],)):
                p = (r["path"] or "/").rstrip("/") or "/"
                exact[p] = (r["created"], r["modified"])
            fallback: dict[str, list[float]] = {}
            for r in conn.execute(
                "SELECT path, file_time, created FROM link_files "
                "WHERE link_id=? AND (file_time IS NOT NULL OR created IS NOT NULL)", (lk["id"],)
            ):
                c_val = min(x for x in (r["created"], r["file_time"]) if x is not None)
                m_val = r["file_time"] if r["file_time"] is not None else r["created"]
                cur = (r["path"] or "").rstrip("/")
                while True:
                    idx = cur.rfind("/")
                    parent = cur[:idx] if idx > 0 else "/"
                    b = fallback.get(parent)
                    if b is None:
                        fallback[parent] = [c_val, m_val]
                    else:
                        if c_val < b[0]:
                            b[0] = c_val
                        if m_val > b[1]:
                            b[1] = m_val
                    if parent == "/" or parent == root_path:
                        break
                    cur = parent
            for rel in set(exact) | set(fallback):
                ec, em = exact.get(rel, (None, None))
                fb = fallback.get(rel)
                cr = ec if ec is not None else (fb[0] if fb else None)
                mod = em if em is not None else (fb[1] if fb else None)
                if cr is None and mod is None:
                    continue
                if rel in ("/", root_path):
                    local_dir = base
                else:
                    local_dir = resolve_local_path(base, rel)
                pairs.append((local_dir, cr, mod))
        return apply_folder_dates(pairs)
    except Exception as e:  # noqa: BLE001 — даты папок не должны ломать раунд
        log.warning("даты папок ссылок: %s", e)
        return 0
