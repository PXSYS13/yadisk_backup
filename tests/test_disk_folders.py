"""Только выбранные папки Диска и общие папки: настройка, сбор списка, очередь, дерево."""

import argparse
import sqlite3
import time
from types import SimpleNamespace

import pytest
import yadisk
from dotenv import dotenv_values, set_key
from fastapi.testclient import TestClient

import downloader
import main
import webui
from state import CATEGORY_SKIP_MARKER, FOLDER_SKIP_MARKER, SELECTION_MARKERS, State
from utils import (
    MAX_DISK_FOLDERS,
    decode_disk_folders,
    encode_disk_folders,
    normalize_disk_folders,
    path_in_folders,
)


class Script:
    """Подменяет запуск задач спасения: коды выхода по слотам, последний повторяется."""

    def __init__(self, monkeypatch, rescue, codes):
        self.codes = {slot: list(seq) for slot, seq in codes.items()}
        self.started = []
        monkeypatch.setattr(webui.tm, "start", self._start)
        monkeypatch.setattr(rescue, "_wait_task", self._wait)
        monkeypatch.setattr(rescue, "_wait_workers", lambda ids: [0 for _ in ids])

    def _start(self, slot, script, args):
        self.started.append((slot, script, tuple(args)))
        return True, "ok"

    def _wait(self, slot, on_finish_msg=""):
        seq = self.codes.setdefault(slot, [0])
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def steps(self, slot=None):
        return [(s, a[0] if a else "") for s, _sc, a in self.started if slot in (None, s)]


# ── Выбор папок: нормализация и хранение в .env ─────────────────────────────


def test_normalize_disk_folders_cleans_and_drops_nested():
    got = normalize_disk_folders(["/Фото/", "disk:/Фото/2020", "Работа", "disk:/Фото2", "disk:/a//b", " "])
    assert got == ["disk:/a/b", "disk:/Работа", "disk:/Фото", "disk:/Фото2"]
    assert normalize_disk_folders(["disk:/A", "disk:/A"]) == ["disk:/A"]


@pytest.mark.parametrize("bad", [["/"], ["disk:/"], ["disk:/Корзина/x"], ["/Trash"],
                                 ["disk:/a\nb"], [123], ["disk:/" + "x" * 500]])
def test_normalize_disk_folders_refuses_with_plain_text(bad):
    with pytest.raises(ValueError) as e:
        normalize_disk_folders(bad)
    assert str(e.value) and "Error" not in str(e.value)


def test_normalize_disk_folders_caps_the_count():
    with pytest.raises(ValueError, match="не больше"):
        normalize_disk_folders([f"disk:/f{i}" for i in range(MAX_DISK_FOLDERS + 1)])


def test_path_in_folders_respects_name_boundaries():
    assert path_in_folders("disk:/Фото/a.jpg", ["disk:/Фото"])
    assert path_in_folders("disk:/Фото", ["disk:/Фото"])
    assert not path_in_folders("disk:/Фото2/a.jpg", ["disk:/Фото"])


def test_disk_folders_survive_the_env_file(tmp_path):
    folders = normalize_disk_folders([
        "disk:/Отчёты, 2024", "disk:/Договоры 'старые'", 'disk:/Цитаты "умные"', "disk:/a\\b\\",
        "disk:/${HOME}", "disk:/цена $5 # скидка", "disk:/конец\\",
    ])
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    value = encode_disk_folders(folders)
    assert "${" not in value  # python-dotenv подставил бы переменную окружения
    set_key(str(env), "BACKUP_DISK_FOLDERS", value, quote_mode="always")
    assert decode_disk_folders(dotenv_values(env)["BACKUP_DISK_FOLDERS"]) == folders


@pytest.mark.parametrize("raw", [None, "", "not json", '{"a": 1}', "[]", '["/"]', "[1, 2]"])
def test_broken_folder_setting_means_whole_disk(raw):
    assert decode_disk_folders(raw) is None


# ── state: строки, снятые выбором ───────────────────────────────────────────


def test_rows_removed_by_selection_get_fresh_size_and_md5_but_stay_skipped(tmp_path):
    with State(str(tmp_path / "state.db")) as st:
        st.mark_pending_batch([("disk:/A/x.jpg", 1, "a" * 32), ("disk:/B/y.jpg", 1, "b" * 32)])
        conn = st._conn()
        conn.execute("UPDATE files SET status='skipped', error=? WHERE remote_path='disk:/A/x.jpg'",
                     (FOLDER_SKIP_MARKER,))
        conn.execute("UPDATE files SET status='skipped', error=NULL WHERE remote_path='disk:/B/y.jpg'")
        st.mark_pending_batch([("disk:/A/x.jpg", 5, "c" * 32), ("disk:/B/y.jpg", 5, "d" * 32)])
        rows = {r["remote_path"]: r for r in conn.execute("SELECT * FROM files")}
    assert (rows["disk:/A/x.jpg"]["status"], rows["disk:/A/x.jpg"]["size"]) == ("skipped", 5)
    assert rows["disk:/A/x.jpg"]["md5"] == "c" * 32
    # Дубликат или уже лежащий на месте файл — не трогаем
    assert (rows["disk:/B/y.jpg"]["size"], rows["disk:/B/y.jpg"]["md5"]) == (1, "b" * 32)


# ── downloader: обход выбранных папок и общих папок ─────────────────────────


def _dir(path, share=None):
    return SimpleNamespace(type="dir", path=path, name=path.rsplit("/", 1)[-1], size=None, md5=None, share=share)


def _file(path, size=1):
    return SimpleNamespace(type="file", path=path, name=path.rsplit("/", 1)[-1], size=size, md5=None, share=None)


def _share(owned):
    return SimpleNamespace(is_owned=owned, is_root=True, rights="rw")


class FakeDisk:
    """Диск в памяти: папка → содержимое; плоский список — отдельно (как get_files)."""

    def __init__(self, tree, flat=None):
        self.tree = tree
        self.flat = flat if flat is not None else []
        self.listed = []

    def listdir(self, path, **kw):
        self.listed.append(path)
        if path not in self.tree:
            raise yadisk.exceptions.PathNotFoundError("нет такой папки")
        items = self.tree[path]
        cap = kw.get("max_items")
        return iter(items[:cap] if cap else items)

    def get_files(self, offset=0, limit=200, max_items=None, **kw):
        return iter(self.flat[offset:offset + limit])

    def close(self):
        pass


class FakeState:
    def __init__(self):
        self.rows = []

    def mark_pending_batch(self, items):
        self.rows += [path for path, _s, _m in items]


def test_selected_root_is_walked_alone():
    disk = FakeDisk({
        "disk:/A": [_file("disk:/A/x.jpg"), _dir("disk:/A/B")],
        "disk:/A/B": [_file("disk:/A/B/y.jpg")],
        "disk:/C": [_file("disk:/C/z.jpg")],
    })
    st = FakeState()
    assert downloader.scan_tree(disk, st, root="disk:/A") == (2, 2)
    assert sorted(st.rows) == ["disk:/A/B/y.jpg", "disk:/A/x.jpg"]


def test_missing_selected_root_raises_root_not_found():
    with pytest.raises(downloader.RootNotFound) as e:
        downloader.scan_tree(FakeDisk({}), FakeState(), root="/Нет/")
    assert e.value.path == "disk:/Нет"


def test_whole_disk_walks_shared_folders_missing_from_the_flat_list():
    disk = FakeDisk(
        tree={
            "disk:/": [_dir("disk:/A"), _dir("disk:/Моя общая", _share(True)),
                       _dir("disk:/Чужая", _share(False)), _dir("disk:/Чужая видна", _share(False)),
                       _dir("disk:/Непонятная", _share(None)), _file("disk:/корень.txt")],
            "disk:/Чужая": [_file("disk:/Чужая/1.jpg"), _dir("disk:/Чужая/Вложенная")],
            "disk:/Чужая/Вложенная": [_file("disk:/Чужая/Вложенная/2.jpg")],
        },
        flat=[_file("disk:/A/x.jpg"), _file("disk:/Моя общая/m.jpg"), _file("disk:/Чужая видна/v.jpg"),
              _file("disk:/корень.txt")],
    )
    st = FakeState()
    notes = []
    files, _ = downloader.scan_tree(disk, st, root="/", on_note=lambda kind, names: notes.append((kind, names)))
    assert files == 6
    assert "disk:/Чужая/Вложенная/2.jpg" in st.rows
    assert notes == [("shared_folders", ["Чужая"])]
    # Свою общую, уже увиденную и непонятную папку не обходим
    assert not {"disk:/Моя общая", "disk:/Чужая видна", "disk:/Непонятная"} & set(disk.listed)


def test_shared_folder_check_failure_does_not_break_the_scan():
    disk = FakeDisk(tree={}, flat=[_file("disk:/A/x.jpg")])  # корень не читается
    st = FakeState()
    assert downloader.scan_tree(disk, st, root="/") == (1, 1)


def test_list_disk_folders_keeps_dirs_sorted_and_reports_truncation():
    disk = FakeDisk({"disk:/": [
        _file("disk:/a.txt"), _dir("disk:/яблоки"), _dir("disk:/Корзина"), _dir("disk:/Бумаги", _share(False)),
        _dir("disk:/архив", _share(True)),
    ]})
    folders, truncated = downloader.list_disk_folders(disk, "/")
    assert [f["name"] for f in folders] == ["архив", "Бумаги", "яблоки"]
    assert folders[1] == {"name": "Бумаги", "path": "disk:/Бумаги", "shared": True, "owned": False}
    assert folders[0]["owned"] is True and not truncated
    assert downloader.list_disk_folders(disk, "disk:/", cap=2)[1] is True


# ── main.py scan ────────────────────────────────────────────────────────────


def test_scan_root_option_repeats_and_old_roots_still_work():
    p = main.build_parser()
    assert p.parse_args(["scan", "--root", "/A", "--root", "/B"]).root == ["/A", "/B"]
    old = p.parse_args(["scan", "--roots", "/A,/B"])
    assert old.root is None and old.roots == "/A,/B"
    assert p.parse_args(["scan"]).root is None


def test_scan_reports_missing_and_shared_folders_at_the_end(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(main, "_require_token", lambda cfg: None)
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "state.db")
    monkeypatch.setattr(main, "make_client", lambda token: FakeDisk({}))

    def fake_scan(client, state, root, on_file, on_note):
        if root == "/B":
            raise downloader.RootNotFound("disk:/B")
        on_note("shared_folders", ["Общая"])
        on_file("disk:/A/1", 1)
        on_file("disk:/A/2", 1)
        return 2, 2

    monkeypatch.setattr(main, "scan_tree", fake_scan)
    code = main.cmd_scan({"token": "t"}, argparse.Namespace(root=["/A", "/B"], roots=None))
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert 'folder_missing | {"path": "disk:/B"}' in lines
    assert 'shared_folders | {"names": ["Общая"]}' in lines
    assert lines.index('folder_missing | {"path": "disk:/B"}') > lines.index("Найдено: 2")
    assert webui._listing_found({"log_tail": lines}) == 2


# ── webui: очередь по выбору папок ──────────────────────────────────────────


def _disk_db(path, rows):
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT, status TEXT,"
        " local_path TEXT, error TEXT, downloaded_at TIMESTAMP, storage_host TEXT, preview_path TEXT);"
    )
    con.executemany("INSERT INTO files (remote_path, size, status, error) VALUES (?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def _statuses(path):
    con = sqlite3.connect(path)
    try:
        return {p: (s, e) for p, s, e in con.execute("SELECT remote_path, status, error FROM files")}
    finally:
        con.close()


def test_folder_filter_uses_exact_prefixes_and_can_be_undone(tmp_path):
    db = tmp_path / "state.db"
    _disk_db(db, [
        ("disk:/Фото/a.jpg", 1, "pending", None), ("disk:/Фото2/b.jpg", 1, "pending", None),
        ("disk:/50%_off/c.jpg", 1, "pending", None), ("disk:/50X_off/d.jpg", 1, "pending", None),
        ("disk:/фото/e.jpg", 1, "pending", None), ("disk:/Other/f.jpg", 1, "failed", "server_50x | x"),
        ("disk:/Other/g.jpg", 1, "downloaded", None), ("disk:/Фото/h.jpg", 1, "failed", "x"),
    ])
    assert webui.filter_to_folders(db, ["disk:/Фото", "disk:/50%_off"]) == (4, 2)
    st = _statuses(db)
    assert {p for p, (s, e) in st.items() if e == FOLDER_SKIP_MARKER} == {
        "disk:/Фото2/b.jpg", "disk:/50X_off/d.jpg", "disk:/фото/e.jpg", "disk:/Other/f.jpg"}
    assert st["disk:/Other/g.jpg"] == ("downloaded", None) and st["disk:/Фото/h.jpg"][0] == "failed"
    assert webui.unskip_markers(db, "files", SELECTION_MARKERS) == 4
    assert all(s != "skipped" for s, _e in _statuses(db).values())


def test_db_stats_counts_both_selection_markers_as_not_chosen(tmp_path):
    db = tmp_path / "state.db"
    _disk_db(db, [("disk:/a", 10, "skipped", FOLDER_SKIP_MARKER), ("disk:/b", 5, "skipped", CATEGORY_SKIP_MARKER),
                  ("disk:/c", 3, "skipped", None), ("disk:/d", 7, "pending", None)])
    s = webui.db_stats(db, "files", "remote_path")
    assert (s["category_skipped"], s["category_skipped_bytes"]) == (2, 15)
    assert (s["folder_skipped"], s["folder_skipped_bytes"]) == (1, 10)
    assert s["bytes_done"] == 3 and s["bytes_selected_total"] == 25 - 15
    block = webui._source_block("disk", s, None, True, "", None, None)
    assert block["files"]["excluded"] == 2


def test_category_counts_ignore_files_outside_chosen_folders(tmp_path):
    db = tmp_path / "state.db"
    _disk_db(db, [("disk:/A/a.jpg", 1, "pending", None), ("disk:/B/b.jpg", 1, "skipped", FOLDER_SKIP_MARKER)])
    assert webui.preview_db(db, "files", "remote_path")["categories"]["photos"]["count"] == 1


# ── webui: настройки и дерево папок ─────────────────────────────────────────


@pytest.fixture()
def client():
    return TestClient(webui.app, headers={"Host": "localhost:8000"})


def test_settings_store_folders_and_validate(client, monkeypatch):
    st = client.post("/api/settings", json={"disk_folders": [
        "/Фото/", "disk:/Фото/2020", "Отчёты, 2024", "Договоры 'старые'"]}).json()["settings"]
    want = ["disk:/Договоры 'старые'", "disk:/Отчёты, 2024", "disk:/Фото"]
    assert st["disk_folders"] == want
    assert client.get("/api/settings").json()["disk_folders"] == want
    assert decode_disk_folders(dotenv_values(webui.ENV_FILE)["BACKUP_DISK_FOLDERS"]) == want
    for bad in ([], ["/"], ["disk:/Корзина"]):
        r = client.post("/api/settings", json={"disk_folders": bad})
        assert r.status_code == 400 and r.json()["detail"]
    assert client.post("/api/settings", json={"all_folders": True}).json()["settings"]["disk_folders"] is None
    monkeypatch.setattr(webui.pipeline, "is_running", lambda: True)
    assert client.post("/api/settings", json={"disk_folders": ["/A"]}).status_code == 409


def test_folder_names_stay_out_of_the_server_log(client, caplog):
    with caplog.at_level("INFO", logger="webui"):
        client.post("/api/settings", json={"disk_folders": ["/Секретный проект"]})
    assert "Секретный" not in caplog.text and "1 папок" in caplog.text


def test_every_start_button_passes_the_saved_folders(client, monkeypatch):
    calls = []
    monkeypatch.setattr(webui, "preflight_sources", lambda d, u: {"error": None, "notes": [], "run_disk": d, "run_unlim": u})
    monkeypatch.setattr(webui.pipeline, "start", lambda **kw: (calls.append(kw), (True, "ok"))[1])
    monkeypatch.setattr(webui.tm, "any_alive", lambda prefix: False)
    client.post("/api/settings", json={"disk_folders": ["/A"]})
    client.post("/api/rescue/start")
    client.post("/api/start", json={})
    client.post("/api/plan/start", json={})
    assert [c["disk_folders"] for c in calls] == [["disk:/A"]] * 3


def test_folder_tree_needs_a_token(client):
    r = client.get("/api/disk/folders").json()
    assert r["ok"] is False and r["action"] == "token" and "ключ" in r["msg"]


def test_folder_tree_lists_folders_and_explains_errors(client, monkeypatch):
    monkeypatch.setenv("YA_DISK_TOKEN", "y0_test")
    disk = FakeDisk({"disk:/Работа": [_dir("disk:/Работа/2024"), _file("disk:/Работа/a.pdf")]})
    monkeypatch.setattr(webui, "make_browse_client", lambda token: disk)
    r = client.get("/api/disk/folders", params={"path": "/Работа"}).json()
    assert r == {"ok": True, "path": "disk:/Работа", "truncated": False,
                 "folders": [{"name": "2024", "path": "disk:/Работа/2024", "shared": False, "owned": False}]}
    assert client.get("/api/disk/folders", params={"path": "/Нет"}).json()["ok"] is False

    class Broken(FakeDisk):
        def __init__(self, exc):
            super().__init__({})
            self.exc = exc

        def listdir(self, path, **kw):
            raise self.exc

    monkeypatch.setattr(webui, "make_browse_client", lambda token: Broken(yadisk.exceptions.UnauthorizedError("401")))
    assert client.get("/api/disk/folders").json()["action"] == "token"
    monkeypatch.setattr(webui, "make_browse_client", lambda token: Broken(RuntimeError("boom")))
    r = client.get("/api/disk/folders").json()
    assert r["ok"] is False and "Яндекс" in r["msg"] and "boom" not in r["msg"]


def test_folder_tree_demo(client, monkeypatch):
    monkeypatch.setenv("YADISK_DEMO", "1")
    r = client.get("/api/disk/folders", params={"path": "disk:/", "demo": "1"}).json()
    names = {f["name"]: f for f in r["folders"]}
    assert names["Семейный архив"]["shared"] is True and names["Семейный архив"]["owned"] is False
    sub = client.get("/api/disk/folders", params={"path": "disk:/Загрузки/Project1", "demo": "1"}).json()
    assert [f["path"] for f in sub["folders"]] == ["disk:/Загрузки/Project1/Photos", "disk:/Загрузки/Project1/Words"]


# ── Режим спасения с выбранными папками ─────────────────────────────────────


@pytest.fixture()
def rescue(monkeypatch):
    monkeypatch.setattr(webui, "check_token", lambda tok: {"ok": True, "login": "t", "used": 1})
    monkeypatch.setattr(webui, "check_cookies", lambda: {"ok": True, "msg": "ok", "count": 5})
    r = webui.Rescue()
    r.retry_every = 0.05
    r.workers = 1
    r.skip_unlim = True
    monkeypatch.setattr(r, "_task_lines", lambda slot: [])
    return r


def test_selected_folders_are_listed_by_root(rescue, monkeypatch):
    sc = Script(monkeypatch, rescue, {"disk": [0, 0]})
    rescue.disk_folders = ["disk:/A", "disk:/Б в"]
    rescue._run()
    assert ("disk", "main.py", ("scan", "--root=disk:/A", "--root=disk:/Б в")) in sc.started
    assert rescue.state == "done"


def test_roots_covered():
    assert webui._roots_covered(None, None)
    assert webui._roots_covered(["disk:/A"], None)  # весь Диск покрывает любые папки
    assert not webui._roots_covered(None, ["disk:/A"])
    assert webui._roots_covered(["disk:/A/B"], ["disk:/A"])
    assert not webui._roots_covered(["disk:/AB"], ["disk:/A"])


def test_new_folders_force_a_new_listing(rescue):
    rescue._listed_at["disk"] = time.time()
    rescue._listed_roots["disk"] = ["disk:/A"]
    assert not rescue._needs_listing("disk", ["disk:/A/x"])
    assert rescue._needs_listing("disk", ["disk:/B"])
    assert rescue._needs_listing("disk", None)
    rescue._listed_roots["disk"] = None
    assert not rescue._needs_listing("disk", ["disk:/B"])


def test_all_chosen_folders_gone_asks_to_choose_again(rescue, monkeypatch):
    sc = Script(monkeypatch, rescue, {"disk": [0]})
    monkeypatch.setattr(rescue, "_task_lines", lambda slot: ['folder_missing | {"path": "disk:/A"}'])
    rescue.disk_folders = ["disk:/A"]
    rescue._run()
    disk = rescue.status()["branches"]["disk"]
    assert disk["state"] == "needs_action" and disk["action"] == "folders"
    assert ("disk", "download") not in sc.steps("disk")
    assert "folders" in webui._RESCUE_ACTIONS


def test_some_folders_gone_and_shared_folders_found_become_events(rescue, monkeypatch):
    sc = Script(monkeypatch, rescue, {"disk": [0, 0]})
    monkeypatch.setattr(rescue, "_task_lines", lambda slot: [
        'folder_missing | {"path": "disk:/B"}', 'shared_folders | {"names": ["Общая"]}', "мусор | {"])
    rescue.disk_folders = ["disk:/A", "disk:/B"]
    rescue._run()
    texts = [e["text"] for e in rescue.status()["events"]]
    assert any("Папки «B» больше нет на Диске" in t for t in texts)
    assert any("«Общая»" in t and "общие папки" in t for t in texts)
    assert ("disk", "download") in sc.steps("disk")


def test_queue_follows_folders_then_categories(rescue, monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    _disk_db(db, [("disk:/A/x.jpg", 1, "pending", None), ("disk:/A/y.pdf", 1, "pending", None),
                  ("disk:/B/z.jpg", 1, "pending", None), ("disk:/B/w.jpg", 1, "skipped", FOLDER_SKIP_MARKER),
                  ("disk:/A/v.jpg", 1, "skipped", CATEGORY_SKIP_MARKER)])
    monkeypatch.setattr(webui, "DB_FILE", db)
    rescue.disk_folders = ["disk:/A"]
    rescue.disk_categories = ["photos"]
    rescue._apply_disk_selection()
    st = _statuses(db)
    assert st["disk:/A/x.jpg"] == ("pending", None) and st["disk:/A/v.jpg"] == ("pending", None)
    assert st["disk:/A/y.pdf"] == ("skipped", CATEGORY_SKIP_MARKER)
    assert st["disk:/B/z.jpg"] == st["disk:/B/w.jpg"] == ("skipped", FOLDER_SKIP_MARKER)
    rescue.disk_folders = None
    rescue.disk_categories = None
    rescue._apply_disk_selection()
    assert all(s == "pending" for s, _e in _statuses(db).values())
