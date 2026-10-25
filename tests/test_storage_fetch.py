"""storage_fetch: скачивание одного файла и реестр молчащих серверов хранения."""

import errno
import hashlib

import pytest
import requests

import storage_fetch as sf


class _Stream:
    def __init__(self, data=b"", headers=None, status=200):
        self._data = data
        self.headers = headers or {}
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size):
        if self._data:
            yield self._data


class _Session:
    """downloader (host 'dl') → 302 → сервер хранения; storage: host → ответ или исключение."""

    def __init__(self, storage):
        self.storage = storage
        self.hits = []

    def get(self, url, stream=False, timeout=None, allow_redirects=True):
        host = url.split("/")[2]
        self.hits.append(host)
        if host == "dl":
            return _Stream(status=302, headers={"Location": "https://" + url.split("/", 3)[3]})
        answer = self.storage[host]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _fetch(tmp_path, storage, **kw):
    s = _Session(storage)
    tmp = tmp_path / "f.part"
    written, host, failure = sf.fetch_to_file(s, "https://dl/s1klg.storage.yandex.net/x", str(tmp), **kw)
    return s, tmp, written, host, failure


def test_downloads_through_redirect_and_reports_host(tmp_path):
    data = b"photo-bytes"
    _, tmp, written, host, failure = _fetch(
        tmp_path, {"s1klg.storage.yandex.net": _Stream(data)},
        expected_size=len(data), expected_md5=hashlib.md5(data).hexdigest())
    assert failure is None
    assert written == len(data) and host == "s1klg.storage.yandex.net"
    assert tmp.read_bytes() == data


def test_empty_file_is_fine_when_disk_says_it_is_empty(tmp_path):
    _, tmp, written, _, failure = _fetch(tmp_path, {"s1klg.storage.yandex.net": _Stream(b"")}, expected_size=0)
    assert failure is None and written == 0


def test_empty_answer_for_unknown_size_is_a_bad_file(tmp_path):
    *_, failure = _fetch(tmp_path, {"s1klg.storage.yandex.net": _Stream(b"")})
    assert failure.kind == "bad_file"


def test_real_html_files_pass_when_pages_are_allowed(tmp_path):
    page = _Stream(b"<html>my notes</html>", headers={"Content-Type": "text/html"})
    *_, failure = _fetch(tmp_path, {"s1klg.storage.yandex.net": page}, reject_pages=False)
    assert failure is None
    page = _Stream(b"<html>captcha</html>", headers={"Content-Type": "text/html"})
    *_, failure = _fetch(tmp_path, {"s1klg.storage.yandex.net": page})
    assert failure.kind == "refused"


def test_silent_storage_and_known_dead_storage(tmp_path):
    silent = {"s1klg.storage.yandex.net": requests.exceptions.ConnectTimeout("timed out")}
    *_, host, failure = _fetch(tmp_path, silent)
    assert failure.kind == "storage_down" and failure.is_storage and host == "s1klg.storage.yandex.net"

    s, _, _, _, failure = _fetch(tmp_path, silent, host_is_dead=lambda h: True)
    assert failure.kind == "storage_skip" and failure.is_storage
    assert s.hits == ["dl"]  # к молчащему серверу даже не ходили


def test_progress_callback_gets_written_and_total(tmp_path):
    calls = []
    _fetch(tmp_path, {"s1klg.storage.yandex.net": _Stream(b"abc")}, expected_size=3,
           on_progress=lambda w, t: calls.append((w, t)))
    assert calls == [(3, 3)]


@pytest.mark.parametrize("status,kind", [(503, "server_50x"), (429, "rate_limit"), (410, "refused")])
def test_http_errors_are_typed(tmp_path, status, kind):
    *_, failure = _fetch(tmp_path, {"s1klg.storage.yandex.net": _Stream(status=status)})
    assert failure.kind == kind


def test_dead_hosts_expire(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(sf.time, "monotonic", lambda: now[0])
    dead = sf.DeadHosts(ttl=60)
    dead.mark("s1klg.storage.yandex.net")
    assert dead.is_dead("s1klg.storage.yandex.net") and dead.count() == 1
    assert not dead.is_dead("s2klg.storage.yandex.net")
    now[0] += 61
    assert not dead.is_dead("s1klg.storage.yandex.net") and dead.count() == 0
    dead.mark("s3klg.storage.yandex.net")
    dead.clear()
    assert dead.count() == 0


class _BrokenFile:
    """Файл, запись в который падает с заданным OSError."""

    def __init__(self, err):
        self.err = err

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def write(self, data):
        raise self.err


@pytest.mark.parametrize("err,kind", [
    # Номера errno разные в Linux, macOS и Windows (ENAMETOOLONG — 36, 63 и 38): только константы
    (OSError(errno.EFBIG, "File too large"), "local_file"),     # FAT32 и файл больше 4 ГБ (Linux, macOS)
    (OSError(errno.ENAMETOOLONG, "File name too long"), "local_file"),
    (OSError(errno.ENOSPC, "No space left on device"), "local_disk"),
    (OSError(errno.EIO, "Input/output error"), "local_disk"),  # флешка отвалилась
    (OSError("без errno"), "local_disk"),
])
def test_write_errors_are_local_never_yandex(tmp_path, monkeypatch, err, kind):
    """Ошибка записи — не «обрыв связи»: иначе файл качался бы снова и ждал Яндекса вечно."""
    import builtins

    real_open = builtins.open
    monkeypatch.setattr(builtins, "open",
                        lambda path, mode="r", *a, **k: _BrokenFile(err) if "w" in mode else real_open(path, mode, *a, **k))
    *_, failure = _fetch(tmp_path, {"s1klg.storage.yandex.net": _Stream(b"data")}, expected_size=4)
    assert failure.kind == kind


@pytest.mark.parametrize("windows,kind", [(True, "local_disk"), (False, "local_file")])
def test_einval_while_writing_is_the_disk_on_windows(tmp_path, monkeypatch, windows, kind):
    """write() на отвалившийся NAS или выдернутую флешку Windows отвечает EINVAL без кода Win32:
    это диск, а не файл. На Linux EINVAL при записи — имя, которое не принимает FAT или SMB."""
    import builtins

    import utils

    monkeypatch.setattr(utils, "IS_WINDOWS", windows)
    err = OSError(errno.EINVAL, "Invalid argument")
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open",
                        lambda path, mode="r", *a, **k: _BrokenFile(err) if "w" in mode else real_open(path, mode, *a, **k))
    *_, failure = _fetch(tmp_path, {"s1klg.storage.yandex.net": _Stream(b"data")}, expected_size=4)
    assert failure.kind == kind


def test_progress_errors_never_fail_the_file(tmp_path):
    """print в закрытый вывод (Web UI закрыли) бросает OSError, на Windows — с EINVAL: файл от этого
    не портится, и считать это ошибкой записи файла нельзя."""
    def progress(written, total):
        raise OSError(errno.EINVAL, "Invalid argument")

    _, tmp, written, _, failure = _fetch(tmp_path, {"s1klg.storage.yandex.net": _Stream(b"data")},
                                         expected_size=4, on_progress=progress)
    assert failure is None and written == 4 and tmp.read_bytes() == b"data"


def test_network_errors_inside_the_body_stay_network(tmp_path):
    class Chunked(_Stream):
        def iter_content(self, chunk_size):
            yield b"da"
            raise requests.exceptions.ChunkedEncodingError("Connection broken")

    *_, failure = _fetch(tmp_path, {"s1klg.storage.yandex.net": Chunked(b"data")}, expected_size=4)
    assert failure.kind == "connection"


def test_signed_links_never_reach_the_error_text(tmp_path):
    """Подписанная ссылка на файл — сама по себе доступ к нему: в журнал и отчёт она не попадает."""
    link = "https://s1klg.storage.yandex.net/rdisk/abc123?uid=42&rtoken=SECRET&s=sig"
    err = requests.exceptions.HTTPError(f"403 Client Error: Forbidden for url: {link}",
                                        response=_Stream(status=403))
    cut = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='s1klg.storage.yandex.net', port=443): Max retries exceeded "
        "with url: /rdisk/abc123?uid=42&rtoken=SECRET (Caused by ProtocolError())")

    class Raises(_Stream):
        def __init__(self, exc):
            super().__init__(b"x")
            self.exc = exc

        def raise_for_status(self):
            raise self.exc

        def iter_content(self, chunk_size):
            raise self.exc

    for exc in (err, cut):
        *_, failure = _fetch(tmp_path, {"s1klg.storage.yandex.net": Raises(exc)}, expected_size=1)
        text = str(failure)
        assert "SECRET" not in text and "uid=42" not in text and "abc123" not in text
        assert "s1klg.storage.yandex.net" in text  # сервер для диагностики остаётся


def test_headers_go_only_to_the_first_request(tmp_path):
    seen = {}

    class Session(_Session):
        def get(self, url, stream=False, timeout=None, allow_redirects=True, headers=None):
            seen[url.split("/")[2]] = headers
            return super().get(url, stream, timeout, allow_redirects)

    s = Session({"s1klg.storage.yandex.net": _Stream(b"data")})
    *_, failure = sf.fetch_to_file(s, "https://dl/s1klg.storage.yandex.net/x", str(tmp_path / "f"),
                                   expected_size=4, headers={"Authorization": "OAuth t"})
    assert failure is None
    assert seen == {"dl": {"Authorization": "OAuth t"}, "s1klg.storage.yandex.net": None}
