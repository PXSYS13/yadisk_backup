"""Где работать и кого пускать: выбор порта при запуске и защита от чужих сайтов.

Если 8000 держит чужая программа (в Linux — служба печати ghostscript-printer-app),
программа берёт следующий свободный порт. Если там уже работает её копия, вторая
не запускается: две копии мешали бы друг другу в одних и тех же базах.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import time

from fastapi.responses import JSONResponse

from utils import IS_WINDOWS
from webapp import paths

log = logging.getLogger("webui")

HOST = "127.0.0.1"
# Так вторая копия узнаёт первую на занятом порте (/api/hello)
APP_ID = "yadisk-backup"
PORT_TRIES = 20


def _port_from_env() -> int:
    """Порт интерфейса. YADISK_PORT — для разработки и второй копии программы."""
    try:
        port = int(os.environ.get("YADISK_PORT") or 8000)
    except ValueError:
        return 8000
    return port if 1024 <= port <= 65535 else 8000


# paths уже прочитал .env: YADISK_PORT оттуда тоже учитывается
PORT = _port_from_env()
# Порт задан явно — не подменяем его, даже если он занят
PORT_FIXED = bool((os.environ.get("YADISK_PORT") or "").strip())

# ── Защита от чужих сайтов: Host и Origin ──

_ALLOWED_ORIGINS = {f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"}
_ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}", "localhost", "127.0.0.1"}


def use_port(port: int) -> None:
    """Порт выбран при запуске: проверка Host и Origin должна пускать именно его."""
    global PORT, _ALLOWED_ORIGINS, _ALLOWED_HOSTS
    PORT = port
    _ALLOWED_ORIGINS = {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    _ALLOWED_HOSTS = {f"localhost:{port}", f"127.0.0.1:{port}", "localhost", "127.0.0.1"}


async def csrf_guard(request, call_next):
    """Блокирует запросы с чужих сайтов (CSRF) и DNS-rebinding.

    Host проверяется для ВСЕХ методов: иначе страница с чужого домена, перепривязанного
    на 127.0.0.1, прочитала бы /api/status (логин, пути, имена файлов, логи).
    Origin — для state-changing методов.
    """
    host = (request.headers.get("host") or "").lower()
    if host and host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "Forbidden host"}, status_code=403)
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin") or ""
        if origin and origin not in _ALLOWED_ORIGINS:
            return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
    return await call_next(request)


# ── Выбор порта ──

_ADDR_IN_USE = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}


def bind_port(port: int) -> socket.socket | None:
    """Занимает 127.0.0.1:port так же, как занял бы uvicorn; None — порт занят или запрещён.

    SO_REUSEADDR — только не в Windows: в Linux и macOS без него порт после перезапуска
    держали бы старые соединения, а в Windows с ним можно сесть на порт чужой программы.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if not IS_WINDOWS:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, port))
    except OSError:
        sock.close()
        return None
    return sock


def _ipv6_port_taken(port: int) -> bool:
    """Порт на ::1 держит другая программа — адрес localhost:порт в браузере открыл бы её."""
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:  # IPv6 выключен
        return False
    try:
        if not IS_WINDOWS:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("::1", port))
    except OSError as e:
        return e.errno in _ADDR_IN_USE
    finally:
        sock.close()
    return False


def _ask_port(port: int, path: str, timeout: float) -> dict | None:
    """GET http://127.0.0.1:port/path: JSON-объект при ответе 200, иначе None."""
    import http.client
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        data = json.loads(resp.read())
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    return data if isinstance(data, dict) else None


def _our_copy_on(port: int) -> dict | None:
    """Порт держит другая копия этой программы? Её /api/hello или None."""
    hello = _ask_port(port, "/api/hello", 2)
    if hello is not None:
        return hello if hello.get("app") == APP_ID else None
    # Версии до 2.6.1 не знают /api/hello — узнаём их по /api/status
    status = _ask_port(port, "/api/status", 5)
    return {"app": APP_ID} if status is not None and "tasks" in status else None


def same_folder(info: dict | None) -> bool:
    """Копия программы на порту запущена из этой же папки, то есть над этими же базами.

    Версии до 2.6.1 папку не сообщают — считаем, что из этой: лучше не запуститься,
    чем открыть вторую копию над теми же базами.
    """
    folder = str((info or {}).get("folder") or "")
    if not folder:
        return True
    return os.path.normcase(os.path.abspath(folder)) == os.path.normcase(os.path.abspath(str(paths.PROJECT)))


def other_copy_on(port: int) -> dict | None:
    """На порту работает эта же программа, но из другой папки — например, для второго аккаунта."""
    info = _our_copy_on(port)
    return info if info is not None and not same_folder(info) else None


def choose_port(first: int, tries: int) -> tuple[socket.socket | None, int, dict | None]:
    """Где запускаться.

    (сокет, порт, None) — порт свободен и уже занят нами; (None, порт, сведения) — там
    работает эта программа из этой же папки; (None, first, None) — все порты заняты.
    Вторую копию над теми же базами не запускаем: две копии мешали бы друг другу. Копия
    из другой папки — это другой аккаунт со своими ключом, базами и папкой для файлов:
    рядом с ней можно работать, поэтому просто берём следующий порт.
    """
    for port in range(first, min(first + tries, 65536)):
        sock = bind_port(port)
        if sock is not None:
            if not _ipv6_port_taken(port):
                return sock, port, None
            sock.close()
            continue
        ours = _our_copy_on(port)
        if ours is not None and same_folder(ours):
            return None, port, ours
    return None, first, None


def show_running_copy(port: int, info: dict) -> None:
    """Программа из этой папки уже запущена — показываем её, а не запускаем вторую."""
    url = f"http://{HOST}:{port}"
    print(f"  Программа уже работает в другом окне: {url}")
    print("  Открываю её в браузере. Это окно можно закрыть.")
    log.info("порт %d: уже работает копия программы из этой папки (%s), вторую не запускаю", port,
             info.get("version") or "версия до 2.6.1")
    if os.getenv("YADISK_NO_BROWSER") != "1":
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass


def open_browser_when_ready(host: str, port: int):
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
