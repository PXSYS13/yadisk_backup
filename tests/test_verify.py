"""«Проверить скачанные файлы»: строки verify_*, возврат в очередь и защита эндпоинтов.

Люди боятся, что скачанное повреждено. Проверка сверяет каждый файл с Яндексом —
сначала размер, потом MD5 — и печатает итог строкой verify_summary для «Подробнее».
Что не нашлось или испорчено, кнопка «Скачать заново» возвращает в очередь.
"""

import hashlib
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import main
import unlim_download
import verify
import webapp.diagnostics
import webapp.paths
import webapp.rescue
import webapp.tasks
import webui
from state import FOLDER_SKIP_MARKER, State
from unlim_common import UNLIM_SCHEMA, requeue_verify_problems


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


@pytest.fixture()
def files(tmp_path):
    """Скачанное: целый файл, удалённый, обрезанный, испорченный и файл без MD5 от Яндекса."""
    root = tmp_path / "downloads"
    root.mkdir()
    good, cut, bad, plain = b"a" * 3000, b"b" * 5000, b"c" * 4000, b"d" * 10
    (root / "ok.jpg").write_bytes(good)
    (root / "cut.mp4").write_bytes(cut[:1000])
    (root / "bad.jpg").write_bytes(b"x" + bad[1:])
    (root / "plain.txt").write_bytes(plain)
    return [
        verify.Item("disk:/ok.jpg", str(root / "ok.jpg"), len(good), _md5(good)),
        verify.Item("disk:/Фото/нет.jpg", str(root / "Фото" / "нет.jpg"), 700, _md5(b"gone")),
        verify.Item("disk:/cut.mp4", str(root / "cut.mp4"), len(cut), _md5(cut)),
        verify.Item("disk:/bad.jpg", str(root / "bad.jpg"), len(bad), _md5(bad).upper()),
        verify.Item("disk:/plain.txt", str(root / "plain.txt"), len(plain), None),
    ]


# ── сама проверка и строка итога ────────────────────────────────────────────


def test_summary_counts_each_kind_and_lists_examples(files):
    seen = []
    summary, problems = verify.check_files(files, seen.append)
    assert {k: summary[k] for k in ("checked", "ok", "missing", "size", "md5", "unreadable", "no_md5")} == {
        "checked": 5, "ok": 2, "missing": 1, "size": 1, "md5": 1, "unreadable": 0, "no_md5": 1}
    assert problems == [("disk:/Фото/нет.jpg", "missing"), ("disk:/cut.mp4", "size"), ("disk:/bad.jpg", "md5")]
    assert summary["examples"]["size"] == [{"path": "disk:/cut.mp4", "size": 5000, "local_size": 1000}]
    assert summary["examples"]["missing"] == [{"path": "disk:/Фото/нет.jpg", "size": 700}]
    assert "unreadable" not in summary["examples"]
    assert summary["bad_bytes"] == 700 + 5000 + 4000 and summary["all_missing"] is False
    # Сначала размеры всех файлов, потом MD5 только у двух совпавших по размеру (у plain.txt MD5 нет)
    stages = [p["stage"] for p in seen]
    assert stages[:5] == ["size"] * 5 and set(stages[5:]) == {"md5"}
    last = seen[-1]
    assert (last["done"], last["total"], last["bytes_done"], last["bytes_total"]) == (2, 2, 7000, 7000)
    assert last["found"] == 3


def test_summary_line_is_one_json_line_that_keeps_cyrillic(files):
    summary, _ = verify.check_files(files)
    line = verify.summary_line(summary)
    assert line.startswith("verify_summary | ") and "\n" not in line
    assert "Фото/нет.jpg" in line  # кириллица как есть, а не Ф
    assert json.loads(line.split(" | ", 1)[1]) == summary
    assert verify.progress_line({"stage": "size", "done": 1, "total": 2, "found": 0}) == (
        'verify_progress | {"stage": "size", "done": 1, "total": 2, "found": 0}')


def test_examples_are_capped_but_counts_are_not(tmp_path):
    items = [verify.Item(f"disk:/{i}.jpg", str(tmp_path / f"{i}.jpg"), 1, None) for i in range(60)]
    summary, problems = verify.check_files(items)
    assert summary["missing"] == len(problems) == 60
    assert len(summary["examples"]["missing"]) == verify.EXAMPLES_PER_KIND == 50
    # Ни одного файла на месте — скорее всего, отключён диск: интерфейс предупредит
    assert summary["all_missing"] is True


def test_unreadable_files_are_reported_but_not_requeued(files, monkeypatch):
    def locked(path, on_chunk):
        raise PermissionError(13, "Файл занят другой программой")

    monkeypatch.setattr(verify, "_md5", locked)
    summary, problems = verify.check_files(files)
    assert summary["unreadable"] == 2 and summary["md5"] == 0
    assert summary["examples"]["unreadable"][0]["error"] == "Файл занят другой программой"
    assert [k for _, k in problems] == ["missing", "size"]


def test_rows_without_local_path_are_unchecked_and_odd_md5_is_size_only(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"12345")
    items = [verify.Item("/photounlim/a.jpg", str(tmp_path / "a.jpg"), None, "etag-not-md5"),
             verify.Item("/photounlim/b.jpg", None, 5, None)]
    summary, problems = verify.check_files(items)
    assert (summary["checked"], summary["unchecked"], summary["ok"], summary["no_md5"]) == (1, 1, 1, 1)
    assert problems == []


def test_progress_lines_for_webui_are_throttled(capsys):
    clock = iter([100.0, 100.5, 101.0, 102.5, 103.0])
    progress = verify.Progress(tty=False, clock=lambda: next(clock))
    for stage, done in (("size", 1), ("size", 2), ("md5", 0), ("md5", 1), ("md5", 2)):
        progress({"stage": stage, "done": done})
    out = [json.loads(ln.split(" | ", 1)[1]) for ln in capsys.readouterr().out.splitlines()]
    # Начало каждого этапа — сразу, дальше не чаще раза в 2 секунды
    assert [(p["stage"], p["done"]) for p in out] == [("size", 1), ("md5", 0), ("md5", 2)]


# ── main.py verify и unlim_download.py --verify ─────────────────────────────


def _disk_db(path, root):
    """state.db: 2 скачанных файла (один пропал), 1 найден при докачке, 1 снят выбором папок."""
    (root / "a.jpg").write_bytes(b"a" * 10)
    (root / "c.jpg").write_bytes(b"c" * 30)
    with State(str(path)) as st:
        for name, data in (("a.jpg", b"a" * 10), ("b.jpg", b"b" * 20), ("c.jpg", b"c" * 30), ("d.jpg", b"d")):
            st.mark_pending(f"disk:/{name}", len(data), _md5(data))
        st.mark_downloaded("disk:/a.jpg", str(root / "a.jpg"))
        st.mark_downloaded("disk:/b.jpg", str(root / "b.jpg"))
        st.mark_skipped("disk:/c.jpg", str(root / "c.jpg"))
        st.assign_local_paths(str(root))
        st._conn().execute("UPDATE files SET status='skipped', error=? WHERE remote_path='disk:/d.jpg'",
                           (FOLDER_SKIP_MARKER,))


def test_main_verify_prints_summary_last_and_remembers_problems(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "state.db")
    monkeypatch.setenv("YADISK_WEBUI", "1")
    root = tmp_path / "downloads"
    root.mkdir()
    _disk_db(main.DB_FILE, root)
    assert main.cmd_verify({}, None) == 1
    lines = capsys.readouterr().out.strip().splitlines()
    summary = json.loads(lines[-1].removeprefix("verify_summary | "))
    # Снятый выбором папок d.jpg не скачивался — его не проверяем и пропавшим не считаем
    assert (summary["checked"], summary["ok"], summary["missing"]) == (3, 2, 1)
    assert any(ln.startswith("verify_progress | ") for ln in lines)
    assert not any("b.jpg" in ln for ln in lines[:-1])  # примеры под Web UI — только в строке итога
    with State(str(main.DB_FILE)) as st:
        assert [tuple(r) for r in st._conn().execute("SELECT remote_path, kind FROM verify_problems")] == [
            ("disk:/b.jpg", "missing")]


def test_main_verify_without_downloads_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "state.db")
    assert main.cmd_verify({}, None) == 0
    out = capsys.readouterr().out
    assert "проверять нечего" in out
    assert json.loads(out.strip().splitlines()[-1].split(" | ", 1)[1])["checked"] == 0


def _unlim_db(path, root, rows):
    c = sqlite3.connect(path)
    c.executescript(UNLIM_SCHEMA)
    c.executemany("INSERT INTO unlim_files (file_id, name, size, md5, status, local_path, attempts, downloaded_at) "
                  "VALUES (?, ?, ?, ?, 'downloaded', ?, 2, CURRENT_TIMESTAMP)", rows)
    c.commit()
    c.close()


def test_unlim_verify_and_requeue(tmp_path, monkeypatch, capsys):
    db = tmp_path / "unlim_state.db"
    monkeypatch.setattr(unlim_download, "DB_FILE", str(db))
    root = tmp_path / "_unlim"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"a" * 10)
    (root / "b.jpg").write_bytes(b"x" * 20)
    _unlim_db(db, root, [
        ("/photounlim/a.jpg", "a.jpg", 10, _md5(b"a" * 10), str(root / "a.jpg")),
        ("/photounlim/b.jpg", "b.jpg", 20, _md5(b"b" * 20), str(root / "b.jpg")),
        ("/photounlim/c.jpg", "c.jpg", 0, None, None),  # старая версия не записала путь
    ])
    assert unlim_download.cmd_verify() == 1
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1].split(" | ", 1)[1])
    assert (summary["checked"], summary["ok"], summary["md5"], summary["unchecked"]) == (2, 1, 1, 1)

    assert requeue_verify_problems(str(db)) == 1
    c = sqlite3.connect(db)
    row = c.execute("SELECT status, attempts, local_path, downloaded_at FROM unlim_files "
                    "WHERE file_id='/photounlim/b.jpg'").fetchone()
    c.close()
    # local_path остаётся: воркер положит новую копию на место испорченной
    assert row == ("pending", 0, str(root / "b.jpg"), None)
    assert requeue_verify_problems(str(db)) == 0  # список израсходован
    assert requeue_verify_problems(str(tmp_path / "нет.db")) == 0
    assert not (tmp_path / "нет.db").exists()


# ── «Скачать заново» на стороне Диска ───────────────────────────────────────


def test_requeue_returns_only_checked_problems_to_the_queue(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    _disk_db(tmp_path / "state.db", root)
    with State(str(tmp_path / "state.db")) as st:
        conn = st._conn()
        conn.execute("UPDATE files SET storage_host='s1.storage', error=NULL WHERE remote_path='disk:/b.jpg'")
        # Устаревшая запись о снятом выбором файле в очередь его не вернёт
        st.save_verify_problems([("disk:/b.jpg", "missing"), ("disk:/c.jpg", "md5"), ("disk:/d.jpg", "missing")])
        assert st.requeue_verify_problems() == 2
        rows = dict((r[0], tuple(r[1:])) for r in conn.execute(
            "SELECT remote_path, status, error, storage_host, downloaded_at, local_path IS NOT NULL FROM files"))
        assert rows["disk:/b.jpg"] == ("pending", None, None, None, 1)
        assert rows["disk:/c.jpg"] == ("pending", None, None, None, 1)
        assert rows["disk:/a.jpg"][0] == "downloaded"
        assert rows["disk:/d.jpg"][:2] == ("skipped", FOLDER_SKIP_MARKER)
        assert st.requeue_verify_problems() == 0
        # Скачанные и найденные при докачке проверяются, снятые выбором — нет
        assert [r["remote_path"] for r in st.get_all_downloaded()] == ["disk:/a.jpg"]


# ── Web UI: эндпоинты и их защита ───────────────────────────────────────────


class _Proc:
    """Процесс задачи без вывода: жив, пока alive=True."""

    def __init__(self, alive=True, lines=()):
        self.stdout = list(lines)
        self.alive = alive

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        return 0


def _task(slot, cmd, alive=True, lines=()):
    t = webapp.tasks.Task(slot, _Proc(alive, lines), cmd=cmd)
    t.thread.join(1)
    return t


@pytest.fixture()
def client():
    return TestClient(webui.app, headers={"Host": "localhost:8000"})


def test_verify_start_only_when_nothing_downloads(client, monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(webapp.tasks.tm, "start", lambda slot, script, args: (started.append((slot, script, args)),
                                                                            (True, "Запущено"))[1])
    # Пока программа ничего не скачала — проверять нечего
    assert client.post("/api/verify/start", json={"source": "disk"}).json()["ok"] is False
    State(str(webapp.paths.DB_FILE)).close()
    sqlite3.connect(webapp.paths.UNLIM_DB_FILE).close()

    monkeypatch.setattr(webapp.rescue.rescue, "is_running", lambda: True)
    r = client.post("/api/verify/start", json={"source": "disk"})
    assert r.status_code == 409 and "скачивание остановлено" in r.json()["detail"]
    assert client.post("/api/verify/requeue", json={"source": "disk"}).status_code == 409
    assert started == []

    monkeypatch.setattr(webapp.rescue.rescue, "is_running", lambda: False)
    assert client.post("/api/verify/start", json={"source": "disk"}).json() == {"ok": True, "msg": "Проверяю скачанные файлы"}
    assert client.post("/api/verify/start", json={"source": "unlim"}).json()["ok"] is True
    assert started == [("disk", "main.py", ["verify"]), ("unlim_collect", "unlim_download.py", ["--verify"])]
    assert client.post("/api/verify/start", json={"source": "all"}).status_code == 422


def test_running_verify_blocks_downloads_and_relisting(client, monkeypatch):
    State(str(webapp.paths.DB_FILE)).close()
    task = _task("disk", ["main.py", "verify"])
    assert task.kind == "disk_verify"
    monkeypatch.setitem(webapp.tasks.tm._tasks, "disk", task)
    monkeypatch.setattr(webapp.rescue.rescue, "start", lambda **kw: pytest.fail("скачивание поверх проверки"))
    for url, body in (("/api/rescue/start", None), ("/api/rescue/retry_failed", {"source": "all"}),
                      ("/api/rescue/relist", {"source": "all"})):
        res = client.post(url, json=body or {}).json()
        assert res["ok"] is False and "идёт проверка скачанных файлов" in res["msg"], url
    assert client.post("/api/verify/start", json={"source": "disk"}).json()["msg"] == "Проверка уже идёт"
    assert client.post("/api/verify/requeue", json={"source": "disk"}).json()["msg"] == "Дождитесь конца проверки"
    # Безлимит проверке Диска не мешает, а «Остановить проверку» гасит только её
    assert client.post("/api/verify/stop", json={"source": "unlim"}).json()["ok"] is False
    assert client.post("/api/verify/stop", json={"source": "disk"}).json() == {"ok": True, "msg": "Проверка остановлена"}
    assert task.was_stopped


def test_verify_start_waits_for_a_list_being_collected(client, monkeypatch):
    sqlite3.connect(webapp.paths.UNLIM_DB_FILE).close()
    monkeypatch.setitem(webapp.tasks.tm._tasks, "unlim_collect", _task("unlim_collect", ["unlim_download.py", "--collect"]))
    res = client.post("/api/verify/start", json={"source": "unlim"}).json()
    assert res["ok"] is False and "собирается список" in res["msg"]
    # Сбор списка — не проверка: остановить его этой кнопкой нельзя
    assert client.post("/api/verify/stop", json={"source": "unlim"}).json()["ok"] is False


def test_requeue_endpoint_counts_files(client, tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    _disk_db(webapp.paths.DB_FILE, root)
    with State(str(webapp.paths.DB_FILE)) as st:
        st.save_verify_problems([("disk:/b.jpg", "missing")])
    res = client.post("/api/verify/requeue", json={"source": "disk"}).json()
    assert res == {"ok": True, "count": 1,
                   "msg": "1 файл снова в очереди. Нажмите «Продолжить» на Главной — программа скачает их заново"}
    again = client.post("/api/verify/requeue", json={"source": "disk"}).json()
    assert again["ok"] is False and again["count"] == 0
    assert client.post("/api/verify/requeue", json={"source": "unlim"}).json()["count"] == 0


def test_verify_output_never_explains_yandex_problems(monkeypatch):
    """Имена файлов в итоге проверки («timeout_2024.docx») — не сбои Яндекса."""
    line = verify.summary_line({"examples": {"missing": [{"path": "disk:/timeout_2024 auth_expired.docx"}]}})
    task = _task("disk", ["main.py", "verify"], alive=False, lines=[line + "\n"])
    task.finished_at = time.time()
    monkeypatch.setattr(webapp.tasks.tm, "_tasks", {"disk": task})
    assert task.log and webapp.diagnostics.diagnose_tasks()["kind"] is None
    # Та же строка от скачивания — уже объяснение для человека
    download = _task("disk", ["main.py", "download"], alive=False, lines=[line + "\n"])
    monkeypatch.setattr(webapp.tasks.tm, "_tasks", {"disk": download})
    assert webapp.diagnostics.diagnose_tasks()["kind"] == "auth_expired"
    assert webapp.tasks.task_kind(["unlim_download.py", "--verify"]) == "unlim_verify"
    assert webapp.tasks.task_kind(["unlim_download.py", "--collect"]) == "unlim_collect"
