"""Live-наблюдалка прогресса скачивания.

Каждые 2 секунды читает state.db, перерисовывает экран:
проценты, скорость, ETA, последние скачанные файлы.

Запуск: двойной клик по watch.bat или `python watch.py` в терминале.
Закрыть: Ctrl+C или просто закрыть окно.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import sqlite_ro_uri  # noqa: E402  (после sys.path — иначе не найдётся при запуске из другой папки)

REFRESH_SEC = 2.0
HISTORY_LEN = 30   # храним последние N измерений для скорости / ETA
RECENT_LIMIT = 6   # сколько последних файлов показывать

DB_FILE = Path(__file__).resolve().parent / "state.db"


def human_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def human_duration(seconds: float) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:  # NaN check
        return "—"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}ч {m:02d}м {s:02d}с"
    if m > 0:
        return f"{m}м {s:02d}с"
    return f"{s}с"


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def progress_bar(pct: float, width: int = 40) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {pct:5.1f}%"


def fetch_state() -> dict | None:
    """Читает текущее состояние из БД. None если БД ещё не создана."""
    if not DB_FILE.exists():
        return None
    try:
        conn = sqlite3.connect(sqlite_ro_uri(DB_FILE), uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        # Статистика
        stats = {
            "pending": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "total": 0,
            "bytes_total": 0,
            "bytes_done": 0,
        }
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(size), 0) AS b "
            "FROM files GROUP BY status"
        ):
            stats[row["status"]] = row["n"]
            stats["total"] += row["n"]
            stats["bytes_total"] += row["b"] or 0
            if row["status"] in ("downloaded", "skipped"):
                stats["bytes_done"] += row["b"] or 0

        # Последние скачанные
        recent = list(
            conn.execute(
                "SELECT remote_path, size, status, downloaded_at "
                "FROM files "
                "WHERE downloaded_at IS NOT NULL "
                "ORDER BY downloaded_at DESC LIMIT ?",
                (RECENT_LIMIT,),
            )
        )

        # Последние fail
        recent_fail = list(
            conn.execute(
                "SELECT remote_path, error FROM files "
                "WHERE status='failed' ORDER BY rowid DESC LIMIT 3"
            )
        )

        conn.close()
        return {"stats": stats, "recent": recent, "fail": recent_fail}
    except sqlite3.Error as e:
        return {"error": str(e)}


def main() -> int:
    # Для корректного вывода кириллицы в Windows-консоли
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    history: deque[tuple[float, int, int]] = deque(maxlen=HISTORY_LEN)
    started_at = time.time()

    print("Жду появления state.db...")
    while not DB_FILE.exists():
        time.sleep(1)

    try:
        while True:
            data = fetch_state()
            now = time.time()

            clear_screen()
            print("=" * 70)
            print("  ЯНДЕКС.ДИСК — ПРОГРЕСС СКАЧИВАНИЯ")
            print("  Обновляется каждые 2 сек. Закрыть: Ctrl+C")
            print("=" * 70)

            if not data:
                print("\nБаза данных не найдена. Запусти scan/download.")
                time.sleep(REFRESH_SEC)
                continue
            if "error" in data:
                print(f"\nОшибка чтения БД: {data['error']}")
                time.sleep(REFRESH_SEC)
                continue

            s = data["stats"]
            total = s["total"]
            done = s["downloaded"] + s["skipped"]
            pct_files = (done / total * 100) if total else 0
            bytes_done = s["bytes_done"]
            bytes_total = s["bytes_total"]
            pct_bytes = (bytes_done / bytes_total * 100) if bytes_total else 0

            history.append((now, done, bytes_done))

            # Скорость считаем по окну истории (примерно за последние N*REFRESH сек)
            if len(history) >= 2:
                t0, f0, b0 = history[0]
                dt = now - t0
                if dt > 0:
                    files_per_sec = (done - f0) / dt
                    bytes_per_sec = (bytes_done - b0) / dt
                else:
                    files_per_sec = 0
                    bytes_per_sec = 0
            else:
                files_per_sec = 0
                bytes_per_sec = 0

            remaining_files = total - done - s["failed"]
            eta_sec = (remaining_files / files_per_sec) if files_per_sec > 0 else None

            print("\n  ФАЙЛЫ:")
            print(f"  {progress_bar(pct_files)}  {done} / {total}")

            print("\n  ОБЪЁМ:")
            print(
                f"  {progress_bar(pct_bytes)}  "
                f"{human_bytes(bytes_done)} / {human_bytes(bytes_total)}"
            )

            print("\n  СТАТУСЫ:")
            print(f"     скачано:   {s['downloaded']:>6}")
            print(f"     пропущено: {s['skipped']:>6}  (уже было локально)")
            print(f"     pending:   {s['pending']:>6}")
            print(f"     ОШИБОК:    {s['failed']:>6}")

            print("\n  СКОРОСТЬ:")
            print(f"     {files_per_sec:6.2f} файл/сек   {human_bytes(bytes_per_sec)}/сек")
            print(f"     осталось:  ~{human_duration(eta_sec)}")
            print(f"     работает:  {human_duration(now - started_at)}")

            if data["recent"]:
                print("\n  ПОСЛЕДНИЕ:")
                for r in data["recent"]:
                    name = r["remote_path"].rsplit("/", 1)[-1]
                    if len(name) > 55:
                        name = name[:52] + "..."
                    tag = "OK " if r["status"] == "downloaded" else "skip"
                    print(f"     [{tag}] {name}")

            if data["fail"]:
                print("\n  ПОСЛЕДНИЕ ОШИБКИ:")
                for f in data["fail"]:
                    name = f["remote_path"].rsplit("/", 1)[-1]
                    if len(name) > 50:
                        name = name[:47] + "..."
                    err = (f["error"] or "")[:50]
                    print(f"     [FAIL] {name} — {err}")

            # Если всё доделано — мигаем и выходим
            if total > 0 and s["pending"] == 0:
                print("\n" + "=" * 70)
                print("  ✅ ГОТОВО! Все файлы обработаны.")
                if s["failed"] > 0:
                    print(f"  ⚠️  Ошибок: {s['failed']}. Запусти:  python main.py retry-failed")
                print("=" * 70)
                break

            time.sleep(REFRESH_SEC)
    except KeyboardInterrupt:
        print("\nЗакрываю.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
