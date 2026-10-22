"""Лаунчеры: то, что пользователь запускает двойным кликом, должно запускаться.

Ломалось на практике: .bat с LF в ZIP с GitHub, webui.sh без бита исполнения.
"""

import re
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


def test_launchers_pin_and_verify_the_uv_installer():
    """uv ставит установщик закреплённой версии и только после сверки SHA-256, а не «irm | iex».

    Версия та же, что в CI: пользователи получают uv, на котором проходят тесты.
    """
    bat = (ROOT / "webui.bat").read_text(encoding="ascii")
    sh = (ROOT / "webui.sh").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    version = re.search(r'set "UV_VERSION=(\d+\.\d+\.\d+)"', bat).group(1)
    assert f'UV_VERSION="{version}"' in sh
    assert f'version: "{version}"' in ci
    assert re.search(r'set "UV_INSTALLER_SHA256=[0-9a-f]{64}"', bat)
    assert re.search(r'UV_INSTALLER_SHA256="[0-9a-f]{64}"', sh)
    for text in (bat, sh):
        assert "| iex" not in text and "| sh" not in text and "astral.sh/uv/install" not in text


@pytest.mark.skipif(not shutil.which("git"), reason="нужен git")
@pytest.mark.parametrize("name", ["webui.sh", "webui.command"])
def test_unix_launchers_are_executable_in_git(name):
    """Бит исполнения попадает в ZIP с GitHub — без него двойной клик на macOS не сработает."""
    out = subprocess.run(["git", "ls-files", "-s", name], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8").stdout
    if not out:
        pytest.skip("файл ещё не в индексе git")
    assert out.split()[0] == "100755", out
