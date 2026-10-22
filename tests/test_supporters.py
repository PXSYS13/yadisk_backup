"""«Нас поддерживают»: проверка списка, GitHub с запасной копией, эндпоинт."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import supporters
import utils
import webui

ROOT = Path(__file__).resolve().parent.parent


def test_normalize_keeps_good_records_and_drops_junk():
    raw = {
        "updated": "2026-10-15",
        "supporters": [
            {"name": "  Анна\nП. ", "amount": 500, "currency": "RUB", "date": "2026-10-10", "message": "Спасибо!"},
            "мусор",
            {"name": "", "amount": "много", "currency": "рубли", "date": "вчера"},
            {"name": "Ольга", "amount": 250.5, "date": "2026-10-14", "message": "x" * 400},
            {"name": "Б" * 100, "amount": True, "date": "2026-02-30"},
        ],
    }
    data = supporters.normalize(raw)
    assert data["updated"] == "2026-10-15"
    names = [x["name"] for x in data["supporters"]]
    # Новые сверху; записи без даты — в конце
    assert names[:2] == ["Ольга", "Анна П."]
    anon = data["supporters"][2]
    assert anon == {"name": "Аноним", "amount": None, "currency": "RUB", "date": None, "message": None}
    long = data["supporters"][3]
    assert len(long["name"]) == supporters.MAX_NAME and long["name"].endswith("…")
    assert long["amount"] is None and long["date"] is None  # bool — не сумма, 30 февраля — не дата
    assert data["supporters"][0]["amount"] == 250.5
    assert len(data["supporters"][0]["message"]) == supporters.MAX_MESSAGE


def test_normalize_survives_garbage():
    for raw in (None, [], "текст", {"supporters": "нет"}, {"updated": 5}):
        assert supporters.normalize(raw) == {"updated": None, "supporters": []}


def test_count_people_merges_repeat_donations_but_not_anonymous():
    items = supporters.normalize({"supporters": [
        {"name": "Анна"}, {"name": "анна"}, {"name": "Сергей"}, {"name": ""}, {"name": "Аноним"}]})["supporters"]
    assert supporters.count_people(items) == 4


def test_repository_file_is_valid():
    # Файл правят руками: он должен оставаться правильным JSON со списком
    raw = json.loads((ROOT / "supporters.json").read_text(encoding="utf-8"))
    assert isinstance(raw["supporters"], list)
    data = supporters.normalize(raw)
    assert len(data["supporters"]) == len(raw["supporters"]), "в supporters.json есть записи, которые программа отбросит"
    assert data["updated"]


def test_github_first_then_cache_then_local(monkeypatch, tmp_path):
    local = tmp_path / "supporters.json"
    local.write_text(json.dumps({"updated": "2026-10-01", "supporters": [{"name": "Местный"}]}), encoding="utf-8")
    monkeypatch.setattr(supporters, "LOCAL_FILE", local)
    calls = []
    remote = {"value": {"updated": "2026-10-15", "supporters": [{"name": "С GitHub"}]}}

    def fake_fetch(url=None):
        calls.append(1)
        return supporters.normalize(remote["value"]) if remote["value"] else None

    monkeypatch.setattr(supporters, "fetch_remote", fake_fetch)
    first = supporters.get_supporters(now=0)
    assert first["source"] == "github" and first["supporters"][0]["name"] == "С GitHub"
    # Через минуту — из кеша, GitHub не трогаем
    assert supporters.get_supporters(now=60) is first and len(calls) == 1
    # Через час GitHub не ответил — остаётся последний полученный список, а не старая копия
    remote["value"] = None
    stale = supporters.get_supporters(now=supporters.CACHE_SEC + 1)
    assert stale["supporters"][0]["name"] == "С GitHub" and len(calls) == 2


def test_falls_back_to_bundled_copy(monkeypatch, tmp_path):
    local = tmp_path / "supporters.json"
    local.write_text(json.dumps({"updated": "2026-10-01", "supporters": [{"name": "Местный"}]}), encoding="utf-8")
    monkeypatch.setattr(supporters, "LOCAL_FILE", local)
    monkeypatch.setattr(supporters, "fetch_remote", lambda url=None: None)
    data = supporters.get_supporters(now=0)
    assert data["source"] == "local" and data["supporters"][0]["name"] == "Местный"
    # Без копии и без сети — пустой список, а не ошибка
    local.unlink()
    monkeypatch.setattr(supporters, "_cache", None)
    assert supporters.get_supporters(now=0)["supporters"] == []


def test_fetch_remote_rejects_big_or_broken_answers(monkeypatch):
    class Resp:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body
            self.raw = self

        def read(self, n, decode_content=True):
            return self._body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    answers = {}
    monkeypatch.setattr(supporters.requests, "get", lambda url, **kw: answers["r"])
    answers["r"] = Resp(200, json.dumps({"supporters": [{"name": "Анна"}]}).encode())
    assert supporters.fetch_remote("https://example.test/s.json")["supporters"][0]["name"] == "Анна"
    answers["r"] = Resp(200, b" " * (supporters.MAX_BYTES + 10))
    assert supporters.fetch_remote("https://example.test/s.json") is None
    answers["r"] = Resp(404, b"Not Found")
    assert supporters.fetch_remote("https://example.test/s.json") is None
    answers["r"] = Resp(200, b"<html>")
    assert supporters.fetch_remote("https://example.test/s.json") is None
    assert supporters.fetch_remote("") is None


def test_endpoint_and_demo(monkeypatch):
    client = TestClient(webui.app, headers={"Host": "localhost:8000"})
    res = client.get("/api/supporters").json()
    assert res["ok"] is True and res["supporters"] == [] and res["people"] == 0
    assert res["donation_url"] == utils.DONATION_URL
    # Демо-список — только с YADISK_DEMO=1
    assert client.get("/api/supporters?demo=1").json()["supporters"] == []
    monkeypatch.setenv("YADISK_DEMO", "1")
    demo = client.get("/api/supporters?demo=1").json()
    assert demo["source"] == "demo" and demo["people"] == len(demo["supporters"]) == 12


def test_remote_answer_must_be_a_supporters_list(monkeypatch):
    """«[]», «null» или ошибка прокси вместо списка не заменяют хороший список пустым на 15 минут."""
    class Resp:
        def __init__(self, body):
            self.status_code = 200
            self._body = body
            self.raw = self

        def read(self, n, decode_content=True):
            return self._body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    for body in (b"[]", b"null", b'{"error": "blocked by proxy"}', b'{"supporters": {"a": 1}}'):
        monkeypatch.setattr(supporters.requests, "get", lambda url, body=body, **kw: Resp(body))
        assert supporters.fetch_remote("https://example.test/s.json") is None, body


def test_many_donations_do_not_push_out_newer_manual_records():
    """Донаты в файле идут первыми: обрезка до сортировки теряла свежие записи, добавленные руками."""
    donations = [{"id": f"da-{i}", "name": f"Донор {i}", "date": "2026-01-01"} for i in range(supporters.MAX_ITEMS)]
    manual = {"name": "Ручная запись", "date": "2026-10-20"}
    got = supporters.normalize({"supporters": [*donations, manual]})["supporters"]
    assert len(got) == supporters.MAX_ITEMS and got[0]["name"] == "Ручная запись"


def test_invisible_characters_are_dropped_from_names_and_messages():
    raw = {"supporters": [{"name": "Ан​на‮", "message": "t​.me/канал⁦ спасибо"}]}
    item = supporters.normalize(raw)["supporters"][0]
    assert item["name"] == "Анна" and "​" not in item["message"] and "⁦" not in item["message"]
