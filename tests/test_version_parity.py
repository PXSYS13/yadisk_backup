"""Проверка согласованности версии в pyproject.toml и utils.APP_VERSION."""

from __future__ import annotations

import re
from pathlib import Path

import utils

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib
    except ModuleNotFoundError:
        # Python 3.10 не имеет встроенного tomllib — читаем секцию [project] напрямую
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        assert m, "В pyproject.toml не найдено поле version"
        return m.group(1)
    return str(tomllib.loads(text)["project"]["version"])


def test_app_version_matches_pyproject():
    assert utils.APP_VERSION == _pyproject_version()
