"""«Обновить программу»: скачивание с проверками, проверка архива, что обновление не трогает,
откат при сбое, перезапуск и отказы, пока что-то качается/загружается/сортируется."""

import errno
import json
import os
import stat
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

import uploader.webapp.manager as up_mgr
import utils
import webapp.paths
import webapp.people
import webapp.rescue
import webapp.selfupdate as su
import webapp.tasks
import webui

ROOT = Path(__file__).resolve().parent.parent
OLD = "3.0.0"
NEW = "3.1.0"
BACKUP = f"{su.BACKUP_DIRNAME}/{OLD}_20261019-213005"

_BASE = {
    "webui.py": "from utils import APP_VERSION\n",
    "webapp/__init__.py": "",
    "webapp/rescue.py": "STATE = 'rescue {v}'\n",
    "pyproject.toml": '[project]\nname = "yadisk-backup"\nversion = "{v}"\ndependencies = ["requests>=2.31.0"]\n',
    "uv.lock": 'version = 1\n\n[[package]]\nname = "yadisk-backup"\nversion = "{v}"\nsource = {{ virtual = "." }}\n',
    ".python-version": "3.13\n",
    "README.md": "readme {v}\n",
    "webui.bat": "@echo off\r\nREM {v}\r\n",
}
# Данные человека в папке программы: обновление обязано оставить их байт в байт
USER_DATA = {
    ".env": b"YA_DISK_TOKEN='y0_secret'\nDOWNLOAD_DIR='D:\\\\backup'\n",
    "cookies.json": b"[]",
    "cookies.json.tmp": b"[]",
    "upload_unlim_config.json": b'{"source_dir": "D:/photos"}',
    "people_sorter_config.json": b'{"model_pack": "buffalo_l"}',
    "state.db": b"SQLite format 3\x00disk",
    "state.db-wal": b"wal",
    "unlim_state.db": b"SQLite format 3\x00unlim",
    "links_state.db-shm": b"shm",
    "logs/webui.log": b"log",
    ".uv/uv.exe": b"MZuv",
    ".models/buffalo_l/det.onnx": b"onnx",
    ".cache/crops/1.jpg": b"jpg",
    "downloads/\u0424\u043e\u0442\u043e/IMG_1.JPG": b"photo",
}


def program(version: str) -> dict:
    files = {name: text.format(v=version).encode("utf-8") for name, text in _BASE.items()}
    files["utils.py"] = f'"""Утилиты."""\n\nAPP_VERSION = "{version}"\n'.encode("utf-8")
    return files


def make_zip(path: Path, files: dict, top: str = su.ZIP_TOP, modes: dict | None = None) -> Path:
    """Архив как у GitHub: папка yadisk_backup-main/, записи папок, права в external_attr."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo(top + "/"), b"")
        for name, data in files.items():
            info = zipfile.ZipInfo(f"{top}/{name}")
            info.compress_type = zipfile.ZIP_DEFLATED
            if modes and name in modes:
                info.external_attr = modes[name] << 16
            zf.writestr(info, data)
    return path


def make_tree(root: Path, files: dict) -> Path:
    for name, data in files.items():
        p = root.joinpath(*name.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def prepared(tmp_path: Path, new_extra: dict | None = None, old_extra: dict | None = None):
    """Папка программы версии OLD с данными человека и распакованный архив версии NEW."""
    project = make_tree(tmp_path / "app", program(OLD) | USER_DATA | (old_extra or {}))
    archive = make_zip(tmp_path / "main.zip", program(NEW) | (new_extra or {}))
    release = su.check_archive(archive, OLD)
    src = tmp_path / "src"
    su.extract_archive(archive, release, src)
    changes, _skipped = su.plan_install(src, release, project, None)
    return project, release, changes


# ── Скачивание ──


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else [body[i:i + 65536] for i in range(0, len(body), 65536)]

    def iter_content(self, size):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        answer = self.routes[url]
        if isinstance(answer, Exception):
            raise answer
        assert kw["allow_redirects"] is False and kw["stream"] is True and kw["timeout"]
        return answer


CODELOAD = "https://codeload.github.com/PXSYS13/yadisk_backup/zip/refs/heads/main"


def test_download_follows_github_redirect(tmp_path):
    body = b"PK" + os.urandom(200_000)
    s = FakeSession({su.ZIP_URL: FakeResp(302, headers={"Location": CODELOAD}),
                     CODELOAD: FakeResp(200, body, {"Content-Length": str(len(body))})})
    seen = []
    size = su.download_zip(su.ZIP_URL, tmp_path / "a.zip", lambda got, total: seen.append((got, total)), session=s)
    assert size == len(body) and (tmp_path / "a.zip").read_bytes() == body
    assert s.calls == [su.ZIP_URL, CODELOAD] and seen[-1] == (len(body), len(body))


def test_download_never_goes_to_a_foreign_host(tmp_path):
    s = FakeSession({su.ZIP_URL: FakeResp(302, headers={"Location": "https://evil.example/main.zip"})})
    with pytest.raises(su.UpdateError, match="чужого адреса") as e:
        su.download_zip(su.ZIP_URL, tmp_path / "a.zip", session=s)
    assert s.calls == [su.ZIP_URL]  # к чужому адресу программа даже не обратилась
    assert "Программа не изменилась" in e.value.text
    # http — только с этого компьютера (YADISK_UPDATE_URL для проверок)
    for url in ("http://github.com/PXSYS13/yadisk_backup/archive/refs/heads/main.zip", "http://example.com/main.zip"):
        with pytest.raises(su.UpdateError, match="чужого адреса"):
            su.download_zip(url, tmp_path / "b.zip", session=FakeSession({}))
    local = "http://127.0.0.1:8032/main.zip"
    assert su.download_zip(local, tmp_path / "c.zip", session=FakeSession({local: FakeResp(200, b"PK\x03\x04")})) == 4


def test_download_has_a_size_cap_and_a_deadline(tmp_path):
    big = FakeResp(200, headers={"Content-Length": str(su.MAX_ZIP_BYTES + 1)}, chunks=[])
    with pytest.raises(su.UpdateError, match="больше 30 МБ"):
        su.download_zip(su.ZIP_URL, tmp_path / "a.zip", session=FakeSession({su.ZIP_URL: big}))
    # Без Content-Length — режем на лету
    endless = FakeResp(200, chunks=[b"x" * 1024] * 50)
    with pytest.raises(su.UpdateError, match="не похоже на программу"):
        su.download_zip(su.ZIP_URL, tmp_path / "b.zip", session=FakeSession({su.ZIP_URL: endless}),
                        max_bytes=10 * 1024)
    ticks = iter(range(0, 100_000, 100))
    slow = FakeResp(200, chunks=[b"x"] * 10)
    with pytest.raises(su.UpdateError, match="слишком медленно"):
        su.download_zip(su.ZIP_URL, tmp_path / "c.zip", session=FakeSession({su.ZIP_URL: slow}),
                        deadline_sec=250, clock=lambda: next(ticks))


def test_download_errors_are_plain_russian(tmp_path):
    with pytest.raises(su.UpdateError, match="ошибкой 404"):
        su.download_zip(su.ZIP_URL, tmp_path / "a.zip", session=FakeSession({su.ZIP_URL: FakeResp(404)}))
    offline = FakeSession({su.ZIP_URL: requests.ConnectionError("Max retries exceeded")})
    with pytest.raises(su.UpdateError, match="Проверьте интернет"):
        su.download_zip(su.ZIP_URL, tmp_path / "a.zip", session=offline)
    loop = FakeSession({su.ZIP_URL: FakeResp(302, headers={"Location": su.ZIP_URL})})
    with pytest.raises(su.UpdateError, match="перенаправлял"):
        su.download_zip(su.ZIP_URL, tmp_path / "a.zip", session=loop)
    with pytest.raises(su.UpdateError, match="пустой ответ"):
        su.download_zip(su.ZIP_URL, tmp_path / "a.zip", session=FakeSession({su.ZIP_URL: FakeResp(200, b"")}))


# ── Тот ли это архив ──


def test_archive_of_the_program_passes(tmp_path):
    archive = make_zip(tmp_path / "a.zip", program(NEW) | {"webui.sh": b"#!/bin/sh\n"}, modes={"webui.sh": 0o100755})
    release = su.check_archive(archive, OLD)
    assert release.version == NEW
    assert {"webui.py", "utils.py", "webapp/__init__.py", "webui.sh"} <= set(release.files)
    assert all(not name.endswith("/") for name in release.files)


@pytest.mark.parametrize("current,offered,ok", [
    ("2.7.3", "2.7.4", True), ("2.9.0", "2.10.0", True), ("2.7.3", "2.8", True),
    ("2.7.3", "2.7.3", False), ("2.8.0", "2.7.9", False), ("2.10.0", "2.9.9", False),
])
def test_archive_must_be_newer_by_numbers(tmp_path, current, offered, ok):
    archive = make_zip(tmp_path / "a.zip", program(offered))
    if ok:
        assert su.check_archive(archive, current).version == "%d.%d.%d" % utils_version(offered)
    else:
        with pytest.raises(su.UpdateError, match="обновлять нечего"):
            su.check_archive(archive, current)


def utils_version(text):
    import webapp.updates
    return webapp.updates.parse_version(text)


@pytest.mark.parametrize("change,match", [
    ({"__top__": "yadisk_backup-dev"}, "не та программа"),
    ({"webapp/__init__.py": None}, "нет файла webapp/__init__.py"),
    ({"webui.py": None}, "нет файла webui.py"),
    ({"utils.py": b"VERSION = '9.9.9'\n"}, "нет номера версии"),
    ({"utils.py": b"APP_VERSION = 'latest'\n"}, "нет номера версии"),
    ({"../evil.py": b"x"}, "недопустимым именем"),
    ({"webapp/../../evil.py": b"x"}, "недопустимым именем"),
    ({"CON.txt": b"x"}, "недопустимым именем"),
    ({"CONIN$.txt": b"x"}, "недопустимым именем"),
    ({"CONOUT$.txt": b"x"}, "недопустимым именем"),
    ({"COM0.txt": b"x"}, "недопустимым именем"),
    ({"COM\u00b9.txt": b"x"}, "недопустимым именем"),
    ({"LPT0.txt": b"x"}, "недопустимым именем"),
    ({"LPT\u00b3.txt": b"x"}, "недопустимым именем"),
    ({"notes./a.txt": b"x"}, "недопустимым именем"),
    ({"bad:name.py": b"x"}, "недопустимым именем"),
    ({"README.MD": b"twice"}, "два файла с одним именем"),
])
def test_archive_that_is_not_the_program_is_refused(tmp_path, change, match):
    change = dict(change)
    files = program(NEW)
    top = change.pop("__top__", su.ZIP_TOP)
    for name, data in change.items():
        if data is None:
            files.pop(name)
        else:
            files[name] = data
    archive = make_zip(tmp_path / "a.zip", files, top=top)
    with pytest.raises(su.UpdateError, match=match) as e:
        su.check_archive(archive, OLD)
    assert "Программа не изменилась" in e.value.text


def test_archive_with_a_link_is_refused(tmp_path):
    archive = make_zip(tmp_path / "a.zip", program(NEW) | {"link.py": b"/etc/passwd"},
                       modes={"link.py": stat.S_IFLNK | 0o777})
    with pytest.raises(su.UpdateError, match="ссылка на другой файл"):
        su.check_archive(archive, OLD)


def test_archive_bombs_and_garbage_are_refused(tmp_path, monkeypatch):
    html = tmp_path / "rate_limited.zip"
    html.write_bytes(b"<html>Too many requests</html>")
    with pytest.raises(su.UpdateError, match="не архив программы"):
        su.check_archive(html, OLD)
    archive = make_zip(tmp_path / "a.zip", program(NEW))
    monkeypatch.setattr(su, "MAX_FILES", 3)
    with pytest.raises(su.UpdateError, match="не похоже на программу"):
        su.check_archive(archive, OLD)
    monkeypatch.setattr(su, "MAX_FILES", 3000)
    monkeypatch.setattr(su, "MAX_UNPACKED_BYTES", 100)
    with pytest.raises(su.UpdateError, match="Распакованный архив больше"):
        su.check_archive(archive, OLD)


def test_extract_archive_enforces_real_streamed_bytes_against_spoofed_zip_header(tmp_path, monkeypatch):
    """extract_archive считает реальные распакованные байты, не доверяя только заголовкам check_archive."""
    import struct

    payload = b"A" * 50_000
    archive = make_zip(tmp_path / "big.zip", program(NEW) | {"payload.bin": payload})
    release = su.check_archive(archive, OLD)
    monkeypatch.setattr(su, "MAX_UNPACKED_BYTES", 4096)
    out = tmp_path / "out"
    with pytest.raises(su.UpdateError, match="Распакованный архив больше") as e:
        su.extract_archive(archive, release, out)
    assert "Программа не изменилась" in e.value.text
    # На диск записано не больше потолка (не распаковали все 50 КБ)
    written = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    assert written <= 4096

    # Подделанный file_size в заголовках ZIP тоже отвергается (по потолку байт или по контрольной сумме)
    spoofed = tmp_path / "spoofed.zip"
    raw = archive.read_bytes().replace(struct.pack("<I", len(payload)), struct.pack("<I", 1))
    spoofed.write_bytes(raw)
    release2 = su.check_archive(spoofed, OLD)
    with pytest.raises(su.UpdateError):
        su.extract_archive(spoofed, release2, tmp_path / "out2")


def test_damaged_file_in_the_archive_is_caught_on_unpacking(tmp_path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
        for name, data in program(NEW).items():
            zf.writestr(f"{su.ZIP_TOP}/{name}", data)
    raw = bytearray(archive.read_bytes())
    at = raw.index(f"readme {NEW}".encode("utf-8"))
    raw[at] ^= 0xFF  # содержимое испорчено, контрольная сумма — прежняя
    archive.write_bytes(bytes(raw))
    release = su.check_archive(archive, OLD)  # utils.py цел — номер версии читается
    with pytest.raises(su.UpdateError, match="повреждён"):
        su.extract_archive(archive, release, tmp_path / "src")


# ── Что обновление не трогает ──


@pytest.mark.parametrize("rel", [
    ".env", ".env.local", "cookies.json", "cookies_old.json", "cookies.json.tmp",
    "upload_unlim_config.json", "people_sorter_config.json",
    "state.db", "state.db-wal", "state.db-shm",
    "unlim_state.db-journal", "links_state.db", "upload_unlim_state.db", "people_sorter.db",
    "logs/webui.log", "logs/tasks/disk.log", "downloads/a.txt", "Downloads/a.txt",
    ".venv/pyvenv.cfg", "venv/x", ".uv/uv.exe", ".uv/uv",
    ".models/buffalo_l/det.onnx", ".cache/crops/face.jpg", ".cache/thumbs/1.jpg",
    ".pytest_cache/v/cache/nodeids", ".ruff_cache/0.6.0/x", ".mypy_cache/3.13/x",
    ".git/config", "webapp/__pycache__/rescue.cpython-312.pyc",
    "x.pyc", f"{BACKUP}/utils.py", "utils.py.yadisk-new", "utils.py.yadisk-restore", "_unlim_tmp_zips/1.zip",
])
def test_user_data_is_protected(rel):
    assert su.is_protected(rel)


@pytest.mark.parametrize("rel", [
    ".env.example", ".python-version", "webapp/rescue.py", "webui.bat", "webui.sh", "webui.command",
    "README.md", "uv.lock", "pyproject.toml", "supporters.json", "tests/test_utils.py",
    "docs/images/token.png", ".gitignore", ".gitattributes", ".github/workflows/ci.yml",
    "webui_assets/static/js/core.js", "uploader/engine.py", "uploader/webapp/manager.py",
    "sorter/paths.py", "sorter/vision/models.py", "evals/run_family_benchmark.py",
])
def test_program_files_are_not_protected(rel):
    assert not su.is_protected(rel)


def test_no_file_of_the_repository_is_skipped_by_the_update():
    """Иначе обновление молча не доставит этот файл никому."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    except OSError:
        pytest.skip("нужен git")
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip("нет git-копии")
    files = out.stdout.splitlines()
    assert "webui.py" in files
    assert [f for f in files if su.is_protected(f)] == []


def test_plan_skips_user_data_even_if_the_archive_has_it(tmp_path):
    project = make_tree(tmp_path / "app", program(OLD) | USER_DATA | {"files/mine.txt": b"mine"})
    evil = {name: b"EVIL" for name in (
        ".env", "cookies.json", "cookies.json.tmp", "upload_unlim_config.json", "people_sorter_config.json",
        "state.db", "logs/x.log", "downloads/y.txt", ".uv/uv.exe", ".models/a.onnx", ".cache/1.jpg",
        "files/mine.txt", ".venv/pyvenv.cfg",
    )}
    archive = make_zip(tmp_path / "a.zip", program(NEW) | evil)
    release = su.check_archive(archive, OLD)
    src = tmp_path / "src"
    su.extract_archive(archive, release, src)
    # Папка скачивания внутри папки программы — тоже чужая территория
    changes, skipped = su.plan_install(src, release, project, str(project / "files"))
    assert {c.rel for c in changes} == {"utils.py", "pyproject.toml", "uv.lock", "README.md", "webui.bat",
                                        "webapp/rescue.py"}
    assert set(evil) <= set(skipped)
    # Неизменившиеся файлы не трогаем вовсе
    assert "webui.py" not in {c.rel for c in changes} and "webapp/__init__.py" not in {c.rel for c in changes}
    with pytest.raises(su.UpdateError, match="сама папка программы"):
        su.plan_install(src, release, project, str(project))


def test_plan_refuses_odd_program_folders(tmp_path):
    project = make_tree(tmp_path / "app", program(OLD))
    archive = make_zip(tmp_path / "a.zip", program(NEW))
    release = su.check_archive(archive, OLD)
    src = tmp_path / "src"
    su.extract_archive(archive, release, src)
    (project / "README.md").unlink()
    (project / "README.md").mkdir()
    with pytest.raises(su.UpdateError, match="вместо файла README.md лежит папка"):
        su.plan_install(src, release, project)
    (project / "README.md").rmdir()
    # Папка программы ссылается на папку снаружи (symlink, в Windows — junction)
    outside = tmp_path / "outside"
    outside.mkdir()
    for p in (project / "webapp").iterdir():
        p.unlink()
    (project / "webapp").rmdir()
    try:
        if sys.platform.startswith("win"):
            import _winapi
            _winapi.CreateJunction(str(outside), str(project / "webapp"))
        else:
            os.symlink(outside, project / "webapp")
    except (OSError, AttributeError):
        pytest.skip("не удалось создать ссылку на папку")
    with pytest.raises(su.UpdateError, match="ссылка на другую папку"):
        su.plan_install(src, release, project)


# ── Замена файлов и откат ──


def test_install_replaces_the_program_and_keeps_user_data(tmp_path):
    project, release, changes = prepared(tmp_path, new_extra={"webapp/new_module.py": b"x = 1\n",
                                                             "docs/new/deep.txt": b"deep"})
    before = snapshot(project)
    backup = project / BACKUP
    steps, progress = [], []
    su.install(changes, project, backup, old=OLD, new=NEW, on_step=steps.append,
               on_progress=lambda done, total: progress.append((done, total)))
    after = snapshot(project)
    assert {name: after[name] for name in USER_DATA} == USER_DATA
    new = program(NEW)
    for name in new:
        assert after[name] == new[name], name
    assert after["webapp/new_module.py"] == b"x = 1\n" and after["docs/new/deep.txt"] == b"deep"
    # Копия: прежние версии заменённых файлов, пояснение и журнал
    assert (backup / "utils.py").read_bytes() == before["utils.py"]
    assert (backup / "webapp" / "rescue.py").read_bytes() == before["webapp/rescue.py"]
    assert not (backup / "webapp" / "new_module.py").exists() and not (backup / ".env").exists()
    manifest = json.loads((backup / "update.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "done" and manifest["from"] == OLD and manifest["to"] == NEW
    assert "utils.py" in manifest["replaced"] and "webapp/new_module.py" in manifest["created"]
    readme = (backup / "README.txt").read_text(encoding="utf-8")
    assert "Копия программы до обновления" in readme and OLD in readme and NEW in readme
    assert not [p for p in project.rglob("*") if p.name.endswith((su.STAGE_SUFFIX, su.RESTORE_SUFFIX))]
    assert steps == ["backup", "install"] and progress[-1] == (len(changes), len(changes))


@pytest.mark.skipif(sys.platform.startswith("win"), reason="права файлов — Linux и macOS")
def test_install_keeps_permissions_and_sets_exec_bit(tmp_path):
    project, release, changes = prepared(
        tmp_path, new_extra={"webui.sh": b"#!/bin/sh\necho new\n", "private.txt": b"new"},
        old_extra={"webui.sh": b"#!/bin/sh\necho old\n", "private.txt": b"old"})
    (project / "webui.sh").chmod(0o644)
    (project / "private.txt").chmod(0o600)
    archive = make_zip(tmp_path / "main2.zip", program(NEW) | {"webui.sh": b"#!/bin/sh\necho new\n",
                                                               "private.txt": b"new"}, modes={"webui.sh": 0o100755})
    release = su.check_archive(archive, OLD)
    src = tmp_path / "src2"
    su.extract_archive(archive, release, src)
    changes, _ = su.plan_install(src, release, project)
    su.install(changes, project, project / BACKUP, old=OLD, new=NEW)
    assert stat.S_IMODE((project / "webui.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((project / "private.txt").stat().st_mode) == 0o600


def test_read_only_program_file_is_replaced(tmp_path):
    project, release, changes = prepared(tmp_path)
    (project / "utils.py").chmod(stat.S_IREAD)
    su.install(changes, project, project / BACKUP, old=OLD, new=NEW)
    assert (project / "utils.py").read_bytes() == program(NEW)["utils.py"]


def test_replace_retries_even_if_chmod_raises_oserror(tmp_path, monkeypatch):
    src = tmp_path / "a.new"
    dst = tmp_path / "a.txt"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    real_replace = os.replace
    attempts = []

    def flaky_replace(a, b):
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError(errno.EACCES, "busy")
        return real_replace(a, b)

    monkeypatch.setattr(su, "IS_WINDOWS", True)
    monkeypatch.setattr(su.os, "replace", flaky_replace)
    monkeypatch.setattr(su.os, "access", lambda p, mode: False)
    monkeypatch.setattr(su.os, "chmod", lambda p, mode: (_ for _ in ()).throw(PermissionError("chmod denied")))
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    su._replace(src, dst, tries=3)
    assert len(attempts) == 2 and dst.read_bytes() == b"new"


def _flaky_replace(monkeypatch, fail_from: int, fail_until: int | None = None):
    """Замена номер fail_from (и дальше, до fail_until) — «файл занят»."""
    real = su._replace
    calls = []

    def flaky(src, dst, tries=10):
        calls.append(dst)
        if len(calls) >= fail_from and (fail_until is None or len(calls) <= fail_until):
            raise PermissionError(errno.EACCES, "Отказано в доступе", str(dst))
        return real(src, dst, tries)

    monkeypatch.setattr(su, "_replace", flaky)
    return calls


def test_failed_replace_puts_everything_back(tmp_path, monkeypatch):
    project, release, changes = prepared(tmp_path, new_extra={"webapp/sub/new.py": b"new\n"})
    before = snapshot(project)
    _flaky_replace(monkeypatch, fail_from=4, fail_until=4)
    backup = project / BACKUP
    with pytest.raises(su.UpdateError) as e:
        su.install(changes, project, backup, old=OLD, new=NEW)
    assert e.value.rolled_back
    assert "Всё вернул как было" in e.value.text and "Отказано в доступе" in e.value.text
    assert ".yadisk-new" not in e.value.text  # временное имя человеку ничего не скажет
    assert snapshot(project) == before  # байт в байт: заменённые вернулись, добавленных нет
    assert not (project / "webapp" / "sub").exists()
    assert not (project / su.BACKUP_DIRNAME).exists()  # копия совпадала с вернувшимся — не мусорим


def test_failed_rollback_tells_where_the_old_files_are(tmp_path, monkeypatch):
    project, release, changes = prepared(tmp_path)
    before = snapshot(project)
    _flaky_replace(monkeypatch, fail_from=3)  # и замена, и возврат из копии не выходят
    backup = project / BACKUP
    with pytest.raises(su.UpdateError) as e:
        su.install(changes, project, backup, old=OLD, new=NEW)
    assert not e.value.rolled_back
    assert BACKUP in e.value.text and "скопируйте их в папку программы" in e.value.text
    manifest = json.loads((backup / "update.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "rollback_failed" and manifest["failed"]
    for rel in manifest["failed"]:  # в копии лежит прежний файл — человеку есть что вернуть
        assert (backup / rel).read_bytes() == before[rel]


def test_no_backup_means_no_changes(tmp_path, monkeypatch):
    project, release, changes = prepared(tmp_path)
    before = snapshot(project)

    def no_space(*a, **k):
        raise OSError(errno.ENOSPC, "Недостаточно места на диске")

    monkeypatch.setattr(su.shutil, "copy2", no_space)
    with pytest.raises(su.UpdateError, match="Не удалось сохранить копию нынешней версии.*Недостаточно места") as e:
        su.install(changes, project, project / BACKUP, old=OLD, new=NEW)
    assert e.value.rolled_back and "Программа не изменилась" in e.value.text
    assert snapshot(project) == before and not (project / BACKUP).exists()


def test_failed_staging_leaves_no_traces(tmp_path, monkeypatch):
    project, release, changes = prepared(tmp_path, new_extra={"webapp/sub/new.py": b"new\n"})
    before = snapshot(project)
    real = su._copy_file
    n = []

    def flaky(src, dst, **kw):
        n.append(1)
        if len(n) == len(changes):
            raise OSError(errno.ENOSPC, "Недостаточно места на диске", str(dst))
        return real(src, dst, **kw)

    monkeypatch.setattr(su, "_copy_file", flaky)
    with pytest.raises(su.UpdateError, match="Не удалось записать новые файлы"):
        su.install(changes, project, project / BACKUP, old=OLD, new=NEW)
    assert snapshot(project) == before
    assert not (project / "webapp" / "sub").exists() and not (project / BACKUP).exists()


def test_existing_backup_folder_is_never_deleted(tmp_path):
    project, release, changes = prepared(tmp_path)
    backup = project / BACKUP
    backup.mkdir(parents=True)
    (backup / "keep.txt").write_text("чужое", encoding="utf-8")
    with pytest.raises(su.UpdateError, match="Не удалось сохранить копию"):
        su.install(changes, project, backup, old=OLD, new=NEW)
    assert (backup / "keep.txt").read_text(encoding="utf-8") == "чужое"


def test_unique_backup_path_handles_same_second_retries(tmp_path):
    root = tmp_path / su.BACKUP_DIRNAME
    stamp = "20261019-213005"
    p1 = su._unique_backup_path(root, OLD, stamp=stamp)
    assert p1.name == f"{OLD}_{stamp}"
    p1.mkdir(parents=True)
    p2 = su._unique_backup_path(root, OLD, stamp=stamp)
    assert p2.name == f"{OLD}_{stamp}-2"
    p2.mkdir(parents=True)
    p3 = su._unique_backup_path(root, OLD, stamp=stamp)
    assert p3.name == f"{OLD}_{stamp}-3"
    p3.mkdir(parents=True)
    p4 = su._unique_backup_path(root, OLD, stamp=stamp)
    assert p4.name == f"{OLD}_{stamp}-4"
    p4.mkdir(parents=True)
    su.prune_backups(root, keep=2)
    assert sorted(p.name for p in root.iterdir()) == [f"{OLD}_{stamp}-3", f"{OLD}_{stamp}-4"]


def test_prune_keeps_three_newest_and_foreign_folders(tmp_path):
    root = tmp_path / su.BACKUP_DIRNAME
    names = ["2.7.0_20261001-100000", "2.6.1_20261010-100000", "2.7.2_20261015-100000",
             "2.7.3_20261019-100000", "мои файлы", "2.7.9_garbage"]
    for name in names:
        (root / name).mkdir(parents=True)
    (root / "notes.txt").write_text("x", encoding="utf-8")
    su.prune_backups(root)
    assert sorted(p.name for p in root.iterdir()) == sorted(names[1:] + ["notes.txt"])
    su.prune_backups(tmp_path / "missing")  # нет папки — не беда


# ── Новая версия здесь запускается? ──


def test_smoke_test_catches_a_broken_release(tmp_path):
    project = make_tree(tmp_path / "app", program(OLD))
    cases = [({"webapp/bad.py": b"def broken(:\n"}, "ошибка в webapp/bad.py, строка 1"),
             ({"webui.py": b"import no_such_module_for_sure\n"}, "не запускается на этом компьютере"),
             ({}, None)]
    for i, (extra, match) in enumerate(cases):
        archive = make_zip(tmp_path / f"{i}.zip", program(NEW) | extra)
        release = su.check_archive(archive, OLD)
        src = tmp_path / f"src{i}"
        su.extract_archive(archive, release, src)
        if match is None:
            su.smoke_test(src, release, project)
        else:
            with pytest.raises(su.UpdateError, match=match) as e:
                su.smoke_test(src, release, project)
            assert "Программа не изменилась" in e.value.text


def test_version_bump_alone_is_not_a_dependency_change(tmp_path):
    # Настоящие файлы проекта: номер версии программы меняется в каждом выпуске
    real = {n: (ROOT / n).read_bytes() for n in ("pyproject.toml", "uv.lock", ".python-version")}
    mine = f'version = "{utils.APP_VERSION}"'.encode()
    newline = b"\r\n" if b"\r\n" in real["uv.lock"] else b"\n"  # рабочая копия Windows — с CRLF
    lock_entry = b'name = "yadisk-backup"' + newline + mine
    assert real["pyproject.toml"].count(mine) == 1 and real["uv.lock"].count(lock_entry) == 1
    old = make_tree(tmp_path / "old", real)
    bumped = {
        "pyproject.toml": real["pyproject.toml"].replace(mine, b'version = "9.9.9"').replace(b"\r\n", b"\n"),
        "uv.lock": real["uv.lock"].replace(lock_entry, lock_entry.replace(mine, b'version = "9.9.9"')).replace(
            b"\r\n", b"\n"
        ),
        ".python-version": real[".python-version"].replace(b"\r\n", b"\n"),
    }
    # Разные переводы строк (CRLF vs LF) и смена номера версии не должны вызывать пересборку
    assert not su.deps_changed(make_tree(tmp_path / "new", bumped), old)
    # Смена .python-version считается изменением окружения
    pyver_changed = dict(bumped, **{".python-version": b"3.12\n"})
    assert su.deps_changed(make_tree(tmp_path / "new_py", pyver_changed), old)
    more = dict(bumped, **{"pyproject.toml": bumped["pyproject.toml"].replace(
        b'"requests>=2.31.0",', b'"requests>=2.31.0",\n    "rich>=13.0",')})
    assert su.deps_changed(make_tree(tmp_path / "new2", more), old)


def test_find_uv_resolves_relative_env_and_local_fallbacks(tmp_path, monkeypatch):
    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.setattr(webapp.paths, "PROJECT", project)
    exe = "uv.exe" if su.IS_WINDOWS else "uv"
    # Относительный путь в UV разрешается относительно папки программы
    monkeypatch.setenv("UV", f".uv/{exe}")
    assert Path(su._find_uv()) == project / ".uv" / exe
    # Без UV и без uv в PATH находится локальный .uv/uv(.exe)
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.setattr(su.shutil, "which", lambda name: None)
    local_uv = project / ".uv" / exe
    local_uv.parent.mkdir(parents=True)
    local_uv.write_bytes(b"uv")
    assert Path(su._find_uv()) == local_uv


def test_prepare_deps_three_tier_fallback(tmp_path, monkeypatch):
    """_prepare_deps пробует people -> people-legacy (3.12) -> базовое ядро, всегда с --no-dev."""
    pyproject = (
        '[project]\nname = "yadisk-backup"\nversion = "3.1.0"\ndependencies = ["requests>=2.31.0"]\n\n'
        '[project.optional-dependencies]\npeople = ["onnxruntime>=1.19.0"]\n'
        'people-legacy = ["onnxruntime==1.19.2"]\n'
    ).encode("utf-8")
    src = make_tree(tmp_path / "src", {"pyproject.toml": pyproject, ".python-version": b"3.13\n"})
    monkeypatch.setenv("UV", sys.executable)
    venv_py = src / ".venv" / ("Scripts/python.exe" if su.IS_WINDOWS else "bin/python")
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"py")

    # 1) people падает, people-legacy проходит
    runs = []

    def fail_first(cmd, **kw):
        runs.append(cmd)
        if "--extra" in cmd and "people" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "no wheel for people")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(su.subprocess, "run", fail_first)
    assert su._prepare_deps(src) == str(venv_py)
    assert len(runs) == 2
    assert "--no-dev" in runs[0] and "--extra" in runs[0] and "people" in runs[0]
    assert "--python" in runs[0] and runs[0][runs[0].index("--python") + 1] == "3.13"
    assert "--no-dev" in runs[1] and "--extra" in runs[1] and "people-legacy" in runs[1]
    assert "--python" in runs[1] and runs[1][runs[1].index("--python") + 1] == "3.12"

    # 2) people и people-legacy падают, базовое ядро проходит
    runs.clear()

    def only_core_ok(cmd, **kw):
        runs.append(cmd)
        if "--extra" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "no wheel")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(su.subprocess, "run", only_core_ok)
    assert su._prepare_deps(src) == str(venv_py)
    assert len(runs) == 3
    assert "--extra" not in runs[2] and "--no-dev" in runs[2]
    assert runs[2][runs[2].index("--python") + 1] == "3.13"

    # 3) Все три варианта падают — UpdateError
    runs.clear()

    def all_fail(cmd, **kw):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 2, "", "network error")

    monkeypatch.setattr(su.subprocess, "run", all_fail)
    with pytest.raises(su.UpdateError, match="Новой версии нужны новые компоненты.*network error"):
        su._prepare_deps(src)
    assert len(runs) == 3


def test_new_dependencies_without_internet_cancel_the_update(tmp_path, monkeypatch):
    project = make_tree(tmp_path / "app", program(OLD))
    files = program(NEW)
    files["pyproject.toml"] = files["pyproject.toml"].replace(b'"requests>=2.31.0"', b'"requests>=2.31.0", "rich"')
    archive = make_zip(tmp_path / "a.zip", files)
    release = su.check_archive(archive, OLD)
    src = tmp_path / "src"
    su.extract_archive(archive, release, src)
    monkeypatch.setenv("UV", sys.executable)  # «uv» есть
    runs = []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 2, "", "error: Failed to fetch: `https://pypi.org/simple/rich/`")

    monkeypatch.setattr(su.subprocess, "run", fake_run)
    with pytest.raises(su.UpdateError, match="Новой версии нужны новые компоненты.*Проверьте интернет"):
        su.smoke_test(src, release, project)
    assert runs[0][1:5] == ["sync", "--frozen", "--no-dev", "--no-progress"]


# ── Кнопка целиком: отказы, перезапуск лаунчером, перезапуск вручную ──


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    project = make_tree(tmp_path / "app", program(OLD) | USER_DATA)
    monkeypatch.setattr(webapp.paths, "PROJECT", project)
    monkeypatch.setattr(utils, "APP_VERSION", OLD)
    # Баннер уже знает о релизе NEW; тестовые архивы собраны с папкой ZIP_TOP
    monkeypatch.setitem(webapp.updates._state, "latest", {"version": NEW, "url": webapp.updates.RELEASES_PAGE})
    real_source = su.release_source
    monkeypatch.setattr(su, "release_source", lambda version: (real_source(version)[0], su.ZIP_TOP))
    return project


def serve_zip(monkeypatch, zip_path: Path):
    """Вместо GitHub — готовый архив."""
    def fake_download(url, dest, progress=None, **kw):
        data = zip_path.read_bytes()
        dest.write_bytes(data)
        if progress:
            progress(len(data), len(data))
        return len(data)

    monkeypatch.setattr(su, "download_zip", fake_download)


def _finish():
    assert su._thread is not None
    su._thread.join(60)
    assert not su._thread.is_alive()
    return su.job_status()


class _AliveTask:
    def __init__(self, kind="disk_download"):
        self.kind = kind

    def alive(self):
        return True


def test_update_waits_until_nothing_downloads(app_dir, monkeypatch):
    tm = webapp.tasks.tm
    monkeypatch.setattr(webapp.rescue.rescue, "is_running", lambda: True)
    ok, msg = su.start_install()
    assert not ok and msg.startswith("Сначала остановите скачивание")
    assert su.install_info()["busy"] == msg
    monkeypatch.setattr(webapp.rescue.rescue, "is_running", lambda: False)
    for slot, kind, text in ((webapp.tasks.SLOT_LINKS, "links", "по ссылкам"),
                             (webapp.tasks.SLOT_ORGANIZE, "organize", "сортировки"),
                             (webapp.tasks.SLOT_DISK, "disk_download", "Сначала остановите скачивание"),
                             # Проверка скачанных файлов идёт в слотах скачивания, но просить надо другое
                             (webapp.tasks.SLOT_DISK, "disk_verify", "проверки скачанных файлов"),
                             (webapp.tasks.SLOT_UNLIM_COLLECT, "unlim_verify", "проверки скачанных файлов")):
        monkeypatch.setitem(tm._tasks, slot, _AliveTask(kind))
        ok, msg = su.start_install()
        assert not ok and text in msg, msg
        assert su.install_info()["busy"] == msg
        monkeypatch.delitem(tm._tasks, slot)
    assert su.job_status() is None and tm.frozen() is None and su.install_info()["busy"] is None


def test_update_waits_for_upload_and_people_and_freezes_their_endpoints(app_dir, monkeypatch):
    tm = webapp.tasks.tm
    # 1) Загрузка в Безлимит активна — обновлять нельзя
    monkeypatch.setattr(up_mgr.manager, "is_running", lambda: True)
    ok, msg = su.start_install()
    assert not ok and msg == su.UPLOAD_FIRST
    assert su.install_info()["busy"] == su.UPLOAD_FIRST
    monkeypatch.setattr(up_mgr.manager, "is_running", lambda: False)

    # 2) «По людям» активно — обновлять нельзя
    monkeypatch.setattr(webapp.people.manager, "is_running", lambda: True)
    ok, msg = su.start_install()
    assert not ok and msg == su.PEOPLE_FIRST
    assert su.install_info()["busy"] == su.PEOPLE_FIRST
    monkeypatch.setattr(webapp.people.manager, "is_running", lambda: False)

    # 3) Гонка: загрузка стартовала после pre-check, до post-freeze — заморозка снимается
    calls = {"n": 0}

    def racy_upload():
        calls["n"] += 1
        return calls["n"] >= 3  # 1: install_info, 2: pre_busy, 3: post_busy

    monkeypatch.setattr(up_mgr.manager, "is_running", racy_upload)
    ok, msg = su.start_install()
    assert not ok and msg == su.UPLOAD_FIRST and tm.frozen() is None
    monkeypatch.setattr(up_mgr.manager, "is_running", lambda: False)

    # 4) Если people.manager == None (зависимости не установлены), проверка не падает
    with monkeypatch.context() as mp:
        mp.setattr(webapp.people, "manager", None)
        assert su.install_info()["busy"] is None

    # 5) Когда tm заморожен, upload / people / rescue не стартуют
    assert tm.freeze(su.FROZEN_TEXT) == []
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    for url, body in (
        ("/api/upload/scan", {}),
        ("/api/upload/start", {}),
        ("/api/people/scan/start", {}),
        ("/api/people/cluster/run", None),
        ("/api/people/sort/start", {}),
        ("/api/people/runs/1/resume", {}),
        ("/api/people/runs/1/rollback", None),
    ):
        resp = client.post(url, json=body) if body is not None else client.post(url)
        assert resp.status_code == 409 and resp.json()["detail"] == su.FROZEN_TEXT, (url, resp.status_code, resp.text)
    rescue_resp = client.post("/api/rescue/start").json()
    assert rescue_resp == {"ok": False, "msg": su.FROZEN_TEXT}
    tm.unfreeze()


@pytest.mark.skipif(sys.platform.startswith("win") or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="права папок — Linux и macOS, не root")
def test_read_only_program_folder_is_refused_before_download(app_dir, monkeypatch):
    monkeypatch.setattr(su, "download_zip", lambda *a, **k: pytest.fail("качать не нужно"))
    app_dir.chmod(0o555)
    try:
        assert su.start_install()[0]
        job = _finish()
    finally:
        app_dir.chmod(0o755)
    assert job["state"] == "failed" and "Нет прав на запись в папку программы" in job["error"]
    assert webapp.tasks.tm.frozen() is None


def test_git_checkout_is_updated_with_git_pull(app_dir):
    (app_dir / ".git").mkdir()
    ok, msg = su.start_install()
    assert not ok and "git pull" in msg
    info = su.install_info()
    assert info["ok"] is False and "git pull" in info["why"]


def test_update_button_installs_and_the_launcher_restarts(app_dir, tmp_path, monkeypatch):
    serve_zip(monkeypatch, make_zip(tmp_path / "main.zip", program(NEW) | {"webapp/new.py": b"x = 1\n"}))
    monkeypatch.setenv(su.RESTART_ENV, "75")
    monkeypatch.setattr(su, "RESTART_DELAY_SEC", 0.01)
    monkeypatch.setattr(su, "EXIT_FALLBACK_SEC", 0.05)
    hard = threading.Event()
    monkeypatch.setattr(su, "_hard_exit", hard.set)
    stopped = threading.Event()
    su.set_restart_hook(stopped.set, 8123)
    assert su.install_info()["restart"] == "auto"
    before = snapshot(app_dir)
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    res = client.post("/api/update/install").json()
    assert res["ok"], res
    job = _finish()
    assert job["state"] == "restarting" and job["to"] == NEW and job["rolled_back"]
    assert job["backup"].startswith(f"{su.BACKUP_DIRNAME}/{OLD}_")
    # Сервер остановлен с кодом 75; не остановился бы сам — сработал бы жёсткий выход
    assert stopped.wait(5) and su.exit_code == su.RESTART_CODE
    assert hard.wait(5)
    after = snapshot(app_dir)
    assert {name: after[name] for name in USER_DATA} == {name: before[name] for name in USER_DATA}
    assert after["utils.py"] == program(NEW)["utils.py"] and after["webapp/new.py"] == b"x = 1\n"
    # Пока новая версия не запустилась, задачи не стартуют
    assert webapp.tasks.tm.start(webapp.tasks.SLOT_DISK, "main.py", ["scan"]) == (False, su.FROZEN_TEXT)
    marker = json.loads((webapp.paths.LOG_DIR / su.MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["from"] == OLD and marker["to"] == NEW and marker["port"] == 8123 and marker["restart"] == "auto"
    st = client.get("/api/status").json()
    assert st["update_job"]["state"] == "restarting"


def test_without_a_looping_launcher_the_person_restarts_by_hand(app_dir, tmp_path, monkeypatch):
    serve_zip(monkeypatch, make_zip(tmp_path / "main.zip", program(NEW)))
    su.set_restart_hook(lambda: pytest.fail("сервер не должен останавливаться сам"), 8000)
    assert su.install_info()["restart"] == "manual"  # лаунчер не сказал YADISK_RESTART_CODE=75
    ok, msg = su.start_install()
    assert ok, msg
    job = _finish()
    launcher = su.launcher_name()
    assert job["state"] == "restart_manual" and job["launcher"] == launcher and su.exit_code is None
    ok, msg = webapp.tasks.tm.start(webapp.tasks.SLOT_DISK, "main.py", ["scan"])
    assert not ok and "перезапустите" in msg and launcher in msg
    assert su.start_install() == (False, su.restart_text(launcher))
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    res = client.post("/api/rescue/start").json()
    assert not res["ok"] and "перезапустите" in res["msg"]
    assert (app_dir / "utils.py").read_bytes() == program(NEW)["utils.py"]


def test_failed_update_lets_downloads_run_again(app_dir, tmp_path, monkeypatch):
    bad = tmp_path / "main.zip"
    bad.write_bytes(b"<html>GitHub is down</html>")
    serve_zip(monkeypatch, bad)
    before = snapshot(app_dir)
    assert su.start_install()[0]
    job = _finish()
    assert job["state"] == "failed" and job["rolled_back"] and "не архив программы" in job["error"]
    assert webapp.tasks.tm.frozen() is None and snapshot(app_dir) == before
    assert not list(Path(app_dir).glob(f"{su.BACKUP_DIRNAME}/*"))


def test_second_click_while_updating_is_refused(app_dir, tmp_path, monkeypatch):
    gate = threading.Event()

    def slow_download(url, dest, progress=None, **kw):
        gate.wait(10)
        raise su.UpdateError("GitHub не ответил. Программа не изменилась.")

    monkeypatch.setattr(su, "download_zip", slow_download)
    assert su.start_install()[0]
    assert su.start_install() == (False, "Обновление уже идёт")
    assert webapp.tasks.tm.start(webapp.tasks.SLOT_LINKS, "public_download.py", []) == (False, su.FROZEN_TEXT)
    gate.set()
    assert _finish()["state"] == "failed"


def test_restart_marker_tells_the_new_version_it_was_just_installed(monkeypatch):
    monkeypatch.setattr(utils, "APP_VERSION", NEW)
    path = webapp.paths.LOG_DIR / su.MARKER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)

    def mark(at, to=NEW):
        path.write_text(json.dumps({"from": OLD, "to": to, "at": at, "port": 8000, "restart": "auto",
                                    "backup": BACKUP}), encoding="utf-8")

    mark(1000.0)
    data = su.take_restart_marker(now=1010.0)
    assert data["port"] == 8000 and data["restart"] == "auto" and not path.exists()
    assert su.updated_info(now=1020.0) == {"from": OLD, "to": NEW, "at": 1010.0, "backup": BACKUP}
    assert su.updated_info(now=1010.0 + su.UPDATED_SHOW_SEC + 1) is None
    # Старая метка и метка про другую версию не считаются — и тоже удаляются
    monkeypatch.setattr(su, "_updated", None)
    for at, to in ((1000.0 - su.MARKER_FRESH_SEC - 1, NEW), (1000.0, "9.9.9")):
        mark(at, to)
        assert su.take_restart_marker(now=1000.0) is None and not path.exists()
    assert su.take_restart_marker(now=1000.0) is None  # метки нет — тихо None
    assert su.updated_info() is None


def test_status_reports_the_update(app_dir, monkeypatch):
    import webapp.access
    import webapp.updates
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    monkeypatch.setitem(webapp.updates._state, "latest", None)
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    st = client.get("/api/status").json()
    assert st["update"] is None and st["update_job"] is None and st["updated"] is None
    monkeypatch.setitem(webapp.updates._state, "latest", {"version": NEW, "url": webapp.updates.RELEASES_PAGE})
    upd = client.get("/api/status").json()["update"]
    assert upd["latest"] == NEW and upd["install"] == {"ok": True, "why": None, "busy": None, "restart": "manual",
                                                       "launcher": su.launcher_name()}


def test_demo_states_of_the_update(monkeypatch):
    import webapp.access
    monkeypatch.setenv("YADISK_DEMO", "1")
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    st = client.get("/api/status?demo=update").json()
    assert st["update"]["install"]["ok"] and st["update_job"] is None
    running = client.get("/api/status?demo=update_running").json()
    assert running["update_job"]["state"] == "running" and running["update_job"]["detail"]
    assert client.get("/api/status?demo=update_manual").json()["update_job"]["state"] == "restart_manual"
    failed = client.get("/api/status?demo=update_failed").json()["update_job"]
    assert failed["state"] == "failed" and failed["rolled_back"]
    assert client.get("/api/status?demo=updated").json()["updated"]["to"] == utils.APP_VERSION
    # Демо-кнопка ничего не ставит: демо есть только в копии разработчика (git) — там кнопки нет
    assert client.get("/api/status?demo=ready").json()["update_job"] is None


@pytest.fixture
def fake_main(monkeypatch):
    """webui.main() без настоящего сервера и сети: порты — из ports_answer(first, tries), записываются в calls."""
    import logging

    import uvicorn

    calls = []
    run = {"exit_code": None}

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self, sockets=None):
            su.exit_code = run["exit_code"]  # RESTART_CODE — как после _exit_for_restart

    def choose(first, tries):
        calls.append((first, tries))
        return run["ports"](first, tries)

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(webui.ports, "PORT", 8000)
    monkeypatch.setattr(webui.ports, "PORT_FIXED", False)
    monkeypatch.setattr(webui.ports, "choose_port", choose)
    monkeypatch.setattr(webui.control, "resume_after_restart", lambda: False)
    monkeypatch.setattr(webui, "RESTART_PORT_WAIT_SEC", 0.05)
    monkeypatch.setattr(webui, "RESTART_PORT_STEP_SEC", 0.01)
    asked = []
    run["asked"] = asked
    # не стучаться на настоящий 8000; записываем, спрашивали ли соседа
    monkeypatch.setattr(webui.ports, "other_copy_on", lambda port: asked.append(port))
    monkeypatch.setattr(webui.ports, "use_port", lambda port: None)
    monkeypatch.setattr(webui.diagnostics, "start_health_monitor", lambda token: None)
    monkeypatch.setenv("YADISK_NO_BROWSER", "1")
    logger_names = ("webui", "uvicorn.error", "photo_sorter", "unlim_uploader")
    handlers = {name: list(logging.getLogger(name).handlers) for name in logger_names}
    yield run, calls
    # main() вешает файловый и консольный журналы на 4 логгера — снимаем, чтобы не текли в другие тесты
    for name, before in handlers.items():
        logger = logging.getLogger(name)
        for h in [h for h in logger.handlers if h not in before]:
            logger.removeHandler(h)
            h.close()


def test_webui_main_restarts_with_code_75(fake_main):
    """webui.main(): сервер остановили ради перезапуска — процесс выходит с кодом 75 для лаунчера."""
    run, calls = fake_main
    run.update(exit_code=su.RESTART_CODE, ports=lambda first, tries: (object(), 8123, None))
    with pytest.raises(SystemExit) as e:
        webui.main()
    assert e.value.code == su.RESTART_CODE
    assert su._port == 8123 and su._restart_hook is not None
    assert calls == [(8000, webui.ports.PORT_TRIES)]  # метки нет — порт выбирается как обычно


@pytest.mark.parametrize("old_port_free", [True, False])
def test_restart_takes_its_old_port_first(fake_main, old_port_free, capsys):
    """После обновления — прежний порт: страница на нём продолжит работать с этой копией. Пока шёл
    перезапуск, порт могла занять копия для второго аккаунта — тогда как обычно, первый свободный."""
    run, calls = fake_main
    path = webapp.paths.LOG_DIR / su.MARKER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"from": OLD, "to": utils.APP_VERSION, "at": time.time(), "port": 8001,
                                "restart": "auto", "backup": BACKUP}), encoding="utf-8")

    def answer(first, tries):
        if first == 8001:
            return (object(), 8001, None) if old_port_free else (None, 8001, None)
        return object(), 8002, None

    run["ports"] = answer
    webui.main()
    out = capsys.readouterr().out
    if old_port_free:
        assert calls == [(8001, 1)] and su._port == 8001
        # 8000 никто не держит — значит, и писать «Порт 8000 занят другой программой» нельзя; соседа не
        # спрашиваем (в Windows отказ соединения ждёт секунды, а открытая страница ждёт перезапуска)
        assert "занят другой программой" not in out and "Страница в браузере обновится сама" in out
        assert run["asked"] == []
    else:
        assert calls[0] == (8001, 1) and calls[-1] == (8000, webui.ports.PORT_TRIES) and su._port == 8002
        assert run["asked"] == [8000]
        assert "Страница в браузере обновится сама" not in out  # порт другой — откроется новая вкладка
    assert su.updated_info()["to"] == utils.APP_VERSION


def test_auto_restart_retries_same_wanted_port_before_falling_back(fake_main, capsys, monkeypatch):
    """На Windows без SO_REUSEADDR порт 8000 освобождается с задержкой — автоперезапуск ждёт и остаётся на 8000."""
    # Без настоящих пауз и с запасом по времени: на медленном раннере macOS 2,5 с истекали
    # раньше третьей попытки, и тест падал без ошибки в коде.
    monkeypatch.setattr(webui, "RESTART_PORT_STEP_SEC", 0.0)
    monkeypatch.setattr(webui, "RESTART_PORT_WAIT_SEC", 60.0)
    run, calls = fake_main
    path = webapp.paths.LOG_DIR / su.MARKER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"from": OLD, "to": utils.APP_VERSION, "at": time.time(), "port": 8000,
                                "restart": "auto", "backup": BACKUP}), encoding="utf-8")
    attempts = {"n": 0}

    def answer(first, tries):
        attempts["n"] += 1
        if first == 8000 and tries == 1 and attempts["n"] < 3:
            return None, 8000, None
        return object(), first, None

    run["ports"] = answer
    webui.main()
    out = capsys.readouterr().out
    assert calls == [(8000, 1), (8000, 1), (8000, 1)] and su._port == 8000
    assert "Страница в браузере обновится сама" in out


def test_slow_ops_never_block_the_status_poll(app_dir, monkeypatch):
    """Обновление идёт в фоне: /api/status отвечает сразу."""
    import webapp.access
    gate = threading.Event()
    monkeypatch.setattr(su, "download_zip", lambda *a, **k: gate.wait(10) and 0)
    monkeypatch.setattr(webapp.access, "check_token", lambda tok: {"ok": False, "msg": "Токен не задан"})
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    try:
        assert client.post("/api/update/install").json()["ok"]
        started = time.monotonic()
        job = client.get("/api/status").json()["update_job"]
        assert job["state"] == "running" and job["step"] == "download"
        assert time.monotonic() - started < 5
    finally:
        gate.set()
        _finish()


def test_release_source_is_the_tag_archive():
    """Ставится архив того релиза, о котором сказал баннер, а не вершина main."""
    url, top = su.release_source("3.1.0")
    assert url == "https://github.com/PXSYS13/yadisk_backup/archive/refs/tags/v3.1.0.zip"
    assert top == "yadisk_backup-3.1.0"


def test_tag_archive_is_checked_by_its_folder_and_version(tmp_path):
    archive = make_zip(tmp_path / "t.zip", program(NEW), top=f"yadisk_backup-{NEW}")
    assert su.check_archive(archive, OLD, top=f"yadisk_backup-{NEW}", expect=NEW).version == NEW
    with pytest.raises(su.UpdateError, match="не та программа"):
        su.check_archive(archive, OLD)  # папка релиза — не yadisk_backup-main
    with pytest.raises(su.UpdateError, match="обещана 3.2.0"):
        su.check_archive(archive, OLD, top=f"yadisk_backup-{NEW}", expect="3.2.0")


def test_install_downloads_the_offered_release(app_dir, monkeypatch):
    seen = []

    def fake_download(url, dest, progress=None, **kw):
        seen.append(url)
        raise su.UpdateError("стоп")

    monkeypatch.setattr(su, "download_zip", fake_download)
    assert su.start_install()[0]
    _finish()
    assert seen == ["https://github.com/PXSYS13/yadisk_backup/archive/refs/tags/v3.1.0.zip"]


def test_install_waits_until_the_release_is_known(app_dir, monkeypatch):
    import webapp.updates
    monkeypatch.setitem(webapp.updates._state, "latest", None)
    assert su.start_install() == (False, su.NO_OFFER_TEXT)
    assert not webapp.tasks.tm.frozen()  # отказ до заморозки задач
