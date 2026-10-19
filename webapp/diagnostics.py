"""Почему не качается: объяснения сбоев по журналам задач и монитор серверов Яндекса.

Скрипты печатают строки «kind | текст» (auth_expired, storage_down, local_disk …);
diagnose_tasks() ищет эти метки в свежих журналах и выбирает объяснение для человека.
"""

from __future__ import annotations

import re

from utils import (
    EXIT_AUTH,
    EXIT_DEFERRED,
    EXIT_DISK_FULL,
    EXIT_OUTAGE,
    EXIT_PARTIAL,
    TELEGRAM_CHANNEL,
)
from webapp import paths
from webapp.tasks import SLOT_LINKS, SLOT_ORGANIZE, VERIFY_KINDS, tm
from yandex_health import HealthMonitor

# Ссылки для объяснений в UI: что происходит с Яндексом и где следить за новостями
NEWS_LINKS = [
    {"title": "РБК: «Яндекс» ограничил часть функций Диска (11.10.2026)",
     "url": "https://www.rbc.ru/society/11/10/2026/6acbb470b603d0e16d9a1cb0"},
    {"title": "Поддержка Яндекса: скачивание всех файлов с Диска",
     "url": "https://yandex.ru/support/yandex-360/customers/disk/web/ru/actions-files/download-all"},
    {"title": "Новости и статус серверов в Telegram", "url": TELEGRAM_CHANNEL},
]

# Коды выхода дочерних скриптов (константы — в utils.py). Это же объяснение уходит во
# фронтенд через /api/status.exit_codes, своей копии у фронтенда нет.
EXIT_EXPLAIN = {
    EXIT_PARTIAL: "часть файлов не скачалась — их можно повторить",
    EXIT_AUTH: "ключ доступа к Диску или доступ к Безлимиту устарел — обновите его в настройках",
    EXIT_OUTAGE: "Яндекс временно не отвечает или отказывает — программа повторит позже",
    EXIT_DISK_FULL: "на локальном диске нет места — освободите место и запустите снова",
    EXIT_DEFERRED: "всё доступное скачано, остальные файлы ждут, когда серверы Яндекса оживут",
}

# Монитор состояния серверов Яндекса; стартует в webui.main(), в тестах остаётся None
health_monitor: HealthMonitor | None = None


def start_health_monitor(token_getter) -> None:
    """Два лёгких запроса в минуту, история в logs/yandex_health.jsonl."""
    global health_monitor
    health_monitor = HealthMonitor(token_getter=token_getter, log_path=paths.HEALTH_LOG)
    health_monitor.start()


def health_snapshot() -> dict | None:
    return health_monitor.snapshot() if health_monitor else None


def _affects(disk: tuple[str, str], unlim: tuple[str, str]) -> list[dict]:
    return [
        {"what": "Обычный диск (REST API)", "state": disk[0], "text": disk[1]},
        {"what": "Безлимит (веб-версия Диска)", "state": unlim[0], "text": unlim[1]},
    ]


# Хвост строки FAIL — путь файла на Диске («… | disk:/Видео/Timeout.mkv»), и пути в кавычках
# из текста ошибки: имя файла — не повод для баннера («Timeout.mkv» — не таймаут Яндекса)
_FILE_TAIL_RE = re.compile(r" \| (?:disk:|/).*$")
_QUOTED_RE = re.compile(r"'[^']*'")


def _marker_text(line: str) -> str:
    """Строка журнала без путей к файлам — в ней ищутся маркеры правил."""
    return _QUOTED_RE.sub("''", _FILE_TAIL_RE.sub("", line))


# Правила диагностики по маркерам в логах задач. Порядок = приоритет.
# markers — подстроки (в нижнем регистре); первая сработавшая категория определяет
# объяснение, счётчики считаются по всем.
_DIAG_RULES: list[dict] = [
    {
        "kind": "auth_expired",
        "markers": ("auth_expired", "401 unauthorized", "403 forbidden"),
        "title": "Яндекс отклонил авторизацию (HTTP 401/403)",
        "message": "Авторизация отклонена Яндексом (HTTP 401/403). Токен или cookies устарели.",
        "explain": "Токен Диска или cookies.json протухли. Это не сбой Яндекса: нужно заново "
                   "получить токен или заэкспортировать куки из браузера, где вы залогинены.",
        "recommendation": "Обновите токен Яндекс.Диска или cookies.json в настройках Web UI.",
        "affects": _affects(("fail", "нужен новый токен"), ("fail", "нужны свежие куки")),
    },
    {
        "kind": "local_disk",
        "markers": ("local_disk",),
        "title": "Проблема с вашим диском, а не с Яндексом",
        "message": "На локальном диске нет места, нет прав на запись или папка скачивания недоступна.",
        "explain": "Скрипт не может записать файлы: закончилось место, папка на отключённом диске "
                   "или нет прав. Повторные попытки бесполезны, пока это не исправлено.",
        "recommendation": "Освободите место или укажите другую папку скачивания в настройках, затем повторите.",
        "affects": _affects(("fail", "ждёт места на диске"), ("fail", "ждёт места на диске")),
    },
    {
        "kind": "bulk_disabled",
        "markers": ("bulk_unavailable", "not_zip |", "refused |"),
        # Правило про безлимит: строки Диска сюда не считаем (unlim_collect, unlim_w1…)
        "slots": ("unlim",),
        # Один отказ на один файл — не повод кричать «Яндекс отказывает»
        "min_lines": 3,
        "strong_markers": ("bulk_unavailable",),
        "title": "Яндекс отказывает в скачивании безлимита",
        "message": "Воркер безлимита раз за разом получает от Яндекса отказ вместо файлов.",
        "explain": "С 11 октября 2026 после атак на дата-центры Яндекс отключил массовое скачивание "
                   "в веб-версии Диска (папки, наборы файлов, альбомы, ZIP-архивы). Поэтому программа "
                   "качает безлимит по одному файлу, как кнопка «Скачать» в веб-версии. Сейчас Яндекс "
                   "отказывает и в этом: отвечает 403 при живой сессии или присылает страницу вместо файла. "
                   "Одиночные файлы обычного диска через официальный API Яндекс отдаёт.",
        "recommendation": "Качайте обычный диск сейчас. Безлимит оставьте: воркер ждёт растущими "
                          "паузами, попытки файлов не сжигает и сам продолжит, когда Яндекс снимет ограничение "
                          "(через час без успеха он выйдет — запустите снова позже).",
        "affects": _affects(("ok", "работает"), ("wait", "ждёт разрешения Яндекса")),
    },
    {
        "kind": "storage_down",
        "markers": ("storage_down",),
        "title": "Часть файлов лежит на серверах Яндекса, которые не отвечают",
        "message": "Серверы хранения Яндекса с частью файлов молчат — такие файлы отложены.",
        "explain": "Каждый файл хранится на конкретном сервере Яндекса. После атак на дата-центры "
                   "часть серверов не отвечает: эти файлы не отдаёт ни программа, ни веб-версия, ни "
                   "официальный API. Воркер откладывает их без сжигания попыток, качает остальные и "
                   "через 30 минут пробует отложенные снова.",
        "recommendation": "Ничего делать не нужно: доступные файлы качаются. Если воркер вышел с кодом 3, "
                          "остались только недоступные файлы — запустите скачивание позже. "
                          "Не удаляйте эти файлы с Диска.",
        "affects": _affects(("ok", "работает"), ("wait", "часть файлов отложена")),
    },
    {
        "kind": "rate_limit",
        "markers": ("rate_limit", "429 toomanyrequests", "429 too many requests"),
        "title": "Яндекс ограничивает частоту запросов (HTTP 429)",
        "message": "Яндекс ограничивает частоту запросов (HTTP 429 Too Many Requests).",
        "explain": "Серверы Яндекса просят сбавить темп. Это не отключение функции: при получении 429 "
                   "все воркеры делают общую паузу (от 45 секунд до 10 минут) и продолжают сами.",
        "recommendation": "Если 429 повторяются, остановите скачивание и запустите снова с 1–2 воркерами "
                          "и пачкой 10.",
        "affects": _affects(("wait", "работает с паузами"), ("wait", "работает с паузами")),
    },
    {
        "kind": "server_50x",
        "markers": ("server_50x", "504 gateway timeout", "сбой сервера яндекса"),
        "title": "Сбой или перегрузка серверов Яндекса (HTTP 50x)",
        "message": "Сбой или перегрузка серверов Яндекса (50x/504).",
        "explain": "Узлы Яндекса отвечают ошибками 500–504: перегрузка или повреждённая инфраструктура "
                   "дата-центров. Скрипт повторяет запросы с паузами, данные не теряются.",
        "recommendation": "Сервер Яндекса перегружен. Снизьте размер пачки до 10–15 и число воркеров до 2.",
        "affects": _affects(("wait", "работает с повторами"), ("wait", "работает с повторами")),
    },
    {
        "kind": "timeout",
        "markers": ("download_timeout", "timeout |", "таймаут"),
        "title": "Яндекс отвечает слишком медленно (таймауты)",
        "message": "Превышено время ожидания ответа от Яндекса (сетевой таймаут).",
        "explain": "Ответы приходят дольше лимита ожидания — перегрузка на стороне Яндекса "
                   "или нестабильное соединение.",
        "recommendation": "Проверьте стабильность интернет-соединения или уменьшите нагрузку.",
        "affects": _affects(("wait", "работает с повторами"), ("wait", "работает с повторами")),
    },
    {
        "kind": "prepare_failed",
        "markers": ("prepare_failed",),
        "title": "Яндекс не выдал часть файлов безлимита",
        "message": "Яндекс отвечает ошибкой на запрос ссылки на файл или сборки архива.",
        "explain": "Веб-версия Диска вернула ошибку вместо ссылки на файл (или архива). Такие файлы "
                   "возвращаются в очередь и повторяются; после 3 неудачных попыток файл помечается ошибкой — "
                   "его можно повторить позже кнопкой «Повторить ошибки».",
        "recommendation": "Ничего делать не нужно: файлы повторяются сами. Если ошибок много — снизьте число воркеров.",
        "affects": _affects(("ok", "работает"), ("wait", "повторяет файлы")),
    },
]
_DIAG_FLAG_KINDS = ("rate_limit", "server_50x", "auth_expired", "timeout", "prepare_failed")


def diagnose_tasks() -> dict:
    """Анализирует логи активных и недавних задач: что не так, почему и что делать.

    Возвращает и старые поля (*_detected, message, recommendation), и новые:
    kind, title, explain, affects, counts, links.
    """
    # Строки скачивания по ссылкам объясняет свой экран: здесь они дали бы баннер «Диска».
    # Сортировка и проверка скачанного печатают имена файлов («timeout_2024.docx») — это не про Яндекс
    # Превью — бонус: «превью …: HTTP 434» или таймаут превью не объясняют, почему не качаются файлы
    tagged = [(slot, _marker_text(ln.lower())) for slot, ln in
              tm.recent_log_lines(40, exclude=(SLOT_LINKS, SLOT_ORGANIZE), with_slots=True,
                                  exclude_kinds=VERIFY_KINDS)
              if "превью" not in ln.lower()]

    def rule_lines(rule: dict) -> list[str]:
        slots = rule.get("slots")
        return [ln for slot, ln in tagged if not slots or slot.startswith(slots)]

    counts = {
        rule["kind"]: sum(1 for ln in rule_lines(rule) if any(m in ln for m in rule["markers"]))
        for rule in _DIAG_RULES
    }
    diag: dict = {f"{k}_detected": False for k in _DIAG_FLAG_KINDS}
    diag.update({
        "kind": None, "title": None, "message": None, "explain": None,
        "recommendation": None, "affects": [], "counts": counts, "links": NEWS_LINKS,
    })

    def fired(rule: dict) -> bool:
        n = counts[rule["kind"]]
        if n >= rule.get("min_lines", 1):
            return True
        strong = rule.get("strong_markers", ())
        return bool(n) and any(m in ln for ln in rule_lines(rule) for m in strong)

    for rule in _DIAG_RULES:
        if fired(rule):
            diag["kind"] = rule["kind"]
            for key in ("title", "message", "explain", "recommendation", "affects"):
                diag[key] = rule[key]
            if rule["kind"] in _DIAG_FLAG_KINDS:
                diag[f"{rule['kind']}_detected"] = True
            break
    return diag
