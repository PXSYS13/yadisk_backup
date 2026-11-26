"""Управление фоновой задачей сканирования/загрузки и живой лентой событий для Web UI."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import deque
from typing import Any

from uploader import paths
from uploader.cookies import (
    CookieJarDict,
    CookiesFormatError,
    get_sk_uid,
    load_cookies,
    make_session,
)
from uploader.engine import LiveProgress, UploadEngine, scan_source_folder
from uploader.quota_guard import QuotaSnapshot, fetch_quota_snapshot
from uploader.state import UploadState
from uploader.utils import (
    EXIT_AUTH,
    EXIT_INTERRUPTED,
    EXIT_OK,
    IS_WINDOWS,
    CancelledError,
    hhmm,
    human_bytes,
    validate_source_dir_safe,
)
from uploader.webapp import config as cfg_mod

log = logging.getLogger("unlim_uploader.webui")


class UploadManager:
    """Потокобезопасный менеджер фоновой операции и сводки для GET /api/status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._progress = LiveProgress()
        self._mode: str = "idle"
        self._running: bool = False
        self._exit_code: int | None = None
        self._message: str = ""
        self._events: deque[dict[str, str]] = deque(maxlen=120)
        self._account: dict[str, Any] | None = None
        self._account_checked_at: float = 0.0
        self._cookies_error: str | None = None
        self._cookies_sig: tuple[int, int] | None = None

    @staticmethod
    def _current_cookies_sig() -> tuple[int, int] | None:
        try:
            st = os.stat(paths.COOKIES_FILE)
            return (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return None

    def add_event(self, kind: str, text: str) -> None:
        with self._lock:
            self._events.appendleft({"at": hhmm(), "kind": kind, "text": text})

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def check_cookies_file(self) -> dict[str, Any]:
        if not paths.COOKIES_FILE.exists():
            return {"present": False, "ok": False, "error": "Данные входа ещё не добавлены"}
        try:
            jar = load_cookies(paths.COOKIES_FILE)
            return {"present": True, "ok": True, "base_url": jar.base_url, "error": None}
        except (CookiesFormatError, OSError, UnicodeDecodeError) as e:
            return {"present": True, "ok": False, "error": str(e)}

    def verify_account_live(self, jar: CookieJarDict | None = None) -> dict[str, Any]:
        """Живая проверка кук и квоты аккаунта Яндекса (с обязательным закрытием HTTP-сессии)."""
        session = (
            make_session(cookies=jar)
            if jar is not None
            else make_session(cookies_file=paths.COOKIES_FILE)
        )
        try:
            sk, uid = get_sk_uid(session)
            snap: QuotaSnapshot = fetch_quota_snapshot(session, sk, uid)
            info = snap.to_dict()
            info["base_url"] = getattr(session, "yadisk_base", "https://disk.yandex.ru")
        finally:
            session.close()
        with self._lock:
            self._account = info
            self._account_checked_at = time.time()
            self._cookies_error = None
            self._cookies_sig = self._current_cookies_sig()
        return info

    def set_account_cache(self, info: dict[str, Any] | None, error: str | None = None) -> None:
        with self._lock:
            self._account = info
            self._account_checked_at = time.time() if info else 0.0
            self._cookies_error = error
            self._cookies_sig = self._current_cookies_sig() if (info or error) else None

    def start_scan(self, source_dir: str) -> str:
        clean_dir = validate_source_dir_safe(source_dir)
        if not os.path.isdir(clean_dir):
            raise ValueError("Указанная папка на компьютере не найдена")

        stop_ev = threading.Event()
        prog = LiveProgress()
        prog.set_phase("scanning")

        with self._lock:
            if self._running:
                raise RuntimeError("Сейчас уже идёт другая операция")
            self._running = True
            self._stop_event = stop_ev
            self._progress = prog
            self._mode = "scan"
            self._exit_code = None
            self._message = f"Сканирую папку {clean_dir}…"

        self.add_event("run", f"Начинаю проверку папки {clean_dir} (файлы не меняются)")

        def _worker() -> None:
            try:
                state = UploadState(paths.DB_FILE)

                def _on_prog(n: int) -> None:
                    with prog.lock:
                        prog.scanned_files = n

                res = scan_source_folder(
                    clean_dir,
                    state,
                    stop_event=stop_ev,
                    on_progress=_on_prog,
                )
                st = state.stats()
                msg = (
                    f"Проверка завершена: найдено {res.photos} фото и {res.videos} видео "
                    f"({human_bytes(st['media_bytes'])}), пропущено неподходящих: {res.unsupported}"
                )
                if res.unreadable_dirs or res.unreadable_files:
                    msg += f" (не прочитано папок: {res.unreadable_dirs}, файлов: {res.unreadable_files})"
                    self.add_event("warn", msg)
                else:
                    self.add_event("ok", msg)
                prog.set_phase("done")
                with self._lock:
                    self._exit_code = EXIT_OK
                    self._message = msg
            except CancelledError:
                msg = "Проверка папки остановлена пользователем"
                self.add_event("info", msg)
                prog.set_phase("idle")
                with self._lock:
                    self._exit_code = EXIT_INTERRUPTED
                    self._message = msg
            except Exception as e:  # noqa: BLE001
                log.exception("Сбой сканирования папки")
                self.add_event("err", f"Ошибка сканирования: {e}")
                prog.set_phase("error")
                with self._lock:
                    self._exit_code = 1
                    self._message = str(e)
            finally:
                with self._lock:
                    self._running = False

        t = threading.Thread(target=_worker, name="webui-scan", daemon=True)
        try:
            with self._lock:
                self._thread = t
            t.start()
        except BaseException:
            with self._lock:
                self._running = False
                self._thread = None
            raise
        return clean_dir

    def start_upload(
        self,
        *,
        source_dir: str,
        allow_quota_count: bool = False,
        refresh_index: bool = False,
        watch_mode: bool | None = None,
    ) -> str:
        clean_dir = validate_source_dir_safe(source_dir)
        if not os.path.isdir(clean_dir):
            raise ValueError("Выберите существующую папку на компьютере")
        ck = self.check_cookies_file()
        if not ck["ok"]:
            raise ValueError(ck["error"] or "Сначала добавьте данные входа (cookies)")

        cfg = cfg_mod.get_config()
        use_watch = cfg["watch_mode"] if watch_mode is None else bool(watch_mode)

        stop_ev = threading.Event()
        prog = LiveProgress()

        with self._lock:
            if self._running:
                raise RuntimeError("Загрузка уже запущена")
            self._running = True
            self._stop_event = stop_ev
            self._progress = prog
            self._mode = "watch" if use_watch else "upload"
            self._exit_code = None
            self._message = "Запускаю загрузку в Безлимит…"

        self.add_event("run", f"Старт загрузки из папки {clean_dir}")

        def _worker() -> None:
            try:
                state = UploadState(paths.DB_FILE)
                engine = UploadEngine(
                    state,
                    workers=cfg["max_workers"],
                    chunk_mb=cfg["chunk_mb"],
                    strict_quota_guard=cfg["strict_quota_guard"],
                    allow_quota_count=allow_quota_count,
                    refresh_remote_index=refresh_index,
                    cookies_file=paths.COOKIES_FILE,
                    on_event=self.add_event,
                    progress=prog,
                    stop_event=stop_ev,
                )
                try:
                    if use_watch:
                        summary = engine.run_watch(
                            clean_dir, interval_sec=cfg["watch_interval_sec"]
                        )
                    else:
                        summary = engine.run_once(clean_dir)
                finally:
                    engine.close()

                if summary.exit_code == EXIT_AUTH:
                    self.set_account_cache(None, summary.message)
                elif summary.quota_after is not None:
                    self.set_account_cache(summary.quota_after.to_dict(), None)
                elif summary.quota_before is not None:
                    self.set_account_cache(summary.quota_before.to_dict(), None)

                with self._lock:
                    self._exit_code = summary.exit_code
                    self._message = summary.message
            except Exception as e:  # noqa: BLE001
                log.exception("Сбой фоновой загрузки")
                self.add_event("err", f"Непредвиденная ошибка: {e}")
                prog.set_phase("error")
                with self._lock:
                    self._exit_code = 1
                    self._message = str(e)
            finally:
                with self._lock:
                    self._running = False

        t = threading.Thread(target=_worker, name="webui-upload", daemon=True)
        try:
            with self._lock:
                self._thread = t
            t.start()
        except BaseException:
            with self._lock:
                self._running = False
                self._thread = None
            raise
        return clean_dir

    def stop(self) -> bool:
        with self._lock:
            if not self._running:
                return False
            ev = self._stop_event
        ev.set()
        self.add_event("info", "Запрошена остановка — завершаю текущий шаг…")
        return True

    def shutdown(self, timeout: float = 10.0) -> None:
        """Корректно останавливает фоновую задачу и ждёт её завершения при закрытии сервера."""
        with self._lock:
            ev = self._stop_event
            t = self._thread
        ev.set()
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
            if t.is_alive():
                log.warning(
                    "Фоновый поток загрузки не завершился за %.1f с при остановке сервера", timeout
                )

    def status_dict(self) -> dict[str, Any]:
        cfg = cfg_mod.get_config()
        state = UploadState(paths.DB_FILE)
        stats = state.stats()
        ck = self.check_cookies_file()
        cur_sig = self._current_cookies_sig()
        with self._lock:
            if (self._account is not None or self._cookies_error is not None) and (
                cur_sig != self._cookies_sig
            ):
                self._account = None
                self._account_checked_at = 0.0
                self._cookies_error = None
                self._cookies_sig = None
            running = self._running
            mode = self._mode
            exit_code = self._exit_code
            message = self._message
            events = list(self._events)
            account = dict(self._account) if self._account else None
            cookies_err = self._cookies_error
            live_snap = self._progress.snapshot()
        if cookies_err and ck["ok"]:
            ck["ok"] = False
            ck["error"] = cookies_err
        return {
            "running": running,
            "mode": mode,
            "exit_code": exit_code,
            "message": message,
            "config": cfg,
            "cookies": ck,
            "account": account,
            "stats": stats,
            "live": live_snap,
            "events": events,
            "source_saved": state.get_meta("source_dir"),
            "last_scan_at": state.get_meta("last_scan_at"),
            "remote_indexed_at": state.get_meta("remote_indexed_at"),
        }


manager = UploadManager()


def pick_folder_dialog(initial_dir: str = "") -> tuple[str | None, str | None]:
    """Открывает системное окно выбора папки: (путь | None, ошибка | None)."""
    errors: list[str] = []
    if IS_WINDOWS:
        # Статический скрипт: initial_dir передаётся только через переменную окружения YADISK_INIT_DIR,
        # UTF-8 вывод фиксируется явно, чтобы кириллица не билась при консольной кодировке CP866.
        ps = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = 'Выберите папку с фото и видео'; "
            "$p = $env:YADISK_INIT_DIR; "
            "if ($p -and (Test-Path -LiteralPath $p -PathType Container)) { $d.SelectedPath = $p }; "
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
        )
        ps_exe = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
        )
        env = {**os.environ, "YADISK_INIT_DIR": initial_dir or ""}
        try:
            proc = subprocess.run(
                [ps_exe, "-NoProfile", "-NonInteractive", "-STA", "-Command", ps],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            if proc.returncode == 0:
                chosen = (proc.stdout or "").strip()
                return (chosen or None), None
            msg = f"FolderBrowserDialog завершился с кодом {proc.returncode}"
            log.warning("%s: %s", msg, (proc.stderr or "")[:200])
            errors.append(msg)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось открыть PowerShell FolderBrowserDialog: %s", e)
            errors.append(f"PowerShell: {e}")

    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            initialdir=initial_dir or None, title="Выберите папку с фото и видео"
        )
        return (chosen or None), None
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось открыть диалог выбора папки tkinter: %s", e)
        errors.append(f"tkinter: {e}")
        return None, "; ".join(errors) or "Не удалось открыть системное окно выбора папки"
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
