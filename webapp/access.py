"""Доступ к Яндексу: ключ к Диску (токен) и данные входа для Безлимита (cookies.json).

Проверки кешируются: /api/status опрашивается раз в 1,5 с, и ждать там Яндекс нельзя.
Здесь же адреса, которыми интерфейс сохраняет и удаляет ключ и данные входа.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import yadisk
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from downloader import account_info, api_error_details, make_client
from unlim_common import missing_required_cookies
from webapp import config, paths

log = logging.getLogger("webui")
router = APIRouter()

# Кэш для check_token: хранит только ОДИН токен (последний проверенный).
# Так не накапливаются старые токены в памяти.
_token_check_cache: dict = {"token": None, "at": 0, "data": None}
_TOKEN_TTL = 30  # секунд
_token_refreshing = threading.Event()


def check_token(token: str) -> dict:
    if not token or token.startswith("AQAAA_paste"):
        return {"ok": False, "msg": "Токен не задан"}
    # Кэш — не дёргать Яндекс каждые 1.5 сек!
    now = time.time()
    if (_token_check_cache["token"] == token
            and now - _token_check_cache["at"] < _TOKEN_TTL):
        return _token_check_cache["data"]
    try:
        cli = make_client(token)
        try:
            if not cli.check_token():
                data = {"ok": False, "msg": "Токен невалиден: Яндекс ответил, что он не действует. "
                                           "Скопируйте токен заново целиком."}
                _token_check_cache.update({"token": token, "at": now, "data": data})
                return data
            acc = account_info(cli)
        finally:
            cli.close()
        data = {"ok": True, "msg": "OK", "used": acc["used"], "total": acc["total"],
                "photo_unlim": acc["photo_unlim"], "login": acc["login"], "account": acc}
        log.info("токен проверен: %s, занято %s из %s, безлимит %s", acc["login"],
                 acc["used"], acc["total"], acc["photo_unlim"])
    except (yadisk.exceptions.UnauthorizedError, yadisk.exceptions.ForbiddenError) as e:
        data = {"ok": False, "msg": f"Токен не принят Яндексом: {type(e).__name__}{api_error_details(e)}"}
        log.warning("токен отклонён: %s", data["msg"])
    except Exception as e:
        # Сеть, 50x, таймаут: Яндекс не ответил — это не значит, что токен плохой
        data = {"ok": False, "unreachable": True,
                "msg": f"Яндекс не ответил на проверку токена ({type(e).__name__}). Повторю позже."}
        log.warning("проверка токена не удалась: %s: %s%s", type(e).__name__, e, api_error_details(e))
    _token_check_cache.update({"token": token, "at": now, "data": data})
    return data


def token_snapshot(token: str) -> dict:
    """Последняя проверка токена сразу, свежая — в фоне.

    /api/status опрашивается раз в 1,5 с: ждать ответа Яндекса там нельзя — во время
    сбоев проверка висит минутами, и весь интерфейс замирает.
    """
    cached = _token_check_cache
    if cached["token"] == token and cached["data"] is not None:
        if time.time() - cached["at"] >= _TOKEN_TTL and not _token_refreshing.is_set():
            _token_refreshing.set()

            def refresh():
                try:
                    check_token(token)
                finally:
                    _token_refreshing.clear()

            threading.Thread(target=refresh, daemon=True).start()
        return cached["data"]
    return check_token(token)  # первый раз — синхронно


def cookies_report(arr: list) -> dict:
    """Проверяет массив кук (формат Cookie-Editor) на наличие обязательных."""
    if not isinstance(arr, list):
        return {"ok": False, "msg": "Должен быть массив", "count": 0}
    missing = missing_required_cookies(arr)
    if missing:
        return {"ok": False,
                "msg": f"Не хватает кук: {', '.join(missing)}. "
                       f"Залогинься в браузере на disk.yandex.ru и заэкспортируй заново.",
                "count": len(arr)}
    return {"ok": True, "msg": f"Куки OK ({len(arr)} шт)", "count": len(arr)}


def check_cookies() -> dict:
    if not paths.COOKIES_FILE.exists():
        return {"ok": False, "msg": "cookies.json не загружен", "count": 0}
    try:
        raw = json.loads(paths.COOKIES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "msg": f"Ошибка: {e}", "count": 0}
    if not isinstance(raw, list):
        return {"ok": False, "msg": "Формат не Cookie-Editor", "count": 0}
    return cookies_report(raw)


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


def plan_sources(do_disk: bool, do_unlim: bool, token_ok: bool, cookies_ok: bool) -> dict:
    """Что реально можно скачать при текущих токене и cookies.

    Нет токена — обычный диск пропускаем, если есть что качать в безлимите; нет
    cookies — наоборот. Ошибка только когда скачать нечего совсем: тогда пользователь
    получает подсказку, что настроить, а не падение на первом шаге.
    """
    run_disk = do_disk and token_ok
    run_unlim = do_unlim and cookies_ok
    notes: list[str] = []
    if do_disk and not token_ok and run_unlim:
        notes.append("⚠️ Токен не задан или не работает: обычный диск пропускаю, качаю только безлимит. "
                     "Добавить токен: на Главной кнопка «Получить и ввести токен».")
    if do_unlim and not cookies_ok and run_disk:
        notes.append("⚠️ Cookies не загружены: безлимит пропускаю, качаю только обычный диск. "
                     "Добавить cookies: на Главной кнопка «Добавить cookies».")
    error = None
    if not run_disk and not run_unlim:
        if do_disk and do_unlim:
            error = ("Нечего скачивать: нет ни токена, ни cookies. "
                     "Настройте хотя бы одно на Главной в блоке «Путь к бэкапу».")
        elif do_disk:
            error = "Для обычного диска нужен токен: на Главной кнопка «Получить и ввести токен»."
        elif do_unlim:
            error = "Для безлимита нужны cookies: на Главной кнопка «Добавить cookies»."
        else:
            error = "Выберите хотя бы одно: обычный диск или безлимит."
    return {"run_disk": run_disk, "run_unlim": run_unlim, "notes": notes, "error": error}


def preflight_sources(do_disk: bool, do_unlim: bool) -> dict:
    """plan_sources по текущим настройкам — до запуска, чтобы ответить пользователю сразу.

    Яндекс не ответил на проверку токена — это не «нет токена»: Диск всё равно пробуем.
    """
    tok = check_token(config.get_config()["token"]) if do_disk else {}
    token_ok = bool(tok.get("ok") or tok.get("unreachable"))
    cookies_ok = bool(check_cookies().get("ok")) if do_unlim else False
    return plan_sources(do_disk, do_unlim, token_ok, cookies_ok)


# ── Адреса: ключ к Диску и данные входа для Безлимита ──────────────────────


class TokenPayload(BaseModel):
    token: str


class CookiesPayload(BaseModel):
    cookies: list


@router.post("/api/config/token/check")
def api_check_token(payload: TokenPayload):
    """Проверка без сохранения."""
    return check_token((payload.token or "").strip())


@router.post("/api/config/token")
def api_set_token(payload: TokenPayload):
    token = (payload.token or "").strip()
    if not token:
        raise HTTPException(400, "Пустой токен")
    config.env_set("YA_DISK_TOKEN", token)
    check = check_token(token)
    log.info("токен сохранён (%s): %s", config.mask_token(token),
             check.get("login") if check.get("ok") else check.get("msg"))
    return {"ok": True, "check": check}


@router.post("/api/config/token/clear")
def api_clear_token():
    if paths.ENV_FILE.exists():
        config.env_set("YA_DISK_TOKEN", "", quote_mode="never")
    log.info("токен удалён пользователем")
    return {"ok": True}


@router.post("/api/cookies/check")
def api_check_cookies(payload: CookiesPayload):
    """Проверка без сохранения."""
    return cookies_report(payload.cookies)


@router.post("/api/cookies")
def api_set_cookies(payload: CookiesPayload):
    """Сохраняет cookies.json атомарно через tmp+replace с предварительной валидацией."""
    val = cookies_report(payload.cookies)
    if not val.get("ok"):
        raise HTTPException(400, val.get("msg", "Невалидные cookies"))
    arr = payload.cookies
    tmp = paths.COOKIES_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, paths.COOKIES_FILE)
    except Exception as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise HTTPException(500, f"Не удалось сохранить cookies.json: {e}")
    log.info("cookies сохранены: %d шт", len(arr))
    return {"ok": True, "check": check_cookies(), "saved_count": len(arr)}


@router.post("/api/cookies/clear")
def api_clear_cookies():
    if paths.COOKIES_FILE.exists():
        try:
            paths.COOKIES_FILE.unlink()
        except OSError as e:
            return {"ok": False, "msg": str(e)}
    log.info("cookies удалены пользователем")
    return {"ok": True}
