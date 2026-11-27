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
    """uv закреплённой версии и только после сверки SHA-256, а не «irm | iex».

    Версия та же, что в CI: пользователи получают uv, на котором проходят тесты.
    """
    bat = (ROOT / "webui.bat").read_text(encoding="ascii")
    sh = (ROOT / "webui.sh").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    version = re.search(r'set "UV_VERSION=(\d+\.\d+\.\d+)"', bat).group(1)
    assert f'UV_VERSION="{version}"' in sh
    assert f'version: "{version}"' in ci
    for arch in ("X64", "ARM64", "X86"):
        assert re.search(rf'set "UV_ZIP_SHA256_{arch}=[0-9a-f]{{64}}"', bat), arch
    assert re.search(r'UV_INSTALLER_SHA256="[0-9a-f]{64}"', sh)
    for text in (bat, sh):
        assert "| iex" not in text and "| sh" not in text and "astral.sh/uv/install" not in text


def test_windows_launcher_runs_nothing_it_downloads_as_a_script():
    """Kaspersky принял webui.bat 2.8.2 за загрузчик (UDS:Trojan.BAT.Alien.gen): тот скачивал .ps1
    в %TEMP% и запускал его с -ExecutionPolicy Bypass. Теперь из сети приходит только официальный
    zip-архив uv: его сумма сверяется, uv.exe распаковывается в .uv, PATH и реестр не трогаются."""
    bat = (ROOT / "webui.bat").read_text(encoding="ascii")
    for pattern in ("ExecutionPolicy", "-File ", ".ps1", "Invoke-WebRequest", "DownloadFile", "iex",
                    "Invoke-Expression", "WindowStyle", "EncodedCommand", "%TEMP%", "certutil", "bitsadmin",
                    "setx", "reg add"):
        assert pattern.lower() not in bat.lower(), pattern
    assert "-pc-windows-msvc.zip" in bat and "Get-FileHash" in bat
    # Архив и uv.exe — по относительным путям: curl и tar не получают кириллицу из пути к папке
    assert 'cd /d "%~dp0"' in bat and 'tar -xf "%UV_ZIP%" -C .uv uv.exe' in bat


@pytest.mark.parametrize("name", ["stats.bat", "watch.bat"])
def test_helper_bats_find_the_uv_that_webui_bat_downloaded(name):
    """Без uv в PATH webui.bat кладёт uv.exe в .uv — stats.bat и watch.bat ищут его там же,
    а не просят «запустить webui.bat» по кругу. Инструменты для тестов людям не ставятся."""
    text = (ROOT / name).read_text(encoding="ascii")
    assert r'set "UV=.uv\uv.exe"' in text and r'set "UV=%USERPROFILE%\.local\bin\uv.exe"' in text
    assert '"%UV%" run --no-dev ' in text


def test_launchers_never_install_test_tools_for_people():
    """uv run ставит группу dev: pytest, ruff.exe, httpx2.exe — лишние exe у людей и лишние
    поводы для антивируса. Лаунчеры запускают программу с --no-dev."""
    assert '"%UV%" run --no-dev webui.py' in (ROOT / "webui.bat").read_text(encoding="ascii")
    assert "exec uv run --no-dev webui.py" in (ROOT / "webui.sh").read_text(encoding="utf-8")


@pytest.mark.skipif(not shutil.which("git"), reason="нужен git")
@pytest.mark.parametrize("name", ["webui.sh", "webui.command"])
def test_unix_launchers_are_executable_in_git(name):
    """Бит исполнения попадает в ZIP с GitHub — без него двойной клик на macOS не сработает."""
    out = subprocess.run(["git", "ls-files", "-s", name], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8").stdout
    if not out:
        pytest.skip("файл ещё не в индексе git")
    assert out.split()[0] == "100755", out


def _parse_minor(ver: str) -> tuple[int, int]:
    major, minor = ver.strip().split(".")[:2]
    return int(major), int(minor)


def test_python_version_pinned_and_bounded():
    """Без верхней границы requires-python и .python-version uv скачивает свежий CPython (напр. 3.15),
    для которого у onnxruntime ещё нет wheel — программа перестаёт запускаться у пользователей."""
    py_ver_file = ROOT / ".python-version"
    assert py_ver_file.is_file(), "Нет .python-version: uv сам выберет новейший Python без wheel'ов"
    pinned = py_ver_file.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"3\.\d+", pinned), f"Ожидается 3.X в .python-version, получено {pinned!r}"

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'(?m)^requires-python\s*=\s*"([^"]+)"', pyproject)
    assert m, "В pyproject.toml не найден requires-python"
    spec = m.group(1)

    low_m = re.search(r">=\s*(3\.\d+)", spec)
    high_m = re.search(r"<\s*(3\.\d+)", spec)
    assert low_m and high_m, f"requires-python должен иметь и нижнюю, и верхнюю границу: {spec!r}"
    assert _parse_minor(low_m.group(1)) <= _parse_minor(pinned) < _parse_minor(high_m.group(1)), (
        f".python-version ({pinned}) вне диапазона requires-python ({spec})"
    )

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f'"{pinned}"' in ci, f"Закреплённый Python {pinned} должен проверяться в CI"


def test_uv_lock_has_wheels_for_pinned_python_on_all_desktop_platforms():
    """Для закреплённого Python каждый нативный пакет из uv.lock должен иметь готовый wheel
    на Windows (x64/arm64), macOS (Apple Silicon и Intel) и Linux (x64/arm64)."""
    try:
        import tomllib
    except ModuleNotFoundError:
        pytest.skip("tomllib доступен с Python 3.11+")

    py_ver_file = ROOT / ".python-version"
    assert py_ver_file.is_file(), "Нет .python-version"
    pinned = py_ver_file.read_text(encoding="utf-8").strip()
    py_num = int(pinned.replace(".", ""))
    cp_tag = f"cp{py_num}"

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    root_pkg = next(p for p in lock["package"] if p["name"] == "yadisk-backup")

    by_name_ver = {(p["name"], p.get("version")): p for p in lock["package"]}
    # Собираем нативные зависимости с их маркерами, действующими для закреплённого Python
    native_deps: dict[str, list[tuple[str, dict]]] = {}
    for dep in root_pkg.get("dependencies", []):
        name = dep["name"]
        ver = dep.get("version")
        marker = dep.get("marker", "")
        if f"python_full_version < '{pinned}'" in marker:
            continue
        if "python_full_version ==" in marker and f"'{pinned}.*'" not in marker:
            continue
        pkg = by_name_ver.get((name, ver))
        if not pkg:
            continue
        wheels = pkg.get("wheels", [])
        if not wheels or any(w["url"].endswith("-none-any.whl") for w in wheels):
            continue
        native_deps.setdefault(name, []).append((marker, pkg))

    assert "onnxruntime" in native_deps, "onnxruntime должен присутствовать в uv.lock"

    platforms = {
        "win_amd64": lambda p: "win_amd64" in p,
        "win_arm64": lambda p: "win_arm64" in p,
        "macos_arm64": lambda p: "macosx" in p and ("arm64" in p or "universal2" in p),
        "macos_x86_64": lambda p: "macosx" in p and ("x86_64" in p or "universal2" in p),
        "linux_x86_64": lambda p: "manylinux" in p and "x86_64" in p,
        "linux_aarch64": lambda p: "manylinux" in p and "aarch64" in p,
    }

    def wheel_matches(pkg: dict, plat_pred) -> bool:
        for w in pkg.get("wheels", []):
            fn = w["url"].rsplit("/", 1)[-1][:-4]
            py_tag, abi_tag, plat_tag = fn.split("-")[-3:]
            if not plat_pred(plat_tag):
                continue
            if py_tag == cp_tag:
                return True
            if abi_tag == "abi3" and py_tag.startswith("cp") and py_num >= int(py_tag[2:]):
                return True
        return False

    missing = []
    for name, entries in sorted(native_deps.items()):
        for plat_name, pred in platforms.items():
            if not any(wheel_matches(pkg, pred) for _, pkg in entries):
                missing.append(f"{name} ({plat_name}, {cp_tag})")
    assert not missing, f"В uv.lock нет готовых wheel'ов для {pinned}: {', '.join(missing)}"
