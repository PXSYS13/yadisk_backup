"""Что люди вставляют в «Доступ»: данные входа для Безлимита и ключ к Диску.

Жалоба из канала (ноябрь 2026): вставил куки, нажал «Сохранить доступ», а доступа нет.
Сохранение раньше смотрело только на имена кук и не спрашивало Яндекс.
"""

import json

import pytest
from fastapi.testclient import TestClient

import webapp.access
import webapp.paths
import webui
from unlim_common import AuthExpiredError, YandexUnavailableError, cookies_dict

REQUIRED = ("Session_id", "yandexuid", "sessionid2")


def cookie_rows(domain=".yandex.ru"):
    return [{"name": n, "value": f"v-{n}", "domain": domain} for n in REQUIRED]


@pytest.fixture
def client():
    return TestClient(webui.app, base_url="http://127.0.0.1:8000")


def test_parse_json_array_and_wrapped_object():
    rows = cookie_rows()
    assert webapp.access.parse_cookies_text(json.dumps(rows)) == rows
    assert webapp.access.parse_cookies_text(json.dumps({"cookies": rows})) == rows
    # Блокнот пишет BOM в начало файла
    assert webapp.access.parse_cookies_text(chr(0xFEFF) + json.dumps(rows)) == rows


def test_parse_header_string():
    rows = webapp.access.parse_cookies_text("Cookie: Session_id=a=b; yandexuid=2;sessionid2=3")
    assert rows == [{"name": "Session_id", "value": "a=b"}, {"name": "yandexuid", "value": "2"},
                    {"name": "sessionid2", "value": "3"}]


def test_parse_netscape_cookies_txt():
    text = "# Netscape HTTP Cookie File\n" + "\n".join(
        f"{'#HttpOnly_' if n == 'Session_id' else ''}.yandex.ru\tTRUE\t/\tTRUE\t0\t{n}\tv-{n}" for n in REQUIRED)
    rows = webapp.access.parse_cookies_text(text)
    assert [(r["name"], r["value"], r["domain"]) for r in rows] == [(n, f"v-{n}", ".yandex.ru") for n in REQUIRED]


def test_parse_rejects_text_that_is_not_cookies():
    assert webapp.access.parse_cookies_text("") is None
    assert webapp.access.parse_cookies_text('"просто строка"') is None
    assert webapp.access.parse_cookies_text("привет как дела") is None


def test_cookies_regional_yandex_domains_accepted_and_foreign_refused():
    # Региональные домены Яндекса (куда браузер редиректит за пределами РФ) принимаются
    for domain, expected_base in (
        (".yandex.ru", "https://disk.yandex.ru"),
        ("disk.yandex.ru", "https://disk.yandex.ru"),
        (".yandex.com", "https://disk.yandex.com"),
        (".yandex.kz", "https://disk.yandex.kz"),
        (".yandex.by", "https://disk.yandex.by"),
        (".yandex.uz", "https://disk.yandex.uz"),
        (".yandex.com.tr", "https://disk.yandex.com.tr"),
    ):
        rep = webapp.access.cookies_report(cookie_rows(domain))
        assert rep["ok"], domain
        assert rep["base"] == expected_base, domain
    # Чужие сайты и чужие поддомены Яндекса (passport.yandex.ru) отклоняются
    for bad in (".google.com", "passport.yandex.ru", "mail.yandex.ru"):
        rep = webapp.access.cookies_report(cookie_rows(bad))
        assert not rep["ok"], bad
        assert bad.lstrip(".") in rep["msg"], rep["msg"]


def test_parse_flat_json_dict_curl_and_devtools_table():
    # Плоский JSON-словарь
    flat = json.dumps({"Session_id": "s1", "yandexuid": "u1", "sessionid2": "s2"})
    rows = webapp.access.parse_cookies_text(flat)
    assert {r["name"]: r["value"] for r in rows} == {"Session_id": "s1", "yandexuid": "u1", "sessionid2": "s2"}

    # Copy as cURL (POSIX с переносом строки и -b)
    curl_posix = (
        "curl 'https://disk.yandex.ru/client/photo' \\\n"
        "  -H 'accept: */*' \\\n"
        "  -H 'cookie: Session_id=abc; yandexuid=123; sessionid2=def'"
    )
    rows = webapp.access.parse_cookies_text(curl_posix)
    assert {r["name"]: r["value"] for r in rows} == {"Session_id": "abc", "yandexuid": "123", "sessionid2": "def"}

    curl_b = "curl https://disk.yandex.ru/client/disk -b 'Session_id=x; yandexuid=y; sessionid2=z'"
    rows = webapp.access.parse_cookies_text(curl_b)
    assert {r["name"]: r["value"] for r in rows} == {"Session_id": "x", "yandexuid": "y", "sessionid2": "z"}

    # Copy as cURL (Windows cmd с ^)
    curl_win = (
        'curl ^"https://disk.yandex.ru/client/photo^" ^\n'
        '  -H ^"cookie: Session_id=w1; yandexuid=w2; sessionid2=w3^"'
    )
    rows = webapp.access.parse_cookies_text(curl_win)
    assert {r["name"]: r["value"] for r in rows} == {"Session_id": "w1", "yandexuid": "w2", "sessionid2": "w3"}

    # Таблица из DevTools -> Application -> Cookies (4-5 колонок через таб)
    devtools = (
        "Session_id\td1\t.yandex.ru\t/\t2030-01-01\n"
        "yandexuid\td2\t.yandex.ru\t/\t2030-01-01\n"
        "sessionid2\td3\t.yandex.ru\t/\t2030-01-01\n"
    )
    rows = webapp.access.parse_cookies_text(devtools)
    assert {r["name"]: (r["value"], r["domain"]) for r in rows} == {
        "Session_id": ("d1", ".yandex.ru"),
        "yandexuid": ("d2", ".yandex.ru"),
        "sessionid2": ("d3", ".yandex.ru"),
    }

    # Header String в кавычках
    quoted = '"Session_id=q1; yandexuid=q2; sessionid2=q3"'
    rows = webapp.access.parse_cookies_text(quoted)
    assert {r["name"]: r["value"] for r in rows} == {"Session_id": "q1", "yandexuid": "q2", "sessionid2": "q3"}


def test_empty_and_expired_required_cookies_are_rejected():
    # Пустое value у обязательной куки
    empty_val = [{"name": "Session_id", "value": "", "domain": ".yandex.ru"},
                 {"name": "yandexuid", "value": "1", "domain": ".yandex.ru"},
                 {"name": "sessionid2", "value": "2", "domain": ".yandex.ru"}]
    rep = webapp.access.cookies_report(empty_val)
    assert not rep["ok"] and "Session_id" in rep["msg"]

    # Просроченная обязательная кука (expirationDate в прошлом)
    expired = [{"name": n, "value": f"v-{n}", "domain": ".yandex.ru", "expirationDate": 1000000} for n in REQUIRED]
    rep = webapp.access.cookies_report(expired)
    assert not rep["ok"] and "истёк" in rep["msg"].lower()

    # Есть просроченный дубль и живая кука — побеждает живая
    mixed = expired + [{"name": n, "value": f"live-{n}", "domain": ".yandex.ru", "expirationDate": 4102444800} for n in REQUIRED]
    rep = webapp.access.cookies_report(mixed)
    assert rep["ok"]
    assert cookies_dict(mixed)["Session_id"] == "live-Session_id"

    # Нет HttpOnly кук (скопировали document.cookie из консоли) — понятная подсказка
    only_js = [{"name": "yandexuid", "value": "123", "domain": ".yandex.ru"}]
    rep = webapp.access.cookies_report(only_js)
    assert not rep["ok"] and "Cookie-Editor" in rep["msg"] and "Session_id" in rep["msg"]

    # Обязательные куки разбросаны по разным доменам — должно отклоняться с указанием куки
    split_domains = [
        {"name": "Session_id", "value": "s1", "domain": ".yandex.ru"},
        {"name": "sessionid2", "value": "s2", "domain": ".yandex.ru"},
        {"name": "yandexuid", "value": "u1", "domain": ".yandex.com"},
    ]
    rep = webapp.access.cookies_report(split_domains)
    assert not rep["ok"] and "yandexuid" in rep["msg"]

    # Обязательная кука с не-latin1 символами — отклоняется
    bad_chars = [
        {"name": "Session_id", "value": "кириллица", "domain": ".yandex.ru"},
        {"name": "yandexuid", "value": "u1", "domain": ".yandex.ru"},
        {"name": "sessionid2", "value": "s2", "domain": ".yandex.ru"},
    ]
    rep = webapp.access.cookies_report(bad_chars)
    assert not rep["ok"] and "Session_id" in rep["msg"]


def test_cookies_dict_ignores_foreign_subdomains_non_latin1_and_ranks_path():
    raw = [
        {"name": "Session_id", "value": "good", "domain": ".yandex.ru", "path": "/"},
        {"name": "Session_id", "value": "narrow", "domain": ".yandex.ru", "path": "/client/special"},
        {"name": "Session_id", "value": "passport", "domain": "passport.yandex.ru", "path": "/"},
        {"name": "yandexuid", "value": "disk", "domain": "disk.yandex.ru", "path": "/"},
        {"name": "yandexuid", "value": "root", "domain": ".yandex.ru", "path": "/"},
        {"name": "foreign", "value": "evil", "domain": ".google.com", "path": "/"},
        {"name": "cyrillic", "value": "привет", "domain": ".yandex.ru", "path": "/"},
        {"name": "newline", "value": "a\nb", "domain": ".yandex.ru", "path": "/"},
    ]
    d = cookies_dict(raw)
    assert d["Session_id"] == "good"
    assert d["yandexuid"] == "disk"
    assert "foreign" not in d
    assert "cyrillic" not in d
    assert "newline" not in d


def test_save_cookies_retries_permission_error_on_replace(client, monkeypatch):
    import os as _os

    real_replace = _os.replace
    attempts = [0]

    def flaky_replace(src, dst):
        attempts[0] += 1
        if attempts[0] < 3:
            raise PermissionError("locked by antivirus")
        return real_replace(src, dst)

    monkeypatch.setattr(webapp.access.os, "replace", flaky_replace)
    monkeypatch.setattr(webapp.access.time, "sleep", lambda _: None)
    r = client.post("/api/cookies", json={"text": json.dumps(cookie_rows())})
    assert r.status_code == 200, r.text
    assert attempts[0] == 3
    assert webapp.paths.COOKIES_FILE.exists()


def test_same_name_prefers_yandex_ru_domain():
    raw = [{"name": "Session_id", "value": "ru", "domain": ".yandex.ru"},
           {"name": "Session_id", "value": "com", "domain": ".yandex.com"}]
    assert cookies_dict(raw)["Session_id"] == "ru"
    assert cookies_dict(list(reversed(raw)))["Session_id"] == "ru"


def test_save_refuses_cookies_yandex_rejects(client, monkeypatch):
    def rejected(s):
        raise AuthExpiredError("passport")

    monkeypatch.setattr(webapp.access, "get_sk_uid", rejected)
    r = client.post("/api/cookies", json={"text": json.dumps(cookie_rows())})
    assert r.status_code == 400 and "Яндекс не пустил" in r.json()["detail"]
    assert not webapp.paths.COOKIES_FILE.exists()


def test_save_keeps_cookies_when_yandex_is_silent(client, monkeypatch):
    def silent(s):
        raise YandexUnavailableError("timeout")

    monkeypatch.setattr(webapp.access, "get_sk_uid", silent)
    r = client.post("/api/cookies", json={"text": json.dumps(cookie_rows())})
    assert r.status_code == 200 and r.json()["live"]["unreachable"]
    assert webapp.paths.COOKIES_FILE.exists()


def test_save_header_string_and_report_saved_time(client):
    r = client.post("/api/cookies", json={"text": "Session_id=1; yandexuid=2; sessionid2=3"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["check"]["ok"] and body["check"]["saved_at"] > 0 and body["live"]["ok"]
    saved = json.loads(webapp.paths.COOKIES_FILE.read_text(encoding="utf-8"))
    assert [c["name"] for c in saved] == list(REQUIRED)


def test_save_garbage_text_explains_what_to_do(client):
    r = client.post("/api/cookies", json={"text": "не то"})
    assert r.status_code == 400 and "Export as JSON" in r.json()["detail"]


@pytest.mark.parametrize("raw", [
    "y0_AgAAAAB",
    "  y0_AgAAAAB\n",
    "OAuth y0_AgAAAAB",
    "bearer y0_AgAAAAB",
    "«y0_AgAAAAB»",
    "y0_AgAA\nAAB",
    "https://oauth.yandex.ru/verification_code#access_token=y0_AgAAAAB&token_type=bearer&expires_in=31536000",
])
def test_normalize_token_takes_the_key_out_of_what_people_paste(raw):
    assert webapp.access.normalize_token(raw) == "y0_AgAAAAB"


def test_token_endpoints_use_the_normalized_key(client, monkeypatch):
    seen = []
    monkeypatch.setattr(webapp.access, "check_token", lambda t: seen.append(t) or {"ok": True, "login": "x"})
    monkeypatch.setattr(webapp.access, "_resume_branch", lambda source: None)
    client.post("/api/config/token/check", json={"token": "OAuth y0_K\n"})
    client.post("/api/config/token", json={"token": "#access_token=y0_K&token_type=bearer"})
    assert seen == ["y0_K", "y0_K"]
