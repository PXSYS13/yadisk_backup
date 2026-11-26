"""Парсинг cookies.json, HTTP-сессия disk.yandex.* и вызовы внутреннего web-API models-v2."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from uploader import paths
from uploader.utils import CancelledError, redact_urls

log = logging.getLogger("unlim_uploader")

BASE = "https://disk.yandex.ru"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
YANDEX_COOKIE_DOMAINS = (
    "yandex.ru",
    "yandex.com",
    "yandex.kz",
    "yandex.by",
    "yandex.uz",
    "yandex.com.tr",
)
REQUIRED_COOKIES = ("Session_id", "yandexuid", "sessionid2")
_AUTH_JSON_MARKERS = (
    "bad_sk",
    "invalid_sk",
    "unauthorized",
    "http_401",
    "http_403",
    "not_authorized",
    "auth_error",
)


class UnlimApiError(Exception):
    """Базовая ошибка взаимодействия с web-API Яндекс.Диска."""


class AuthExpiredError(UnlimApiError):
    """Куки невалидны или истёк срок сессии."""


class YandexUnavailableError(UnlimApiError):
    """Яндекс временно недоступен (сеть, 50x, 429, капча) — куки при этом живые."""


class CaptchaError(YandexUnavailableError):
    """Яндекс показал SmartCaptcha — нужна пауза."""


class RateLimitError(YandexUnavailableError):
    """Яндекс вернул HTTP 429 Too Many Requests."""


class CookiesFormatError(UnlimApiError):
    """Файл cookies.json отсутствует или имеет неверный формат."""


class CookieJarDict(dict):
    """Словарь кук с определённым базовым доменом Диска."""

    def __init__(self, *args, base_url: str = BASE, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_url = base_url


@dataclass
class RawApiResponse:
    status_code: int
    data: Any
    text: str
    headers: dict[str, str]
    request_id: str = ""


def _cookie_expiry(c: dict) -> float | None:
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
        return val / 1000.0 if val > 1e12 else val
    return None


def _is_expired(c: dict, now: float) -> bool:
    exp = _cookie_expiry(c)
    return exp is not None and exp < now


def _clean_cookie_value(val: Any) -> str | None:
    if not isinstance(val, str):
        return None
    v = val.strip("\r\n")
    if not v or any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
        return None
    try:
        v.encode("latin-1")
    except UnicodeEncodeError:
        return None
    return v


def _clean_cookie_name(name: Any) -> str | None:
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


def cookie_base_domain(domain: Any) -> str | None:
    if domain is None or domain == "":
        return ""
    if not isinstance(domain, str):
        return None
    d = domain.strip().lstrip(".").lower()
    if not d:
        return ""
    for tld in YANDEX_COOKIE_DOMAINS:
        if d in (tld, f"disk.{tld}", f"disk.360.{tld}"):
            return tld
    return None


def _parse_header_cookies(header: str) -> list[dict]:
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
    joined = re.sub(r"[\\^]\r?\n", " ", text)
    if '^"' in joined or "^|" in joined:
        joined = re.sub(r"\^([\"&|%<>^!()])", r"\1", joined)
    chunks: list[str] = []
    for m in re.finditer(
        r"""(?:-H|--header)\s+(?:\$?'([^']*)'|"([^"]*)")""",
        joined,
        flags=re.IGNORECASE,
    ):
        hdr = (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()
        if re.match(r"^cookie\s*:", hdr, flags=re.IGNORECASE):
            chunks.append(hdr)
    for m in re.finditer(r"""(?:-b|--cookie)\s+(?:\$?'([^']*)'|"([^"]*)"|(\S+))""", joined):
        val = next((g for g in m.groups() if g is not None), "").strip()
        if "=" in val:
            chunks.append(val)
    rows: list[dict] = []
    for ch in chunks:
        rows.extend(_parse_header_cookies(ch))
    return rows


def parse_cookies_text(text: str) -> list | None:
    """Поддерживает Cookie-Editor JSON, плоский JSON, Copy as cURL, Netscape cookies.txt, DevTools, Header String."""
    text = (text or "").strip().lstrip("\ufeff").strip()
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
        if any(k in raw for k in REQUIRED_COOKIES):
            rows = [
                {"name": str(k), "value": str(v)}
                for k, v in raw.items()
                if isinstance(k, str)
                and isinstance(v, (str, int, float))
                and not isinstance(v, bool)
            ]
            return rows or None
        return None
    if isinstance(raw, str):
        return _parse_header_cookies(raw) or None if "=" in raw else None
    if raw is not None:
        return None

    if re.match(r"^\s*curl(?:\.exe)?\s+", text, flags=re.IGNORECASE):
        return _extract_curl_cookies(text) or None

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if any(ln.count("\t") >= 6 for ln in lines):
        rows = []
        for ln in lines:
            if ln.startswith("#") and not ln.startswith("#HttpOnly_"):
                continue
            parts = ln.split("\t")
            if len(parts) >= 7:
                domain = parts[0].removeprefix("#HttpOnly_").strip()
                item: dict[str, Any] = {
                    "name": parts[5].strip(),
                    "value": parts[6].strip(),
                    "domain": domain,
                }
                if parts[2].strip():
                    item["path"] = parts[2].strip()
                try:
                    exp = float(parts[4].strip())
                    if exp > 0:
                        item["expirationDate"] = exp
                except ValueError:
                    pass
                rows.append(item)
        return rows or None

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

    if len(lines) > 1 and all("=" in ln for ln in lines):
        combined = "; ".join(
            re.sub(r"^cookie\s*:\s*", "", ln.strip(), flags=re.IGNORECASE) for ln in lines
        )
    else:
        combined = " ".join(lines)
    return _parse_header_cookies(combined) or None


def resolve_cookies_base(cookies: list, now: float | None = None) -> tuple[str, str]:
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
    best_tld = max(YANDEX_COOKIE_DOMAINS, key=lambda t: len(by_tld[t]))
    if by_tld[best_tld]:
        return f"https://disk.{best_tld}", best_tld
    return BASE, "yandex.ru"


def missing_required_cookies(cookies: list, now: float | None = None) -> list[str]:
    """Проверяет наличие всех REQUIRED_COOKIES на ОДНОМ целевом домене Яндекса."""
    now = time.time() if now is None else now
    _, base_domain = resolve_cookies_base(cookies, now)
    jar = cookies_dict(cookies, base_domain=base_domain, now=now)
    return [n for n in REQUIRED_COOKIES if n not in jar]


def filter_yandex_cookies(raw: list, now: float | None = None) -> list[dict[str, Any]]:
    """Оставляет только живые и валидные куки доменов Яндекс.Диска (сторонние сайты не сохраняются)."""
    now = time.time() if now is None else now
    _, base_domain = resolve_cookies_base(raw, now)
    kept: list[dict[str, Any]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = _clean_cookie_name(c.get("name"))
        value = _clean_cookie_value(c.get("value"))
        if not name or not value or _is_expired(c, now):
            continue
        cbd = cookie_base_domain(c.get("domain"))
        if cbd is None or (cbd != "" and cbd != base_domain):
            continue
        item: dict[str, Any] = {"name": name, "value": value}
        dom = c.get("domain")
        if isinstance(dom, str) and dom.strip():
            item["domain"] = dom.strip()
        path = c.get("path")
        if isinstance(path, str) and path.strip():
            item["path"] = path.strip()
        exp = _cookie_expiry(c)
        if exp is not None:
            item["expirationDate"] = exp
        kept.append(item)
    return kept


def _cookie_rank(c: dict, base_domain: str, now: float) -> tuple[int, int, int, float]:
    not_expired = 0 if _is_expired(c, now) else 1
    raw_dom = (
        (c.get("domain") or "").strip().lstrip(".").lower()
        if isinstance(c.get("domain"), str)
        else ""
    )
    dom_rank = 3 if raw_dom == f"disk.{base_domain}" else 2 if raw_dom == base_domain else 1
    path = c.get("path") if isinstance(c.get("path"), str) else "/"
    path_rank = 2 if (not path or path == "/") else 1 if path.startswith("/client") else 0
    exp = _cookie_expiry(c)
    return (not_expired, dom_rank, path_rank, exp if exp is not None else float("inf"))


def cookies_dict(
    raw: list, base_domain: str | None = None, now: float | None = None
) -> dict[str, str]:
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


def build_cookie_jar(raw: list) -> CookieJarDict:
    """Строит проверенный CookieJarDict из разобранного списка кук (без записи на диск)."""
    missing = missing_required_cookies(raw)
    if missing:
        raise CookiesFormatError(
            f"В куках не хватает обязательных записей Яндекса: {', '.join(missing)}"
        )
    base_url, tld = resolve_cookies_base(raw)
    return CookieJarDict(cookies_dict(raw, base_domain=tld), base_url=base_url)


def load_cookies(cookies_file: str | os.PathLike | None = None) -> CookieJarDict:
    path = os.fspath(cookies_file or paths.COOKIES_FILE)
    if not os.path.exists(path):
        raise CookiesFormatError("Нет cookies.json — сначала сохраните данные входа из браузера.")
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise CookiesFormatError(f"Не удалось прочитать cookies.json: {e}") from None
    raw = parse_cookies_text(text)
    if not isinstance(raw, list):
        raise CookiesFormatError(
            "Не удалось распознать куки — вставьте экспорт из Cookie-Editor или DevTools."
        )
    return build_cookie_jar(raw)


def _base_tld(base_url: str) -> str:
    host = base_url.removeprefix("https://").removeprefix("http://").split("/")[0].lower()
    return host.removeprefix("disk.") or "yandex.ru"


def make_session(
    cookies: dict[str, str] | None = None,
    cookies_file: str | os.PathLike | None = None,
    base: str | None = None,
) -> requests.Session:
    if cookies is None:
        cookies = load_cookies(cookies_file)
    base_url = (base or getattr(cookies, "base_url", None) or BASE).rstrip("/")
    tld = _base_tld(base_url)
    s = requests.Session()
    s.yadisk_base = base_url  # type: ignore[attr-defined]
    s.yadisk_seed_cookies = dict(cookies)  # type: ignore[attr-defined]
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "ru,en;q=0.9",
            "Origin": base_url,
            "Referer": f"{base_url}/client/photo",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
        }
    )
    for k, v in cookies.items():
        name = _clean_cookie_name(k)
        value = _clean_cookie_value(v)
        if name and value:
            s.cookies.set(name, value, domain=f".{tld}", secure=True)
    return s


def is_captcha_response(r: requests.Response) -> bool:
    url_low = str(getattr(r, "url", "") or "").lower()
    if "showcaptcha" in url_low:
        return True
    status = getattr(r, "status_code", 0)
    headers = getattr(r, "headers", None) or {}
    ctype = str(headers.get("Content-Type") or headers.get("content-type") or "").lower()
    if status == 200 and "application/json" in ctype:
        try:
            data = r.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            if data.get("captcha") or data.get("showcaptcha") or data.get("type") == "captcha":
                return True
            err = data.get("error")
            if err and "captcha" in str(err).lower():
                return True
        return False
    body_low = str(getattr(r, "text", "") or "")[:4000].lower()
    if "smartcaptcha" in body_low or "showcaptcha" in body_low:
        return True
    return status in (403, 429) and "captcha" in body_low


def is_json_auth_error(err: Any) -> bool:
    low = str(err or "").lower()
    return any(m in low for m in _AUTH_JSON_MARKERS)


def _extract_sk_uid(text: str) -> tuple[str | None, str | None]:
    sk_m = re.search(r'\\?"sk\\?"\s*:\s*\\?"([^"\\]+)\\?"', text)
    uid_m = re.search(r'\\?"uid\\?"\s*:\s*\\?"?(\d+)\\?"?', text)
    return (sk_m.group(1) if sk_m else None, uid_m.group(1) if uid_m else None)


def _fetch_page_sk_uid(s: requests.Session, url: str) -> tuple[str | None, str | None]:
    try:
        r = s.get(url, timeout=30)
    except requests.exceptions.RequestException as e:
        raise YandexUnavailableError(
            f"Яндекс не ответил ({type(e).__name__}: {redact_urls(str(e))[:140]})"
        ) from None
    if is_captcha_response(r):
        raise CaptchaError("Яндекс попросил проверку на робота (SmartCaptcha) — повторю позже")
    if "passport.yandex" in str(getattr(r, "url", "") or "").lower() or r.status_code in (401, 403):
        raise AuthExpiredError("Куки невалидны — нужно обновить cookies.json")
    if r.status_code != 200:
        raise YandexUnavailableError(f"Яндекс ответил HTTP {r.status_code}")
    return _extract_sk_uid(r.text)


def get_sk_uid(s: requests.Session) -> tuple[str, str]:
    base_url = getattr(s, "yadisk_base", BASE).rstrip("/")
    for suffix in ("/client/disk", "/client/photo"):
        sk, uid = _fetch_page_sk_uid(s, f"{base_url}{suffix}")
        if uid == "0":
            raise AuthExpiredError("Куки невалидны (гостевая сессия) — обновите cookies.json")
        if sk and uid:
            return sk, uid
    raise UnlimApiError("Не удалось найти sk/uid на странице Яндекс.Диска")


def make_cid(uid: str) -> str:
    return f"{uid}{int(time.time() * 1000)}{random.randint(100, 999)}"


def _sleep_interruptible(seconds: float, stop_event: threading.Event | None) -> None:
    if stop_event is not None:
        if stop_event.wait(seconds):
            raise CancelledError("Запрос остановлен пользователем")
    else:
        time.sleep(seconds)


def raw_api_call(
    s: requests.Session,
    sk: str,
    uid: str,
    method: str,
    params: Any = None,
    *,
    timeout: float | tuple[float, float] = (15.0, 60.0),
) -> RawApiResponse:
    """Единичный POST-вызов /models-v2 без скрытого ретрая: возвращает статус и разобранный JSON."""
    base_url = getattr(s, "yadisk_base", BASE).rstrip("/")
    body: dict[str, Any] = {"sk": sk, "connection_id": make_cid(uid), "apiMethod": method}
    if params is not None:
        body["requestParams"] = params
    try:
        r = s.post(f"{base_url}/models-v2", params={"m": method}, json=body, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise YandexUnavailableError(
            f"{method}: сетевая ошибка ({type(e).__name__}: {redact_urls(str(e))[:140]})"
        ) from None

    req_id = r.headers.get("x-request-id") or r.headers.get("X-Yandex-Req-Id") or ""
    if is_captcha_response(r):
        raise CaptchaError(f"{method}: SmartCaptcha (HTTP {r.status_code})")
    if r.status_code == 429:
        raise RateLimitError(f"{method}: HTTP 429 (лимит запросов Яндекса)")
    if "passport.yandex" in str(getattr(r, "url", "") or "").lower() or r.status_code in (401, 403):
        raise AuthExpiredError(f"{method}: сессия истекла (HTTP {r.status_code})")

    data: Any = None
    try:
        data = r.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and "error" in data and is_json_auth_error(data.get("error")):
        raise AuthExpiredError(f"{method}: сессия/sk истекли ({str(data.get('error'))[:120]})")
    return RawApiResponse(
        status_code=r.status_code,
        data=data,
        text=r.text,
        headers=dict(r.headers),
        request_id=req_id,
    )


def api_call(
    s: requests.Session,
    sk: str,
    uid: str,
    method: str,
    params: Any = None,
    *,
    retries: int = 3,
    timeout: float | tuple[float, float] = (15.0, 90.0),
    stop_event: threading.Event | None = None,
) -> Any:
    """Вызов /models-v2 с ретраями на 50x/сеть/429/капчу для методов, где ожидается HTTP 200 JSON."""
    last_exc: Exception | None = None
    for att in range(max(1, retries)):
        if stop_event is not None and stop_event.is_set():
            raise CancelledError("Запрос остановлен пользователем")
        try:
            resp = raw_api_call(s, sk, uid, method, params, timeout=timeout)
            if resp.status_code == 200 and isinstance(resp.data, (dict, list)):
                if isinstance(resp.data, dict) and resp.data.get("error"):
                    raise UnlimApiError(
                        f"{method}: ошибка API: {redact_urls(str(resp.data.get('error')))[:180]}"
                    )
                return resp.data
            if resp.status_code >= 500:
                last_exc = YandexUnavailableError(
                    f"{method}: HTTP {resp.status_code}: {redact_urls(resp.text)[:180]}"
                )
            else:
                raise UnlimApiError(
                    f"{method}: HTTP {resp.status_code}: {redact_urls(resp.text)[:180]}"
                )
        except (AuthExpiredError, CancelledError):
            raise
        except (CaptchaError, RateLimitError) as e:
            last_exc = e
            if att + 1 < retries:
                _sleep_interruptible(20 + att * 20, stop_event)
            continue
        except YandexUnavailableError as e:
            last_exc = e
        if att + 1 < retries:
            _sleep_interruptible(2**att, stop_event)
    assert last_exc is not None
    raise last_exc
