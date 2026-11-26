"""Потокобезопасное атомарное чтение и запись JSON-конфигурации в paths.CONFIG_FILE."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from collections.abc import Mapping
from typing import Any

from sorter import paths
from sorter.utils import IS_WINDOWS

log = logging.getLogger("photo_sorter.config")

_CONFIG_LOCK = threading.RLock()

VALID_MODEL_PACKS: tuple[str, ...] = ("antelopev2", "buffalo_l")
VALID_MODES: tuple[str, ...] = ("hardlink", "copy", "move")
VALID_TREE_LAYOUTS: tuple[str, ...] = ("people_first", "dual")
VALID_GROUP_POLICIES: tuple[str, ...] = ("each_person", "shared_folder", "priority")
VALID_FALLBACK_POLICIES: tuple[str, ...] = ("folder", "skip")

_ENUM_RULES: dict[str, tuple[str, ...]] = {
    "model_pack": VALID_MODEL_PACKS,
    "mode": VALID_MODES,
    "tree_layout": VALID_TREE_LAYOUTS,
    "group_policy": VALID_GROUP_POLICIES,
    "no_faces_policy": VALID_FALLBACK_POLICIES,
    "unnamed_policy": VALID_FALLBACK_POLICIES,
}

_BOOL_KEYS: frozenset[str] = frozenset(
    {
        "prefer_gpu",
        "include_child_age",
        "skip_ignored",
        "preserve_dates",
    }
)

_STR_KEYS: frozenset[str] = frozenset({"source_dir", "target_dir"})

DEFAULT_CONFIG: dict[str, Any] = {
    "source_dir": "",
    "target_dir": "",
    "model_pack": "antelopev2",
    "prefer_gpu": True,
    "mode": "hardlink",
    "tree_layout": "people_first",
    "include_child_age": True,
    "group_policy": "each_person",
    "no_faces_policy": "folder",
    "unnamed_policy": "folder",
    "skip_ignored": True,
    "preserve_dates": True,
}


def _parse_bool(raw: Any, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    low = str(raw).strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    return default


def _write_json_atomic(target: os.PathLike[str], data: dict[str, Any]) -> None:
    target_path = os.fspath(target)
    parent = os.path.dirname(os.path.abspath(target_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{target_path}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
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
        os.replace(tmp_path, target_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def validate_config_updates(updates: Mapping[str, Any]) -> dict[str, Any]:
    """Проверяет и нормализует словарь обновлений конфигурации; кидает ValueError при ошибках."""
    cleaned: dict[str, Any] = {}
    for key, val in updates.items():
        if key not in DEFAULT_CONFIG or val is None:
            continue
        if key in _ENUM_RULES:
            s_val = str(val).strip()
            allowed = _ENUM_RULES[key]
            if s_val not in allowed:
                raise ValueError(
                    f"Недопустимое значение для {key}: {val!r} (ожидается одно из {', '.join(allowed)})"
                )
            cleaned[key] = s_val
        elif key in _BOOL_KEYS:
            cleaned[key] = _parse_bool(val, bool(DEFAULT_CONFIG[key]))
        elif key in _STR_KEYS:
            cleaned[key] = str(val).strip()
    return cleaned


def get_config() -> dict[str, Any]:
    """Потокобезопасно читает конфигурацию из paths.CONFIG_FILE с подстановкой значений по умолчанию."""
    with _CONFIG_LOCK:
        result = dict(DEFAULT_CONFIG)
        cfg_path = paths.CONFIG_FILE
        if not cfg_path.is_file():
            return result
        try:
            raw_text = cfg_path.read_text(encoding="utf-8")
            raw_data = json.loads(raw_text)
        except (OSError, ValueError) as exc:
            log.warning("Не удалось прочитать %s (%s) — используются настройки по умолчанию", cfg_path, exc)
            return result
        if not isinstance(raw_data, dict):
            return result

        for key, default_val in DEFAULT_CONFIG.items():
            if key not in raw_data or raw_data[key] is None:
                continue
            val = raw_data[key]
            if key in _ENUM_RULES:
                s_val = str(val).strip()
                if s_val in _ENUM_RULES[key]:
                    result[key] = s_val
            elif key in _BOOL_KEYS:
                result[key] = _parse_bool(val, bool(default_val))
            elif key in _STR_KEYS:
                result[key] = str(val).strip()
        return result


def save_config(updates: Mapping[str, Any]) -> dict[str, Any]:
    """Валидирует, объединяет с текущими настройками и атомарно записывает paths.CONFIG_FILE."""
    normalized = validate_config_updates(updates)
    with _CONFIG_LOCK:
        merged = get_config()
        merged.update(normalized)
        _write_json_atomic(paths.CONFIG_FILE, merged)
        return dict(merged)
