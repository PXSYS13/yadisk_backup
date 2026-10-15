"""Обновляет supporters.json по донатам из DonationAlerts.

Запускается GitHub Actions по расписанию (.github/workflows/supporters.yml): берёт все
донаты через API DonationAlerts и переписывает их часть списка. Записи, которые автор
добавил руками (без "id"), остаются как есть. Программы людей подхватывают свежий
supporters.json с GitHub сами — так имя поддержавшего появляется в разделе
«Нас поддерживают».

Только стандартная библиотека: в Actions не нужно ставить зависимости проекта.

Ключ DonationAlerts берётся из переменной окружения DONATIONALERTS_TOKEN (секрет
репозитория). Получить его один раз:

    python tools/sync_supporters.py --get-token

Проверить без записи в файл:

    DONATIONALERTS_TOKEN=... python tools/sync_supporters.py --dry-run

Скрыть донат целиком — добавить его номер в "hidden", скрыть только сообщение — в
"no_message" (номер — число из "id" записи: "da-123" → 123).
"""

from __future__ import annotations

import argparse
import getpass
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPPORTERS_FILE = ROOT / "supporters.json"

API_URL = "https://www.donationalerts.com/api/v1/alerts/donations"
AUTHORIZE_URL = "https://www.donationalerts.com/oauth/authorize"
TOKEN_URL = "https://www.donationalerts.com/oauth/token"
SCOPE = "oauth-donation-index"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
TOKEN_ENV = "DONATIONALERTS_TOKEN"

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
    "Список заполняется сам: донаты DonationAlerts попадают сюда автоматически (записи с id «da-…»). "
    "Записи без id можно добавлять руками: name — имя, как человек подписался (без имени — «Аноним»), "
    "amount — сумма числом, currency — RUB/USD/EUR, date — ГГГГ-ММ-ДД, message — короткие слова или null. "
    "Скрыть донат целиком — добавьте его номер в hidden (da-123 → 123), скрыть только сообщение — "
    "в no_message. Программы людей подхватят список с GitHub в течение получаса."
)

# Ссылки и @упоминания вырезаем: список видят все пользователи программы, рекламе там не место
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_DOMAIN_RE = re.compile(
    r"(?i)\b[\w-]+(?:\.[\w-]+)*\.(?:ru|su|com|net|org|me|io|gg|tv|cc|ly|link|xyz|top|info|site|online|shop)"
    r"\b(?:/\S*)?"
)
_MENTION_RE = re.compile(r"(?<![\w.])@\w{2,}")


class SyncError(Exception):
    """Ошибка, после которой список обновлять нельзя. fatal — ключ не подходит (нужен человек)."""

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


def merge(current: dict, donations: list[dict], today: str | None = None) -> dict:
    """Новый supporters.json: ручные записи + донаты из DonationAlerts без скрытых.

    "updated" меняется, только если изменился сам список, — иначе файл остаётся прежним
    и GitHub Actions ничего не коммитит.
    """
    current = current if isinstance(current, dict) else {}
    hidden = _id_set(current.get("hidden"))
    no_message = _id_set(current.get("no_message"))
    old = current.get("supporters") if isinstance(current.get("supporters"), list) else []
    manual = [x for x in old if isinstance(x, dict) and not str(x.get("id", "")).startswith(ID_PREFIX)]
    fresh: dict[str, dict] = {}
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


def _request(url: str, token: str):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=TIMEOUT_SEC)


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
                text = "Не получилось: " + got["error"]
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
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
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
    parser.add_argument("--dry-run", action="store_true", help="показать итог, не меняя supporters.json")
    args = parser.parse_args(argv)
    if args.get_token:
        return get_token()
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
