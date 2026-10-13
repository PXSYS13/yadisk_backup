"""Лаунчеры: то, что пользователь запускает двойным кликом, должно запускаться.

Ломалось на практике: .bat с LF в ZIP с GitHub, webui.sh без бита исполнения.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["webui.bat", "stats.bat", "watch.bat"])
def test_bat_files_are_crlf(name):
    data = (ROOT / name).read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n"), f"{name}: cmd.exe нужен CRLF"


@pytest.mark.parametrize("name", ["webui.sh", "webui.command"])
def test_unix_launchers_are_lf_with_shebang(name):
    data = (ROOT / name).read_bytes()
    assert data.startswith(b"#!"), f"{name}: нет shebang"
    assert b"\r" not in data, f"{name}: CRLF сломает bash"


def test_mac_launcher_delegates_to_webui_sh():
    text = (ROOT / "webui.command").read_text(encoding="utf-8")
    assert 'cd "$(dirname "$0")"' in text and "webui.sh" in text


@pytest.mark.skipif(not shutil.which("git"), reason="нужен git")
@pytest.mark.parametrize("name", ["webui.sh", "webui.command"])
def test_unix_launchers_are_executable_in_git(name):
    """Бит исполнения попадает в ZIP с GitHub — без него двойной клик на macOS не сработает."""
    out = subprocess.run(["git", "ls-files", "-s", name], cwd=ROOT,
                         capture_output=True, text=True).stdout
    if not out:
        pytest.skip("файл ещё не в индексе git")
    assert out.split()[0] == "100755", out
