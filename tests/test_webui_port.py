"""Порт при запуске: 8000 держит чужая программа (в Linux — служба печати) или уже запущенная копия."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

import utils
import webapp.paths
import webapp.ports
import webui


class _StrictHTTPServer(HTTPServer):
    # Без SO_REUSEADDR: в Windows с ним два сокета могли бы делить один порт
    allow_reuse_address = False


class _Occupant:
    """Программа на 127.0.0.1:port, которая отвечает JSON по своим путям, а на остальные — 404."""

    def __init__(self, port: int, routes: dict[str, dict] | None = None):
        routes = routes or {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = routes.get(self.path)
                data = json.dumps(body if body is not None else {"detail": "Not Found"}).encode()
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = _StrictHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _two_free_ports() -> int:
    """Порт P, у которого свободен и следующий P+1."""
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port >= 65535:
            continue
        nxt = webapp.ports.bind_port(port + 1)
        if nxt is not None:
            nxt.close()
            return port
    pytest.skip("не нашлось двух свободных портов подряд")


def test_free_port_is_used_as_is():
    port = _two_free_ports()
    sock, chosen, running = webapp.ports.choose_port(port, 1)
    try:
        assert sock is not None and chosen == port and running is None
    finally:
        sock.close()


def test_foreign_program_on_port_moves_to_the_next_one():
    port = _two_free_ports()
    with _Occupant(port):  # как ghostscript-printer-app: на наши адреса отвечает 404
        sock, chosen, running = webapp.ports.choose_port(port, 5)
        try:
            assert sock is not None and chosen == port + 1 and running is None
        finally:
            sock.close()


def test_fixed_port_held_by_foreign_program_gives_up():
    port = _two_free_ports()
    with _Occupant(port):
        assert webapp.ports.choose_port(port, 1) == (None, port, None)


def test_running_copy_is_opened_instead_of_starting_a_second_one():
    port = _two_free_ports()
    # Та же папка — те же базы: вторую копию не запускаем, открываем первую
    hello = {"app": webapp.ports.APP_ID, "version": "2.7.3", "folder": str(webapp.paths.PROJECT)}
    with _Occupant(port, {"/api/hello": hello}):
        sock, chosen, running = webapp.ports.choose_port(port, 5)
    assert sock is None and chosen == port and running["folder"] == str(webapp.paths.PROJECT)


def test_copy_from_another_folder_runs_next_to_it():
    """Свой аккаунт и аккаунт жены — две папки программы: вторая работает рядом, на следующем порту."""
    port = _two_free_ports()
    hello = {"app": webapp.ports.APP_ID, "version": "2.7.3", "folder": "/home/user/yadisk_backup_wife"}
    with _Occupant(port, {"/api/hello": hello}):
        sock, chosen, running = webapp.ports.choose_port(port, 5)
        try:
            assert sock is not None and chosen == port + 1 and running is None
            assert webapp.ports.other_copy_on(port)["folder"] == "/home/user/yadisk_backup_wife"
        finally:
            sock.close()


def test_same_folder_compares_paths_not_strings():
    project = str(webapp.paths.PROJECT)
    assert webapp.ports.same_folder({"folder": project})
    assert webapp.ports.same_folder({"folder": project + "/"})
    assert not webapp.ports.same_folder({"folder": project + "_wife"})
    # До 2.6.1 папку не сообщали — считаем той же: лучше не запуститься, чем делить базы
    assert webapp.ports.same_folder({"app": webapp.ports.APP_ID})
    assert webapp.ports.same_folder(None)


def test_copy_of_an_old_version_is_recognized_by_status():
    port = _two_free_ports()
    with _Occupant(port, {"/api/status": {"tasks": {}, "pipeline": {}}}):  # до 2.6.1 нет /api/hello
        sock, chosen, running = webapp.ports.choose_port(port, 5)
    assert sock is None and chosen == port and running == {"app": webapp.ports.APP_ID}


def test_other_app_with_hello_is_not_mistaken_for_us():
    port = _two_free_ports()
    with _Occupant(port, {"/api/hello": {"app": "something-else"}}):
        sock, chosen, running = webapp.ports.choose_port(port, 5)
        try:
            assert chosen == port + 1 and running is None
        finally:
            sock.close()


def test_listener_only_on_ipv6_localhost_is_skipped():
    port = _two_free_ports()
    try:
        v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        v6.bind(("::1", port))
    except OSError:
        pytest.skip("IPv6 недоступен")
    v6.listen()
    try:
        sock, chosen, running = webapp.ports.choose_port(port, 5)
        try:
            assert chosen == port + 1 and running is None
        finally:
            sock.close()
    finally:
        v6.close()


def test_hello_names_the_app():
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    data = client.get("/api/hello").json()
    assert data["app"] == webapp.ports.APP_ID and data["version"] == utils.APP_VERSION and data["folder"]


def test_moved_port_is_trusted_by_the_csrf_guard(monkeypatch):
    for name in ("PORT", "_ALLOWED_ORIGINS", "_ALLOWED_HOSTS"):
        monkeypatch.setattr(webapp.ports, name, getattr(webapp.ports, name))  # вернуть после теста
    webapp.ports.use_port(8001)
    assert TestClient(webui.app, headers={"Host": "127.0.0.1:8001"}).get("/api/hello").status_code == 200
    assert TestClient(webui.app, headers={"Host": "127.0.0.1:8000"}).get("/api/hello").status_code == 403
    client = TestClient(webui.app, headers={"Host": "127.0.0.1:8001", "Origin": "http://127.0.0.1:8001"})
    assert client.post("/api/settings", json={}).status_code == 200
