"""Единые SQL-предикаты и подключения для баз Web UI (queue_db, file_list, folders, links).

Хранит маркеры отложенных/ограниченных файлов в одном месте, чтобы счётчики Главной,
дерева «Папок» и «Списка файлов» всегда сходились до одного файла.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from unlim_common import DEFERRED_MARKER
from utils import sqlite_ro_uri

LINK_LIMIT_PREFIX = "link_limit |"


def deferred_sql(alias: str = "") -> str:
    """Точное сравнение префикса storage_down через substr (без LIKE-подстановок '_'/'%')."""
    col = f"{alias}.error" if alias else "error"
    return f"substr(COALESCE({col}, ''), 1, {len(DEFERRED_MARKER)}) = ?"


def limited_sql(alias: str = "") -> str:
    """Точное сравнение префикса link_limit через substr."""
    col = f"{alias}.error" if alias else "error"
    return f"substr(COALESCE({col}, ''), 1, {len(LINK_LIMIT_PREFIX)}) = '{LINK_LIMIT_PREFIX}'"


def connect_ro(db_file: Path, *, timeout: float = 30) -> sqlite3.Connection:
    """Соединение только на чтение (URI mode=ro) с sqlite3.Row."""
    c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=timeout)
    c.row_factory = sqlite3.Row
    return c


def connect_rw(db_file: Path, *, timeout: float = 30) -> sqlite3.Connection:
    """Соединение на запись в autocommit-режиме (isolation_level=None) для sqlite_tx."""
    c = sqlite3.connect(db_file, timeout=timeout, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c
