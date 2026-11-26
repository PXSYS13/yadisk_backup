"""Потокобезопасное чтение и запись настроек загрузки в Безлимит (upload_unlim_config.json)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from typing import Any

from uploader import paths
from uploader.utils import (
    DEFAULT_CHUNK_MB,
    DEFAULT_WORKERS,
    IS_WINDOWS,
    MAX_CHUNK_MB,
    MAX_WORKERS,
    MIN_CHUNK_MB,
)

log = logging.getLogger("unlim_uploader")
_CFG_LOCK = threading.RLock()


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _as_int(raw: Any, default: int, lo: int, hi: int, *, name: str = "") -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        if name:
            log.warning("Некорректное число в настройке %s=%r — использую %d", name, raw, default)
        return default
    return max(lo, min(hi, val))


def _defaults() -> dict[str, Any]:
    return {
        "source_dir": "",
        "max_workers": DEFAULT_WORKERS,
        "chunk_mb": DEFAULT_CHUNK_MB,
        "strict_quota_guard": True,
        "watch_mode": False,
        "watch_interval_sec": 60,
    }


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_dir": str(raw.get("source_dir") or "").strip(),
        "max_workers": _as_int(
            raw.get("max_workers"), DEFAULT_WORKERS, 1, MAX_WORKERS, name="max_workers"
        ),
        "chunk_mb": _as_int(
            raw.get("chunk_mb"), DEFAULT_CHUNK_MB, MIN_CHUNK_MB, MAX_CHUNK_MB, name="chunk_mb"
        ),
        "strict_quota_guard": _as_bool(raw.get("strict_quota_guard"), True),
        "watch_mode": _as_bool(raw.get("watch_mode"), False),
        "watch_interval_sec": _as_int(
            raw.get("watch_interval_sec"), 60, 15, 3600, name="watch_interval_sec"
        ),
    }


def get_config() -> dict[str, Any]:
    with _CFG_LOCK:
        cfg_file = paths.CONFIG_FILE
        if not cfg_file.exists():
            return _defaults()
        try:
            loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Не удалось прочитать %s: %s — использую настройки по умолчанию", cfg_file, exc)
            return _defaults()
        if not isinstance(loaded, dict):
            log.warning("Некорректный формат %s — использую настройки по умолчанию", cfg_file)
            return _defaults()
        return _normalize(loaded)


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    with _CFG_LOCK:
        current = get_config()
        merged = {**current, **{k: v for k, v in updates.items() if v is not None}}
        cleaned = _normalize(merged)

        cfg_file = paths.CONFIG_FILE
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        target = os.fspath(cfg_file)
        tmp_path = f"{target}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
        payload = json.dumps(cleaned, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            if not IS_WINDOWS:
                try:
                    os.chmod(tmp_path, 0o600)
                except OSError:
                    pass
            os.replace(tmp_path, target)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return cleaned
