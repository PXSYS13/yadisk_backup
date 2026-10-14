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
import sys
import time
from collections import Counter, defaultdict

from utils import (
    DIR_ARCHIVE,
    DIR_AUDIO,
    DIR_DOC,
    DIR_DUP,
    DIR_OTHER,
    DIR_PHOTO,
    DIR_PROGRAM,
    DIR_VIDEO,
    LINKS_DIRNAME,
    PREVIEW_DIRNAME,
    classify_dir,
    compute_md5,
    ensure_utf8_stdio,
    human_bytes,
    is_safe_dir,
)

# Папки, скачанные по ссылкам, — чужие подборки: их структуру не трогаем
SKIP_DIRS = {DIR_PHOTO, DIR_VIDEO, DIR_DOC, DIR_ARCHIVE, DIR_PROGRAM,
             DIR_AUDIO, DIR_OTHER, DIR_DUP, ".sync", "_unlim_tmp_zips", PREVIEW_DIRNAME, LINKS_DIRNAME}
# Недокачанные файлы и БД состояния — не трогаем
SKIP_SUFFIXES = (".part", ".db", ".db-wal", ".db-shm")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def is_project_dir(root: str) -> bool:
    """Папка проекта или её родитель: move-режим утащил бы state.db, .env и сами скрипты."""
    try:
        return os.path.commonpath([os.path.abspath(root), PROJECT_DIR]) == os.path.abspath(root)
    except ValueError:  # разные диски на Windows
        return False

DATE_REGEXES = [
    re.compile(r"(19\d{2}|20\d{2})[-_.](\d{2})[-_.](\d{2})"),
    re.compile(r"(19\d{2}|20\d{2})(\d{2})(\d{2})"),
    re.compile(r"IMG_(19\d{2}|20\d{2})(\d{2})(\d{2})"),
    re.compile(r"VID-(19\d{2}|20\d{2})(\d{2})(\d{2})"),
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


def _get_exif_date_jpeg(path: str) -> tuple[int, int] | None:
    """Извлекает дату съёмки из APP1 сегмента JPEG без внешних зависимостей."""
    try:
        with open(path, "rb") as f:
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
                if marker == b"\xff\xe1":  # APP1 (Exif)
                    data = f.read(length)
                    if data.startswith(b"Exif\x00\x00"):
                        m = re.search(rb"(19\d{2}|20\d{2}):(\d{2}):(\d{2})", data)
                        if m:
                            y = int(m.group(1).decode("ascii"))
                            mo = int(m.group(2).decode("ascii"))
                            if 1970 <= y <= 2035 and 1 <= mo <= 12:
                                return y, mo
                    break
                else:
                    f.seek(length, os.SEEK_CUR)
    except Exception:
        pass
    return None


def _get_exif_date_pil(path: str) -> tuple[int, int] | None:
    """Извлекает дату съёмки через Pillow для форматов HEIC, TIFF, PNG, WebP и т.д."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            dt = exif.get(0x0132) or exif.get(306)
            try:
                exif_ifd = exif.get_ifd(0x8769)
                if exif_ifd:
                    dt = exif_ifd.get(0x9003) or exif_ifd.get(0x9004) or dt
            except Exception:
                pass
            if dt:
                return _parse_exif_date_str(dt)
    except Exception:
        pass
    return None


def get_exif_date(path: str) -> tuple[int, int] | None:
    """Извлекает дату съёмки из метаданных изображения (EXIF)."""
    ext = os.path.splitext(path)[1].lower()
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
                if 1970 <= y <= 2035 and 1 <= mo <= 12:
                    return y, mo
            except (ValueError, IndexError):
                pass
    # 2. Поиск даты съёмки в EXIF метаданных для изображений
    exif_date = get_exif_date(path)
    if exif_date:
        return exif_date
    # 3. Fallback на mtime файла
    try:
        t = time.localtime(os.path.getmtime(path))
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


def unique_path(dst: str) -> str:
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    i = 1
    while True:
        cand = f"{base}_{i}{ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def scan(root: str, tree: list | None = None) -> list[dict]:
    """Файлы для сортировки. В tree (если передан) ложится сам обход: (папка, подпапки, файлы)."""
    files = []
    skip = {os.path.join(root, d) for d in SKIP_DIRS}
    print(f"\n[1/4] Сканирую {root}...")
    for dp, dirs, fns in os.walk(root):
        dirs[:] = [d for d in dirs if os.path.join(dp, d) not in skip]
        if tree is not None:
            tree.append((dp, list(dirs), list(fns)))
        for fn in fns:
            if fn.lower().endswith(SKIP_SUFFIXES):
                continue
            full = os.path.join(dp, fn)
            try:
                files.append({"path": full, "size": os.path.getsize(full)})
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
    держат папку; папка, куда что-то переедет (на Windows «фото» и «Фото» — одна папка),
    остаётся. Не угадывает только сбои перемещения и удаления (файл занят, нет прав).
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
    if not os.path.isdir(root):
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
    moving = {src for src, dst, _ in plan if os.path.normcase(src) != os.path.normcase(dst)}
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
        "action": args.action, "root": root, "total": len(files), "total_bytes": total_b,
        "duplicates": len(dups), "duplicates_bytes": dup_b, "shelves": shelves,
        "folders": len(gone), "examples": examples,
    }, ensure_ascii=False), flush=True)

    if args.action == "report-only":
        print("\n[INFO] Это только отчёт: файлы не перемещались. При перемещении папки, как на "
              "Диске, исчезнут: файлы разъедутся по папкам по типам, опустевшие папки удалятся.")
        return 0

    print(f"\nПеремещаю {len(plan)}...")
    moved = 0
    err = 0
    last = time.time()
    for src, dst, is_dup in plan:
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            continue  # Файл уже лежит в нужной папке
        try:
            target = unique_path(dst)
            if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(target)):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(src, target)
            moved += 1
        except Exception as e:
            err += 1
            if err <= 5:
                print(_printable(f"  err: {src}: {e}"))
        if time.time() - last > 5:
            print(f"  progress: {moved}/{len(plan)} (err={err})")
            last = time.time()

    cleaned = 0
    for dp, dirs, fns in os.walk(root, topdown=False):
        rel = os.path.relpath(dp, root)
        first = rel.split(os.sep)[0] if rel != "." else ""
        if first in SKIP_DIRS:
            continue
        try:
            if not os.listdir(dp) and dp != root:
                os.rmdir(dp)
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
