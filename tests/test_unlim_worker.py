"""Тесты воркера безлимита: распаковка ZIP и учёт того, что реально приехало."""

import os
import sqlite3
import zipfile

import pytest

import unlim_worker as uw

# ── защита от zip-slip ──────────────────────────────────────────────────────


def test_safe_extract_normal_name(tmp_path):
    base = str(tmp_path)
    dst = uw.safe_extract_dst(base, "2020/photo.jpg")
    assert dst == os.path.join(base, "2020", "photo.jpg")


def test_safe_extract_strips_parent_refs(tmp_path):
    base = str(tmp_path / "extract")
    os.makedirs(base)
    dst = uw.safe_extract_dst(base, "../../evil.txt")
    assert dst == os.path.join(base, "evil.txt")


def test_safe_extract_rejects_drive_letter(tmp_path):
    """'C:/evil.txt' — os.path.join просто выбросил бы базу и записал в корень диска."""
    base = str(tmp_path / "extract")
    os.makedirs(base)
    dst = uw.safe_extract_dst(base, "C:/evil.txt")
    assert dst == os.path.join(base, "evil.txt")


def test_safe_extract_rejects_absolute(tmp_path):
    base = str(tmp_path / "extract")
    os.makedirs(base)
    dst = uw.safe_extract_dst(base, "/etc/passwd")
    assert dst == os.path.join(base, "etc", "passwd")


def test_safe_extract_result_always_inside_base(tmp_path):
    base = str(tmp_path / "extract")
    os.makedirs(base)
    for name in ["../x", r"..\..\x", "C:/x", "//server/share/x", "./x", "a/../../x"]:
        dst = uw.safe_extract_dst(base, name)
        if dst is None:
            continue
        assert os.path.commonpath([os.path.realpath(base), os.path.realpath(dst)]) \
            == os.path.realpath(base), name


def test_safe_extract_empty_name(tmp_path):
    assert uw.safe_extract_dst(str(tmp_path), "../..") is None
    assert uw.safe_extract_dst(str(tmp_path), "") is None


def test_safe_extract_sanitizes_windows_forbidden_chars(tmp_path):
    """'12:30:00.jpg' раньше выбрасывался целиком — файл уходил в failed с ложной причиной."""
    base = str(tmp_path)
    dst = uw.safe_extract_dst(base, "2024/2024-05-01 12:30:00.jpg")
    assert dst == os.path.join(base, "2024", "2024-05-01 12_30_00.jpg")
    assert uw.safe_extract_dst(base, "what?.jpg") == os.path.join(base, "what_.jpg")


# ── сопоставление «что просили» ↔ «что приехало» ────────────────────────────


def test_split_marks_only_extracted_files():
    batch = [
        {"file_id": "1", "name": "a.jpg", "size": 100},
        {"file_id": "2", "name": "b.jpg", "size": 200},
        {"file_id": "3", "name": "c.jpg", "size": 300},
    ]
    res = uw.split_by_extracted(batch, [("a.jpg", 100), ("c.jpg", 300)])
    assert res.done == ["1", "3"]
    assert res.missing == ["2"]


def test_split_all_missing_when_zip_empty():
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    res = uw.split_by_extracted(batch, [])
    assert res.done == []
    assert res.missing == ["1"]


def test_split_respects_multiplicity():
    """Два одинаковых файла в пачке требуют двух записей в архиве."""
    batch = [
        {"file_id": "1", "name": "a.jpg", "size": 100},
        {"file_id": "2", "name": "a.jpg", "size": 100},
    ]
    res = uw.split_by_extracted(batch, [("a.jpg", 100)])
    assert res.done == ["1"]
    assert res.missing == ["2"]


def test_split_size_must_match():
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    res = uw.split_by_extracted(batch, [("a.jpg", 999)])
    assert res.done == []
    assert res.missing == ["1"]


def test_split_ignores_zip_subfolders():
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    res = uw.split_by_extracted(batch, [("2020/a.jpg", 100)])
    assert res.done == ["1"]
    assert res.missing == []


def test_split_falls_back_to_size_when_zip_renamed():
    """Яндекс может переименовать файл в архиве — по размеру всё равно засчитываем."""
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    res = uw.split_by_extracted(batch, [("a (1).jpg", 100)])
    assert res.done == ["1"]
    assert res.missing == []


def test_split_fallback_does_not_invent_files():
    """Если в архиве нет файла подходящего размера — честно считаем непришедшим."""
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    res = uw.split_by_extracted(batch, [("b.jpg", 7)])
    assert res.done == []
    assert res.missing == ["1"]


def test_split_fallback_respects_counts():
    batch = [
        {"file_id": "1", "name": "a.jpg", "size": 100},
        {"file_id": "2", "name": "b.jpg", "size": 100},
    ]
    res = uw.split_by_extracted(batch, [("renamed.jpg", 100)])
    assert res.done == ["1"]
    assert res.missing == ["2"]


def test_split_does_not_cross_match_different_extensions_by_size():
    batch = [{"file_id": "1", "name": "clip.mp4", "size": 100}]
    res = uw.split_by_extracted(batch, [("unrelated.jpg", 100)])
    assert res.done == []
    assert res.missing == ["1"]


def test_split_by_extracted_preserves_paths_and_done_bytes():
    batch = [
        {"file_id": "1", "name": "a.jpg", "size": 100},
        {"file_id": "2", "name": "b.jpg", "size": 200},
        {"file_id": "3", "name": "c.jpg", "size": 300},
    ]
    extracted = [
        uw.ExtractedItem("a.jpg", 100, "/dst/a.jpg"),
        uw.ExtractedItem("c.jpg", 300, "/dst/c.jpg"),
    ]
    res = uw.split_by_extracted(batch, extracted)
    assert res.done == ["1", "3"]
    assert res.missing == ["2"]
    assert res.paths == {"1": "/dst/a.jpg", "3": "/dst/c.jpg"}

    # Подсчёт байт только по реально совпавшим done
    id_to_size = {f["file_id"]: f["size"] for f in batch}
    done_bytes = sum(id_to_size.get(fid, 0) for fid in res.done)
    assert done_bytes == 400  # 100 + 300, без 200 байт от missing


def test_split_matches_sanitized_archive_names():
    """В архиве имя уже без ':' (так его распаковали), в БД — оригинал с ':'."""
    batch = [{"file_id": "1", "name": "2024-05-01 12:30:00.jpg", "size": 100}]
    res = uw.split_by_extracted(batch, [("2024-05-01 12_30_00.jpg", 100)])
    assert res.done == ["1"]


def test_split_exact_match_wins_over_renamed():
    """Точное совпадение имени предпочтительнее переименованного файла того же размера."""
    batch = [{"file_id": "1", "name": "a.jpg", "size": 100}]
    extracted = [
        uw.ExtractedItem("a (1).jpg", 100, "/dst/a (1).jpg"),
        uw.ExtractedItem("a.jpg", 100, "/dst/a.jpg"),
    ]
    res = uw.split_by_extracted(batch, extracted)
    assert res.paths == {"1": "/dst/a.jpg"}


def test_clean_stem():
    assert uw._clean_stem("photo (1).jpg") == ("photo", ".jpg")
    assert uw._clean_stem("photo_2.jpg") == ("photo", ".jpg")
    assert uw._clean_stem("photo.jpg") == ("photo", ".jpg")


def test_claim_free_path_takes_the_name_at_once(tmp_path):
    """Имя занимается сразу (O_EXCL): второй воркер с тем же именем получит следующее."""
    orig = tmp_path / "test.jpg"
    orig.write_text("first", encoding="utf-8")
    cand1 = uw._claim_free_path(str(orig))
    assert cand1.endswith("test_1.jpg") and os.path.exists(cand1)
    cand2 = uw._claim_free_path(str(orig))
    assert cand2.endswith("test_2.jpg")
    assert orig.read_text(encoding="utf-8") == "first"


def test_place_file_never_overwrites_another_photo(tmp_path):
    (tmp_path / "IMG_0001.JPG").write_bytes(b"first camera")
    tmp = tmp_path / "w1.part"
    tmp.write_bytes(b"second camera")
    final = uw._place_file(str(tmp), str(tmp_path / "IMG_0001.JPG"))
    assert final.endswith("IMG_0001_1.JPG") and not tmp.exists()
    assert (tmp_path / "IMG_0001.JPG").read_bytes() == b"first camera"
    assert (tmp_path / "IMG_0001_1.JPG").read_bytes() == b"second camera"


# ── БД: попытки и переход в failed ──────────────────────────────────────────


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "unlim_state.db")
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE unlim_files (
            file_id TEXT PRIMARY KEY, cluster_id TEXT, name TEXT, size INTEGER,
            md5 TEXT, mime TEXT, status TEXT DEFAULT 'pending', local_path TEXT,
            error TEXT, worker_id INTEGER, assigned_at TIMESTAMP,
            downloaded_at TIMESTAMP
        );
        """
    )
    con.executemany(
        "INSERT INTO unlim_files (file_id, name, size) VALUES (?, ?, ?)",
        [("1", "a.jpg", 10), ("2", "b.jpg", 20)],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(uw, "DB_FILE", path)
    return path


def status_of(db_path, file_id):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT status, attempts FROM unlim_files WHERE file_id=?", (file_id,)
        ).fetchone()
    finally:
        con.close()


def test_migration_adds_attempts_column(db):
    uw.ensure_attempts_column()
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(unlim_files)")}
    con.close()
    assert "attempts" in cols


def test_take_batch_returns_names_and_sizes(db):
    uw.ensure_attempts_column()
    batch = uw.take_batch(worker_id=1, size=10)
    assert {it["file_id"] for it in batch} == {"1", "2"}
    assert all("name" in it and "size" in it for it in batch)
    # повторный вызов ничего не отдаёт — файлы уже in_progress
    assert uw.take_batch(worker_id=2, size=10) == []


def test_release_to_pending_keeps_attempts(db):
    """Возврат в очередь без вины файла не сжигает попытку."""
    uw.ensure_attempts_column()
    uw.take_batch(worker_id=1, size=10)
    assert status_of(db, "1") == ("in_progress", 0)
    uw._release_to_pending(["1"])
    assert status_of(db, "1") == ("pending", 0)
    assert status_of(db, "2") == ("in_progress", 0)


def test_release_to_pending_never_touches_downloaded(db):
    uw.ensure_attempts_column()
    uw.mark_done(["1"])
    uw._release_to_pending(["1"])
    assert status_of(db, "1") == ("downloaded", 0)


def test_owner_guard_ignores_foreign_worker(db):
    """После recover_stuck пачку мог забрать другой воркер — старый не должен перетирать статус."""
    uw.ensure_attempts_column()
    uw.take_batch(worker_id=1, size=10)
    uw._release_to_pending(["1"], worker_id=2)
    uw.mark_pending(["2"], "boom", worker_id=2)
    assert status_of(db, "1") == ("in_progress", 0)
    assert status_of(db, "2") == ("in_progress", 0)
    uw.mark_pending(["2"], "boom", worker_id=1)
    assert status_of(db, "2") == ("pending", 1)


def test_heartbeat_keeps_batch_alive(db):
    uw.ensure_attempts_column()
    uw.take_batch(worker_id=1, size=10)
    con = sqlite3.connect(db)
    con.execute("UPDATE unlim_files SET assigned_at = datetime('now', '-2 hours')")
    con.commit()
    con.close()
    uw.heartbeat(["1"], worker_id=1)
    # «2» без heartbeat — зависла, «1» жива
    assert uw.recover_stuck(timeout_sec=600) == 1
    assert status_of(db, "1") == ("in_progress", 0)
    assert status_of(db, "2") == ("pending", 0)


def test_mark_pending_counts_attempts_then_fails(db):
    uw.ensure_attempts_column()
    for expected_attempt in (1, 2):
        uw.mark_pending(["1"], "boom")
        status, attempts = status_of(db, "1")
        assert (status, attempts) == ("pending", expected_attempt)

    uw.mark_pending(["1"], "boom")
    status, attempts = status_of(db, "1")
    assert status == "failed"          # больше не крутится в бесконечном цикле
    assert attempts == uw.MAX_ATTEMPTS


def test_mark_done(db):
    uw.ensure_attempts_column()
    uw.mark_done(["1"])
    status, _ = status_of(db, "1")
    assert status == "downloaded"


def test_mark_done_saves_local_path(tmp_path, monkeypatch):
    db_file = tmp_path / "test_mark_done.db"
    monkeypatch.setattr(uw, "DB_FILE", str(db_file))
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE unlim_files (
            file_id TEXT PRIMARY KEY, status TEXT, attempts INTEGER, error TEXT, local_path TEXT, downloaded_at TIMESTAMP, worker_id INTEGER
        );
        INSERT INTO unlim_files (file_id, status) VALUES ('f1', 'pending');
        INSERT INTO unlim_files (file_id, status) VALUES ('f2', 'in_progress');
        """
    )
    con.close()

    paths = {"f1": str(tmp_path / "f1.jpg"), "f2": str(tmp_path / "f2.jpg")}
    uw.mark_done(["f1", "f2"], paths=paths)

    con = sqlite3.connect(db_file)
    rows = dict(con.execute("SELECT file_id, local_path FROM unlim_files").fetchall())
    statuses = dict(con.execute("SELECT file_id, status FROM unlim_files").fetchall())
    con.close()

    assert statuses["f1"] == "downloaded"
    assert statuses["f2"] == "downloaded"
    assert rows["f1"] == str(tmp_path / "f1.jpg")
    assert rows["f2"] == str(tmp_path / "f2.jpg")


def test_take_batch_respects_max_bytes(tmp_path, monkeypatch):
    db_file = tmp_path / "unlim.db"
    monkeypatch.setattr(uw, "DB_FILE", str(db_file))
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE unlim_files (
            file_id TEXT PRIMARY KEY, name TEXT, size INTEGER,
            status TEXT DEFAULT 'pending', worker_id INTEGER, assigned_at TIMESTAMP
        );
        INSERT INTO unlim_files (file_id, name, size) VALUES
            ('1', 'small1.jpg', 100),
            ('2', 'small2.jpg', 100),
            ('3', 'big.mp4', 500000000);
        """
    )
    con.close()

    batch = uw.take_batch(1, size=10, max_bytes=150)
    assert len(batch) == 1
    assert batch[0]["file_id"] == "1"


def test_take_batch_isolates_attempted_file(tmp_path, monkeypatch):
    """Файлы с attempts > 0 изолируются в соло-пачку по 1 файлу."""
    db_file = tmp_path / "unlim_att.db"
    monkeypatch.setattr(uw, "DB_FILE", str(db_file))
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE unlim_files (
            file_id TEXT PRIMARY KEY, name TEXT, size INTEGER, attempts INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending', worker_id INTEGER, assigned_at TIMESTAMP
        );
        INSERT INTO unlim_files (file_id, name, size, attempts) VALUES
            ('1', 'clean1.jpg', 100, 0),
            ('2', 'failing.jpg', 100, 1),
            ('3', 'clean2.jpg', 100, 0);
        """
    )
    con.close()

    # Сначала забираются чистые файлы (attempts = 0)
    batch1 = uw.take_batch(1, size=10)
    assert [b["file_id"] for b in batch1] == ["1", "3"]

    # Затем проблемный файл забирается ровно в одиночку (соло-пачка)
    batch2 = uw.take_batch(1, size=10)
    assert len(batch2) == 1
    assert batch2[0]["file_id"] == "2"


def test_recover_stuck_with_worker_id(tmp_path, monkeypatch):
    db_file = tmp_path / "test_unlim_stuck.db"
    monkeypatch.setattr(uw, "DB_FILE", str(db_file))
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE unlim_files (
            file_id TEXT PRIMARY KEY, status TEXT, worker_id INTEGER, assigned_at TIMESTAMP
        );
        INSERT INTO unlim_files VALUES ('f1', 'in_progress', 1, CURRENT_TIMESTAMP);
        INSERT INTO unlim_files VALUES ('f2', 'in_progress', 2, CURRENT_TIMESTAMP);
        """
    )
    con.close()

    n = uw.recover_stuck(worker_id=1)
    assert n == 1

    con = sqlite3.connect(db_file)
    rows = dict(con.execute("SELECT file_id, status FROM unlim_files").fetchall())
    con.close()
    assert rows["f1"] == "pending"
    assert rows["f2"] == "in_progress"


def test_reset_failed(tmp_path, monkeypatch):
    db_file = tmp_path / "test_unlim_reset.db"
    monkeypatch.setattr(uw, "DB_FILE", str(db_file))
    con = sqlite3.connect(db_file)
    con.executescript(
        """
        CREATE TABLE unlim_files (
            file_id TEXT PRIMARY KEY, status TEXT, attempts INTEGER, error TEXT
        );
        INSERT INTO unlim_files VALUES ('f1', 'failed', 3, 'some error');
        INSERT INTO unlim_files VALUES ('f2', 'pending', 1, 'prev error');
        INSERT INTO unlim_files VALUES ('f3', 'downloaded', 0, NULL);
        """
    )
    con.close()

    n = uw.reset_failed()
    assert n == 2

    con = sqlite3.connect(db_file)
    rows = {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT file_id, status, attempts, error FROM unlim_files").fetchall()}
    con.close()
    assert rows["f1"] == ("pending", 0, None)
    assert rows["f2"] == ("pending", 0, None)
    assert rows["f3"] == ("downloaded", 0, None)


# ── подготовка ZIP: типизированные ошибки ───────────────────────────────────


class _FakeSession:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def post(self, url, params=None, json=None, timeout=None):
        class Resp:
            def __init__(self, code, j, t):
                self.status_code = code
                self._j = j
                self.text = t

            def json(self):
                return self._j
        return Resp(self.status_code, self._json, self.text)


@pytest.mark.parametrize(
    "status,text,kind,needle",
    [
        (504, "Gateway Timeout", "server_50x", "504"),
        (429, "Too Many Requests", "rate_limit", "429"),
        (401, "Unauthorized", "auth_expired", "401"),
        (403, "Forbidden", "auth_expired", "403"),
        (500, "Internal Server Error", "server_50x", "500"),
        (404, "Not Found", "prepare_failed", "404"),
    ],
)
def test_prepare_zip_http_errors(status, text, kind, needle):
    url, err = uw.prepare_zip(_FakeSession(status, text=text), "sk", "123", ["p1"])
    assert url is None
    assert err.kind == kind
    assert needle in str(err)
    # str() начинается с категории — по ней диагностика Web UI читает логи
    assert str(err).startswith(f"{kind} | ")


def test_prepare_zip_success_normalizes_protocol_relative_url():
    s200 = _FakeSession(200, {"download_url": "//downloader.disk.yandex.ru/zip/123"})
    url, err = uw.prepare_zip(s200, "sk", "123", ["p1"])
    assert err is None
    assert url == "https://downloader.disk.yandex.ru/zip/123"


def test_prepare_zip_service_error_and_bad_json():
    s_err = _FakeSession(200, {"error": {"title": "QuotaExceeded"}})
    url, err = uw.prepare_zip(s_err, "sk", "123", ["p1"])
    assert url is None
    assert err.kind == "prepare_failed"
    assert "QuotaExceeded" in str(err)

    class BadJsonSession:
        def post(self, *args, **kwargs):
            class BadResp:
                status_code = 200
                text = "<html>not json</html>"

                def json(self):
                    raise ValueError("Expecting value")
            return BadResp()

    url, err = uw.prepare_zip(BadJsonSession(), "sk", "123", ["p1"])
    assert url is None
    assert err.kind == "invalid_json"


def test_prepare_error_policy():
    """Серверные сбои не сжигают попытку файла и ждут дольше, чем прочие ошибки."""
    assert uw.PrepareError("rate_limit", "x").is_server_issue
    assert uw.PrepareError("server_50x", "x", 503).is_server_issue
    assert uw.PrepareError("timeout", "x").is_server_issue
    assert not uw.PrepareError("prepare_failed", "x", 404).is_server_issue
    assert not uw.PrepareError("auth_expired", "x", 401).is_server_issue
    assert uw.PrepareError("auth_expired", "x", 401).is_auth

    assert uw.PrepareError("rate_limit", "x", 429).retry_delay == 45
    assert uw.PrepareError("server_50x", "x", 504).retry_delay == 15
    assert uw.PrepareError("server_50x", "x", 502).retry_delay == 10
    assert uw.PrepareError("prepare_failed", "x", 400).retry_delay == 5


# ── цикл воркера: обработка сбоев ───────────────────────────────────────────


@pytest.fixture()
def sleeps(monkeypatch):
    """Перехватывает time.sleep воркера: паузы не ждём, но запоминаем."""
    recorded: list[float] = []
    monkeypatch.setattr(uw.time, "sleep", lambda s: recorded.append(s))
    return recorded


@pytest.fixture()
def quiet_worker(db, sleeps, monkeypatch):
    """Воркер с подготовленной БД, без реальных пауз и сети (куки считаются живыми)."""
    uw.ensure_attempts_column()
    monkeypatch.setattr(uw, "get_sk_uid", lambda s: ("sk", "uid"))
    return uw.Worker(worker_id=1, batch_size=1)


def test_solo_server_failure_burns_attempt_only_after_streak(db, quiet_worker):
    """Одиночный файл, который сервер не отдаёт по своей вине, не должен крутиться вечно."""
    (item,) = uw.take_batch(1, size=1)
    err = uw.PrepareError("server_50x", "HTTP 503", 503)

    for _ in range(uw.SOLO_SERVER_FAIL_STREAK - 1):
        quiet_worker._on_prepare_failed([item], err)
        assert status_of(db, item["file_id"]) == ("pending", 0)
        uw.take_batch(1, size=1)

    quiet_worker._on_prepare_failed([item], err)
    assert status_of(db, item["file_id"]) == ("pending", 1)  # попытка сожжена


def test_permanent_prepare_failure_burns_attempt_immediately(db, quiet_worker):
    (item,) = uw.take_batch(1, size=1)
    quiet_worker._on_prepare_failed([item], uw.PrepareError("prepare_failed", "HTTP 404", 404))
    assert status_of(db, item["file_id"]) == ("pending", 1)


def test_multi_file_prepare_failure_releases_and_shrinks_batch(db, sleeps, monkeypatch):
    uw.ensure_attempts_column()
    monkeypatch.setattr(uw, "get_sk_uid", lambda s: ("sk", "uid"))
    w = uw.Worker(worker_id=1, batch_size=10, mode="zip")
    batch = uw.take_batch(1, size=10)
    assert len(batch) == 2

    w._on_prepare_failed(batch, uw.PrepareError("server_50x", "HTTP 502", 502))
    assert status_of(db, "1") == ("pending", 0)
    assert status_of(db, "2") == ("pending", 0)
    assert w.batch_size == 5


def test_auth_failure_exits_worker_when_cookies_really_dead(db, quiet_worker, monkeypatch):
    def dead(_s):
        raise uw.AuthExpiredError("passport redirect")

    monkeypatch.setattr(uw, "get_sk_uid", dead)
    (item,) = uw.take_batch(1, size=1)
    with pytest.raises(SystemExit) as e:
        quiet_worker._on_prepare_failed([item], uw.PrepareError("auth_expired", "HTTP 401", 401))
    assert e.value.code == uw.EXIT_AUTH
    assert status_of(db, item["file_id"]) == ("pending", 0)  # файл возвращён в очередь


def test_forbidden_with_live_cookies_is_a_refusal_not_auth(db, quiet_worker):
    """403 на bulk-prepare при живой сессии — Яндекс ограничил функцию, куки менять бессмысленно."""
    (item,) = uw.take_batch(1, size=1)
    quiet_worker._on_prepare_failed([item], uw.PrepareError("auth_expired", "HTTP 403", 403))
    assert status_of(db, item["file_id"]) == ("pending", 0)  # без выхода и без сжигания попытки


def test_outage_streak_stops_burning_attempts(db, quiet_worker, sleeps):
    """Пять пачек подряд без единого файла на разных файлах = отказ Яндекса, а не битые файлы."""
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO unlim_files (file_id, name, size) VALUES (?, ?, ?)",
                    [(str(i), f"f{i}.jpg", 10) for i in range(3, 8)])
    con.commit()
    con.close()
    err = uw.PrepareError("prepare_failed", "HTTP 400 bad request", 400)  # «постоянная» ошибка

    for _ in range(uw.OUTAGE_STREAK_THRESHOLD - 1):
        (item,) = uw.take_batch(1, size=1)
        quiet_worker._on_prepare_failed([item], err)
        assert status_of(db, item["file_id"])[1] == 1  # до порога попытки сжигаются

    (item,) = uw.take_batch(1, size=1)
    quiet_worker._on_prepare_failed([item], err)
    assert status_of(db, item["file_id"]) == ("pending", 0)  # порог: попытка НЕ сожжена
    assert sleeps[-1] == uw.OUTAGE_PAUSES_SEC[0]


def test_rate_limit_is_not_reported_as_disabled_bulk(db, quiet_worker, sleeps, capsys):
    """Ровно случай из лога: серия 429 — это лимит запросов, а не отключённая функция."""
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO unlim_files (file_id, name, size) VALUES (?, ?, ?)",
                    [(str(i), f"f{i}.jpg", 10) for i in range(3, 8)])
    con.commit()
    con.close()
    err = uw.PrepareError("rate_limit", "HTTP 429 Too Many Requests", 429)
    for _ in range(uw.OUTAGE_STREAK_THRESHOLD):
        (item,) = uw.take_batch(1, size=1)
        quiet_worker._on_prepare_failed([item], err)
        assert status_of(db, item["file_id"])[1] == 0  # 429 не сжигает попытки

    out = capsys.readouterr().out
    assert "bulk_unavailable" not in out
    assert "массовое скачивание ограничено" not in out
    assert "rate_limit | Яндекс 5 раз подряд" in out  # direct: счёт по файлам, не пачкам
    # Пауза растёт и не меньше базовой для 429
    assert sleeps[-1] >= uw.RATE_LIMIT_BASE_PAUSE_SEC
    assert max(sleeps) == uw.RATE_LIMIT_MAX_PAUSE_SEC or max(sleeps) > uw.RATE_LIMIT_BASE_PAUSE_SEC


def test_rate_limit_sets_shared_cooldown_for_all_workers(db, quiet_worker):
    assert uw.shared_cooldown_remaining() == 0  # таблицы ещё нет — паузы нет
    (item,) = uw.take_batch(1, size=1)
    quiet_worker._on_prepare_failed([item], uw.PrepareError("rate_limit", "HTTP 429", 429))
    remaining = uw.shared_cooldown_remaining()
    assert uw.RATE_LIMIT_BASE_PAUSE_SEC - 2 < remaining <= uw.RATE_LIMIT_BASE_PAUSE_SEC
    # Более короткая пауза не укорачивает уже назначенную
    uw.set_shared_cooldown(5)
    assert uw.shared_cooldown_remaining() > 30


def test_prepare_429_keeps_body_and_retry_after():
    class Resp:
        status_code = 429
        text = '{"error":{"id":"TooManyRequests","title":"Слишком много запросов"}}'
        headers = {"Retry-After": "120", "Content-Type": "application/json"}

    class S:
        def post(self, *a, **k):
            return Resp()

    url, err = uw.prepare_zip(S(), "sk", "1", ["p"])
    assert url is None and err.kind == "rate_limit"
    assert err.retry_after == 120 and err.retry_delay == 120
    assert "Слишком много запросов" in str(err)  # тело ответа попадает в лог
    assert "t.me" not in str(err)


def test_outage_cause_follows_the_failures(quiet_worker):
    w = quiet_worker
    w._streak_causes.update({"server": 3, "rate_limit": 1})
    assert w._dominant_cause("rate_limit") == "server"
    w._streak_causes.update({"rate_limit": 2})
    assert w._dominant_cause("rate_limit") == "rate_limit"


def test_outage_gives_up_after_an_hour(db, quiet_worker):
    quiet_worker._fail_streak = uw.OUTAGE_STREAK_THRESHOLD
    quiet_worker._streak_files = {"a", "b", "c"}
    quiet_worker._last_success_at -= uw.OUTAGE_GIVE_UP_SEC + 1
    (item,) = uw.take_batch(1, size=1)
    with pytest.raises(SystemExit) as e:
        quiet_worker._on_prepare_failed([item], uw.PrepareError("server_50x", "HTTP 503", 503))
    assert e.value.code == uw.EXIT_OUTAGE
    assert status_of(db, item["file_id"]) == ("pending", 0)


def test_on_downloaded_marks_only_arrived_files(db, quiet_worker, tmp_path):
    batch = uw.take_batch(1, size=10)  # a.jpg (10 байт) и b.jpg (20 байт), владелец — w1
    extracted = [uw.ExtractedItem("a.jpg", 10, str(tmp_path / "a.jpg"))]
    quiet_worker._on_downloaded(batch, uw.FetchResult(extracted, 10, None), dur=1.0)

    assert status_of(db, "1") == ("downloaded", 0)
    assert status_of(db, "2") == ("pending", 1)  # не приехал — попытка сожжена
    assert quiet_worker.total_files == 1
    assert quiet_worker.total_bytes == 10


def test_on_downloaded_local_disk_failure_does_not_blame_files(db, quiet_worker):
    batch = uw.take_batch(1, size=10)
    failure = uw.FetchFailure("local_disk", "ошибка записи архива: [Errno 28] No space left")
    quiet_worker._on_downloaded(batch, uw.FetchResult([], 0, failure), dur=1.0)
    assert status_of(db, "1") == ("pending", 0)
    assert status_of(db, "2") == ("pending", 0)
    assert quiet_worker._disk_full_streak == 1


def test_disk_full_gives_up(db, quiet_worker, monkeypatch):
    monkeypatch.setattr(uw, "check_disk_space", lambda d, min_bytes: (False, 10))
    quiet_worker._disk_full_streak = uw.DISK_FULL_GIVE_UP - 1
    batch = uw.take_batch(1, size=10)
    with pytest.raises(SystemExit) as e:
        quiet_worker._process(batch)
    assert e.value.code == uw.EXIT_DISK_FULL
    assert status_of(db, "1") == ("pending", 0)


# ── интеграция: злой архив не вылезает из папки ─────────────────────────────


def test_download_and_extract_contains_malicious_zip(tmp_path, monkeypatch):
    extract_dir = tmp_path / "extract"
    zip_dir = tmp_path / "zips"
    monkeypatch.setattr(uw, "EXTRACT_DIR", str(extract_dir))
    monkeypatch.setattr(uw, "ZIP_TMP_DIR", str(zip_dir))

    src_zip = tmp_path / "payload.zip"
    with zipfile.ZipFile(src_zip, "w") as zf:
        zf.writestr("ok.jpg", b"good")
        zf.writestr("../../escaped.txt", b"bad")
        zf.writestr("C:/escaped2.txt", b"bad")

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield self._data

    class FakeSession:
        def get(self, url, stream=False, timeout=None, **kwargs):
            return FakeResponse(src_zip.read_bytes())

    res = uw.download_and_extract(FakeSession(), "http://x", 1, 1)

    assert res.failure is None
    names = sorted(item.name for item in res.extracted)
    assert names == ["escaped.txt", "escaped2.txt", "ok.jpg"]
    # всё легло внутрь extract_dir и никуда больше
    assert (extract_dir / "ok.jpg").exists()
    assert (extract_dir / "escaped.txt").exists()
    assert (extract_dir / "escaped2.txt").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()
    assert res.total_bytes == 10  # 4 ("good") + 3 + 3
    # временный ZIP убран
    assert not any(zip_dir.iterdir()) if zip_dir.exists() else True


class _FakeStream:
    def __init__(self, data, headers=None, status=200):
        self._data = data
        self.headers = headers or {}
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size):
        yield self._data


def _session_with(resp):
    class S:
        def get(self, url, stream=False, timeout=None, **kwargs):
            return resp
    return S()


def test_download_and_extract_html_instead_of_zip(tmp_path, monkeypatch):
    """Так выглядит «массовое скачивание отключено»: 200 OK и HTML-страница вместо архива."""
    monkeypatch.setattr(uw, "EXTRACT_DIR", str(tmp_path / "extract"))
    monkeypatch.setattr(uw, "ZIP_TMP_DIR", str(tmp_path / "zips"))
    resp = _FakeStream(b"<html><body>Service unavailable</body></html>",
                       headers={"Content-Type": "text/html; charset=utf-8"})
    res = uw.download_and_extract(_session_with(resp), "http://x", 1, 7)
    assert res.extracted == []
    assert res.failure.kind == "not_zip"
    assert res.failure.is_server_issue
    assert not list((tmp_path / "zips").iterdir())  # ни .part, ни .zip не осталось


def test_download_and_extract_garbage_is_not_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(uw, "EXTRACT_DIR", str(tmp_path / "extract"))
    monkeypatch.setattr(uw, "ZIP_TMP_DIR", str(tmp_path / "zips"))
    res = uw.download_and_extract(_session_with(_FakeStream(b"garbage-not-a-zip")), "http://x", 1, 8)
    assert res.failure.kind == "not_zip"
    assert not list((tmp_path / "zips").iterdir())


def test_download_and_extract_http_errors_are_typed(tmp_path, monkeypatch):
    monkeypatch.setattr(uw, "EXTRACT_DIR", str(tmp_path / "extract"))
    monkeypatch.setattr(uw, "ZIP_TMP_DIR", str(tmp_path / "zips"))
    res = uw.download_and_extract(_session_with(_FakeStream(b"", status=503)), "http://x", 1, 9)
    assert res.failure.kind == "server_50x"
    res = uw.download_and_extract(_session_with(_FakeStream(b"", status=429)), "http://x", 1, 10)
    assert res.failure.kind == "rate_limit"
    res = uw.download_and_extract(_session_with(_FakeStream(b"", status=403)), "http://x", 1, 11)
    assert res.failure.kind == "refused"


def test_check_disk_space(tmp_path, monkeypatch):
    import shutil
    has_space, free_bytes = uw.check_disk_space(str(tmp_path), min_bytes=100)
    assert isinstance(has_space, bool)
    assert isinstance(free_bytes, int)

    # Имитация нехватки места
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda path: shutil._ntuple_diskusage(1000, 950, 50)
    )
    ok, free = uw.check_disk_space(str(tmp_path), min_bytes=100)
    assert ok is False
    assert free == 50


# ── режим direct: по одному файлу через mpfs/url ────────────────────────────


class _UrlResp:
    def __init__(self, status=200, data=None, text="", headers=None):
        self.status_code = status
        self._data = data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._data is None:
            raise ValueError("Expecting value")
        return self._data


def _url_session(resp):
    class S:
        def __init__(self):
            self.bodies = []

        def post(self, url, params=None, json=None, timeout=None):
            self.bodies.append(json)
            return resp
    return S()


def test_get_file_url_success_normalizes_protocol_relative_url():
    s = _url_session(_UrlResp(data={"file": "//downloader.disk.yandex.ru/disk/abc", "digest": "//x"}))
    url, err = uw.get_file_url(s, "sk", "123", "/photounlim/a.jpg")
    assert err is None
    assert url == "https://downloader.disk.yandex.ru/disk/abc"
    body = s.bodies[0]
    assert body["apiMethod"] == "mpfs/url"
    assert body["requestParams"] == {"path": "/photounlim/a.jpg"}


@pytest.mark.parametrize(
    "status,kind",
    [(500, "server_50x"), (504, "server_50x"), (429, "rate_limit"), (403, "auth_expired"), (404, "gone"),
     (400, "prepare_failed")],
)
def test_get_file_url_http_errors_are_typed(status, kind):
    url, err = uw.get_file_url(_url_session(_UrlResp(status, text="oops")), "sk", "1", "/p")
    assert url is None
    assert err.kind == kind
    assert str(err).startswith(f"{kind} | ")


def test_get_file_url_service_error_missing_link_and_bad_json():
    url, err = uw.get_file_url(_url_session(_UrlResp(data={"error": {"id": "HTTP_404"}})), "sk", "1", "/p")
    assert url is None and err.kind == "prepare_failed" and "HTTP_404" in str(err)
    url, err = uw.get_file_url(_url_session(_UrlResp(data={"something": 1})), "sk", "1", "/p")
    assert url is None and err.kind == "prepare_failed"
    url, err = uw.get_file_url(_url_session(_UrlResp(text="<html>")), "sk", "1", "/p")
    assert url is None and err.kind == "invalid_json"


def test_get_file_url_captcha_is_a_rate_limit():
    """Капча на частые запросы — пауза для всех воркеров, а не «куки протухли»."""
    resp = _UrlResp(403, text='{"type":"captcha","captcha":{"img_url":"..."}}')
    url, err = uw.get_file_url(_url_session(resp), "sk", "1", "/p")
    assert url is None
    assert err.kind == "rate_limit"
    assert "капч" in str(err)


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    extract = tmp_path / "extract"
    monkeypatch.setattr(uw, "EXTRACT_DIR", str(extract))
    monkeypatch.setattr(uw, "ZIP_TMP_DIR", str(tmp_path / "tmp"))
    return extract


def test_download_file_writes_file_and_leaves_no_part(dirs):
    dst = str(dirs / "a.jpg")
    path, failure = uw.download_file(_session_with(_FakeStream(b"0123456789")), "http://x", dst, 10, 1)
    assert failure is None
    assert path == dst
    assert (dirs / "a.jpg").read_bytes() == b"0123456789"
    assert not list((dirs.parent / "tmp").iterdir())


def test_download_file_never_overwrites_existing_file(dirs):
    dirs.mkdir()
    (dirs / "a.jpg").write_bytes(b"other")
    path, failure = uw.download_file(_session_with(_FakeStream(b"0123456789")), "http://x",
                                     str(dirs / "a.jpg"), 10, 1)
    assert failure is None
    assert path == str(dirs / "a_1.jpg")
    assert (dirs / "a.jpg").read_bytes() == b"other"


def test_download_file_size_mismatch_is_not_saved(dirs):
    path, failure = uw.download_file(_session_with(_FakeStream(b"short")), "http://x", str(dirs / "a.jpg"), 10, 1)
    assert path == ""
    assert failure.kind == "bad_file"
    assert not failure.is_server_issue  # файл не тот — попытку сжигаем
    assert not (dirs / "a.jpg").exists()
    assert not list((dirs.parent / "tmp").iterdir())


def test_download_file_truncated_transfer_is_a_connection_issue(dirs):
    resp = _FakeStream(b"01234", headers={"Content-Length": "10"})
    path, failure = uw.download_file(_session_with(resp), "http://x", str(dirs / "a.jpg"), 0, 1)
    assert failure.kind == "connection" and failure.is_server_issue


def test_download_file_html_and_http_errors(dirs):
    html = _FakeStream(b"<html>captcha</html>", headers={"Content-Type": "text/html"})
    _, failure = uw.download_file(_session_with(html), "http://x", str(dirs / "a.jpg"), 0, 1)
    assert failure.kind == "refused"
    _, failure = uw.download_file(_session_with(_FakeStream(b"", status=503)), "http://x", str(dirs / "a.jpg"), 0, 1)
    assert failure.kind == "server_50x"
    _, failure = uw.download_file(_session_with(_FakeStream(b"", status=429)), "http://x", str(dirs / "a.jpg"), 0, 1)
    assert failure.kind == "rate_limit"
    assert not (dirs / "a.jpg").exists()


class _DirectSession:
    """models-v2 mpfs/url + downloader: files — путь → байты, broken — путь → HTTP-код ссылки."""

    def __init__(self, files, broken=None):
        self.files = files
        self.broken = broken or {}
        self.asked = []

    def post(self, url, params=None, json=None, timeout=None):
        path = json["requestParams"]["path"]
        self.asked.append(path)
        code = self.broken.get(path)
        if code:
            return _UrlResp(code, text="error")
        return _UrlResp(data={"file": f"//dl/{path}"})

    def get(self, url, stream=False, timeout=None, **kwargs):
        return _FakeStream(self.files[url.split("//dl/", 1)[1]])


def test_direct_batch_downloads_each_file(db, quiet_worker, dirs, capsys):
    w = quiet_worker
    w.session = _DirectSession({"1": b"a" * 10, "2": b"b" * 20})
    batch = uw.take_batch(1, size=10)
    w.batch_idx = 1
    w._process(batch)

    assert status_of(db, "1") == ("downloaded", 0)
    assert status_of(db, "2") == ("downloaded", 0)
    assert (dirs / "a.jpg").read_bytes() == b"a" * 10
    assert (dirs / "b.jpg").read_bytes() == b"b" * 20
    con = sqlite3.connect(db)
    assert con.execute("SELECT local_path FROM unlim_files WHERE file_id='2'").fetchone()[0] == str(dirs / "b.jpg")
    con.close()
    assert w.total_files == 2 and w.total_bytes == 30
    # Строку прогресса читает Web UI (parseDownloadProgress)
    assert "batch 1: ✓ 2/2 files / 0.00 GB" in capsys.readouterr().out


def test_direct_failure_keeps_done_files_and_returns_the_rest(db, quiet_worker, dirs):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO unlim_files (file_id, name, size) VALUES ('3', 'c.jpg', 5)")
    con.commit()
    con.close()
    w = quiet_worker
    w.session = _DirectSession({"1": b"a" * 10, "3": b"c" * 5}, broken={"2": 500})
    w._process(uw.take_batch(1, size=10))

    assert status_of(db, "1") == ("downloaded", 0)
    assert status_of(db, "2") == ("pending", 0)  # 500 — вина Яндекса, попытку не сжигаем
    assert status_of(db, "3") == ("pending", 0)  # до него очередь не дошла — вернулся в очередь
    assert w.session.asked == ["1", "2"]


def test_direct_reuses_file_already_on_disk(db, quiet_worker, dirs):
    dirs.mkdir()
    (dirs / "a.jpg").write_bytes(b"a" * 10)
    w = quiet_worker
    w.session = _DirectSession({"2": b"b" * 20})
    w._process(uw.take_batch(1, size=10))
    assert status_of(db, "1") == ("downloaded", 0)
    assert w.session.asked == ["2"]  # за уже скачанным к Яндексу не ходили


def test_direct_forbidden_with_live_cookies_does_not_exit(db, quiet_worker, dirs):
    w = quiet_worker
    w.session = _DirectSession({}, broken={"1": 403, "2": 403})
    w._process(uw.take_batch(1, size=10))
    assert status_of(db, "1") == ("pending", 0)
    assert status_of(db, "2") == ("pending", 0)


def test_direct_stops_batch_when_another_worker_hit_rate_limit(db, quiet_worker, dirs):
    w = quiet_worker
    w.session = _DirectSession({"1": b"a" * 10, "2": b"b" * 20})
    batch = uw.take_batch(1, size=10)
    uw.set_shared_cooldown(60)
    w._process(batch)
    assert status_of(db, "1") == ("downloaded", 0)  # первый файл пачки качается всегда
    assert status_of(db, "2") == ("pending", 0)
    assert w.session.asked == ["1"]


def test_worker_rejects_unknown_mode():
    with pytest.raises(ValueError):
        uw.Worker(1, mode="torrent")


# ── серверы хранения и MD5 ──────────────────────────────────────────────────


class _Redirect:
    """Ответ downloader'а: 302 на сервер хранения."""

    def __init__(self, location):
        self.status_code = 302
        self.headers = {"Location": location}

    def close(self):
        pass


class _StorageSession:
    """downloader → 302 → сервер хранения; storage — host → байты или исключение."""

    def __init__(self, storage):
        self.storage = storage
        self.hits = []

    def get(self, url, stream=False, timeout=None, allow_redirects=True):
        host = url.split("/")[2]
        self.hits.append(host)
        if host == "dl":
            return _Redirect("https://" + url.split("/", 3)[3])
        data = self.storage[host]
        if isinstance(data, Exception):
            raise data
        return _FakeStream(data)


def test_download_file_follows_redirect_and_checks_md5(dirs):
    import hashlib
    s = _StorageSession({"s1klg.storage.yandex.net": b"photo"})
    good = hashlib.md5(b"photo").hexdigest()
    path, failure = uw.download_file(s, "https://dl/s1klg.storage.yandex.net/f", str(dirs / "a.jpg"),
                                     5, 1, expected_md5=good)
    assert failure is None and (dirs / "a.jpg").read_bytes() == b"photo"

    path, failure = uw.download_file(s, "https://dl/s1klg.storage.yandex.net/f", str(dirs / "b.jpg"),
                                     5, 1, expected_md5="0" * 32)
    assert failure.kind == "bad_file" and "MD5" in str(failure)
    assert not (dirs / "b.jpg").exists()


def test_download_file_silent_storage_is_storage_down(dirs):
    import requests
    s = _StorageSession({"s2klg.storage.yandex.net": requests.exceptions.ReadTimeout("read timed out")})
    path, failure = uw.download_file(s, "https://dl/s2klg.storage.yandex.net/f", str(dirs / "a.jpg"), 5, 1)
    assert path == ""
    assert failure.kind == "storage_down"
    assert failure.host == "s2klg.storage.yandex.net"
    assert str(failure).startswith("storage_down | ")  # маркер для диагностики Web UI


def test_download_file_skips_known_dead_storage_without_waiting(dirs):
    s = _StorageSession({})
    path, failure = uw.download_file(s, "https://dl/s3klg.storage.yandex.net/f", str(dirs / "a.jpg"), 5, 1,
                                     host_is_dead=lambda h: h == "s3klg.storage.yandex.net")
    assert failure.kind == "storage_skip"
    assert s.hits == ["dl"]  # к самому серверу хранения не ходили


def test_dead_hosts_are_shared_and_expire(db):
    assert not uw.is_host_dead("s4klg.storage.yandex.net")  # таблицы ещё нет
    uw.mark_host_dead("s4klg.storage.yandex.net")
    assert uw.is_host_dead("s4klg.storage.yandex.net")
    assert not uw.is_host_dead("s5klg.storage.yandex.net")
    uw.mark_host_dead("s4klg.storage.yandex.net", ttl=-1)
    assert not uw.is_host_dead("s4klg.storage.yandex.net")


class _StorageDirectSession(_StorageSession):
    """mpfs/url отдаёт https://dl/<host>/<file_id>; hosts — file_id → сервер хранения."""

    def __init__(self, storage, hosts):
        super().__init__(storage)
        self.file_hosts = hosts

    def post(self, url, params=None, json=None, timeout=None):
        fid = json["requestParams"]["path"]
        return _UrlResp(data={"file": f"//dl/{self.file_hosts[fid]}/{fid}"})


def test_direct_defers_files_on_dead_storage_and_continues(db, quiet_worker, dirs, sleeps, capsys):
    import requests
    con = sqlite3.connect(db)
    con.execute("INSERT INTO unlim_files (file_id, name, size) VALUES ('3', 'c.jpg', 5)")
    con.commit()
    con.close()
    dead, alive = "s6klg.storage.yandex.net", "s7klg.storage.yandex.net"
    w = quiet_worker
    w.session = _StorageDirectSession(
        {dead: requests.exceptions.ConnectTimeout("timed out"), alive: b"c" * 5},
        {"1": dead, "2": dead, "3": alive},
    )
    w._process(uw.take_batch(1, size=10))

    assert status_of(db, "1") == ("pending", 0)  # отложен, попытка не сожжена
    assert status_of(db, "2") == ("pending", 0)  # тот же сервер — отложен без ожидания
    assert status_of(db, "3") == ("downloaded", 0)  # пачка не прервалась
    assert w.session.hits.count(dead) == 1
    assert uw.is_host_dead(dead)
    out = capsys.readouterr().out
    assert out.count("storage_down | ") == 1
    assert "отложено 2" in out
    assert w._fail_streak == 0  # недоступный сервер — не отказ Яндекса в целом


def test_direct_all_deferred_pauses_then_exits_5(db, quiet_worker, dirs, sleeps):
    uw.ensure_attempts_column()  # колонка storage_host
    uw.mark_host_dead("s8klg.storage.yandex.net")
    w = quiet_worker
    w.session = _StorageDirectSession({}, {"1": "s8klg.storage.yandex.net", "2": "s8klg.storage.yandex.net"})
    w._process(uw.take_batch(1, size=10))
    assert sleeps[-1] == 5
    assert status_of(db, "1") == ("pending", 0)

    # Файлы молчащего сервера в очереди есть, но take_batch их не отдаёт —
    # воркер не крутится вхолостую, а сразу говорит «ждём Яндекса» (код 5)
    assert uw.take_batch(1, size=10) == []
    assert w.run() == uw.EXIT_DEFERRED
    assert status_of(db, "2") == ("pending", 0)


def test_all_deferred_for_an_hour_exits_5(db, quiet_worker, sleeps):
    quiet_worker._last_success_at -= uw.OUTAGE_GIVE_UP_SEC + 1
    with pytest.raises(SystemExit) as e:
        quiet_worker._on_all_deferred(2)
    assert e.value.code == uw.EXIT_DEFERRED


def test_deferred_file_remembers_host_and_returns_when_host_revives(db, quiet_worker):
    uw.ensure_attempts_column()
    (item,) = uw.take_batch(1, size=1)
    uw.mark_deferred([item["file_id"]], "s9klg.storage.yandex.net", "сервер молчит", worker_id=1)
    con = sqlite3.connect(db)
    error, host = con.execute("SELECT error, storage_host FROM unlim_files WHERE file_id=?",
                              (item["file_id"],)).fetchone()
    con.close()
    assert error.startswith("storage_down | ") and host == "s9klg.storage.yandex.net"
    assert uw.count_deferred(db) == 1

    uw.mark_host_dead("s9klg.storage.yandex.net")
    assert all(b["file_id"] != item["file_id"] for b in uw.take_batch(1, size=10))
    uw.mark_host_dead("s9klg.storage.yandex.net", ttl=-1)  # сервер «ожил»
    assert any(b["file_id"] == item["file_id"] for b in uw.take_batch(1, size=10))


def test_persistent_500_on_file_defers_instead_of_burning(db, quiet_worker, dirs):
    import requests
    w = quiet_worker
    alive = "s5nrg.storage.yandex.net"

    class Broken(_StorageDirectSession):
        def get(self, url, stream=False, timeout=None, allow_redirects=True):
            if url.split("/")[2] == alive:
                self.hits.append(alive)
                raise requests.exceptions.HTTPError("500", response=type("R", (), {"status_code": 500})())
            return super().get(url, stream, timeout, allow_redirects)

    w.session = Broken({}, {"1": alive, "2": alive})
    w._process(uw.take_batch(1, size=10))
    assert status_of(db, "1") == ("pending", 0)
    assert status_of(db, "2") == ("pending", 0)
    assert w.session.hits.count(alive) == 4  # по одному повтору на файл
    assert w._fail_streak == 0  # не «отказ Яндекса в целом»


# ── файлы, которые не записать, удалённые файлы, тёзки ──────────────────────


def test_write_failures_end_the_worker_even_when_space_looks_free(db, quiet_worker, dirs, monkeypatch):
    """Место есть, а файл не пишется (нет прав, диск отключён): серия не обнуляется перед каждой
    пачкой — воркер выходит с кодом 4, а не перекачивает файл вечно."""
    monkeypatch.setattr(uw, "check_disk_space", lambda d, n: (True, 10 ** 12))
    w = quiet_worker
    w.session = _DirectSession({"1": b"a" * 10, "2": b"b" * 20})
    denied = uw.FetchFailure("local_disk", "ошибка записи файла: [Errno 13] Permission denied")
    monkeypatch.setattr(uw, "download_file", lambda *a, **k: ("", denied))
    with pytest.raises(SystemExit) as e:
        for _ in range(uw.DISK_FULL_GIVE_UP + 1):
            batch = uw.take_batch(1, size=1)
            w._process(batch)
    assert e.value.code == uw.EXIT_DISK_FULL
    assert status_of(db, "1")[0] == "pending" and status_of(db, "2")[0] == "pending"


def test_file_that_cannot_be_written_fails_alone(db, quiet_worker, dirs, monkeypatch):
    """Длинное имя или FAT32 — вина файла, не диска: он сразу в ошибках, остальные качаются."""
    w = quiet_worker
    w.session = _DirectSession({"1": b"a" * 10, "2": b"b" * 20})
    real = uw.download_file

    def fake(s, url, dst, *a, **k):
        if dst.endswith("a.jpg"):
            return "", uw.FetchFailure("local_file", "ошибка записи файла: [Errno 36] File name too long")
        return real(s, url, dst, *a, **k)

    monkeypatch.setattr(uw, "download_file", fake)
    w._process(uw.take_batch(1, size=10))
    assert status_of(db, "1") == ("failed", uw.MAX_ATTEMPTS)
    assert status_of(db, "2") == ("downloaded", 0)
    assert w._disk_full_streak == 0


def test_too_big_for_fat32_is_not_downloaded(db, quiet_worker, dirs, monkeypatch):
    con = sqlite3.connect(db)
    con.execute("UPDATE unlim_files SET size=? WHERE file_id='1'", (5 * 1024 ** 3,))
    con.commit()
    con.close()
    monkeypatch.setattr(uw, "too_big_for_folder", lambda folder, size: "FAT32" if size > 4 * 1024 ** 3 else None)
    w = quiet_worker
    w.session = _DirectSession({"2": b"b" * 20})
    while batch := uw.take_batch(1, size=10):  # тяжёлый файл берётся отдельной пачкой
        w._process(batch)
    assert status_of(db, "1")[0] == "failed" and status_of(db, "2")[0] == "downloaded"
    assert w.session.asked == ["2"]  # за ссылкой на большой файл не ходили


def test_files_deleted_from_disk_do_not_look_like_an_outage(db, quiet_worker, dirs):
    """404 на ссылку — файла больше нет. Пять таких подряд — не «Яндекс отказывает»: попытки
    сгорают, и через MAX_ATTEMPTS файлы в ошибках, а не держат очередь часами."""
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO unlim_files (file_id, name, size) VALUES (?, ?, 5)",
                    [(str(i), f"gone{i}.jpg") for i in range(3, 10)])
    con.commit()
    con.close()
    w = quiet_worker
    w.session = _DirectSession({}, broken={str(i): 404 for i in range(1, 10)})
    for _ in range(9 * uw.MAX_ATTEMPTS):
        batch = uw.take_batch(1, size=1)
        if not batch:
            break
        w._process(batch)
    assert {status_of(db, str(i)) for i in range(1, 10)} == {("failed", uw.MAX_ATTEMPTS)}
    assert w._fail_streak == 0


def test_same_name_and_size_but_other_photo_is_downloaded(db, quiet_worker, dirs):
    """«a.jpg» того же размера в папке — другое фото (MD5 другой): качаем рядом, а не считаем скачанным."""
    import hashlib

    dirs.mkdir()
    (dirs / "a.jpg").write_bytes(b"x" * 10)
    con = sqlite3.connect(db)
    con.execute("UPDATE unlim_files SET md5=? WHERE file_id='1'", (hashlib.md5(b"a" * 10).hexdigest(),))
    con.commit()
    con.close()
    w = quiet_worker
    w.session = _DirectSession({"1": b"a" * 10, "2": b"b" * 20})
    w._process(uw.take_batch(1, size=10))
    assert status_of(db, "1")[0] == "downloaded"
    assert (dirs / "a.jpg").read_bytes() == b"x" * 10 and (dirs / "a_1.jpg").read_bytes() == b"a" * 10


def test_dead_cookies_after_a_failure_stop_the_worker(db, quiet_worker, dirs, monkeypatch):
    """После сбоя sk обновляется; вход не прошёл — это протухшие куки (код 2), а не отказ Яндекса."""
    (item,) = uw.take_batch(1, size=1)

    def expired(s):
        raise uw.AuthExpiredError("redirect to passport")

    monkeypatch.setattr(uw, "get_sk_uid", expired)
    with pytest.raises(SystemExit) as e:
        quiet_worker._on_prepare_failed([item], uw.PrepareError("server_50x", "HTTP 503", 503))
    assert e.value.code == uw.EXIT_AUTH
    assert status_of(db, item["file_id"]) == ("pending", 0)


@pytest.mark.parametrize("header,want", [("30", 30.0), ("86400", float(uw.RATE_LIMIT_MAX_PAUSE_SEC)),
                                         ("inf", None), ("nan", None), ("-5", None), ("soon", None)])
def test_retry_after_is_capped(header, want):
    """Пауза общая для всех воркеров и запусков: «Retry-After: 86400» не должен вставать на сутки."""
    resp = type("R", (), {"headers": {"Retry-After": header}})()
    assert uw._retry_after_sec(resp) == want


def test_broken_shared_cooldown_does_not_stall_forever(db):
    uw.ensure_attempts_column()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS unlim_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO unlim_meta VALUES ('cooldown_until', 'inf')")
    con.commit()
    con.close()
    assert uw.shared_cooldown_remaining() == 0.0
