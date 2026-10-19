"""«Какие папки уже скачаны»: подсчёт по базе, карточка «Папки», лента, место на диске, FAT."""

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import webapp.access
import webapp.config
import webapp.control
import webapp.folders
import webapp.local_disk
import webapp.paths
import webapp.queue_db
import webapp.rescue
import webapp.rescue_view
import webapp.tasks
import webui
from state import CATEGORY_SKIP_MARKER, DEFERRED_MARKER, FOLDER_SKIP_MARKER, State
from utils import EXIT_DEFERRED, filesystem_type, fs_type_from_mounts, is_fat_filesystem, remove_preview


@pytest.fixture()
def client():
    return TestClient(webui.app, headers={"Host": "localhost:8000"})


def _fill(db, rows):
    """rows: (путь на Диске, статус, ошибка, размер)."""
    with State(str(db)) as st:
        st.mark_pending_batch([(path, size, None) for path, _st, _err, size in rows])
        st._conn().executemany("UPDATE files SET status=?, error=? WHERE remote_path=?",
                               [(status, err, path) for path, status, err, _size in rows])


ROWS = [
    ("disk:/Документы/a.pdf", "downloaded", None, 10),
    ("disk:/Документы/b.pdf", "skipped", None, 20),            # уже лежал в папке — тоже скачан
    ("disk:/Фото/2023/1.jpg", "downloaded", None, 5),
    ("disk:/Фото/2023/2.jpg", "pending", DEFERRED_MARKER + "s1klg молчит", 5),
    ("disk:/Фото/2024/3.jpg", "failed", "server_50x | 503", 5),
    ("disk:/Фото/4.jpg", "pending", None, 5),
    ("disk:/Фото2/x.jpg", "downloaded", None, 1),              # «Фото2» — не часть «Фото»
    ("disk:/Музыка/song.mp3", "skipped", CATEGORY_SKIP_MARKER, 3),
    ("disk:/Музыка/old/y.mp3", "skipped", FOLDER_SKIP_MARKER, 3),
    ("disk:/root.txt", "downloaded", None, 1),
    ("disk:/50%_off/a_b.txt", "downloaded", None, 2),          # % и _ — не шаблоны
    ("disk:/50X-off/z.txt", "pending", None, 2),
]


def test_progress_of_top_level_folders(tmp_path):
    db = tmp_path / "state.db"
    _fill(db, ROWS)
    res = webapp.folders.folder_progress(db, "disk:/")
    by = {f["name"]: f for f in res["folders"]}
    assert list(by) == ["50%_off", "50X-off", "Документы", "Музыка", "Фото", "Фото2"]
    assert (by["Документы"]["files"], by["Документы"]["done"], by["Документы"]["complete"]) == (2, 2, True)
    assert by["Документы"]["bytes"] == by["Документы"]["bytes_done"] == 30
    photo = by["Фото"]
    assert (photo["files"], photo["done"], photo["deferred"], photo["failed"], photo["waiting"]) == (4, 1, 1, 1, 1)
    assert photo["complete"] is False and photo["has_sub"] is True and photo["path"] == "disk:/Фото"
    assert by["Фото2"]["complete"] and by["50%_off"]["complete"] and not by["50X-off"]["complete"]
    # Снятое выбором не считается: папка «не выбрана», а не «не докачана»
    assert (by["Музыка"]["files"], by["Музыка"]["excluded"], by["Музыка"]["complete"]) == (0, 2, False)
    assert by["Музыка"]["has_sub"] is True
    assert res["here"]["files"] == 1 and res["here"]["complete"]  # файл в корне Диска
    assert (res["total"], res["complete"]) == (5, 3)
    assert "on_disk" not in by["Фото"]  # папку скачивания не передали


@pytest.mark.parametrize("parent", ["disk:/Фото", "disk:/Фото/", "/Фото", "Фото"])
def test_progress_inside_a_folder(tmp_path, parent):
    db = tmp_path / "state.db"
    _fill(db, ROWS)
    res = webapp.folders.folder_progress(db, parent)
    assert res["path"] == "disk:/Фото"
    by = {f["name"]: f for f in res["folders"]}
    assert set(by) == {"2023", "2024"}
    assert (by["2023"]["done"], by["2023"]["deferred"]) == (1, 1)
    assert by["2024"]["failed"] == 1 and by["2024"]["has_sub"] is False
    assert res["here"]["waiting"] == 1  # 4.jpg лежит прямо в «Фото»


def test_progress_prefix_is_exact(tmp_path):
    db = tmp_path / "state.db"
    _fill(db, ROWS)
    res = webapp.folders.folder_progress(db, "disk:/50%_off")
    assert res["folders"] == [] and res["here"]["files"] == 1 and res["here"]["complete"]
    assert webapp.folders.folder_progress(db, "disk:/Нет такой")["empty"] is True


def test_progress_knows_which_folders_are_in_the_download_folder(tmp_path):
    db = tmp_path / "state.db"
    _fill(db, ROWS + [("disk:/Встреча: итоги/notes.txt", "downloaded", None, 1)])
    dl = tmp_path / "dl"
    (dl / "Документы").mkdir(parents=True)
    (dl / "Встреча_ итоги").mkdir()  # имя очищено так же, как при скачивании
    by = {f["name"]: f for f in webapp.folders.folder_progress(db, "disk:/", str(dl))["folders"]}
    assert by["Документы"]["on_disk"] is True and by["Встреча: итоги"]["on_disk"] is True
    assert by["Фото2"]["on_disk"] is False  # скачана целиком и уже перенесена


def test_progress_limit_and_missing_database(tmp_path):
    db = tmp_path / "state.db"
    _fill(db, [(f"disk:/П{i:02d}/f.txt", "downloaded", None, 1) for i in range(5)])
    res = webapp.folders.folder_progress(db, limit=3)
    assert [f["name"] for f in res["folders"]] == ["П00", "П01", "П02"] and res["more"] == 2
    assert res["total"] == res["complete"] == 5  # сводка считает все папки, не только показанные
    empty = webapp.folders.folder_progress(tmp_path / "нет.db")
    assert empty["ok"] and empty["empty"] and empty["folders"] == []


def test_summary_is_cached_until_forced(tmp_path):
    db = tmp_path / "state.db"
    _fill(db, ROWS)
    first = webapp.folders.disk_folders_summary(db)
    assert (first["total"], first["complete"]) == (5, 3)
    assert set(first["names"]) == {"50%_off", "Документы", "Фото2"}
    con = sqlite3.connect(db)
    con.execute("UPDATE files SET status='downloaded', error=NULL WHERE remote_path LIKE 'disk:/Фото/%'")
    con.commit()
    con.close()
    assert webapp.folders.disk_folders_summary(db) is first  # опрос статуса каждые 1,5 с базу не пересчитывает
    assert webapp.folders.disk_folders_summary(db, force=True)["complete"] == 4
    assert webapp.folders.disk_folders_summary(tmp_path / "нет.db", force=True) is None


def test_folder_progress_endpoint_and_demo(client, tmp_path, monkeypatch):
    _fill(webapp.paths.DB_FILE, ROWS)
    monkeypatch.setattr(webapp.config, "get_config", lambda: {"download_dir": str(tmp_path / "dl")})
    res = client.get("/api/disk/folder_progress", params={"path": "disk:/Фото"}).json()
    assert res["ok"] and {f["name"] for f in res["folders"]} == {"2023", "2024"}
    assert all(f["on_disk"] is False for f in res["folders"])
    # Демо работает только с YADISK_DEMO=1
    assert client.get("/api/disk/folder_progress?demo=1").json()["total"] == 5
    monkeypatch.setenv("YADISK_DEMO", "1")
    demo = client.get("/api/disk/folder_progress?demo=1").json()
    assert demo["total"] == 6 and demo["complete"] == 3
    assert any(f["has_sub"] for f in demo["folders"])
    inner = client.get("/api/disk/folder_progress?demo=1&path=disk:/Фото").json()
    assert inner["here"]["complete"] and len(inner["folders"]) == 3


def test_open_a_disk_folder_inside_the_download_folder(client, tmp_path, monkeypatch):
    dl = tmp_path / "dl"
    (dl / "Фото" / "2023").mkdir(parents=True)
    (dl / "Встреча_ итоги").mkdir()
    opened = []
    monkeypatch.setattr(webapp.config, "get_config", lambda: {"download_dir": str(dl)})
    monkeypatch.setattr(webapp.control.os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(webapp.control.subprocess, "Popen", lambda args, **kw: opened.append(args[-1]))

    def post(path):
        return client.post("/api/open_folder", json={"which": "disk_folder", "path": path})

    assert post("disk:/Фото/2023").json()["ok"] is True
    assert Path(opened[-1]) == dl / "Фото" / "2023"
    assert post("disk:/Встреча: итоги").json()["ok"] is True
    assert Path(opened[-1]) == dl / "Встреча_ итоги"
    gone = post("disk:/Музыка").json()
    assert gone["ok"] is False and "перенесли" in gone["msg"]
    assert post("disk:/").status_code == 400
    # «..» из имени папки на Диске превращается в «_» — выйти за папку скачивания нельзя
    assert post("disk:/../../Windows").json()["ok"] is False
    assert len(opened) == 2


# ── Лента: «Папка «…» скачана целиком» ──────────────────────────────────────


def _rescue_with_steps(monkeypatch, steps):
    """Спасение только Диска; шаг скачивания — функция, меняющая базу и отдающая код выхода."""
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": True, "login": "t", "used": 1})
    monkeypatch.setattr(webapp.access, "check_cookies", lambda: {"ok": False, "msg": "нет"})
    monkeypatch.setattr(webapp.tasks.tm, "start", lambda slot, script, args: (True, "ok"))
    r = webapp.rescue.Rescue()
    r.retry_every = 0.05
    r.skip_unlim = True
    steps = list(steps)

    def wait(slot, on_finish_msg=""):
        if r.branches["disk"].state != "downloading":
            return 0  # сбор списка: база уже заполнена
        con = sqlite3.connect(webapp.paths.DB_FILE)
        code = steps.pop(0)(con)
        con.commit()
        con.close()
        return code

    monkeypatch.setattr(r, "_wait_task", wait)
    return r


def _done(con, *paths):
    con.executemany("UPDATE files SET status='downloaded', error=NULL WHERE remote_path=?", [(p,) for p in paths])


def test_feed_says_when_a_folder_is_complete(monkeypatch):
    _fill(webapp.paths.DB_FILE, [("disk:/A/1.txt", "pending", None, 1), ("disk:/B/2.txt", "pending", None, 1),
                          ("disk:/C/3.txt", "downloaded", None, 1)])

    def first(con):
        _done(con, "disk:/A/1.txt")
        con.execute("UPDATE files SET error=? WHERE remote_path='disk:/B/2.txt'", (DEFERRED_MARKER + "молчит",))
        return EXIT_DEFERRED

    def second(con):
        _done(con, "disk:/B/2.txt")
        return 0

    r = _rescue_with_steps(monkeypatch, [first, second])
    r._run()
    assert r.state == "done"
    texts = [e["text"] for e in r.events if e["source"] == "disk"]
    assert texts.count("Папка «A» скачана целиком") == 1
    assert texts.count("Папка «B» скачана целиком") == 1
    assert not any("«C»" in t for t in texts)  # была скачана до запуска — без события
    assert texts.index("Папка «A» скачана целиком") < texts.index("Папка «B» скачана целиком")


def test_feed_groups_many_complete_folders(monkeypatch):
    names = ["Д", "Г", "В", "Б", "А"]
    _fill(webapp.paths.DB_FILE, [(f"disk:/{n}/f.txt", "pending", None, 1) for n in names])
    r = _rescue_with_steps(monkeypatch, [lambda con: _done(con, *(f"disk:/{n}/f.txt" for n in names)) or 0])
    r._run()
    texts = [e["text"] for e in r.events if e["text"].startswith("Скачано целиком")]
    assert texts == ["Скачано целиком папок: 5 — «А», «Б», «В» и другие. Список — в «Подробнее» у Диска"]
    assert not any(e["text"].startswith("Папка «") for e in r.events)


# ── Главная: сколько папок скачано и хватит ли места ────────────────────────


def _db(**kw):
    st = webapp.queue_db.empty_db_stats()
    st.update(kw)
    st["recent"] = []
    return st


def test_status_tells_when_space_is_short():
    gb = 1024 ** 3
    disk = _db(total=10, downloaded=4, pending=6, deferred=2, bytes_total=10 * gb, bytes_done=4 * gb,
               bytes_pending=6 * gb, bytes_deferred=2 * gb, bytes_selected_total=10 * gb)
    unlim = _db(total=4, failed=1, downloaded=3, bytes_total=4 * gb, bytes_failed=1 * gb, bytes_done=3 * gb,
                bytes_selected_total=4 * gb)
    tok, coo = {"ok": True}, {"ok": True}
    cfg = {"download_dir": "D:\\Яндекс"}

    def build(free, **kw):
        return webapp.rescue_view.build_rescue_status({"branches": {}}, disk, unlim, tok, coo, cfg, free_space=free, **kw)

    r = build({"free": 5 * gb, "fs": "NTFS", "fat": False}, disk_folders={"total": 6, "complete": 2, "names": ["x"]})
    assert r["space"] == {"free": 5 * gb, "need": 7 * gb, "short": True, "fs": "NTFS", "fat": False}
    assert r["sources"]["disk"]["folders"] == {"total": 6, "complete": 2}
    assert r["sources"]["unlim"].get("folders") is None
    assert build({"free": 8 * gb})["space"]["short"] is False
    assert build(None)["space"] == {"free": None, "need": 7 * gb, "short": False, "fs": None, "fat": False}
    assert build({"free": 1, "fs": "FAT32", "fat": True})["space"]["fat"] is True
    assert build({"free": 8 * gb})["sources"]["disk"]["folders"] is None


def test_free_space_reports_the_filesystem(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp.local_disk, "filesystem_type", lambda path: "FAT32")
    monkeypatch.setattr(webapp.local_disk, "_fs_cache", {})
    info = webapp.local_disk.free_space(str(tmp_path / "ещё нет"))
    assert info["fs"] == "FAT32" and info["fat"] is True and info["free"] > 0


# ── Файловая система и превью в прежней папке ───────────────────────────────

MOUNTS = ("/dev/sda1 / ext4 rw 0 0\n"
          "/dev/sdb1 /media/user/My\\040Disk vfat rw,nosuid 0 0\n"
          "/dev/sdc1 /media/user/My exfat rw 0 0\n"
          "garbage\n")


@pytest.mark.parametrize("path, fs", [
    ("/media/user/My Disk/Яндекс", "vfat"),
    ("/media/user/My Disk", "vfat"),
    ("/media/user/My/backup", "exfat"),
    ("/media/user/MyOther", "ext4"),  # «My» — не начало «MyOther»
    ("/home/user", "ext4"),
])
def test_filesystem_from_proc_mounts(path, fs):
    assert fs_type_from_mounts(path, MOUNTS) == fs


def test_fat_detection_and_real_volume(tmp_path):
    assert is_fat_filesystem("FAT32") and is_fat_filesystem("vfat") and is_fat_filesystem(" msdos ")
    assert not is_fat_filesystem("exFAT") and not is_fat_filesystem("NTFS") and not is_fat_filesystem(None)
    fs = filesystem_type(str(tmp_path / "ещё" / "нет"))  # папки нет — смотрим ближайшую существующую
    assert fs is None or (isinstance(fs, str) and fs)


def _put(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8preview")
    return path


def test_preview_in_the_previous_download_folder_is_removed(tmp_path):
    # Папку сменили на внешний диск, оригинал докачался туда — превью в старой «_превью» больше не нужно
    old, new = tmp_path / "C" / "Яндекс", tmp_path / "E" / "Яндекс"
    preview = _put(old / "_превью" / "Фото" / "a.jpg.jpg")
    original = _put(old / "Фото" / "b.jpg")
    lookalike = _put(old / "_превью2" / "c.jpg.jpg")
    assert remove_preview(str(preview), str(new))
    assert not preview.exists()
    assert (old / "_превью").is_dir() and not (old / "_превью" / "Фото").exists()
    assert remove_preview(str(original), str(new)) is False and original.exists()
    assert remove_preview(str(lookalike), str(new)) is False and lookalike.exists()
    assert os.listdir(old / "_превью") == []
