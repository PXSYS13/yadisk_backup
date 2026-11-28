"""Веб-дашборд для бэкапа Яндекс.Диска.

Запуск:
  uv run webui.py
  → http://127.0.0.1:8000 (если порт занят чужой программой — следующий свободный)

Здесь только сборка приложения и запуск сервера. Логика — в пакете webapp/:
«Режим спасения» (webapp.rescue), /api/status (webapp.status), кнопки интерфейса
(webapp.control) и остальные разделы — см. webapp/__init__.py.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import platform
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from utils import APP_VERSION, TELEGRAM_CHANNEL, YOUTUBE_VIDEO_URL, ensure_utf8_stdio
from webapp import paths  # первым: читает .env, в нём бывает YADISK_PORT
from webapp import (access, config, control, diagnostics, file_list, folders, guide, links, logs, pages, people, ports,
                    selfupdate, status, upload_unlim)

log = logging.getLogger("webui")

RESTART_PORT_WAIT_SEC = 2.5
RESTART_PORT_STEP_SEC = 0.25

app = FastAPI(title="Yandex.Disk Backup UI")
app.middleware("http")(ports.csrf_guard)
# Конкретные роутеры (/api/upload/*, /api/people/*) регистрируем ДО file_list (/api/{source}/files),
# чтобы шаблон {source} не перехватывал префикс /api/upload/files.
for _module in (status, control, access, upload_unlim, people, file_list, folders, links, logs, guide, pages,
                selfupdate):
    app.include_router(_module.router)

# Windows берёт MIME из реестра, и там .js бывает text/plain — браузер такой скрипт не выполнит
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
if (paths.TEMPLATES_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=str(paths.TEMPLATES_DIR / "static")), name="static")


def _setup_logging() -> None:
    """Файл-лог сервера: действия пользователя, запуски задач, ошибки — без секретов."""
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    handler = RotatingFileHandler(paths.WEBUI_LOG, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(fmt)
    for name in ("webui", "uvicorn.error", "photo_sorter", "unlim_uploader"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)
    # Ошибки сервера (HTTP 500) и предупреждения подсистем видно и в чёрном окне
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(fmt)
    for name in ("uvicorn.error", "photo_sorter", "unlim_uploader"):
        logging.getLogger(name).addHandler(console)


def main():
    ensure_utf8_stdio()
    _setup_logging()
    log.info("webui v%s старт: python %s, %s, проект %s", APP_VERSION,
             platform.python_version(), platform.platform(), paths.PROJECT)
    # Эту версию только что поставила кнопка «Обновить программу»?
    restarted = selfupdate.take_restart_marker()

    import uvicorn

    wanted = ports.PORT
    sock = running = None
    kept = False  # снова на прежнем порту после обновления, хотя обычный порт другой
    # После «Обновить программу» — сначала свой прежний порт: открытая страница продолжит работать
    # с этой копией, а не с соседней (копия для второго аккаунта могла бы занять первый свободный).
    # На Windows без SO_REUSEADDR прежний процесс может освобождать сокет долю секунды —
    # при автоперезапуске пробуем занять прежний порт до ~2,5 с, прежде чем уйти на другой.
    old_port = (restarted or {}).get("port")
    auto_restart = bool(restarted and restarted.get("restart") == "auto")
    if type(old_port) is int and 1024 <= old_port <= 65535 and (not ports.PORT_FIXED or old_port == wanted):
        if auto_restart:
            deadline = time.monotonic() + RESTART_PORT_WAIT_SEC
            while True:
                sock, port, _ = ports.choose_port(old_port, 1)
                if sock is not None or time.monotonic() >= deadline:
                    break
                time.sleep(min(RESTART_PORT_STEP_SEC, max(0.0, deadline - time.monotonic())))
            kept = sock is not None and port != wanted
        elif old_port != wanted:
            sock, port, _ = ports.choose_port(old_port, 1)
            kept = sock is not None
    if sock is None:
        sock, port, running = ports.choose_port(wanted, 1 if ports.PORT_FIXED else ports.PORT_TRIES)
    if running is not None:
        ports.show_running_copy(port, running)
        return
    if sock is None:
        last = wanted + (0 if ports.PORT_FIXED else ports.PORT_TRIES - 1)
        if ports.PORT_FIXED:
            print(f"  Порт {wanted} из YADISK_PORT занят другой программой. Укажите другой порт или уберите YADISK_PORT.")
        else:
            print(f"  Порты {wanted}–{last} заняты другими программами. Закройте лишние программы и запустите снова.")
        log.warning("не запустился: порты %d–%d заняты", wanted, last)
        sys.exit(1)
    ports.use_port(port)
    url = f"http://{ports.HOST}:{port}"
    # Кто держит наш обычный порт: копия этой программы из другой папки (второй аккаунт) или чужая программа.
    # После обновления на прежнем порту не спрашиваем: в Windows отказ соединения ждёт секунды, а страница ждёт нас
    neighbour = ports.other_copy_on(wanted) if port != wanted and not kept else None
    if neighbour or (port != wanted and not kept):
        log.info("порт %d занят %s, работаю на %d", wanted,
                 "копией программы из другой папки" if neighbour else "другой программой", port)
    elif kept:
        log.info("после обновления работаю на прежнем порту %d", port)

    # Монитор Яндекса: два лёгких запроса в минуту, история в logs/yandex_health.jsonl
    diagnostics.start_health_monitor(lambda: config.get_config()["token"])
    print("=" * 65)
    print(f"  Yandex.Disk Backup — Web UI by Davron (PXSYS) · v{APP_VERSION}")
    print(f"  Адрес: {url}")
    print(f"  Telegram: {TELEGRAM_CHANNEL} (новости, помощь, статус Яндекса)")
    print(f"  YouTube: {YOUTUBE_VIDEO_URL}")
    print("=" * 65)
    print()
    if neighbour:
        print(f"  На http://{ports.HOST}:{wanted} уже работает эта программа из другой папки:")
        print(f"  {ports.neighbour_text(neighbour)}")
        print(f"  Эта копия работает рядом, на {url}. У каждой копии свои ключ доступа, папка и прогресс.")
        print()
    elif port != wanted and not kept:
        print(f"  Порт {wanted} занят другой программой (в Linux так бывает со службой печати),")
        print(f"  поэтому программа работает на {url}")
        print()
    # После перезапуска ради обновления страница уже открыта на этом порту и обновится сама
    reopened = bool(restarted and restarted.get("restart") == "auto" and restarted.get("port") == port)
    if reopened:
        print(f"  Работает новая версия {APP_VERSION}. Страница в браузере обновится сама.")
    else:
        print("  Сервер запускается, браузер откроется автоматически.")
    print("  Чтобы остановить — закрой это окно или нажми Ctrl+C.")
    print()

    # Открыть браузер в фоне когда сервер готов (YADISK_NO_BROWSER=1 — не открывать: dev/CI)
    if os.getenv("YADISK_NO_BROWSER") != "1" and not reopened:
        threading.Thread(target=ports.open_browser_when_ready, args=(ports.HOST, port), daemon=True).start()
    # Скачивание шло, когда программу закрыли (окно, перезагрузка, пропал свет), — продолжаем без кнопки
    threading.Thread(target=control.resume_after_restart, daemon=True).start()

    # Порт уже занят нами в choose_port: uvicorn получает готовый сокет, и между
    # проверкой и запуском его никто не перехватит
    # log_config=None: своя настройка журналов uvicorn сняла бы наш файл-лог с uvicorn.error,
    # и тексты ошибок сервера (HTTP 500) не попадали бы ни в webui.log, ни в отчёт для поддержки
    server = uvicorn.Server(uvicorn.Config(app, host=ports.HOST, port=port, log_level="warning",
                                           log_config=None, timeout_graceful_shutdown=5))
    # Обновление останавливает сервер и выходит с кодом 75 — лаунчер запускает новую версию
    selfupdate.set_restart_hook(lambda: setattr(server, "should_exit", True), port)
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        from webapp.rescue import rescue
        from webapp.tasks import (
            SLOT_DISK,
            SLOT_LINKS,
            SLOT_ORGANIZE,
            UNLIM_SLOT_PREFIX,
            stop_and_wait,
            tm,
        )
        if diagnostics.health_monitor is not None:
            diagnostics.health_monitor.stop()
        if rescue.is_running():
            rescue.stop()
        try:
            from uploader.webapp import manager as upload_mgr
            upload_mgr.manager.shutdown(timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("upload_mgr shutdown error: %s", exc)
        if people.manager is not None:
            try:
                people.manager.shutdown(timeout=5.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("people.manager shutdown error: %s", exc)
        stop_and_wait(
            tm.stop_matching(SLOT_DISK)
            + tm.stop_matching(UNLIM_SLOT_PREFIX)
            + tm.stop_matching(SLOT_LINKS)
            + tm.stop_matching(SLOT_ORGANIZE)
        )
        selfupdate.wait_for_files()
        fallback = selfupdate._fallback
        if fallback is not None:
            fallback.cancel()
    if selfupdate.exit_code is not None:
        sys.exit(selfupdate.exit_code)


if __name__ == "__main__":
    main()
