"""«Режим спасения»: ветки Диска и Безлимита параллельно, отложенное — раундами."""

import sqlite3
import threading
import time

import pytest

import state
import unlim_common
import webapp.access
import webapp.config
import webapp.listing
import webapp.paths
import webapp.queue_db
import webapp.rescue
import webapp.rescue_view
import webapp.tasks
from utils import EXIT_AUTH, EXIT_DEFERRED, EXIT_DISK_FULL, EXIT_OUTAGE, EXIT_PARTIAL


class Script:
    """Подменяет запуск задач: коды выхода по слотам, по очереди; последний повторяется."""

    def __init__(self, monkeypatch, rescue, codes, worker_codes=(0,)):
        self.codes = {slot: list(seq) for slot, seq in codes.items()}
        self.worker_codes = list(worker_codes)
        self.started = []
        self.lock = threading.Lock()
        monkeypatch.setattr(webapp.tasks.tm, "start", self._start)
        monkeypatch.setattr(rescue, "_wait_task", self._wait)
        monkeypatch.setattr(rescue, "_wait_workers", self._wait_workers)

    def _start(self, slot, script, args):
        with self.lock:
            self.started.append((slot, script, tuple(args)))
        return True, "ok"

    def _next(self, seq):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def _wait(self, slot, on_finish_msg=""):
        with self.lock:
            return self._next(self.codes.setdefault(slot, [0]))

    def _wait_workers(self, ids):
        with self.lock:
            code = self._next(self.worker_codes)
        return [code for _ in ids]

    def steps(self, slot=None):
        return [(s, a[0] if a else "") for s, _sc, a in self.started if slot in (None, s)]


@pytest.fixture()
def rescue(monkeypatch):
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": True, "login": "t", "used": 1})
    monkeypatch.setattr(webapp.access, "check_cookies", lambda: {"ok": True, "msg": "ok", "count": 5})
    r = webapp.rescue.Rescue()
    r.retry_every = 0.05  # раунды в тесте — мгновенные
    r.workers = 1
    return r


def test_both_branches_run_and_finish(rescue, monkeypatch):
    sc = Script(monkeypatch, rescue, {"disk": [0, 0], "unlim_collect": [0]})
    webapp.config.set_resume_pending(True)
    rescue._run()
    assert rescue.state == "done"
    assert sc.steps("disk") == [("disk", "scan"), ("disk", "download")]
    assert ("unlim_collect", "--collect") in sc.steps()
    assert any(s.startswith("unlim_w") for s, _ in sc.steps())
    assert {b["state"] for b in rescue.status()["branches"].values()} == {"done"}
    # Всё скачано: после перезапуска программы продолжать нечего
    assert webapp.config.resume_pending() is False


def test_action_needed_does_not_resume_after_restart(rescue, monkeypatch):
    Script(monkeypatch, rescue, {"disk": [0, EXIT_AUTH]}, worker_codes=[EXIT_AUTH])
    webapp.config.set_resume_pending(True)
    rescue._run()
    assert rescue.state == "failed" and webapp.config.resume_pending() is False


def test_deferred_files_get_another_round_without_relisting(rescue, monkeypatch):
    sc = Script(monkeypatch, rescue, {"disk": [0, EXIT_DEFERRED, EXIT_DEFERRED, 0]},
                worker_codes=[EXIT_DEFERRED, 0])
    rescue._run()
    assert rescue.state == "done"
    disk = sc.steps("disk")
    assert disk.count(("disk", "scan")) == 1  # список не пересобирается в раундах
    assert disk.count(("disk", "download")) == 3
    assert sum(1 for s, _ in sc.steps() if s.startswith("unlim_w")) == 2 * rescue.workers
    assert any("Следующая попытка" in line for line in rescue.log)


def test_lists_are_remembered_across_restarts(rescue, monkeypatch):
    sc = Script(monkeypatch, rescue, {"disk": [0, 0], "unlim_collect": [0]})
    rescue._run()
    assert ("unlim_collect", "--collect") in sc.steps()
    again = webapp.rescue.Rescue()  # перезапуск программы: новый объект, те же базы
    again.retry_every, again.workers = 0.05, 1
    sc2 = Script(monkeypatch, again, {"disk": [0], "unlim_collect": [0]})
    again._run()
    assert again.state == "done"
    assert sc2.steps("disk") == [("disk", "download")]
    assert ("unlim_collect", "--collect") not in sc2.steps()
    texts = [e["text"] for e in again.events]
    assert any(t.startswith("Список фото и видео собран") for t in texts)
    unlim = webapp.queue_db.db_stats(webapp.paths.UNLIM_DB_FILE, "unlim_files", "file_id")
    assert unlim["listed_at"] == webapp.listing.read_listing("unlim")["at"]
    block = webapp.rescue_view.source_block("unlim", unlim, None, True, "", None, None)
    assert block["listed_at"] == unlim["listed_at"]


def _disk_rows(*names):
    st = state.State(str(webapp.paths.DB_FILE))
    st.mark_pending_batch([(f"disk:/{n}", 1, None) for n in names])
    st.close()


def _unlim_rows(*names):
    con = sqlite3.connect(webapp.paths.UNLIM_DB_FILE)
    con.executescript(unlim_common.UNLIM_SCHEMA)
    con.executemany("INSERT INTO unlim_files (file_id, name, size) VALUES (?, ?, 1)",
                    [(f"/photounlim/{n}", n) for n in names])
    con.commit()
    con.close()


def _download_everything(db, table):
    con = sqlite3.connect(db)
    con.execute(f"UPDATE {table} SET status='downloaded' WHERE status='pending'")
    con.commit()
    con.close()


def _old_listing(source, hours=7):
    at = time.time() - hours * 3600
    webapp.listing.write_listing(source, {"at": at, "whole": True, "folders": []})
    return at


class DiskSteps(Script):
    """Скачивание выкачивает очередь, сбор списка добавляет в неё new_files."""

    def __init__(self, monkeypatch, rescue, new_files=()):
        super().__init__(monkeypatch, rescue, {"disk": [0]})
        self.new_files = list(new_files)

    def _wait(self, slot, on_finish_msg=""):
        step = self.started[-1][2][0]
        if step == "download":
            _download_everything(webapp.paths.DB_FILE, "files")
        elif step == "scan" and self.new_files:
            _disk_rows(*self.new_files)
            self.new_files.clear()
        return 0


def test_rows_outside_unlim_list_first_but_not_again_after_a_broken_listing(rescue, monkeypatch):
    """Строки старых версий вне /photounlim убирает только полный сбор списка, поэтому он идёт до
    скачивания. Но если Яндекс не дал собрать список до конца, следующий запуск не ждёт его часами
    снова: качает по старому списку и собирает его после шага — как 2.7.4."""
    rescue.skip_disk = True
    _unlim_rows("a.jpg")
    con = sqlite3.connect(webapp.paths.UNLIM_DB_FILE)
    con.execute("INSERT INTO unlim_files (file_id, name, size) VALUES ('disk:/Фото/b.jpg', 'b.jpg', 1)")
    con.commit()
    con.close()
    sc = Script(monkeypatch, rescue, {"unlim_collect": [EXIT_OUTAGE]}, worker_codes=[0])
    rescue._run()
    first = [s for s, _ in sc.steps() if s == "unlim_collect" or s.startswith("unlim_w")]
    assert first[0] == "unlim_collect"  # сбор списка — первым
    assert webapp.listing.outside_list_failed_recently(time.time())
    again = webapp.rescue.Rescue()
    again.retry_every, again.workers, again.skip_disk = 0.05, 1, True
    sc2 = Script(monkeypatch, again, {"unlim_collect": [EXIT_OUTAGE]}, worker_codes=[0])
    again._run()
    second = [s for s, _ in sc2.steps() if s == "unlim_collect" or s.startswith("unlim_w")]
    assert second[0].startswith("unlim_w") and "unlim_collect" in second  # сначала качаем, список — после
    # Полный сбор списка забывает неудачу
    webapp.listing.note_outside_list_failed(None)
    assert not webapp.listing.outside_list_failed_recently(time.time())


def test_an_old_list_is_downloaded_first_and_refreshed_after(rescue, monkeypatch):
    """Список старше 6 часов, а по нему есть что качать: качаем сразу, список обновляем после шага.
    Раньше большой Диск часами перечислялся заново, и всё это время скачивание стояло."""
    rescue.skip_unlim = True
    _disk_rows("a.jpg", "b.jpg")
    old = _old_listing("disk")
    sc = DiskSteps(monkeypatch, rescue)
    rescue._run()
    assert sc.steps("disk") == [("disk", "download"), ("disk", "scan")]
    assert rescue.state == "done"
    assert webapp.listing.read_listing("disk")["at"] > old
    texts = [e["text"] for e in rescue.events]
    assert any("сначала качаю по нему, потом обновлю его" in t for t in texts)


def test_new_files_from_the_refreshed_list_are_downloaded_at_once(rescue, monkeypatch):
    rescue.skip_unlim = True
    rescue.retry_every = 3600  # новые файлы не должны ждать раунда
    _disk_rows("a.jpg")
    _old_listing("disk")
    sc = DiskSteps(monkeypatch, rescue, new_files=["new.jpg"])
    rescue._run()
    assert sc.steps("disk") == [("disk", "download"), ("disk", "scan"), ("disk", "download")]
    assert rescue.state == "done"
    assert "В обновлённом списке есть новые файлы — качаю их" in [e["text"] for e in rescue.events]


def test_a_list_without_a_record_is_downloaded_first(rescue, monkeypatch):
    """Прежний интерфейс собирал список без записи о сборе: после перехода на Главную спасение
    часами перечисляло большой Диск заново, прежде чем качать."""
    rescue.skip_unlim = True
    _disk_rows("a.jpg")
    sc = DiskSteps(monkeypatch, rescue)
    rescue._run()
    assert sc.steps("disk") == [("disk", "download"), ("disk", "scan")]
    assert "Сначала качаю по уже собранному списку файлов, потом обновлю его" in [e["text"] for e in rescue.events]
    assert webapp.listing.read_listing("disk") is not None


def test_relist_lists_first(rescue, monkeypatch):
    """«Обновить список» — список собирается до скачивания, как просили."""
    rescue.skip_unlim = True
    _disk_rows("a.jpg")
    _old_listing("disk")
    assert rescue.relist("disk") == (None, "")  # спасение не идёт — его запустит вызывающий
    sc = DiskSteps(monkeypatch, rescue)
    rescue._run()
    assert sc.steps("disk") == [("disk", "scan"), ("disk", "download")]
    assert not rescue._relist_asked


def test_relist_while_checking_access_lists_first(rescue, monkeypatch):
    """«Обновить список» нажали, пока ветка проверяет доступ: ответ «список и так собирается» — правда."""
    rescue.skip_unlim = True
    _disk_rows("a.jpg")
    _old_listing("disk")
    sc = DiskSteps(monkeypatch, rescue)
    check = webapp.access.check_token

    def relist_during_check(tok):
        assert rescue.relist("disk") == (True, "Список и так собирается прямо сейчас")
        return check(tok)

    monkeypatch.setattr(webapp.access, "check_token", relist_during_check)
    monkeypatch.setattr(rescue, "is_running", lambda: True)
    rescue._run()
    assert sc.steps("disk") == [("disk", "scan"), ("disk", "download")]


def test_an_old_unlim_list_is_downloaded_first_too(rescue, monkeypatch):
    rescue.skip_disk = True
    _unlim_rows("a.jpg")
    _old_listing("unlim")
    sc = Script(monkeypatch, rescue, {"unlim_collect": [0]})

    def workers(ids):
        _download_everything(webapp.paths.UNLIM_DB_FILE, "unlim_files")
        return [0 for _ in ids]

    monkeypatch.setattr(rescue, "_wait_workers", workers)
    rescue._run()
    slots = [s for s, _ in sc.steps()]
    assert slots == ["unlim_w1", "unlim_collect"]
    assert rescue.state == "done"


def test_rescue_waits_for_a_branch_restarted_by_relist(rescue, monkeypatch):
    """Диск готов, Безлимит ждёт попытки; «Обновить список» запускает Диск снова — спасение ждёт и его."""
    sc = Script(monkeypatch, rescue, {"disk": [0, 0], "unlim_collect": [0]}, worker_codes=[EXIT_DEFERRED, 0])
    rescue.retry_every = 1.0
    unlim = rescue.branches["unlim"]
    wait = sc._wait
    disk_steps = []

    def slow_second_listing(slot, on_finish_msg=""):
        if slot == "disk":
            disk_steps.append(slot)
            if len(disk_steps) == 3:  # новый сбор списка заканчивается позже Безлимита
                deadline = time.time() + 5
                while unlim.state != "done" and time.time() < deadline:
                    time.sleep(0.01)
        return wait(slot, on_finish_msg)

    monkeypatch.setattr(rescue, "_wait_task", slow_second_listing)
    rescue._thread = threading.Thread(target=rescue._run)
    rescue._thread.start()
    deadline = time.time() + 5
    while not (rescue.branches["disk"].state == "done" and unlim.state == "waiting") and time.time() < deadline:
        time.sleep(0.005)
    assert rescue.relist("disk") == (True, "Обновляю список файлов")
    rescue._thread.join(10)
    assert not rescue._thread.is_alive() and not rescue.branches["disk"].alive()
    assert sc.steps("disk").count(("disk", "scan")) == 2
    assert rescue.state == "done"


def test_events_tell_the_story_in_plain_words(rescue, monkeypatch):
    Script(monkeypatch, rescue, {"disk": [0, EXIT_DEFERRED, 0], "unlim_collect": [0]})
    rescue._run()
    events = rescue.status()["events"]
    texts = [e["text"] for e in events]
    assert texts[0] == "Начинаю: проверяю доступ к Яндексу"
    assert "Ключ доступа к Диску работает" in texts and "Доступ к Безлимиту работает" in texts
    assert any(t.startswith("Список получен:") for t in texts)
    assert any(e["kind"] == "wait" and e["source"] == "disk" for e in events)
    assert "Пробую отложенные файлы снова" in texts
    assert texts.count("Готово: все файлы сохранены") == 2
    assert all(set(e) == {"at", "source", "kind", "text"} for e in events)
    # Без кодов выхода и жаргона
    assert not any("код" in t or "воркер" in t.lower() or "cookies" in t.lower() for t in texts)


def test_expired_token_stops_only_the_disk(rescue, monkeypatch):
    Script(monkeypatch, rescue, {"disk": [0, EXIT_AUTH], "unlim_collect": [0]})
    rescue._run()
    branches = rescue.status()["branches"]
    assert branches["disk"]["state"] == "needs_action" and branches["disk"]["action"] == "token"
    assert branches["unlim"]["state"] == "done"
    assert rescue.state == "failed"


def test_full_disk_pauses_both_branches(rescue, monkeypatch):
    stopped = []
    monkeypatch.setattr(webapp.tasks.tm, "stop_matching", lambda prefix: stopped.append(prefix) or [])
    Script(monkeypatch, rescue, {"disk": [0, EXIT_DISK_FULL], "unlim_collect": [0]},
           worker_codes=[EXIT_DEFERRED])
    rescue._run()
    branches = rescue.status()["branches"]
    assert {b["action"] for b in branches.values()} == {"free_space"}
    assert {"disk", "unlim"} <= set(stopped)


def test_yandex_down_at_listing_waits_and_retries(rescue, monkeypatch):
    rescue.skip_unlim = True
    sc = Script(monkeypatch, rescue, {"disk": [EXIT_OUTAGE, 0, 0]})
    rescue._run()
    assert rescue.state == "done"
    assert sc.steps("disk") == [("disk", "scan"), ("disk", "scan"), ("disk", "download")]


def test_unknown_exit_codes_ask_for_a_report_after_a_few_rounds(rescue, monkeypatch):
    rescue.skip_unlim = True
    Script(monkeypatch, rescue, {"disk": [0, 7]})
    rescue._run()
    disk = rescue.status()["branches"]["disk"]
    assert disk["state"] == "needs_action" and disk["action"] == "send_logs"


def test_partial_failures_finish_the_branch(rescue, monkeypatch):
    rescue.skip_unlim = True
    Script(monkeypatch, rescue, {"disk": [0, EXIT_PARTIAL]})
    rescue._run()
    assert rescue.status()["branches"]["disk"]["state"] == "done"


def test_unreachable_token_check_still_downloads_the_disk(rescue, monkeypatch):
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "unreachable": True, "msg": "timeout"})
    rescue.skip_unlim = True
    sc = Script(monkeypatch, rescue, {"disk": [0, 0]})
    rescue._run()
    assert ("disk", "download") in sc.steps("disk")


def test_start_while_waiting_retries_now_and_clears_dead_hosts(rescue, monkeypatch):
    cleared = []
    monkeypatch.setattr(webapp.rescue, "clear_dead_hosts", lambda path: cleared.append(path) or 0)
    rescue.retry_every = 3600  # без кнопки раунд не начнётся
    rescue.skip_unlim = True
    Script(monkeypatch, rescue, {"disk": [0, EXIT_DEFERRED, 0]})
    t = threading.Thread(target=rescue._run, daemon=True)
    rescue._thread = t
    t.start()
    for _ in range(200):
        if rescue.branches["disk"].state == "waiting":
            break
        t.join(0.01)
    assert rescue.branches["disk"].state == "waiting"
    assert rescue.status()["next_retry_at"] is not None
    ok, msg = rescue.start()  # «Скачать всё» во время ожидания = «Попробовать сейчас»
    assert ok and "сейчас" in msg
    t.join(5)
    assert rescue.state == "done"
    assert cleared  # молчавшим серверам дали новый шанс


def test_stop_kills_every_disk_and_unlim_task(rescue, monkeypatch):
    stopped = []
    monkeypatch.setattr(webapp.tasks.tm, "stop_matching", lambda prefix: stopped.append(prefix) or [])
    rescue.retry_every = 3600
    Script(monkeypatch, rescue, {"disk": [0, EXIT_DEFERRED]}, worker_codes=[EXIT_DEFERRED])
    t = threading.Thread(target=rescue._run, daemon=True)
    rescue._thread = t
    t.start()
    for _ in range(200):
        if all(b.state == "waiting" for b in rescue.branches.values()):
            break
        t.join(0.01)
    ok, _ = rescue.stop()
    assert ok
    assert {"disk", "unlim"} <= set(stopped)
    assert rescue.state == "stopped"
    assert {b["state"] for b in rescue.status()["branches"].values()} == {"stopped"}


def test_choosing_everything_brings_back_deselected_categories(rescue, monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending', local_path TEXT, error TEXT, downloaded_at TIMESTAMP);"
    )
    con.execute("INSERT INTO files (remote_path, size, status, error) VALUES ('disk:/a.pdf', 1, 'skipped', ?)",
                (state.CATEGORY_SKIP_MARKER,))
    con.commit()
    con.close()
    monkeypatch.setattr(webapp.paths, "DB_FILE", db)
    rescue.skip_unlim = True
    Script(monkeypatch, rescue, {"disk": [0, 0]})
    rescue._run()  # disk_categories=None — «всё»
    con = sqlite3.connect(db)
    assert con.execute("SELECT status FROM files").fetchone()[0] == "pending"
    con.close()
    # Прежний интерфейс снимал типы файлов сам: скачок числа файлов объясняется в ленте
    assert any(e["text"].startswith("Снова в очереди: 1 файл, который раньше не выбирали") for e in rescue.events)


def test_a_crashed_download_is_not_reported_as_done(rescue, monkeypatch, tmp_path):
    # Процесс упал (у Python код падения 1 = EXIT_PARTIAL), очередь не тронута
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending', local_path TEXT, error TEXT, downloaded_at TIMESTAMP);"
        "INSERT INTO files (remote_path, size) VALUES ('disk:/a.jpg', 1), ('disk:/b.jpg', 2);"
    )
    con.close()
    monkeypatch.setattr(webapp.paths, "DB_FILE", db)
    rescue.skip_unlim = True
    sc = Script(monkeypatch, rescue, {"disk": [0, EXIT_PARTIAL]})
    rescue._run()
    disk = rescue.status()["branches"]["disk"]
    assert disk["state"] == "needs_action" and disk["action"] == "send_logs"
    assert rescue.state == "failed"
    # Три быстрые попытки, а не «Скачано почти всё» после первой
    assert sc.steps("disk").count(("disk", "download")) == webapp.rescue.RESCUE_STUCK_ROUNDS
    texts = [e["text"] for e in rescue.status()["events"]]
    assert "Скачивание прервалось — пробую ещё раз через минуту" in texts
    assert "Запускаю скачивание снова" in texts
    assert not any(t.startswith("Готово") for t in texts)


def test_status_keeps_the_old_pipeline_shape(rescue):
    st = rescue.status()
    assert {"state", "running", "step", "started_at", "elapsed", "partial", "log"} <= set(st)
    assert {"branches", "next_retry_at", "retry_every"} <= set(st)


def test_combine_worker_codes():
    combine = webapp.rescue.Rescue._combine_worker_codes
    assert combine([0, 0]) == 0
    assert combine([0, EXIT_DEFERRED]) == EXIT_DEFERRED
    assert combine([EXIT_DEFERRED, EXIT_AUTH]) == EXIT_AUTH
    assert combine([EXIT_AUTH, EXIT_DISK_FULL]) == EXIT_DISK_FULL
    assert combine([None, 0]) == 0


def test_failed_listing_on_an_empty_database_is_never_done(rescue, monkeypatch):
    """Сбор списка упал (код 1) при пустой базе: раньше это было «Готово! Все файлы сохранены»."""
    rescue.skip_unlim = True
    sc = Script(monkeypatch, rescue, {"disk": [1]})
    rescue._run()
    disk = rescue.status()["branches"]["disk"]
    assert disk["state"] == "needs_action" and disk["action"] == "send_logs"
    assert rescue.state == "failed"
    assert sc.steps("disk") == [("disk", "scan")] * webapp.rescue.RESCUE_STUCK_ROUNDS
    texts = [e["text"] for e in rescue.status()["events"]]
    assert not any(t.startswith("Готово") for t in texts)
    assert "Не получилось получить список файлов — пробую ещё раз через минуту" in texts
    assert "Снова получаю список файлов" in texts


def test_unknown_exit_code_is_a_crash_not_a_yandex_outage(rescue, monkeypatch):
    rescue.skip_unlim = True
    Script(monkeypatch, rescue, {"disk": [0, 7]})
    rescue._run()
    texts = [e["text"] for e in rescue.status()["events"]]
    assert not any("Яндекс сейчас не отвечает" in t for t in texts)
    assert "Запускаю скачивание снова" in texts
    assert any("Скачивание прервалось" in line for line in rescue.log)


def test_orphaned_in_progress_rows_are_released_and_not_reported_as_done(rescue, monkeypatch, tmp_path):
    """Воркер упал посреди пачки: его файлы «в работе» — это не «все файлы сохранены»."""
    db = tmp_path / "unlim_state.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, name TEXT, size INTEGER, md5 TEXT,"
        " status TEXT DEFAULT 'pending', local_path TEXT, error TEXT, attempts INTEGER DEFAULT 0,"
        " worker_id INTEGER, assigned_at TIMESTAMP, downloaded_at TIMESTAMP, storage_host TEXT,"
        " preview_path TEXT);"
        "CREATE TABLE unlim_meta (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO unlim_files (file_id, name, size, status, worker_id)"
        " VALUES ('1', 'a.jpg', 1, 'in_progress', 1);"
    )
    con.close()
    monkeypatch.setattr(webapp.paths, "UNLIM_DB_FILE", db)
    rescue.skip_disk = True
    Script(monkeypatch, rescue, {"unlim_collect": [0]}, worker_codes=[1])
    rescue._run()
    unlim = rescue.status()["branches"]["unlim"]
    assert unlim["state"] == "needs_action" and unlim["action"] == "send_logs"
    con = sqlite3.connect(db)
    assert con.execute("SELECT status FROM unlim_files").fetchone()[0] == "pending"
    con.close()
    texts = [e["text"] for e in rescue.status()["events"]]
    assert not any(t.startswith("Готово") for t in texts)


def test_requeued_flag_runs_another_round_instead_of_a_crash(rescue, monkeypatch, tmp_path):
    """«Повторить ошибки» во время скачивания: файлы снова в очереди — это не обрыв шага."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending', local_path TEXT, error TEXT, downloaded_at TIMESTAMP);"
        "INSERT INTO files (remote_path, size) VALUES ('disk:/a.jpg', 1);"
    )
    con.close()
    monkeypatch.setattr(webapp.paths, "DB_FILE", db)
    b = rescue.branches["disk"]
    b.requeued = True
    assert rescue._after_exit(b, EXIT_PARTIAL, (db, "files", "remote_path")) is True
    assert b.requeued is False and b.odd_rounds == 0 and b.state != "needs_action"


def test_retry_failed_restarts_a_finished_branch_and_wakes_a_waiting_one(rescue, monkeypatch):
    """Диск готов, Безлимит ждёт попытки; «Повторить ошибки» запускает Диск снова, а Безлимит будит."""
    sc = Script(monkeypatch, rescue, {"disk": [0, 0], "unlim_collect": [0]}, worker_codes=[EXIT_DEFERRED, 0])
    rescue.retry_every = 3600
    unlim = rescue.branches["unlim"]
    rescue._thread = threading.Thread(target=rescue._run)
    rescue._thread.start()
    deadline = time.time() + 5
    while not (rescue.branches["disk"].state == "done" and unlim.state == "waiting") and time.time() < deadline:
        time.sleep(0.005)
    ok, msg = rescue.requeue_now("all")
    assert ok and "Диск" in msg and "Безлимит" in msg
    rescue._thread.join(10)
    assert not rescue._thread.is_alive()
    assert sc.steps("disk").count(("disk", "download")) == 2
    assert sc.steps("disk").count(("disk", "scan")) == 1  # список свежий — не пересобирается
    assert rescue.state == "done"


# Схема unlim_files до 21.08.2026: ни attempts, ни storage_host, ни preview_path, ни unlim_meta
OLD_UNLIM_SCHEMA = (
    "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, cluster_id TEXT, name TEXT, size INTEGER,"
    " md5 TEXT, mime TEXT, status TEXT DEFAULT 'pending', local_path TEXT, error TEXT,"
    " worker_id INTEGER, assigned_at TIMESTAMP, downloaded_at TIMESTAMP);"
    "CREATE INDEX idx_unlim_status ON unlim_files(status);"
    "CREATE TABLE unlim_clusters (cluster_id TEXT PRIMARY KEY, size INTEGER, fetched INTEGER DEFAULT 0);"
)


def test_unlim_branch_survives_a_database_from_an_old_version(rescue, monkeypatch, tmp_path):
    """Папку обновили поверх старой: база без колонки attempts не роняет ветку «no such column»."""
    db = tmp_path / "unlim_state.db"
    con = sqlite3.connect(db)
    con.executescript(
        OLD_UNLIM_SCHEMA
        + "INSERT INTO unlim_files (file_id, name, size, status, error)"
        " VALUES ('1', 'a.jpg', 1, 'failed', 'boom'), ('2', 'b.jpg', 1, 'downloaded', NULL);"
    )
    con.close()
    monkeypatch.setattr(webapp.paths, "UNLIM_DB_FILE", db)
    Script(monkeypatch, rescue, {"unlim_collect": [0]})
    b = rescue.branches["unlim"]
    assert rescue._unlim_prepare(b) == 0
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(unlim_files)")}
    rows = dict(con.execute("SELECT file_id, status FROM unlim_files").fetchall())
    con.close()
    assert {"attempts", "storage_host", "preview_path"} <= cols
    assert rows == {"1": "pending", "2": "downloaded"}
    assert any("снова в очереди" in line for line in rescue.log)


def test_failures_from_versions_before_1_2_go_back_to_the_queue(tmp_path, monkeypatch):
    """До 1.2 таймаут молчащего сервера записывался ошибкой файла («RequestTimeoutError: …»,
    без метки). Такие 610 файлов у пользователя застряли навсегда: явный старт их не трогал."""
    db = tmp_path / "state.db"
    monkeypatch.setattr(webapp.paths, "DB_FILE", db)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE files (remote_path TEXT PRIMARY KEY, status TEXT, error TEXT)")
    con.executemany("INSERT INTO files VALUES (?, 'failed', ?)", [
        ("a", "RequestTimeoutError: HTTPSConnectionPool(host='s345klg.storage.yandex.net', port=443)"),
        ("b", "InternalServerError: Status code: 500"),
        ("c", "429 TooManyRequests: слишком часто"),
        ("d", "RequestError: Connection aborted"),
        ("e", "download_timeout | Яндекс не отдал файл за 300 с"),
        ("f", "PathNotFound: файла больше нет"),
        ("g", "bad_file | размер не совпал"),
    ])
    con.commit()
    con.close()
    assert webapp.queue_db.reset_transient_disk_failures() == 5
    con = sqlite3.connect(db)
    still = {r[0] for r in con.execute("SELECT remote_path FROM files WHERE status='failed'")}
    con.close()
    assert still == {"f", "g"}  # файл удалён или испорчен — это не сбой, повтор не поможет
