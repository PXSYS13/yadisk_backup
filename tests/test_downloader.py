"""Тесты политики повторов: что ретраим, что нет и сколько ждём."""

import pytest
import yadisk

from downloader import (
    DownloadJob,
    _download_one,
    _is_retryable,
    _is_skipped,
    _wait_policy,
)


def test_is_skipped_trash_prefix():
    assert _is_skipped("disk:/Корзина") is True
    assert _is_skipped("disk:/Корзина/sub/file.txt") is True
    assert _is_skipped("/Корзина/file.txt") is True
    assert _is_skipped("disk:/Trash/sub/file.txt") is True
    # "Корзина2" не должна пропускаться!
    assert _is_skipped("disk:/Корзина2") is False
    assert _is_skipped("disk:/Корзина2/file.txt") is False
    assert _is_skipped("disk:/Trash2/file.txt") is False


class _Outcome:
    def __init__(self, exc):
        self._exc = exc

    def exception(self):
        return self._exc


class _RetryState:
    """Минимальный дублёр tenacity.RetryCallState."""

    def __init__(self, exc, attempt=1):
        self.outcome = _Outcome(exc)
        self.attempt_number = attempt
        self.idle_for = 0
        self.seconds_since_start = 0


def _err(cls):
    """Создаёт исключение yadisk без реального HTTP-ответа."""
    return cls("boom")


def test_unauthorized_is_not_retried():
    """Битый токен не станет валидным — не тратим 5 попыток на каждый файл."""
    assert _is_retryable(_err(yadisk.exceptions.UnauthorizedError)) is False


def test_forbidden_is_not_retried():
    assert _is_retryable(_err(yadisk.exceptions.ForbiddenError)) is False


def test_path_not_found_is_not_retried():
    assert _is_retryable(_err(yadisk.exceptions.PathNotFoundError)) is False


def test_rate_limit_is_retried():
    assert _is_retryable(_err(yadisk.exceptions.TooManyRequestsError)) is True


def test_network_errors_are_retried():
    assert _is_retryable(ConnectionError("reset")) is True
    assert _is_retryable(TimeoutError("slow")) is True
    assert _is_retryable(OSError("io")) is True


def test_unknown_exception_is_not_retried():
    assert _is_retryable(ValueError("nonsense")) is False


def test_local_disk_errors_are_not_retried():
    """Полный диск не рассосётся от 7 повторов с паузами — это десятки часов на большой очереди."""
    import errno

    assert _is_retryable(OSError(errno.ENOSPC, "No space left on device")) is False
    assert _is_retryable(PermissionError(errno.EACCES, "denied")) is False
    assert _is_retryable(OSError("generic io without errno")) is True


def test_account_info_maps_api_fields():
    from datetime import datetime

    from downloader import account_info, describe_account

    class User:
        login = "ivan"
        display_name = "Иван"
        uid = 42
        country = "ru"
        reg_time = datetime(2015, 3, 4)

    class Info:
        used_space = 50 * 1024 ** 3
        total_space = 100 * 1024 ** 3
        trash_size = 1024 ** 3
        photounlim_size = 230 * 1024 ** 3
        is_paid = True
        unlimited_autoupload_enabled = True
        max_file_size = 10 * 1024 ** 3
        free_photounlim_end_date = None
        user = User()

    class Client:
        def get_disk_info(self):
            return Info()

    acc = account_info(Client())
    assert acc["login"] == "ivan" and acc["display_name"] == "Иван" and acc["uid"] == "42"
    assert acc["free"] == 50 * 1024 ** 3
    assert acc["photo_unlim"] == 230 * 1024 ** 3
    assert acc["reg_time"].startswith("2015-03-04")
    line = describe_account(acc)
    assert "ivan (Иван)" in line and "платный" in line and "230.00 GB" in line


def test_api_error_details_from_yadisk_exception():
    from downloader import api_error_details

    class Resp:
        status = 503

    e = yadisk.exceptions.UnavailableError("DiskUnavailable", "boom", Resp())
    assert api_error_details(e) == " [error_type=DiskUnavailable, http=503]"
    assert api_error_details(ValueError("x")) == ""


def test_makedirs_failure_becomes_failed_result_not_exception(tmp_path, monkeypatch):
    """Раньше исключение из makedirs в потоке роняло весь download_all."""
    import errno

    import downloader

    def boom(_path):
        raise FileExistsError(errno.EEXIST, "file exists where a folder is expected")

    monkeypatch.setattr(downloader, "_ensure_local_dirs", boom)
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/x.jpg", size=1, md5=None),
        FakeClient(),
        str(tmp_path),
    )
    assert res.status == "failed"
    assert (res.error or "").startswith("local_disk |")


def test_wait_policy_sleeps_a_minute_on_429():
    """Раньше эта ветка была мёртвым кодом и не срабатывала никогда."""
    wait = _wait_policy(_RetryState(_err(yadisk.exceptions.TooManyRequestsError)))
    assert wait == 60.0


def test_wait_policy_is_exponential_otherwise():
    w1 = _wait_policy(_RetryState(ConnectionError("x"), attempt=1))
    w3 = _wait_policy(_RetryState(ConnectionError("x"), attempt=3))
    assert w1 < w3 <= 60


# ── resume-логика ───────────────────────────────────────────────────────────


# Ссылка → содержимое файла: FakeClient выдаёт ссылки, _FakeHttp по ним отдаёт байты
_LINKS: dict = {}


class FakeClient:
    def __init__(self, payload=b"data", fail_with=None):
        self.payload = payload
        self.fail_with = fail_with
        self.calls = []

    def get_download_link(self, remote):
        self.calls.append(remote)
        if self.fail_with:
            raise self.fail_with
        link = f"https://downloader.test/{len(_LINKS)}"
        _LINKS[link] = self.payload
        return link


class _Stream:
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        if self._data:
            yield self._data


class _FakeHttp:
    def get(self, url, stream=False, timeout=None, allow_redirects=True):
        return _Stream(_LINKS[url])


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    import downloader
    monkeypatch.setattr(downloader, "_thread_http", lambda: _FakeHttp())
    downloader.DEAD_HOSTS.clear()


def test_existing_file_with_same_size_is_skipped(tmp_path):
    target = tmp_path / "disk" / "photo.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"1234")

    cli = FakeClient()
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/photo.jpg", size=4, md5=None),
        cli, str(tmp_path),
    )
    assert res.status == "skipped"
    assert cli.calls == []  # сеть не трогали


def test_partial_file_is_redownloaded(tmp_path):
    target = tmp_path / "disk" / "photo.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"12")  # обрыв на середине

    cli = FakeClient(payload=b"1234")
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/photo.jpg", size=4, md5=None),
        cli, str(tmp_path),
    )
    assert res.status == "downloaded"
    assert res.bytes_written == 4
    assert target.read_bytes() == b"1234"


def test_failure_is_reported_not_raised(tmp_path):
    cli = FakeClient(fail_with=yadisk.exceptions.PathNotFoundError("gone"))
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/gone.jpg", size=10, md5=None),
        cli, str(tmp_path),
    )
    assert res.status == "failed"
    assert "PathNotFound" in (res.error or "")


def test_close_all_clients_clears_registry():
    import downloader

    class DummyClient:
        closed = False

        def close(self):
            self.closed = True

    dummy = DummyClient()
    with downloader._all_clients_lock:
        downloader._all_clients.append(dummy)
    assert len(downloader._all_clients) > 0
    downloader._close_all_clients()
    assert len(downloader._all_clients) == 0
    assert dummy.closed is True


def test_existing_file_same_size_md5_match_is_skipped(tmp_path):
    import hashlib

    data = b"hello_yadisk"
    target = tmp_path / "disk" / "doc.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(data)

    digest = hashlib.md5(data).hexdigest()
    cli = FakeClient()
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/doc.txt", size=len(data), md5=digest),
        cli,
        str(tmp_path),
    )
    assert res.status == "skipped"
    assert cli.calls == []


def test_existing_file_same_size_md5_mismatch_is_redownloaded(tmp_path):
    import hashlib

    target = tmp_path / "disk" / "doc.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupted123")

    real_data = b"original1234"
    real_md5 = hashlib.md5(real_data).hexdigest()

    cli = FakeClient(payload=real_data)
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/doc.txt", size=len(real_data), md5=real_md5),
        cli,
        str(tmp_path),
    )
    assert res.status == "downloaded"
    assert len(cli.calls) == 1
    assert target.read_bytes() == real_data


def test_download_one_cleans_up_part_file_on_failure(tmp_path, monkeypatch):
    import downloader

    def fail_download(cli, remote, local, job=None):
        with open(local, "wb") as f:
            f.write(b"part_data")
        raise ConnectionError("dropped")

    monkeypatch.setattr(downloader, "_download_with_retry", fail_download)
    cli = FakeClient()
    res = _download_one(
        DownloadJob(remote_path="disk:/disk/fail.txt", size=10, md5=None),
        cli,
        str(tmp_path),
    )
    assert res.status == "failed"
    target = tmp_path / "disk" / "fail.txt"
    part_file = tmp_path / "disk" / "fail.txt.part"
    assert not target.exists()
    assert not part_file.exists()


def test_download_one_diagnostics_classification(tmp_path, monkeypatch):
    import downloader

    def make_failer(exc):
        def _fail(cli, remote, local, job=None):
            raise exc

        return _fail

    # Server 50x
    monkeypatch.setattr(
        downloader,
        "_download_with_retry",
        make_failer(yadisk.exceptions.BadGatewayError("bad gw")),
    )
    r500 = _download_one(
        DownloadJob(remote_path="disk:/disk/f1.txt", size=10, md5=None),
        FakeClient(),
        str(tmp_path),
    )
    assert "server_50x" in (r500.error or "")

    # Timeout
    monkeypatch.setattr(
        downloader, "_download_with_retry", make_failer(TimeoutError("timed out"))
    )
    r_to = _download_one(
        DownloadJob(remote_path="disk:/disk/f2.txt", size=10, md5=None),
        FakeClient(),
        str(tmp_path),
    )
    assert "download_timeout" in (r_to.error or "")

    # Auth expired
    monkeypatch.setattr(
        downloader,
        "_download_with_retry",
        make_failer(yadisk.exceptions.UnauthorizedError("bad token")),
    )
    r_auth = _download_one(
        DownloadJob(remote_path="disk:/disk/f3.txt", size=10, md5=None),
        FakeClient(),
        str(tmp_path),
    )
    assert "auth_expired" in (r_auth.error or "")



def test_download_all_lazy_bounded_queue(tmp_path, monkeypatch):
    import threading
    import downloader

    yielded_count = 0
    in_flight_at_start = []
    first_worker_started = threading.Event()
    release_workers = threading.Event()

    class FakeState:
        def assign_local_paths(self, download_dir):
            pass

        def stale_previews(self):
            return []

        def get_pending_stats(self):
            return {"count": 20, "bytes": 200}

        def get_pending(self):
            nonlocal yielded_count
            for i in range(20):
                yielded_count += 1
                yield {
                    "remote_path": f"disk:/f{i}.txt",
                    "size": 10,
                    "md5": None,
                    "local_path": None,
                }

        def mark_done(self, **kwargs):
            pass

        def mark_downloaded(self, remote_path, local_path):
            pass

        def mark_failed(self, **kwargs):
            pass

        def mark_skipped(self, **kwargs):
            pass

    def mock_download_one(job, client, download_dir):
        if not first_worker_started.is_set():
            in_flight_at_start.append(yielded_count)
            first_worker_started.set()
        release_workers.wait(timeout=2.0)
        return downloader.DownloadResult(
            job=job,
            status="downloaded",
            local_path=str(tmp_path / "dummy"),
            bytes_written=10,
            error=None,
            duration_ms=5,
        )

    monkeypatch.setattr(downloader, "_download_one", mock_download_one)
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: None)

    # Запускаем в отдельном потоке, передавая (token, state, download_dir)
    t = threading.Thread(
        target=downloader.download_all,
        args=("fake_token", FakeState(), str(tmp_path)),
        kwargs={"workers": 2},
    )
    t.start()

    assert first_worker_started.wait(timeout=2.0)
    # При старте первых воркеров генератор не должен быть выкачан целиком (20 задач)
    # max_in_flight для workers=2 равен max(2*2, 4) = 4
    assert in_flight_at_start[0] <= 5
    release_workers.set()
    t.join(timeout=5.0)
    assert not t.is_alive()


