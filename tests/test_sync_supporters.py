"""Список «Нас поддерживают» из DonationAlerts: очистка, слияние, страницы API."""

import io
import json
import urllib.error

import pytest

import supporters
from tools import sync_supporters as ss


def donation(i, username="Анна", message="Спасибо!", amount=500, currency="RUB",
             created_at="2026-10-15 09:00:00", message_type="text"):
    return {"id": i, "name": "donation", "username": username, "message_type": message_type,
            "message": message, "amount": amount, "currency": currency, "is_shown": 1,
            "created_at": created_at, "shown_at": None}


def test_clean_text_drops_links_and_mentions():
    text = "Спасибо! Подпишись https://spam.example/x и t.me/spamchan, пиши @spammer — www.ads.ru"
    assert ss.clean_text(text, 160) == "Спасибо! Подпишись и пиши"
    assert ss.clean_text("Вернула фото мамы", 160) == "Вернула фото мамы"
    assert ss.clean_text("  много \n строк  ", 160) == "много строк"
    assert ss.clean_text("x" * 300, 160).endswith("…") and len(ss.clean_text("x" * 300, 160)) == 160
    assert ss.clean_text(None, 10) == ""
    # «Яндекс.Диск» — не ссылка: название остаётся
    assert ss.clean_text("Спасибо за Яндекс.Диск", 160) == "Спасибо за Яндекс.Диск"


def test_donation_entry_maps_fields():
    entry = ss.donation_entry(donation(42, username="@ivan_tg", amount="250.50", currency="usd"))
    assert entry == {"id": "da-42", "name": "ivan_tg", "amount": 250.5, "currency": "USD",
                     "date": "2026-10-15", "message": "Спасибо!"}
    # Ссылка вместо имени — аноним; голосовое сообщение текстом не показываем
    entry = ss.donation_entry(donation(7, username="https://t.me/spam", message_type="audio"))
    assert entry["name"] == "Аноним" and entry["message"] is None
    assert ss.donation_entry(donation(8), no_message={"8"})["message"] is None
    assert ss.donation_entry({"username": "без номера"}) is None
    assert ss.donation_entry("мусор") is None
    assert ss.donation_entry(donation(9, created_at="2026-10-15T23:30:00+00:00"))["date"] == "2026-10-15"


def test_merge_keeps_manual_entries_and_hides_on_request():
    current = {
        "updated": "2026-10-01",
        "hidden": [2, "da-3"],
        "no_message": ["4"],
        "supporters": [
            {"name": "Друг из чата", "amount": 1000, "currency": "RUB", "date": "2026-10-02", "message": None},
            {"id": "da-99", "name": "Старый донат, которого в API уже нет"},
        ],
    }
    donations = [donation(1, "Анна", created_at="2026-10-10 10:00:00"),
                 donation(2, "Хам"), donation(3, "Спамер"),
                 donation(4, "Сергей", message="грубость", created_at="2026-10-12 10:00:00"),
                 donation(1, "Анна", created_at="2026-10-10 10:00:00")]  # повтор страницы
    new = ss.merge(current, donations, today="2026-10-15")
    names = [x["name"] for x in new["supporters"]]
    assert names == ["Сергей", "Анна", "Друг из чата"]
    assert new["supporters"][0]["message"] is None
    assert new["hidden"] == ["2", "3"] and new["no_message"] == ["4"]
    assert new["updated"] == "2026-10-15"
    # Программа принимает файл целиком, ничего не отбрасывая
    assert len(supporters.normalize(new)["supporters"]) == 3


def test_merge_keeps_updated_when_nothing_changed():
    first = ss.merge({}, [donation(1)], today="2026-10-15")
    again = ss.merge(json.loads(ss.dump(first)), [donation(1)], today="2026-10-20")
    assert again == first and again["updated"] == "2026-10-15"


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def pages(*pages_data):
    calls = []

    def opener(url, token):
        calls.append(url)
        page = int(url.rsplit("=", 1)[1])
        body = {"data": pages_data[page - 1], "meta": {"current_page": page, "last_page": len(pages_data)}}
        return FakeResp(json.dumps(body).encode())

    return opener, calls


def test_fetch_walks_all_pages():
    opener, calls = pages([donation(1), donation(2)], [donation(3)])
    got = ss.fetch_donations("t", opener=opener, pause=0)
    assert [d["id"] for d in got] == [1, 2, 3]
    assert calls[-1].endswith("page=2") and len(calls) == 2


def test_fetch_errors_are_fatal_only_for_a_bad_key():
    def denied(url, token):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    with pytest.raises(ss.SyncError) as e:
        ss.fetch_donations("t", opener=denied, pause=0)
    assert e.value.fatal

    def down(url, token):
        raise urllib.error.URLError("timed out")

    with pytest.raises(ss.SyncError) as e:
        ss.fetch_donations("t", opener=down, pause=0)
    assert not e.value.fatal

    def html(url, token):
        return FakeResp(b"<html>maintenance</html>")

    with pytest.raises(ss.SyncError):
        ss.fetch_donations("t", opener=html, pause=0)


def test_sync_writes_only_on_change(tmp_path, capsys):
    path = tmp_path / "supporters.json"
    path.write_text(json.dumps({"updated": "2026-10-01", "supporters": []}), encoding="utf-8")
    opener, _ = pages([donation(1, "Анна")])
    assert ss.sync("t", path=path, opener=opener, pause=0, today="2026-10-15") is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["supporters"][0]["name"] == "Анна" and data["updated"] == "2026-10-15"
    mtime = path.stat().st_mtime_ns
    opener, _ = pages([donation(1, "Анна")])
    assert ss.sync("t", path=path, opener=opener, pause=0, today="2026-10-16") is False
    assert path.stat().st_mtime_ns == mtime
    # Пробный запуск ничего не пишет
    opener, _ = pages([donation(1, "Анна"), donation(2, "Сергей")])
    assert ss.sync("t", path=path, opener=opener, pause=0, dry_run=True) is True
    assert "Сергей" not in path.read_text(encoding="utf-8")


def test_failed_fetch_leaves_file_alone(tmp_path):
    path = tmp_path / "supporters.json"
    path.write_text('{"supporters": []}', encoding="utf-8")

    def down(url, token):
        raise urllib.error.URLError("timed out")

    with pytest.raises(ss.SyncError):
        ss.sync("t", path=path, opener=down, pause=0)
    assert path.read_text(encoding="utf-8") == '{"supporters": []}'


def test_main_without_token_does_nothing(monkeypatch, capsys):
    monkeypatch.delenv(ss.TOKEN_ENV, raising=False)
    assert ss.main([]) == 0
    assert "Нет ключа" in capsys.readouterr().out


def test_main_exit_codes(monkeypatch):
    monkeypatch.setenv(ss.TOKEN_ENV, "t")

    def boom(fatal):
        def sync(*a, **kw):
            raise ss.SyncError("x", fatal=fatal)
        return sync

    # Ключ не подходит — запуск падает (GitHub пришлёт письмо), сбой сети — тихо ждём следующего
    monkeypatch.setattr(ss, "sync", boom(True))
    assert ss.main([]) == 1
    monkeypatch.setattr(ss, "sync", boom(False))
    assert ss.main([]) == 0
