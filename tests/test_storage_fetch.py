"""storage_fetch: скачивание одного файла и реестр молчащих серверов хранения."""

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
