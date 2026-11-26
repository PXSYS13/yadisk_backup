"""Доступ к Яндексу: ключ к Диску (токен) и данные входа для Безлимита (cookies.json).

Проверки кешируются: /api/status опрашивается раз в 1,5 с, и ждать там Яндекс нельзя.
Здесь же адреса, которыми интерфейс сохраняет и удаляет ключ и данные входа.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time

import yadisk
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from downloader import account_info, api_error_details, is_overdraft, make_browse_client, overdraft_account
from unlim_common import (
    REQUIRED_COOKIES,
    AuthExpiredError,
    cookies_dict,
    expired_required_cookies,
    foreign_cookie_domains,
    get_sk_uid,
    make_session,
    missing_required_cookies,
    parse_cookies_text,
    resolve_cookies_base,
)
from webapp import config, paths

log = logging.getLogger("webui")
router = APIRouter()

# Кэш для check_token: хранит только ОДИН токен (последний проверенный).
# Так не накапливаются старые токены в памяти.
_token_cache_lock = threading.Lock()
_token_check_cache: dict = {"token": None, "at": 0.0, "data": None}
_TOKEN_TTL = 30  # секунд
_token_refreshing = threading.Event()
_cookies_cache_lock = threading.Lock()
_cookies_cache: dict = {"key": None, "at": 0.0, "data": None}
_COOKIES_TTL = 5.0


def invalidate_token_cache() -> None:
    """Сбрасывает кэш проверки ключа — например, после удаления ключа или продления подписки."""
    with _token_cache_lock:
        _token_check_cache.update({"token": None, "at": 0.0, "data": None})


def invalidate_cookies_cache() -> None:
    """Сбрасывает кэш разбора cookies.json (включая кэш загрузчика в Безлимит)."""
    with _cookies_cache_lock:
        _cookies_cache.update({"key": None, "at": 0.0, "data": None})
    try:
        from uploader.webapp import manager as upload_mgr
        upload_mgr.manager.set_account_cache(None, None)
    except Exception:  # noqa: BLE001
        pass


def normalize_token(raw: str) -> str:
    """Ключ из того, что человек вставил: сам ключ, «OAuth …», адрес страницы с #access_token=….

    Люди копируют адрес из строки браузера целиком, приставку «OAuth» из примеров или
    ключ с переносом строки посередине — Яндекс такой не принимает.
    """
    text = (raw or "").strip().strip("'\"«»").strip()
    m = re.search(r"access_token=([^&#\s]+)", text)
    if m:
        text = m.group(1)
    text = re.sub(r"^(?:oauth|bearer)\s+", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", "", text)


def _overdraft_status(cli) -> dict:
    """Ключ жив, но Диск переполнен: Яндекс закрыл по нему доступ. Цифры места он отдаёт и так."""
    acc = overdraft_account(cli) or {}
    data = {"ok": False, "overdraft": True,
            "msg": "Диск переполнен — Яндекс закрыл программам доступ к нему, пока не продлена подписка",
            "used": acc.get("used"), "total": acc.get("total"), "login": acc.get("login"),
            "account": acc or None}
    log.warning("Диск переполнен, Яндекс закрыл доступ по ключу: %s, занято %s из %s",
                data["login"], data["used"], data["total"])
    return data


def _token_data(cli) -> dict:
    """Проверка ключа у Яндекса. Отказ во входе и сбои — исключениями, их разбирает check_token."""
    try:
        valid = cli.check_token()
    except yadisk.exceptions.ForbiddenError as e:
        if is_overdraft(e):
            return _overdraft_status(cli)
        raise
    if not valid:
        return {"ok": False, "msg": "Токен невалиден: Яндекс ответил, что он не действует. "
                                   "Скопируйте токен заново целиком."}
    acc = account_info(cli)
    log.info("токен проверен: %s, занято %s из %s, безлимит %s", acc["login"],
             acc["used"], acc["total"], acc["photo_unlim"])
    return {"ok": True, "msg": "OK", "used": acc["used"], "total": acc["total"],
            "photo_unlim": acc["photo_unlim"], "login": acc["login"], "account": acc}


def check_token(token: str) -> dict:
    if not token or token.startswith("AQAAA_paste"):
        return {"ok": False, "msg": "Токен не задан"}
    # Кэш — не дёргать Яндекс каждые 1.5 сек!
    now = time.time()
    with _token_cache_lock:
        if (_token_check_cache["token"] == token
                and now - _token_check_cache["at"] < _TOKEN_TTL
                and _token_check_cache["data"] is not None):
            return _token_check_cache["data"]
    try:
        # Человек ждёт ответа (первый экран, «Сохранить мои файлы», отчёт): короткие таймауты.
        # С таймаутами скачивания проверка во время сбоя Яндекса висела бы до получаса
        cli = make_browse_client(token)
        try:
            data = _token_data(cli)
        finally:
            cli.close()
    except (yadisk.exceptions.UnauthorizedError, yadisk.exceptions.ForbiddenError) as e:
        data = {"ok": False, "msg": f"Токен не принят Яндексом: {type(e).__name__}{api_error_details(e)}"}
        log.warning("токен отклонён: %s", data["msg"])
    except Exception as e:
        # Сеть, 50x, таймаут: Яндекс не ответил — это не значит, что токен плохой
        data = {"ok": False, "unreachable": True,
                "msg": f"Яндекс не ответил на проверку токена ({type(e).__name__}). Повторю позже."}
        log.warning("проверка токена не удалась: %s: %s%s", type(e).__name__, e, api_error_details(e))
    with _token_cache_lock:
        _token_check_cache.update({"token": token, "at": time.time(), "data": data})
    return data


def token_snapshot(token: str) -> dict:
    """Последняя проверка токена сразу, свежая — в фоне.

    /api/status опрашивается раз в 1,5 с: ждать ответа Яндекса там нельзя — во время
    сбоев проверка висит минутами, и весь интерфейс замирает.
    """
    with _token_cache_lock:
        hit = _token_check_cache["token"] == token and _token_check_cache["data"] is not None
        cached_at = _token_check_cache["at"]
        cached_data = _token_check_cache["data"]
    if hit:
        if time.time() - cached_at >= _TOKEN_TTL and not _token_refreshing.is_set():
            _token_refreshing.set()

            def refresh():
                try:
                    # Если пока шёл сетевой запрос, человек сохранил другой токен — старый кэш не возвращаем
                    with _token_cache_lock:
                        if _token_check_cache["token"] not in (None, token):
                            return
                    fresh = check_token(token)
                    with _token_cache_lock:
                        if _token_check_cache["token"] == token:
                            _token_check_cache["data"] = fresh
                finally:
                    _token_refreshing.clear()

            threading.Thread(target=refresh, daemon=True).start()
        return cached_data
    return check_token(token)  # первый раз — синхронно


def cookies_report(arr: list) -> dict:
    """Проверяет массив кук на наличие обязательных, срок действия и допустимый домен Яндекса."""
    if not isinstance(arr, list):
        return {"ok": False, "msg": "Должен быть массив", "count": 0}
    expired = expired_required_cookies(arr)
    if expired:
        return {
            "ok": False,
            "msg": f"Срок действия данных входа уже истёк ({', '.join(expired)}). "
                   f"Откройте disk.yandex.ru в браузере, убедитесь, что вы вошли, и скопируйте заново.",
            "count": len(arr),
        }
    missing = missing_required_cookies(arr)
    if missing:
        if "Session_id" in missing and "sessionid2" in missing and "yandexuid" not in missing:
            return {
                "ok": False,
                "msg": f"Не хватает защищённых кук ({', '.join(missing)}): они помечены HttpOnly и не копируются "
                       f"через консоль браузера. Используйте расширение Cookie-Editor (Export → Export as JSON) "
                       f"или Copy as cURL из вкладки Сеть (Network).",
                "count": len(arr),
            }
        return {
            "ok": False,
            "msg": f"Не хватает кук: {', '.join(missing)}. "
                   f"Войдите в браузере на disk.yandex.ru и скопируйте данные заново.",
            "count": len(arr),
        }
    foreign = foreign_cookie_domains(arr)
    if foreign:
        return {
            "ok": False,
            "msg": f"Данные входа скопированы с {', '.join(foreign)}, а нужны с disk.yandex.ru. "
                   f"Откройте именно disk.yandex.ru, войдите и скопируйте заново.",
            "count": len(arr),
        }
    base_url, tld = resolve_cookies_base(arr)
    picked = cookies_dict(arr, base_domain=tld)
    missing_after = [n for n in REQUIRED_COOKIES if n not in picked]
    if missing_after:
        return {
            "ok": False,
            "msg": f"Не удалось использовать обязательные куки ({', '.join(missing_after)}) для disk.{tld}: "
                   f"они с другого домена или содержат недопустимые символы. Откройте disk.yandex.ru "
                   f"и скопируйте данные заново через Cookie-Editor (Export → Export as JSON).",
            "count": len(arr),
        }
    return {"ok": True, "msg": f"Куки OK ({len(arr)} шт)", "count": len(arr), "base": base_url}


def verify_cookies(arr: list) -> dict:
    """Пускает ли Яндекс с этими куками: та же страница, с которой начинает работу Безлимит.

    ok=False + rejected — Яндекс явно не пустил (вход завершён, не тот аккаунт или сайт);
    ok=True + unreachable — проверить не удалось (сбой, капча, сеть): сохраняем, проверит скачивание.
    """
    base_url, tld = resolve_cookies_base(arr)
    try:
        s = make_session(cookies_dict(arr, base_domain=tld), base=base_url)
        try:
            get_sk_uid(s)
        finally:
            s.close()
    except AuthExpiredError:
        log.warning("куки отклонены Яндексом при сохранении (%s)", base_url)
        return {"ok": False, "rejected": True,
                "msg": "Яндекс не пустил с этими данными входа: вход в браузере уже завершён или "
                       "скопировано не с того сайта. Откройте disk.yandex.ru, проверьте, что вы вошли в свой "
                       "аккаунт, и скопируйте данные заново."}
    except Exception as e:  # noqa: BLE001 — сеть, капча, 50x, изменённая страница: это не «плохие куки»
        log.warning("проверка кук не удалась: %s: %s", type(e).__name__, e)
        msg = ("Яндекс попросил проверку на робота (капчу) — сохранил данные входа, проверю при скачивании"
               if "капч" in str(e).lower()
               else "Яндекс сейчас не ответил на проверку — сохранил, проверю при скачивании")
        return {"ok": True, "unreachable": True, "msg": msg}
    return {"ok": True, "msg": "Яндекс пускает с этими данными входа", "base": base_url}


def check_cookies() -> dict:
    try:
        st = paths.COOKIES_FILE.stat()
    except FileNotFoundError:
        return {"ok": False, "msg": "cookies.json не загружен", "count": 0}
    except OSError as e:
        return {"ok": False, "msg": f"Ошибка: {e}", "count": 0}
    cache_key = (str(paths.COOKIES_FILE), st.st_mtime_ns, st.st_size)
    now = time.monotonic()
    with _cookies_cache_lock:
        if _cookies_cache["key"] == cache_key and now - _cookies_cache["at"] < _COOKIES_TTL and _cookies_cache["data"]:
            return dict(_cookies_cache["data"])
    try:
        text = paths.COOKIES_FILE.read_text(encoding="utf-8-sig")
    except Exception as e:
        return {"ok": False, "msg": f"Ошибка: {e}", "count": 0}
    raw = parse_cookies_text(text)
    if not isinstance(raw, list):
        rep = {"ok": False, "msg": "Формат не Cookie-Editor", "count": 0}
    else:
        rep = cookies_report(raw)
        # Когда сохранены: доступ, который запуск отверг, снова годен, только если файл новее отказа
        rep["saved_at"] = st.st_mtime
    with _cookies_cache_lock:
        _cookies_cache.update({"key": cache_key, "at": now, "data": dict(rep)})
    return rep


def onboarding_state(cfg: dict, token: dict, cookies: dict, disk_db: dict, unlim_db: dict) -> dict:
    """Воронка первого запуска: что уже настроено, какой шаг следующий."""
    steps = {
        "token": bool(token.get("ok")),
        "dir": bool(cfg.get("download_dir_set")),
        "cookies": bool(cookies.get("ok")),
        "scanned": bool(disk_db.get("total") or unlim_db.get("total")),
        "downloaded": bool(disk_db.get("downloaded") or unlim_db.get("downloaded")),
    }
    if not steps["token"] and not steps["cookies"]:
        next_step = "token"
    elif not steps["dir"]:
        next_step = "dir"
    elif not steps["cookies"]:
        next_step = "cookies"   # опционально — UI даёт «пропустить»
    elif not steps["downloaded"]:
        next_step = "download"
    else:
        next_step = "done"
    return {**steps, "next": next_step}


def token_usable(tok: dict) -> bool:
    """Диск запускать: ключ работает, Яндекс не ответил на проверку или Диск переполнен.

    На переполненном Диске сбор списка сразу кончится кодом 6, и человек увидит, что
    нужна подписка, а не «нет ключа доступа» или «нечего скачивать».
    """
    return bool(tok.get("ok") or tok.get("unreachable") or tok.get("overdraft"))


def plan_sources(do_disk: bool, do_unlim: bool, token_ok: bool, cookies_ok: bool) -> dict:
    """Что реально можно скачать при текущих токене и cookies.

    Нет токена — обычный диск пропускаем, если есть что качать в безлимите; нет
    cookies — наоборот. Ошибка только когда скачать нечего совсем: тогда пользователь
    получает подсказку, что настроить, а не падение на первом шаге.
    """
    run_disk = do_disk and token_ok
    run_unlim = do_unlim and cookies_ok
    notes: list[str] = []
    # Куда идти — по нынешнему интерфейсу: кнопок прежнего на Главной с 2.7.1 нет
    if do_disk and not token_ok and run_unlim:
        notes.append("⚠️ Ключа доступа к Диску нет или он не работает: обычный Диск пропускаю, качаю только "
                     "Безлимит. Добавить ключ: «Настройки» → «Доступ» → «Ключ доступа к Диску».")
    if do_unlim and not cookies_ok and run_disk:
        notes.append("⚠️ Доступ к Безлимиту не подключён: Безлимит пропускаю, качаю только обычный Диск. "
                     "Подключить: «Настройки» → «Доступ» → «Доступ к Безлимиту».")
    error = None
    if not run_disk and not run_unlim:
        if do_disk and do_unlim:
            error = ("Нечего скачивать: нет ни ключа доступа к Диску, ни доступа к Безлимиту. "
                     "Подключите хотя бы одно в «Настройках» → «Доступ».")
        elif do_disk:
            error = "Для обычного Диска нужен ключ доступа: «Настройки» → «Доступ» → «Ключ доступа к Диску»."
        elif do_unlim:
            error = "Для Безлимита нужен доступ: «Настройки» → «Доступ» → «Доступ к Безлимиту»."
        else:
            error = "Выберите хотя бы одно: обычный Диск или Безлимит."
    return {"run_disk": run_disk, "run_unlim": run_unlim, "notes": notes, "error": error}


def preflight_sources(do_disk: bool, do_unlim: bool) -> dict:
    """plan_sources по текущим настройкам — до запуска, чтобы ответить пользователю сразу.

    Яндекс не ответил на проверку токена — это не «нет токена»: Диск всё равно пробуем.
    Переполненный Диск — тоже не «нет токена»: запуск остановится на понятном действии.
    """
    tok = check_token(config.get_config()["token"]) if do_disk else {}
    token_ok = token_usable(tok)
    cookies_ok = bool(check_cookies().get("ok")) if do_unlim else False
    return plan_sources(do_disk, do_unlim, token_ok, cookies_ok)


# ── Адреса: ключ к Диску и данные входа для Безлимита ──────────────────────


class TokenPayload(BaseModel):
    token: str


class CookiesPayload(BaseModel):
    cookies: list | None = None
    text: str | None = None  # вставленный как есть: JSON, Header String или cookies.txt


@router.post("/api/config/token/check")
def api_check_token(payload: TokenPayload):
    """Проверка без сохранения (не перезаписывает кэш сохранённого ключа)."""
    tok = normalize_token(payload.token)
    with _token_cache_lock:
        prev = dict(_token_check_cache)
    res = check_token(tok)
    if prev["token"] and prev["token"] != tok:
        with _token_cache_lock:
            if _token_check_cache["token"] == tok:
                _token_check_cache.update(prev)
    return res


@router.post("/api/config/token")
def api_set_token(payload: TokenPayload):
    token = normalize_token(payload.token)
    if not token:
        raise HTTPException(400, "Пустой токен")
    config.env_set("YA_DISK_TOKEN", token)
    check = check_token(token)
    log.info("токен сохранён (%s): %s", config.mask_token(token),
             check.get("login") if check.get("ok") else check.get("msg"))
    if check.get("ok") or check.get("unreachable"):
        _resume_branch("disk")
    return {"ok": True, "check": check}


def _resume_branch(source: str) -> None:
    """Ветка спасения ждала нового ключа или доступа — пусть качает дальше."""
    from webapp.rescue import rescue  # rescue импортирует access: импорт здесь, а не наверху

    rescue.resume_access(source)


@router.post("/api/config/token/clear")
def api_clear_token():
    if paths.ENV_FILE.exists():
        config.env_set("YA_DISK_TOKEN", "", quote_mode="never")
    invalidate_token_cache()
    log.info("токен удалён пользователем")
    return {"ok": True}


def _atomic_write_cookies(arr: list) -> None:
    """Атомарно записывает cookies.json с короткими повторами на Windows (блокировка антивирусом/воркером)."""
    parent = paths.COOKIES_FILE.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".cookies-", suffix=".tmp", dir=str(parent))
    tmp = paths.COOKIES_FILE.parent / os.path.basename(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(arr, ensure_ascii=False, indent=2))
        config.owner_only(tmp)  # данные входа — только владельцу компьютера
        last_err: OSError | None = None
        for attempt in range(10):
            try:
                os.replace(tmp, paths.COOKIES_FILE)
                return
            except PermissionError as e:
                last_err = e
                if attempt < 9:
                    time.sleep(0.15)
        if last_err is not None:
            raise last_err
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


@router.post("/api/cookies")
def api_set_cookies(payload: CookiesPayload):
    """Сохраняет cookies.json атомарно через tmp+replace с предварительной валидацией."""
    arr = payload.cookies if payload.cookies is not None else parse_cookies_text(payload.text or "")
    if arr is None:
        raise HTTPException(400, "Это не похоже на данные входа. В Cookie-Editor нажмите Export → "
                                 "Export as JSON и вставьте сюда целиком (также подходят Header String, "
                                 "cookies.txt и Copy as cURL)")
    val = cookies_report(arr)
    if not val.get("ok"):
        raise HTTPException(400, val.get("msg", "Невалидные cookies"))
    live = verify_cookies(arr)
    if not live.get("ok"):
        raise HTTPException(400, live["msg"])
    try:
        _atomic_write_cookies(arr)
    except Exception as e:
        raise HTTPException(500, f"Не удалось сохранить cookies.json: {e}") from e
    invalidate_cookies_cache()
    log.info("cookies сохранены: %d шт (%s)", len(arr), val.get("base", "yandex.ru"))
    _resume_branch("unlim")
    return {"ok": True, "check": check_cookies(), "saved_count": len(arr), "live": live}


@router.post("/api/cookies/clear")
def api_clear_cookies():
    if paths.COOKIES_FILE.exists():
        last_err: OSError | None = None
        for attempt in range(5):
            try:
                paths.COOKIES_FILE.unlink()
                last_err = None
                break
            except FileNotFoundError:
                last_err = None
                break
            except PermissionError as e:
                last_err = e
                if attempt < 4:
                    time.sleep(0.15)
            except OSError as e:
                return {"ok": False, "msg": str(e)}
        if last_err is not None:
            return {"ok": False, "msg": str(last_err)}
    invalidate_cookies_cache()
    log.info("cookies удалены пользователем")
    return {"ok": True}
