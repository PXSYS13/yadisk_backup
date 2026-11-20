"""Сопоставление запрошенных файлов Безлимита с реально распакованными из ZIP-архива."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import NamedTuple

from storage_fetch import FetchFailure
from utils import sanitize_component


def _clean_stem(name: str) -> tuple[str, str]:
    """'photo (1).jpg' → ('photo', '.jpg'): Яндекс дописывает суффиксы при коллизиях имён в ZIP."""
    base = os.path.basename(name).lower()
    stem, ext = os.path.splitext(base)
    clean = re.sub(r"[\s_]*\(\d+\)$|_+\d+$", "", stem).strip()
    return clean, ext


class ExtractedItem(NamedTuple):
    """Запись архива, которую удалось распаковать."""

    name: str
    size: int
    dst: str = ""


class RejectedItem(NamedTuple):
    """Запись архива, которую не записать в эту папку (длинный путь, FAT32): виноват файл, не диск."""

    name: str
    size: int
    reason: str  # «local_file | …» — так же, как в режиме direct


class FetchResult(NamedTuple):
    extracted: list[ExtractedItem]
    total_bytes: int
    failure: FetchFailure | None = None
    rejected: tuple[RejectedItem, ...] = ()


class SplitResult(NamedTuple):
    done: list[str]        # file_id, которые реально приехали
    missing: list[str]     # file_id, которых в архиве не оказалось
    paths: dict[str, str]  # file_id → локальный путь распакованного файла


def _batch_name(item: dict) -> str:
    return os.path.basename(item.get("name") or "").lower()


def _batch_size(item: dict) -> int:
    return int(item.get("size") or 0)


def split_by_extracted(batch: list[dict], extracted: Iterable[tuple]) -> SplitResult:
    """Делит пачку на реально приехавшие и не приехавшие файлы.

    1. Точное совпадение по (имя файла, размер).
    2. Совпадение по базовому имени (вариации вроде 'photo (1).jpg') + расширению + размеру.
    3. Фоллбэк только для одинакового расширения и размера (не путаем разные форматы).
    4. Строки с неизвестным размером (size=0 / NULL из старого фотосреза) — по имени/стэму.

    extracted принимает кортежи (имя, размер) или (имя, размер, путь).
    """
    unused: list[ExtractedItem] = [ExtractedItem(*it) for it in extracted]
    done: list[str] = []
    paths: dict[str, str] = {}

    def stage(
        pending: list[dict],
        key_batch: Callable[[dict], tuple],
        key_item: Callable[[ExtractedItem], tuple],
    ) -> list[dict]:
        """Сопоставляет pending с ещё не использованными записями архива по ключу,
        с учётом кратности. Возвращает несопоставленные."""
        nonlocal unused
        buckets: dict[tuple, list[int]] = defaultdict(list)
        for idx, it in enumerate(unused):
            buckets[key_item(it)].append(idx)
        taken: set[int] = set()
        rest: list[dict] = []
        for item in pending:
            idxs = buckets.get(key_batch(item))
            if not idxs:
                rest.append(item)
                continue
            idx = idxs.pop(0)
            taken.add(idx)
            fid = str(item["file_id"])
            done.append(fid)
            if unused[idx].dst:
                paths[fid] = unused[idx].dst
        unused = [it for idx, it in enumerate(unused) if idx not in taken]
        return rest

    # Архив приезжает с санитизированными именами (см. safe_extract_dst) —
    # сравниваем в том же виде
    def norm_name(name: str) -> str:
        return sanitize_component(os.path.basename(name)).lower()

    # 1. Точное совпадение (имя, размер)
    pending = stage(
        batch,
        lambda b: (norm_name(b.get("name") or ""), _batch_size(b)),
        lambda it: (norm_name(it.name), int(it.size or 0)),
    )
    # 2. Нормализованный стэм: 'a (1).jpg' ↔ 'a.jpg'
    if pending:
        pending = stage(
            pending,
            lambda b: (*_clean_stem(norm_name(b.get("name") or "")), _batch_size(b)),
            lambda it: (*_clean_stem(norm_name(it.name)), int(it.size or 0)),
        )
    # 3. Фоллбэк: только расширение + размер
    if pending:
        pending = stage(
            pending,
            lambda b: (os.path.splitext(_batch_name(b))[1], _batch_size(b)),
            lambda it: (os.path.splitext(it.name.lower())[1], int(it.size or 0)),
        )
    # 4. Строки с неизвестным размером (size=0 / NULL из старого фотосреза): сопоставляем по имени/стэму
    if pending and unused:
        unknown = [b for b in pending if _batch_size(b) == 0]
        known = [b for b in pending if _batch_size(b) != 0]
        if unknown:
            unknown = stage(
                unknown,
                lambda b: (norm_name(b.get("name") or ""),),
                lambda it: (norm_name(it.name),),
            )
            if unknown and unused:
                unknown = stage(
                    unknown,
                    lambda b: _clean_stem(norm_name(b.get("name") or "")),
                    lambda it: _clean_stem(norm_name(it.name)),
                )
        pending = known + unknown

    return SplitResult(done, [str(b["file_id"]) for b in pending], paths)
