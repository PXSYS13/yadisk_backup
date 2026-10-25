"""Очереди скачивания в базах: счётчики для интерфейса, выбор «что качать», упавшие файлы.

state.db (таблица files, ключ remote_path) — обычный Диск; unlim_state.db (unlim_files,
ключ file_id) — Безлимит. Отложенный файл — это pending с ошибкой «storage_down | …»,
снятый выбором человека — skipped с маркером из state.SELECTION_MARKERS.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from state import CATEGORY_SKIP_MARKER, FOLDER_SKIP_MARKER
from unlim_common import DEFERRED_MARKER, RELEASE_IN_PROGRESS_SQL, unlim_tx
from utils import CATEGORY_LABELS, categorize, sqlite_ro_uri, sqlite_tx, utc_ts
from webapp import paths
from webapp.listing import LISTING_KEY, parse_listing

# Белый список для SQL-идентификаторов — защита от инъекций через f-string
ALLOWED_TABLES = {"files", "unlim_files"}
ALLOWED_COLUMNS = {"remote_path", "file_id"}

# Временные ошибки Диска: явный старт возвращает такие файлы в очередь
DISK_TRANSIENT_ERRORS = ("local_disk |", "local_file |", "auth_expired |", "server_50x |",
                         "download_timeout |", "rate_limit |",
                         # Версии до 1.2 писали «ИмяИсключения: текст» без метки и считали
                         # таймаут молчащего сервера ошибкой файла — такие файлы застревали навсегда
                         "RequestTimeoutError:", "InternalServerError:", "BadGatewayError:",
                         "GatewayTimeoutError:", "ServiceUnavailableError:", "RequestError:",
                         "TooManyRequestsError:", "429 TooManyRequests:", "ConnectionError:",
                         "ReadTimeout:", "ConnectTimeout:")

# Почему файл не скачан — простыми словами (по префиксу ошибки «kind | …»)
REASON_TEXT = (
    ("storage_down |", "Сервер Яндекса с этим файлом не отвечает — программа повторит сама"),
    ("server_50x |", "Сбой серверов Яндекса"),
    ("download_timeout |", "Яндекс отвечал слишком долго"),
    ("timeout |", "Яндекс отвечал слишком долго"),
    ("rate_limit |", "Яндекс просил обращаться реже"),
    ("auth_expired |", "Доступ устарел — обновите ключ или доступ к Безлимиту"),
    ("local_disk |", "Не удалось записать файл на ваш диск"),
    ("local_file |", "Файл не записать в эту папку: слишком длинное имя или файл больше 4 ГБ для диска FAT32"),
    ("gone |", "Файла больше нет на Диске"),
    ("bad_file |", "Файл пришёл повреждённым"),
    ("http_error |", "Яндекс отказал в скачивании"),
    ("refused |", "Яндекс отказал в скачивании"),
    ("prepare_failed |", "Яндекс не выдал ссылку на файл"),
    ("pathnotfound", "Файла больше нет на Диске"),
)


def validate_sql_ident(value: str, allowed: set[str], label: str) -> str:
    """Проверяет что SQL-идентификатор из белого списка."""
    if value not in allowed:
        raise ValueError(f"Недопустимый {label}: {value!r}")
    return value


def empty_db_stats() -> dict:
    return {
        "total": 0, "pending": 0, "downloaded": 0, "skipped": 0,
        "failed": 0, "in_progress": 0,
        # Сняты выбором человека: category_* — категории и папки вместе (так их
        # показывает интерфейс: «не выбраны»), folder_* — только выбор папок
        "category_skipped": 0, "category_skipped_bytes": 0,
        "folder_skipped": 0, "folder_skipped_bytes": 0,
        "bytes_total": 0, "bytes_done": 0,
        "bytes_done_real": 0, "bytes_pending": 0,
        "bytes_in_progress": 0, "bytes_failed": 0,
        "bytes_selected_total": 0,
        "done_10m": 0, "bytes_10m": 0,
        # Когда последний раз собирали список (см. listing.read_listing); None — не собирали
        "listed_at": None,
        # Отложенные (сервер хранения молчит) — часть pending
        "deferred": 0, "bytes_deferred": 0, "deferred_hosts": 0,
        # Страховочные превью у ещё не скачанных оригиналов
        "previews": 0,
    }


def db_stats(db_file: Path, table: str = "files",
             id_col: str = "remote_path") -> dict:
    table = validate_sql_ident(table, ALLOWED_TABLES, "table")
    id_col = validate_sql_ident(id_col, ALLOWED_COLUMNS, "column")
    out = empty_db_stats()
    if not db_file.exists():
        out["recent"] = []
        return out
    c = None
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        for r in c.execute(
            f"SELECT status, (COALESCE(error, '') LIKE ?) AS deferred, "
            f"(COALESCE(error, '') = ?) AS cat_x, (COALESCE(error, '') = ?) AS fold_x, "
            f"COUNT(*) AS n, COALESCE(SUM(size),0) AS b "
            f"FROM {table} GROUP BY status, deferred, cat_x, fold_x",
            (DEFERRED_MARKER + "%", CATEGORY_SKIP_MARKER, FOLDER_SKIP_MARKER),
        ):
            st = r["status"]
            n = r["n"]
            b = r["b"]
            out[st] = out.get(st, 0) + n
            out["total"] += n
            out["bytes_total"] += b
            if st == "pending":
                out["bytes_pending"] += b
                if r["deferred"]:
                    out["deferred"] += n
                    out["bytes_deferred"] += b
            elif st == "in_progress":
                out["bytes_in_progress"] += b
            elif st == "failed":
                out["bytes_failed"] += b
            elif st == "downloaded":
                out["bytes_done_real"] += b
                out["bytes_done"] += b
            elif st == "skipped":
                if r["cat_x"] or r["fold_x"]:
                    out["category_skipped"] += n
                    out["category_skipped_bytes"] += b
                    if r["fold_x"]:
                        out["folder_skipped"] += n
                        out["folder_skipped_bytes"] += b
                else:
                    out["bytes_done"] += b

        out["bytes_selected_total"] = max(0, out["bytes_total"] - out["category_skipped_bytes"])
        if out["deferred"]:
            try:
                out["deferred_hosts"] = c.execute(
                    f"SELECT COUNT(DISTINCT storage_host) FROM {table} "
                    f"WHERE status='pending' AND error LIKE ? AND storage_host IS NOT NULL",
                    (DEFERRED_MARKER + "%",),
                ).fetchone()[0]
            except sqlite3.OperationalError:  # старая БД без колонки storage_host
                pass
        try:
            out["previews"] = c.execute(
                f"SELECT COUNT(*) FROM {table} WHERE preview_path IS NOT NULL AND status <> 'downloaded'"
            ).fetchone()[0]
        except sqlite3.OperationalError:  # старая БД без колонки preview_path
            pass
        # Скорость за последние 10 минут — для оценки времени в мастере плана.
        # files пишет downloaded_at как 'YYYY-MM-DDTHH:MM:SS', unlim — 'YYYY-MM-DD HH:MM:SS' (оба UTC)
        rate = c.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM {table} "
            f"WHERE status='downloaded' AND downloaded_at IS NOT NULL "
            f"AND replace(downloaded_at, 'T', ' ') >= datetime('now', '-10 minutes')"
        ).fetchone()
        out["done_10m"] = rate["n"]
        out["bytes_10m"] = rate["b"]
        recent = list(c.execute(
            f"SELECT {id_col} AS path, status, size, downloaded_at FROM {table} "
            f"WHERE downloaded_at IS NOT NULL "
            f"ORDER BY downloaded_at DESC LIMIT 10"
        ))
        out["recent"] = [{"path": r["path"], "status": r["status"],
                          "size": r["size"], "at": utc_ts(r["downloaded_at"])} for r in recent]
        meta = {"files": "meta", "unlim_files": "unlim_meta"}.get(table)
        if meta:
            try:
                row = c.execute(f"SELECT value FROM {meta} WHERE key = ?", (LISTING_KEY,)).fetchone()
                rec = parse_listing(row[0]) if row else None
                out["listed_at"] = rec["at"] if rec else None
            except sqlite3.OperationalError:  # старая база без служебной таблицы
                pass
        return out
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        if c is not None:
            c.close()


def disk_db_stats() -> dict:
    return db_stats(paths.DB_FILE, "files", "remote_path")


def unlim_db_stats() -> dict:
    return db_stats(paths.UNLIM_DB_FILE, "unlim_files", "file_id")


def db_has_rows(db_file: Path, table: str) -> bool:
    table = validate_sql_ident(table, ALLOWED_TABLES, "table")
    if not db_file.exists():
        return False
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        try:
            return c.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        finally:
            c.close()
    except sqlite3.Error:
        return False


def db_has_pending(db_file: Path, table: str) -> bool:
    """В очереди есть файлы, которые ещё не скачаны (в том числе ждущие Яндекса)."""
    table = validate_sql_ident(table, ALLOWED_TABLES, "table")
    if not db_file.exists():
        return False
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        try:
            return c.execute(f"SELECT 1 FROM {table} WHERE status = 'pending' LIMIT 1").fetchone() is not None
        finally:
            c.close()
    except sqlite3.Error:
        return False


def unlim_has_rows_outside(db_file: Path) -> bool:
    """В очереди безлимита есть файлы не из безлимита: старый список (фотосрез до октября 2026)
    брал все фото аккаунта. Их уберёт только свежий сбор списка — до скачивания, иначе они
    ушли бы второй копией в _unlim (их скачивает ветка Диска)."""
    if not db_file.exists():
        return False
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        try:
            return c.execute(
                "SELECT 1 FROM unlim_files WHERE status IN ('pending', 'failed') "
                "AND substr(file_id, 1, 12) != '/photounlim/' AND substr(file_id, 1, 11) != 'photounlim:' "
                "LIMIT 1").fetchone() is not None
        finally:
            c.close()
    except sqlite3.Error:
        return False


def reset_transient_disk_failures() -> int:
    """Явный старт: файлы, упавшие из-за сбоев (а не из-за самих файлов), — снова в очередь."""
    if not paths.DB_FILE.exists():
        return 0
    try:
        c = sqlite3.connect(paths.DB_FILE, timeout=30)
    except sqlite3.Error:
        return 0
    try:
        where = " OR ".join("error LIKE ?" for _ in DISK_TRANSIENT_ERRORS)
        with sqlite_tx(c):
            return c.execute(
                f"UPDATE files SET status='pending', error=NULL WHERE status='failed' AND ({where})",
                [p + "%" for p in DISK_TRANSIENT_ERRORS],
            ).rowcount
    except sqlite3.Error:
        return 0
    finally:
        c.close()


def reset_failed_disk_files() -> int:
    """«Повторить ошибки» у Диска: все упавшие файлы — снова в очередь. sqlite3.Error — наружу."""
    if not paths.DB_FILE.exists():
        return 0
    c = sqlite3.connect(paths.DB_FILE, timeout=30)
    try:
        with sqlite_tx(c):
            return c.execute("UPDATE files SET status='pending', error=NULL WHERE status='failed'").rowcount
    finally:
        c.close()


def release_orphaned_unlim() -> int:
    """Живых воркеров нет — значит, все in_progress осиротели: воркер с другим id
    (например, после запуска с 4 воркерами, а потом с 2) их сам не сбросит никогда.
    """
    if not paths.UNLIM_DB_FILE.exists():
        return 0
    try:
        with unlim_tx(paths.UNLIM_DB_FILE) as c:
            return c.execute(RELEASE_IN_PROGRESS_SQL).rowcount
    except Exception:
        return 0


# ── Выбор: какие типы файлов и папки качать ──


def category_key_expr(table: str, id_col: str) -> str:
    """По чему определять категорию: у безлимита расширение надёжнее брать из name."""
    return f"COALESCE(name, {id_col})" if table == "unlim_files" else id_col


def preview_db(db_file: Path, table: str, id_col: str) -> dict:
    """Группирует файлы в БД по категориям."""
    table = validate_sql_ident(table, ALLOWED_TABLES, "table")
    id_col = validate_sql_ident(id_col, ALLOWED_COLUMNS, "column")
    if not db_file.exists():
        return {"empty": True, "categories": {}}
    c = None
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        summary: dict[str, dict] = {
            k: {"count": 0, "bytes": 0, "label": label}
            for k, label in CATEGORY_LABELS.items()
        }
        total = 0
        # Снятое выбором папок не считаем: галочки категорий должны показывать то,
        # что реально попадёт в очередь
        for row in c.execute(
            f"SELECT {category_key_expr(table, id_col)}, COALESCE(size, 0) FROM {table} "
            f"WHERE status IN ('pending','downloaded','skipped','in_progress','failed') "
            f"AND COALESCE(error, '') <> ?",
            (FOLDER_SKIP_MARKER,),
        ):
            cat = categorize(row[0])
            summary[cat]["count"] += 1
            summary[cat]["bytes"] += row[1]
            total += 1
        return {"empty": total == 0, "total": total, "categories": summary}
    except sqlite3.Error as e:
        return {"empty": True, "error": str(e)}
    finally:
        if c is not None:
            c.close()


def filter_to_categories(db_file: Path, table: str, id_col: str,
                         selected: list[str]) -> tuple[int, int]:
    """Помечает файлы вне выбранных категорий как 'skipped' (одним проходом).
    Уже скачанные/skipped/in_progress не трогаем.
    Возвращает (skipped_count, kept_pending).
    """
    table = validate_sql_ident(table, ALLOWED_TABLES, "table")
    id_col = validate_sql_ident(id_col, ALLOWED_COLUMNS, "column")
    if not db_file.exists():
        return (0, 0)
    sel = set(selected)
    c = sqlite3.connect(db_file, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        rows = c.execute(
            f"SELECT {id_col}, {category_key_expr(table, id_col)} FROM {table} WHERE status='pending'"
        ).fetchall()
        to_skip = [ident for (ident, key) in rows if categorize(key) not in sel]
        kept = len(rows) - len(to_skip)
        if to_skip:
            with sqlite_tx(c):
                c.executemany(
                    f"UPDATE {table} SET status='skipped', error=? WHERE {id_col}=?",
                    [(CATEGORY_SKIP_MARKER, p) for p in to_skip],
                )
        return (len(to_skip), kept)
    finally:
        c.close()


def unskip_markers(db_file: Path, table: str, markers: tuple[str, ...]) -> int:
    """Возвращает в pending файлы, снятые выбором (по маркерам) — на случай нового выбора."""
    table = validate_sql_ident(table, ALLOWED_TABLES, "table")
    if not db_file.exists() or not markers:
        return 0
    c = sqlite3.connect(db_file, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        with sqlite_tx(c):
            return c.execute(
                f"UPDATE {table} SET status='pending', error=NULL "
                f"WHERE status='skipped' AND error IN ({','.join('?' * len(markers))})",
                markers,
            ).rowcount
    finally:
        c.close()


def unskip_category_marker(db_file: Path, table: str) -> int:
    """Сбрасывает skipped-with-marker обратно в pending — на случай повторного выбора."""
    return unskip_markers(db_file, table, (CATEGORY_SKIP_MARKER,))


def filter_to_folders(db_file: Path, folders: list[str]) -> tuple[int, int]:
    """Снимает с очереди Диска файлы вне выбранных папок: 'skipped' + маркер папок.

    Трогает pending и failed: иначе «Повторить» скачал бы невыбранное, а Главная
    показывала бы его как «не скачалось». Сравнение — точный префикс через substr:
    в именах папок бывают % и _, а LIKE к тому же не различает регистр.
    Возвращает (снято, осталось в очереди).
    """
    if not db_file.exists() or not folders:
        return (0, 0)
    inside = " OR ".join("(remote_path = ? OR substr(remote_path, 1, ?) = ?)" for _ in folders)
    params: list = []
    for f in folders:
        params += [f, len(f) + 1, f + "/"]
    c = sqlite3.connect(db_file, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        with sqlite_tx(c):
            skipped = c.execute(
                f"UPDATE files SET status='skipped', error=? "
                f"WHERE status IN ('pending','failed') AND NOT ({inside})",
                [FOLDER_SKIP_MARKER, *params],
            ).rowcount
        kept = c.execute("SELECT COUNT(*) FROM files WHERE status='pending'").fetchone()[0]
        return (skipped, kept)
    finally:
        c.close()


# ── Что не скачалось и почему ──


def reason_text(error: str | None) -> str:
    low = (error or "").lower()
    for prefix, text in REASON_TEXT:
        if low.startswith(prefix.lower()) or prefix.lower() in low[:40]:
            return text
    return "Другая ошибка — подробности в отчёте для поддержки" if low else ""


def list_problem_files(db_file: Path, table: str, id_col: str, limit: int) -> dict:
    """Нескачанные файлы: упавшие и отложенные, с причиной простыми словами."""
    table = validate_sql_ident(table, ALLOWED_TABLES, "table")
    id_col = validate_sql_ident(id_col, ALLOWED_COLUMNS, "column")
    out: dict = {"items": [], "reasons": {}, "total": 0, "failed": 0}
    if not db_file.exists():
        return out
    c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        preview_col = "preview_path" if "preview_path" in cols else "NULL AS preview_path"
        where = ("status='failed' OR (status='pending' AND COALESCE(error, '') LIKE ?)")
        for r in c.execute(f"SELECT status, error, COUNT(*) AS n FROM {table} WHERE {where} "
                           f"GROUP BY status, substr(error, 1, 20)", (DEFERRED_MARKER + "%",)):
            text = reason_text(r["error"])
            out["reasons"][text] = out["reasons"].get(text, 0) + r["n"]
            out["total"] += r["n"]
            if r["status"] == "failed":
                out["failed"] += r["n"]
        # Сначала упавшие: их можно повторить кнопкой, а отложенных бывают тысячи, и за ними
        # упавшие не попадали в первые limit строк
        for r in c.execute(
            f"SELECT {id_col} AS path, size, status, error, {preview_col} FROM {table} WHERE {where} "
            f"ORDER BY status, {id_col} LIMIT ?",
            (DEFERRED_MARKER + "%", limit),
        ):
            out["items"].append({
                "path": r["path"], "name": os.path.basename(str(r["path"]).rstrip("/")),
                "size": r["size"] or 0,
                "state": "deferred" if r["status"] == "pending" else "failed",
                "reason": reason_text(r["error"]),
                "preview": bool(r["preview_path"]),
            })
    except sqlite3.Error as e:
        out["error"] = str(e)
    finally:
        c.close()
    return out
