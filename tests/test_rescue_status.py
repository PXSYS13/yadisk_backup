"""Сводка «Режима спасения» для главного экрана: формат и тексты по состояниям."""

import utils
import webapp.access
import webapp.queue_db
import webapp.rescue_view
import webapp.texts
import webui

CFG = {"download_dir": "D:\\Backup", "download_dir_set": True}
TOKEN_OK = {"ok": True, "account": {"photo_unlim": 69 * 10**9}}
COOKIES_OK = {"ok": True}
NO = {"ok": False}


def db(**kw):
    base = webapp.queue_db.empty_db_stats()
    base.update(kw)
    return base


def rescue_st(running=False, started=100.0, state="running", disk=None, unlim=None, next_retry=None):
    return {
        "running": running, "started_at": started, "finished_at": None if running else 200.0,
        "state": state, "next_retry_at": next_retry,
        "branches": {"disk": disk or {"state": "off"}, "unlim": unlim or {"state": "off"}},
    }


def build(st, disk_db=None, unlim_db=None, token=TOKEN_OK, cookies=COOKIES_OK, **kw):
    return webapp.rescue_view.build_rescue_status(st, disk_db or db(), unlim_db or db(), token, cookies, CFG,
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
    assert "Ничего делать не нужно" in r["subline"]
    assert r["summary"]["downloaded_files"] == 40 and r["summary"]["left_files"] == 60
    assert r["summary"]["selected_files"] == 100
    src = r["sources"]["disk"]
    assert src["speed_bps"] == 10**6 and src["eta_sec"] == 6000


def test_listing_phase_has_its_own_headline():
    r = build(rescue_st(running=True, disk={"state": "listing"}, unlim={"state": "checking"}))
    assert r["state"] == "running" and r["headline"] == "Готовлюсь к скачиванию"
    # Как только хоть что-то качается — обычный заголовок
    r = build(rescue_st(running=True, disk={"state": "listing"}, unlim={"state": "downloading"}))
    assert r["headline"] == "Скачиваем ваши файлы"


def test_live_signs_events_and_recent_files():
    disk = db(total=10, pending=5, downloaded=5, recent=[
        {"path": "/Фото/IMG_1.JPG", "status": "downloaded", "size": 2048, "at": 990.0},
        {"path": "/Фото/skip.JPG", "status": "skipped", "size": 1, "at": 980.0}])
    st = rescue_st(running=True, disk={"state": "downloading"}, unlim={"state": "listing"})
    st["events"] = [{"at": 900.0, "source": None, "kind": "run", "text": "первое"},
                    {"at": 950.0, "source": "disk", "kind": "ok", "text": "второе"}]
    live = {"disk": {"last_activity_at": 998.0, "tasks_running": 1, "listing_found": None,
                     "recent": {"window_sec": 300, "checked": 30, "done": 4}},
            "unlim": {"last_activity_at": 999.0, "tasks_running": 1, "listing_found": 5000,
                      "recent": {"window_sec": 120, "checked": 0, "done": 0}}}
    r = build(st, disk_db=disk, live=live)
    assert r["now"] == 1000.0
    assert [e["text"] for e in r["events"]] == ["второе", "первое"]  # новые сверху
    assert r["last_activity_at"] == 999.0
    assert r["recent"] == {"window_sec": 300, "checked": 30, "done": 4}
    src = r["sources"]["disk"]
    assert src["recent_files"] == [{"name": "IMG_1.JPG", "size": 2048, "at": 990.0}]
    assert src["tasks_running"] == 1 and src["last_activity_at"] == 998.0
    assert r["sources"]["unlim"]["listing_found"] == 5000
    # До запуска лента пуста, даже если в сводке что-то осталось
    assert build(dict(st, started_at=None, running=False), disk_db=disk, live=live)["events"] == []


def test_progress_meter_counts_recent_work():
    m = webapp.rescue_view.ProgressMeter()
    assert m.sample("disk", 10, 5, now=1000.0) is None  # мало данных
    assert m.sample("disk", 12, 5, now=1010.0) is None
    r = m.sample("disk", 40, 9, now=1030.0)
    assert r == {"window_sec": 30, "checked": 30, "done": 4}
    # Отложенные ушли на повтор — проверенных стало меньше, но прирост не отрицательный
    assert m.sample("disk", 5, 9, now=1040.0)["checked"] == 0
    # Старые замеры выпадают из окна
    r = m.sample("disk", 50, 20, now=1030.0 + m.WINDOW_SEC + 5)
    assert r["window_sec"] <= m.WINDOW_SEC


def test_listing_found_and_source_live():
    tasks = {
        "disk": {"running": True, "last_line_at": 50.0,
                 "log_tail": ["12:00:01  Найдено: 1200 (Найдено [/])", "12:00:03  Найдено: 2400 (Найдено [/])"]},
        "unlim_collect": {"running": False, "last_line_at": 40.0,
                          "log_tail": ["  Найдено: 13602 файлов (64.34 GB), новых 0"]},
        "unlim_w1": {"running": True, "last_line_at": 70.0, "log_tail": []},
    }
    disk = webapp.rescue_view.source_live("disk", tasks, "listing", None)
    assert disk["listing_found"] == 2400 and disk["last_activity_at"] == 50.0
    unlim = webapp.rescue_view.source_live("unlim", tasks, "downloading", {"window_sec": 60, "checked": 1, "done": 1})
    assert unlim["listing_found"] is None  # список уже собран
    assert unlim["last_activity_at"] == 70.0 and unlim["tasks_running"] == 1
    assert webapp.rescue_view.source_live("unlim", tasks, "listing", None)["listing_found"] == 13602
    assert webapp.rescue_view.source_live("disk", {}, None, None)["last_activity_at"] is None


def test_downloaded_at_is_read_as_utc():
    assert webapp.queue_db.utc_ts("1970-01-01T00:01:40") == 100.0
    assert webapp.queue_db.utc_ts("1970-01-01 00:01:40") == 100.0
    assert webapp.queue_db.utc_ts(None) is None and webapp.queue_db.utc_ts("мусор") is None


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


def test_unwritable_folder_is_not_called_a_full_disk():
    r = build(rescue_st(state="failed", disk={"state": "needs_action", "action": "free_space"}),
              free_space={"free": 500 * 1024**3})
    assert r["action"]["id"] == "free_space" and r["action"]["variant"] == "folder"
    assert r["action"]["title"] == "Не получается записать файлы в папку"
    assert "Свободно" not in r["action"]["text"]


def test_waiting_headline_names_the_real_reason():
    crash = build(rescue_st(running=True, next_retry=1060.0,
                            disk={"state": "waiting", "wait_kind": "crash", "next_retry_at": 1060.0}),
                  disk_db=db(total=10, pending=10))
    assert crash["state"] == "waiting"
    assert crash["headline"] == "Скачивание прервалось — пробую ещё раз"
    assert crash["sources"]["disk"]["wait_kind"] == "crash"
    outage = build(rescue_st(running=True, next_retry=1800.0,
                             disk={"state": "waiting", "wait_kind": "outage", "next_retry_at": 1800.0}),
                   disk_db=db(total=10, pending=10))
    assert outage["headline"] == "Яндекс сейчас не отвечает"


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
    assert utils.files_text(1) == "1 файл"
    assert utils.files_text(3) == "3 файла"
    assert utils.files_text(11) == "11 файлов"
    assert utils.files_text(10900) == "10 900 файлов"
    assert webapp.texts.ru_bytes(69 * 1024**3) == "69,0 ГБ"


def test_status_endpoint_carries_rescue(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    r = client.get("/api/status").json()["rescue"]
    assert r["schema"] == 1 and set(r["sources"]) == {"disk", "unlim"}
