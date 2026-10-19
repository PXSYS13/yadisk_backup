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
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from utils import APP_VERSION, TELEGRAM_CHANNEL, YOUTUBE_VIDEO_URL, ensure_utf8_stdio
from webapp import paths  # первым: читает .env, в нём бывает YADISK_PORT
from webapp import access, config, control, diagnostics, folders, guide, links, logs, pages, ports, status

log = logging.getLogger("webui")

app = FastAPI(title="Yandex.Disk Backup UI")
app.middleware("http")(ports.csrf_guard)
for _module in (status, control, access, folders, links, logs, guide, pages):
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
    for name in ("webui", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)


def main():
    ensure_utf8_stdio()
    _setup_logging()
    log.info("webui v%s старт: python %s, %s, проект %s", APP_VERSION,
             platform.python_version(), platform.platform(), paths.PROJECT)

    import uvicorn

    wanted = ports.PORT
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
    # Кто держит наш обычный порт: копия этой программы из другой папки (второй аккаунт) или чужая программа
    neighbour = ports.other_copy_on(wanted) if port != wanted else None
    if port != wanted:
        log.info("порт %d занят %s, работаю на %d", wanted,
                 "копией программы из другой папки" if neighbour else "другой программой", port)

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
        print(f"  {neighbour.get('folder')}")
        print(f"  Эта копия работает рядом, на {url}. У каждой копии свои ключ доступа, папка и прогресс.")
        print()
    elif port != wanted:
        print(f"  Порт {wanted} занят другой программой (в Linux так бывает со службой печати),")
        print(f"  поэтому программа работает на {url}")
        print()
    print("  Сервер запускается, браузер откроется автоматически.")
    print("  Чтобы остановить — закрой это окно или нажми Ctrl+C.")
    print()

    # Открыть браузер в фоне когда сервер готов (YADISK_NO_BROWSER=1 — не открывать: dev/CI)
    if os.getenv("YADISK_NO_BROWSER") != "1":
        threading.Thread(target=ports.open_browser_when_ready, args=(ports.HOST, port), daemon=True).start()

    # Порт уже занят нами в choose_port: uvicorn получает готовый сокет, и между
    # проверкой и запуском его никто не перехватит
    server = uvicorn.Server(uvicorn.Config(app, host=ports.HOST, port=port, log_level="warning"))
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
