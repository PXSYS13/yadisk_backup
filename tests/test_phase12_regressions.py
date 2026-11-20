"""Регрессионные тесты исправлений фаз 1–2 (ядро, безлимит, публичные ссылки, веб-интерфейс, безопасность)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import storage_fetch
import unlim_common
import utils
import webapp.access
import webapp.control
import webapp.file_list
import webapp.guide
import webapp.logs
import webapp.paths
import webapp.rescue
import webapp.tasks
import webui
import yandex_health


@pytest.fixture()
def client():
    return TestClient(webui.app, headers={"Host": "localhost:8000"})


# ── storage_fetch ────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code=200, headers=None, chunks=(), url="https://downloader.disk.yandex.ru/disk/x"):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.url = url
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=65536):
        yield from self._chunks

    def close(self):
        self.closed = True


class _RedirectSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_storage_fetch_rejects_nonzero_body_when_expected_size_is_zero(tmp_path):
    sess = _RedirectSession([
        _FakeResp(200, headers={"Content-Length": "5"}, chunks=[b"hello"]),
    ])
    dst = tmp_path / "zero.bin"
    _written, _host, failure = storage_fetch.fetch_to_file(
        sess,
        "https://downloader.disk.yandex.ru/disk/z",
        str(dst),
        expected_size=0,
    )
    assert failure is not None and failure.kind == "bad_file"


def test_storage_fetch_blocks_non_http_redirect_scheme(tmp_path):
    sess = _RedirectSession([
        _FakeResp(302, headers={"Location": "file:///etc/passwd"}),
    ])
    _written, _host, failure = storage_fetch.fetch_to_file(
        sess,
        "https://downloader.disk.yandex.ru/disk/a",
        str(tmp_path / "a.bin"),
    )
    assert failure is not None and failure.kind == "refused"
    assert len(sess.calls) == 1


def test_storage_fetch_blocks_http_downgrade_redirect(tmp_path):
    sess = _RedirectSession([
        _FakeResp(302, headers={"Location": "http://s1.storage.yandex.net/file"}),
    ])
    _written, _host, failure = storage_fetch.fetch_to_file(
        sess,
        "https://downloader.disk.yandex.ru/disk/a",
        str(tmp_path / "a.bin"),
    )
    assert failure is not None and failure.kind == "refused"


def test_storage_fetch_skips_dead_redirect_target(tmp_path):
    sess = _RedirectSession([
        _FakeResp(302, headers={"Location": "https://dead.storage.yandex.net/file"}),
    ])
    _written, host, failure = storage_fetch.fetch_to_file(
        sess,
        "https://downloader.disk.yandex.ru/disk/a",
        str(tmp_path / "a.bin"),
        host_is_dead=lambda h: h == "dead.storage.yandex.net",
    )
    assert failure is not None and failure.kind == "storage_skip"
    assert host == "dead.storage.yandex.net"
    assert len(sess.calls) == 1


# ── utils ────────────────────────────────────────────────────────────────────


def test_scrub_urls_handles_parentheses_in_filename_and_protocol_relative():
    msg = (
        "err https://downloader.disk.yandex.ru/disk/abc?filename=photo(1).jpg&rtoken=TOPSECRET "
        "and //s123.storage.yandex.net/rdisk/xyz?uid=999&hash=SECRET2"
    )
    scrubbed = utils.scrub_urls(msg)
    assert "TOPSECRET" not in scrubbed
    assert "SECRET2" not in scrubbed
    assert "uid=999" not in scrubbed
    assert "downloader.disk.yandex.ru" in scrubbed
    assert "s123.storage.yandex.net" in scrubbed


def test_path_key_strips_windows_long_prefix():
    assert utils.path_key(r"\\?\C:\Users\Test\file.jpg") == utils.path_key(r"C:\Users\Test\file.jpg")


def test_classify_yandex_error_boundaries_and_local_file():
    d1 = utils.classify_yandex_error("local_file | filename too long")
    assert d1["category"] == "local_file" and not d1["is_yandex_server"]
    d2 = utils.classify_yandex_error("failed on port 4040 timeout")
    assert d2["category"] != "not_found"


# ── unlim_common ─────────────────────────────────────────────────────────────


def test_curl_cookie_parser_unescapes_windows_caret_pipe_and_bash_dollar_quote():
    win_curl = (
        'curl ^"https://disk.yandex.ru/client/photo^" ^\n'
        '  -H ^"cookie: Session_id=3:1700^|abc^&x; yandexuid=123; sessionid2=3:456^|def^"'
    )
    rows = unlim_common.parse_cookies_text(win_curl)
    by_name = {r["name"]: r["value"] for r in rows}
    assert by_name["Session_id"] == "3:1700|abc&x"
    assert by_name["sessionid2"] == "3:456|def"

    bash_curl = "curl 'https://disk.yandex.ru/client/photo' -H $'cookie: Session_id=s1; yandexuid=2; sessionid2=s3'"
    rows2 = unlim_common.parse_cookies_text(bash_curl)
    assert {r["name"]: r["value"] for r in rows2} == {"Session_id": "s1", "yandexuid": "2", "sessionid2": "s3"}


def test_is_captcha_response_ignores_json_filename_and_flags_html_captcha():
    class _R:
        def __init__(self, status_code, url, ct, text):
            self.status_code = status_code
            self.url = url
            self.headers = {"Content-Type": ct}
            self.text = text

    json_ok = _R(200, "https://disk.yandex.ru/models", "application/json", '{"name": "my_captcha_screenshot.png"}')
    assert not unlim_common.is_captcha_response(json_ok)

    html_cap = _R(200, "https://disk.yandex.ru/showcaptcha?retpath=...", "text/html", "<html>SmartCaptcha</html>")
    assert unlim_common.is_captcha_response(html_cap)

    api_403 = _R(403, "https://disk.yandex.ru/models", "application/json", '{"type": "captcha"}')
    assert unlim_common.is_captcha_response(api_403)


# ── yandex_health ────────────────────────────────────────────────────────────


def test_yandex_health_detects_302_showcaptcha_and_stale_summary():
    class _Sess:
        def get(self, url, **kwargs):
            return _FakeResp(302, headers={"Location": "https://disk.yandex.ru/showcaptcha?retpath=x"})

    spec = yandex_health.PROBES[1]  # web
    sample = yandex_health.run_probe(spec, session=_Sess())
    assert sample.ok is False and sample.status == 302 and "captcha" in (sample.error or "").lower()

    old = yandex_health.Sample(ts=1000.0, ok=True, status=200, latency_ms=50, error=None)
    summary = yandex_health.summarize(spec, [old], now=2000.0)
    assert summary["state"] == yandex_health.STATE_UNKNOWN


# ── webapp security, privacy, headers ────────────────────────────────────────


def test_security_headers_and_empty_host_rejection(client):
    r = client.get("/api/hello")
    assert r.status_code == 200
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"

    r_home = client.get("/")
    assert r_home.status_code == 200
    assert "no-cache" in (r_home.headers.get("cache-control") or "")

    r_guide = client.get("/guide")
    if r_guide.status_code == 200:
        csp = r_guide.headers.get("content-security-policy") or ""
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp

    bad = TestClient(webui.app, headers={"Host": ""})
    assert bad.get("/api/hello").status_code == 403


def test_support_scrub_removes_tokens_cookies_and_uid():
    raw = (
        "token=y0_AgAAAAB1234567890abcdef Authorization: OAuth supersecret "
        "Cookie: Session_id=3:abc|def; sessionid2=999; yandexuid=111222; sk=deadbeef uid=424242"
    )
    cleaned = webapp.logs._scrub_support_text(raw)
    for secret in ("AgAAAAB1234567890abcdef", "supersecret", "3:abc|def", "999", "111222", "deadbeef", "424242"):
        assert secret not in cleaned


def test_task_cmd_redacts_root_folder_names():
    redacted = webapp.tasks._redact_cmd(["main.py", "scan", "--root=disk:/Личное/Паспорт", "--root", "disk:/Работа"])
    assert "Паспорт" not in redacted
    assert "Работа" not in redacted
    assert "--root=<folder>" in redacted


# ── webapp.file_list ─────────────────────────────────────────────────────────


def test_file_list_csv_safe_txt_newlines_and_link_id_validation(client, tmp_path):
    assert webapp.file_list._csv_safe("  =SUM(A1)").startswith("'")
    assert webapp.file_list._csv_safe("\n=cmd").startswith("'")
    txt_bytes = b"".join(webapp.file_list.iter_export_bytes(
        iter([{"display_path": "/a\nb\rc.jpg", "link_name": "X\nY"}]), "links", "txt",
    ))
    assert txt_bytes.decode("utf-8").count("\n") == 1

    assert client.get(f"/api/links/files?link_id={2 ** 64}").status_code == 422
    assert client.get("/api/links/files?link_id=0").status_code == 422

    with pytest.raises(ValueError):
        list(webapp.file_list.iter_files_for_export("bad_source"))

    demo = webapp.file_list.demo_file_list("disk", page=0, limit=99999, status="invalid")
    assert demo["page"] == 1 and demo["limit"] == 500 and demo["status"] == "not_downloaded"


# ── webapp.rescue, control, access ───────────────────────────────────────────


def test_rescue_after_exit_does_not_mark_done_on_db_error(monkeypatch):
    r = webapp.rescue.Rescue()
    b = r.branches["disk"]
    b.state = "downloading"
    monkeypatch.setattr(webapp.rescue, "db_stats", lambda *a, **k: {"error": "database is locked"})
    waited = []
    monkeypatch.setattr(r, "_wait_round", lambda br, *a, **k: (waited.append(k.get("kind")), False)[1])
    res = r._after_exit(b, 0, (Path("x"), "files", "remote_path"))
    assert res is False
    assert b.state != "done"
    assert waited == ["crash"]


def test_rescue_disk_list_propagates_halt_codes_even_with_existing_rows(monkeypatch):
    r = webapp.rescue.Rescue()
    b = r.branches["disk"]
    monkeypatch.setattr(r, "_run_step", lambda *a, **k: utils.EXIT_AUTH)
    monkeypatch.setattr(webapp.rescue, "db_has_rows", lambda *a, **k: True)
    assert r._disk_list(b, "all", None, None) == utils.EXIT_AUTH


def test_rescue_unlim_list_propagates_halt_codes_even_with_existing_rows(monkeypatch):
    r = webapp.rescue.Rescue()
    b = r.branches["unlim"]
    monkeypatch.setattr(r, "_run_step", lambda *a, **k: utils.EXIT_AUTH)
    monkeypatch.setattr(webapp.rescue, "db_has_rows", lambda *a, **k: True)
    assert r._unlim_list(b) == utils.EXIT_AUTH


def test_organize_rejects_program_subdirectories_and_open_folder_rejects_file(client, tmp_path):
    logs_dir = webapp.paths.PROJECT / "logs"
    if logs_dir.is_dir():
        ok, _ = webapp.control.is_safe_organize_path(str(logs_dir))
        assert not ok

    # Внутри папки скачивания лежит файл с именем _unlim, а не папка — open_folder не должен его запускать
    dl = tmp_path / "downloads"
    dl.mkdir()
    (dl / "_unlim").write_text("not a dir", encoding="utf-8")
    r = client.post("/api/open_folder", json={"which": "unlim"})
    assert r.status_code == 200 and r.json()["ok"] is False


def test_token_check_does_not_poison_saved_token_cache(client, monkeypatch):
    webapp.access.invalidate_token_cache()
    with webapp.access._token_cache_lock:
        webapp.access._token_check_cache.update({"token": "y0_saved", "at": 1e12, "data": {"ok": True, "login": "me"}})

    monkeypatch.setattr(webapp.access, "make_browse_client", lambda tok: (_ for _ in ()).throw(RuntimeError("net")))
    r = client.post("/api/config/token/check", json={"token": "y0_other"})
    assert r.status_code == 200 and r.json()["ok"] is False
    with webapp.access._token_cache_lock:
        assert webapp.access._token_check_cache["token"] == "y0_saved"
        assert webapp.access._token_check_cache["data"]["ok"] is True

    client.post("/api/config/token/clear")
    with webapp.access._token_cache_lock:
        assert webapp.access._token_check_cache["token"] is None
