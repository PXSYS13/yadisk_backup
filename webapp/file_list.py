"""Списки скачанных и нескачанных файлов (Диск, Безлимит, По ссылке) и потоковый экспорт CSV/TXT.

Единые SQL-предикаты совпадают с webapp.queue_db.db_stats и webapp.folders.folder_progress,
поэтому счётчики вкладок сходятся со статистикой Главной до одного файла.
"""

from __future__ import annotations

import csv
import io
import math
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from state import CATEGORY_SKIP_MARKER, FOLDER_SKIP_MARKER, SELECTION_MARKERS
from unlim_common import DEFERRED_MARKER
from utils import human_bytes, sqlite_ro_uri, utc_ts
from webapp import paths
from webapp.folders import folder_split
from webapp.queue_db import reason_text

router = APIRouter()

VALID_SOURCES = ("disk", "unlim", "links")
VALID_STATUSES = ("all", "downloaded", "not_downloaded", "queued", "deferred", "failed", "excluded", "limited")
VALID_SORTS = ("default", "name", "size_desc", "size_asc", "date_desc")
EXPORT_CHUNK = 2000

STATE_LABELS = {
    "downloaded": "Скачан",
    "queued": "В очереди",
    "deferred": "Ждёт Яндекса",
    "failed": "Не скачался",
    "excluded": "Не выбран",
    "limited": "Лимит Яндекса",
}


def _ci_contains(text: str | None, needle: str | None) -> int:
    """Регистронезависимый поиск с поддержкой кириллицы (SQLite LIKE/LOWER работают только с ASCII)."""
    if not needle:
        return 1
    if not text:
        return 0
    return 1 if needle.casefold() in text.casefold() else 0


def _clean_display_path(source: str, raw_path: str) -> str:
    p = str(raw_path or "")
    if source == "disk":
        if p.startswith("disk:"):
            p = p[5:]
        return p if p.startswith("/") else "/" + p
    if source == "unlim":
        if p.startswith("/photounlim/"):
            return p[len("/photounlim/"):]
        if p.startswith("photounlim:/"):
            return p[len("photounlim:/"):]
        if p.startswith("photounlim:"):
            return p[len("photounlim:"):]
    return p


def _split_name_dir(source: str, raw_path: str, name_col: str | None = None) -> tuple[str, str]:
    clean = _clean_display_path(source, raw_path)
    stripped = clean.rstrip("/")
    if "/" in stripped:
        parent, base = stripped.rsplit("/", 1)
    else:
        parent, base = "", stripped
    name = (name_col or "").strip() or base or stripped
    return name, parent


def _classify_row(source: str, status: str, error: str | None) -> tuple[str, str]:
    err = error or ""
    if status == "downloaded":
        return "downloaded", ""
    if status == "skipped":
        if source == "disk" and err not in SELECTION_MARKERS:
            return "downloaded", ""
        if err == FOLDER_SKIP_MARKER:
            return "excluded", "Папка не выбрана в «Настройках»"
        if err == CATEGORY_SKIP_MARKER:
            return "excluded", "Тип файла не выбран в «Настройках»"
        return "excluded", "Не выбран в «Настройках»"
    if status == "failed":
        return "failed", reason_text(err)
    if status == "pending":
        if err.startswith(DEFERRED_MARKER):
            return "deferred", reason_text(err)
        if source == "links" and err.startswith("link_limit |"):
            return "limited", reason_text(err)
        return "queued", ""
    if status == "in_progress":
        return "queued", ""
    return "queued", reason_text(err)


def _source_db(source: str, db_override: Path | None = None) -> Path:
    if db_override is not None:
        return db_override
    if source == "disk":
        return paths.DB_FILE
    if source == "unlim":
        return paths.UNLIM_DB_FILE
    return paths.LINKS_DB_FILE


def _status_clause(source: str, status: str) -> tuple[str, list]:
    """SQL WHERE-условие для выбранного статуса (алиас таблицы f)."""
    if source == "disk":
        done = "(f.status = 'downloaded' OR (f.status = 'skipped' AND COALESCE(f.error, '') NOT IN (?, ?)))"
        done_p: list = [CATEGORY_SKIP_MARKER, FOLDER_SKIP_MARKER]
        exc = "(f.status = 'skipped' AND COALESCE(f.error, '') IN (?, ?))"
        exc_p: list = [CATEGORY_SKIP_MARKER, FOLDER_SKIP_MARKER]
    elif source == "unlim":
        done, done_p = "(f.status = 'downloaded')", []
        exc, exc_p = "(f.status = 'skipped')", []
    else:
        done, done_p = "(f.status = 'downloaded')", []
        exc, exc_p = "(0)", []

    deferred = "(f.status = 'pending' AND COALESCE(f.error, '') LIKE ?)"
    deferred_p = [DEFERRED_MARKER + "%"]
    limited = "(f.status = 'pending' AND COALESCE(f.error, '') LIKE 'link_limit |%')" if source == "links" else "(0)"
    queued = (
        "(f.status = 'in_progress' OR (f.status = 'pending' "
        "AND COALESCE(f.error, '') NOT LIKE ? AND COALESCE(f.error, '') NOT LIKE 'link_limit |%'))"
        if source == "links"
        else "(f.status = 'in_progress' OR (f.status = 'pending' AND COALESCE(f.error, '') NOT LIKE ?))"
    )
    queued_p = [DEFERRED_MARKER + "%"]
    not_done = "(f.status IN ('pending', 'in_progress', 'failed'))"

    if status == "all":
        return ("f.status <> 'gone'", []) if source == "links" else ("1=1", [])
    if status == "downloaded":
        return done, done_p
    if status == "not_downloaded":
        return not_done, []
    if status == "queued":
        return queued, queued_p
    if status == "deferred":
        return deferred, deferred_p
    if status == "failed":
        return "(f.status = 'failed')", []
    if status == "excluded":
        return exc, exc_p
    if status == "limited":
        return limited, []
    return "1=1", []


def _base_where(source: str, folder: str, link_id: int | None) -> tuple[list[str], list]:
    clauses: list[str] = []
    params: list = []
    if source == "disk" and folder:
        _path, prefix = folder_split(folder)
        if prefix != "disk:/":
            clauses.append("substr(f.remote_path, 1, ?) = ?")
            params.extend([len(prefix), prefix])
    if source == "links":
        clauses.append("l.state = 'active'")
        clauses.append("f.status <> 'gone'")
        if link_id is not None:
            clauses.append("f.link_id = ?")
            params.append(link_id)
    return clauses, params


def _order_by(source: str, status: str, sort: str, id_col: str) -> str:
    if sort == "name":
        return f"COALESCE(f.name, f.{id_col}) ASC" if source == "unlim" else f"f.{id_col} ASC"
    if sort == "size_desc":
        return f"COALESCE(f.size, 0) DESC, f.{id_col} ASC"
    if sort == "size_asc":
        return f"COALESCE(f.size, 0) ASC, f.{id_col} ASC"
    if sort == "date_desc":
        return f"replace(COALESCE(f.downloaded_at, ''), 'T', ' ') DESC, f.{id_col} ASC"
    # default: для скачанных — самые свежие сверху; для нескачанных — сначала ошибки, потом отложенные, потом очередь
    if status == "downloaded":
        return f"replace(COALESCE(f.downloaded_at, ''), 'T', ' ') DESC, f.{id_col} ASC"
    if status == "not_downloaded":
        return (
            "CASE "
            "WHEN f.status = 'failed' THEN 0 "
            "WHEN f.status = 'pending' AND COALESCE(f.error, '') LIKE 'storage_down | %' THEN 1 "
            "WHEN f.status = 'pending' AND COALESCE(f.error, '') LIKE 'link_limit |%' THEN 2 "
            "ELSE 3 END ASC, "
            f"f.{id_col} ASC"
        )
    return f"f.{id_col} ASC"


def _table_meta(source: str) -> tuple[str, str, str]:
    """(table, id_col, from_sql)."""
    if source == "disk":
        return "files", "remote_path", "files f"
    if source == "unlim":
        return "unlim_files", "file_id", "unlim_files f"
    return "link_files", "path", "link_files f JOIN links l ON l.id = f.link_id"


def _empty_counts() -> dict[str, int]:
    return {
        "all": 0, "downloaded": 0, "not_downloaded": 0,
        "queued": 0, "deferred": 0, "failed": 0, "excluded": 0, "limited": 0,
    }


def _collect_counts(c: sqlite3.Connection, source: str, from_sql: str,
                    base_clauses: list[str], base_params: list) -> dict[str, int]:
    where_sql = " AND ".join(base_clauses) if base_clauses else "1=1"
    counts = _empty_counts()
    rows = c.execute(
        f"SELECT f.status, "
        f"(COALESCE(f.error, '') LIKE ?) AS is_def, "
        f"(COALESCE(f.error, '') LIKE 'link_limit |%') AS is_lim, "
        f"(COALESCE(f.error, '') IN (?, ?)) AS is_sel, "
        f"COUNT(*) AS n "
        f"FROM {from_sql} WHERE {where_sql} "
        f"GROUP BY f.status, is_def, is_lim, is_sel",
        [DEFERRED_MARKER + "%", CATEGORY_SKIP_MARKER, FOLDER_SKIP_MARKER, *base_params],
    )
    for r in rows:
        st, n = r["status"], int(r["n"])
        counts["all"] += n
        if st == "downloaded":
            counts["downloaded"] += n
        elif st == "skipped":
            if source == "disk" and not r["is_sel"]:
                counts["downloaded"] += n
            else:
                counts["excluded"] += n
        elif st == "failed":
            counts["failed"] += n
            counts["not_downloaded"] += n
        elif st == "in_progress":
            counts["queued"] += n
            counts["not_downloaded"] += n
        elif st == "pending":
            counts["not_downloaded"] += n
            if r["is_def"]:
                counts["deferred"] += n
            elif source == "links" and r["is_lim"]:
                counts["limited"] += n
            else:
                counts["queued"] += n
    return counts


def _select_columns(source: str, cols: set[str], id_col: str) -> str:
    name_expr = "f.name AS name_col" if "name" in cols else "NULL AS name_col"
    local_expr = "f.local_path AS local_path" if "local_path" in cols else "NULL AS local_path"
    dl_expr = "f.downloaded_at AS downloaded_at" if "downloaded_at" in cols else "NULL AS downloaded_at"
    prev_expr = "f.preview_path AS preview_path" if "preview_path" in cols else "NULL AS preview_path"
    link_expr = ", f.link_id AS link_id, l.name AS link_name" if source == "links" else ""
    return (
        f"f.rowid AS _rid, f.{id_col} AS raw_path, {name_expr}, COALESCE(f.size, 0) AS size, "
        f"f.status AS status, f.error AS error, {local_expr}, {dl_expr}, {prev_expr}{link_expr}"
    )


def _format_row(source: str, r: sqlite3.Row) -> dict:
    raw_path = str(r["raw_path"] or "")
    name, parent = _split_name_dir(source, raw_path, r["name_col"])
    state, reason = _classify_row(source, r["status"], r["error"])
    item = {
        "path": raw_path,
        "display_path": _clean_display_path(source, raw_path),
        "name": name,
        "dir": parent,
        "size": int(r["size"] or 0),
        "state": state,
        "state_label": STATE_LABELS.get(state, state),
        "reason": reason,
        "local_path": r["local_path"] or "",
        "downloaded_at": utc_ts(r["downloaded_at"]) if r["downloaded_at"] else None,
        "downloaded_at_raw": r["downloaded_at"] or "",
        "preview": bool(r["preview_path"]) and state != "downloaded",
    }
    if source == "links":
        item["link_id"] = r["link_id"]
        item["link_name"] = r["link_name"] or "Ссылка"
    return item


def _open_ro(db_file: Path) -> sqlite3.Connection:
    c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    c.create_function("ci_contains", 2, _ci_contains)
    return c


def query_files(
    source: str,
    *,
    status: str = "not_downloaded",
    q: str = "",
    folder: str = "",
    link_id: int | None = None,
    sort: str = "default",
    page: int = 1,
    limit: int = 100,
    db_file: Path | None = None,
) -> dict:
    """Постраничная выборка файлов источника со счётчиками по всем вкладкам."""
    if source not in VALID_SOURCES:
        raise ValueError(f"Неизвестный источник: {source}")
    status = status if status in VALID_STATUSES else "not_downloaded"
    sort = sort if sort in VALID_SORTS else "default"
    page = max(1, int(page))
    limit = max(1, min(int(limit), 500))
    q = (q or "").strip()

    db = _source_db(source, db_file)
    empty = {
        "ok": True, "source": source, "status": status, "q": q, "folder": folder,
        "counts": _empty_counts(), "total": 0, "page": 1, "pages": 1, "limit": limit, "items": [],
    }
    if not db.exists():
        return empty

    table, id_col, from_sql = _table_meta(source)
    c = None
    try:
        c = _open_ro(db)
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return empty
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        base_clauses, base_params = _base_where(source, folder, link_id)
        counts = _collect_counts(c, source, from_sql, base_clauses, base_params)

        st_clause, st_params = _status_clause(source, status)
        where_parts = [*base_clauses, st_clause]
        where_params = [*base_params, *st_params]
        if q:
            if source == "unlim" and "name" in cols:
                where_parts.append("(ci_contains(f.name, ?) OR ci_contains(f.file_id, ?))")
                where_params.extend([q, q])
            else:
                where_parts.append(f"ci_contains(f.{id_col}, ?)")
                where_params.append(q)

        where_sql = " AND ".join(where_parts) if where_parts else "1=1"
        total = int(c.execute(f"SELECT COUNT(*) FROM {from_sql} WHERE {where_sql}", where_params).fetchone()[0])
        pages = max(1, math.ceil(total / limit))
        if page > pages:
            page = pages
        offset = (page - 1) * limit
        order_sql = _order_by(source, status, sort, id_col)
        sel = _select_columns(source, cols, id_col)
        rows = c.execute(
            f"SELECT {sel} FROM {from_sql} WHERE {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
            [*where_params, limit, offset],
        ).fetchall()
        return {
            "ok": True, "source": source, "status": status, "q": q, "folder": folder,
            "counts": counts, "total": total, "page": page, "pages": pages, "limit": limit,
            "items": [_format_row(source, r) for r in rows],
        }
    except sqlite3.Error as e:
        return {**empty, "ok": False, "error": str(e)}
    finally:
        if c is not None:
            c.close()


# ── Потоковый экспорт CSV / TXT ─────────────────────────────────────────────


def _csv_safe(value: str) -> str:
    """Защита от CSV-инъекции формул в Excel (=, +, -, @, табуляция, перенос)."""
    s = str(value or "")
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def iter_files_for_export(
    source: str,
    *,
    status: str = "not_downloaded",
    q: str = "",
    folder: str = "",
    link_id: int | None = None,
    db_file: Path | None = None,
) -> Iterator[dict]:
    """Читает файлы батчами по rowid (O(1) память даже на 250 000+ записях)."""
    status = status if status in VALID_STATUSES else "not_downloaded"
    q = (q or "").strip()
    db = _source_db(source, db_file)
    if not db.exists():
        return
    table, id_col, from_sql = _table_meta(source)
    c = _open_ro(db)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        base_clauses, base_params = _base_where(source, folder, link_id)
        st_clause, st_params = _status_clause(source, status)
        where_parts = [*base_clauses, st_clause]
        where_params = [*base_params, *st_params]
        if q:
            if source == "unlim" and "name" in cols:
                where_parts.append("(ci_contains(f.name, ?) OR ci_contains(f.file_id, ?))")
                where_params.extend([q, q])
            else:
                where_parts.append(f"ci_contains(f.{id_col}, ?)")
                where_params.append(q)
        sel = _select_columns(source, cols, id_col)
        last_rid = 0
        while True:
            chunk_where = " AND ".join([*where_parts, "f.rowid > ?"])
            rows = c.execute(
                f"SELECT {sel} FROM {from_sql} WHERE {chunk_where} ORDER BY f.rowid ASC LIMIT ?",
                [*where_params, last_rid, EXPORT_CHUNK],
            ).fetchall()
            if not rows:
                break
            last_rid = int(rows[-1]["_rid"])
            for r in rows:
                yield _format_row(source, r)
    finally:
        c.close()


def iter_export_bytes(
    items: Iterator[dict],
    source: str,
    fmt: str = "csv",
) -> Iterator[bytes]:
    """Кодирует поток файлов в CSV (UTF-8 BOM, ';') или TXT (один путь на строку)."""
    if fmt == "txt":
        buf: list[str] = []
        for it in items:
            prefix = f"[{it['link_name']}] " if source == "links" and it.get("link_name") else ""
            buf.append(f"{prefix}{it['display_path']}\n")
            if len(buf) >= 500:
                yield "".join(buf).encode("utf-8")
                buf.clear()
        if buf:
            yield "".join(buf).encode("utf-8")
        return

    # CSV с UTF-8 BOM и разделителем ';' — Excel на русской Windows сразу раскладывает колонки
    yield b"\xef\xbb\xbf"
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    header = ["Статус", "Имя файла", "Путь на Яндексе", "Размер (байт)", "Размер",
              "Причина", "Локальный путь", "Дата скачивания (UTC)", "Превью"]
    if source == "links":
        header.insert(1, "Ссылка")
    writer.writerow(header)
    yield out.getvalue().encode("utf-8")
    out.seek(0)
    out.truncate(0)

    count = 0
    for it in items:
        row = [
            it["state_label"],
            _csv_safe(it["name"]),
            _csv_safe(it["display_path"]),
            it["size"],
            human_bytes(it["size"]) if it["size"] else "",
            _csv_safe(it["reason"]),
            _csv_safe(it["local_path"]),
            it["downloaded_at_raw"],
            "да" if it["preview"] else "",
        ]
        if source == "links":
            row.insert(1, _csv_safe(it.get("link_name") or ""))
        writer.writerow(row)
        count += 1
        if count >= 500:
            yield out.getvalue().encode("utf-8")
            out.seek(0)
            out.truncate(0)
            count = 0
    if count:
        yield out.getvalue().encode("utf-8")


def export_to_file(
    source: str,
    out_path: str | Path,
    *,
    status: str = "not_downloaded",
    fmt: str = "csv",
    q: str = "",
    folder: str = "",
    link_id: int | None = None,
    db_file: Path | None = None,
) -> int:
    """Пишет экспорт в файл (для CLI). Возвращает число записанных строк."""
    fmt = "txt" if fmt == "txt" else "csv"
    counted = 0

    def _counting() -> Iterator[dict]:
        nonlocal counted
        for item in iter_files_for_export(source, status=status, q=q, folder=folder,
                                          link_id=link_id, db_file=db_file):
            counted += 1
            yield item

    dst = Path(out_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as f:
        for chunk in iter_export_bytes(_counting(), source, fmt):
            f.write(chunk)
    return counted


# ── Демо-режим ──────────────────────────────────────────────────────────────


_DEMO_ITEMS = {
    "disk": [
        ("disk:/Документы/Договор аренды.pdf", 2_400_000, "downloaded", None, "D:\\YandexBackup\\Документы\\Договор аренды.pdf", "2026-11-18T12:10:00", False),
        ("disk:/Фото/2023/Отпуск/IMG_2041.JPG", 5_120_000, "downloaded", None, "D:\\YandexBackup\\Фото\\2023\\Отпуск\\IMG_2041.JPG", "2026-11-18T12:08:00", False),
        ("disk:/Фото/2023/Отпуск/IMG_2042.JPG", 4_890_000, "pending", "storage_down | host silent", "", "", True),
        ("disk:/Видео/День рождения.mp4", 840_000_000, "pending", None, "", "", False),
        ("disk:/Работа/Отчёт_2024.xlsx", 420_000, "failed", "http_error | 500", "", "", False),
        ("disk:/Архивы/backup_2020.zip", 1_500_000_000, "skipped", CATEGORY_SKIP_MARKER, "", "", False),
    ],
    "unlim": [
        ("/photounlim/2021-07-14 18-22-05.JPG", 4_200_000, "downloaded", None, "D:\\YandexBackup\\_unlim\\2021-07-14 18-22-05.JPG", "2026-11-18 12:12:00", False),
        ("/photounlim/2021-07-15 09-02-11.HEIC", 3_600_000, "pending", "storage_down | silent", "", "", True),
        ("/photounlim/2021-07-15 09-03-37.MOV", 125_000_000, "pending", None, "", "", False),
        ("/photounlim/2021-07-16 20-14-55.JPG", 2_900_000, "failed", "bad_file | MD5 mismatch", "", "", False),
    ],
    "links": [
        ("/Фото/Пляж.jpg", 3_200_000, "downloaded", None, "D:\\YandexBackup\\_ссылки\\Отпуск\\Фото\\Пляж.jpg", "2026-11-18 11:30:00", False),
        ("/Фото/Закат.jpg", 4_100_000, "pending", "storage_down | silent", "", "", True),
        ("/Видео/Море.mp4", 98_000_000, "pending", "link_limit | 429", "", "", False),
    ],
}


def _demo_rows(source: str, link_id: int | None = None) -> list[dict]:
    out = []
    for raw_path, size, st, err, local, dl_at, preview in _DEMO_ITEMS.get(source, []):
        name, parent = _split_name_dir(source, raw_path)
        state, reason = _classify_row(source, st, err)
        item = {
            "path": raw_path, "display_path": _clean_display_path(source, raw_path),
            "name": name, "dir": parent, "size": size, "state": state,
            "state_label": STATE_LABELS.get(state, state), "reason": reason,
            "local_path": local, "downloaded_at": utc_ts(dl_at) if dl_at else None,
            "downloaded_at_raw": dl_at, "preview": preview,
        }
        if source == "links":
            item["link_id"] = link_id or 1
            item["link_name"] = "Отпуск"
        out.append(item)
    return out


def demo_file_list(source: str, *, status: str = "not_downloaded", q: str = "",
                   folder: str = "", link_id: int | None = None,
                   page: int = 1, limit: int = 100) -> dict:
    all_rows = _demo_rows(source, link_id)
    if source == "disk" and folder:
        _p, prefix = folder_split(folder)
        if prefix != "disk:/":
            all_rows = [r for r in all_rows if r["path"].startswith(prefix)]
    counts = _empty_counts()
    for r in all_rows:
        counts["all"] += 1
        st = r["state"]
        if st == "downloaded":
            counts["downloaded"] += 1
        elif st == "excluded":
            counts["excluded"] += 1
        else:
            counts["not_downloaded"] += 1
            counts[st] = counts.get(st, 0) + 1
    if status == "not_downloaded":
        rows = [r for r in all_rows if r["state"] in ("queued", "deferred", "failed", "limited")]
    elif status != "all":
        rows = [r for r in all_rows if r["state"] == status]
    else:
        rows = list(all_rows)
    if q:
        qf = q.casefold()
        rows = [r for r in rows if qf in r["path"].casefold() or qf in r["name"].casefold()]
    total = len(rows)
    pages = max(1, math.ceil(total / limit))
    page = max(1, min(page, pages))
    start = (page - 1) * limit
    return {
        "ok": True, "source": source, "status": status, "q": q, "folder": folder,
        "counts": counts, "total": total, "page": page, "pages": pages, "limit": limit,
        "items": rows[start:start + limit],
    }


def _demo_on(demo: str | None) -> bool:
    return bool(demo) and os.environ.get("YADISK_DEMO") == "1"


# ── HTTP-маршруты ───────────────────────────────────────────────────────────


@router.get("/api/{source}/files")
def api_file_list(
    source: str,
    status: str = "not_downloaded",
    q: str = "",
    folder: str = "",
    link_id: int | None = None,
    sort: str = "default",
    page: int = 1,
    limit: int = 100,
    demo: str | None = None,
):
    if source not in VALID_SOURCES:
        raise HTTPException(404, "Нет такого источника")
    if _demo_on(demo):
        return demo_file_list(source, status=status, q=q, folder=folder, link_id=link_id, page=page, limit=limit)
    return query_files(source, status=status, q=q, folder=folder, link_id=link_id, sort=sort, page=page, limit=limit)


@router.get("/api/{source}/files/export")
def api_file_list_export(
    source: str,
    status: str = "not_downloaded",
    format: str = "csv",  # noqa: A002
    q: str = "",
    folder: str = "",
    link_id: int | None = None,
    demo: str | None = None,
):
    if source not in VALID_SOURCES:
        raise HTTPException(404, "Нет такого источника")
    fmt = "txt" if format == "txt" else "csv"
    st = status if status in VALID_STATUSES else "not_downloaded"
    ts = time.strftime("%Y%m%d-%H%M")
    filename = f"yadisk-{source}-{st}-{ts}.{fmt}"
    media = "text/plain; charset=utf-8" if fmt == "txt" else "text/csv; charset=utf-8"
    if _demo_on(demo):
        items = iter(demo_file_list(source, status=st, q=q, folder=folder, link_id=link_id, limit=500)["items"])
    else:
        items = iter_files_for_export(source, status=st, q=q, folder=folder, link_id=link_id)
    return StreamingResponse(
        iter_export_bytes(items, source, fmt),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
