"""Пост-обработка state.db после scan'а:
1) Удаляет все НЕ медиа-файлы (оставляет фото и видео).
2) Группирует оставшиеся по (md5, size) — дубликаты помечает skipped.

Запускать ПОСЛЕ scan, ДО download. Идемпотентно — повторный запуск ничего не сломает.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

DB = os.path.join(os.path.dirname(__file__), "state.db")

# Расширения которые считаем «медиа» — то что хочет пользователь
MEDIA_EXTS = {
    # фото
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif",
    ".bmp", ".tiff", ".tif", ".raw", ".arw", ".cr2", ".nef", ".dng", ".orf",
    # видео
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".3gp", ".3g2",
    ".mpg", ".mpeg", ".wmv", ".flv", ".vob", ".mts", ".m2ts",
}

# Приоритет папок — какой путь оставлять при дубликате
# (раньше в списке = выше приоритет = его оставляем, остальные → skipped)
PRIORITY_PREFIXES = [
    "disk:/Фотокамера/",
    "disk:/Фото/",
    "disk:/Скриншоты/",
    "disk:/Видео/",
    "disk:/Загрузки/",
]


def folder_priority(path: str) -> int:
    """Меньше = приоритетнее. Для не указанных папок = большой номер."""
    for i, prefix in enumerate(PRIORITY_PREFIXES):
        if path.startswith(prefix):
            return i
    return 1000


def is_media(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in MEDIA_EXTS


def main() -> int:
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")

    # ── шаг 0: статистика до ────────────────────────────────────────────────
    before = conn.execute(
        "SELECT status, COUNT(*) AS n, COALESCE(SUM(size), 0) AS b "
        "FROM files GROUP BY status"
    ).fetchall()
    print("ДО ОБРАБОТКИ:")
    total_before = 0
    bytes_before = 0
    for r in before:
        print(f"  {r['status']:<12} {r['n']:>5} файлов   {r['b']/1e9:.2f} ГБ")
        total_before += r["n"]
        bytes_before += r["b"]
    print(f"  {'итого':<12} {total_before:>5} файлов   {bytes_before/1e9:.2f} ГБ\n")

    # ── шаг 1: удалить не-медиа ─────────────────────────────────────────────
    # Удаляем только из pending — уже скачанное не трогаем (даже если это .docx,
    # пусть лежит, файл уже на диске)
    print("=" * 70)
    print("ШАГ 1: УДАЛЕНИЕ НЕ-МЕДИА ИЗ PENDING")
    print("=" * 70)
    rows = conn.execute(
        "SELECT remote_path, size FROM files WHERE status='pending'"
    ).fetchall()
    non_media = [r for r in rows if not is_media(r["remote_path"])]
    print(f"  pending всего: {len(rows)}")
    print(f"  из них НЕ медиа: {len(non_media)} файлов "
          f"({sum(r['size'] or 0 for r in non_media)/1e9:.2f} ГБ)")
    if non_media:
        # Покажем примеры
        from collections import Counter
        ext_counter = Counter()
        for r in non_media:
            ext_counter[os.path.splitext(r["remote_path"])[1].lower() or "<нет>"] += 1
        print("  топ расширений:")
        for ext, n in ext_counter.most_common(10):
            print(f"     {ext or '<нет>'}  {n}")

        # Удаляем
        conn.executemany(
            "DELETE FROM files WHERE remote_path = ?",
            [(r["remote_path"],) for r in non_media],
        )
        conn.commit()
        print(f"  ✓ удалено {len(non_media)} записей\n")
    else:
        print("  нечего удалять\n")

    # ── шаг 2: дедупликация по (md5, size) ──────────────────────────────────
    print("=" * 70)
    print("ШАГ 2: ДЕДУПЛИКАЦИЯ ПО MD5")
    print("=" * 70)
    rows = conn.execute(
        "SELECT remote_path, size, md5, status FROM files "
        "WHERE status IN ('pending', 'downloaded', 'skipped') AND md5 IS NOT NULL"
    ).fetchall()
    groups: dict[tuple[str, int], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[(r["md5"], r["size"] or 0)].append(r)

    duplicates_to_skip: list[str] = []
    dup_groups = 0
    saved_bytes = 0

    for key, members in groups.items():
        if len(members) <= 1:
            continue
        dup_groups += 1
        # Выбираем КОГО ОСТАВИТЬ:
        # 1) downloaded > skipped > pending (если уже качали — не трогаем)
        # 2) Иначе — по PRIORITY_PREFIXES
        # 3) Иначе — по короткому пути
        def keep_priority(r: sqlite3.Row):
            status_rank = {"downloaded": 0, "skipped": 1, "pending": 2}.get(r["status"], 3)
            return (
                status_rank,
                folder_priority(r["remote_path"]),
                len(r["remote_path"]),
            )

        members_sorted = sorted(members, key=keep_priority)
        keeper = members_sorted[0]
        for dup in members_sorted[1:]:
            # Помечаем дубль skipped (но только если он pending — downloaded не трогаем)
            if dup["status"] == "pending":
                duplicates_to_skip.append(dup["remote_path"])
                saved_bytes += dup["size"] or 0

    print(f"  Уникальных групп с дубликатами: {dup_groups}")
    print(f"  Файлов помечено как дубликаты: {len(duplicates_to_skip)}")
    print(f"  Сэкономлено объёма: {saved_bytes/1e9:.2f} ГБ")

    if duplicates_to_skip:
        conn.executemany(
            "UPDATE files SET status='skipped', error='duplicate (md5 match)' "
            "WHERE remote_path = ?",
            [(p,) for p in duplicates_to_skip],
        )
        conn.commit()

    # Файлы без md5 (бывает у некоторых) — могут быть дубликатами по имени+размеру
    no_md5_rows = conn.execute(
        "SELECT remote_path, size FROM files "
        "WHERE status='pending' AND (md5 IS NULL OR md5 = '')"
    ).fetchall()
    if no_md5_rows:
        print(f"\n  Файлов без MD5: {len(no_md5_rows)} (для них дедуп пропущен)")

    # ── финальная статистика ────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("ПОСЛЕ ОБРАБОТКИ:")
    print("=" * 70)
    after = conn.execute(
        "SELECT status, COUNT(*) AS n, COALESCE(SUM(size), 0) AS b "
        "FROM files GROUP BY status"
    ).fetchall()
    total_after = 0
    bytes_after = 0
    pending_n = 0
    pending_b = 0
    for r in after:
        print(f"  {r['status']:<12} {r['n']:>5} файлов   {r['b']/1e9:.2f} ГБ")
        total_after += r["n"]
        bytes_after += r["b"]
        if r["status"] == "pending":
            pending_n = r["n"]
            pending_b = r["b"]
    print(f"  {'итого':<12} {total_after:>5} файлов   {bytes_after/1e9:.2f} ГБ")
    print(f"\n  💾 ОСТАЛОСЬ СКАЧАТЬ: {pending_n} файлов / {pending_b/1e9:.2f} ГБ")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
