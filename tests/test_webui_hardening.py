"""Локальный пульт: чужие сайты, гонки с «Остановить», секреты в отчёте, права на файлы."""

import os
import stat
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

import utils
import webapp.access
import webapp.config
import webapp.control
import webapp.logs
import webapp.paths
import webapp.rescue
import webapp.tasks
import webui


@pytest.fixture()
def client():
    return TestClient(webui.app, headers={"Host": "localhost:8000"})


@pytest.mark.parametrize("site,code", [("cross-site", 403), ("same-site", 403), ("same-origin", 200), (None, 200)])
def test_other_sites_cannot_trigger_api_gets(client, site, code):
    """<img src="http://127.0.0.1:8000/api/logs/bundle"> с чужого сайта не пишет архивы и не тратит
    лимит запросов к Яндексу: браузер помечает такой запрос Sec-Fetch-Site: cross-site."""
    headers = {"Sec-Fetch-Site": site} if site else {}
    assert client.get("/api/hello", headers=headers).status_code == code
    # Сам интерфейс открывается по ссылке откуда угодно
    assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_support_bundle_has_no_signed_links(client, monkeypatch):
    webapp.paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (webapp.paths.LOG_DIR / "backup.log").write_text(
        "FAIL | http_error | 403 Client Error: Forbidden for url: "
        "https://downloader.disk.yandex.ru/disk/abc?uid=42&rtoken=SECRET | 1 | disk:/a.jpg\n", encoding="utf-8")
    monkeypatch.setattr(webapp.logs, "support_info", lambda: {"tail": "url: /rdisk/x?rtoken=SECRET2 (Caused by)"})
    bundle = webapp.logs.build_support_bundle()
    with zipfile.ZipFile(bundle) as zf:
        text = "".join(zf.read(n).decode("utf-8") for n in zf.namelist())
    assert "SECRET" not in text and "uid=42" not in text
    assert "downloader.disk.yandex.ru" in text and "disk:/a.jpg" in text
    # Журналы в архиве — со своим временем изменения, а не 1980-01-01: по нему ищут, когда что было
    with zipfile.ZipFile(bundle) as zf:
        assert zf.getinfo("backup.log").date_time[0] >= 2024


def test_relist_right_after_a_wait_round_is_kept():
    """«Обновить список» в ту секунду, когда ветка проснулась после ожидания и считает базу перед
    скачиванием: просьба не теряется, а исполняется после шага — как во время скачивания."""
    r = webapp.rescue.Rescue()
    hold = threading.Event()
    r._thread = threading.Thread(target=hold.wait, daemon=True)
    b = r.branches["disk"]
    b.thread = threading.Thread(target=hold.wait, daemon=True)
    r._thread.start()
    b.thread.start()
    try:
        b.state, b.prepared = "checking", False  # первый подсчёт: prepare соберёт список сам
        assert r.relist("disk") == (True, "Список и так собирается прямо сейчас") and not b.relist
        b.prepared = True  # после ожидания: prepare уже не запустится
        ok, _ = r.relist("disk")
        assert ok is True and b.relist is True
    finally:
        hold.set()


def test_nothing_starts_after_stop():
    """Воркер, запущенный в ту же секунду, что нажали «Остановить», качал бы дальше."""
    r = webapp.rescue.Rescue()
    started = []
    orig = webapp.tasks.tm.start
    webapp.tasks.tm.start = lambda slot, script, args: (started.append(slot), (True, "ok"))[1]
    try:
        r._stop_evt.set()
        assert r._own_start("unlim_w1", "unlim_worker.py", [])[0] is False
        assert started == []
    finally:
        webapp.tasks.tm.start = orig


def test_branch_waiting_for_a_new_token_resumes_when_it_is_saved(client, monkeypatch):
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": True, "login": "t"})
    r = webapp.rescue.Rescue()
    r._thread = threading.Thread(target=lambda: None)
    monkeypatch.setattr(r, "is_running", lambda: True)
    b = r.branches["disk"]
    b.state, b.action = "needs_action", "token"
    restarted = []
    monkeypatch.setattr(r, "_restart_branch", lambda br: restarted.append(br.name))
    monkeypatch.setattr(webapp.rescue, "rescue", r)
    assert client.post("/api/config/token", json={"token": "y0_new-token"}).json()["ok"]
    assert restarted == ["disk"]
    # Ветка, которая не ждёт доступа, не перезапускается
    b.state, b.action = "waiting", None
    assert r.resume_access("disk") is False


def test_settings_saved_at_once_do_not_overwrite_each_other():
    """Переключатели в настройках шлют запросы независимо: оба значения должны остаться."""
    webapp.config.ensure_env_file()
    errors = []

    def save(key):
        try:
            for i in range(15):
                webapp.config.env_set_verified(key, str(i))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=save, args=(k,)) for k in ("GENTLE_MODE", "YADISK_PREVIEWS")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    from dotenv import dotenv_values
    saved = dotenv_values(webapp.paths.ENV_FILE)
    assert saved.get("GENTLE_MODE") == "14" and saved.get("YADISK_PREVIEWS") == "14"


@pytest.mark.skipif(utils.IS_WINDOWS, reason="права доступа POSIX")
def test_token_and_cookies_are_readable_only_by_the_owner(client):
    client.post("/api/config/token", json={"token": "y0_secret"})
    assert stat.S_IMODE(os.stat(webapp.paths.ENV_FILE).st_mode) == 0o600
    cookies = [{"name": n, "value": "v", "domain": ".yandex.ru"} for n in ("Session_id", "yandexuid", "sessionid2")]
    assert client.post("/api/cookies", json={"cookies": cookies}).status_code == 200
    assert stat.S_IMODE(os.stat(webapp.paths.COOKIES_FILE).st_mode) == 0o600


@pytest.mark.parametrize("bad", ["/data/${HOME}/x", "/data/a\x00b", "/data/a\nb"])
def test_download_dir_with_expansions_or_control_chars_is_refused(bad):
    ok, _ = utils.is_safe_download_dir(bad)
    assert not ok


def test_download_dir_cannot_be_the_program_folder(client):
    for d in (webapp.paths.PROJECT, webapp.paths.PROJECT / "logs", webapp.paths.PROJECT / "webui_assets" / "x"):
        r = client.post("/api/config/download_dir", json={"download_dir": str(d)})
        assert r.status_code == 400, d
    ok = client.post("/api/config/download_dir", json={"download_dir": str(webapp.paths.PROJECT / "downloads")})
    assert ok.status_code == 200


def test_downloads_wait_for_a_running_sort(client, monkeypatch):
    """Сортировка переносит файлы: запуск скачивания посреди неё перепутал бы их."""
    monkeypatch.setattr(webapp.control, "organize_moving", lambda: True)
    res = client.post("/api/rescue/start").json()
    assert res["ok"] is False and "сортировк" in res["msg"]
    assert client.post("/api/config/download_dir", json={"download_dir": "/tmp/x"}).status_code == 409
