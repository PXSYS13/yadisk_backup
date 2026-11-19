"""Общий код для модулей безлимитного фото-хранилища (unlim_download / unlim_worker / webui).

Вынесены повторяющиеся: константы, загрузка кук, создание сессии, получение sk/uid,
миграция БД, соединения и транзакции над unlim_state.db.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

import requests
from dotenv import load_dotenv

from storage_fetch import response_trace  # noqa: F401 — заодно реэкспорт для воркера и тестов
from utils import path_key, set_aside, sqlite_tx

log = logging.getLogger("unlim")

PROJECT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT, ".env"))

COOKIES_FILE = os.path.join(PROJECT, "cookies.json")
UNLIM_DB_FILE = os.path.join(PROJECT, "unlim_state.db")
LOG_DIR = os.path.join(PROJECT, "logs")

BASE = "https://disk.yandex.ru"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
# Региональные домены Яндекса, на которые браузер редиректит за пределами РФ.
# При наличии кук с нескольких доменов приоритет всегда у yandex.ru.
YANDEX_COOKIE_DOMAINS = ("yandex.ru", "yandex.com", "yandex.kz", "yandex.by", "yandex.uz", "yandex.com.tr")

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
    downloaded_at TIMESTAMP,
    storage_host TEXT,
    preview_path TEXT,
    file_time REAL,
    dated REAL
);
CREATE INDEX IF NOT EXISTS idx_unlim_status ON unlim_files(status);
CREATE TABLE IF NOT EXISTS unlim_clusters (
    cluster_id TEXT PRIMARY KEY,
    size INTEGER,
    fetched INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS unlim_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""
# Что последняя проверка скачанного (unlim_download.py --verify) не нашла в папке или
# нашла испорченным: kind — missing / size / md5. Кнопки «Скачать недостающие» и «Заменить
# повреждённые» возвращают их в очередь
VERIFY_PROBLEMS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS unlim_verify_problems (file_id TEXT PRIMARY KEY, kind TEXT NOT NULL)"
)
UNLIM_SCHEMA += VERIFY_PROBLEMS_SCHEMA + ";\n"


class UnlimError(Exception):
    """Базовая ошибка подсистемы безлимитного хранилища."""


class AuthExpiredError(UnlimError):
    """Срок действия сессии или кук истёк (401/403/passport redirect)."""


class YandexUnavailableError(UnlimError):
    """Яндекс не ответил или ответил ошибкой сервера — куки при этом могут быть живы."""


class CookiesFormatError(SystemExit, UnlimError):
    """Неверный формат или отсутствие файла cookies.json."""


# ── Куки и HTTP-сессия ──────────────────────────────────────────────────────

# Без этих кук web-API Диска не авторизует запрос.
REQUIRED_COOKIES = ("Session_id", "yandexuid", "sessionid2")
_AUTH_JSON_MARKERS = ("bad_sk", "invalid_sk", "unauthorized", "http_401", "http_403", "not_authorized", "auth_error")


class CookieJarDict(dict):
    """Словарь кук с выбранным базовым адресом Диска (полностью совместим с обычным dict)."""

    def __init__(self, *args, base_url: str = BASE, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_url = base_url


def _cookie_expiry(c: dict) -> float | None:
    """Время истечения куки в секундах эпохи или None (сессионная кука)."""
    for key in ("expirationDate", "expires", "expiry"):
        raw = c.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val <= 0:
            return None
        # Некоторые браузеры/расширения выгружают миллисекунды
        return val / 1000.0 if val > 1e12 else val
    return None


def _is_expired(c: dict, now: float) -> bool:
    exp = _cookie_expiry(c)
    return exp is not None and exp < now


def _clean_cookie_value(val) -> str | None:
    """Проверяет, что значение куки годится для HTTP-заголовка (непустое, latin-1, без control chars)."""
    if not isinstance(val, str):
        return None
    v = val.strip("\r\n")
    if not v:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
        return None
    try:
        v.encode("latin-1")
    except UnicodeEncodeError:
        return None
    return v


def _clean_cookie_name(name) -> str | None:
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not n or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in n):
        return None
    try:
        n.encode("latin-1")
    except UnicodeEncodeError:
        return None
    return n


def cookie_base_domain(domain) -> str | None:
    """Базовый домен Яндекса для куки Диска, '' если домен не указан, или None для чужих доменов/поддоменов.

    Куки с passport.yandex.ru, mail.yandex.ru, id.yandex.ru и посторонних сайтов (google.com)
    не действуют на disk.yandex.* и не должны перетирать куки самого Диска.
    """
    if domain is None or domain == "":
        return ""
    if not isinstance(domain, str):
        return None
    d = domain.strip().lstrip(".").lower()
    if not d:
        return ""
    for tld in YANDEX_COOKIE_DOMAINS:
        if d == tld or d == f"disk.{tld}":
            return tld
    return None


def is_yandex_ru_domain(domain) -> bool:
    """Кука с домена yandex.ru / disk.yandex.ru (или без домена)."""
    return cookie_base_domain(domain) in ("", "yandex.ru")


def _parse_header_cookies(header: str) -> list[dict]:
    """Разбирает строку формата 'Cookie: a=1; b=2' (в том числе в кавычках или из нескольких строк)."""
    text = header.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    text = re.sub(r"^cookie\s*:\s*", "", text, flags=re.IGNORECASE)
    rows: list[dict] = []
    for part in text.split(";"):
        chunk = part.strip()
        if not chunk:
            continue
        name, eq, value = chunk.partition("=")
        name = name.strip()
        if eq and name and not any(ch.isspace() for ch in name):
            rows.append({"name": name, "value": value.strip()})
    return rows


def _extract_curl_cookies(text: str) -> list[dict]:
    """Извлекает куки из скопированной в DevTools команды 'Copy as cURL' (POSIX и Windows cmd)."""
    # Склеиваем переносы строк bash (\) и Windows cmd (^)
    joined = re.sub(r"[\\^]\r?\n", " ", text)
    # Windows cmd оборачивает кавычки как ^" и экранирует спецсимволы ^& ^% ^^
    if '^"' in joined:
        joined = joined.replace('^"', '"').replace("^&", "&").replace("^%", "%").replace("^^", "^")
    chunks: list[str] = []
    for m in re.finditer(
        r"""(?:-H|--header)\s+(?:'([^']*)'|"([^"]*)")""",
        joined,
        flags=re.IGNORECASE,
    ):
        hdr = (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()
        if re.match(r"^cookie\s*:", hdr, flags=re.IGNORECASE):
            chunks.append(hdr)
    for m in re.finditer(
        r"""(?:-b|--cookie)\s+(?:'([^']*)'|"([^"]*)"|(\S+))""",
        joined,
    ):
        val = next((g for g in m.groups() if g is not None), "").strip()
        if "=" in val:
            chunks.append(val)
    rows: list[dict] = []
    for ch in chunks:
        rows.extend(_parse_header_cookies(ch))
    return rows


def parse_cookies_text(text: str) -> list | None:
    """Массив кук из вставленного текста: JSON Cookie-Editor, плоский JSON-словарь,
    Copy as cURL, Netscape cookies.txt, таблица DevTools или Header String.

    None — ничего похожего на куки.
    """
    text = (text or "").strip().lstrip("\ufeff").strip()  # BOM из Блокнота
    if not text:
        return None
    try:
        raw = json.loads(text)
    except ValueError:
        raw = None
    if isinstance(raw, dict) and isinstance(raw.get("cookies"), list):
        raw = raw["cookies"]
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        # Плоский JSON {"Session_id": "...", "yandexuid": "...", ...}
        if any(k in raw for k in REQUIRED_COOKIES):
            rows = [
                {"name": str(k), "value": str(v)}
                for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, (str, int, float)) and not isinstance(v, bool)
            ]
            return rows or None
        return None
    if isinstance(raw, str):
        # Иногда пользователь вставляет Header String в двойных кавычках — json.loads видит JSON-строку
        if "=" in raw:
            return _parse_header_cookies(raw) or None
        return None
    if raw is not None:
        return None

    # Copy as cURL из DevTools (Network → Copy as cURL bash/cmd)
    if re.match(r"^\s*curl(?:\.exe)?\s+", text, flags=re.IGNORECASE):
        curl_rows = _extract_curl_cookies(text)
        return curl_rows or None

    lines = [ln for ln in text.splitlines() if ln.strip()]
    # Netscape cookies.txt (7+ колонок через \t)
    if any(ln.count("\t") >= 6 for ln in lines):
        rows = []
        for ln in lines:
            if ln.startswith("#") and not ln.startswith("#HttpOnly_"):
                continue
            parts = ln.split("\t")
            if len(parts) >= 7:
                domain = parts[0].removeprefix("#HttpOnly_").strip()
                item: dict = {"name": parts[5].strip(), "value": parts[6].strip(), "domain": domain}
                path = parts[2].strip()
                if path:
                    item["path"] = path
                try:
                    exp = float(parts[4].strip())
                    if exp > 0:
                        item["expirationDate"] = exp
                except ValueError:
                    pass
                rows.append(item)
        return rows or None

    # Таблица из DevTools → Application → Cookies (2–6 колонок через \t: name, value[, domain[, path]])
    if any(1 <= ln.count("\t") < 6 for ln in lines):
        rows = []
        for ln in lines:
            parts = [p.strip() for p in ln.split("\t")]
            if len(parts) < 2 or not parts[0]:
                continue
            item = {"name": parts[0], "value": parts[1]}
            if len(parts) >= 3 and parts[2] and "." in parts[2]:
                item["domain"] = parts[2]
            if len(parts) >= 4 and parts[3].startswith("/"):
                item["path"] = parts[3]
            rows.append(item)
        if any(r["name"] in REQUIRED_COOKIES for r in rows):
            return rows

    # Header String: одна или несколько строк Cookie: a=b; c=d
    if len(lines) > 1 and all("=" in ln for ln in lines):
        combined = "; ".join(re.sub(r"^cookie\s*:\s*", "", ln.strip(), flags=re.IGNORECASE) for ln in lines)
    else:
        combined = " ".join(lines)
    rows = _parse_header_cookies(combined)
    return rows or None


def resolve_cookies_base(cookies: list, now: float | None = None) -> tuple[str, str]:
    """Выбирает базовый адрес Диска и домен по обязательным кукам.

    Приоритет у yandex.ru; если полный набор обязательных кук есть на другом региональном
    домене Яндекса (yandex.com, .kz, .by, .uz, .com.tr), использует его.
    """
    now = time.time() if now is None else now
    by_tld: dict[str, set[str]] = {tld: set() for tld in YANDEX_COOKIE_DOMAINS}
    generic: set[str] = set()
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if name not in REQUIRED_COOKIES:
            continue
        if not _clean_cookie_value(c.get("value")) or _is_expired(c, now):
            continue
        cbd = cookie_base_domain(c.get("domain"))
        if cbd == "":
            generic.add(name)
        elif cbd in by_tld:
            by_tld[cbd].add(name)
    for tld in YANDEX_COOKIE_DOMAINS:
        if set(REQUIRED_COOKIES) <= (by_tld[tld] | generic):
            return f"https://disk.{tld}", tld
    # Полного набора нет — берём домен, где найдено больше всего обязательных кук (приоритет yandex.ru)
    best_tld = max(YANDEX_COOKIE_DOMAINS, key=lambda t: len(by_tld[t]))
    if by_tld[best_tld]:
        return f"https://disk.{best_tld}", best_tld
    return BASE, "yandex.ru"


def expired_required_cookies(cookies: list, now: float | None = None) -> list[str]:
    """Обязательные куки, у которых в массиве есть только истёкшие записи."""
    now = time.time() if now is None else now
    live: set[str] = set()
    expired: set[str] = set()
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if name not in REQUIRED_COOKIES or not _clean_cookie_value(c.get("value")):
            continue
        if cookie_base_domain(c.get("domain")) is None:
            continue
        if _is_expired(c, now):
            expired.add(name)
        else:
            live.add(name)
    return [n for n in REQUIRED_COOKIES if n in expired and n not in live]


def missing_required_cookies(cookies: list, now: float | None = None) -> list[str]:
    """Какие из обязательных кук отсутствуют (или имеют пустое/истёкшее значение)."""
    now = time.time() if now is None else now
    names: set[str] = set()
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if name not in REQUIRED_COOKIES:
            continue
        if isinstance(c.get("value"), str) and c.get("value").strip("\r\n") and not _is_expired(c, now):
            names.add(name)
    return [n for n in REQUIRED_COOKIES if n not in names]


def foreign_cookie_domains(cookies: list) -> list[str]:
    """Домены, с которых пришли обязательные куки, когда они не с допустимого домена Диска.

    Пусто — всё в порядке (домены yandex.ru/.com/.kz/.by/.uz/.com.tr или домен не указан).
    Иначе человек скопировал вход с постороннего сайта или чужого поддомена (passport.yandex.ru).
    """
    required = [
        c for c in cookies
        if isinstance(c, dict) and c.get("name") in REQUIRED_COOKIES
        and isinstance(c.get("value"), str) and c.get("value").strip("\r\n")
    ]
    if not required:
        return []
    # Проверяем, набирается ли хотя бы на одном домене Яндекса хоть какая-то валидная обязательная кука
    # и нет ли ситуации, когда все обязательные куки с чужих доменов
    by_tld: dict[str, set[str]] = {tld: set() for tld in YANDEX_COOKIE_DOMAINS}
    generic: set[str] = set()
    foreign: set[str] = set()
    for c in required:
        cbd = cookie_base_domain(c.get("domain"))
        if cbd == "":
            generic.add(c["name"])
        elif cbd in by_tld:
            by_tld[cbd].add(c["name"])
        else:
            dom = str(c.get("domain") or "").strip().lstrip(".")
            if dom:
                foreign.add(dom)
    if any(set(REQUIRED_COOKIES) <= (by_tld[tld] | generic) for tld in YANDEX_COOKIE_DOMAINS):
        return []
    if any(by_tld.values()) or generic:
        return []
    return sorted(foreign)


def _cookie_rank(c: dict, base_domain: str, now: float) -> tuple[int, int, int, float]:
    """Приоритет записи при дублях одного имени: живая > disk.<tld> > .<tld> > path='/' > позже истекает."""
    not_expired = 0 if _is_expired(c, now) else 1
    raw_dom = (c.get("domain") or "").strip().lstrip(".").lower() if isinstance(c.get("domain"), str) else ""
    if raw_dom == f"disk.{base_domain}":
        dom_rank = 3
    elif raw_dom == base_domain:
        dom_rank = 2
    else:
        dom_rank = 1
    path = c.get("path") if isinstance(c.get("path"), str) else "/"
    if not path or path == "/":
        path_rank = 2
    elif path.startswith("/client"):
        path_rank = 1
    else:
        path_rank = 0
    exp = _cookie_expiry(c)
    exp_score = exp if exp is not None else float("inf")
    return (not_expired, dom_rank, path_rank, exp_score)


def cookies_dict(raw: list, base_domain: str | None = None, now: float | None = None) -> dict[str, str]:
    """Имя → значение из массива формата Cookie-Editor для целевого домена Яндекса.

    Отфильтровывает чужие сайты и чужие поддомены (passport.yandex.ru, mail.yandex.ru),
    просроченные куки и невалидные для HTTP-заголовков значения (не-latin-1, переносы строк).
    При повторе имени выбирает наиболее подходящую куку (приоритет у yandex.ru, disk.*, path='/').
    """
    now = time.time() if now is None else now
    if base_domain is None:
        _, base_domain = resolve_cookies_base(raw, now)
    best: dict[str, tuple[tuple[int, int, int, float], str]] = {}
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = _clean_cookie_name(c.get("name"))
        value = _clean_cookie_value(c.get("value"))
        if not name or not value:
            continue
        cbd = cookie_base_domain(c.get("domain"))
        if cbd is None or (cbd != "" and cbd != base_domain):
            continue
        if _is_expired(c, now):
            continue
        rank = _cookie_rank(c, base_domain, now)
        prev = best.get(name)
        if prev is None or rank > prev[0]:
            best[name] = (rank, value)
    return {k: v for k, (_, v) in best.items()}


def load_cookies(cookies_file: str | None = None) -> CookieJarDict:
    """Загружает куки из cookies.json (Cookie-Editor JSON, BOM, wrapped dict, Netscape, Header String)."""
    path = cookies_file or COOKIES_FILE
    if not os.path.exists(path):
        raise CookiesFormatError(
            "Нет cookies.json — сначала экспортируй куки из браузера. "
            "См. README раздел про куки."
        )
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    raw = parse_cookies_text(text)
    if not isinstance(raw, list):
        raise CookiesFormatError("cookies.json должен быть массивом (формат Cookie-Editor)")
    base_url, tld = resolve_cookies_base(raw)
    return CookieJarDict(cookies_dict(raw, base_domain=tld), base_url=base_url)


def _base_tld(base_url: str) -> str:
    host = base_url.removeprefix("https://").removeprefix("http://").split("/")[0].lower()
    return host.removeprefix("disk.") or "yandex.ru"


def make_session(cookies: dict[str, str] | None = None,
                 cookies_file: str | None = None,
                 base: str | None = None) -> requests.Session:
    """Создаёт requests.Session с нужными заголовками и куками.

    Если cookies не переданы — загружает из cookies.json.
    """
    if cookies is None:
        cookies = load_cookies(cookies_file)
    base_url = (base or getattr(cookies, "base_url", None) or BASE).rstrip("/")
    tld = _base_tld(base_url)
    s = requests.Session()
    s.yadisk_base = base_url  # type: ignore[attr-defined]
    s.headers.update({
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "ru,en;q=0.9",
        "Origin": base_url, "Referer": f"{base_url}/client/photo",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
    })
    for k, v in cookies.items():
        name = _clean_cookie_name(k)
        value = _clean_cookie_value(v)
        if name and value:
            s.cookies.set(name, value, domain=f".{tld}")
    return s


def _is_captcha_response(r) -> bool:
    """Яндекс показал SmartCaptcha — куки при этом живые, нужен повтор позже."""
    url_low = str(getattr(r, "url", "") or "").lower()
    if "showcaptcha" in url_low:
        return True
    body_low = str(getattr(r, "text", "") or "")[:4000].lower()
    if "smartcaptcha" in body_low or "showcaptcha" in body_low:
        return True
    return getattr(r, "status_code", 0) in (403, 429) and "captcha" in body_low


def _extract_sk_uid(text: str) -> tuple[str | None, str | None]:
    """Ищет sk и uid в HTML (обычный JSON и экранированный JSON)."""
    sk_m = re.search(r'\\?"sk\\?"\s*:\s*\\?"([^"\\]+)\\?"', text)
    uid_m = re.search(r'\\?"uid\\?"\s*:\s*\\?"?(\d+)\\?"?', text)
    return (sk_m.group(1) if sk_m else None, uid_m.group(1) if uid_m else None)


def is_json_auth_error(err) -> bool:
    """JSON-ответ Яндекса с ошибкой авторизации / протухшего sk внутри HTTP 200."""
    low = str(err or "").lower()
    return any(m in low for m in _AUTH_JSON_MARKERS)


def _fetch_page_sk_uid(s: requests.Session, url: str) -> tuple[str | None, str | None]:
    try:
        r = s.get(url, timeout=30)
    except requests.exceptions.RequestException as e:
        raise YandexUnavailableError(f"Яндекс не ответил ({type(e).__name__})") from e
    if _is_captcha_response(r):
        raise YandexUnavailableError("Яндекс попросил проверку на робота (капчу) — куки в порядке, повторю позже")
    if "passport.yandex" in str(getattr(r, "url", "") or "") or r.status_code in (401, 403):
        raise AuthExpiredError("Куки невалидны — нужно обновить cookies.json")
    if r.status_code != 200:
        raise YandexUnavailableError(f"Яндекс ответил HTTP {r.status_code} — сбой на его стороне")
    return _extract_sk_uid(r.text)


def get_sk_uid(s: requests.Session) -> tuple[str, str]:
    """Извлекает sk и uid из HTML-страницы диска (с резервной проверкой /client/photo).

    AuthExpiredError — только когда Яндекс явно не пускает (редирект на вход, 401/403, uid=0).
    Сеть, 50x, 429 и капча дают YandexUnavailableError: во время сбоев Яндекса куки живы,
    и просить человека обновить их — значит гонять его по кругу.
    """
    base_url = getattr(s, "yadisk_base", BASE).rstrip("/")
    sk, uid = _fetch_page_sk_uid(s, f"{base_url}/client/disk")
    if uid == "0":
        raise AuthExpiredError("Куки невалидны (гостевая сессия) — нужно обновить cookies.json")
    if sk and uid:
        return sk, uid
    # Иногда /client/disk отдаёт заглушку без стейта, а /client/photo содержит sk/uid
    sk2, uid2 = _fetch_page_sk_uid(s, f"{base_url}/client/photo")
    if uid2 == "0":
        raise AuthExpiredError("Куки невалидны (гостевая сессия) — нужно обновить cookies.json")
    if sk2 and uid2:
        return sk2, uid2
    raise UnlimError("Не нашёл sk/uid в HTML — Яндекс изменил формат")


def make_cid(uid: str) -> str:
    """Генерирует connection_id для API-запроса."""
    return f"{uid}{int(time.time() * 1000)}{random.randint(100, 999)}"


def api_call(s: requests.Session, sk: str, uid: str, method: str,
             params=None, retries: int = 3):
    """Вызов web-API Яндекс.Диска с retry и обработкой rate limit / капчи."""
    base_url = getattr(s, "yadisk_base", BASE).rstrip("/")
    last_err = None
    for att in range(retries):
        try:
            body = {"sk": sk, "connection_id": make_cid(uid), "apiMethod": method}
            if params is not None:
                body["requestParams"] = params
            t0 = time.monotonic()
            r = s.post(f"{base_url}/models-v2", params={"m": method},
                       json=body, timeout=120)
            log.info("%s: HTTP %s за %d мс%s", method, r.status_code,
                     int((time.monotonic() - t0) * 1000), response_trace(r))
            if _is_captcha_response(r):
                wait = 30 + att * 30
                last_err = f"HTTP {r.status_code}: Яндекс показал капчу"
                log.warning(f"{method} капча (HTTP {r.status_code}) → sleep {wait}с")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    raise RuntimeError(f"{method}: ответ не JSON: {r.text[:200]}")
                # mpfs/list отвечает списком, остальные методы — объектом
                if not isinstance(data, (dict, list)):
                    raise RuntimeError(f"{method}: неожиданный формат ответа: {type(data).__name__}")
                if isinstance(data, dict) and "error" in data and is_json_auth_error(data.get("error")):
                    raise AuthExpiredError(f"Сессия/sk истекли: {str(data.get('error'))[:120]}")
                return data
            if r.status_code in (401, 403):
                raise AuthExpiredError(f"Сессия истекла (HTTP {r.status_code})")
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code == 429:
                wait = 30 + att * 30
                log.warning(f"{method} HTTP 429 (Лимит запросов Яндекса) → sleep {wait}с")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                log.warning(f"{method} HTTP {r.status_code} (Сбой узлов Яндекса / дата-центра) → повтор через {2 ** att}с")
            time.sleep(2 ** att)
        except AuthExpiredError:
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** att)
    raise RuntimeError(f"{method}: {last_err} (Проблема на стороне серверов Яндекса. Новости и статус: https://t.me/davronsv)")


# ── БД безлимита: соединения, транзакции, миграция ──────────────────────────


def connect_unlim_db(db_path: str | None = None, timeout: float = 60) -> sqlite3.Connection:
    """Соединение с unlim_state.db в WAL-режиме, строки — sqlite3.Row."""
    c = sqlite3.connect(db_path or UNLIM_DB_FILE, timeout=timeout)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def unlim_tx(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Короткая транзакция над unlim_state.db: открыть → BEGIN IMMEDIATE → COMMIT → закрыть.

    Несколько воркеров пишут в одну БД параллельно, поэтому каждая операция —
    отдельное короткое соединение и явная транзакция с блокировкой на запись.
    """
    c = connect_unlim_db(db_path)
    try:
        with sqlite_tx(c):
            yield c
    finally:
        c.close()


RESET_FAILED_SQL = (
    "UPDATE unlim_files SET status='pending', attempts=0, error=NULL "
    "WHERE status='failed' OR (status='pending' AND attempts > 0)"
)
RELEASE_IN_PROGRESS_SQL = (
    "UPDATE unlim_files SET status='pending', worker_id=NULL WHERE status='in_progress'"
)


def reset_failed_files(db_path: str | None = None, *, include_in_progress: bool = False) -> int:
    """Возвращает упавшие файлы в очередь (attempts=0). Возвращает число затронутых строк.

    include_in_progress=True — также сбрасывает зависшие in_progress (когда
    точно известно, что воркеры не работают).

    Web UI сбрасывает ошибки до сбора списка, а сбор и воркеры, которые раньше одни
    делали миграцию, ещё не запускались. База из старой версии (без колонки attempts)
    роняла здесь всю ветку безлимита, поэтому схема дотягивается и тут.
    """
    db_path = db_path or UNLIM_DB_FILE
    ensure_unlim_schema(db_path)
    with unlim_tx(db_path) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='unlim_files'").fetchone():
            return 0  # база без списка файлов (воркер запускали до сбора) — сбрасывать нечего
        n = c.execute(RESET_FAILED_SQL).rowcount
        if include_in_progress:
            n += c.execute(RELEASE_IN_PROGRESS_SQL).rowcount
        return n


# Маркер отложенного файла в колонке error (его же считает db_stats в webui.py)
DEFERRED_MARKER = "storage_down | "


def _add_column(c: sqlite3.Connection, ddl: str) -> None:
    """ALTER TABLE, который не падает, если колонку только что добавил соседний воркер."""
    try:
        c.execute(ddl)
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def ensure_unlim_schema(db_path: str | None = None) -> None:
    """Миграция старых БД: колонки attempts, storage_host, preview_path, file_time и dated,
    индекс, таблица unlim_meta.

    Воркеры стартуют одновременно и зовут её параллельно — это безопасно.
    """
    db_path = db_path or UNLIM_DB_FILE
    c = sqlite3.connect(db_path, timeout=60)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(unlim_files)")}
        if cols and "attempts" not in cols:
            _add_column(c, "ALTER TABLE unlim_files ADD COLUMN attempts INTEGER DEFAULT 0")
            cols.add("attempts")
        if cols and "storage_host" not in cols:
            _add_column(c, "ALTER TABLE unlim_files ADD COLUMN storage_host TEXT")
        if cols and "preview_path" not in cols:
            _add_column(c, "ALTER TABLE unlim_files ADD COLUMN preview_path TEXT")
        # file_time — дата для копии (съёмки или загрузки, секунды эпохи); dated — с какой
        # file_time уже сверили дату скачанной копии (file_dates)
        for col in ("file_time", "dated"):
            if cols and col not in cols:
                _add_column(c, f"ALTER TABLE unlim_files ADD COLUMN {col} REAL")
        if {"status", "attempts", "assigned_at"}.issubset(cols):
            c.execute("CREATE INDEX IF NOT EXISTS idx_unlim_pending ON unlim_files(status, attempts, assigned_at)")
        # Общие для всех воркеров значения: пауза по 429, молчащие серверы хранения
        c.execute("CREATE TABLE IF NOT EXISTS unlim_meta (key TEXT PRIMARY KEY, value TEXT)")
        c.execute(VERIFY_PROBLEMS_SCHEMA)
        c.commit()
    finally:
        c.close()


# Старое имя — им пользуются CLI и тесты
ensure_attempts_column = ensure_unlim_schema


def clear_dead_hosts(db_path: str | None = None) -> int:
    """Забывает молчащие серверы хранения: в новом раунде каждый пробуется снова."""
    db_path = db_path or UNLIM_DB_FILE
    if not os.path.exists(db_path):
        return 0
    with unlim_tx(db_path) as c:
        try:
            return c.execute("DELETE FROM unlim_meta WHERE key LIKE 'dead_host:%'").rowcount
        except sqlite3.OperationalError:  # старая БД без unlim_meta
            return 0


def count_deferred(db_path: str | None = None) -> int:
    """Сколько файлов ждут, когда их серверы хранения оживут."""
    db_path = db_path or UNLIM_DB_FILE
    if not os.path.exists(db_path):
        return 0
    c = connect_unlim_db(db_path)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM unlim_files WHERE status='pending' AND error LIKE ?",
            (DEFERRED_MARKER + "%",),
        ).fetchone()[0]
    finally:
        c.close()


def count_failed(db_path: str | None = None) -> int:
    """Сколько файлов так и не скачались (status='failed')."""
    db_path = db_path or UNLIM_DB_FILE
    if not os.path.exists(db_path):
        return 0
    c = connect_unlim_db(db_path)
    try:
        return c.execute("SELECT COUNT(*) FROM unlim_files WHERE status='failed'").fetchone()[0]
    finally:
        c.close()


def save_verify_problems(problems: list[tuple[str, str]], db_path: str | None = None) -> None:
    """Запоминает итог проверки скачанного: пары (file_id, вид). Прошлый итог стирается."""
    db_path = db_path or UNLIM_DB_FILE
    ensure_unlim_schema(db_path)
    with unlim_tx(db_path) as c:
        c.execute("DELETE FROM unlim_verify_problems")
        c.executemany("INSERT OR REPLACE INTO unlim_verify_problems (file_id, kind) VALUES (?, ?)", problems)


def requeue_verify_problems(db_path: str | None = None, kinds: tuple[str, ...] = ("missing", "size", "md5"),
                            download_dir: str | None = None) -> tuple[int, int, int]:
    """«Скачать недостающие» и «Заменить повреждённые» для безлимита: файлы этих видов из итога
    проверки — снова в очередь, попытки обнуляются. Возвращает (вернулось в очередь, отложено
    старых копий, не удалось отложить).

    Испорченную копию не затираем, а перекладываем в <папка скачивания>/_заменённые (utils.set_aside):
    имя освобождается, и воркер положит новую копию туда же. Безлимит кладёт фото на первое свободное
    имя, и по одному пути бывают записаны несколько файлов (verify._not_owners): если хоть один из них
    проверка не назвала пропавшим, файл может быть его — не трогаем, а новая копия ляжет рядом под
    свободным именем. Копия, которую переложить не вышло, остаётся в итоге проверки и в очередь
    не возвращается.
    """
    db_path = db_path or UNLIM_DB_FILE
    if not os.path.exists(db_path):
        return 0, 0, 0
    ensure_unlim_schema(db_path)
    c = connect_unlim_db(db_path)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='unlim_files'").fetchone():
            return 0, 0, 0
        rows = c.execute(
            "SELECT p.file_id, p.kind, f.local_path FROM unlim_verify_problems p "
            "JOIN unlim_files f ON f.file_id = p.file_id WHERE f.status='downloaded'").fetchall()
        lost = {fid for fid, kind, _local in rows if kind == "missing"}
        # Сколько записей, чей файл может лежать по этому пути
        holders = Counter(path_key(local) for fid, local in c.execute(
            "SELECT file_id, local_path FROM unlim_files WHERE status='downloaded' AND local_path IS NOT NULL "
            "AND local_path != ''") if fid not in lost)
    finally:
        c.close()
    back: list[tuple[str]] = []
    moved = kept = 0
    for fid, kind, local in rows:
        if kind not in kinds:
            continue
        if kind != "missing" and download_dir and local and holders[path_key(local)] == 1:
            try:
                if set_aside(download_dir, local):
                    moved += 1
            except OSError:
                kept += 1
                continue
        back.append((fid,))
    with unlim_tx(db_path) as c:
        c.executemany(
            "UPDATE unlim_files SET status='pending', attempts=0, error=NULL, worker_id=NULL, "
            "storage_host=NULL, downloaded_at=NULL WHERE file_id = ? AND status='downloaded'", back)
        c.executemany("DELETE FROM unlim_verify_problems WHERE file_id = ?", back)
        # Файлы этих видов, которые уже не скачаны, — из итога прочь
        marks = ",".join("?" * len(kinds))
        c.execute(f"DELETE FROM unlim_verify_problems WHERE kind IN ({marks}) AND file_id NOT IN "
                  f"(SELECT file_id FROM unlim_files WHERE status='downloaded')", kinds)
    return len(back), moved, kept
