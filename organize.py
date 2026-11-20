"""Сортирует папку с бэкапом по полочкам.

Что делает:
  1. Сканирует папку рекурсивно
  2. Дедуп по (size, md5) — дубликаты → _duplicates/
  3. Раскладывает по типам:
     - Фото:    Фото/YYYY/YYYY-MM/
     - Видео:   Видео/YYYY/YYYY-MM/
     - Док:     Документы/
     - Архивы:  Архивы/
     - Программы: Программы/
     - Аудио:   Аудио/
     - Прочее:  Прочее/

  Дату берёт из имени файла (YYYY-MM-DD), затем из EXIF фото, иначе из mtime.

  Внимание: при перемещении папки, как на Диске, пропадают — каждый файл уезжает в общую
  папку своего типа, опустевшие папки удаляются. Сами файлы не удаляются. Отчёт
  (report-only) заранее показывает, сколько папок исчезнет, и примеры переносов.

Запуск:
  python organize.py --input "E:/папка" --action report-only
  python organize.py --input "E:/папка" --action move
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from utils import (
    CASE_INSENSITIVE_PATHS,
    DIR_ARCHIVE,
    DIR_AUDIO,
    DIR_DOC,
    DIR_DUP,
    DIR_OTHER,
    DIR_PHOTO,
    DIR_PROGRAM,
    DIR_VIDEO,
    LINKS_DIRNAME,
    MAX_NAME_LEN,
    PREVIEW_DIRNAME,
    SET_ASIDE_DIRNAME,
    _strip_long_prefix,
    classify_dir,
    compute_md5,
    ensure_utf8_stdio,
    human_bytes,
    is_safe_dir,
    keep_awake,
    name_with_tag,
    path_key,
    sqlite_tx,
    to_long_path,
)

# Папки, скачанные по ссылкам, — чужие подборки: их структуру не трогаем. Старые копии,
# отложенные проверкой скачанного, человек разбирает сам
SKIP_DIRS = {DIR_PHOTO, DIR_VIDEO, DIR_DOC, DIR_ARCHIVE, DIR_PROGRAM,
             DIR_AUDIO, DIR_OTHER, DIR_DUP, ".sync", "_unlim_tmp_zips", PREVIEW_DIRNAME, LINKS_DIRNAME,
             SET_ASIDE_DIRNAME}
_SKIP_DIRS_KEYS = {d.casefold() for d in SKIP_DIRS} if CASE_INSENSITIVE_PATHS else set(SKIP_DIRS)
# Недокачанные файлы и БД состояния — не трогаем
SKIP_SUFFIXES = (".part", ".db", ".db-wal", ".db-shm")
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".mts", ".m2ts"})

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _is_skip_dir_name(name: str) -> bool:
    return (name.casefold() if CASE_INSENSITIVE_PATHS else name) in _SKIP_DIRS_KEYS


def is_project_dir(root: str) -> bool:
    """Папка проекта или её родитель: move-режим утащил бы state.db, .env и сами скрипты."""
    try:
        r = path_key(os.path.realpath(to_long_path(root)))
        p = path_key(os.path.realpath(to_long_path(PROJECT_DIR)))
        return os.path.commonpath([r, p]) == r
    except (OSError, ValueError):  # разные диски на Windows
        return False

DATE_REGEXES = [
    re.compile(r"(?<!\d)(19\d{2}|20\d{2})[-_.](\d{2})[-_.](\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(19\d{2}|20\d{2})(\d{2})(\d{2})(?!\d)"),
]


def _parse_exif_date_str(val: str | bytes) -> tuple[int, int] | None:
    if isinstance(val, bytes):
        try:
            val = val.decode("utf-8", "ignore")
        except Exception:
            return None
    s = str(val).strip()
    m = re.match(r"^(19\d{2}|20\d{2})[:\-\.](\d{2})", s)
    if m:
        try:
            y, mo = int(m.group(1)), int(m.group(2))
            if 1970 <= y <= 2035 and 1 <= mo <= 12:
                return y, mo
        except (ValueError, IndexError):
            pass
    return None


# Даты EXIF по старшинству: съёмка (DateTimeOriginal), оцифровка (DateTimeDigitized), последняя
# правка файла (DateTime). Дату из GPS не берём никогда: она в UTC, и снимок сразу после полуночи
# 1-го числа уехал бы в прошлый месяц
EXIF_DATE_TAGS = (0x9003, 0x9004, 0x0132)
_EXIF_IFD_POINTER = 0x8769
_EXIF_ASCII = 2


def _exif_tiff_dates(tiff: bytes) -> dict[int, bytes]:
    """Даты EXIF_DATE_TAGS из TIFF-структуры EXIF: тег → сырое значение.

    Читает только IFD0 и Exif IFD — туда, где их пишут камеры. GPS IFD, миниатюру (IFD1) и
    MakerNote не трогает: даты там другие (UTC, правка миниатюры, внутренние счётчики камеры).
    """
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if order is None or len(tiff) < 8:
        return {}
    dates: dict[int, bytes] = {}

    def walk(offset: int, inside_exif: bool) -> None:
        if offset < 8 or offset + 2 > len(tiff):
            return
        (count,) = struct.unpack_from(order + "H", tiff, offset)
        for i in range(count):
            entry = offset + 2 + 12 * i
            if entry + 12 > len(tiff):
                return
            tag, typ, n, value = struct.unpack_from(order + "HHII", tiff, entry)
            if tag == _EXIF_IFD_POINTER and not inside_exif:
                walk(value, True)
            elif tag in EXIF_DATE_TAGS and typ == _EXIF_ASCII and n > 4:
                dates.setdefault(tag, tiff[value:value + n])

    walk(struct.unpack_from(order + "I", tiff, 4)[0], False)
    return dates


def _first_exif_date(values: dict) -> tuple[int, int] | None:
    """Первая разборчивая дата по старшинству EXIF_DATE_TAGS (пустую «    :  :  » пропускает)."""
    for tag in EXIF_DATE_TAGS:
        if values.get(tag):
            d = _parse_exif_date_str(values[tag])
            if d:
                return d
    return None


def _get_exif_date_jpeg(path: str) -> tuple[int, int] | None:
    """Извлекает дату съёмки из APP1 сегмента JPEG без внешних зависимостей."""
    try:
        with open(to_long_path(path), "rb") as f:
            header = f.read(2)
            if header != b"\xff\xd8":
                return None
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xff or marker in (b"\xff\xd9", b"\xff\xda"):
                    break
                length_bytes = f.read(2)
                if len(length_bytes) < 2:
                    break
                length = int.from_bytes(length_bytes, "big") - 2
                if length <= 0:
                    continue
                if marker == b"\xff\xe1":  # APP1 (Exif или XMP — ищем именно Exif)
                    data = f.read(length)
                    if data.startswith(b"Exif\x00\x00"):
                        return _first_exif_date(_exif_tiff_dates(data[6:]))
                    continue
                else:
                    f.seek(length, os.SEEK_CUR)
    except Exception:
        pass
    return None


def _get_exif_date_pil(path: str) -> tuple[int, int] | None:
    """Извлекает дату съёмки через Pillow для форматов HEIC, TIFF, PNG, WebP и т.д."""
    try:
        from PIL import Image
        with Image.open(to_long_path(path)) as img:
            exif = img.getexif()
            if not exif:
                return None
            values = {0x0132: exif.get(0x0132)}
            try:
                exif_ifd = exif.get_ifd(_EXIF_IFD_POINTER)
                if exif_ifd:
                    values.update({tag: exif_ifd.get(tag) for tag in (0x9003, 0x9004)})
            except Exception:
                pass
            return _first_exif_date(values)
    except Exception:
        pass
    return None


def get_exif_date(path: str) -> tuple[int, int] | None:
    """Извлекает дату съёмки из метаданных изображения (EXIF)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _VIDEO_EXTS:
        return None
    if ext in (".jpg", ".jpeg"):
        d = _get_exif_date_jpeg(path)
        if d:
            return d
    return _get_exif_date_pil(path)


def get_date(path: str) -> tuple[int, int]:
    name = os.path.basename(path)
    # 1. Поиск даты в имени файла
    for rx in DATE_REGEXES:
        m = rx.search(name)
        if m:
            try:
                y = int(m.group(1))
                mo = int(m.group(2))
                day = int(m.group(3))
                if 1970 <= y <= 2035 and 1 <= mo <= 12 and 1 <= day <= 31:
                    return y, mo
            except (ValueError, IndexError):
                pass
    # 2. Поиск даты съёмки в EXIF метаданных для изображений
    exif_date = get_exif_date(path)
    if exif_date:
        return exif_date
    # 3. Fallback на mtime файла
    try:
        t = time.localtime(os.path.getmtime(to_long_path(path)))
        return t.tm_year, t.tm_mon
    except OSError:
        return 1970, 1


def target_path(root: str, src: str, category: str) -> str:
    name = os.path.basename(src)
    if category in (DIR_PHOTO, DIR_VIDEO):
        y, mo = get_date(src)
        sub = os.path.join(root, category, str(y), f"{y}-{mo:02d}")
    else:
        sub = os.path.join(root, category)
    return os.path.join(sub, name)


def unique_path(dst: str, counters: dict[str, int] | None = None) -> str:
    if not os.path.lexists(to_long_path(dst)):
        return dst
    folder, fname = os.path.split(dst)
    key = path_key(dst)
    i = counters.get(key, 1) if counters is not None else 1
    while True:
        cand = os.path.join(folder, name_with_tag(fname, f"_{i}", MAX_NAME_LEN))
        if not os.path.lexists(to_long_path(cand)):
            if counters is not None:
                counters[key] = i + 1
            return cand
        i += 1


# Бит «имя-заменитель» в теге точки повторной обработки Windows: так помечены ссылки на другое
# место — символьные ссылки (IO_REPARSE_TAG_SYMLINK, 0xA000000C), junction и подключённые папкой
# диски (IO_REPARSE_TAG_MOUNT_POINT, 0xA0000003)
_REPARSE_TAG_NAME_SURROGATE = 0x20000000


def _is_link_or_mount(path: str) -> bool:
    """Ссылка, junction Windows или другой диск, подключённый папкой: внутрь не ходим.

    os.walk не считает junction ссылкой: сортировка уносила бы файлы с другого диска
    (копированием, которое при нехватке места оставляет обрезанный файл) и удаляла бы там папки.
    Прочие точки повторной обработки — не ссылки: ими помечены, например, папки OneDrive
    («Рабочий стол» и «Документы» с резервным копированием), и в них сортировка ходит.
    """
    lp = to_long_path(path)
    try:
        if os.path.islink(lp) or os.path.ismount(lp):
            return True
        tag = getattr(os.lstat(lp), "st_reparse_tag", 0)
    except OSError:
        return True
    return bool(tag & _REPARSE_TAG_NAME_SURROGATE)


def scan(root: str, tree: list | None = None) -> list[dict]:
    """Файлы для сортировки. В tree (если передан) ложится сам обход: (папка, подпапки, файлы).

    В подпапках tree — и те, куда обход не пошёл (ссылки, служебные папки): папка со ссылкой
    внутри не опустеет, и уборка её не удалит — отчёт не должен обещать обратное.
    """
    files = []
    skip = {path_key(os.path.join(root, d)) for d in SKIP_DIRS}
    print(f"\n[1/4] Сканирую {root}...")
    for raw_dp, dirs, fns in os.walk(to_long_path(root)):
        dp = _strip_long_prefix(raw_dp)
        if tree is not None:
            tree.append((dp, list(dirs), list(fns)))
        dirs[:] = [
            d for d in dirs
            if path_key(os.path.join(dp, d)) not in skip and not _is_link_or_mount(os.path.join(dp, d))
        ]
        for fn in fns:
            if fn.lower().endswith(SKIP_SUFFIXES):
                continue
            full = os.path.join(dp, fn)
            try:
                st = os.lstat(to_long_path(full))
                if not stat.S_ISREG(st.st_mode):
                    continue
                files.append({"path": full, "size": st.st_size})
            except OSError:
                pass
    print(f"  Найдено: {len(files)}")
    return files


def compute_md5_batch(files: list[dict]):
    print("\n[2/4] MD5 для подозреваемых...")
    by_size = defaultdict(list)
    for f in files:
        if (f.get("size") or 0) > 0:
            by_size[f["size"]].append(f)
    suspect = [f for v in by_size.values() if len(v) > 1 for f in v]
    print(f"  Подозреваемых: {len(suspect)}")
    for i, f in enumerate(suspect):
        try:
            f["md5"] = compute_md5(f["path"])
        except OSError:
            f["md5"] = None
        if (i + 1) % 500 == 0:
            print(f"    progress: {i+1}/{len(suspect)}")


def find_dup_paths(files: list[dict]) -> set[str]:
    g = defaultdict(list)
    for f in files:
        if f.get("md5") and (f.get("size") or 0) > 0:
            g[(f["size"], f["md5"])].append(f)
    out = set()
    for grp in g.values():
        if len(grp) > 1:
            grp.sort(key=lambda x: (len(x["path"]), x["path"]))
            for d in grp[1:]:
                out.add(d["path"])
    return out


def doomed_dirs(root: str, tree: list, moving: set[str], targets) -> list[str]:
    """Папки, которые после перемещения опустеют и будут удалены уборкой в конце main().

    Повторяет уборку: корень и отсечённые служебные папки не трогаются; папка уходит, если в ней
    не осталось ни файла, ни уцелевшей подпапки. Файлы, которые сортировка не берёт (.part, .db),
    и ссылки на папки (обход в них не заходит, уборка их не удаляет) держат папку; папка, куда
    что-то переедет (на Windows «фото» и «Фото» — одна папка), остаётся. Не угадывает только
    сбои перемещения и удаления (файл занят, нет прав).
    """
    root_key = os.path.normcase(root)
    alive: set[str] = set()
    for t in targets:
        d = os.path.normcase(os.path.dirname(t))
        while len(d) > len(root_key) and d not in alive:
            alive.add(d)
            d = os.path.dirname(d)
    gone: set[str] = set()
    # os.walk идёт сверху вниз — задом наперёд подпапки проверяются раньше родителей
    for dp, dirs, fns in reversed(tree):
        if dp == root or os.path.normcase(dp) in alive:
            continue
        if (all(os.path.join(dp, f) in moving for f in fns)
                and all(os.path.join(dp, d) in gone for d in dirs)):
            gone.add(dp)
    return sorted(gone)


# Порядок примеров в отчёте: документы первыми — их раскладка по типам удивляет сильнее всего
EXAMPLE_ORDER = [DIR_DOC, DIR_PHOTO, DIR_VIDEO, DIR_AUDIO, DIR_ARCHIVE, DIR_PROGRAM, DIR_OTHER]


def _printable(text: str) -> str:
    """Имя с непарным суррогатом уронило бы print: stdout строгий после ensure_utf8_stdio."""
    return text.encode("utf-8", "replace").decode("utf-8")


def pick_examples(root: str, plan: list, moving: set[str], limit: int = 3) -> list[dict]:
    """Примеры переносов для отчёта: по одному на полку, пути относительно root.

    Берёт самый глубоко вложенный файл — он теряет больше всего папок, при равенстве — первый
    по алфавиту. Если подпапок нет, примеры — из файлов в самой папке. Пути режутся срезом
    строки, а не relpath: на сотнях тысяч файлов relpath заметно медленнее.
    """
    deep: dict[str, tuple] = {}
    flat: dict[str, tuple] = {}
    for src, dst, is_dup in plan:
        if is_dup or src not in moving:
            continue
        rel_src = src[len(root):].lstrip("\\/")
        rel_dst = dst[len(root):].lstrip("\\/")
        depth = rel_src.count(os.sep)
        pool = deep if depth else flat
        shelf = rel_dst.split(os.sep, 1)[0]
        key = (-depth, rel_src.casefold(), rel_src)
        if shelf not in pool or key < pool[shelf][0]:
            pool[shelf] = (key, rel_src, rel_dst)
    chosen = deep or flat
    return [{"from": _printable(chosen[s][1]), "to": _printable(chosen[s][2])}
            for s in EXAMPLE_ORDER if s in chosen][:limit]


def main():
    ensure_utf8_stdio()

    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--action", default="report-only",
                    choices=["report-only", "move"])
    args = ap.parse_args()

    root = os.path.abspath(args.input)
    if not os.path.isdir(to_long_path(root)):
        sys.exit(f"Нет папки: {root}")

    # Та же проверка, что и в Web UI: корень диска, домашняя и системные папки — нельзя
    ok, why = is_safe_dir(root, must_exist=True)
    if not ok:
        sys.exit(f"Опасный путь для сортировки: {why}")
    if is_project_dir(root):
        sys.exit("Опасный путь для сортировки: это папка самого инструмента (state.db, .env, скрипты)")

    print("=" * 60)
    print(f"  СОРТИРОВКА: {root}")
    print(f"  Действие: {args.action}")
    print("=" * 60)

    with keep_awake():
        return _run_organize(root, args.action)


def sync_moved_paths_in_dbs(moved_pairs: list[tuple[str, str]], db_dir: Path | None = None) -> int:
    """Обновляет local_path перенесённых файлов в state.db и unlim_state.db.

    Без этого после сортировки проверка скачанного (verify) считала бы перенесённые файлы
    недостающими. Ошибки БД не прерывают сортировку.
    """
    if not moved_pairs:
        return 0
    base = Path(__file__).resolve().parent if db_dir is None else Path(db_dir)
    moved_map = {path_key(src): dst for src, dst in moved_pairs}
    total_updated = 0
    for db_name, table, key_col in (
        ("state.db", "files", "remote_path"),
        ("unlim_state.db", "unlim_files", "file_id"),
    ):
        db_path = base / db_name
        if not db_path.is_file():
            continue
        c = None
        try:
            c = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
            if not c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                continue
            updates: list[tuple[str, str]] = []
            for key_val, lp in c.execute(
                f"SELECT {key_col}, local_path FROM {table} "
                f"WHERE local_path IS NOT NULL AND local_path <> ''"
            ):
                new_path = moved_map.get(path_key(lp))
                if new_path and new_path != lp:
                    updates.append((new_path, key_val))
            if updates:
                for start in range(0, len(updates), 5000):
                    with sqlite_tx(c):
                        c.executemany(
                            f"UPDATE {table} SET local_path = ? WHERE {key_col} = ?",
                            updates[start:start + 5000],
                        )
                total_updated += len(updates)
        except sqlite3.Error as e:
            print(_printable(f"  [warn] не удалось обновить пути в {db_name}: {e}"))
        finally:
            if c is not None:
                c.close()
    return total_updated


def _run_organize(root: str, action: str) -> int:
    t0 = time.time()
    tree: list = []
    files = scan(root, tree)
    compute_md5_batch(files)
    dups = find_dup_paths(files)

    print("\n[3/4] Раскладываю...")
    plan = []
    cat_n = Counter()
    cat_b = Counter()
    dup_b = 0
    for f in files:
        src = f["path"]
        if src in dups:
            rel = os.path.relpath(src, root)
            plan.append((src, os.path.join(root, DIR_DUP, rel), True))
            dup_b += f["size"]
        else:
            cat = classify_dir(src)
            plan.append((src, target_path(root, src, cat), False))
            cat_n[cat] += 1
            cat_b[cat] += f["size"]

    total_b = sum(f["size"] for f in files)
    # Прогноз для отчёта: какие папки опустеют и будут удалены, и пара примеров переносов
    moving = {src for src, dst, _ in plan if path_key(src) != path_key(dst)}
    gone = doomed_dirs(root, tree, moving, [dst for _, dst, _ in plan])
    examples = pick_examples(root, plan, moving)
    print("\n[4/4] СТАТИСТИКА:")
    print(f"  Всего:           {len(files)} ({human_bytes(total_b)})")
    print(f"  Дубликатов:      {len(dups)} ({human_bytes(dup_b)})")
    print(f"  Уникальных:      {len(files) - len(dups)}")
    print(f"  Папок исчезнет:  {len(gone)} (опустеют и будут удалены)")
    print("\n  По полкам:")
    for cat in [DIR_PHOTO, DIR_VIDEO, DIR_AUDIO, DIR_DOC, DIR_ARCHIVE,
                DIR_PROGRAM, DIR_OTHER]:
        if cat_n[cat]:
            print(f"    {cat:<12} {cat_n[cat]:>6} файлов ({human_bytes(cat_b[cat])})")
    if examples:
        print("\n  Например:")
        for ex in examples:
            print(f"    {ex['from']}  →  {ex['to']}")
    print(f"\n  Скан: {time.time() - t0:.0f} сек")
    # Итог одной строкой для Web UI («Посмотреть, что получится»)
    shelves = {cat: {"count": cat_n[cat], "bytes": cat_b[cat]}
               for cat in [DIR_PHOTO, DIR_VIDEO, DIR_AUDIO, DIR_DOC, DIR_ARCHIVE, DIR_PROGRAM, DIR_OTHER]
               if cat_n[cat]}
    print("organize_summary | " + json.dumps({
        "action": action, "root": root, "total": len(files), "total_bytes": total_b,
        "duplicates": len(dups), "duplicates_bytes": dup_b, "shelves": shelves,
        "folders": len(gone), "examples": examples,
    }, ensure_ascii=False), flush=True)

    if action == "report-only":
        print("\n[INFO] Это только отчёт: файлы не перемещались. При перемещении папки, как на "
              "Диске, исчезнут: файлы разъедутся по папкам по типам, опустевшие папки удалятся.")
        return 0

    print(f"\nПеремещаю {len(plan)}...")
    moved = 0
    err = 0
    last = time.time()
    uniq_counters: dict[str, int] = {}
    moved_pairs: list[tuple[str, str]] = []
    for src, dst, _is_dup in plan:
        if path_key(os.path.abspath(src)) == path_key(os.path.abspath(dst)):
            continue  # Файл уже лежит в нужной папке
        try:
            target = unique_path(dst, uniq_counters)
            if path_key(os.path.abspath(src)) == path_key(os.path.abspath(target)):
                continue
            os.makedirs(to_long_path(os.path.dirname(target)), exist_ok=True)
            shutil.move(to_long_path(src), to_long_path(target))
            moved_pairs.append((src, target))
            moved += 1
        except Exception as e:
            err += 1
            if err <= 5:
                print(_printable(f"  err: {src}: {e}"))
        if time.time() - last > 5:
            print(f"  progress: {moved}/{len(plan)} (err={err})")
            last = time.time()

    db_updated = sync_moved_paths_in_dbs(moved_pairs)
    if db_updated:
        print(f"  Обновлено путей в базе: {db_updated}")

    cleaned = 0
    walked = []  # сверху вниз, без ссылок и junction; убираем снизу вверх
    for raw_dp, dirs, _fns in os.walk(to_long_path(root)):
        dp = _strip_long_prefix(raw_dp)
        dirs[:] = [d for d in dirs if not _is_link_or_mount(os.path.join(dp, d))]
        walked.append(dp)
    for dp in reversed(walked):
        rel = os.path.relpath(dp, root)
        first = rel.split(os.sep)[0] if rel != "." else ""
        if _is_skip_dir_name(first):
            continue
        try:
            lp = to_long_path(dp)
            if not os.listdir(lp) and path_key(dp) != path_key(root):
                os.rmdir(lp)
                cleaned += 1
        except OSError:
            pass

    print("\n✅ ГОТОВО:")
    print(f"  Перемещено: {moved}")
    print(f"  Ошибок:     {err}")
    print(f"  Удалено пустых папок: {cleaned}")
    print("organize_result | " + json.dumps({"moved": moved, "errors": err, "cleaned": cleaned}), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
