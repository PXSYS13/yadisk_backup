"""Список «Нас поддерживают» из DonationAlerts: очистка, слияние, страницы API."""

import io
import json
import urllib.error
import urllib.request

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
    assert "top_min_rub" not in first


def test_merge_keeps_the_top_threshold_set_by_hand():
    new = ss.merge({"top_min_rub": 1000, "supporters": []}, [donation(1)], today="2026-10-15")
    assert new["top_min_rub"] == 1000
    assert list(new)[:3] == ["_как_заполнять", "updated", "top_min_rub"]
    assert supporters.normalize(new)["top_min_rub"] == 1000


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


def test_key_goes_only_to_donationalerts_over_https():
    assert ss._check_url(ss.API_URL + "?page=2") and ss._check_url(ss.TOKEN_URL)
    for url in ("http://www.donationalerts.com/api/v1/alerts/donations",
                "https://evil.example/api",
                "https://www.donationalerts.com.evil.example/api",
                "https://www.donationalerts.com@evil.example/api",
                "file:///etc/passwd"):
        with pytest.raises(ss.SyncError) as e:
            ss._request(url, "секрет")
        assert e.value.fatal and "секрет" not in str(e.value)


def test_redirects_stay_inside_donationalerts():
    handler = ss._DonationAlertsOnly()
    req = urllib.request.Request(ss.API_URL, headers={"Authorization": "Bearer t"})
    # Обычный urllib унёс бы заголовок с ключом на любой адрес
    for target in ("https://evil.example/steal", "http://www.donationalerts.com/api/v1/alerts/donations"):
        with pytest.raises(ss.SyncError) as e:
            handler.redirect_request(req, None, 302, "Found", {}, target)
        assert e.value.fatal
    moved = handler.redirect_request(req, None, 301, "Moved", {}, "https://donationalerts.com/api/v1/alerts/donations")
    assert moved.full_url.startswith("https://donationalerts.com/")
    assert moved.get_header("Authorization") == "Bearer t"


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


# ── Без ключей: страница виджета «Последние оповещения», сохранённая из браузера ──


def widget_item(alert_id, name, total, created, message="", alert_type="1", self_closing=False):
    """Пункт страницы виджета в разметке DonationAlerts (октябрь 2026)."""
    end = "/>" if self_closing else ">"
    msg = f'<div class="message-container b-last-events-widget__item--text">{message}</div>' if message else ""
    return (f'<div class="event b-last-events-widget__item " data-alert_type="{alert_type}" '
            f'data-alert_id="{alert_id}" data-amount_main="1"><div class="show-progress"></div>'
            f'<input type="hidden" name="date_created" value="{created}"{end}'
            f'<div class="b-last-events-widget__item--from _dalink"><svg><path d="M0"/></svg></div>'
            f'<div class="b-last-events-widget__item--inner"><div class="b-last-events-widget__item--title">'
            f'<span class="_name">{name}<img class="commission-covered-icon" src="/i.svg"></span> отправил '
            f'<span class="_sum">{total}</span></div>{msg}<div class="action-buttons-container-footer">'
            f'<button>Повторить оповещение</button><span class="date-container">1 час назад</span></div></div></div>')


def widget_page(*items):
    # В настоящей странице есть скрипты с ключом виджета — их разбор не читает
    return ("<html><head><script>var token = 'secret'; var _sum = '999 RUB';</script></head><body>"
            f'<div class="event-container b-last-events-widget__list">{"".join(items)}</div></body></html>')


def test_widget_page_gives_donations_like_the_api():
    page = widget_page(
        widget_item(11, "Мария", "1 000 RUB", "2026-10-18 21:16:28", message="Вернула фото &amp; видео"),
        widget_item(12, "@tg_nick", "0,30 USD", "2026-10-19 01:30:20", self_closing=True),
        widget_item(13, "Подписчик", "100 RUB", "2026-10-19 02:00:00", alert_type="4"),  # не донат
    )
    got = ss.parse_widget_html(page)
    assert [d["id"] for d in got] == ["11", "12"]
    entries = [ss.donation_entry(d) for d in got]
    assert entries[0] == {"id": "da-11", "name": "Мария", "amount": 1000, "currency": "RUB",
                          "date": "2026-10-18", "message": "Вернула фото & видео"}
    assert entries[1] == {"id": "da-12", "name": "tg_nick", "amount": 0.3, "currency": "USD",
                          "date": "2026-10-19", "message": None}
    assert ss.parse_widget_html("<html><body>Войдите в аккаунт</body></html>") == []


def test_widget_import_adds_new_and_keeps_older_donations(tmp_path, capsys):
    path = tmp_path / "supporters.json"
    path.write_text(json.dumps({
        "updated": "2026-10-01", "hidden": ["13"], "no_message": ["10"],
        "supporters": [
            {"name": "Друг из чата", "amount": 500, "currency": "RUB", "date": "2026-10-02", "message": None},
            # Старый донат: на странице виджета (последние 100) его уже нет — он должен остаться
            {"id": "da-10", "name": "Анна", "amount": 300, "currency": "RUB", "date": "2026-10-05",
             "message": "грубость"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    html_file = tmp_path / "lastdonations.html"
    html_file.write_text(widget_page(
        widget_item(12, "Сергей", "200 RUB", "2026-10-19 12:19:37"),
        widget_item(13, "Спамер", "10 RUB", "2026-10-19 12:20:00"),
        widget_item(11, "Мария", "1 000 RUB", "2026-10-18 21:16:28", message="Спасибо!"),
    ), encoding="utf-8")
    assert ss.import_widget(html_file, path=path, today="2026-10-19") is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [x["name"] for x in data["supporters"]] == ["Сергей", "Мария", "Анна", "Друг из чата"]
    assert data["supporters"][2]["message"] is None  # no_message действует и на старые записи
    assert data["updated"] == "2026-10-19"
    assert "новых: 3" in capsys.readouterr().out
    # Та же страница ещё раз — файл не меняется
    mtime = path.stat().st_mtime_ns
    assert ss.import_widget(html_file, path=path, today="2026-10-20") is False
    assert path.stat().st_mtime_ns == mtime
    assert len(supporters.normalize(data)["supporters"]) == 4


def test_widget_import_refuses_a_page_without_donations(tmp_path, capsys, monkeypatch):
    path = tmp_path / "supporters.json"
    path.write_text('{"supporters": []}', encoding="utf-8")
    monkeypatch.setattr(ss, "SUPPORTERS_FILE", path)
    page = tmp_path / "login.html"
    page.write_text("<html><body>Войдите в DonationAlerts</body></html>", encoding="utf-8")
    assert ss.main(["--from-widget", str(page)]) == 1
    assert "На странице нет донатов" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == '{"supporters": []}'


@pytest.mark.parametrize("text", [
    "Пишите t​.me/spamchan",       # нулевая ширина внутри ссылки
    "Пишите @​spamchan",           # и после @
    "Подписывайтесь@spamchan",          # @ сразу после слова
    "Спасибо...@spamchan",
    "Пишите ＠spamchan",                # широкий @
    "Бонусы на casino.bet",
    "Скидки на магазин.рф",
])
def test_links_and_mentions_cannot_sneak_through(text):
    cleaned = ss.clean_text(text, 160)
    assert "spamchan" not in cleaned and ".bet" not in cleaned and ".рф" not in cleaned


def test_incomplete_pages_never_shrink_the_list():
    """Пустая страница посреди списка или ответ без номера последней — список неполный: ошибка,
    иначе merge выкинул бы всех, кто дальше этой страницы."""
    def gap(url, token):
        page = int(url.rsplit("=", 1)[1])
        data = [donation(1)] if page == 1 else []
        return FakeResp(json.dumps({"data": data, "meta": {"last_page": 3}}).encode())

    with pytest.raises(ss.SyncError):
        ss.fetch_donations("t", opener=gap, pause=0)

    def no_meta(url, token):
        return FakeResp(json.dumps({"data": [donation(1)]}).encode())

    with pytest.raises(ss.SyncError):
        ss.fetch_donations("t", opener=no_meta, pause=0)


def test_empty_answer_does_not_wipe_the_supporters(tmp_path):
    path = tmp_path / "supporters.json"
    path.write_text(json.dumps({"supporters": [{"id": "da-1", "name": "Анна"}]}), encoding="utf-8")
    opener, _ = pages([])
    with pytest.raises(ss.SyncError):
        ss.sync("t", path=path, opener=opener, pause=0)
    assert json.loads(path.read_text(encoding="utf-8"))["supporters"][0]["name"] == "Анна"
