"""Интеграционные и security-тесты подсистемы загрузки в Безлимит (/api/upload/*) внутри yadisk_backup."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import webapp.paths
import webui
from uploader.utils import validate_source_dir_safe
from uploader.webapp import manager as up_mgr


def _client() -> TestClient:
    return TestClient(webui.app, headers={"Host": "localhost:8000"})


def test_upload_routes_mounted_and_share_cookies(tmp_path: Path) -> None:
    c = _client()
    r = c.get("/api/upload/status")
    assert r.status_code == 200
    data = r.json()
    assert data["running"] is False
    assert data["cookies"]["present"] is False

    # Общие cookies.json из yadisk_backup видны сразу без повторного логина
    sample_cookies = [
        {"name": "Session_id", "value": "3:1700000000.test_sess", "domain": ".yandex.ru", "path": "/"},
        {"name": "sessionid2", "value": "3:1700000000.test_sess2", "domain": ".yandex.ru", "path": "/"},
        {"name": "yandexuid", "value": "1234567890123456", "domain": ".yandex.ru", "path": "/"},
    ]
    webapp.paths.COOKIES_FILE.write_text(json.dumps(sample_cookies), encoding="utf-8")
    r2 = c.get("/api/upload/status")
    assert r2.status_code == 200
    assert r2.json()["cookies"]["present"] is True
    assert r2.json()["cookies"]["ok"] is True


def test_upload_csrf_host_origin_and_unc_guards(tmp_path: Path) -> None:
    # Чужой Host блокируется
    bad_host = TestClient(webui.app, headers={"Host": "evil.example"})
    assert bad_host.get("/api/upload/status").status_code == 403

    c = _client()
    # Чужой Origin / Sec-Fetch-Site блокируется
    assert c.post(
        "/api/upload/settings",
        json={"max_workers": 2},
        headers={"Origin": "http://evil.example"},
    ).status_code == 403
    assert c.get(
        "/api/upload/status",
        headers={"Sec-Fetch-Site": "cross-site"},
    ).status_code == 403

    # Unknown поля отклоняются (ConfigDict extra='forbid')
    assert c.post("/api/upload/settings", json={"unknown_key": 1}).status_code == 422

    # UNC / Windows device bypasses (HIGH-3) отклоняются
    for bad in (
        r"\\server\share\photos",
        r"\\?\unc\server\share",
        r"\\?\Unc\server\share",
        r"\\?\GLOBALROOT\Device\Mup\server\share",
        r"\\??\C:\Windows",
        r"\\.\PhysicalDrive0",
    ):
        with pytest.raises(ValueError):
            validate_source_dir_safe(bad)
        resp = c.post("/api/upload/settings", json={"source_dir": bad})
        assert resp.status_code == 400


def test_upload_settings_scan_files_csv_and_reset(tmp_path: Path) -> None:
    src = tmp_path / "family_photos"
    src.mkdir()
    Image.new("RGB", (64, 64), (100, 150, 200)).save(src / "IMG_20230715_120000.jpg", format="JPEG")
    Image.new("RGB", (64, 64), (200, 100, 150)).save(src / "=SUM(A1).png", format="PNG")

    c = _client()
    # Настройки сохраняются в upload_unlim_config.json (не трогают .env)
    r_cfg = c.post(
        "/api/upload/settings",
        json={"source_dir": str(src), "max_workers": 3, "chunk_mb": 8},
    )
    assert r_cfg.status_code == 200
    assert r_cfg.json()["config"]["max_workers"] == 3
    assert webapp.paths.UPLOAD_CONFIG_FILE.exists()
    assert not webapp.paths.ENV_FILE.exists()

    # Фоновое сканирование папки
    r_scan = c.post("/api/upload/scan", json={"source_dir": str(src)})
    assert r_scan.status_code == 200

    for _ in range(80):
        st = c.get("/api/upload/status").json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert up_mgr.manager.is_running() is False

    r_files = c.get("/api/upload/files?status=all")
    assert r_files.status_code == 200
    assert r_files.json()["total"] == 2

    # CSV нейтрализует формулы Excel
    r_csv = c.get("/api/upload/export.csv")
    assert r_csv.status_code == 200
    assert "'=SUM(A1).png" in r_csv.text

    # Очистка очереди
    r_reset = c.post("/api/upload/reset", json={})
    assert r_reset.status_code == 200
    assert c.get("/api/upload/files").json()["total"] == 0


def test_upload_cookies_cache_invalidates_on_file_update_and_logs_redact_json(tmp_path: Path) -> None:
    from webapp import logs as logs_mod

    c = _client()
    # Симулируем старую ошибку кук и обновление cookies.json из Настроек
    webapp.paths.COOKIES_FILE.write_text(
        json.dumps([
            {"name": "Session_id", "value": "3:1700000000.old_sess", "domain": ".yandex.ru", "path": "/"},
            {"name": "sessionid2", "value": "3:1700000000.old_sess2", "domain": ".yandex.ru", "path": "/"},
            {"name": "yandexuid", "value": "1111111111111111", "domain": ".yandex.ru", "path": "/"},
        ]),
        encoding="utf-8",
    )
    up_mgr.manager.set_account_cache(None, "Сессия истекла")
    assert c.get("/api/upload/status").json()["cookies"]["ok"] is False

    # Обновляем cookies.json (новый размер) — UploadManager автоматически сбрасывает устаревшую ошибку
    webapp.paths.COOKIES_FILE.write_text(
        json.dumps([
            {"name": "Session_id", "value": "3:1700000000.fresh_session_value", "domain": ".yandex.ru", "path": "/"},
            {"name": "sessionid2", "value": "3:1700000000.fresh_session_value2", "domain": ".yandex.ru", "path": "/"},
            {"name": "yandexuid", "value": "9999999999999999", "domain": ".yandex.ru", "path": "/"},
        ]),
        encoding="utf-8",
    )
    assert c.get("/api/upload/status").json()["cookies"]["ok"] is True

    # JSON-ключи кук/sk/uid редактируются в support bundle
    raw = '{"Session_id": "3:17000.secret", "sk": "u123456789", "uid": "987654"}'
    scrubbed = logs_mod._scrub_support_text(raw)
    assert "3:17000.secret" not in scrubbed
    assert "u123456789" not in scrubbed
    assert "987654" not in scrubbed
