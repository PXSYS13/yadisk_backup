"""Тесты общего модуля безлимита unlim_common."""

import json
import sqlite3
import pytest
import unlim_common as uc
from utils import EXIT_OUTAGE


def test_make_cid():
    cid = uc.make_cid("12345")
    assert cid.startswith("12345")
    assert len(cid) > len("12345")


def test_load_cookies_valid(tmp_path):
    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text(
        json.dumps([
            {"name": "Session_id", "value": "xyz"},
            {"name": "yandexuid", "value": "123"},
            {"name": "empty", "value": None},
            {"name": "empty_str", "value": ""},
        ]),
        encoding="utf-8",
    )
    loaded = uc.load_cookies(str(cookies_path))
    assert loaded == {"Session_id": "xyz", "yandexuid": "123"}


def test_load_cookies_missing(tmp_path):
    with pytest.raises(SystemExit):
        uc.load_cookies(str(tmp_path / "nonexistent.json"))


def test_load_cookies_invalid_format(tmp_path):
    cookies_path = tmp_path / "bad.json"
    cookies_path.write_text(json.dumps({"name": "val"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        uc.load_cookies(str(cookies_path))


def test_load_cookies_malformed_entries(tmp_path):
    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text(
        json.dumps([
            "not a dict",
            12345,
            None,
            {"wrong_key": "val"},
            {"name": 999, "value": "abc"},
            {"name": "Session_id", "value": "valid_session"},
            {"name": "yandexuid", "value": 123},  # non-string value
        ]),
        encoding="utf-8",
    )
    loaded = uc.load_cookies(str(cookies_path))
    assert loaded == {"Session_id": "valid_session"}



def test_ensure_attempts_column(tmp_path):
    db_path = str(tmp_path / "test.db")
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, name TEXT)"
    )
    con.commit()
    con.close()

    uc.ensure_attempts_column(db_path)

    con = sqlite3.connect(db_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(unlim_files)")}
    con.close()
    assert "attempts" in cols


def test_make_session(tmp_path):
    cookies = {"Session_id": "test_session", "yandexuid": "999"}
    session = uc.make_session(cookies=cookies)
    assert session.headers["User-Agent"] == uc.UA
    assert session.headers["Origin"] == uc.BASE
    assert session.cookies.get("Session_id", domain=".yandex.ru") == "test_session"


def test_get_sk_uid_auth_expired():
    class FakeResponse:
        url = "https://passport.yandex.ru/auth"
        status_code = 302
        text = "redirect to login"

    class FakeSession:
        def get(self, url, timeout=30):
            return FakeResponse()

    with pytest.raises(uc.AuthExpiredError):
        uc.get_sk_uid(FakeSession())


def test_get_sk_uid_success():
    class FakeResponse:
        url = "https://disk.yandex.ru/client/disk"
        status_code = 200
        text = '<html><script>{"sk":"my_test_sk","uid":"12345678"}</script></html>'

    class FakeSession:
        def get(self, url, timeout=30):
            return FakeResponse()

    sk, uid = uc.get_sk_uid(FakeSession())
    assert sk == "my_test_sk"
    assert uid == "12345678"


def test_get_all_clusters_pagination(monkeypatch):
    import unlim_download as ud

    calls = []

    def fake_api_call(s, sk, uid, method, params=None, retries=3):
        calls.append(params.copy())
        offset = params.get("offset", 0)
        if offset == 0:
            return {"items": [{"id": "c1"}, {"id": "c2"}]}
        elif offset == 2:
            return {"items": [{"id": "c3"}]}
        return {"items": []}

    monkeypatch.setattr(ud, "api_call", fake_api_call)
    clusters = ud.get_all_clusters(None, "sk", "uid", "psid", batch_size=2)
    assert len(clusters) == 3
    assert [c["id"] for c in clusters] == ["c1", "c2", "c3"]
    assert len(calls) == 2


@pytest.fixture()
def unlim_db(tmp_path, monkeypatch):
    import unlim_download as ud
    monkeypatch.setattr(ud, "DB_FILE", str(tmp_path / "unlim_state.db"))
    monkeypatch.setattr(ud, "_tls", __import__("threading").local())
    ud.db_init()
    return ud


def _unlim_item(name, size):
    return {"type": "file", "name": name, "path": f"/photounlim/{name}",
            "meta": {"size": size, "md5": "a" * 32, "mimetype": "image/jpeg"}}


def test_collect_via_list_pages_through_photounlim(unlim_db, monkeypatch, capsys):
    ud = unlim_db
    monkeypatch.setattr(ud, "LIST_PAGE", 2)
    root = {"type": "dir", "path": "/photounlim", "id": "/photounlim/"}
    pages = {0: [root, _unlim_item("a.jpg", 10), _unlim_item("b.jpg", 20)],
             2: [root, _unlim_item("c.jpg", 30)]}
    calls = []

    def fake_api_call(s, sk, uid, method, params=None, retries=3):
        assert method == "mpfs/list" and params["path"] == "/photounlim" and "size" in params["meta"]
        calls.append(params["offset"])
        return pages[params["offset"]]

    monkeypatch.setattr(ud, "api_call", fake_api_call)
    assert ud.collect_via_list(None, "sk", "uid") == 3
    assert calls == [0, 2]
    rows = ud.db().execute("SELECT file_id, name, size, md5, status FROM unlim_files ORDER BY name").fetchall()
    assert [tuple(r) for r in rows] == [
        ("/photounlim/a.jpg", "a.jpg", 10, "a" * 32, "pending"),
        ("/photounlim/b.jpg", "b.jpg", 20, "a" * 32, "pending"),
        ("/photounlim/c.jpg", "c.jpg", 30, "a" * 32, "pending"),
    ]
    assert "Найдено: 3 файлов" in capsys.readouterr().out  # строку читает Web UI


def test_collect_via_list_stops_when_yandex_repeats_pages(unlim_db, monkeypatch, capsys):
    """Яндекс отдаёт ту же страницу снова и снова: сбор не крутится бесконечно, «Найдено» не растёт."""
    ud = unlim_db
    monkeypatch.setattr(ud, "LIST_PAGE", 2)
    root = {"type": "dir", "path": "/photounlim"}
    calls = []

    def same_page(s, sk, uid, method, params=None, retries=3):
        calls.append(params["offset"])
        return [root, _unlim_item("a.jpg", 10), _unlim_item("b.jpg", 20)]

    monkeypatch.setattr(ud, "api_call", same_page)
    with pytest.raises(ud.ListBroken) as e:
        ud.collect_via_list(None, "sk", "uid")
    assert e.value.seen == 2 and "повторяет" in str(e.value)
    assert calls == [0, 2]
    assert ud.db().execute("SELECT COUNT(*) FROM unlim_files").fetchone()[0] == 2
    assert "Найдено: 4" not in capsys.readouterr().out


def test_collect_via_list_retries_a_page_in_the_middle(unlim_db, monkeypatch):
    """Сбой Яндекса на середине большого списка — повтор той же страницы, а не сбор с начала."""
    ud = unlim_db
    monkeypatch.setattr(ud, "LIST_PAGE", 2)
    monkeypatch.setattr(ud, "LIST_PAGE_PAUSES", (0, 0))
    root = {"type": "dir", "path": "/photounlim"}
    pages = {0: [root, _unlim_item("a.jpg", 10), _unlim_item("b.jpg", 20)], 2: [root, _unlim_item("c.jpg", 30)]}
    fails = {2: 2}
    calls = []

    def flaky(s, sk, uid, method, params=None, retries=3):
        off = params["offset"]
        calls.append(off)
        if fails.get(off):
            fails[off] -= 1
            raise RuntimeError("mpfs/list: HTTP 500")
        return pages[off]

    monkeypatch.setattr(ud, "api_call", flaky)
    assert ud.collect_via_list(None, "sk", "uid") == 3
    assert calls == [0, 2, 2, 2]


def test_collect_keeps_a_broken_list_and_does_not_mix_in_photoslice(unlim_db, monkeypatch, capsys):
    """Список оборвался посреди: найденное в базе, код 3 (не запоминать как полный), фотосрез не зовём."""
    ud = unlim_db
    monkeypatch.setattr(ud, "LIST_PAGE", 2)
    monkeypatch.setattr(ud, "LIST_PAGE_PAUSES", ())
    monkeypatch.setattr(ud, "load_cookies", lambda: {})
    monkeypatch.setattr(ud, "get_sk_uid", lambda s: ("sk", "uid"))
    photoslice = []
    monkeypatch.setattr(ud, "collect_via_photoslice", lambda *a: photoslice.append(1))
    root = {"type": "dir", "path": "/photounlim"}

    def broken_after_first(s, sk, uid, method, params=None, retries=3):
        if params["offset"]:
            raise RuntimeError("mpfs/list: HTTP 504")
        return [root, _unlim_item("a.jpg", 10), _unlim_item("b.jpg", 20)]

    monkeypatch.setattr(ud, "api_call", broken_after_first)
    assert ud.cmd_collect() == EXIT_OUTAGE
    assert not photoslice
    assert ud.db().execute("SELECT COUNT(*) FROM unlim_files").fetchone()[0] == 2
    out = capsys.readouterr().out
    assert "Уже найденное сохранено: 2 файла" in out
    assert "получен не полностью" in out


def test_photoslice_takes_only_unlim_files_and_no_second_copies(unlim_db, monkeypatch):
    """Фотосрез отдаёт все фото аккаунта: фото Диска и корзины не берём, найденное mpfs/list — не дублируем."""
    ud = unlim_db
    ud.db().execute("INSERT INTO unlim_files (file_id, name, size, status) "
                    "VALUES ('/photounlim/a.jpg', 'a.jpg', 10, 'downloaded')")
    items = [{"id": i, "name": i.rsplit("/", 1)[-1], "size": 1} for i in (
        "photounlim:/a.jpg", "photounlim:/b.jpg", "/disk/Фотокамера/c.jpg", "disk:/d.jpg", "/trash/e.jpg")]
    monkeypatch.setattr(ud, "api_call", lambda *a, **k: {"clusters": {"fetched": [{"id": "c1", "items": items}]}})
    assert ud.fetch_cluster_resources(None, "sk", "uid", "psid", [{"cluster_id": "c1", "size": 5}]) == 1
    ids = {r[0] for r in ud.db().execute("SELECT file_id FROM unlim_files")}
    assert ids == {"/photounlim/a.jpg", "photounlim:/b.jpg"}


def test_save_listed_files_does_not_duplicate_photoslice_rows(unlim_db):
    ud = unlim_db
    ud.db().execute("INSERT INTO unlim_files (file_id, name, size, status) "
                    "VALUES ('photounlim:/a.jpg', 'a.jpg', 0, 'downloaded')")
    assert ud.save_listed_files([_unlim_item("a.jpg", 10), _unlim_item("b.jpg", 20)]) == 1
    rows = {r["file_id"]: (r["size"], r["md5"], r["status"]) for r in ud.db().execute("SELECT * FROM unlim_files")}
    assert rows["photounlim:/a.jpg"] == (10, "a" * 32, "downloaded")  # дополнен, не задублирован
    assert rows["/photounlim/b.jpg"][2] == "pending"
    assert len(rows) == 2


def test_reset_failed_files_migrates_a_database_from_an_old_version(tmp_path):
    """Web UI сбрасывает ошибки до сбора списка: база без attempts не должна ронять его."""
    db_path = str(tmp_path / "unlim_state.db")
    con = sqlite3.connect(db_path)
    con.executescript(
        "CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, name TEXT, size INTEGER, md5 TEXT,"
        " status TEXT DEFAULT 'pending', local_path TEXT, error TEXT, worker_id INTEGER,"
        " assigned_at TIMESTAMP, downloaded_at TIMESTAMP);"
        "INSERT INTO unlim_files (file_id, status, error) VALUES ('1', 'failed', 'boom'), ('2', 'pending', NULL);"
    )
    con.close()

    assert uc.reset_failed_files(db_path) == 1

    con = sqlite3.connect(db_path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(unlim_files)")}
    rows = con.execute("SELECT file_id, status, attempts, error FROM unlim_files ORDER BY file_id").fetchall()
    con.close()
    assert {"attempts", "storage_host", "preview_path"} <= cols
    assert rows == [("1", "pending", 0, None), ("2", "pending", 0, None)]


def test_reset_failed_files_without_a_file_list(tmp_path):
    """База есть, а таблицы файлов нет (воркер запускали до сбора списка) — сбрасывать нечего."""
    db_path = str(tmp_path / "unlim_state.db")
    uc.ensure_unlim_schema(db_path)  # так её создаёт воркер: только unlim_meta
    assert uc.reset_failed_files(db_path, include_in_progress=True) == 0


def test_old_photoslice_rows_outside_unlim_are_dropped(unlim_db, tmp_path, monkeypatch):
    """Фотосрез до октября 2026 отдавал все фото аккаунта, и с обычного Диска тоже.
    Их качает Диск — из очереди безлимита такие строки уходят, скачанное остаётся."""
    from utils import PREVIEW_DIRNAME

    ud = unlim_db
    monkeypatch.setattr(ud, "DOWNLOAD_DIR", str(tmp_path))
    preview = tmp_path / PREVIEW_DIRNAME / "_unlim" / "c.jpg.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"jpeg")
    ud.db().executemany(
        "INSERT INTO unlim_files (file_id, name, size, status, preview_path) VALUES (?, ?, 0, ?, ?)",
        [("/photounlim/a.jpg", "a.jpg", "pending", None), ("photounlim:/b.jpg", "b.jpg", "failed", None),
         ("/disk/Фотокамера/c.jpg", "c.jpg", "pending", str(preview)), ("disk:/x.jpg", "x.jpg", "failed", None),
         ("/disk/y.jpg", "y.jpg", "downloaded", None), ("/disk/z.jpg", "z.jpg", "in_progress", None)],
    )
    assert ud.drop_rows_outside_unlim() == 2
    left = {r["file_id"]: r["status"] for r in ud.db().execute("SELECT file_id, status FROM unlim_files")}
    assert left == {"/photounlim/a.jpg": "pending", "photounlim:/b.jpg": "failed",
                    "/disk/y.jpg": "downloaded", "/disk/z.jpg": "in_progress"}
    assert not preview.exists()
    assert ud.drop_rows_outside_unlim() == 0


def test_collect_drops_old_rows_only_after_a_full_list(unlim_db, monkeypatch, capsys):
    ud = unlim_db
    monkeypatch.setattr(ud, "load_cookies", lambda: {})
    monkeypatch.setattr(ud, "get_sk_uid", lambda s: ("sk", "uid"))
    monkeypatch.setattr(ud, "collect_via_photoslice", lambda *a: None)
    ud.db().execute("INSERT INTO unlim_files (file_id, name, size, status) VALUES ('/disk/a.jpg', 'a.jpg', 0, 'pending')")

    def broken(*a, **k):
        raise RuntimeError("mpfs/list: HTTP 500")

    # Список не получен — неизвестно, что в безлимите: ничего не трогаем
    monkeypatch.setattr(ud, "api_call", broken)
    ud.cmd_collect()
    assert ud.db().execute("SELECT COUNT(*) FROM unlim_files").fetchone()[0] == 1

    # Безлимит пуст, список полный — старая строка Диска уходит из очереди
    monkeypatch.setattr(ud, "api_call", lambda *a, **k: [{"type": "dir", "path": "/photounlim"}])
    assert not ud.cmd_collect()
    assert ud.db().execute("SELECT COUNT(*) FROM unlim_files").fetchone()[0] == 0
    assert "их скачивает Диск" in capsys.readouterr().out


def test_changed_photo_goes_back_to_the_queue(unlim_db):
    """Фото изменили на Диске (другой MD5): скачанная копия устарела — качаем новую версию."""
    ud = unlim_db
    item = _unlim_item("a.jpg", 10)
    ud.save_listed_files([item])
    ud.db().execute("UPDATE unlim_files SET status='downloaded'")
    ud.save_listed_files([item])  # без изменений — остаётся скачанным
    assert ud.status_counts() == {"downloaded": 1}
    changed = dict(item, meta=dict(item["meta"], md5="b" * 32, size=12))
    ud.save_listed_files([changed])
    row = ud.db().execute("SELECT status, size, md5 FROM unlim_files").fetchone()
    assert tuple(row) == ("pending", 12, "b" * 32)


def test_collect_reports_a_full_disk_not_a_yandex_outage(unlim_db, monkeypatch, capsys):
    """База не записалась посреди списка (место кончилось): код 4 и «local_disk», а не «Яндекс не отвечает»."""
    import sqlite3

    ud = unlim_db
    monkeypatch.setattr(ud, "LIST_PAGE", 2)
    monkeypatch.setattr(ud, "load_cookies", lambda: {})
    monkeypatch.setattr(ud, "make_session", lambda cookies: type("S", (), {"close": lambda self: None})())
    monkeypatch.setattr(ud, "get_sk_uid", lambda s: ("sk", "uid"))
    root = {"type": "dir", "path": "/photounlim"}
    pages = {0: [root, _unlim_item("a.jpg", 10), _unlim_item("b.jpg", 20)], 2: [root, _unlim_item("c.jpg", 30)]}
    monkeypatch.setattr(ud, "api_call", lambda s, sk, uid, method, params=None, retries=3: pages[params["offset"]])
    real_save = ud.save_listed_files

    def save(items):
        if any(i["name"] == "c.jpg" for i in items):
            raise sqlite3.OperationalError("database or disk is full")
        return real_save(items)

    monkeypatch.setattr(ud, "save_listed_files", save)
    assert ud.cmd_collect() == ud.EXIT_DISK_FULL
    out = capsys.readouterr().out
    assert "local_disk |" in out and "server_50x" not in out and "фотосрез" not in out


def test_queue_knows_rows_from_the_old_photoslice_list(tmp_path):
    """Строки старого списка не из безлимита: сбор списка до скачивания уберёт их, иначе они
    ушли бы второй копией в _unlim."""
    import sqlite3

    from webapp.queue_db import unlim_has_rows_outside

    db = tmp_path / "unlim_state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE unlim_files (file_id TEXT PRIMARY KEY, status TEXT)")
    con.executemany("INSERT INTO unlim_files VALUES (?, ?)",
                    [("/photounlim/a.jpg", "pending"), ("photounlim:/b.jpg", "pending"),
                     ("disk:/Фото/c.jpg", "downloaded")])
    con.commit()
    assert not unlim_has_rows_outside(db)
    con.execute("INSERT INTO unlim_files VALUES ('/disk/Фото/d.jpg', 'pending')")
    con.commit()
    con.close()
    assert unlim_has_rows_outside(db)
