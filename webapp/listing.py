"""Когда собирали список файлов и что собирать перед следующим скачиванием.

Запись лежит в самой базе источника (state.db → meta, unlim_state.db → unlim_meta): перезапуск
программы её не стирает, а «Забыть список файлов» удаляет вместе с базой. Значение — JSON
{"at": время сбора, "whole": собран весь Диск, "folders": папки, собранные после этого}.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from state import State
from unlim_common import UNLIM_SCHEMA, connect_unlim_db
from utils import sqlite_ro_uri, sqlite_tx
from webapp import paths

LISTING_KEY = "listing"
# Список файлов моложе этого срока при новом старте не пересобираем — даже после перезапуска
# программы: запись о сборе лежит в самой базе (см. read_listing)
RESCUE_RELIST_SEC = 6 * 3600


def _listing_db(source: str) -> tuple[Path, str]:
    return (paths.DB_FILE, "meta") if source == "disk" else (paths.UNLIM_DB_FILE, "unlim_meta")


def parse_listing(value: str | None) -> dict | None:
    try:
        rec = json.loads(value or "")
    except ValueError:
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("at"), (int, float)):
        return None
    folders = rec.get("folders")
    return {"at": float(rec["at"]), "whole": bool(rec.get("whole")),
            "folders": [f for f in folders if isinstance(f, str)] if isinstance(folders, list) else []}


def read_listing(source: str) -> dict | None:
    """Последний удачный сбор списка источника или None, если списка нет."""
    db, table = _listing_db(source)
    if not db.exists():
        return None
    try:
        c = sqlite3.connect(sqlite_ro_uri(db), uri=True, timeout=30)
        try:
            row = c.execute(f"SELECT value FROM {table} WHERE key = ?", (LISTING_KEY,)).fetchone()
        finally:
            c.close()
    except sqlite3.Error:  # старая база без служебной таблицы
        return None
    return parse_listing(row[0]) if row else None


def write_listing(source: str, rec: dict | None) -> None:
    """Запоминает сбор списка; None — забыть: следующий запуск соберёт список заново."""
    db, _table = _listing_db(source)
    if rec is None and not db.exists():
        return
    value = None if rec is None else json.dumps(rec, ensure_ascii=False)
    if source == "disk":
        st = State(str(db))  # создаст базу со всеми таблицами, если её ещё нет
        try:
            st.set_meta(LISTING_KEY, value)
        finally:
            st.close()
        return
    c = connect_unlim_db(str(db))
    try:
        c.executescript(UNLIM_SCHEMA)
        with sqlite_tx(c):
            if value is None:
                c.execute("DELETE FROM unlim_meta WHERE key = ?", (LISTING_KEY,))
            else:
                c.execute("INSERT INTO unlim_meta (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (LISTING_KEY, value))
    finally:
        c.close()


def listing_stale(rec: dict | None, now: float) -> bool:
    """Список надо собрать заново: его нет, он старше RESCUE_RELIST_SEC или часы ушли назад."""
    return rec is None or now - rec["at"] > RESCUE_RELIST_SEC or rec["at"] - now > 300


def disk_listing_plan(folders: list[str] | None, rec: dict | None,
                      now: float) -> tuple[str, list[str] | None]:
    """Что собрать перед скачиванием Диска. folders: None — весь Диск.

    ("all", folders) — весь выбор заново: списка нет, он устарел или выбран весь Диск, а собраны
    были только папки. ("new", [папки]) — только папки, которых не было в прошлом сборе: так
    найдётся и то, что скопировали на Диск уже после него, а весь Диск заново не сканируется.
    ("none", None) — список свежий.
    """
    if listing_stale(rec, now):
        return "all", folders
    if not folders:
        return ("none", None) if rec["whole"] else ("all", None)
    known = set(rec["folders"])
    new = [f for f in folders if f not in known]
    return ("new", new) if new else ("none", None)
