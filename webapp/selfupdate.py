"""«Обновить программу»: новая версия с GitHub одной кнопкой — без риска остаться без программы.

Раньше человек сам скачивал архив main.zip и распаковывал его поверх папки, и многие так и
сидели на старых версиях. Кнопка делает то же самое, но осторожно:

1. Только когда ничего не идёт: ни спасение, ни скачивание по ссылкам, ни загрузка в Безлимит,
   ни сортировка (в том числе «По людям»). На время обновления запуск задач запрещён (tm.freeze).
2. Архив качается во временную папку (потолок размера, срок), проверяется — та ли это
   программа (папка yadisk_backup-main/ с webui.py, webapp/, utils.py) и новее ли её
   APP_VERSION — и распаковывается тоже во временную папку. Новая версия должна хотя бы
   запускаться здесь: всё компилируется, webui импортируется.
3. Файлы ставятся в три шага: копия каждого заменяемого файла в update_backup/<версия>_<время>/,
   новые файлы рядом с прежними (имя.yadisk-new), и только потом быстрые переименования.
   Ошибка на последнем шаге возвращает всё как было. Данные пользователя (.env, cookies.json,
   базы *.db*, конфиги upload/people, .models/, .cache/, .uv/, logs/, downloads/, папка скачивания,
   .venv/, .git/) обновление не трогает.
4. Перезапуск. Работающий Python себя не перечитает, поэтому сервер выходит с кодом 75,
   а лаунчер (webui.bat, webui.sh) снова запускает программу — uv заодно ставит
   изменившиеся зависимости. Лаунчер сообщает, что умеет так, переменной YADISK_RESTART_CODE;
   без неё (прежний лаунчер без цикла, запуск вручную) человека просят перезапустить программу самому.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from fastapi import APIRouter

import utils
from utils import CASE_INSENSITIVE_PATHS, IS_WINDOWS, TELEGRAM_CHANNEL, to_long_path
from webapp import config, paths, updates
from webapp.rescue import rescue
from webapp.tasks import SLOT_LINKS, SLOT_ORGANIZE, tm

log = logging.getLogger("webui")
router = APIRouter()

ZIP_URL = f"https://github.com/{updates.REPO}/archive/refs/heads/main.zip"
# Для проверок: архив с другого адреса (http — только с этого компьютера)
URL_ENV = "YADISK_UPDATE_URL"
ZIP_TOP = "yadisk_backup-main"
GITHUB_HOSTS = ("github.com", "codeload.github.com")
LOCAL_HOSTS = ("127.0.0.1", "localhost")
# Сейчас архив ~1 МБ, распакованный ~3 МБ: потолки с запасом, но архив-бомбу не пропустят
MAX_ZIP_BYTES = 30 * 1024 * 1024
MAX_FILES = 3000
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
DOWNLOAD_TIMEOUT = (10, 30)  # соединение; ожидание следующего куска
DOWNLOAD_DEADLINE_SEC = 300  # весь архив
PREPARE_TIMEOUT_SEC = 900.0  # общий бюджет времени на uv sync (все 3 варианта)
MAX_REDIRECTS = 5
REQUIRED_FILES = ("webui.py", "utils.py", "pyproject.toml", "webapp/__init__.py")
_VERSION_LINE = re.compile(r"""^APP_VERSION\s*=\s*["']([^"'\r\n]+)["']""", re.M)

RESTART_CODE = 75
RESTART_ENV = "YADISK_RESTART_CODE"
RESTART_DELAY_SEC = 2.5  # интерфейс успевает показать «Перезапускаю программу»
EXIT_FALLBACK_SEC = 20  # сервер так и не остановился (завис запрос) — выходим жёстко
MARKER_NAME = "update_restart.json"  # в logs/: новая версия узнаёт, что её только что поставили
MARKER_FRESH_SEC = 15 * 60
UPDATED_SHOW_SEC = 10 * 60

BACKUP_DIRNAME = "update_backup"
KEEP_BACKUPS = 3
STAGE_SUFFIX = ".yadisk-new"
RESTORE_SUFFIX = ".yadisk-restore"
_BACKUP_NAME = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}_\d{8}-\d{6}(?:-\d{1,6})?$")

# Данные пользователя и локальные кеши: сюда обновление не пишет, даже если такое имя окажется в архиве
PROTECTED_DIRS = frozenset({
    ".git", ".venv", "venv", "env", ".uv", "logs", "downloads", "__pycache__",
    "_unlim_tmp_zips", BACKUP_DIRNAME, ".models", ".cache",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".claude", ".idea", ".vscode",
})
_PROTECTED_FILES = frozenset({
    "upload_unlim_config.json",
    "people_sorter_config.json",
    "cookies.json.tmp",
})

STEP_TEXT = {
    "download": "Скачиваю новую версию с GitHub",
    "check": "Проверяю архив",
    "prepare": "Проверяю, что новая версия запускается",
    "backup": "Сохраняю копию нынешней версии",
    "install": "Заменяю файлы программы",
    "restart": "Перезапускаю программу",
}
STOP_FIRST = ("Сначала остановите скачивание: кнопка «Остановить» на Главной. Скачанное сохранится, "
              "а после обновления программа продолжит с того же места.")
UPLOAD_FIRST = "Сначала остановите загрузку в Безлимит: раздел «Загрузка» → «Остановить»."
PEOPLE_FIRST = "Сначала дождитесь конца операции в разделе «По людям» или остановите её там."
# Проверка скачанных файлов (задачи вида disk_verify, unlim_verify) идёт в слотах скачивания
VERIFY_FIRST = ("Сначала дождитесь конца проверки скачанных файлов или остановите её в «Подробнее» — "
                "потом обновите программу.")
FROZEN_TEXT = "Идёт обновление программы — подождите минуту"
UNCHANGED = "Программа не изменилась."
ASK_AUTHOR = f"Напишите автору в Telegram ({TELEGRAM_CHANNEL}) — поможем."

_WIN_BAD_CHARS = set('<>:"|?*\\')
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in "0123456789¹²³"),
    *(f"LPT{i}" for i in "0123456789¹²³"),
}

_lock = threading.Lock()
# Последнее обновление в этом запуске программы (для /api/status.update_job), иначе None
_job: dict | None = None
_thread: threading.Thread | None = None  # поток обновления (тесты ждут его)
_restart_hook = None  # webui.main(): остановить сервер, чтобы лаунчер запустил программу снова
_port: int | None = None
# webui.main() выходит с этим кодом, когда сервер остановлен ради перезапуска
exit_code: int | None = None
# Программу только что обновили — {"from", "to", "at", "backup"} из метки, иначе None
_updated: dict | None = None
_fallback: threading.Timer | None = None  # жёсткий выход, если сервер не остановился сам


class UpdateError(Exception):
    """Обновление не удалось. text — для человека; rolled_back=False — вернуть всё как было не вышло."""

    def __init__(self, text: str, *, rolled_back: bool = True):
        super().__init__(text)
        self.text = text
        self.rolled_back = rolled_back


@dataclass
class Release:
    version: str
    files: dict  # путь внутри программы ("webapp/rescue.py") → zipfile.ZipInfo


@dataclass
class Change:
    rel: str  # "webapp/rescue.py"
    src: Path  # файл новой версии во временной папке
    dst: Path  # файл в папке программы
    existed: bool  # заменяем прежний (копия — в update_backup) или добавляем новый
    exec_bit: bool  # в архиве помечен исполняемым (webui.sh, webui.command)


# ── Какой лаунчер и умеет ли он перезапускать ──


def launcher_name() -> str:
    """Чем человек запускает программу: его и просим запустить снова."""
    if IS_WINDOWS:
        return "webui.bat"
    return "webui.command" if sys.platform == "darwin" else "webui.sh"


def restart_mode() -> str:
    """auto — лаунчер сам запустит программу снова (код 75), manual — перезапускать человеку."""
    loops = os.environ.get(RESTART_ENV, "").strip() == str(RESTART_CODE)
    return "auto" if loops and _restart_hook is not None else "manual"


def set_restart_hook(hook, port: int | None = None) -> None:
    """webui.main(): чем остановить сервер и на каком он порту."""
    global _restart_hook, _port
    _restart_hook, _port = hook, port


# ── Что обновление не трогает ──


def is_protected(rel: str) -> bool:
    """Путь внутри папки программы ("webapp/rescue.py") — данные пользователя, а не программа.

    .env и .env.* (кроме .env.example), cookies*.json, cookies.json.tmp, upload_unlim_config.json,
    people_sorter_config.json, базы *.db* (с -wal, -shm, -journal), logs/, downloads/, .venv/,
    .uv/, .models/, .cache/, .git/, копии прежних версий, кеш Python/линтеров, наши временные файлы.
    """
    parts = [p.lower() for p in rel.replace("\\", "/").split("/") if p]
    if not parts:
        return True
    if any(p in PROTECTED_DIRS for p in parts):
        return True
    name = parts[-1]
    if name in _PROTECTED_FILES:
        return True
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if name.startswith("cookies") and name.endswith(".json"):
        return True
    if fnmatch.fnmatchcase(name, "*.db*"):
        return True
    return name.endswith((".pyc", ".pyo", STAGE_SUFFIX, RESTORE_SUFFIX))


def _download_rel(download_dir: str | None, project: Path) -> str | None:
    """Папка скачивания внутри папки программы — её путь от папки программы ("downloads").

    "" — это сама папка программы; None — папка скачивания где-то снаружи (или не задана).
    """
    if not download_dir or not str(download_dir).strip():
        return None
    try:
        rel = os.path.relpath(os.path.realpath(download_dir), os.path.realpath(project))
    except ValueError:  # другой диск в Windows
        return None
    if rel == os.curdir:
        return ""
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return rel.replace(os.sep, "/")


def _under(rel: str, prefix: str) -> bool:
    if CASE_INSENSITIVE_PATHS:
        rel, prefix = rel.lower(), prefix.lower()
    return rel == prefix or rel.startswith(prefix + "/")


# ── Шаг 1: скачать архив ──


def _allowed_url(url: str, extra_host: str | None) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return host in GITHUB_HOSTS or (extra_host is not None and host == extra_host)
    return parts.scheme == "http" and host in LOCAL_HOSTS and host == extra_host


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}".replace(".", ",") + " МБ"


def download_zip(url: str, dest: Path, progress=None, *, session=None, max_bytes: int = MAX_ZIP_BYTES,
                 deadline_sec: float = DOWNLOAD_DEADLINE_SEC, clock=time.monotonic) -> int:
    """Скачивает архив в dest и возвращает его размер; любая беда — UpdateError с понятным текстом.

    Только https с GitHub (адрес из YADISK_UPDATE_URL — тоже), каждое перенаправление
    проверяется заново. Не больше max_bytes и не дольше deadline_sec на весь архив.
    progress(получено, всего или None) — для строки «0,4 из 0,9 МБ».
    """
    extra = None if url == ZIP_URL else (urlsplit(url).hostname or "").lower() or None
    http = session or requests.Session()
    deadline = clock() + deadline_sec
    too_big = (f"Архив оказался больше {max_bytes // (1024 * 1024)} МБ — это не похоже на программу. "
               f"Обновление отменено. {UNCHANGED}")
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            if not _allowed_url(url, extra):
                raise UpdateError(f"Архив предлагают скачать с чужого адреса ({urlsplit(url).hostname or url}). "
                                  f"Обновление отменено. {UNCHANGED}")
            with http.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT, allow_redirects=False,
                          headers={"User-Agent": f"yadisk_backup/{utils.APP_VERSION}"}) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                if r.status_code != 200:
                    raise UpdateError(f"GitHub ответил ошибкой {r.status_code}. Попробуйте позже. {UNCHANGED}")
                try:
                    total = int(r.headers.get("Content-Length") or 0) or None
                except ValueError:
                    total = None
                if total and total > max_bytes:
                    raise UpdateError(too_big)
                got = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(64 * 1024):
                        if not chunk:
                            continue
                        got += len(chunk)
                        if got > max_bytes:
                            raise UpdateError(too_big)
                        if clock() > deadline:
                            raise UpdateError("GitHub отдаёт архив слишком медленно. Попробуйте позже. "
                                              + UNCHANGED)
                        f.write(chunk)
                        if progress:
                            progress(got, total)
                if not got:
                    raise UpdateError(f"GitHub прислал пустой ответ. Попробуйте позже. {UNCHANGED}")
                return got
        raise UpdateError(f"GitHub слишком долго перенаправлял на архив. Попробуйте позже. {UNCHANGED}")
    except UpdateError:
        raise
    except requests.exceptions.RequestException as e:
        log.warning("обновление: не скачалось: %s", e)
        raise UpdateError("Не удалось скачать новую версию: GitHub не ответил "
                          f"({e.__class__.__name__}). Проверьте интернет и попробуйте ещё раз. {UNCHANGED}") from None
    except OSError as e:
        raise UpdateError(f"Не удалось сохранить архив во временную папку ({_os_text(e)}). {UNCHANGED}") from None
    finally:
        if session is None:
            http.close()


# ── Шаг 2: проверить и распаковать ──


def _bad_part(part: str) -> bool:
    """Имя, которое нельзя создать в Windows или которое уводит из папки программы."""
    return (part in ("", ".", "..") or any(c in _WIN_BAD_CHARS or ord(c) < 32 for c in part)
            or part.endswith((" ", ".")) or part.split(".")[0].upper() in _WIN_RESERVED)


def check_archive(zip_path: Path, current: str | None = None) -> Release:
    """Это та программа и она новее нашей? Release — номер версии и файлы; иначе UpdateError.

    Всё лежит в yadisk_backup-main/, есть webui.py, utils.py, pyproject.toml, webapp/,
    APP_VERSION в utils.py новее current. Ссылок, зашифрованных файлов, путей наружу,
    недопустимых в Windows имён и имён, совпадающих без учёта регистра, нет.
    """
    current = utils.APP_VERSION if current is None else current
    broken = f"Скачанный файл повреждён или это не архив программы. Попробуйте ещё раз. {UNCHANGED}"
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError, ValueError):
        raise UpdateError(broken) from None
    prefix = ZIP_TOP + "/"
    with zf:
        infos = zf.infolist()
        if not infos or len(infos) > MAX_FILES:
            raise UpdateError(f"В архиве {len(infos)} файлов — это не похоже на программу. {UNCHANGED}")
        files: dict = {}
        seen: set[str] = set()
        total = 0
        for info in infos:
            name = info.filename
            if not name.startswith(prefix):
                raise UpdateError(f"В архиве не та программа: нет папки {ZIP_TOP}. {UNCHANGED}")
            rel = name[len(prefix):].rstrip("/")
            if not rel:
                continue
            if any(_bad_part(p) for p in rel.split("/")):
                raise UpdateError(f"В архиве есть файл с недопустимым именем ({rel[:80]}) — такой архив "
                                  f"программа не ставит. {UNCHANGED}")
            if info.is_dir():
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise UpdateError(f"В архиве есть ссылка на другой файл ({rel[:80]}) — такой архив программа "
                                  f"не ставит. {UNCHANGED}")
            if info.flag_bits & 0x1:
                raise UpdateError(f"В архиве есть зашифрованные файлы — это не архив программы. {UNCHANGED}")
            key = rel.lower()
            if key in seen:
                raise UpdateError(f"В архиве два файла с одним именем ({rel[:80]}). {UNCHANGED}")
            seen.add(key)
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise UpdateError(f"Распакованный архив больше {MAX_UNPACKED_BYTES // (1024 * 1024)} МБ — "
                                  f"это не похоже на программу. {UNCHANGED}")
            files[rel] = info
        missing = [f for f in REQUIRED_FILES if f not in files]
        if missing:
            raise UpdateError(f"В архиве не та программа: нет файла {missing[0]}. {UNCHANGED}")
        try:
            text = zf.read(files["utils.py"]).decode("utf-8")
        except (zipfile.BadZipFile, OSError, UnicodeDecodeError, EOFError):
            raise UpdateError(broken) from None
    m = _VERSION_LINE.search(text)
    parsed = updates.parse_version(m.group(1)) if m else None
    if parsed is None:
        raise UpdateError(f"В архиве нет номера версии — обновление отменено. {UNCHANGED}")
    version = "%d.%d.%d" % parsed
    if not updates.is_newer(version, current):
        raise UpdateError(f"На GitHub сейчас версия {version}, а у вас {current} — обновлять нечего.")
    return Release(version, files)


def extract_archive(zip_path: Path, release: Release, dest: Path) -> None:
    """Распаковывает проверенные файлы во временную папку dest (не в папку программы!)."""
    too_big = (f"Распакованный архив больше {MAX_UNPACKED_BYTES // (1024 * 1024)} МБ — "
               f"это не похоже на программу. {UNCHANGED}")
    total = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for rel, info in release.files.items():
                out = dest.joinpath(*rel.split("/"))
                out.parent.mkdir(parents=True, exist_ok=True)
                # Контрольную сумму zipfile сверяет в конце чтения — испорченный файл даст BadZipFile.
                # Считаем реальные распакованные байты: заголовок file_size в ZIP можно подделать.
                with zf.open(info) as src, open(to_long_path(str(out)), "wb") as dst:
                    while True:
                        chunk = src.read(min(256 * 1024, MAX_UNPACKED_BYTES - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_UNPACKED_BYTES:
                            raise UpdateError(too_big)
                        dst.write(chunk)
    except UpdateError:
        raise
    except (zipfile.BadZipFile, EOFError):
        raise UpdateError(f"Скачанный архив повреждён. Попробуйте ещё раз. {UNCHANGED}") from None
    except OSError as e:
        raise UpdateError(f"Не удалось распаковать архив во временную папку ({_os_text(e)}). {UNCHANGED}") from None


# ── Шаг 3: новая версия здесь вообще запускается? ──


def _deps_key(root: Path) -> bytes | None:
    """Зависимости программы и версия Python без её собственного номера версии: он меняется в каждом выпуске."""
    try:
        py = (root / "pyproject.toml").read_bytes().replace(b"\r\n", b"\n")
        lock = (root / "uv.lock").read_bytes().replace(b"\r\n", b"\n") if (root / "uv.lock").exists() else b""
        pyver_path = root / ".python-version"
        pyver = pyver_path.read_bytes().replace(b"\r\n", b"\n").strip() if pyver_path.exists() else b""
    except OSError:
        return None
    py = re.sub(rb'(?m)^version\s*=\s*"[^"\n]*"', b'version = ""', py)
    lock = re.sub(rb'(name = "yadisk-backup"\nversion = )"[^"\n]*"', rb'\1""', lock)
    return py + b"\0" + lock + b"\0" + pyver


def deps_changed(new_root: Path, project: Path) -> bool:
    """Новой версии нужны другие библиотеки (pyproject.toml, uv.lock или .python-version не те)."""
    new, old = _deps_key(new_root), _deps_key(project)
    return new is None or old is None or new != old


_IMPORT_CHECK = "import sys; sys.path.insert(0, sys.argv[1]); import webui; print(webui.APP_VERSION)"


def _tail(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else "без подробностей")[:200]


def _clean_env(*drop: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", *drop)}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", YADISK_NO_BROWSER="1")
    return env


def _import_check(python: str, src: Path) -> None:
    """Новая версия импортируется: синтаксис, файлы на месте, библиотеки подходят."""
    try:
        r = subprocess.run([python, "-I", "-X", "utf8", "-c", _IMPORT_CHECK, str(src)], cwd=str(src), env=_clean_env(),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UpdateError(f"Не удалось проверить новую версию ({e.__class__.__name__}). {UNCHANGED}") from None
    if r.returncode != 0:
        log.warning("обновление: новая версия не импортируется:\n%s", (r.stderr or "")[-3000:])
        raise UpdateError(f"Новая версия не запускается на этом компьютере ({_tail(r.stderr)}). "
                          f"{UNCHANGED} {ASK_AUTHOR}")


def _find_uv() -> str | None:
    """Находит uv: из переменной UV (относительный путь — от папки программы), PATH, .uv/ или ~/.local/bin."""
    env_uv = (os.environ.get("UV") or "").strip()
    if env_uv:
        p = Path(env_uv)
        if p.is_absolute():
            return str(p)
        resolved = paths.PROJECT / p
        if resolved.exists() or "/" in env_uv or "\\" in env_uv:
            return str(resolved)
        return shutil.which(env_uv) or str(resolved)
    found = shutil.which("uv")
    if found:
        return found
    exe = "uv.exe" if IS_WINDOWS else "uv"
    for candidate in (paths.PROJECT / ".uv" / exe, Path.home() / ".local" / "bin" / exe):
        if candidate.is_file():
            return str(candidate)
    return None


def _optional_extras(pyproject_path: Path) -> set[str]:
    """Имена optional-dependencies в pyproject.toml (работает и на Python 3.10 без tomllib)."""
    try:
        text = pyproject_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        import tomllib
        data = tomllib.loads(text)
        opt = data.get("project", {}).get("optional-dependencies", {})
        return set(opt.keys()) if isinstance(opt, dict) else set()
    except ImportError:
        pass
    except ValueError:
        return set()
    extras: set[str] = set()
    in_opt = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            in_opt = stripped == "[project.optional-dependencies]"
            continue
        if in_opt:
            m = re.match(r"^([A-Za-z0-9_-]+)\s*=", stripped)
            if m:
                extras.add(m.group(1))
    return extras


def _target_python(src: Path) -> str:
    """Версия Python из src/.python-version, если задана, иначе текущий sys.executable."""
    pyver_file = src / ".python-version"
    if pyver_file.is_file():
        try:
            lines = [ln.strip() for ln in pyver_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                return lines[0]
        except OSError:
            pass
    return sys.executable


def _prepare_deps(src: Path) -> str | None:
    """Новые библиотеки — заранее, во временную папку: заодно они лягут в кеш uv, и перезапуск
    поставит их без интернета. Возвращает python временного окружения; None — uv не найден
    (тогда библиотеки поставит перезапуск).

    Пробует до трёх уровней в рамках общего лимита времени (как лаунчеры v3.0):
    1) --extra people --python <py_ver> (если people есть в pyproject.toml)
    2) --extra people-legacy --python 3.12 (если people-legacy есть в pyproject.toml)
    3) базовое ядро --python <py_ver>
    """
    uv = _find_uv()
    if not uv:
        log.info("обновление: uv не найден — новые библиотеки поставит перезапуск")
        return None
    py_ver = _target_python(src)
    extras = _optional_extras(src / "pyproject.toml")
    tiers: list[tuple[str, list[str]]] = []
    if "people" in extras:
        tiers.append(("people", ["--extra", "people", "--python", py_ver]))
    if "people-legacy" in extras:
        tiers.append(("people-legacy", ["--extra", "people-legacy", "--python", "3.12"]))
    tiers.append(("core", ["--python", py_ver]))

    deadline = time.monotonic() + PREPARE_TIMEOUT_SEC
    env = _clean_env("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")
    for idx, (label, extra_args) in enumerate(tiers):
        is_last = idx == len(tiers) - 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UpdateError("Новой версии нужны новые компоненты, а поставить их не удалось "
                              f"(TimeoutExpired). Проверьте интернет и попробуйте ещё раз. {UNCHANGED}")
        cmd = [uv, "sync", "--frozen", "--no-dev", "--no-progress", "--project", str(src), *extra_args]
        try:
            r = subprocess.run(cmd, cwd=str(src), env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=remaining)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise UpdateError("Новой версии нужны новые компоненты, а поставить их не удалось "
                              f"({e.__class__.__name__}). Проверьте интернет и попробуйте ещё раз. "
                              f"{UNCHANGED}") from None
        if r.returncode == 0:
            venv_python = src / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
            return str(venv_python) if venv_python.exists() else None
        err_tail = _tail(r.stderr)
        if not is_last:
            log.info("обновление: uv sync (%s) не удался (%s) — пробую следующий вариант", label, err_tail)
            continue
        log.warning("обновление: uv sync не удался:\n%s", (r.stderr or "")[-3000:])
        raise UpdateError("Новой версии нужны новые компоненты, а скачать их не удалось "
                          f"({err_tail}). Проверьте интернет и попробуйте ещё раз. {UNCHANGED}")
    return None


def smoke_test(src: Path, release: Release, project: Path) -> None:
    """Новая версия хотя бы запускается на этом компьютере. Иначе UpdateError — до любой замены."""
    for rel in sorted(release.files):
        if not rel.endswith(".py"):
            continue
        try:
            compile((src / rel).read_bytes(), rel, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as e:
            where = f"{rel}, строка {e.lineno}" if getattr(e, "lineno", None) else rel
            raise UpdateError(f"Новая версия не запускается на этом компьютере: ошибка в {where}. "
                              f"{UNCHANGED} {ASK_AUTHOR}") from None
        except OSError as e:
            raise UpdateError(f"Не удалось прочитать распакованный файл ({_os_text(e)}). {UNCHANGED}") from None
    python: str | None = sys.executable
    if deps_changed(src, project):
        _step("prepare", "Ставлю новые компоненты программы")
        python = _prepare_deps(src)
    if python:
        _import_check(python, src)


# ── Шаг 4: копия, новые файлы рядом, переименования; ошибка — откат ──


def _lp(path: Path) -> str:
    """Путь для файловых операций: длинные пути Windows — с префиксом \\\\?\\."""
    return to_long_path(str(path))


def _os_text(e: OSError) -> str:
    text = e.strerror or e.__class__.__name__
    name = getattr(e, "filename", None)
    return f"{text}: {os.path.basename(str(name))}" if name else text


def _err_text(e: BaseException) -> str:
    return _os_text(e) if isinstance(e, OSError) else e.__class__.__name__


def _say(text: str) -> None:
    """Строка в чёрное окно программы: там человек и будет перезапускать её."""
    try:
        print(text, flush=True)
    except (OSError, ValueError):  # окна нет или вывод закрыт
        pass


def plan_install(src: Path, release: Release, project: Path,
                 download_dir: str | None = None) -> tuple[list[Change], list[str]]:
    """Что заменить и что добавить. Второй список — пропущенные пути (данные пользователя).

    Файлы, которые уже совпадают с новыми, не трогаем. Папка программы со ссылками наружу
    или папка вместо файла — отказ до любых изменений.
    """
    dl_rel = _download_rel(download_dir, project)
    if dl_rel == "":
        raise UpdateError("Папка для скачанных файлов — это сама папка программы, и обновлять её кнопкой "
                          f"небезопасно. Выберите в «Настройках» отдельную папку для файлов. {UNCHANGED}")
    root = os.path.normcase(os.path.realpath(project))
    changes: list[Change] = []
    skipped: list[str] = []
    for rel in sorted(release.files):
        if is_protected(rel) or (dl_rel is not None and _under(rel, dl_rel)):
            skipped.append(rel)
            continue
        dst = project.joinpath(*rel.split("/"))
        real = os.path.normcase(os.path.realpath(dst))
        if os.path.islink(dst) or not (real == root or real.startswith(root.rstrip(os.sep) + os.sep)):
            raise UpdateError(f"В папке программы {rel} — ссылка на другую папку или файл. Обновлять кнопкой "
                              f"небезопасно — обновите вручную. {UNCHANGED}")
        if dst.is_dir():
            raise UpdateError(f"В папке программы вместо файла {rel} лежит папка. Обновлять кнопкой "
                              f"небезопасно — обновите вручную. {UNCHANGED}")
        new = src.joinpath(*rel.split("/"))
        exec_bit = bool((release.files[rel].external_attr >> 16) & 0o111)
        existed = dst.exists()
        if existed and _same_file(new, dst, exec_bit):
            continue
        changes.append(Change(rel, new, dst, existed, exec_bit))
    return changes, skipped


def _same_file(new: Path, old: Path, exec_bit: bool) -> bool:
    try:
        if new.stat().st_size != old.stat().st_size:
            return False
        if not IS_WINDOWS and exec_bit and not os.access(old, os.X_OK):
            return False
        return new.read_bytes() == old.read_bytes()
    except OSError:
        return False


def _copy_file(src: Path, dst: Path, *, exec_bit: bool = False, like: Path | None = None) -> None:
    """Копирует содержимое и сбрасывает его на диск; права — как у прежнего файла, +x из архива."""
    with open(_lp(src), "rb") as fin, open(_lp(dst), "wb") as fout:
        shutil.copyfileobj(fin, fout, 256 * 1024)
        fout.flush()
        try:
            os.fsync(fout.fileno())  # иначе при отключении питания файл мог бы остаться пустым
        except OSError:
            pass  # не на всех дисках можно — не повод отменять обновление
    if not IS_WINDOWS:
        mode = stat.S_IMODE(os.stat(like).st_mode) if like is not None else stat.S_IMODE(os.stat(dst).st_mode)
        if exec_bit:
            mode |= (mode & 0o444) >> 2  # исполнять может тот, кто может читать
        os.chmod(dst, mode)


def _replace(src: Path, dst: Path, tries: int = 10) -> None:
    """os.replace с повторами: антивирус или индексатор Windows держат файл долю секунды."""
    for attempt in range(tries):
        try:
            os.replace(_lp(src), _lp(dst))
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            if IS_WINDOWS and os.path.exists(_lp(dst)) and not os.access(_lp(dst), os.W_OK):
                try:
                    os.chmod(_lp(dst), stat.S_IWRITE | stat.S_IREAD)  # «только чтение» мешает замене
                except OSError:
                    pass
            time.sleep(0.2)


def _remove(path: Path) -> None:
    try:
        os.remove(_lp(path))
    except FileNotFoundError:
        pass


def _missing_dirs(folder: Path, project: Path) -> list[Path]:
    """Папки, которых ещё нет на пути к folder (от верхней к нижней)."""
    out: list[Path] = []
    while folder != project and not folder.exists():
        out.append(folder)
        folder = folder.parent
    return list(reversed(out))


def _write_manifest(backup: Path, state: str, info: dict) -> None:
    info["state"] = state
    try:
        (backup / "update.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        log.warning("обновление: не записал update.json: %s", e)


_BACKUP_README = """Копия программы до обновления

Здесь лежат файлы версии {old}, которые программа заменила, когда обновлялась до версии {new}
({when}). Ваши скачанные файлы, ключ, данные входа и настройки обновление не трогает — их здесь нет.

Если новая версия не работает: закройте окно программы, скопируйте всё из этой папки, кроме
update.json и этого файла, в папку программы с заменой файлов и запустите программу снова.
И напишите автору в Telegram: {telegram}
"""


def _unique_backup_path(root: Path, version: str, stamp: str | None = None) -> Path:
    """Уникальное имя папки бэкапа: при повторном запуске в ту же секунду добавляет -2, -3, ..."""
    base = f"{version}_{stamp or time.strftime('%Y%m%d-%H%M%S')}"
    cand = root / base
    if not cand.exists():
        return cand
    for n in range(2, 1000):
        cand = root / f"{base}-{n}"
        if not cand.exists():
            return cand
    return root / f"{base}-{int(time.time() * 1000) % 1000000}"


def install(changes: list[Change], project: Path, backup: Path, *, old: str, new: str,
            on_step=None, on_progress=None) -> None:
    """Ставит файлы новой версии. Любая ошибка — всё как было и UpdateError.

    1) копия каждого заменяемого файла в backup; 2) новые файлы рядом с прежними (.yadisk-new);
    3) os.replace одного за другим. Пока не начался шаг 3, папка программы не меняется вовсе;
    ошибка на шаге 3 возвращает заменённые файлы из копии и убирает добавленные.
    """
    on_step = on_step or (lambda step: None)
    on_progress = on_progress or (lambda done, total: None)
    info = {"from": old, "to": new, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "replaced": [c.rel for c in changes if c.existed], "created": [c.rel for c in changes if not c.existed]}
    on_step("backup")
    made_backup = False
    try:
        backup.mkdir(parents=True, exist_ok=False)
        made_backup = True
        (backup / "README.txt").write_text(_BACKUP_README.format(
            old=old, new=new, when=info["started_at"], telegram=TELEGRAM_CHANNEL), encoding="utf-8")
        for c in changes:
            if c.existed:
                copy = backup.joinpath(*c.rel.split("/"))
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(_lp(c.dst), _lp(copy))
        _write_manifest(backup, "backup", info)
    except Exception as e:  # любая ошибка здесь: в папке программы ещё ничего не менялось
        if made_backup:  # чужую папку (вдруг такая уже была) не удаляем
            shutil.rmtree(_lp(backup), ignore_errors=True)
        log.warning("обновление: копия не сохранилась: %r", e)
        raise UpdateError(f"Не удалось сохранить копию нынешней версии ({_err_text(e)}). {UNCHANGED}") from None

    on_step("install")
    staged: list[Path] = []
    new_dirs: list[Path] = []
    try:
        for i, c in enumerate(changes, 1):
            for folder in _missing_dirs(c.dst.parent, project):
                folder.mkdir()
                new_dirs.append(folder)
            tmp = c.dst.with_name(c.dst.name + STAGE_SUFFIX)
            staged.append(tmp)
            _copy_file(c.src, tmp, exec_bit=c.exec_bit, like=c.dst if c.existed else None)
            on_progress(i, len(changes))
    except Exception as e:
        _cleanup(staged, new_dirs)
        shutil.rmtree(_lp(backup), ignore_errors=True)
        log.warning("обновление: новые файлы не записались: %r", e)
        raise UpdateError(f"Не удалось записать новые файлы ({_err_text(e)}). {UNCHANGED}") from None

    _write_manifest(backup, "committing", info)
    done: list[Change] = []
    for c in changes:
        try:
            _replace(c.dst.with_name(c.dst.name + STAGE_SUFFIX), c.dst)
        except Exception as e:
            log.error("обновление: не заменился %s: %r — откатываю", c.rel, e)
            _cleanup(staged, [])  # сначала недоставленные .yadisk-new: иначе новые папки не опустеют
            failed = rollback(done, backup, new_dirs)
            # Имя файла уже в тексте; в ошибке os.replace — временное имя .yadisk-new, оно только путает
            detail = (e.strerror or e.__class__.__name__) if isinstance(e, OSError) else e.__class__.__name__
            if failed:
                info["failed"] = failed
                _write_manifest(backup, "rollback_failed", info)
                where = backup.relative_to(project).as_posix() if backup.is_relative_to(project) else str(backup)
                raise UpdateError(
                    f"Не удалось заменить файл {c.rel} ({detail}), и вернуть всё как было тоже не вышло "
                    f"({len(failed)} из {len(done)} файлов). Прежние файлы лежат в папке {where}: закройте окно "
                    f"программы, скопируйте их в папку программы с заменой и запустите программу снова. "
                    f"{ASK_AUTHOR}", rolled_back=False) from None
            shutil.rmtree(_lp(backup), ignore_errors=True)  # копия совпадает с тем, что вернули
            try:
                backup.parent.rmdir()  # update_backup, если там больше ничего нет
            except OSError:
                pass
            raise UpdateError(f"Не удалось заменить файл {c.rel} ({detail}) — возможно, его держит другая "
                              f"программа или антивирус. Всё вернул как было, работает прежняя версия. "
                              f"Попробуйте ещё раз чуть позже.") from None
        done.append(c)
    _write_manifest(backup, "done", info)


def rollback(done: list[Change], backup: Path, new_dirs: list[Path]) -> list[str]:
    """Возвращает заменённые файлы из копии и убирает добавленные. Возвращает то, что не вышло."""
    failed: list[str] = []
    for c in reversed(done):
        tmp = c.dst.with_name(c.dst.name + RESTORE_SUFFIX)
        try:
            if c.existed:
                shutil.copy2(_lp(backup.joinpath(*c.rel.split("/"))), _lp(tmp))
                _replace(tmp, c.dst)
            else:
                _remove(c.dst)
        except OSError as e:
            log.error("обновление: откат %s не удался: %s", c.rel, e)
            failed.append(c.rel)
            try:
                _remove(tmp)
            except OSError:
                pass
    for folder in reversed(new_dirs):
        try:
            folder.rmdir()
        except OSError:
            pass
    return failed


def _cleanup(staged: list[Path], new_dirs: list[Path]) -> None:
    """Убирает недоставленные новые файлы (.yadisk-new) и созданные для них пустые папки."""
    for tmp in staged:
        try:
            _remove(tmp)
        except OSError:
            pass
    for folder in reversed(new_dirs):
        try:
            folder.rmdir()
        except OSError:
            pass


def _backup_sort_key(d: Path) -> tuple[str, int]:
    tail = d.name.split("_", 1)[1]
    parts = tail.split("-")
    return (f"{parts[0]}-{parts[1]}", int(parts[2]) if len(parts) > 2 else 1)


def prune_backups(root: Path, keep: int = KEEP_BACKUPS) -> None:
    """Оставляет keep последних копий прежних версий; чужие папки не трогает. Не бросает."""
    try:
        dirs = [d for d in root.iterdir() if _BACKUP_NAME.match(d.name) and d.is_dir() and not d.is_symlink()]
    except OSError:
        return
    dirs.sort(key=_backup_sort_key)
    for d in dirs[:-keep] if keep > 0 else dirs:
        shutil.rmtree(_lp(d), ignore_errors=True)


# ── Обновление целиком: фоновый поток и его состояние для интерфейса ──


def _new_job() -> dict:
    return {"state": "running", "step": "download", "text": STEP_TEXT["download"], "detail": None,
            "error": None, "rolled_back": True, "from": utils.APP_VERSION, "to": None,
            "restart": restart_mode(), "launcher": launcher_name(), "backup": None,
            "started_at": time.time(), "finished_at": None, "steps": [[k, v] for k, v in STEP_TEXT.items()]}


def _set(**fields) -> None:
    with _lock:
        if _job is not None:
            _job.update(fields)


def _step(step: str, text: str | None = None) -> None:
    _set(step=step, text=text or STEP_TEXT[step], detail=None)
    log.info("обновление: %s", text or STEP_TEXT[step])


def job_status() -> dict | None:
    """/api/status.update_job: как идёт обновление, запущенное в этом запуске программы, или None."""
    with _lock:
        return dict(_job) if _job is not None else None


def _upload_running() -> bool:
    from uploader.webapp import manager as up_mgr
    return bool(up_mgr.manager.is_running())


def _people_running() -> bool:
    from webapp import people
    return people.manager is not None and bool(people.manager.is_running())


def busy_text(
    alive: list[str],
    rescue_running: bool = False,
    upload_running: bool = False,
    people_running: bool = False,
) -> str | None:
    """Почему сейчас нельзя обновляться (что-то качается, загружается или сортируется); None — можно."""
    if rescue_running:
        return STOP_FIRST
    if upload_running:
        return UPLOAD_FIRST
    if people_running:
        return PEOPLE_FIRST
    if SLOT_LINKS in alive:
        return ("Сначала остановите скачивание по ссылкам: раздел «По ссылке» → «Остановить». "
                "Скачанное сохранится.")
    if SLOT_ORGANIZE in alive:
        return "Сначала дождитесь конца сортировки или остановите её в разделе «Сортировка»."
    if any(str(getattr(tm.get(slot), "kind", "")).endswith("_verify") for slot in alive):
        return VERIFY_FIRST
    return STOP_FIRST if alive else None


def install_info(cfg: dict | None = None) -> dict:
    """Можно ли обновиться кнопкой: {ok, why, busy, restart, launcher} для /api/status.update.install."""
    project = paths.PROJECT
    why = None
    if (project / ".git").exists():
        why = "Программа запущена из копии git — обновите её командой git pull."
    else:
        download_dir = (cfg or {}).get("download_dir")
        if _download_rel(download_dir, project) == "":
            why = ("Папка для скачанных файлов — это сама папка программы, поэтому обновить её кнопкой нельзя. "
                   "Выберите в «Настройках» отдельную папку для файлов или обновите программу вручную.")
    busy = busy_text(tm.alive_slots(), rescue.is_running(), _upload_running(), _people_running())
    return {"ok": why is None, "why": why, "busy": busy,
            "restart": restart_mode(), "launcher": launcher_name()}


def restart_text(launcher: str | None = None) -> str:
    return (f"Программа обновлена — перезапустите её: закройте чёрное окно программы и запустите "
            f"{launcher or launcher_name()} снова")


def start_install() -> tuple[bool, str]:
    """Кнопка «Обновить программу»: проверяет, можно ли, и запускает обновление в фоне."""
    global _job, _thread
    with _lock:
        state = _job["state"] if _job else None
        if state in ("running", "restarting"):
            return False, "Обновление уже идёт"
        if state == "restart_manual":
            return False, restart_text(_job["launcher"])
        if state == "failed" and not _job["rolled_back"]:
            return False, "Прошлое обновление не удалось откатить — сначала верните прежние файлы из копии"
        cfg = config.get_config()
        info = install_info(cfg)
        if not info["ok"]:
            return False, info["why"]
        pre_busy = busy_text([], rescue.is_running(), _upload_running(), _people_running())
        if pre_busy:
            return False, pre_busy
        alive = tm.freeze(FROZEN_TEXT)
        if alive:
            return False, busy_text(alive)
        post_busy = busy_text([], rescue.is_running(), _upload_running(), _people_running())
        if post_busy:  # спасение, загрузка или «По людям» запустились между проверками
            tm.unfreeze()
            return False, post_busy
        url = os.environ.get(URL_ENV, "").strip() or ZIP_URL
        _job = _new_job()
        try:
            _thread = threading.Thread(target=_run, args=(url, cfg.get("download_dir")),
                                       name="self-update", daemon=True)
            _thread.start()
        except RuntimeError:
            _job = None
            tm.unfreeze()
            return False, "Не получилось начать обновление — попробуйте ещё раз"
    log.info("обновление: начинаю (у нас %s, архив %s)", utils.APP_VERSION, url)
    return True, "Обновляю программу — это займёт около минуты"


def _download_progress(got: int, total: int | None) -> None:
    _set(detail=f"{_mb(got)} из {_mb(total)}" if total else _mb(got))


def _check_writable(project: Path) -> None:
    """В папку программы можно писать — иначе отказ сразу, а не на середине."""
    try:
        with tempfile.TemporaryFile(dir=project):
            pass
    except OSError as e:
        raise UpdateError(f"Нет прав на запись в папку программы ({_os_text(e)}), поэтому обновить её кнопкой "
                          "нельзя. Перенесите папку программы, например, в «Документы» и запустите её оттуда — "
                          f"или обновите вручную. {UNCHANGED}") from None


def _run(url: str, download_dir: str | None) -> None:
    tmp: Path | None = None
    try:
        project = paths.PROJECT
        _check_writable(project)
        try:
            tmp = Path(tempfile.mkdtemp(prefix="yadisk-update-"))
        except OSError as e:
            raise UpdateError(f"Не удалось создать временную папку для архива ({_os_text(e)}). {UNCHANGED}") from None
        zip_path = tmp / "main.zip"
        size = download_zip(url, zip_path, progress=_download_progress)
        log.info("обновление: архив скачан, %d байт", size)
        _step("check")
        release = check_archive(zip_path)
        _set(to=release.version)
        src = tmp / "new"
        extract_archive(zip_path, release, src)
        changes, skipped = plan_install(src, release, project, download_dir)
        if skipped:
            log.info("обновление: не трогаю данные пользователя: %s", ", ".join(skipped[:20]))
        _step("prepare")
        smoke_test(src, release, project)
        backup = _unique_backup_path(project / BACKUP_DIRNAME, utils.APP_VERSION)
        _set(backup=f"{BACKUP_DIRNAME}/{backup.name}")
        install(changes, project, backup, old=utils.APP_VERSION, new=release.version, on_step=_step,
                on_progress=lambda done, total: _set(detail=f"{done} из {total}"))
    except UpdateError as e:
        _fail(e.text, e.rolled_back)
        return
    except Exception as e:  # install() откатывает сам: сюда доходят только ошибки до замены файлов
        log.exception("обновление: непредвиденная ошибка")
        _fail(f"Непредвиденная ошибка ({e.__class__.__name__}). {UNCHANGED} {ASK_AUTHOR}", True)
        return
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    log.info("обновление: поставлено %s, версия %s", utils.files_text(len(changes)), release.version)
    prune_backups(project / BACKUP_DIRNAME)
    _installed(release.version, f"{BACKUP_DIRNAME}/{backup.name}")


def _fail(text: str, rolled_back: bool) -> None:
    _set(state="failed", error=text, rolled_back=rolled_back, detail=None, finished_at=time.time())
    log.warning("обновление не удалось: %s", text)
    if rolled_back:
        tm.unfreeze()
    else:
        tm.freeze(text)  # программа в папке перемешана: новые задачи запускать нельзя


def _installed(version: str, backup: str) -> None:
    """Файлы на месте: перезапуск лаунчером или просьба перезапустить вручную."""
    mode = restart_mode()
    _write_marker(version, backup, mode)
    launcher = launcher_name()
    if mode == "auto":
        _set(state="restarting", step="restart", text=STEP_TEXT["restart"], detail=None)
        _say(f"\n  Программа обновлена до версии {version} — перезапускаю её…\n")
        timer = threading.Timer(RESTART_DELAY_SEC, _exit_for_restart)
        timer.daemon = True
        timer.start()
        return
    _set(state="restart_manual", step="restart", text="Перезапустите программу", detail=None,
         finished_at=time.time())
    tm.freeze(restart_text(launcher))
    _say(f"\n  Программа обновлена до версии {version}.\n"
         f"  Закройте это окно и запустите {launcher} снова — тогда заработает новая версия.\n")


def _exit_for_restart() -> None:
    global exit_code, _fallback
    exit_code = RESTART_CODE
    log.info("обновление: останавливаю сервер, лаунчер запустит новую версию (код %d)", RESTART_CODE)
    hook = _restart_hook
    if hook is not None:
        hook()
    _fallback = threading.Timer(EXIT_FALLBACK_SEC, _hard_exit)
    _fallback.daemon = True
    _fallback.start()


def _hard_exit() -> None:
    """Сервер не остановился сам (завис запрос) — выходим, чтобы лаунчер запустил новую версию."""
    logging.shutdown()
    os._exit(RESTART_CODE)


def wait_for_files(timeout: float = 60.0) -> None:
    """Сервер останавливают (Ctrl+C), а обновление как раз меняет файлы — даём ему закончить."""
    t = _thread
    job = job_status()
    if t is not None and t.is_alive() and job and job.get("step") in ("backup", "install"):
        _say("  Дожидаюсь конца обновления, чтобы не оставить программу наполовину заменённой…")
        t.join(timeout)


# ── Метка «программу только что обновили» ──


def _marker_path() -> Path:
    return paths.LOG_DIR / MARKER_NAME


def _write_marker(version: str, backup: str, mode: str) -> None:
    data = {"from": utils.APP_VERSION, "to": version, "at": time.time(), "port": _port, "restart": mode,
            "backup": backup}
    try:
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        _marker_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("обновление: не записал метку перезапуска: %s", e)


def take_restart_marker(now: float | None = None) -> dict | None:
    """При запуске: эту версию только что поставило обновление? Сведения из метки или None.

    Метка удаляется в любом случае; старая (дольше MARKER_FRESH_SEC) или от другой версии не считается.
    """
    global _updated
    path = _marker_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    try:
        path.unlink()
    except OSError:
        pass
    now = time.time() if now is None else now
    if not isinstance(data, dict) or not isinstance(data.get("at"), (int, float)):
        return None
    if not 0 <= now - data["at"] <= MARKER_FRESH_SEC:
        return None
    if data.get("to") != utils.APP_VERSION:
        log.warning("обновление: метка про версию %s, а запущена %s", data.get("to"), utils.APP_VERSION)
        return None
    _updated = {"from": str(data.get("from") or ""), "to": utils.APP_VERSION, "at": now,
                "backup": data.get("backup")}
    log.info("запущена новая версия %s (была %s)", utils.APP_VERSION, _updated["from"])
    return data


def updated_info(now: float | None = None) -> dict | None:
    """/api/status.updated: «Программа обновлена до версии …» — первые минуты после перезапуска."""
    now = time.time() if now is None else now
    u = _updated
    return dict(u) if u and now - u["at"] < UPDATED_SHOW_SEC else None


# ── Демо-состояния: шаги обновления без GitHub ──


def demo_install() -> dict:
    return {"ok": True, "why": None, "busy": None, "restart": "auto", "launcher": "webui.bat"}


def demo_updated() -> dict:
    major, minor, patch = updates.parse_version(utils.APP_VERSION) or (0, 0, 0)
    before = f"{major}.{minor}.{patch - 1}" if patch else f"{major}.{max(minor - 1, 0)}.0"
    return {"from": before, "to": utils.APP_VERSION, "at": time.time(),
            "backup": f"{BACKUP_DIRNAME}/{before}_20261019-213005"}


def demo_job(kind: str) -> dict | None:
    current = utils.APP_VERSION
    latest = updates.demo(current)["latest"]
    job = dict(_new_job(), restart="auto", launcher="webui.bat", to=latest,
               backup=f"{BACKUP_DIRNAME}/{current}_20261019-213005")
    if kind == "update_running":
        job.update(step="install", text=STEP_TEXT["install"], detail="18 из 42")
    elif kind == "update_manual":
        job.update(state="restart_manual", step="restart", text="Перезапустите программу", restart="manual")
    elif kind == "update_failed":
        job.update(state="failed", step="install", error=(
            "Не удалось заменить файл webapp/rescue.py (Отказано в доступе) — возможно, его держит другая "
            "программа или антивирус. Всё вернул как было, работает прежняя версия. Попробуйте ещё раз чуть позже."))
    else:
        return None
    return job


# ── Адрес ──


@router.post("/api/update/install")
def api_update_install():
    """«Обновить программу»: скачать новую версию с GitHub, поставить и перезапуститься."""
    ok, msg = start_install()
    return {"ok": ok, "msg": msg}
