"""Сборка списка файлов безлимитного фото-хранилища через web API Диска.

Использует cookies.json (экспортированные из браузера расширением Cookie-Editor).
Запоминает все файлы в unlim_state.db со статусом 'pending'. Само скачивание —
в unlim_worker.py (можно запускать несколько параллельно).

Список берётся постранично из папки /photounlim (mpfs/list) — с размерами и MD5.
Фотосрез (photoslice) — запасной путь, когда mpfs/list не нашёл ни одного файла: с октября
2026 Яндекс отдаёт его пустым, а из того, что он отдаёт, берутся только файлы безлимита.

CLI:
  python unlim_download.py --collect       # собрать список безлимита
  python unlim_download.py --status        # показать статус БД
  python unlim_download.py --retry-failed  # вернуть упавшие и зависшие в очередь
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import threading
import time

from tqdm import tqdm

from unlim_common import (
    DOWNLOAD_DIR,
    LOG_DIR,
    UNLIM_DB_FILE as DB_FILE,
    UNLIM_SCHEMA as SCHEMA,
    AuthExpiredError,
    CookiesFormatError,
    UnlimError,
    YandexUnavailableError,
    api_call,
    ensure_attempts_column,
    get_sk_uid,
    load_cookies,
    make_session,
    reset_failed_files,
)
from utils import (
    EXIT_AUTH,
    EXIT_DISK_FULL,
    EXIT_OUTAGE,
    ensure_utf8_stdio,
    files_text,
    human_bytes,
    is_sqlite_disk_error,
    remove_preview,
    sqlite_tx,
)

LOG_FILE = os.path.join(LOG_DIR, "unlim.log")
# Сколько кластеров (дней) запрашиваем одним вызовом API
CLUSTER_BATCH = 15
# Безлимит во внутреннем API Диска (mpfs); фотосрез называет те же файлы «photounlim:/имя»
UNLIM_ROOT = "/photounlim"
LIST_PAGE = 1000
LIST_META = "size,md5,mimetype,mediatype"
# Страница списка не пришла и после повторов api_call: ещё попытки через столько секунд.
# Иначе один сбой Яндекса посреди большого безлимита начинал весь сбор заново
LIST_PAGE_PAUSES = (30, 60)

log = logging.getLogger("unlim")


class ListBroken(UnlimError):
    """mpfs/list оборвался посреди списка. Прочитанные страницы уже в базе, seen — сколько в них файлов."""

    def __init__(self, seen: int, cause: BaseException):
        super().__init__(str(cause))
        self.seen = seen
        self.cause = cause


def is_unlim_id(file_id: str) -> bool:
    """Файл безлимита: путь mpfs «/photounlim/…» или имя фотосреза «photounlim:/…»."""
    return file_id.startswith(UNLIM_ROOT + "/") or file_id.startswith("photounlim:")


def unlim_twin(file_id: str) -> str:
    """Тот же файл под другим именем: mpfs «/photounlim/имя» ↔ фотосрез «photounlim:/имя»."""
    if file_id.startswith("photounlim:"):
        return UNLIM_ROOT + file_id[len("photounlim:"):]
    return "photounlim:" + file_id[len(UNLIM_ROOT):]


def _setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s | %(levelname)s | %(message)s")


# ── DB ──────────────────────────────────────────────────────────────────────

_tls = threading.local()


def db() -> sqlite3.Connection:
    c = getattr(_tls, "c", None)
    if c is None:
        c = sqlite3.connect(DB_FILE, timeout=30, isolation_level=None,
                            check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.row_factory = sqlite3.Row
        _tls.c = c
    return c


def db_init():
    c = db()
    c.executescript(SCHEMA)
    ensure_attempts_column(DB_FILE)


def status_counts() -> dict[str, int]:
    return {r["status"]: r["n"] for r in db().execute(
        "SELECT status, COUNT(*) AS n FROM unlim_files GROUP BY status"
    )}


# ── Сбор через mpfs/list ────────────────────────────────────────────────────


def list_unlim_page(s, sk, uid, offset: int, amount: int = LIST_PAGE) -> list[dict]:
    """Одна страница /photounlim. Первый элемент ответа — сама папка."""
    data = api_call(s, sk, uid, "mpfs/list",
                    {"path": UNLIM_ROOT, "amount": amount, "offset": offset, "meta": LIST_META})
    if not isinstance(data, list):
        raise RuntimeError(f"mpfs/list: ожидался список, пришёл {type(data).__name__}: {str(data)[:200]}")
    return data


def save_listed_files(items: list[dict]) -> int:
    """Добавляет файлы из mpfs/list, возвращает число новых.

    Уже известный файл (в том числе собранный фотосрезом как 'photounlim:/имя') не
    дублируется — ему только записываются размер и MD5 из mpfs, они точнее.
    """
    added = 0
    with sqlite_tx(db()) as c:
        for f in items:
            path = f.get("path") or ""
            if not path.startswith(UNLIM_ROOT + "/"):
                continue
            meta = f.get("meta") or {}
            size = int(meta.get("size") or 0)
            md5 = meta.get("md5") or None
            mime = meta.get("mimetype") or meta.get("mediatype")
            alt = unlim_twin(path)
            known = c.execute(
                "UPDATE unlim_files SET size = COALESCE(NULLIF(?, 0), size), md5 = COALESCE(?, md5), "
                "mime = COALESCE(mime, ?) WHERE file_id IN (?, ?)",
                (size, md5, mime, path, alt),
            ).rowcount
            if known:
                continue
            c.execute(
                "INSERT INTO unlim_files (file_id, name, size, md5, mime, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (path, f.get("name") or path.rsplit("/", 1)[-1], size, md5, mime),
            )
            added += 1
    return added


def drop_rows_outside_unlim() -> int:
    """Убирает из очереди строки старого списка, которые лежат не в безлимите. Сколько убрано.

    До октября 2026 список собирал фотосрез, а он отдаёт все фото аккаунта — и с обычного
    Диска тоже. Такие файлы скачивает ветка Диска; отсюда они ушли бы второй копией в
    _unlim. Зовётся только после полного списка mpfs/list: тогда известно, что безлимит —
    это /photounlim (фотосрез называл те же файлы «photounlim:/имя»). Скачанное не трогаем.
    """
    c = db()
    rows = c.execute(
        "SELECT file_id, preview_path FROM unlim_files WHERE status IN ('pending', 'failed') "
        "AND substr(file_id, 1, ?) != ? AND substr(file_id, 1, ?) != ?",
        (len(UNLIM_ROOT) + 1, UNLIM_ROOT + "/", len("photounlim:"), "photounlim:"),
    ).fetchall()
    if not rows:
        return 0
    # Превью таких строк — копии фото Диска; у Диска свои превью
    for r in rows:
        remove_preview(r["preview_path"], DOWNLOAD_DIR)
    with sqlite_tx(c):
        c.executemany("DELETE FROM unlim_files WHERE file_id=? AND status IN ('pending', 'failed')",
                      [(r["file_id"],) for r in rows])
    log.info("убрано строк старого списка вне безлимита: %d", len(rows))
    return len(rows)


def _list_page_patiently(s, sk, uid, offset: int) -> list[dict]:
    """Страница списка; не пришла — ещё попытки с паузами LIST_PAGE_PAUSES. Отказ во входе — сразу наружу."""
    for pause in LIST_PAGE_PAUSES:
        try:
            return list_unlim_page(s, sk, uid, offset)
        except AuthExpiredError:
            raise
        except Exception as e:
            log.warning("mpfs/list offset=%d: %s — повтор через %d с", offset, e, pause)
            print(f"  Яндекс не отдал часть списка — пробую ещё раз через {pause} с", flush=True)
            time.sleep(pause)
    return list_unlim_page(s, sk, uid, offset)


def collect_via_list(s, sk, uid) -> int:
    """Весь безлимит постранично. Возвращает, сколько файлов увидели.

    Первая страница не пришла — её исключение наружу: mpfs/list недоступен. Оборвался дальше —
    ListBroken: прочитанные страницы уже в базе.
    """
    print("\nЧитаю список безлимита (mpfs/list)...", flush=True)
    seen = added = 0
    total_bytes = 0
    dirs = 0
    offset = 0
    # Пути, уже присланные в этом сборе. Полная страница без единого нового значит, что Яндекс
    # повторяет страницы: без этой проверки сбор шёл бы бесконечно, а «Найдено» всё росло
    known: set[str] = set()
    while True:
        try:
            # Терпеливо — только посреди списка: там сбой стоил бы всего уже прочитанного
            page = _list_page_patiently(s, sk, uid, offset) if offset else list_unlim_page(s, sk, uid, offset)
        except Exception as e:
            if not offset:
                raise
            raise ListBroken(seen, e) from e
        children = [x for x in page if (x.get("path") or "").rstrip("/") != UNLIM_ROOT]
        fresh = []
        for x in children:
            if x.get("path") not in known:
                known.add(x.get("path"))
                fresh.append(x)
        if len(children) >= LIST_PAGE and not fresh:
            raise ListBroken(seen, RuntimeError(
                f"Яндекс повторяет уже присланные страницы списка (с {offset}-го файла)"))
        files = [x for x in fresh if x.get("type") == "file"]
        dirs += len(fresh) - len(files)
        added += save_listed_files(files)
        seen += len(files)
        total_bytes += sum(int((x.get("meta") or {}).get("size") or 0) for x in files)
        print(f"  Найдено: {seen} файлов ({human_bytes(total_bytes)}), новых {added}", flush=True)
        if len(children) < LIST_PAGE:
            break
        offset += len(children)
    if dirs:
        log.warning("mpfs/list: в %s есть вложенные папки (%d) — их содержимое не собрано", UNLIM_ROOT, dirs)
    log.info("mpfs/list: %d файлов, %s, новых %d", seen, human_bytes(total_bytes), added)
    return seen


# ── Логика сбора photoslice ─────────────────────────────────────────────────


def init_snapshot(s, sk, uid) -> str:
    d = api_call(s, sk, uid, "intapi/photo-init-snapshot")
    psid = d.get("photoslice_id")
    if not psid:
        raise RuntimeError(f"no photoslice_id in {d}")
    return psid


def get_all_clusters(s, sk, uid, psid, batch_size: int = 5000) -> list[dict]:
    """Получаем плоский список всех кластеров (дни) с поддержкой пагинации."""
    all_items: list[dict] = []
    offset = 0
    while True:
        snap = api_call(
            s, sk, uid, "intapi/photo-get-snapshot",
            {"id": psid, "locale": "ru", "amount": batch_size, "offset": offset},
        )
        items = snap.get("items", []) if isinstance(snap, dict) else []
        if not items:
            break
        all_items.extend(items)
        if len(items) < batch_size:
            break
        offset += len(items)
    return all_items


def save_clusters(items: list[dict]):
    """Сохраняет кластеры (дни). Если в известном дне стало больше файлов —
    он перезапрашивается: иначе новые фото в старых днях никогда не попадут в бэкап.
    """
    with sqlite_tx(db()) as c:
        c.executemany(
            """INSERT INTO unlim_clusters (cluster_id, size) VALUES (?, ?)
               ON CONFLICT(cluster_id) DO UPDATE SET
                   fetched = CASE WHEN excluded.size <> unlim_clusters.size THEN 0 ELSE fetched END,
                   size = excluded.size""",
            [(it.get("id"), int(it.get("size", 0) or 0)) for it in items],
        )


def fetch_cluster_resources(s, sk, uid, psid, cluster_rows: list[dict]) -> int:
    """Запрашивает файлы для пачки кластеров."""
    clusters_map = {}
    for row in cluster_rows:
        size = max(int(row["size"] or 0), 1)
        clusters_map[row["cluster_id"]] = {"range": [0, max(size - 1, 0)]}
    res = api_call(s, sk, uid, "intapi/photo-get-clusters-with-resources", {
        "photosliceId": psid,
        "clusters": clusters_map,
        "hideScreenshots": False,
    })
    fetched = (res.get("clusters") or {}).get("fetched") or []
    added = 0
    with sqlite_tx(db()) as c:
        for cl in fetched:
            cid = cl.get("id")
            for f in cl.get("items", []) or []:
                fid = f.get("id") or f.get("resource_id")
                # Фотосрез отдаёт все фото аккаунта: и обычного Диска (их качает ветка Диска,
                # отсюда они ушли бы второй копией в _unlim), и корзины (их не скачать). Берём
                # только безлимит
                if not fid or not is_unlim_id(fid):
                    continue
                name = f.get("name") or fid.rsplit("/", 1)[-1]
                size = int(f.get("size", 0) or 0)
                md5 = f.get("md5") or f.get("etag")
                mime = f.get("mimetype") or f.get("type")
                # Файл, который mpfs/list уже записал как «/photounlim/имя», второй строкой не нужен
                added += c.execute(
                    """INSERT INTO unlim_files
                       (file_id, cluster_id, name, size, md5, mime, status)
                       SELECT ?, ?, ?, ?, ?, ?, 'pending'
                       WHERE NOT EXISTS (SELECT 1 FROM unlim_files WHERE file_id IN (?, ?))""",
                    (fid, cid, name, size, md5, mime, fid, unlim_twin(fid)),
                ).rowcount
            c.execute("UPDATE unlim_clusters SET fetched=1 WHERE cluster_id=?",
                      (cid,))
    return added


# ── CLI ─────────────────────────────────────────────────────────────────────


def collect_via_photoslice(s, sk, uid) -> int | None:
    """Запасной сбор через фотосрез. Возвращает 2, если куки протухли."""
    psid = init_snapshot(s, sk, uid)
    print("  ✓ photoslice_id получен")

    clusters = get_all_clusters(s, sk, uid, psid)
    print(f"  ✓ кластеров (дней с фото): {len(clusters)}")
    save_clusters(clusters)

    todo = [dict(r) for r in db().execute(
        "SELECT cluster_id, size FROM unlim_clusters WHERE fetched=0"
    )]
    print(f"\nЗапрашиваю файлы для {len(todo)} кластеров...")
    bar = tqdm(total=len(todo), unit="cluster", disable=not sys.stderr.isatty())
    added_total = 0
    try:
        for i in range(0, len(todo), CLUSTER_BATCH):
            batch = todo[i:i + CLUSTER_BATCH]
            try:
                added_total += fetch_cluster_resources(s, sk, uid, psid, batch)
            except AuthExpiredError as e:
                # Куки протухли посреди сбора — дальше бессмысленно, и это не «успех»
                print(f"\n❌ auth_expired | Ошибка авторизации: {e}")
                print("   Обновите cookies.json в Web UI или браузере и запустите сбор заново.")
                return 2
            except Exception as e:
                log.error(f"batch {i}: {e}")
                time.sleep(2)
            bar.update(len(batch))
            bar.set_postfix(files=added_total)
    finally:
        bar.close()
    return None


def _session_alive(s) -> bool:
    """True, если Яндекс по-прежнему пускает по этим кукам (сбой связи — не повод их менять)."""
    try:
        get_sk_uid(s)
        return True
    except AuthExpiredError:
        return False
    except Exception:
        return True


def cmd_collect():
    try:
        db_init()
    except sqlite3.OperationalError as e:
        if not is_sqlite_disk_error(e):
            raise
        print(f"❌ local_disk | Не получается записать базу unlim_state.db: {e}. "
              f"Освободите место или проверьте, что диск подключён.")
        return EXIT_DISK_FULL
    try:
        s = make_session(load_cookies())
    except CookiesFormatError as e:
        print(f"❌ auth_expired | {e}")
        return EXIT_AUTH
    except (ValueError, OSError) as e:  # битый JSON, не тот файл, нет прав на чтение
        print(f"❌ auth_expired | Не получается прочитать cookies.json ({e}). "
              f"Обновите доступ к Безлимиту в «Настройках».")
        return EXIT_AUTH
    try:
        try:
            sk, uid = get_sk_uid(s)
        except AuthExpiredError as e:
            print(f"❌ auth_expired | Ошибка авторизации: {e}")
            print("   Обновите cookies.json в Web UI или браузере.")
            return EXIT_AUTH
        except YandexUnavailableError as e:
            print(f"❌ server_50x | Яндекс не отвечает: {e}. Список соберу при следующей попытке.")
            return EXIT_OUTAGE
        except UnlimError as e:
            # Страница пришла, но без sk/uid: капча или новая вёрстка — повторим позже,
            # это не повод ни менять куки, ни считать список собранным
            print(f"❌ server_50x | Не удалось войти в веб-версию Диска: {e}. Повторю позже.")
            return EXIT_OUTAGE
        print(f"  ✓ uid={uid}, sk OK")

        list_error = None
        try:
            listed = collect_via_list(s, sk, uid)
        except Exception as e:  # noqa: BLE001 — разбираем ниже
            listed = e.seen if isinstance(e, ListBroken) else 0
            cause = e.cause if isinstance(e, ListBroken) else e
            if isinstance(cause, AuthExpiredError):
                # 401/403 на внутренний метод бывает и при живых куках — перепроверяем вход
                if not _session_alive(s):
                    print(f"\n❌ auth_expired | Ошибка авторизации: {cause}")
                    print("   Обновите cookies.json в Web UI или браузере и запустите сбор заново.")
                    return EXIT_AUTH
                list_error = f"Яндекс отклонил запрос списка ({cause}), хотя вход действует"
            else:
                list_error = str(cause)
            log.error(f"mpfs/list: {list_error}")
        if listed and list_error:
            print(f"  ⚠️ Список безлимита оборвался ({list_error}). Уже найденное сохранено: {files_text(listed)}")
        # Фотосрез — только когда mpfs/list не нашёл ни одного файла: после части списка он
        # ничего не добавил бы, кроме риска вторых копий
        if not listed:
            if list_error:
                print(f"  ⚠️ Список через mpfs/list не получен ({list_error}) — пробую фотосрез")
            try:
                code = collect_via_photoslice(s, sk, uid)
            except Exception as e:  # noqa: BLE001 — запасной путь не должен ронять сбор
                log.error(f"photoslice: {e}")
                list_error = list_error or str(e)
                code = None
            if code:
                return code
        if not list_error:
            dropped = drop_rows_outside_unlim()
            if dropped:
                print(f"  ↷ Убрал из очереди {dropped} файлов старого списка: они лежат на обычном Диске, "
                      f"их скачивает Диск")

        stats = status_counts()
        if not stats and list_error:
            print(f"❌ server_50x | Яндекс не отдал список файлов безлимита: {list_error}. "
                  f"Повторю позже — уже собранное не теряется.")
            return EXIT_OUTAGE
        print(f"\n  ✓ Файлов в БД: {sum(stats.values())}")
        for k, v in stats.items():
            print(f"     {k}: {v}")
        if not stats:
            print("  ⚠️ Яндекс не вернул ни одного файла безлимита. Если в веб-версии Диска фото есть — "
                  "обновите cookies.json и запустите сбор ещё раз.")
        if list_error:
            # Часть страниц прочитать не удалось: собранное сохранено, но список неполный —
            # код 3, чтобы Web UI не запомнил его как полный на 6 часов
            print(f"❌ server_50x | Список безлимита получен не полностью: {list_error}. "
                  f"Уже собранное не теряется — дособеру при следующей попытке.")
            return EXIT_OUTAGE
    finally:
        try:
            s.close()
        except Exception:
            pass


def cmd_status():
    if not os.path.exists(DB_FILE):
        print("БД пуста — запусти --collect")
        return
    db_init()
    stats = status_counts()
    print(f"Всего файлов в безлимите: {sum(stats.values())}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


def cmd_retry_failed():
    """Возвращает failed-файлы и зависшие in_progress в очередь (сбрасывает счётчик попыток)."""
    if not os.path.exists(DB_FILE):
        print("БД пуста — запусти --collect")
        return
    db_init()
    n = reset_failed_files(DB_FILE, include_in_progress=True)
    print(f"Возвращено в очередь: {n}")


def main():
    ensure_utf8_stdio()

    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true",
                    help="Собрать список файлов безлимита")
    ap.add_argument("--status", action="store_true",
                    help="Показать статус БД")
    ap.add_argument("--retry-failed", action="store_true",
                    help="Вернуть упавшие и зависшие файлы в очередь")
    args = ap.parse_args()
    _setup_logging()
    if args.collect:
        return cmd_collect()
    elif args.status:
        cmd_status()
    elif args.retry_failed:
        cmd_retry_failed()
    else:
        ap.print_help()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
