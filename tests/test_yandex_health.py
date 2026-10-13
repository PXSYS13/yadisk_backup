"""Тесты монитора состояния серверов Яндекса."""

import json
import time

import requests

import yandex_health as yh


def _s(ts, ok, latency=100, status=200):
    return yh.Sample(ts=ts, ok=ok, status=status if ok else 503, latency_ms=latency,
                     error=None if ok else "HTTP 503")


def test_availability_windows():
    now = time.time()
    samples = [_s(now - 30, True), _s(now - 90, False), _s(now - 5000, True)]
    assert yh.availability(samples, 60, now) == 100.0
    assert yh.availability(samples, 600, now) == 50.0
    assert yh.availability(samples, 86400, now) == round(100 * 2 / 3, 1)
    assert yh.availability([], 60, now) is None


def test_timeline_buckets_from_old_to_new():
    now = time.time()
    samples = [_s(now - 10, True), _s(now - 20, False)]
    tl = yh.timeline(samples, now, buckets=6)
    assert len(tl) == 6
    assert tl[-1]["ok_ratio"] == 0.5
    assert all(b["ok_ratio"] is None for b in tl[:-1])
    assert tl[0]["t"] < tl[-1]["t"]


def test_summarize_states():
    now = time.time()
    probe = yh.PROBES[0]
    assert yh.summarize(probe, [], now)["state"] == yh.STATE_UNKNOWN
    down = [_s(now - 180, False), _s(now - 120, False), _s(now - 60, False)]
    assert yh.summarize(probe, down, now)["state"] == yh.STATE_DOWN
    flaky = [_s(now - 180, True), _s(now - 120, False), _s(now - 60, True)]
    assert yh.summarize(probe, flaky, now)["state"] == yh.STATE_DEGRADED
    slow = [_s(now - 60, True, latency=yh.DEGRADED_LATENCY_MS + 1)]
    assert yh.summarize(probe, slow, now)["state"] == yh.STATE_DEGRADED
    ok = [_s(now - 60, True)]
    summary = yh.summarize(probe, ok, now)
    assert summary["state"] == yh.STATE_OK
    assert summary["availability"]["10m"] == 100.0
    assert summary["latency_ms_p50"] == 100


def test_overall_state():
    assert yh.overall_state([yh.STATE_OK, yh.STATE_OK]) == yh.STATE_OK
    assert yh.overall_state([yh.STATE_OK, yh.STATE_DOWN]) == yh.STATE_DEGRADED
    assert yh.overall_state([yh.STATE_DOWN, yh.STATE_DOWN]) == yh.STATE_DOWN
    assert yh.overall_state([yh.STATE_UNKNOWN, yh.STATE_OK]) == yh.STATE_OK
    assert yh.overall_state([yh.STATE_UNKNOWN]) == yh.STATE_UNKNOWN


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _Session:
    def __init__(self, status=200, exc=None):
        self.status = status
        self.exc = exc
        self.calls = []

    def get(self, url, headers=None, timeout=None, allow_redirects=False):
        self.calls.append((url, headers))
        if self.exc:
            raise self.exc
        return _Resp(self.status)


def test_run_probe_never_raises():
    probe = yh.PROBES[0]
    assert yh.run_probe(probe, "tok", _Session(200)).ok is True
    assert yh.run_probe(probe, None, _Session(401)).ok is True  # сервер жив, токена нет
    assert yh.run_probe(probe, None, _Session(503)).ok is False
    timeout = yh.run_probe(probe, None, _Session(exc=requests.exceptions.Timeout()))
    assert timeout.ok is False and "таймаут" in timeout.error
    weird = yh.run_probe(probe, None, _Session(exc=RuntimeError("x")))
    assert weird.ok is False


def test_run_probe_sends_token_only_where_needed():
    s = _Session(200)
    yh.run_probe(yh.PROBES[0], "secret", s)
    yh.run_probe(yh.PROBES[1], "secret", s)
    assert s.calls[0][1]["Authorization"] == "OAuth secret"
    assert "Authorization" not in s.calls[1][1]


def test_monitor_persists_and_reloads_history(tmp_path):
    log = tmp_path / "health.jsonl"
    m = yh.HealthMonitor(token_getter=lambda: None, log_path=log)
    m._session = _Session(200)
    m.probe_once()
    snap = m.snapshot()
    assert snap["overall"] == yh.STATE_OK
    assert {p["key"] for p in snap["probes"]} == {"rest_api", "web"}
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2

    # Новый экземпляр поднимает историю с диска
    m2 = yh.HealthMonitor(token_getter=lambda: None, log_path=log)
    assert m2.snapshot()["probes"][0]["availability"]["10m"] == 100.0

    # Битые строки в логе не ломают загрузку
    log.write_text("not json\n" + json.dumps({"probe": "web", "ts": time.time(), "ok": True,
                                               "status": 200, "latency_ms": 5}) + "\n",
                   encoding="utf-8")
    m3 = yh.HealthMonitor(token_getter=lambda: None, log_path=log)
    assert m3.snapshot()["probes"][1]["availability"]["10m"] == 100.0
