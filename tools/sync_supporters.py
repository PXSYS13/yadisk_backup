"""Обновляет supporters.json по донатам из DonationAlerts.

Два способа, итог одинаковый (записи с id «da-<номер доната>»):

1. Без ключей, вручную. Откройте в браузере, где вы вошли в DonationAlerts, страницу
   виджета «Последние оповещения» (WIDGET_URL ниже), сохраните её (Ctrl+S, «только HTML»)
   и запустите:

       python tools/sync_supporters.py --from-widget "путь/к/сохранённой/странице.html"

   Виджет показывает последние 100 донатов, поэтому записи, которых на странице уже нет,
   остаются в списке. Ключ виджета в файл списка не попадает: берутся только имя, сумма,
   дата, сообщение и номер доната. Сохранённую страницу в репозиторий не кладите.

2. Автоматически: GitHub Actions по расписанию (.github/workflows/supporters.yml) берёт
   все донаты через API DonationAlerts и переписывает их часть списка. Нужен ключ в
   секрете репозитория DONATIONALERTS_TOKEN; получить его один раз:

       python tools/sync_supporters.py --get-token

   Проверить без записи в файл: DONATIONALERTS_TOKEN=... python tools/sync_supporters.py --dry-run

Записи, которые автор добавил руками (без "id"), остаются как есть. Программы людей
подхватывают свежий supporters.json с GitHub сами — так имя поддержавшего появляется в
разделе «Нас поддерживают».

Только стандартная библиотека: в Actions не нужно ставить зависимости проекта.

Скрыть донат целиком — добавить его номер в "hidden", скрыть только сообщение — в
"no_message" (номер — число из "id" записи: "da-123" → 123).
"""

from __future__ import annotations

import argparse
import getpass
import html
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPPORTERS_FILE = ROOT / "supporters.json"

API_URL = "https://www.donationalerts.com/api/v1/alerts/donations"
# Виджет «Последние оповещения»: открывается только у того, кто вошёл в DonationAlerts
WIDGET_URL = "https://www.donationalerts.com/widget/lastdonations?alert_type=1&limit=100"
AUTHORIZE_URL = "https://www.donationalerts.com/oauth/authorize"
TOKEN_URL = "https://www.donationalerts.com/oauth/token"
SCOPE = "oauth-donation-index"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
TOKEN_ENV = "DONATIONALERTS_TOKEN"
# Ключ и секрет приложения уходят только сюда — по https и без переходов на чужие адреса
ALLOWED_HOSTS = frozenset({"www.donationalerts.com", "donationalerts.com"})

ID_PREFIX = "da-"
ANONYMOUS = "Аноним"
# Те же пределы, что в supporters.py: длиннее программа всё равно обрежет
MAX_NAME = 40
MAX_MESSAGE = 160
MAX_PAGES = 200
# DonationAlerts разрешает 60 запросов в минуту
PAGE_PAUSE_SEC = 1.1
TIMEOUT_SEC = 20
USER_AGENT = "yadisk_backup-supporters-sync"

HOWTO = (
    "Донаты DonationAlerts (записи с id «da-…») добавляет tools/sync_supporters.py: --from-widget "
    "со страницей виджета «Последние оповещения», сохранённой из браузера, или GitHub Actions по ключу. "
    "Записи без id можно добавлять руками: name — имя, как человек подписался (без имени — «Аноним»), "
    "amount — сумма числом, currency — RUB/USD/EUR, date — ГГГГ-ММ-ДД, message — короткие слова или null. "
    "Скрыть донат целиком — добавьте его номер в hidden (da-123 → 123), скрыть только сообщение — "
    "в no_message. Программы людей подхватят список с GitHub в течение получаса после того, как он попадёт "
    "в main."
)

# Ссылки и @упоминания вырезаем: список видят все пользователи программы, рекламе там не место
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_DOMAIN_RE = re.compile(
    r"(?i)\b[\w-]+(?:\.[\w-]+)*\.(?:ru|su|com|net|org|me|io|gg|tv|cc|ly|link|xyz|top|info|site|online|shop)"
    r"\b(?:/\S*)?"
)
_MENTION_RE = re.compile(r"(?<![\w.])@\w{2,}")


class SyncError(Exception):
    """Ошибка, после которой список обновлять нельзя. fatal — нужен человек: ключ не подходит
    или DonationAlerts уводит на чужой адрес."""

    def __init__(self, text: str, fatal: bool = False):
        super().__init__(text)
        self.fatal = fatal


def clean_text(value, limit: int, keep_mentions: bool = False) -> str:
    """Одна строка без ссылок и @упоминаний, не длиннее limit.

    keep_mentions — для имени: «@ivan» остаётся «ivan», человек не становится анонимом.
    """
    if not isinstance(value, str):
        return ""
    text = _URL_RE.sub(" ", value)
    text = _DOMAIN_RE.sub(" ", text)
    text = _MENTION_RE.sub((lambda m: m.group(0)[1:]) if keep_mentions else " ", text)
    text = " ".join(text.split()).strip(" ,.;:-–—")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _id_number(value) -> str | None:
    """Номер доната из 123, "123" или "da-123"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        value = value.strip().removeprefix(ID_PREFIX)
        return value if value.isdigit() else None
    return None


def _id_set(values) -> set[str]:
    return {n for n in map(_id_number, values if isinstance(values, list) else []) if n}


def _date(created_at) -> str | None:
    """«2026-10-15 09:00:00» (или ISO с зоной) → «2026-10-15»."""
    if not isinstance(created_at, str):
        return None
    text = created_at.strip().replace("T", " ")
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return None


def _amount(value) -> int | float | None:
    if isinstance(value, str):
        try:
            value = float(value.replace(",", "."))
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 10 ** 9:
        return None
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


def donation_entry(donation: dict, no_message: set[str] | None = None) -> dict | None:
    """Запись supporters.json из доната DonationAlerts (None — не донат или без номера)."""
    if not isinstance(donation, dict):
        return None
    number = _id_number(donation.get("id"))
    if not number:
        return None
    message = None
    if donation.get("message_type") in ("text", None) and number not in (no_message or set()):
        message = clean_text(donation.get("message"), MAX_MESSAGE) or None
    currency = donation.get("currency")
    return {
        "id": ID_PREFIX + number,
        "name": clean_text(donation.get("username"), MAX_NAME, keep_mentions=True) or ANONYMOUS,
        "amount": _amount(donation.get("amount")),
        "currency": currency.upper() if isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency) else "RUB",
        "date": _date(donation.get("created_at")),
        "message": message,
    }


def _sort_key(entry: dict):
    number = _id_number(entry.get("id")) or "0"
    return (entry.get("date") or "", int(number))


def merge(current: dict, donations: list[dict], today: str | None = None,
          keep_existing: bool = False) -> dict:
    """Новый supporters.json: ручные записи + донаты из DonationAlerts без скрытых.

    donations — все донаты (API): прежние записи «da-…» заменяются ими целиком. keep_existing —
    только последние донаты (страница виджета): прежние записи «da-…» остаются, новые добавляются.
    "updated" меняется, только если изменился сам список, — иначе файл остаётся прежним
    и GitHub Actions ничего не коммитит.
    """
    current = current if isinstance(current, dict) else {}
    hidden = _id_set(current.get("hidden"))
    no_message = _id_set(current.get("no_message"))
    old = current.get("supporters") if isinstance(current.get("supporters"), list) else []
    manual = [x for x in old if isinstance(x, dict) and not str(x.get("id", "")).startswith(ID_PREFIX)]
    fresh: dict[str, dict] = {}
    if keep_existing:
        for x in old:
            number = _id_number(x.get("id")) if isinstance(x, dict) and str(x.get("id", "")).startswith(ID_PREFIX) else None
            if number and number not in hidden:
                fresh[ID_PREFIX + number] = dict(x, message=None) if number in no_message else x
    for donation in donations:
        entry = donation_entry(donation, no_message)
        if entry and entry["id"].removeprefix(ID_PREFIX) not in hidden:
            fresh[entry["id"]] = entry
    auto = sorted(fresh.values(), key=_sort_key, reverse=True)
    supporters = auto + manual
    changed = supporters != old
    return {
        "_как_заполнять": HOWTO,
        "updated": (today or date.today().isoformat()) if changed else current.get("updated"),
        "hidden": sorted(hidden, key=int),
        "no_message": sorted(no_message, key=int),
        "supporters": supporters,
    }


# ── Страница виджета «Последние оповещения», сохранённая из браузера ─────────

_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                        "source", "track", "wbr"})
_WIDGET_FIELDS = {("span", "_name"): "username", ("span", "_sum"): "sum", ("div", "message-container"): "message"}
_SUM_RE = re.compile(r"^\s*([\d\s  .,]+?)\s*([A-Za-z]{3})\s*$")
# Тип оповещения «донат»: остальные (подписки и прочее) в список не идут
DONATION_ALERT_TYPE = "1"


class _WidgetParser(HTMLParser):
    """Донаты со страницы виджета: div.b-last-events-widget__item с data-alert_id, внутри
    input[name=date_created] (время UTC), span._name, span._sum и div.message-container.

    Скрипты, кнопки и адреса страницы (в них ключ виджета) не читаются вовсе.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._field: str | None = None
        self._depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self._field:
            if tag not in _VOID_TAGS:
                self._depth += 1
            return
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if "b-last-events-widget__item" in classes and a.get("data-alert_id"):
            self.items.append({"id": a["data-alert_id"], "alert_type": a.get("data-alert_type"),
                               "message_type": "text"})
            return
        if not self.items:
            return
        if tag == "input" and a.get("name") == "date_created":
            self.items[-1]["created_at"] = a.get("value")
            return
        for (want_tag, cls), field in _WIDGET_FIELDS.items():
            if tag == want_tag and cls in classes:
                self._field, self._depth, self._buf = field, 1, []
                return

    def handle_startendtag(self, tag, attrs):
        # <input …/> и <img …/>: только начало, конца у пустых тегов нет; <div/> внутри поля — пустота
        if tag in _VOID_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if not self._field or tag in _VOID_TAGS:
            return
        self._depth -= 1
        if self._depth <= 0:
            self.items[-1][self._field] = " ".join("".join(self._buf).split())
            self._field = None

    def handle_data(self, data):
        if self._field:
            self._buf.append(data)


def _widget_sum(text) -> tuple[float | None, str | None]:
    """«1 000 RUB» → (1000.0, "RUB"), «0,30 USD» → (0.3, "USD")."""
    m = _SUM_RE.match(text or "")
    if not m:
        return None, None
    digits = re.sub(r"[\s  ]", "", m.group(1)).replace(",", ".")
    try:
        return float(digits), m.group(2).upper()
    except ValueError:
        return None, None


def parse_widget_html(text: str) -> list[dict]:
    """Донаты со страницы виджета в том же виде, что отдаёт API (id, username, amount …)."""
    parser = _WidgetParser()
    parser.feed(text)
    parser.close()
    out = []
    for item in parser.items:
        if item.get("alert_type") not in (None, DONATION_ALERT_TYPE):
            continue
        amount, currency = _widget_sum(item.get("sum"))
        out.append({"id": item["id"], "username": item.get("username"), "amount": amount,
                    "currency": currency, "created_at": item.get("created_at"),
                    "message": item.get("message") or None, "message_type": "text"})
    return out


def import_widget(html_path: Path, path: Path = SUPPORTERS_FILE, dry_run: bool = False,
                  today: str | None = None) -> bool:
    """Добавляет в список донаты со страницы виджета. True — список изменился."""
    try:
        text = Path(html_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise SyncError(f"Не открыть {html_path}: {e}", fatal=True) from e
    donations = parse_widget_html(text)
    if not donations:
        raise SyncError("На странице нет донатов. Откройте страницу виджета в браузере, где вы вошли в "
                        f"DonationAlerts ({WIDGET_URL}), дождитесь списка и сохраните её ещё раз.", fatal=True)
    try:
        current = load_current(path)
    except ValueError as e:
        raise SyncError(f"{path.name} — не JSON ({e}): исправьте файл руками", fatal=True) from e
    known = {_id_number(x.get("id")) for x in current.get("supporters") or [] if isinstance(x, dict)}
    new = merge(current, donations, today=today, keep_existing=True)
    text_out = dump(new)
    try:
        old_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        old_text = ""
    changed = text_out != old_text
    added = sum(1 for d in donations if _id_number(d.get("id")) not in known)
    shown = sum(1 for x in new["supporters"] if str(x.get("id", "")).startswith(ID_PREFIX))
    print(f"Донатов на странице: {len(donations)}, новых: {added}, в списке: {shown}, "
          f"скрыто: {len(new['hidden'])}, записей вручную: {len(new['supporters']) - shown}")
    if changed and not dry_run:
        path.write_text(text_out, encoding="utf-8", newline="\n")
    print("Список изменился" if changed else "Без изменений", "(пробный запуск, файл не тронут)" if dry_run else "")
    return changed


def _check_url(url: str) -> str:
    """Адрес DonationAlerts по https — или SyncError: ключ на чужой адрес не отправляем."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        raise SyncError(f"{url} — не адрес DonationAlerts по https: ключ туда не отправляю", fatal=True)
    return url


class _DonationAlertsOnly(urllib.request.HTTPRedirectHandler):
    """Переадресация только по https внутри DonationAlerts.

    Обычный urllib переносит на новый адрес все заголовки, Authorization с ключом тоже, —
    даже на чужой сайт и по http.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_DonationAlertsOnly)


def _urlopen(req: urllib.request.Request):
    _check_url(req.full_url)
    return _OPENER.open(req, timeout=TIMEOUT_SEC)


def _request(url: str, token: str):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": USER_AGENT})
    return _urlopen(req)


def fetch_donations(token: str, opener=None, pause: float = PAGE_PAUSE_SEC) -> list[dict]:
    """Все донаты со всех страниц API. SyncError — если хоть одна страница не пришла."""
    opener = opener or _request
    out: list[dict] = []
    page = 1
    while page <= MAX_PAGES:
        url = f"{API_URL}?page={page}"
        try:
            with opener(url, token) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SyncError(f"DonationAlerts не принял ключ (HTTP {e.code}): получите новый, "
                                f"python tools/sync_supporters.py --get-token", fatal=True) from e
            raise SyncError(f"DonationAlerts ответил HTTP {e.code} на странице {page}") from e
        except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError) as e:
            raise SyncError(f"Не удалось получить страницу {page}: {e}") from e
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise SyncError(f"Неожиданный ответ DonationAlerts на странице {page}")
        out.extend(x for x in data if isinstance(x, dict))
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        last = meta.get("last_page")
        if not data or not isinstance(last, int) or page >= last:
            break
        page += 1
        if pause:
            time.sleep(pause)
    return out


def load_current(path: Path = SUPPORTERS_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise SyncError(f"{path.name}: внутри не объект JSON — исправьте файл руками", fatal=True)
    return data


def dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def sync(token: str, path: Path = SUPPORTERS_FILE, dry_run: bool = False, opener=None,
         pause: float = PAGE_PAUSE_SEC, today: str | None = None) -> bool:
    """Обновляет файл. True — список изменился (и записан, если не dry_run)."""
    try:
        current = load_current(path)
    except ValueError as e:
        raise SyncError(f"{path.name} — не JSON ({e}): исправьте файл руками", fatal=True) from e
    donations = fetch_donations(token, opener=opener, pause=pause)
    new = merge(current, donations, today=today)
    text = dump(new)
    try:
        old_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        old_text = ""
    changed = text != old_text
    shown = sum(1 for x in new["supporters"] if str(x.get("id", "")).startswith(ID_PREFIX))
    print(f"Донатов в DonationAlerts: {len(donations)}, в списке: {shown}, "
          f"скрыто: {len(new['hidden'])}, записей вручную: {len(new['supporters']) - shown}")
    if changed and not dry_run:
        path.write_text(text, encoding="utf-8", newline="\n")
    print("Список изменился" if changed else "Без изменений", "(пробный запуск, файл не тронут)" if dry_run else "")
    return changed


# ── Ключ DonationAlerts: один раз, на компьютере автора ──────────────────────

def get_token() -> int:
    """Проводит авторизацию в DonationAlerts и печатает ключ для секрета GitHub."""
    print("Ключ DonationAlerts для автоматического списка «Нас поддерживают».\n")
    print("1. Откройте https://www.donationalerts.com/application/clients и нажмите «Создать новое приложение».")
    print(f"2. Название — любое, URL перенаправления — {REDIRECT_URI}")
    print("3. Скопируйте сюда ID приложения и API-ключ (секрет приложения).\n")
    client_id = input("ID приложения: ").strip()
    client_secret = getpass.getpass("API-ключ приложения (не отображается при вводе): ").strip()
    if not client_id.isdigit() or not client_secret:
        print("ID — это число, API-ключ — длинная строка. Запустите ещё раз.")
        return 2
    state = secrets.token_urlsafe(16)
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — имя задаёт http.server
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] == state and query.get("code"):
                got["code"] = query["code"][0]
                text = "Готово. Вернитесь в окно терминала — ключ уже там."
            else:
                got["error"] = query.get("error", ["нет кода авторизации"])[0]
                text = "Не получилось: " + html.escape(got["error"])
            body = f"<meta charset='utf-8'><p style='font:18px sans-serif;margin:40px'>{text}</p>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    params = urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code",
        "scope": SCOPE, "state": state})
    url = f"{AUTHORIZE_URL}?{params}"
    with http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler) as server:
        server.timeout = 300
        print("\nОткрываю браузер. Войдите в DonationAlerts и нажмите «Разрешить».")
        print(f"Если браузер не открылся, откройте ссылку вручную:\n{url}\n")
        webbrowser.open(url)
        server.handle_request()
    if "code" not in got:
        print(f"Авторизация не прошла: {got.get('error', 'истекло 5 минут')}")
        return 1
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI, "code": got["code"]}).encode()
    req = urllib.request.Request(TOKEN_URL, data=form, headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT})
    try:
        with _urlopen(req) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
    except (SyncError, urllib.error.URLError, OSError, ValueError) as e:
        print(f"DonationAlerts не выдал ключ: {e}")
        return 1
    token = answer.get("access_token") if isinstance(answer, dict) else None
    if not token:
        print("DonationAlerts не выдал ключ: в ответе нет access_token")
        return 1
    try:
        count = len(fetch_donations(token, pause=PAGE_PAUSE_SEC))
        print(f"\nКлюч работает: донатов найдено — {count}.")
    except SyncError as e:
        print(f"\nКлюч получен, но проверка не прошла: {e}")
    days = int(answer.get("expires_in") or 0) // 86400
    print(f"Срок действия ключа: {days} дн." if days else "")
    print("\nСкопируйте строку ниже целиком и сохраните её на GitHub:")
    print("Settings → Secrets and variables → Actions → New repository secret, "
          f"имя {TOKEN_ENV}.\nНикому её не пересылайте.\n")
    print(token)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Список «Нас поддерживают» из DonationAlerts")
    parser.add_argument("--get-token", action="store_true", help="получить ключ DonationAlerts (один раз)")
    parser.add_argument("--from-widget", metavar="FILE",
                        help=f"без ключей: страница {WIDGET_URL}, сохранённая из браузера")
    parser.add_argument("--dry-run", action="store_true", help="показать итог, не меняя supporters.json")
    args = parser.parse_args(argv)
    if args.get_token:
        return get_token()
    if args.from_widget:
        try:
            import_widget(Path(args.from_widget), path=SUPPORTERS_FILE, dry_run=args.dry_run)
        except SyncError as e:
            print(e)
            return 1
        return 0
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        print(f"Нет ключа {TOKEN_ENV} — список не обновляю. Как получить: --get-token")
        return 0
    try:
        sync(token, dry_run=args.dry_run)
    except SyncError as e:
        print(e)
        # Ключ не подходит — пусть запуск упадёт и GitHub пришлёт письмо; сбой сети — тихо ждём следующего
        return 1 if e.fatal else 0
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
