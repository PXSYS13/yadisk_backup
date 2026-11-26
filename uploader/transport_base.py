"""Единый контракт сменного транспорта загрузки в /photounlim."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from uploader.state import UploadRow


@dataclass
class UploadResult:
    """Итог попытки загрузки одного файла."""

    outcome: str  # 'uploaded' | 'hardlinked' | 'duplicate_remote'
    remote_id: str
    remote_name: str
    resource: dict[str, Any] | None = None
    note: str | None = None


ProgressCallback = Callable[[int, int], None]  # (bytes_sent, total_bytes)


class UploadTransport(Protocol):
    name: str

    def upload_file(
        self,
        row: UploadRow,
        *,
        md5: str,
        sha256: str,
        on_progress: ProgressCallback | None = None,
        stop_event: threading.Event | None = None,
    ) -> UploadResult: ...
