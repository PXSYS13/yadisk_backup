"""Проверка квоты аккаунта и Quota Guard (защита от случайного расхода обычного места Диска)."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import requests

from uploader.cookies import (
    AuthExpiredError,
    CaptchaError,
    RateLimitError,
    UnlimApiError,
    api_call,
)
from uploader.utils import human_bytes

log = logging.getLogger("unlim_uploader")

QUOTA_DRIFT_TOLERANCE_BYTES = 5 * 1024 * 1024  # 5 МБ


class QuotaGuardError(Exception):
    """Остановка загрузки ради защиты основной квоты Диска."""


@dataclass
class QuotaSnapshot:
    used: int
    disk: int
    limit: int
    free: int
    trash: int
    photounlim: int
    actual_photounlim_size: int
    filesize_limit: int
    is_overdrawn: bool
    conversion_enabled: bool
    has_pro_subscription: bool
    has_family_subscription: bool
    account_name: str = ""
    unlimited_free: bool = True
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fetch_quota_snapshot(session: requests.Session, sk: str, uid: str) -> QuotaSnapshot:
    """Читает mpfs/space и mpfs/get-user-info, оценивая доступность бесплатного безлимита."""
    sp = api_call(session, sk, uid, "mpfs/space", {}, retries=2)
    if not isinstance(sp, dict) or "used" not in sp or "limit" not in sp:
        raise UnlimApiError(
            f"mpfs/space вернул неполный ответ: ключи={sorted(sp.keys()) if isinstance(sp, dict) else type(sp).__name__}"
        )

    ui: dict[str, Any] = {}
    try:
        raw_ui = api_call(session, sk, uid, "mpfs/get-user-info", {}, retries=1)
        if isinstance(raw_ui, dict):
            ui = raw_ui
    except (AuthExpiredError, CaptchaError, RateLimitError):
        raise
    except Exception as e:  # noqa: BLE001 — не критично, но пишем в WARNING
        log.warning("mpfs/get-user-info недоступен (%s) — оценка тарифа только по mpfs/space", e)

    try:
        used = int(sp["used"])
        limit = int(sp["limit"])
    except (TypeError, ValueError) as e:
        raise UnlimApiError(f"mpfs/space: некорректные числовые поля квоты: {e}") from None

    disk = int(sp.get("disk") or 0)
    free = int(sp.get("free") or 0)
    trash = int(sp.get("trash") or 0)
    photounlim = int(sp.get("photounlim") or 0)
    actual_unlim = int(sp.get("actual_photounlim_size") or photounlim or 0)
    filesize_limit = int(sp.get("filesize_limit") or 1024**3)
    is_overdrawn = bool(sp.get("is_overdrawn")) or (limit > 0 and used > limit)
    conversion_enabled = bool(sp.get("photounlim_conversion_experiment_enabled"))
    has_pro = bool(ui.get("hasProSubscription"))
    has_family = bool(ui.get("hasFamilySubscription"))
    has_sub = has_pro or has_family or filesize_limit > 2 * 1024**3
    account_name = str(ui.get("name") or ui.get("login") or "")

    warning: str | None = None
    unlimited_free = True
    if is_overdrawn and not has_sub:
        unlimited_free = False
        warning = (
            f"На аккаунте нет активной подписки Яндекс 360 (занято {human_bytes(used)} из {human_bytes(limit)}). "
            "Файлы попадут в раздел /photounlim, но Яндекс будет учитывать их в основном объёме до продления подписки."
        )
    elif conversion_enabled and photounlim == 0 and not has_sub:
        unlimited_free = False
        warning = (
            "На бесплатном тарифе без подписки Яндекс 360 файлы в /photounlim учитываются в основном объёме Диска. "
            "Для настоящего безлимита нужна активная подписка Яндекс 360."
        )

    return QuotaSnapshot(
        used=used,
        disk=disk,
        limit=limit,
        free=free,
        trash=trash,
        photounlim=photounlim,
        actual_photounlim_size=actual_unlim,
        filesize_limit=filesize_limit,
        is_overdrawn=is_overdrawn,
        conversion_enabled=conversion_enabled,
        has_pro_subscription=has_pro,
        has_family_subscription=has_family,
        account_name=account_name,
        unlimited_free=unlimited_free,
        warning=warning,
    )


class QuotaGuard:
    """Контролирует, что загрузка идёт именно в /photounlim и не тратит основную квоту Диска."""

    def __init__(
        self, baseline: QuotaSnapshot, *, strict: bool = True, allow_quota_count: bool = False
    ):
        self.baseline = baseline
        self.strict = strict
        self.allow_quota_count = allow_quota_count
        self.uploaded_bytes_checked = 0

    def check_preflight(self) -> None:
        if self.strict and not self.baseline.unlimited_free and not self.allow_quota_count:
            raise QuotaGuardError(
                self.baseline.warning
                or "Безлимит не активен на этом аккаунте — загрузка остановлена для защиты основной квоты."
            )

    def check_file_size(self, size: int) -> str | None:
        limit = self.baseline.filesize_limit
        if limit > 0 and size > limit:
            return (
                f"размер файла ({human_bytes(size)}) превышает лимит тарифа ({human_bytes(limit)})"
            )
        return None

    @staticmethod
    def verify_resource(resource: dict[str, Any] | None, *, require_present: bool = True) -> None:
        if not isinstance(resource, dict):
            if require_present:
                raise QuotaGuardError(
                    "Яндекс не вернул метаданные сохранённого файла — невозможно подтвердить /photounlim"
                )
            return
        path = str(resource.get("path") or resource.get("id") or "")
        raw_meta = resource.get("meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        storage_type = meta.get("storage_type")
        if not path.startswith("/photounlim/"):
            raise QuotaGuardError(f"Файл попал не в безлимит, а в путь {path or '<пусто>'}")
        if storage_type != "photounlim":
            raise QuotaGuardError(
                f"Яндекс сохранил файл с типом хранилища '{storage_type or 'неизвестно'}' вместо 'photounlim'"
            )

    def check_drift(self, current: QuotaSnapshot, newly_uploaded_bytes: int) -> None:
        self.uploaded_bytes_checked += max(0, int(newly_uploaded_bytes))
        if not self.strict or not self.baseline.unlimited_free or self.allow_quota_count:
            return
        if self.uploaded_bytes_checked <= 0:
            return
        used_delta = current.used - self.baseline.used
        if used_delta > QUOTA_DRIFT_TOLERANCE_BYTES:
            raise QuotaGuardError(
                f"Обнаружен расход обычной квоты Диска (+{human_bytes(used_delta)} к занятому месту). "
                "Загрузка остановлена — проверьте подписку Яндекс 360 и настройки безлимита."
            )
