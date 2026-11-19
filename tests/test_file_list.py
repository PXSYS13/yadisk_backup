"""Списки скачанных и нескачанных файлов: API, потоковый экспорт CSV/TXT, CLI, приватность."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import main as disk_cli
import public_download as pdl
import state as disk_state
import unlim_common as uc
import unlim_download as ud
import webapp.paths
import webapp.queue_db
import webui


@pytest.fixture
def client():
    return TestClient(webui.app, headers={"Host": "localhost:8000"})


def _seed_disk_db(db_path):
    st = disk_state.State(str(db_path))
    c = st._conn()
    rows = [
        # (remote_path, size, md5, status, local_path, error, downloaded_at, storage_host, preview_path)
        ("disk:/Фото/Отпуск/IMG_001.JPG", 5_000_000, "a" * 32, "downloaded", "D:\\B\\Фото\\Отпуск\\IMG_001.JPG", None, "2026-11-18T10:00:00", None, None),
        ("disk:/Фото/Отпуск/IMG_002.JPG", 3_000_000, "b" * 32, "skipped", "D:\\B\\Фото\\Отпуск\\IMG_002.JPG", None, "2026-11-18T11:00:00", None, None),
        ("disk:/Фото/100%_тест/raw.dng", 12_000_000, "c" * 32, "pending", None, "storage_down | host silent", None, "s1klg.storage.yandex.net", "D:\\B\\_превью\\raw.jpg"),
        ("disk:/Документы/Отчёт.pdf", 800_000, "d" * 32, "pending", None, None, None, None, None),
        ("disk:/Документы/=SUM(A1).xlsx", 200_000, "e" * 32, "failed", None, "local_file | имя не подходит", None, None, None),
        ("disk:/Архивы/old.zip", 50_000_000, "f" * 32, "skipped", None, disk_state.CATEGORY_SKIP_MARKER, None, None, None),
        ("disk:/Прочее/skip_dir.txt", 100, "1" * 32, "skipped", None, disk_state.FOLDER_SKIP_MARKER, None, None, None),
    ]
    c.executemany(
        "INSERT INTO files (remote_path, size, md5, status, local_path, error, downloaded_at, storage_host, preview_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    st.close()


def _seed_unlim_db(db_path):
    c = uc.connect_unlim_db(str(db_path))
    c.executescript(uc.UNLIM_SCHEMA)
    c.executemany(
        "INSERT INTO unlim_files (file_id, name, size, md5, status, local_path, error, downloaded_at, preview_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("/photounlim/Море_2025.jpg", "Море_2025.jpg", 4_000_000, "a" * 32, "downloaded", "D:\\B\\_unlim\\Море_2025.jpg", None, "2026-11-18 12:00:00", None),
            ("/photounlim/Видео.mp4", "Видео.mp4", 40_000_000, "b" * 32, "in_progress", None, None, None, None),
            ("/photounlim/Горы.jpg", "Горы.jpg", 6_000_000, "c" * 32, "pending", None, "storage_down | timeout", None, "D:\\B\\_превью\\Горы.jpg"),
            ("/photounlim/Битый.heic", "Битый.heic", 2_000_000, "d" * 32, "failed", None, "bad_file | MD5 mismatch", None, None),
            ("/photounlim/Снято.mov", "Снято.mov", 10_000_000, "e" * 32, "skipped", None, disk_state.CATEGORY_SKIP_MARKER, None, None),
        ],
    )
    c.commit()
    c.close()


def _seed_links_db(db_path):
    c = pdl.connect_db(str(db_path))
    c.execute(
        "INSERT INTO links (id, url, public_url, name, kind, local_dir, state) "
        "VALUES (1, 'https://disk.yandex.ru/d/SECRET_TOKEN', 'https://disk.yandex.ru/d/SECRET_TOKEN', "
        "'Отпуск друзей', 'dir', '_ссылки/Отпуск друзей', 'active')"
    )
    c.executemany(
        "INSERT INTO link_files (link_id, path, size, status, local_path, error, downloaded_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?)",
        [
            ("/Фото/Пляж.jpg", 1_500_000, "downloaded", "D:\\B\\_ссылки\\Пляж.jpg", None, "2026-11-18 09:00:00"),
            ("/Фото/Закат.jpg", 2_500_000, "pending", None, "storage_down | silent", None),
            ("/Видео/Клип.mp4", 15_000_000, "pending", None, "link_limit | 429", None),
            ("/Документ.pdf", 500_000, "failed", None, "http_error | 403", None),
            ("/Удалён.txt", 10, "gone", None, "gone | 404", None),
        ],
    )
    c.commit()
    c.close()


def test_disk_files_counts_match_db_stats_and_filters(client):
    _seed_disk_db(webapp.paths.DB_FILE)
    stats = webapp.queue_db.disk_db_stats()

    r = client.get("/api/disk/files?status=not_downloaded")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Сходимость со статистикой db_stats
    done_expected = stats["downloaded"] + (stats["skipped"] - stats["category_skipped"])
    assert body["counts"]["downloaded"] == done_expected == 2
    assert body["counts"]["not_downloaded"] == stats["pending"] + stats["failed"] == 3
    assert body["counts"]["queued"] == 1
    assert body["counts"]["deferred"] == 1
    assert body["counts"]["failed"] == 1
    assert body["counts"]["excluded"] == 2
    assert body["counts"]["all"] == 7
    # В нескачанных сначала failed, потом deferred, потом queued
    assert [it["state"] for it in body["items"]] == ["failed", "deferred", "queued"]
    assert body["items"][1]["preview"] is True

    # Скачанные (включая resume-skipped без маркера)
    r_done = client.get("/api/disk/files?status=downloaded")
    assert [it["name"] for it in r_done.json()["items"]] == ["IMG_002.JPG", "IMG_001.JPG"]

    # Исключённые настройками
    r_exc = client.get("/api/disk/files?status=excluded")
    assert r_exc.json()["total"] == 2


def test_cyrillic_search_folder_filter_and_pagination(client):
    _seed_disk_db(webapp.paths.DB_FILE)

    # Поиск по кириллице без учёта регистра
    r = client.get("/api/disk/files", params={"status": "all", "q": "отпуск"})
    assert r.status_code == 200
    names = {it["name"] for it in r.json()["items"]}
    assert names == {"IMG_001.JPG", "IMG_002.JPG"}

    # Фильтр по папке с % и _
    r_fold = client.get("/api/disk/files", params={"status": "all", "folder": "disk:/Фото/100%_тест"})
    assert [it["name"] for it in r_fold.json()["items"]] == ["raw.dng"]

    # Пагинация и сортировка по размеру
    r_p1 = client.get("/api/disk/files", params={"status": "all", "sort": "size_desc", "limit": 2, "page": 1})
    r_p2 = client.get("/api/disk/files", params={"status": "all", "sort": "size_desc", "limit": 2, "page": 2})
    assert r_p1.json()["pages"] == 4
    assert [it["name"] for it in r_p1.json()["items"]] == ["old.zip", "raw.dng"]
    assert [it["name"] for it in r_p2.json()["items"]] == ["IMG_001.JPG", "IMG_002.JPG"]


def test_unlim_and_links_file_lists_and_privacy(client):
    _seed_unlim_db(webapp.paths.UNLIM_DB_FILE)
    _seed_links_db(webapp.paths.LINKS_DB_FILE)

    ru = client.get("/api/unlim/files?status=not_downloaded").json()
    assert ru["counts"]["downloaded"] == 1
    assert ru["counts"]["not_downloaded"] == 3
    assert ru["counts"]["queued"] == 1  # in_progress
    assert ru["counts"]["deferred"] == 1
    assert ru["counts"]["failed"] == 1
    assert ru["counts"]["excluded"] == 1

    # Поиск на кириллице в безлимите
    ru_q = client.get("/api/unlim/files", params={"status": "all", "q": "море"}).json()
    assert [it["name"] for it in ru_q["items"]] == ["Море_2025.jpg"]

    # Ссылки: без SECRET_TOKEN и без gone в основном списке
    rl = client.get("/api/links/files", params={"link_id": 1, "status": "all"}).json()
    assert rl["counts"]["downloaded"] == 1
    assert rl["counts"]["not_downloaded"] == 3
    assert rl["counts"]["limited"] == 1
    assert "SECRET_TOKEN" not in str(rl)
    assert all(it["name"] != "Удалён.txt" for it in rl["items"])


def test_streaming_export_csv_and_txt_and_formula_guard(client):
    _seed_disk_db(webapp.paths.DB_FILE)
    _seed_links_db(webapp.paths.LINKS_DB_FILE)

    rc = client.get("/api/disk/files/export?status=not_downloaded&format=csv")
    assert rc.status_code == 200
    assert rc.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM для Excel
    text = rc.content.decode("utf-8-sig")
    assert "Статус;Имя файла;Путь на Яндексе" in text
    assert "'=SUM(A1).xlsx" in text  # защита от CSV formula injection
    assert "IMG_001.JPG" not in text

    rt = client.get("/api/disk/files/export?status=downloaded&format=txt")
    assert rt.status_code == 200
    lines = rt.text.strip().splitlines()
    assert "/Фото/Отпуск/IMG_001.JPG" in lines and "/Фото/Отпуск/IMG_002.JPG" in lines
    assert len(lines) == 2

    # Экспорт ссылок не утекает URL
    rl = client.get("/api/links/files/export?link_id=1&status=all&format=csv")
    assert rl.status_code == 200
    assert "SECRET_TOKEN" not in rl.content.decode("utf-8-sig")


def test_file_list_works_on_legacy_db_and_respects_csrf_and_demo(client, monkeypatch):
    # Старая схема state.db без storage_host и preview_path
    con = sqlite3.connect(webapp.paths.DB_FILE)
    con.execute(
        "CREATE TABLE files (remote_path TEXT PRIMARY KEY, size INTEGER, md5 TEXT, "
        "status TEXT, local_path TEXT, error TEXT, downloaded_at TIMESTAMP)"
    )
    con.execute("INSERT INTO files VALUES ('disk:/a.txt', 10, NULL, 'downloaded', 'a.txt', NULL, NULL)")
    con.commit()
    con.close()
    assert client.get("/api/disk/files?status=downloaded").json()["total"] == 1

    # CSRF-защита для GET /api/...
    assert client.get("/api/disk/files", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/disk/files/export", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403

    # Demo-режим
    monkeypatch.setenv("YADISK_DEMO", "1")
    rd = client.get("/api/disk/files?status=downloaded&demo=downloading").json()
    assert rd["ok"] and rd["total"] > 0 and rd["items"]
    ru = client.get("/api/unlim/files?status=not_downloaded&demo=downloading").json()
    assert ru["ok"] and ru["total"] > 0 and ru["items"]


def test_cli_export_commands_work_without_token_or_cookies(tmp_path, monkeypatch):
    disk_db = tmp_path / "state.db"
    unlim_db = tmp_path / "unlim_state.db"
    _seed_disk_db(disk_db)
    _seed_unlim_db(unlim_db)

    out_csv = tmp_path / "disk_not_done.csv"
    monkeypatch.setattr(disk_cli, "DB_FILE", disk_db)
    monkeypatch.setattr(disk_cli, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(disk_cli, "LOG_FILE", tmp_path / "logs" / "backup.log")
    assert disk_cli.main(["export", "--status", "not_downloaded", "--format", "csv", "--out", str(out_csv)]) == 0
    content = out_csv.read_text(encoding="utf-8-sig")
    assert "raw.dng" in content and "IMG_001.JPG" not in content

    out_txt = tmp_path / "unlim_done.txt"
    monkeypatch.setattr(ud, "DB_FILE", str(unlim_db))
    monkeypatch.setattr(ud, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(ud, "LOG_FILE", str(tmp_path / "logs" / "unlim.log"))
    assert ud.cmd_export(str(out_txt), status="downloaded", fmt="txt") == 0
    assert "Море_2025.jpg" in out_txt.read_text(encoding="utf-8")
