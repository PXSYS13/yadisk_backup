"""Плюмбинг Web UI для «Режима спасения»: отложенные файлы, честная диагностика, быстрый стоп."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import webui


@pytest.mark.parametrize(
    "cmd,kind",
    [
        (["main.py", "scan"], "disk_scan"),
        (["main.py", "download", "--workers", "4"], "disk_download"),
        (["main.py", "retry-failed"], "disk_download"),
        (["unlim_download.py", "--collect"], "unlim_collect"),
        (["unlim_worker.py", "--id", "1"], "unlim_worker"),
        (["organize.py", "--input", "x"], "organize"),
    ],
)
def test_task_kind(cmd, kind):
    assert webui.task_kind(cmd) == kind


def test_db_stats_counts_deferred_files_and_hosts(tmp_path):
    db = tmp_path / "unlim_state.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, name TEXT, size INTEGER,"
        " status TEXT, error TEXT, storage_host TEXT, downloaded_at TIMESTAMP);"
    )
    con.executemany(
        "INSERT INTO unlim_files (file_id, size, status, error, storage_host) VALUES (?, ?, ?, ?, ?)",
        [
            ("a", 10, "pending", "storage_down | молчит", "s1klg"),
            ("b", 20, "pending", "storage_down | молчит", "s1klg"),
            ("c", 30, "pending", "storage_down | молчит", "s2klg"),
            ("d", 40, "pending", None, None),
            ("e", 50, "downloaded", None, None),
        ],
    )
    con.commit()
    con.close()
    st = webui.db_stats(db, "unlim_files", "file_id")
    assert st["pending"] == 4  # отложенные — часть очереди
    assert st["deferred"] == 3 and st["bytes_deferred"] == 60
    assert st["deferred_hosts"] == 2


class _Task:
    def __init__(self, lines, finished_ago=0.0, alive=False):
        import time
        self.log = list(lines)
        self.finished_at = None if alive else time.time() - finished_ago
        self._alive = alive

    def alive(self):
        return self._alive


def _diag(monkeypatch, tasks):
    monkeypatch.setattr(webui.tm, "_tasks", tasks)
    return webui.diagnose_tasks()


def test_single_refusal_does_not_claim_yandex_refuses(monkeypatch):
    diag = _diag(monkeypatch, {"unlim_w1": _Task(["[w1] ⏳ Файл a.jpg отложен", "refused | HTTP 403 на один файл"])})
    assert diag["kind"] != "bulk_disabled"
    three = ["refused | HTTP 403"] * 3
    assert _diag(monkeypatch, {"unlim_w1": _Task(three)})["kind"] == "bulk_disabled"
    strong = ["bulk_unavailable | Яндекс 5 раз подряд не отдал файл"]
    assert _diag(monkeypatch, {"unlim_w1": _Task(strong)})["kind"] == "bulk_disabled"


def test_sorting_file_names_do_not_become_yandex_banners(monkeypatch):
    """Сортировка печатает имена файлов: «timeout_2024.docx» — не таймаут Яндекса."""
    summary = ('organize_summary | {"folders": 2, "examples": [{"from": "Отчёты/timeout_2024.docx", '
               '"to": "Документы/timeout_2024.docx"}]}')
    sorting = _Task([summary, "  err: D:/YAD-bak/local_disk.txt: [Errno 13] Permission denied"], alive=True)
    assert _diag(monkeypatch, {webui.SLOT_ORGANIZE: sorting})["kind"] is None


def test_old_finished_tasks_do_not_keep_banners(monkeypatch):
    old = _Task(["auth_expired | КРИТИЧЕСКАЯ ОШИБКА"], finished_ago=webui.DIAG_RECENT_SEC + 60)
    assert _diag(monkeypatch, {"unlim_w1": old})["kind"] is None
    live = _Task(["auth_expired | КРИТИЧЕСКАЯ ОШИБКА"], alive=True)
    assert _diag(monkeypatch, {"unlim_w1": live})["kind"] == "auth_expired"


def test_stop_matching_terminates_everyone_before_waiting(monkeypatch):
    events = []

    class T:
        def __init__(self, name):
            self.name = name
            self.running = True

        def alive(self):
            return self.running

        def terminate(self):
            events.append(f"terminate {self.name}")
            self.running = False

        def wait_or_kill(self, timeout=5.0):
            events.append(f"wait {self.name}")

    monkeypatch.setattr(webui.tm, "_tasks", {"unlim_w1": T("w1"), "unlim_w2": T("w2"), "disk": T("disk")})
    stopped = webui.tm.stop_matching("unlim_w")
    assert len(stopped) == 2
    assert events == ["terminate w1", "terminate w2", "wait w1", "wait w2"]


def test_status_exposes_exit_code_texts(monkeypatch):
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    codes = client.get("/api/status").json()["exit_codes"]
    assert set(codes) >= {"1", "2", "3", "4", "5"}
    assert "Яндекс" in codes["5"]


def test_disk_lines_never_claim_that_yandex_refuses_unlim(monkeypatch):
    """Отчёт пользователя: Безлимит не запускался, а баннер писал «Яндекс отказывает в
    скачивании безлимита» — из-за 29 строк Диска «превью …: refused | HTTP 434»."""
    preview = "10:39:55  2026-10-15 10:39:55 | WARNING | превью disk:/Photos/a.JPG: refused | HTTP 434"
    diag = _diag(monkeypatch, {"disk": _Task([preview] * 29 + ["refused | HTTP 403"] * 5, alive=True)})
    assert diag["kind"] != "bulk_disabled"
    assert diag["counts"]["bulk_disabled"] == 0
    # Превью не объясняет и сбои Яндекса: таймаут превью — не «Яндекс отвечает медленно»
    slow = "WARNING | превью disk:/a.JPG: не скачалось — Read timed out. (read timeout=15.0) server_50x"
    diag = _diag(monkeypatch, {"disk": _Task([slow] * 10, alive=True)})
    assert diag["kind"] is None
    # А у воркера безлимита те же отказы — по-прежнему повод для объяснения
    diag = _diag(monkeypatch, {"unlim_w1": _Task(["[w1] refused | HTTP 403"] * 3, alive=True)})
    assert diag["kind"] == "bulk_disabled"
