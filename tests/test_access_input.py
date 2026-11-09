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


def test_cookies_from_another_yandex_site_are_refused():
    rep = webapp.access.cookies_report(cookie_rows(".yandex.com"))
    assert not rep["ok"] and "yandex.com" in rep["msg"] and "disk.yandex.ru" in rep["msg"]
    assert webapp.access.cookies_report(cookie_rows(".yandex.ru"))["ok"]
    assert webapp.access.cookies_report(cookie_rows("disk.yandex.ru"))["ok"]


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
