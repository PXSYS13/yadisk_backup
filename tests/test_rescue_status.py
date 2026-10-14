"""Сводка «Режима спасения» для главного экрана: формат и тексты по состояниям."""

import webui

CFG = {"download_dir": "D:\\Backup", "download_dir_set": True}
TOKEN_OK = {"ok": True, "account": {"photo_unlim": 69 * 10**9}}
COOKIES_OK = {"ok": True}
NO = {"ok": False}


def db(**kw):
    base = webui._empty_db_stats()
    base.update(kw)
    return base


def rescue_st(running=False, started=100.0, state="running", disk=None, unlim=None, next_retry=None):
    return {
        "running": running, "started_at": started, "finished_at": None if running else 200.0,
        "state": state, "next_retry_at": next_retry,
        "branches": {"disk": disk or {"state": "off"}, "unlim": unlim or {"state": "off"}},
    }


def build(st, disk_db=None, unlim_db=None, token=TOKEN_OK, cookies=COOKIES_OK, **kw):
    return webui.build_rescue_status(st, disk_db or db(), unlim_db or db(), token, cookies, CFG,
                                     now=kw.pop("now", 1000.0), **kw)


def test_nothing_configured_asks_for_setup():
    r = build(rescue_st(started=None), token=NO, cookies=NO)
    assert r["schema"] == 1 and r["state"] == "idle"
    assert r["action"]["id"] == "setup"
    assert r["headline"].startswith("Сохраните свои файлы")


def test_ready_to_start_names_sources_and_folder():
    r = build(rescue_st(started=None))
    assert r["state"] == "idle" and r["action"] is None
    assert "Диск и Безлимит" in r["subline"] and "D:\\Backup" in r["subline"]


def test_files_math_separates_deferred_from_available():
    disk = db(total=10, pending=6, deferred=4, downloaded=2, skipped=2, category_skipped=1, failed=0,
              bytes_pending=600, bytes_deferred=400)
    r = build(rescue_st(running=True, disk={"state": "downloading"}), disk_db=disk)
    files = r["sources"]["disk"]["files"]
    assert files["pending"] == 2 and files["deferred"] == 4
    assert files["done"] == 3  # скачано 2 + пропущено как уже лежащее 1
    assert files["excluded"] == 1 and files["selected"] == 9
    assert r["sources"]["disk"]["bytes"]["pending"] == 200


def test_no_speed_or_eta_from_a_tiny_sample():
    disk = db(total=100, pending=60, downloaded=40, bytes_10m=900, bytes_pending=50 * 10**9)
    r = build(rescue_st(running=True, started=100.0, disk={"state": "downloading"}), disk_db=disk, now=100.0 + 600)
    assert r["sources"]["disk"]["speed_bps"] is None and r["sources"]["disk"]["eta_sec"] is None


def test_running_shows_progress_and_speed():
    disk = db(total=100, pending=60, downloaded=40, bytes_10m=600 * 10**6, bytes_pending=6 * 10**9)
    r = build(rescue_st(running=True, started=100.0, disk={"state": "downloading", "activity": "скачиваю файлы"}),
              disk_db=disk, now=100.0 + 600)
    assert r["state"] == "running"
    assert r["headline"] == "Скачиваем ваши файлы"
    assert "Готово 40 из" in r["subline"]
    src = r["sources"]["disk"]
    assert src["speed_bps"] == 10**6 and src["eta_sec"] == 6000


def test_waiting_for_yandex_tells_when_next_try_is():
    disk = db(total=10, pending=8, deferred=8, downloaded=2)
    r = build(rescue_st(running=True, next_retry=1800.0,
                        disk={"state": "waiting", "next_retry_at": 1800.0},
                        unlim={"state": "done"}), disk_db=disk)
    assert r["state"] == "waiting" and r["can_retry_now"] is True
    assert r["headline"].startswith("Всё, что Яндекс отдаёт сейчас")
    assert "Ждут Яндекса: 8 файлов" in r["subline"]
    assert r["notice"]["kind"] == "storage_down"


def test_expired_token_becomes_one_action():
    r = build(rescue_st(running=True, disk={"state": "needs_action", "action": "token"},
                        unlim={"state": "downloading"}))
    assert r["state"] == "needs_action"
    assert r["action"]["id"] == "token" and r["action"]["source"] == "disk"
    assert r["action"]["button"] == "Получить новый ключ"
    assert r["notice"] is None  # пока ждём человека, спокойные плашки не показываем


def test_disk_full_mentions_free_space():
    r = build(rescue_st(state="failed", disk={"state": "needs_action", "action": "free_space"}),
              free_space={"free": 120 * 1024**2})
    assert r["state"] == "needs_action"
    assert "Свободно 120,0 МБ" in r["action"]["text"]


def test_done_fully_and_with_leftovers():
    full = build(rescue_st(state="done", disk={"state": "done"}), disk_db=db(total=5, downloaded=5, bytes_done=5 * 10**9))
    assert full["state"] == "done" and full["summary"]["complete"] is True
    assert full["headline"].startswith("Готово!")
    partial = build(rescue_st(state="done", disk={"state": "done"}), disk_db=db(total=5, downloaded=3, failed=2))
    assert partial["summary"]["complete"] is False
    assert "Не скачались: 2 файла" in partial["subline"]


def test_stopped():
    r = build(rescue_st(state="stopped", disk={"state": "stopped"}))
    assert r["state"] == "stopped" and r["headline"] == "Скачивание на паузе"


def test_plural_and_bytes_helpers():
    assert webui._files(1) == "1 файл"
    assert webui._files(3) == "3 файла"
    assert webui._files(11) == "11 файлов"
    assert webui._files(10900) == "10 900 файлов"
    assert webui._ru_bytes(69 * 1024**3) == "69,0 ГБ"


def test_status_endpoint_carries_rescue(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    r = client.get("/api/status").json()["rescue"]
    assert r["schema"] == 1 and set(r["sources"]) == {"disk", "unlim"}
