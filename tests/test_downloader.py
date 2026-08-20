"""Тесты политики повторов: что ретраим, что нет и сколько ждём."""

import yadisk

from downloader import _is_retryable, _wait_policy, _download_one, DownloadJob


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


def test_wait_policy_sleeps_a_minute_on_429():
    """Раньше эта ветка была мёртвым кодом и не срабатывала никогда."""
    wait = _wait_policy(_RetryState(_err(yadisk.exceptions.TooManyRequestsError)))
    assert wait == 60.0


def test_wait_policy_is_exponential_otherwise():
    w1 = _wait_policy(_RetryState(ConnectionError("x"), attempt=1))
    w3 = _wait_policy(_RetryState(ConnectionError("x"), attempt=3))
    assert w1 < w3 <= 60


# ── resume-логика ───────────────────────────────────────────────────────────


class FakeClient:
    def __init__(self, payload=b"data", fail_with=None):
        self.payload = payload
        self.fail_with = fail_with
        self.calls = []

    def download(self, remote, local):
        self.calls.append(remote)
        if self.fail_with:
            raise self.fail_with
        with open(local, "wb") as f:
            f.write(self.payload)


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
