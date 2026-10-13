"""Общий код для модулей безлимитного фото-хранилища (unlim_download / unlim_worker).

Вынесены повторяющиеся: константы, загрузка кук, создание сессии, получение sk/uid,
миграция БД.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import sys
import time

import requests
from dotenv import load_dotenv

log = logging.getLogger("unlim")

PROJECT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT, ".env"))

COOKIES_FILE = os.path.join(PROJECT, "cookies.json")
UNLIM_DB_FILE = os.path.join(PROJECT, "unlim_state.db")
LOG_DIR = os.path.join(PROJECT, "logs")

BASE = "https://disk.yandex.ru"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(PROJECT, "downloads")).rstrip("/\\")

UNLIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS unlim_files (
    file_id TEXT PRIMARY KEY,
    cluster_id TEXT,
    name TEXT,
    size INTEGER,
    md5 TEXT,
    mime TEXT,
    status TEXT DEFAULT 'pending',
    local_path TEXT,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    worker_id INTEGER,
    assigned_at TIMESTAMP,
    downloaded_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_unlim_status ON unlim_files(status);
CREATE TABLE IF NOT EXISTS unlim_clusters (
    cluster_id TEXT PRIMARY KEY,
    size INTEGER,
    fetched INTEGER DEFAULT 0
);
"""


# ── Куки и HTTP-сессия ──────────────────────────────────────────────────────


def load_cookies(cookies_file: str | None = None) -> dict[str, str]:
    """Загружает куки из cookies.json (формат Cookie-Editor)."""
    path = cookies_file or COOKIES_FILE
    if not os.path.exists(path):
        sys.exit("Нет cookies.json — сначала экспортируй куки из браузера. "
                 "См. README раздел про куки.")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        sys.exit("cookies.json должен быть массивом (формат Cookie-Editor)")
    return {
        c["name"]: c["value"]
        for c in raw
        if c.get("name") and c.get("value")
    }


def make_session(cookies: dict[str, str] | None = None,
                 cookies_file: str | None = None) -> requests.Session:
    """Создаёт requests.Session с нужными заголовками и куками.

    Если cookies не переданы — загружает из cookies.json.
    """
    if cookies is None:
        cookies = load_cookies(cookies_file)
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "ru,en;q=0.9",
        "Origin": BASE, "Referer": f"{BASE}/client/photo",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
    })
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".yandex.ru")
    return s


def get_sk_uid(s: requests.Session) -> tuple[str, str]:
    """Извлекает sk и uid из HTML-страницы диска."""
    r = s.get(f"{BASE}/client/disk", timeout=30)
    if "passport.yandex" in r.url or r.status_code != 200:
        sys.exit("Куки невалидны — нужно обновить cookies.json")
    sk = re.search(r'"sk"\s*:\s*"([^"]+)"', r.text)
    uid = re.search(r'"uid"\s*:\s*"?(\d+)"?', r.text)
    if not sk or not uid:
        sys.exit("Не нашёл sk/uid в HTML — Яндекс изменил формат")
    return sk.group(1), uid.group(1)


def make_cid(uid: str) -> str:
    """Генерирует connection_id для API-запроса."""
    return f"{uid}{int(time.time() * 1000)}{random.randint(100, 999)}"


def api_call(s: requests.Session, sk: str, uid: str, method: str,
             params=None, retries: int = 3):
    """Вызов web-API Яндекс.Диска с retry и обработкой rate limit."""
    last_err = None
    for att in range(retries):
        try:
            body = {"sk": sk, "connection_id": make_cid(uid), "apiMethod": method}
            if params is not None:
                body["requestParams"] = params
            r = s.post(f"{BASE}/models-v2", params={"m": method},
                       json=body, timeout=120)
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    raise RuntimeError(f"{method}: ответ не JSON: {r.text[:200]}")
                if not isinstance(data, dict):
                    raise RuntimeError(f"{method}: неожиданный формат ответа: {type(data).__name__}")
                return data
            if r.status_code in (401, 403):
                sys.exit(f"Сессия истекла (HTTP {r.status_code})")
            if r.status_code == 429:
                wait = 30 + att * 30
                log.warning(f"{method} HTTP 429 (Лимит запросов Яндекса) → sleep {wait}с")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                log.warning(f"{method} HTTP {r.status_code} (Сбой узлов Яндекса / дата-центра) → повтор через {2 ** att}с")
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** att)
    raise RuntimeError(f"{method}: {last_err} (Проблема на стороне серверов Яндекса. Новости и статус: https://t.me/davronsv)")


# ── Миграция БД ─────────────────────────────────────────────────────────────


def ensure_attempts_column(db_path: str | None = None) -> None:
    """Миграция для старых БД: добавляет колонку attempts и индекс если отсутствуют."""
    db_path = db_path or UNLIM_DB_FILE
    c = sqlite3.connect(db_path, timeout=60)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(unlim_files)")}
        if "attempts" not in cols:
            c.execute("ALTER TABLE unlim_files ADD COLUMN attempts INTEGER DEFAULT 0")
            cols.add("attempts")
        if {"status", "attempts", "assigned_at"}.issubset(cols):
            c.execute("CREATE INDEX IF NOT EXISTS idx_unlim_pending ON unlim_files(status, attempts, assigned_at)")
        c.commit()
    finally:
        c.close()
