"""Веб-дашборд для бэкапа Яндекс.Диска.

Запуск:
  python webui.py
  → http://localhost:8000

Что умеет:
  - Сохранить токен в .env через UI
  - Сохранить cookies.json через UI (для безлимита)
  - Запуск scan / download / verify / retry для обычного диска
  - Запуск collect / worker для photoslice (безлимита)
  - Запуск organize (сортировка по полочкам)
  - Live прогресс через polling
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv, set_key
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from utils import sqlite_ro_uri

sys.stdout.reconfigure(encoding="utf-8")

PROJECT = Path(__file__).resolve().parent
ENV_FILE = PROJECT / ".env"
load_dotenv(ENV_FILE)

DB_FILE = PROJECT / "state.db"
UNLIM_DB_FILE = PROJECT / "unlim_state.db"
COOKIES_FILE = PROJECT / "cookies.json"
TEMPLATES_DIR = PROJECT / "webui_assets"


def get_config() -> dict:
    """Перечитывает .env заново."""
    load_dotenv(ENV_FILE, override=True)
    return {
        "token": (os.getenv("YA_DISK_TOKEN", "") or "").strip(),
        "download_dir": (os.getenv("DOWNLOAD_DIR", str(PROJECT / "downloads")) or "").strip(),
        "max_workers": int(os.getenv("MAX_WORKERS", "4") or "4"),
    }


def mask_token(token: str) -> str:
    """Маскирует токен для отображения: y0_xxxx****xxxx"""
    if not token or len(token) < 8:
        return ""
    if len(token) <= 12:
        return token[:3] + "***"
    return f"{token[:6]}…{token[-4:]} ({len(token)} симв.)"


app = FastAPI(title="Yandex.Disk Backup UI")


# ── CSRF защита: блокируем POST с чужих сайтов ─────────────────────────────

_ALLOWED_ORIGINS = ("http://localhost:8000", "http://127.0.0.1:8000")


@app.middleware("http")
async def csrf_guard(request, call_next):
    """Блокирует state-changing запросы с явно чужих сайтов (CSRF).
    GET-запросы пропускаем всегда — они не меняют состояние.
    """
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin") or ""
        # Блокируем только если Origin явно с чужого сайта
        if origin and not any(origin.startswith(a) for a in _ALLOWED_ORIGINS):
            return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
    return await call_next(request)


# ── Категории файлов ────────────────────────────────────────────────────────

CATEGORIES = {
    "photos": {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif",
               ".bmp", ".tiff", ".tif", ".raw", ".arw", ".cr2", ".nef",
               ".dng", ".orf"},
    "videos": {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".3gp", ".3g2",
               ".mpg", ".mpeg", ".wmv", ".flv", ".vob", ".mts", ".m2ts"},
    "audio": {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".aac", ".wma",
              ".opus", ".aiff", ".alac"},
    "documents": {".doc", ".docx", ".pdf", ".txt", ".rtf", ".odt",
                  ".xls", ".xlsx", ".ods", ".csv",
                  ".ppt", ".pptx", ".odp"},
    "archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
                 ".cab", ".tgz", ".tbz"},
    "programs": {".exe", ".dll", ".msi", ".bat", ".cmd", ".sys", ".com",
                 ".scr", ".vbs", ".ps1", ".sh", ".jar"},
}

CATEGORY_LABELS = {
    "photos": "Фото",
    "videos": "Видео",
    "audio": "Аудио",
    "documents": "Документы",
    "archives": "Архивы",
    "programs": "Программы",
    "other": "Прочее",
}


def categorize(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "other"


# ── Менеджер фоновых процессов ──────────────────────────────────────────────


class Task:
    def __init__(self, name: str, proc: subprocess.Popen):
        self.name = name
        self.proc = proc
        self.started_at = time.time()
        self.log: deque[str] = deque(maxlen=300)
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self):
        try:
            for line in self.proc.stdout:  # type: ignore
                line = line.rstrip("\r\n")
                if line:
                    self.log.append(line)
        except Exception:
            pass
        finally:
            # Забираем код возврата, иначе на Linux/macOS остаётся зомби-процесс
            try:
                self.proc.wait(timeout=10)
            except Exception:
                pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self):
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, Task] = {}  # ключ — название/слот
        self._lock = threading.Lock()

    def start(self, slot: str, script: str, args: list[str]) -> tuple[bool, str]:
        """slot — логическое имя ('disk', 'photoslice_collect', 'worker-1', ...).
        Можно одновременно несколько задач в разных слотах.
        """
        with self._lock:
            existing = self._tasks.get(slot)
            if existing and existing.alive():
                return False, f"Уже идёт: {slot}"
            # Чистка зомби — если предыдущая задача в слоте умерла, закроем pipe и забудем
            if existing and not existing.alive():
                # Сначала дождёмся, что reader-тред допишет последние строки
                try:
                    existing._t.join(timeout=1.0)
                except Exception:
                    pass
                try:
                    if existing.proc.stdout:
                        existing.proc.stdout.close()
                except Exception:
                    pass
                del self._tasks[slot]
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            cmd = [sys.executable, "-u", script] + args
            proc = subprocess.Popen(
                cmd, cwd=str(PROJECT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env,
            )
            self._tasks[slot] = Task(slot, proc)
            return True, f"Запущено: {slot}"

    def stop(self, slot: str) -> tuple[bool, str]:
        with self._lock:
            t = self._tasks.get(slot)
            if not t or not t.alive():
                return False, f"Нет активной задачи {slot}"
            t.stop()
            return True, f"Остановлено: {slot}"

    def stop_all(self) -> int:
        n = 0
        with self._lock:
            for t in self._tasks.values():
                if t.alive():
                    t.stop()
                    n += 1
        return n

    def status(self) -> dict:
        out = {}
        with self._lock:
            for slot, t in self._tasks.items():
                out[slot] = {
                    "running": t.alive(),
                    "elapsed": time.time() - t.started_at,
                    "exit_code": t.proc.poll(),
                    "log_tail": list(t.log)[-80:],
                }
        return out


tm = TaskManager()


# ── Pipeline ────────────────────────────────────────────────────────────────


class Pipeline:
    """Автоматическая последовательность: scan → download диска → collect → workers безлимита.
    Сам мониторит окончание каждого шага и переходит к следующему.
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop = False
        self._owned_slots: set[str] = set()  # слоты которые pipeline сам запустил
        self.state: str = "idle"  # idle | running | done | failed
        self.step: str = "—"
        self.log: deque[str] = deque(maxlen=120)
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.skip_disk: bool = False
        self.skip_unlim: bool = False
        self.workers: int = 4

    def _own_start(self, slot: str, script: str, args: list[str]) -> tuple[bool, str]:
        """Запускает задачу через tm и помечает её как принадлежащую pipeline'у."""
        ok, msg = tm.start(slot, script, args)
        if ok:
            self._owned_slots.add(slot)
        return ok, msg

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"{ts}  {msg}")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, workers: int = 4, do_disk: bool = True, do_unlim: bool = True) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                return False, "Pipeline уже идёт"
            self._stop = False
            self._owned_slots.clear()
            self.state = "running"
            self.step = "старт"
            self.log.clear()
            self.started_at = time.time()
            self.finished_at = None
            self.workers = max(1, min(workers, 6))
            self.skip_disk = not do_disk
            self.skip_unlim = not do_unlim
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, "Pipeline запущен"

    def stop(self) -> tuple[bool, str]:
        if not self.is_running():
            return False, "Pipeline не идёт"
        self._stop = True
        # Гасим ТОЛЬКО задачи которые pipeline сам запустил
        for slot in list(self._owned_slots):
            tm.stop(slot)
        self._log("⏹ остановлено пользователем")
        return True, "Остановлено"

    def _wait_task(self, slot: str, on_finish_msg: str) -> int:
        """Блокирующее ожидание завершения задачи в слоте."""
        while not self._stop:
            with tm._lock:
                t = tm._tasks.get(slot)
            if not t:
                time.sleep(0.5); continue
            if not t.alive():
                code = t.proc.poll()
                self._log(f"{on_finish_msg} (exit={code})")
                return code if code is not None else 0
            time.sleep(1.0)
        return -1

    def _run(self):
        try:
            cfg = get_config()

            # Шаг 0: токен
            self.step = "проверка токена"
            self._log("🔑 Проверяю токен...")
            tok = check_token(cfg["token"])
            if not tok.get("ok"):
                self._log(f"❌ Токен невалиден: {tok.get('msg')}")
                self.state = "failed"; return
            self._log(f"✓ Токен OK: {tok.get('login')}, {tok.get('used',0)/1e9:.1f} ГБ занято")

            # === ДИСК ===
            if not self.skip_disk:
                # scan
                if self._stop: self._abort(); return
                self.step = "сканирую диск"
                self._log("🔍 Запускаю scan...")
                ok, msg = self._own_start("disk", "main.py", ["scan"])
                if not ok:
                    self._log(f"❌ scan: {msg}"); self.state = "failed"; return
                self._wait_task("disk", "✓ scan завершён")
                if self._stop: self._abort(); return

                # download
                self.step = "качаю диск"
                self._log(f"⬇️ Запускаю download (workers={self.workers})...")
                ok, msg = self._own_start("disk", "main.py",
                                          ["download", "--workers", str(self.workers), "--no-scan"])
                if not ok:
                    self._log(f"❌ download: {msg}"); self.state = "failed"; return
                self._wait_task("disk", "✓ download диска завершён")
                if self._stop: self._abort(); return
            else:
                self._log("⏭ Шаг 'диск' пропущен")

            # === БЕЗЛИМИТ ===
            if not self.skip_unlim:
                cookies = check_cookies()
                if not cookies.get("ok"):
                    self._log(f"⚠️ Cookies не загружены, пропускаю безлимит: {cookies.get('msg')}")
                else:
                    # collect
                    if self._stop: self._abort(); return
                    self.step = "собираю список безлимита"
                    self._log("🗂 Запускаю unlim collect...")
                    ok, msg = self._own_start("unlim_collect", "unlim_download.py", ["--collect"])
                    if not ok:
                        self._log(f"❌ unlim collect: {msg}"); self.state = "failed"; return
                    self._wait_task("unlim_collect", "✓ список безлимита собран")
                    if self._stop: self._abort(); return

                    # workers
                    self.step = f"скачиваю безлимит ({self.workers} воркеров)"
                    self._log(f"🚀 Запускаю {self.workers} воркеров...")
                    for i in range(1, self.workers + 1):
                        ok, msg = self._own_start(f"unlim_w{i}", "unlim_worker.py", ["--id", str(i)])
                        if ok:
                            self._log(f"  ✓ воркер {i} запущен")
                        else:
                            self._log(f"  ✗ воркер {i}: {msg}")

                    # Ждём пока все воркеры закончат
                    while not self._stop:
                        any_alive = False
                        with tm._lock:
                            for slot, t in tm._tasks.items():
                                if slot.startswith("unlim_w") and t.alive():
                                    any_alive = True
                                    break
                        if not any_alive:
                            break
                        time.sleep(2)
                    self._log("✓ все воркеры безлимита завершили работу")
            else:
                self._log("⏭ Шаг 'безлимит' пропущен")

            self.state = "done"
            self.step = "финиш"
            self._log("🏆 ВСЁ ГОТОВО!")
        except Exception as e:
            self._log(f"❌ Ошибка pipeline: {e}")
            self.state = "failed"
        finally:
            self.finished_at = time.time()

    def _abort(self):
        self.state = "stopped"
        self.step = "остановлено"

    def status(self) -> dict:
        return {
            "state": self.state,
            "running": self.is_running(),
            "step": self.step,
            "started_at": self.started_at,
            "elapsed": (time.time() - self.started_at) if self.started_at and not self.finished_at else
                       ((self.finished_at - self.started_at) if self.started_at else None),
            "log": list(self.log),
        }


pipeline = Pipeline()


# ── Чтение состояния ────────────────────────────────────────────────────────


def db_stats(db_file: Path, table: str = "files",
             id_col: str = "remote_path") -> dict:
    if not db_file.exists():
        return {"total": 0, "pending": 0, "downloaded": 0, "skipped": 0,
                "failed": 0, "in_progress": 0,
                "bytes_total": 0, "bytes_done": 0, "recent": []}
    c = None
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        out = {"total": 0, "pending": 0, "downloaded": 0, "skipped": 0,
               "failed": 0, "in_progress": 0,
               "bytes_total": 0, "bytes_done": 0}
        for r in c.execute(
            f"SELECT status, COUNT(*) AS n, COALESCE(SUM(size),0) AS b "
            f"FROM {table} GROUP BY status"
        ):
            st = r["status"]
            if st not in out:
                out[st] = 0
            out[st] = r["n"]
            out["total"] += r["n"]
            out["bytes_total"] += r["b"]
            if st in ("downloaded", "skipped"):
                out["bytes_done"] += r["b"]
        recent = list(c.execute(
            f"SELECT {id_col} AS path, status, size FROM {table} "
            f"WHERE downloaded_at IS NOT NULL "
            f"ORDER BY downloaded_at DESC LIMIT 10"
        ))
        out["recent"] = [{"path": r["path"], "status": r["status"],
                          "size": r["size"]} for r in recent]
        return out
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        if c is not None:
            c.close()


# Кэш для тяжёлых операций (polling /api/status каждые 1.5 сек)
_local_size_cache: dict[str, dict] = {}
# 60 сек, а не 10: обход папки — это диск, а по нему в это же время идёт
# скачивание. /api/status опрашивается каждые 1.5 сек, так что дёшево тут важно.
_LOCAL_SIZE_TTL = 60  # секунд


def local_size(path: str) -> dict:
    r"""Размер папки. С TTL-кэшем 10 сек.
    Защита от корня диска (C:\) — не обходим, иначе зависнет на десятках ГБ.
    """
    if not os.path.isdir(path):
        return {"files": 0, "bytes": 0}

    # Защита: если path это корень диска (C:\, D:\) — не считаем (займёт минуты)
    abs_path = os.path.abspath(path)
    if len(abs_path) <= 3 and abs_path.endswith(":\\"):
        return {"files": 0, "bytes": 0, "skipped": "корень диска"}

    cached = _local_size_cache.get(path)
    now = time.time()
    if cached and (now - cached["at"] < _LOCAL_SIZE_TTL):
        return cached["data"]

    n = 0; b = 0
    partial = False
    deadline = now + 2.0  # макс 2 секунды на обход
    try:
        for dp, _, fns in os.walk(path):
            if time.time() > deadline:
                partial = True  # не досчитали — честно скажем UI
                break
            for fn in fns:
                try:
                    b += os.path.getsize(os.path.join(dp, fn))
                    n += 1
                except OSError:
                    pass
    except OSError:
        pass

    data = {"files": n, "bytes": b, "partial": partial}
    # Ограничиваем размер кэша — храним максимум 4 пути
    if len(_local_size_cache) >= 4 and path not in _local_size_cache:
        # удалить самый старый
        oldest = min(_local_size_cache.items(), key=lambda x: x[1]["at"])[0]
        _local_size_cache.pop(oldest, None)
    _local_size_cache[path] = {"at": now, "data": data}
    return data


# Кэш для check_token: хранит только ОДИН токен (последний проверенный).
# Так не накапливаются старые токены в памяти.
_token_check_cache: dict = {"token": None, "at": 0, "data": None}
_TOKEN_TTL = 30  # секунд


def check_token(token: str) -> dict:
    if not token or token.startswith("AQAAA_paste"):
        return {"ok": False, "msg": "Токен не задан"}
    # Кэш — не дёргать Яндекс каждые 1.5 сек!
    now = time.time()
    if (_token_check_cache["token"] == token
            and now - _token_check_cache["at"] < _TOKEN_TTL):
        return _token_check_cache["data"]
    try:
        import yadisk
        cli = yadisk.Client(token=token)
        if not cli.check_token():
            cli.close()
            return {"ok": False, "msg": "Токен невалиден"}
        info = cli.get_disk_info()
        used = getattr(info, "used_space", 0) or 0
        total = getattr(info, "total_space", 0) or 0
        photo_unlim = getattr(info, "photounlim_size", 0) or 0
        login = getattr(info.user, "login", "?") if getattr(info, "user", None) else "?"
        cli.close()
        data = {"ok": True, "msg": "OK", "used": used, "total": total,
                "photo_unlim": photo_unlim, "login": login}
        _token_check_cache.update({"token": token, "at": now, "data": data})
        return data
    except Exception as e:
        data = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
        _token_check_cache.update({"token": token, "at": now, "data": data})
        return data


def check_cookies() -> dict:
    if not COOKIES_FILE.exists():
        return {"ok": False, "msg": "cookies.json не загружен", "count": 0}
    try:
        raw = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return {"ok": False, "msg": "Формат не Cookie-Editor", "count": 0}
        names = {c.get("name") for c in raw if isinstance(c, dict)}
        important = ["Session_id", "yandexuid", "sessionid2"]
        missing = [n for n in important if n not in names]
        if missing:
            return {"ok": False,
                    "msg": f"Не хватает: {', '.join(missing)}. "
                           f"Залогинься в браузере и заэкспортируй заново.",
                    "count": len(raw)}
        return {"ok": True, "msg": f"Куки OK ({len(raw)} шт)", "count": len(raw)}
    except Exception as e:
        return {"ok": False, "msg": f"Ошибка: {e}", "count": 0}


# ── Endpoints ───────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = TEMPLATES_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>UI не установлен</h1>")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/status")
def api_status():
    cfg = get_config()
    return {
        "config": {**cfg, "token_masked": mask_token(cfg["token"])},
        "token": check_token(cfg["token"]),
        "cookies": check_cookies(),
        "disk": {
            "db": db_stats(DB_FILE, "files", "remote_path"),
            "local": local_size(cfg["download_dir"]),
        },
        "unlim": {
            "db": db_stats(UNLIM_DB_FILE, "unlim_files", "file_id"),
            "local": local_size(os.path.join(cfg["download_dir"], "_unlim")),
        },
        "tasks": tm.status(),
        "pipeline": pipeline.status(),
    }


class StartPayload(BaseModel):
    workers: int = Field(default=4, ge=1, le=6)
    do_disk: bool = True
    do_unlim: bool = True


@app.post("/api/start")
def api_start(payload: StartPayload):
    ok, msg = pipeline.start(workers=payload.workers,
                              do_disk=payload.do_disk,
                              do_unlim=payload.do_unlim)
    return {"ok": ok, "msg": msg}


@app.post("/api/start/stop")
def api_start_stop():
    ok, msg = pipeline.stop()
    return {"ok": ok, "msg": msg}


class TokenPayload(BaseModel):
    token: str


@app.post("/api/config/token/check")
def api_check_token(payload: TokenPayload):
    """Проверка без сохранения."""
    return check_token((payload.token or "").strip())


@app.post("/api/config/token")
def api_set_token(payload: TokenPayload):
    token = (payload.token or "").strip()
    if not token:
        raise HTTPException(400, "Пустой токен")
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    set_key(str(ENV_FILE), "YA_DISK_TOKEN", token, quote_mode="never")
    load_dotenv(ENV_FILE, override=True)
    return {"ok": True, "check": check_token(token)}


class DownloadDirPayload(BaseModel):
    download_dir: str


@app.post("/api/config/download_dir")
def api_set_dir(payload: DownloadDirPayload):
    d = (payload.download_dir or "").strip()
    if not d:
        raise HTTPException(400, "Пустой путь")
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    set_key(str(ENV_FILE), "DOWNLOAD_DIR", d, quote_mode="never")
    load_dotenv(ENV_FILE, override=True)
    return {"ok": True, "download_dir": d}


class CookiesPayload(BaseModel):
    cookies: list


def _validate_cookies(arr: list) -> dict:
    if not isinstance(arr, list):
        return {"ok": False, "msg": "Должен быть массив"}
    names = {c.get("name") for c in arr if isinstance(c, dict)}
    important = ["Session_id", "yandexuid", "sessionid2"]
    missing = [n for n in important if n not in names]
    if missing:
        return {"ok": False,
                "msg": f"Не хватает кук: {', '.join(missing)}. "
                       f"Залогинься в браузере на disk.yandex.ru и заэкспортируй заново."}
    return {"ok": True, "msg": f"Куки готовы ({len(arr)} шт)"}


@app.post("/api/cookies/check")
def api_check_cookies(payload: CookiesPayload):
    """Проверка без сохранения."""
    return _validate_cookies(payload.cookies)


@app.post("/api/cookies")
def api_set_cookies(payload: CookiesPayload):
    """Сохраняет cookies.json атомарно через tmp+replace."""
    arr = payload.cookies
    tmp = COOKIES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, COOKIES_FILE)
    return {"ok": True, "check": check_cookies(), "saved_count": len(arr)}


@app.post("/api/cookies/clear")
def api_clear_cookies():
    if COOKIES_FILE.exists():
        try:
            COOKIES_FILE.unlink()
        except OSError as e:
            return {"ok": False, "msg": str(e)}
    return {"ok": True}


@app.post("/api/config/token/clear")
def api_clear_token():
    if ENV_FILE.exists():
        set_key(str(ENV_FILE), "YA_DISK_TOKEN", "", quote_mode="never")
        load_dotenv(ENV_FILE, override=True)
    return {"ok": True}


def preview_db(db_file: Path, table: str, id_col: str) -> dict:
    """Группирует файлы в БД по категориям."""
    if not db_file.exists():
        return {"empty": True, "categories": {}}
    c = None
    try:
        c = sqlite3.connect(sqlite_ro_uri(db_file), uri=True, timeout=30)
        summary: dict[str, dict] = {
            k: {"count": 0, "bytes": 0, "label": CATEGORY_LABELS[k]}
            for k in list(CATEGORY_LABELS.keys())
        }
        total = 0
        for row in c.execute(
            f"SELECT {id_col}, COALESCE(size, 0) FROM {table} "
            f"WHERE status IN ('pending','downloaded','skipped','in_progress','failed')"
        ):
            cat = categorize(row[0])
            summary[cat]["count"] += 1
            summary[cat]["bytes"] += row[1]
            total += 1
        return {"empty": total == 0, "total": total, "categories": summary}
    except sqlite3.Error as e:
        return {"empty": True, "error": str(e)}
    finally:
        if c is not None:
            c.close()


def filter_to_categories(db_file: Path, table: str, id_col: str,
                          selected: list[str]) -> tuple[int, int]:
    """Помечает файлы вне выбранных категорий как 'skipped' (одним проходом).
    Уже скачанные/skipped/in_progress не трогаем.
    Возвращает (skipped_count, kept_pending).
    """
    if not db_file.exists():
        return (0, 0)
    sel = set(selected)
    c = sqlite3.connect(db_file, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        rows = c.execute(
            f"SELECT {id_col} FROM {table} WHERE status='pending'"
        ).fetchall()
        to_skip = []
        kept = 0
        for (path,) in rows:
            if categorize(path) in sel:
                kept += 1
            else:
                to_skip.append(path)
        if to_skip:
            c.execute("BEGIN")
            try:
                c.executemany(
                    f"UPDATE {table} SET status='skipped', "
                    f"error='not in selected categories' WHERE {id_col}=?",
                    [(p,) for p in to_skip],
                )
                c.execute("COMMIT")
            except Exception:
                try: c.execute("ROLLBACK")
                except sqlite3.Error: pass
                raise
        return (len(to_skip), kept)
    finally:
        c.close()


def unskip_category_marker(db_file: Path, table: str) -> int:
    """Сбрасывает skipped-with-marker обратно в pending — на случай повторного выбора."""
    if not db_file.exists():
        return 0
    c = sqlite3.connect(db_file, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        c.execute("BEGIN")
        try:
            n = c.execute(
                f"UPDATE {table} SET status='pending', error=NULL "
                f"WHERE status='skipped' AND error='not in selected categories'"
            ).rowcount
            c.execute("COMMIT")
            return n
        except Exception:
            try: c.execute("ROLLBACK")
            except sqlite3.Error: pass
            raise
    finally:
        c.close()


@app.get("/api/disk/preview")
def disk_preview():
    return preview_db(DB_FILE, "files", "remote_path")


@app.get("/api/unlim/preview")
def unlim_preview():
    return preview_db(UNLIM_DB_FILE, "unlim_files", "file_id")


class SelectivePayload(BaseModel):
    categories: list[str]
    workers: int = Field(default=4, ge=1, le=6)


@app.post("/api/disk/download_selected")
def disk_download_selected(payload: SelectivePayload):
    # Сначала вернуть всё что было ранее помечено как "не выбрано" в pending
    unskip_category_marker(DB_FILE, "files")
    # Потом отфильтровать заново
    filter_to_categories(DB_FILE, "files", "remote_path", payload.categories)
    # И запустить download
    workers = max(1, min(payload.workers, 5))
    ok, msg = tm.start("disk", "main.py",
                       ["download", "--workers", str(workers), "--no-scan"])
    return {"ok": ok, "msg": msg}


@app.post("/api/unlim/download_selected")
def unlim_download_selected(payload: SelectivePayload):
    unskip_category_marker(UNLIM_DB_FILE, "unlim_files")
    filter_to_categories(UNLIM_DB_FILE, "unlim_files", "file_id",
                         payload.categories)
    n = max(1, min(payload.workers, 6))
    started = []
    failed = []
    for i in range(1, n + 1):
        ok, msg = tm.start(f"unlim_w{i}", "unlim_worker.py", ["--id", str(i)])
        if ok:
            started.append(i)
        else:
            failed.append({"id": i, "msg": msg})
    return {"ok": True, "started": started, "failed": failed}


# Disk actions
@app.post("/api/disk/scan")
def disk_scan():
    return _resp(tm.start("disk", "main.py", ["scan"]))


class DownloadOpts(BaseModel):
    workers: int = Field(default=4, ge=1, le=5)


@app.post("/api/disk/download")
def disk_download(opts: DownloadOpts):
    return _resp(tm.start("disk", "main.py",
                          ["download", "--workers", str(opts.workers), "--no-scan"]))


@app.post("/api/disk/retry")
def disk_retry():
    return _resp(tm.start("disk", "main.py", ["retry-failed"]))


@app.post("/api/disk/verify")
def disk_verify():
    return _resp(tm.start("disk", "main.py", ["verify"]))


@app.post("/api/disk/stop")
def disk_stop():
    return _resp(tm.stop("disk"))


@app.post("/api/disk/clear")
def disk_clear():
    """Полная очистка БД сканирования диска + остановка задач."""
    tm.stop("disk")
    try:
        for suffix in ("", "-wal", "-shm"):
            f = DB_FILE.with_suffix(DB_FILE.suffix + suffix) if suffix else DB_FILE
            if f.exists():
                f.unlink()
        return {"ok": True, "msg": "БД диска очищена"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


@app.post("/api/unlim/clear")
def unlim_clear():
    """Полная очистка БД безлимита + остановка воркеров."""
    with tm._lock:
        for slot, t in list(tm._tasks.items()):
            if slot.startswith("unlim") and t.alive():
                t.stop()
    try:
        for suffix in ("", "-wal", "-shm"):
            f = UNLIM_DB_FILE.with_suffix(UNLIM_DB_FILE.suffix + suffix) if suffix else UNLIM_DB_FILE
            if f.exists():
                f.unlink()
        return {"ok": True, "msg": "БД безлимита очищена"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


# Photoslice (безлимит)
@app.post("/api/unlim/collect")
def unlim_collect():
    return _resp(tm.start("unlim_collect", "unlim_download.py", ["--collect"]))


class WorkersPayload(BaseModel):
    workers: int = 4


@app.post("/api/unlim/start_workers")
def unlim_start_workers(payload: WorkersPayload):
    n = max(1, min(payload.workers, 6))
    started = []
    failed = []
    for i in range(1, n + 1):
        ok, msg = tm.start(f"unlim_w{i}", "unlim_worker.py", ["--id", str(i)])
        if ok:
            started.append(i)
        else:
            failed.append({"id": i, "msg": msg})
    return {"ok": True, "started": started, "failed": failed}


@app.post("/api/unlim/stop")
def unlim_stop():
    n = 0
    with tm._lock:
        for slot, t in tm._tasks.items():
            if slot.startswith("unlim") and t.alive():
                t.stop()
                n += 1
    return {"ok": True, "stopped": n}


# Organize / dedupe
class OrganizePayload(BaseModel):
    input: str
    action: str = "report-only"  # report-only | move


# Запрещённые пути для organize (нельзя бесконтрольно сортировать систему)
_FORBIDDEN_ORGANIZE_PREFIXES = [
    "c:\\windows", "c:\\program files", "c:\\programdata",
    "c:\\users\\all users", "c:\\$recycle.bin",
    "/", "/etc", "/usr", "/bin", "/sbin", "/system", "/library",
]


def _is_safe_organize_path(path: str) -> tuple[bool, str]:
    """Запрещаем сортировку системных папок и корней дисков."""
    try:
        abs_path = os.path.abspath(path)
    except Exception:
        return False, "Невалидный путь"
    norm = abs_path.lower().replace("/", "\\")
    # Корень диска (типа C:\ или D:\) — слишком опасно
    if len(abs_path) <= 3 and abs_path.endswith(":\\"):
        return False, "Нельзя сортировать корень диска целиком"
    for bad in _FORBIDDEN_ORGANIZE_PREFIXES:
        bad_norm = bad.replace("/", "\\")
        if norm == bad_norm or norm.startswith(bad_norm + "\\"):
            return False, f"Системная папка запрещена: {bad}"
    if not os.path.isdir(abs_path):
        return False, "Папка не существует"
    return True, ""


@app.post("/api/organize")
def organize(payload: OrganizePayload):
    ok, msg = _is_safe_organize_path(payload.input)
    if not ok:
        return {"ok": False, "msg": msg}
    if payload.action not in ("report-only", "move"):
        return {"ok": False, "msg": "Неизвестное действие"}
    return _resp(tm.start("organize", "organize.py",
                          ["--input", payload.input,
                           "--action", payload.action]))


@app.post("/api/organize/stop")
def organize_stop():
    return _resp(tm.stop("organize"))


@app.post("/api/stop_all")
def stop_all():
    n = tm.stop_all()
    return {"ok": True, "stopped": n}


@app.post("/api/task/{slot}/clear_log")
def clear_task_log(slot: str):
    """Очищает буфер логов задачи (или всех slot с префиксом)."""
    cleared = 0
    with tm._lock:
        for s, t in tm._tasks.items():
            if s == slot or (slot.endswith("*") and s.startswith(slot[:-1])):
                t.log.clear()
                cleared += 1
    return {"ok": True, "cleared": cleared}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _resp(t: tuple[bool, str]):
    return {"ok": t[0], "msg": t[1]}


if (TEMPLATES_DIR / "static").exists():
    app.mount("/static",
              StaticFiles(directory=str(TEMPLATES_DIR / "static")),
              name="static")


def _open_browser_when_ready(host: str, port: int):
    """Открывает браузер только когда сервер начал отвечать."""
    import http.client
    import webbrowser
    url = f"http://{host}:{port}"
    for _ in range(60):  # ждём до ~30 сек
        time.sleep(0.5)
        try:
            conn = http.client.HTTPConnection(host, port, timeout=1)
            conn.request("GET", "/api/status")
            resp = conn.getresponse()
            if resp.status == 200:
                conn.close()
                break
            conn.close()
        except Exception:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    import uvicorn
    host = "127.0.0.1"
    port = 8000
    url = f"http://{host}:{port}"
    print("=" * 60)
    print("  Yandex.Disk Backup — Web UI")
    print(f"  {url}")
    print("=" * 60)
    print()
    print("  Сервер запускается, браузер откроется автоматически.")
    print("  Чтобы остановить — закрой это окно или нажми Ctrl+C.")
    print()

    # Открыть браузер в фоне когда сервер готов
    threading.Thread(
        target=_open_browser_when_ready,
        args=(host, port),
        daemon=True,
    ).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
