"""Обычный Диск: файлы на молчащих серверах хранения откладываются, а не висят по 35 минут."""

import re

import pytest
import requests
import yadisk

import downloader
import main
from state import DEFERRED_MARKER, State
from utils import EXIT_AUTH, EXIT_DEFERRED, EXIT_DISK_FULL, EXIT_OK, EXIT_PARTIAL

# Формат строки прогресса в консоли и в журнале задачи (как в test_public_download)
PROGRESS_RE = re.compile(r"Прогресс:\s*(\d+)\/(\d+)\s*файлов.*?ошибок\s*(\d+)")


class _Stream:
    def __init__(self, data=b"", status=200, headers=None):
        self._data = data
        self.status_code = status
        self.headers = headers if headers is not None else {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        if self._data:
            yield self._data


class _Http:
    """downloader → 302 → сервер хранения. storage: host → байты или исключение."""

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
        return _Stream(answer)


class _Client:
    """get_download_link: remote → https://dl/<host>/<remote>; hosts — remote → сервер."""

    def __init__(self, hosts, fail_with=None):
        self.hosts = hosts
        self.fail_with = fail_with
        self.calls = []

    def get_download_link(self, remote):
        self.calls.append(remote)
        if self.fail_with:
            raise self.fail_with
        return f"https://dl/{self.hosts[remote]}/{remote.split('/')[-1]}"

    def close(self):
        pass


DEAD = "s1klg.storage.yandex.net"
ALIVE = "s2nrg.storage.yandex.net"


@pytest.fixture()
def net(monkeypatch):
    """Подменяет сеть: настраивается через net.storage / net.hosts."""
    downloader.DEAD_HOSTS.clear()
    http = _Http({DEAD: requests.exceptions.ConnectTimeout("timed out"), ALIVE: b"photo"})
    monkeypatch.setattr(downloader, "_thread_http", lambda: http)
    monkeypatch.setattr(downloader.time, "sleep", lambda s: None)
    yield http
    downloader.DEAD_HOSTS.clear()


def test_silent_storage_defers_file_and_marks_host(tmp_path, net):
    cli = _Client({"disk:/a.jpg": DEAD})
    res = downloader._download_one(downloader.DownloadJob("disk:/a.jpg", 5, None), cli, str(tmp_path))
    assert res.status == "deferred"
    assert res.storage_host == DEAD
    assert res.error.startswith("storage_down | ")
    assert downloader.DEAD_HOSTS.is_dead(DEAD)
    assert not list(tmp_path.rglob("*.part"))


class _Http500(_Http):
    def get(self, url, stream=False, timeout=None, allow_redirects=True):
        if url.split("/")[2] == ALIVE:
            self.hits.append(ALIVE)
            raise requests.exceptions.HTTPError("500", response=type("R", (), {"status_code": 500})())
        return super().get(url, stream, timeout, allow_redirects)


def test_persistent_500_defers_and_deferred_file_gets_one_try(tmp_path, monkeypatch):
    downloader.DEAD_HOSTS.clear()
    http = _Http500({})
    monkeypatch.setattr(downloader, "_thread_http", lambda: http)
    monkeypatch.setattr(downloader.time, "sleep", lambda s: None)
    cli = _Client({"disk:/a.jpg": ALIVE})

    res = downloader._download_one(downloader.DownloadJob("disk:/a.jpg", 5, None), cli, str(tmp_path))
    assert res.status == "deferred"
    assert http.hits.count(ALIVE) == downloader.FRESH_LINK_RETRIES + 1
    assert not downloader.DEAD_HOSTS.is_dead(ALIVE)  # 500 на один файл — сервер не «мёртвый»

    http.hits.clear()
    job = downloader.DownloadJob("disk:/a.jpg", 5, None, error=res.error, storage_host=res.storage_host)
    res = downloader._download_one(job, cli, str(tmp_path))
    assert res.status == "deferred" and http.hits.count(ALIVE) == 1


def test_regular_disk_keeps_html_and_empty_files(tmp_path, net):
    net.storage[ALIVE] = b""
    cli = _Client({"disk:/empty.txt": ALIVE})
    res = downloader._download_one(downloader.DownloadJob("disk:/empty.txt", 0, None), cli, str(tmp_path))
    assert res.status == "downloaded"

    net.storage[ALIVE] = b"<html>notes</html>"
    cli = _Client({"disk:/notes.html": ALIVE})
    res = downloader._download_one(downloader.DownloadJob("disk:/notes.html", 18, None), cli, str(tmp_path))
    assert res.status == "downloaded"


def _state_with(tmp_path, rows):
    st = State(str(tmp_path / "state.db"))
    st.mark_pending_batch([(r, size, None) for r, size in rows])
    return st


def _status(st, remote):
    row = st._conn().execute(
        "SELECT status, error, storage_host FROM files WHERE remote_path=?", (remote,)).fetchone()
    return tuple(row)


def test_download_all_defers_dead_and_downloads_alive(tmp_path, net, monkeypatch, capsys):
    st = _state_with(tmp_path, [("disk:/a.jpg", 5), ("disk:/b.jpg", 5), ("disk:/c.jpg", 5)])
    hosts = {"disk:/a.jpg": DEAD, "disk:/b.jpg": ALIVE, "disk:/c.jpg": DEAD}
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: _Client(hosts))
    monkeypatch.setattr(downloader, "PLAIN_PROGRESS_EVERY_SEC", 0)

    counters = downloader.download_all("t", st, str(tmp_path / "dl"), workers=1)

    assert counters["downloaded"] == 1 and counters["deferred"] == 2 and counters["failed"] == 0
    assert _status(st, "disk:/b.jpg")[0] == "downloaded"
    status, error, host = _status(st, "disk:/a.jpg")
    assert status == "pending" and error.startswith(DEFERRED_MARKER) and host == DEAD
    assert net.hits.count(DEAD) == 1  # второй файл того же сервера — без ожидания
    assert main._download_exit_code(counters) == EXIT_DEFERRED

    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("Прогресс:")]
    m = PROGRESS_RE.search(lines[-1])
    assert m and m.group(1) == "3" and m.group(2) == "3" and m.group(3) == "0"
    assert "отложено 2" in lines[-1]
    st.close()


def test_known_dead_host_is_skipped_without_asking_yandex(tmp_path, net, monkeypatch):
    st = _state_with(tmp_path, [("disk:/a.jpg", 5)])
    st.mark_deferred("disk:/a.jpg", DEAD, "сервер хранения не отвечает")
    downloader.DEAD_HOSTS.mark(DEAD)
    cli = _Client({"disk:/a.jpg": DEAD})
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: cli)

    counters = downloader.download_all("t", st, str(tmp_path / "dl"), workers=1)

    assert counters["deferred"] == 1
    assert cli.calls == []  # ни ссылки, ни запроса к серверу хранения
    assert net.hits == []
    st.close()


def test_full_disk_stops_the_queue(tmp_path, net, monkeypatch):
    rows = [(f"disk:/f{i}.jpg", 5) for i in range(15)]
    st = _state_with(tmp_path, rows)
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: _Client({}))

    def full(job, client, download_dir):
        return downloader.DownloadResult(job, "failed", None, 0, "local_disk | OSError: [Errno 28] No space", 0)

    monkeypatch.setattr(downloader, "_download_one", full)
    counters = downloader.download_all("t", st, str(tmp_path / "dl"), workers=1)
    assert counters["aborted"] == "local_disk"
    # Начатые до остановки файлы записаны, остальные не тронуты
    assert downloader.LOCAL_DISK_GIVE_UP <= counters["failed"] < len(rows)
    assert st.get_stats()["failed"] == counters["failed"]
    assert main._download_exit_code(counters) == EXIT_DISK_FULL
    st.close()


@pytest.mark.parametrize("token_state,aborted", [("ok", None), ("invalid", "auth")])
def test_403_streak_rechecks_the_token(tmp_path, net, monkeypatch, token_state, aborted):
    rows = [(f"disk:/f{i}.jpg", 5) for i in range(8)]
    st = _state_with(tmp_path, rows)
    monkeypatch.setattr(downloader, "_get_thread_client",
                        lambda token: _Client({}, fail_with=yadisk.exceptions.ForbiddenError("403")))
    monkeypatch.setattr(downloader, "token_state", lambda token: (token_state, "msg"))
    counters = downloader.download_all("t", st, str(tmp_path / "dl"), workers=1)
    assert counters.get("aborted") == aborted
    if aborted is None:
        assert counters["failed"] == 8  # живой токен — 403 на файлы не повод останавливаться
    else:
        assert main._download_exit_code(counters) == EXIT_AUTH
    st.close()


def test_exit_code_priorities():
    assert main._download_exit_code({"failed": 0, "deferred": 0}) == EXIT_OK
    assert main._download_exit_code({"failed": 3, "deferred": 0}) == EXIT_PARTIAL
    assert main._download_exit_code({"failed": 3, "deferred": 1}) == EXIT_DEFERRED
    assert main._download_exit_code({"failed": 0, "deferred": 1, "aborted": "local_disk"}) == EXIT_DISK_FULL


def test_api_outage_defers_the_file_instead_of_failing_it(tmp_path, net):
    """Яндекс так и не выдал ссылку (503 после всех повторов): файл не виноват — он ждёт раунда,
    а не пишется ошибкой, которую повторит только человек."""
    cli = _Client({}, fail_with=yadisk.exceptions.UnavailableError(None, "503"))  # паузы tenacity — через time.sleep
    res = downloader._download_one(downloader.DownloadJob("disk:/a.jpg", 5, None), cli, str(tmp_path))
    assert res.status == "deferred" and res.error.startswith(DEFERRED_MARKER)
    # Удалённый файл — по-прежнему ошибка: ждать нечего
    gone = _Client({}, fail_with=yadisk.exceptions.PathNotFoundError(None, "404"))
    res = downloader._download_one(downloader.DownloadJob("disk:/b.jpg", 5, None), gone, str(tmp_path))
    assert res.status == "failed" and len(gone.calls) == 1  # без повторов


def test_part_named_sibling_is_not_overwritten(tmp_path, net):
    """В папке Диска лежат «видео.mp4» и «видео.mp4.part» (недокачанная загрузка браузера):
    скачивание первого не удаляет и не затирает второй."""
    net.storage[ALIVE] = b"photo"
    cli = _Client({"disk:/v/видео.mp4.part": ALIVE, "disk:/v/видео.mp4": ALIVE})
    for remote in ("disk:/v/видео.mp4.part", "disk:/v/видео.mp4"):
        res = downloader._download_one(downloader.DownloadJob(remote, 5, None), cli, str(tmp_path))
        assert res.status == "downloaded"
    assert sorted(p.name for p in (tmp_path / "v").iterdir()) == ["видео.mp4", "видео.mp4.part"]


def test_file_too_big_for_the_folder_fails_alone_without_download(tmp_path, net, monkeypatch):
    import utils

    monkeypatch.setattr(utils, "filesystem_type", lambda path: "FAT32")
    cli = _Client({"disk:/big.mkv": ALIVE})
    res = downloader._download_one(downloader.DownloadJob("disk:/big.mkv", 5 * 1024 ** 3, None), cli, str(tmp_path))
    assert res.status == "failed" and res.error.startswith("local_file | ")
    assert cli.calls == [] and net.hits == []  # гигабайты не качались впустую


def test_files_that_cannot_be_written_do_not_stop_the_queue(tmp_path, net, monkeypatch):
    """Длинное имя или FAT32 мешают одному файлу, а не диску: остальные качаются дальше."""
    rows = [(f"disk:/f{i}.jpg", 5) for i in range(15)]
    st = _state_with(tmp_path, rows)
    monkeypatch.setattr(downloader, "_get_thread_client", lambda token: _Client({}))

    def name_too_long(job, client, download_dir):
        return downloader.DownloadResult(job, "failed", None, 0, "local_file | OSError: [Errno 36] File name too long", 0)

    monkeypatch.setattr(downloader, "_download_one", name_too_long)
    counters = downloader.download_all("t", st, str(tmp_path / "dl"), workers=1)
    assert "aborted" not in counters and counters["failed"] == len(rows)
    assert main._download_exit_code(counters) == EXIT_PARTIAL
    st.close()
