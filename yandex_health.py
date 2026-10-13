"""Монитор состояния серверов Яндекса: пробы с машины пользователя.

Раз в PROBE_INTERVAL_SEC дёргает REST API Диска и веб-версию, хранит историю
за сутки (в памяти и в logs/yandex_health.jsonl), считает доступность и задержку.
Ничего не скачивает и не создаёт нагрузки: два лёгких GET-запроса в минуту.

Использование:
    monitor = HealthMonitor(token_getter=lambda: cfg["token"], log_path=...)
    monitor.start()            # фоновый daemon-поток
    monitor.snapshot()         # dict для /api/status
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from statistics import median

import requests

PROBE_INTERVAL_SEC = 60
HISTORY_SEC = 24 * 3600
BUCKET_SEC = 600  # полоска таймлайна: 144 ведра по 10 минут
# Ответ медленнее этого — «деградация», даже если код успешный
DEGRADED_LATENCY_MS = 5000
REQUEST_TIMEOUT = (10, 20)

STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_DOWN = "down"
STATE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Probe:
    key: str
    label: str
    url: str
    ok_statuses: frozenset[int]
    needs_token: bool = False


# 401/403 для REST API без валидного токена — тоже «сервер жив и отвечает»
PROBES: tuple[Probe, ...] = (
    Probe("rest_api", "REST API Диска", "https://cloud-api.yandex.net/v1/disk",
          frozenset({200, 401, 403}), needs_token=True),
    Probe("web", "Веб-версия Диска", "https://disk.yandex.ru/",
          frozenset({200, 301, 302, 303, 307, 308})),
)


@dataclass(frozen=True)
class Sample:
    ts: float
    ok: bool
    status: int | None
    latency_ms: int
    error: str | None = None

    @property
    def state(self) -> str:
        if not self.ok:
            return STATE_DOWN
        if self.latency_ms >= DEGRADED_LATENCY_MS:
            return STATE_DEGRADED
        return STATE_OK


def run_probe(probe: Probe, token: str | None = None, session: requests.Session | None = None) -> Sample:
    """Один запрос к Яндексу. Никогда не бросает исключений."""
    headers = {"User-Agent": "yadisk-backup-health/1.0"}
    if probe.needs_token and token:
        headers["Authorization"] = f"OAuth {token}"
    s = session or requests
    t0 = time.monotonic()
    try:
        r = s.get(probe.url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        latency = int((time.monotonic() - t0) * 1000)
        ok = r.status_code in probe.ok_statuses
        return Sample(time.time(), ok, r.status_code, latency, None if ok else f"HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        return Sample(time.time(), False, None, int((time.monotonic() - t0) * 1000), "таймаут")
    except requests.exceptions.ConnectionError as e:
        return Sample(time.time(), False, None, int((time.monotonic() - t0) * 1000), f"нет соединения: {str(e)[:120]}")
    except Exception as e:  # noqa: BLE001 — монитор не должен падать ни при каких ошибках
        return Sample(time.time(), False, None, int((time.monotonic() - t0) * 1000), f"{type(e).__name__}: {str(e)[:120]}")


def availability(samples: list[Sample], window_sec: float, now: float) -> float | None:
    """Доля успешных проб за окно, в процентах. None — проб не было."""
    recent = [s for s in samples if now - s.ts <= window_sec]
    if not recent:
        return None
    return round(100.0 * sum(1 for s in recent if s.ok) / len(recent), 1)


def timeline(samples: list[Sample], now: float, buckets: int = HISTORY_SEC // BUCKET_SEC) -> list[dict]:
    """Полоска за сутки: по ведру на BUCKET_SEC, от старых к новым."""
    start = now - buckets * BUCKET_SEC
    counts = [[0, 0] for _ in range(buckets)]  # [ok, total]
    for s in samples:
        idx = int((s.ts - start) // BUCKET_SEC)
        if 0 <= idx < buckets:
            counts[idx][1] += 1
            if s.ok:
                counts[idx][0] += 1
    out = []
    for i, (ok, total) in enumerate(counts):
        ratio = (ok / total) if total else None
        out.append({"t": int(start + i * BUCKET_SEC), "ok_ratio": ratio})
    return out


def summarize(probe: Probe, samples: list[Sample], now: float) -> dict:
    last = samples[-1] if samples else None
    recent = [s for s in samples if now - s.ts <= 600]
    lat = [s.latency_ms for s in recent if s.ok] or [s.latency_ms for s in samples[-10:] if s.ok]
    if last is None:
        state = STATE_UNKNOWN
    else:
        # Состояние — по последним 3 пробам, чтобы одиночный глюк сети не красил всё в красный
        tail = samples[-3:]
        states = [s.state for s in tail]
        if all(st == STATE_DOWN for st in states):
            state = STATE_DOWN
        elif any(st != STATE_OK for st in states):
            state = STATE_DEGRADED
        else:
            state = STATE_OK
    return {
        "key": probe.key,
        "label": probe.label,
        "url": probe.url,
        "state": state,
        "last": asdict(last) if last else None,
        "checked_ago_sec": int(now - last.ts) if last else None,
        "latency_ms_p50": int(median(lat)) if lat else None,
        "availability": {
            "10m": availability(samples, 600, now),
            "1h": availability(samples, 3600, now),
            "24h": availability(samples, HISTORY_SEC, now),
        },
        "timeline": timeline(samples, now),
    }


def overall_state(probe_states: list[str]) -> str:
    known = [s for s in probe_states if s != STATE_UNKNOWN]
    if not known:
        return STATE_UNKNOWN
    if all(s == STATE_DOWN for s in known):
        return STATE_DOWN
    if any(s != STATE_OK for s in known):
        return STATE_DEGRADED
    return STATE_OK


class HealthMonitor:
    """Фоновый поток с пробами. snapshot() можно звать из любого потока."""

    def __init__(self, token_getter=None, log_path: str | os.PathLike | None = None,
                 interval_sec: float = PROBE_INTERVAL_SEC, probes: tuple[Probe, ...] = PROBES):
        self._token_getter = token_getter or (lambda: None)
        self._log_path = str(log_path) if log_path else None
        self._interval = interval_sec
        self._probes = probes
        maxlen = int(HISTORY_SEC / max(interval_sec, 1)) + 10
        self._history: dict[str, deque[Sample]] = {p.key: deque(maxlen=maxlen) for p in probes}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._session = requests.Session()
        self.started_at: float | None = None
        self._load_history()

    # ── история на диске ──

    def _load_history(self) -> None:
        if not self._log_path or not os.path.exists(self._log_path):
            return
        cutoff = time.time() - HISTORY_SEC
        try:
            with open(self._log_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        key = rec.pop("probe")
                        sample = Sample(**rec)
                    except (ValueError, TypeError, KeyError):
                        continue
                    if key in self._history and sample.ts >= cutoff:
                        self._history[key].append(sample)
        except OSError:
            pass

    def _append_log(self, key: str, sample: Sample) -> None:
        if not self._log_path:
            return
        try:
            os.makedirs(os.path.dirname(self._log_path) or ".", exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"probe": key, **asdict(sample)}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ── пробы ──

    def probe_once(self) -> dict[str, Sample]:
        token = None
        try:
            token = self._token_getter()
        except Exception:
            pass
        results: dict[str, Sample] = {}
        for probe in self._probes:
            sample = run_probe(probe, token, self._session)
            results[probe.key] = sample
            with self._lock:
                self._history[probe.key].append(sample)
            self._append_log(probe.key, sample)
        return results

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.probe_once()
            self._stop.wait(self._interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.started_at = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="yandex-health", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── отчёт ──

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            probes = [summarize(p, list(self._history[p.key]), now) for p in self._probes]
        state = overall_state([p["state"] for p in probes])
        return {
            "updated_at": now,
            "interval_sec": self._interval,
            "running": bool(self._thread and self._thread.is_alive()),
            "overall": state,
            "summary": {
                STATE_OK: "Серверы Яндекса отвечают нормально",
                STATE_DEGRADED: "Яндекс отвечает с перебоями или медленно",
                STATE_DOWN: "Серверы Яндекса не отвечают",
                STATE_UNKNOWN: "Проверка ещё не выполнялась",
            }[state],
            "probes": probes,
        }
