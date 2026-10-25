"""«Проверить скачанные файлы»: строки verify_*, возврат в очередь и защита эндпоинтов.

Люди боятся, что скачанное повреждено. Проверка сверяет каждый файл с Яндексом —
сначала размер, потом MD5 — и печатает итог строкой verify_summary для «Подробнее».
Что не нашлось, возвращает в очередь кнопка «Скачать недостающие», что испорчено —
«Заменить повреждённые»: старая копия не затирается, а уезжает в папку «_заменённые».
"""

import hashlib
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import main
import state
import unlim_download
import utils
import verify
import webapp.diagnostics
import webapp.paths
import webapp.rescue
import webapp.tasks
import webui
from state import FOLDER_SKIP_MARKER, State
from unlim_common import UNLIM_SCHEMA, requeue_verify_problems
from utils import SET_ASIDE_DIRNAME, set_aside


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
    # Сколько скачают «Скачать недостающие» и «Заменить повреждённые»
    assert (summary["missing_bytes"], summary["damaged_bytes"]) == (700, 5000 + 4000)
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


def test_a_file_shared_by_two_rows_belongs_to_the_one_it_matches(tmp_path):
    """Безлимит кладёт фото на первое свободное имя: место удалённого человеком IMG_0001.JPG заняло
    другое фото с тем же именем. Обе записи указывают на один файл — он принадлежит той, с которой
    совпал, другой его «нет в папке». Иначе «Заменить повреждённые» отложило бы чужое фото."""
    photo = tmp_path / "IMG_0001.JPG"
    photo.write_bytes(b"new" * 10)
    old = verify.Item("/photounlim/2019/IMG_0001.JPG", str(photo), 7, _md5(b"old"))
    new = verify.Item("/photounlim/2024/IMG_0001.JPG", str(photo), 30, _md5(b"new" * 10))
    summary, problems = verify.check_files([old, new])
    assert (summary["ok"], summary["missing"], summary["size"]) == (1, 1, 0)
    assert problems == [(old.key, "missing")]
    assert summary["examples"]["missing"] == [{"path": old.key, "size": 7, "taken": True}]
    # Размер совпал у обеих — решает MD5; не совпал MD5 ни у одной — файл ничей, обеим «нет в папке»
    twin = new._replace(key="/photounlim/2020/IMG_0001.JPG", md5=_md5(b"other"))
    assert verify.check_files([twin, new])[1] == [(twin.key, "missing")]
    stranger = twin._replace(key="/photounlim/2021/IMG_0001.JPG")
    assert verify.check_files([twin, stranger])[1] == [(twin.key, "missing"), (stranger.key, "missing")]


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

    # Испорченная копия не затирается: уезжает в «_заменённые», новая ляжет на её место
    assert requeue_verify_problems(str(db), download_dir=str(tmp_path)) == (1, 1, 0)
    c = sqlite3.connect(db)
    row = c.execute("SELECT status, attempts, local_path, downloaded_at FROM unlim_files "
                    "WHERE file_id='/photounlim/b.jpg'").fetchone()
    c.close()
    assert row == ("pending", 0, str(root / "b.jpg"), None)
    assert not (root / "b.jpg").exists()
    assert (tmp_path / SET_ASIDE_DIRNAME / "_unlim" / "b.jpg").read_bytes() == b"x" * 20
    assert requeue_verify_problems(str(db)) == (0, 0, 0)  # список израсходован
    assert requeue_verify_problems(str(tmp_path / "нет.db")) == (0, 0, 0)
    assert not (tmp_path / "нет.db").exists()


def test_unlim_requeue_keeps_a_file_another_row_may_own(tmp_path, monkeypatch, capsys):
    """Человек удалил IMG_0001.JPG, туда легло другое фото с тем же именем, и оно испорчено.
    «Заменить повреждённые» перекладывает файл, только когда все остальные записи этого пути
    проверка назвала пропавшими: иначе файл может быть их."""
    db = tmp_path / "unlim_state.db"
    monkeypatch.setattr(unlim_download, "DB_FILE", str(db))
    root = tmp_path / "_unlim"
    root.mkdir()
    photo = root / "IMG_0001.JPG"
    photo.write_bytes(b"z" * 20)  # размер как у нового фото, содержимое испорчено
    old, new = "/photounlim/2019/IMG_0001.JPG", "/photounlim/2024/IMG_0001.JPG"
    _unlim_db(db, root, [(old, "IMG_0001.JPG", 10, _md5(b"a" * 10), str(photo)),
                         (new, "IMG_0001.JPG", 20, _md5(b"b" * 20), str(photo))])
    assert unlim_download.cmd_verify() == 1
    capsys.readouterr()
    c = sqlite3.connect(db)
    assert sorted(c.execute("SELECT file_id, kind FROM unlim_verify_problems")) == [(old, "missing"), (new, "md5")]
    # Итог устарел: старое фото пропавшим не числится — файл может быть его, не трогаем
    c.execute("DELETE FROM unlim_verify_problems WHERE file_id=?", (old,))
    c.commit()
    assert requeue_verify_problems(str(db), ("size", "md5"), str(tmp_path)) == (1, 0, 0)
    assert photo.read_bytes() == b"z" * 20
    # Свежий итог: старое фото пропало, файл — испорченная копия нового, её можно отложить
    c.execute("UPDATE unlim_files SET status='downloaded'")
    c.executemany("INSERT INTO unlim_verify_problems (file_id, kind) VALUES (?, ?)", [(old, "missing"), (new, "md5")])
    c.commit()
    c.close()
    assert requeue_verify_problems(str(db), ("size", "md5"), str(tmp_path)) == (1, 1, 0)
    assert not photo.exists()
    assert (tmp_path / SET_ASIDE_DIRNAME / "_unlim" / "IMG_0001.JPG").read_bytes() == b"z" * 20
    # Недостающее — своей кнопкой
    assert requeue_verify_problems(str(db), ("missing",), str(tmp_path)) == (1, 0, 0)


# ── «Скачать недостающие» и «Заменить повреждённые» на стороне Диска ────────


def test_requeue_returns_only_checked_problems_to_the_queue(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    _disk_db(tmp_path / "state.db", root)
    with State(str(tmp_path / "state.db")) as st:
        conn = st._conn()
        conn.execute("UPDATE files SET storage_host='s1.storage', error=NULL WHERE remote_path='disk:/b.jpg'")
        # Устаревшая запись о снятом выбором файле в очередь его не вернёт
        st.save_verify_problems([("disk:/b.jpg", "missing"), ("disk:/c.jpg", "md5"), ("disk:/d.jpg", "missing")])
        assert st.requeue_verify_problems() == (2, 0, 0)
        rows = dict((r[0], tuple(r[1:])) for r in conn.execute(
            "SELECT remote_path, status, error, storage_host, downloaded_at, local_path IS NOT NULL FROM files"))
        assert rows["disk:/b.jpg"] == ("pending", None, None, None, 1)
        assert rows["disk:/c.jpg"] == ("pending", None, None, None, 1)
        assert rows["disk:/a.jpg"][0] == "downloaded"
        assert rows["disk:/d.jpg"][:2] == ("skipped", FOLDER_SKIP_MARKER)
        assert st.requeue_verify_problems() == (0, 0, 0)
        # Скачанные и найденные при докачке проверяются, снятые выбором — нет
        assert [r["remote_path"] for r in st.get_all_downloaded()] == ["disk:/a.jpg"]


def _locked(folder, path):
    raise PermissionError(13, "Файл занят другой программой")


def test_replacing_damaged_files_sets_the_old_copy_aside(tmp_path, monkeypatch):
    """«Заменить повреждённые» не затирает испорченную копию — вдруг это правка самого человека
    (повёрнутое фото, исправленный документ): она уезжает в «_заменённые». Каждая кнопка — своё."""
    root = tmp_path / "downloads"
    root.mkdir()
    _disk_db(tmp_path / "state.db", root)
    with State(str(tmp_path / "state.db")) as st:
        conn = st._conn()

        def status():
            return conn.execute("SELECT status FROM files WHERE remote_path='disk:/c.jpg'").fetchone()[0]

        st.save_verify_problems([("disk:/b.jpg", "missing"), ("disk:/c.jpg", "md5")])
        assert st.requeue_verify_problems(("missing",), str(root)) == (1, 0, 0)
        assert (root / "c.jpg").exists() and status() == "skipped"  # повреждённый ждёт своей кнопки
        assert [tuple(r) for r in conn.execute("SELECT remote_path, kind FROM verify_problems")] == [
            ("disk:/c.jpg", "md5")]

        # Файл открыт в другой программе: остаётся на месте, в итоге проверки и вне очереди
        monkeypatch.setattr(state, "set_aside", _locked)
        assert st.requeue_verify_problems(("size", "md5"), str(root)) == (0, 0, 1)
        assert status() == "skipped" and conn.execute("SELECT COUNT(*) FROM verify_problems").fetchone()[0] == 1

        monkeypatch.setattr(state, "set_aside", utils.set_aside)
        assert st.requeue_verify_problems(("size", "md5"), str(root)) == (1, 1, 0)
        assert status() == "pending" and not (root / "c.jpg").exists()
        assert (root / SET_ASIDE_DIRNAME / "c.jpg").read_bytes() == b"c" * 30


def test_set_aside_keeps_the_path_and_never_overwrites(tmp_path):
    root = tmp_path / "downloads"
    (root / "Фото").mkdir(parents=True)
    for n in (1, 2):
        (root / "Фото" / "a.jpg").write_bytes(b"%d" % n)
        assert set_aside(str(root), str(root / "Фото" / "a.jpg"))
    aside = root / SET_ASIDE_DIRNAME / "Фото"
    assert (aside / "a.jpg").read_bytes() == b"1" and (aside / "a (2).jpg").read_bytes() == b"2"
    # Перекладывать нечего: файла нет, на его месте папка, он вне папки скачивания (её сменили)
    (tmp_path / "elsewhere.jpg").write_bytes(b"x")
    assert set_aside(str(root), str(root / "нет.jpg")) is None
    assert set_aside(str(root), str(root / "Фото")) is None
    assert set_aside(str(root), str(tmp_path / "elsewhere.jpg")) is None
    assert (tmp_path / "elsewhere.jpg").exists() and (root / "Фото").is_dir()


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
    assert res == {"ok": True, "count": 1, "moved": 0, "kept": 0,
                   "msg": "1 файл снова в очереди — нажмите «Скачать сейчас»"}
    again = client.post("/api/verify/requeue", json={"source": "disk"}).json()
    assert again["ok"] is False and again["count"] == 0
    assert client.post("/api/verify/requeue", json={"source": "unlim"}).json()["count"] == 0


def test_requeue_endpoint_replaces_damaged_files_separately(client, tmp_path, monkeypatch):
    root = tmp_path / "downloads"  # папка скачивания из conftest
    root.mkdir()
    _disk_db(webapp.paths.DB_FILE, root)
    with State(str(webapp.paths.DB_FILE)) as st:
        st.save_verify_problems([("disk:/b.jpg", "missing"), ("disk:/c.jpg", "md5")])
    res = client.post("/api/verify/requeue", json={"source": "disk", "what": "damaged"}).json()
    assert res["ok"] and (res["count"], res["moved"], res["kept"]) == (1, 1, 0)
    assert res["msg"].endswith("Старые копии лежат в папке «_заменённые» внутри папки скачивания")
    assert (root / SET_ASIDE_DIRNAME / "c.jpg").exists()
    res = client.post("/api/verify/requeue", json={"source": "disk", "what": "missing"}).json()
    assert (res["ok"], res["count"], res["moved"]) == (True, 1, 0)
    assert client.post("/api/verify/requeue", json={"source": "disk", "what": "всё"}).status_code == 422

    # Копию не получилось переложить — в очередь она не вернулась, человеку сказано почему
    with State(str(webapp.paths.DB_FILE)) as st:
        st.save_verify_problems([("disk:/a.jpg", "size")])
    monkeypatch.setattr(state, "set_aside", _locked)
    res = client.post("/api/verify/requeue", json={"source": "disk", "what": "damaged"}).json()
    assert res["ok"] is False and res["kept"] == 1
    assert res["msg"].startswith("Не получилось переложить 1 файл: он открыт в другой программе")


def test_sorting_and_checking_never_run_together(client, monkeypatch, tmp_path):
    """Сортировка переносит файлы: проверка посреди неё нашла бы их пропавшими, а «Заменить
    повреждённые» переложило бы файл, который сортировка как раз переносит."""
    State(str(webapp.paths.DB_FILE)).close()
    (tmp_path / "pile").mkdir()
    sorting = _task("organize", ["organize.py", "--input", str(tmp_path / "pile"), "--action", "move"])
    monkeypatch.setitem(webapp.tasks.tm._tasks, "organize", sorting)
    for url in ("/api/verify/start", "/api/verify/requeue"):
        res = client.post(url, json={"source": "disk"}).json()
        assert res["ok"] is False and "сортировки" in res["msg"], url
    monkeypatch.delitem(webapp.tasks.tm._tasks, "organize")
    monkeypatch.setitem(webapp.tasks.tm._tasks, "disk", _task("disk", ["main.py", "verify"]))
    res = client.post("/api/organize", json={"input": str(tmp_path / "pile"), "action": "move"}).json()
    assert res["ok"] is False and "идёт проверка скачанных файлов" in res["msg"]


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
