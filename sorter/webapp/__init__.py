"""Конфигурация и координатор фоновых задач подсистемы сортировки фото по людям."""

from __future__ import annotations

from sorter.webapp.config import DEFAULT_CONFIG, get_config, save_config
from sorter.webapp.manager import TaskBusyError, TaskManager, pick_folder_dialog

__all__ = [
    "DEFAULT_CONFIG",
    "TaskBusyError",
    "TaskManager",
    "get_config",
    "pick_folder_dialog",
    "save_config",
]
