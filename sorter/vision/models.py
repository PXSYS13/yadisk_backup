"""Загрузка, проверка и выбор провайдеров ONNX-моделей InsightFace (antelopev2 / buffalo_l)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urljoin, urlparse
import zipfile

import requests

from sorter import paths
from sorter.utils import (
    CancelledError,
    compute_sha256,
    is_symlink_or_junction,
    redact_urls,
    to_long_path,
)

log = logging.getLogger("photo_sorter.vision.models")

PREFERRED_ORT_PROVIDERS: tuple[str, ...] = (
    "DmlExecutionProvider",
    "CUDAExecutionProvider",
    "CoreMLExecutionProvider",
    "CPUExecutionProvider",
)

_MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB
_MAX_REDIRECT_HOPS = 5
_MAX_ZIP_ENTRIES = 64
_MAX_ZIP_FILE_BYTES = 512 * 1024 * 1024  # 512 MiB
_MAX_ZIP_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GiB
_CACHE_MIN_FILE_BYTES = 1024 * 1024  # 1 MiB

_WIN_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

_SHA256_CACHE_LOCK = threading.RLock()
_SHA256_CACHE: dict[tuple[str, int, int], str] = {}


@dataclass(frozen=True)
class ModelPackSpec:
    """Описание официального пакета ONNX-моделей InsightFace."""

    name: str
    url: str
    detector_file: str
    embedder_file: str
    genderage_file: str
    zip_sha256: str | None = None
    file_sha256s: Mapping[str, str] | None = None

    @property
    def required_files(self) -> tuple[str, str, str]:
        return (self.detector_file, self.embedder_file, self.genderage_file)


MODEL_PACKS: dict[str, ModelPackSpec] = {
    "antelopev2": ModelPackSpec(
        name="antelopev2",
        url="https://github.com/deepinsight/insightface/releases/download/v0.7/antelopev2.zip",
        detector_file="scrfd_10g_bnkps.onnx",
        embedder_file="glintr100.onnx",
        genderage_file="genderage.onnx",
        zip_sha256="8e182f14fc6e80b3bfa375b33eb6cff7ee05d8ef7633e738d1c89021dcf0c5c5",
        file_sha256s=MappingProxyType(
            {
                "1k3d68.onnx": "df5c06b8a0c12e422b2ed8947b8869faa4105387f199c477af038aa01f9a45cc",
                "2d106det.onnx": "f001b856447c413801ef5c42091ed0cd516fcd21f2d6b79635b1e733a7109dbf",
                "genderage.onnx": "4fde69b1c810857b88c64a335084f1c3fe8f01246c9a191b48c7bb756d6652fb",
                "glintr100.onnx": "4ab1d6435d639628a6f3e5008dd4f929edf4c4124b1a7169e1048f9fef534cdf",
                "scrfd_10g_bnkps.onnx": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
            }
        ),
    ),
    "buffalo_l": ModelPackSpec(
        name="buffalo_l",
        url="https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
        detector_file="det_10g.onnx",
        embedder_file="w600k_r50.onnx",
        genderage_file="genderage.onnx",
    ),
}


@dataclass(frozen=True)
class ModelPackPaths:
    """Локальные пути к распакованным ONNX-файлам пакета моделей."""

    pack_name: str
    pack_dir: Path
    detector_path: Path
    embedder_path: Path
    genderage_path: Path


def _get_pack_spec(pack_name: str) -> ModelPackSpec:
    spec = MODEL_PACKS.get(pack_name)
    if spec is None:
        supported = ", ".join(sorted(MODEL_PACKS))
        raise ValueError(f"Неизвестный пакет моделей '{pack_name}'. Доступны: {supported}")
    return spec


def _resolve_models_dir(models_dir: Path | str | None) -> Path:
    return Path(models_dir).resolve() if models_dir is not None else paths.MODELS_DIR


def _cached_or_fresh_sha256(
    fpath: Path,
    st: os.stat_result,
    *,
    bypass_cache: bool,
    stop_event: threading.Event | None = None,
) -> str:
    key = (os.fspath(fpath), int(st.st_size), int(st.st_mtime_ns))
    if not bypass_cache and st.st_size >= _CACHE_MIN_FILE_BYTES:
        with _SHA256_CACHE_LOCK:
            cached = _SHA256_CACHE.get(key)
            if cached is not None:
                return cached
    digest = compute_sha256(str(fpath), stop_event=stop_event)
    if st.st_size >= _CACHE_MIN_FILE_BYTES:
        with _SHA256_CACHE_LOCK:
            if len(_SHA256_CACHE) > 128:
                _SHA256_CACHE.clear()
            _SHA256_CACHE[key] = digest
    return digest


def select_ort_providers(prefer_gpu: bool = True) -> list[str]:
    """Выбирает доступные ONNX Runtime Execution Providers с приоритетом GPU -> CPU."""
    try:
        import onnxruntime as ort

        available = list(ort.get_available_providers())
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Не удалось получить список провайдеров ONNX Runtime (%s) — используется CPUExecutionProvider",
            exc,
        )
        return ["CPUExecutionProvider"]

    if not prefer_gpu:
        return ["CPUExecutionProvider"]

    selected = [p for p in PREFERRED_ORT_PROVIDERS if p in available]
    if "CPUExecutionProvider" not in selected:
        selected.append("CPUExecutionProvider")
    return selected


def _validate_zip_member(info: zipfile.ZipInfo) -> tuple[str, ...]:
    """Проверяет элемент архива на zip-slip, абсолютные пути, ADS, резервные имена и симлинки."""
    raw_name = info.filename
    if not raw_name or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_name):
        raise ValueError(f"Недопустимое имя файла в архиве: {raw_name!r}")

    unix_mode = (info.external_attr >> 16) & 0o170000
    if unix_mode == stat.S_IFLNK:
        raise ValueError(f"Символические ссылки в архиве моделей запрещены: {raw_name!r}")

    norm = raw_name.replace("\\", "/")
    if norm.startswith("/") or PurePosixPath(norm).is_absolute():
        raise ValueError(f"Абсолютный путь в архиве моделей запрещён: {raw_name!r}")

    parts = [part for part in norm.split("/") if part not in ("", ".")]
    if not parts:
        return ()

    for part in parts:
        if part == "..":
            raise ValueError(f"Обнаружен zip-slip путь в архиве моделей: {raw_name!r}")
        if ":" in part:
            raise ValueError(f"Недопустимый символ ':' в пути архива моделей: {raw_name!r}")
        stem_head = part.split(".", 1)[0].strip().upper()
        if stem_head in _WIN_RESERVED_NAMES:
            raise ValueError(f"Зарезервированное имя Windows в архиве моделей: {raw_name!r}")
    return tuple(parts)


def safe_extract_zip(
    zip_path: Path | str,
    dest_dir: Path | str,
    *,
    required_files: Iterable[str] | None = None,
    stop_event: threading.Event | None = None,
) -> list[Path]:
    """Безопасно распаковывает ZIP-архив без zip-slip, симлинков и zip-бомб."""
    dest_root = Path(dest_dir).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    req_set = set(required_files) if required_files is not None else None

    with zipfile.ZipFile(to_long_path(str(zip_path)), "r") as zf:
        infolist = zf.infolist()
        if len(infolist) > _MAX_ZIP_ENTRIES:
            raise ValueError(
                f"Архив моделей содержит слишком много записей ({len(infolist)} > {_MAX_ZIP_ENTRIES})"
            )

        entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        for info in infolist:
            parts = _validate_zip_member(info)
            if info.is_dir() or not parts:
                continue
            entries.append((info, parts))

        if not entries:
            raise ValueError("Архив моделей пуст")

        # Если все файлы лежат внутри одной общей корневой папки (например, antelopev2/...),
        # снимаем этот верхний сегмент для плоской структуры в pack_dir.
        first_dirs = {parts[0] for _, parts in entries if len(parts) > 1}
        has_root_files = any(len(parts) == 1 for _, parts in entries)
        strip_prefix = len(first_dirs) == 1 and not has_root_files

        extracted: list[Path] = []
        extracted_rel: set[str] = set()
        total_read_bytes = 0

        for info, parts in entries:
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Распаковка моделей остановлена пользователем")

            rel_parts = parts[1:] if strip_prefix else parts
            if not rel_parts:
                continue

            rel_posix = "/".join(rel_parts)
            is_onnx = rel_parts[-1].lower().endswith(".onnx")
            is_required = req_set is not None and rel_posix in req_set
            if not is_onnx and not is_required:
                continue

            if info.file_size > _MAX_ZIP_FILE_BYTES:
                raise ValueError(
                    f"Файл {info.filename!r} в архиве превышает лимит размера "
                    f"({info.file_size} > {_MAX_ZIP_FILE_BYTES})"
                )

            target = (dest_root.joinpath(*rel_parts)).resolve()
            if not target.is_relative_to(dest_root):
                raise ValueError(f"Выход файла за пределы каталога распаковки: {info.filename!r}")

            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=to_long_path(os.fspath(target.parent)),
                prefix=".extract.",
                suffix=".part",
            )
            tmp_target = Path(tmp_name)
            file_read_bytes = 0
            try:
                with zf.open(info, "r") as src, os.fdopen(fd, "wb") as dst:
                    fd = -1
                    while True:
                        if stop_event is not None and stop_event.is_set():
                            raise CancelledError("Распаковка моделей остановлена пользователем")
                        chunk = src.read(256 * 1024)
                        if not chunk:
                            break
                        file_read_bytes += len(chunk)
                        total_read_bytes += len(chunk)
                        if file_read_bytes > _MAX_ZIP_FILE_BYTES:
                            raise ValueError(
                                f"Файл {info.filename!r} превысил лимит распаковки "
                                f"({_MAX_ZIP_FILE_BYTES} байт)"
                            )
                        if total_read_bytes > _MAX_ZIP_TOTAL_BYTES:
                            raise ValueError(
                                f"Суммарный размер распакованных файлов превысил лимит "
                                f"({_MAX_ZIP_TOTAL_BYTES} байт)"
                            )
                        dst.write(chunk)
                os.replace(to_long_path(str(tmp_target)), to_long_path(str(target)))
            except BaseException:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                _unlink_quietly(tmp_target)
                raise
            extracted.append(target)
            extracted_rel.add(rel_posix)

    if req_set is not None:
        missing = sorted(r for r in req_set if r not in extracted_rel)
        if missing:
            raise ValueError(f"В архиве отсутствуют обязательные файлы: {', '.join(missing)}")

    return extracted


def download_file(
    url: str,
    dst_path: Path | str,
    *,
    timeout: float = 30.0,
    expected_sha256: str | None = None,
    progress_cb: Callable[[int, int | None], None] | None = None,
    stop_event: threading.Event | None = None,
    chunk_size: int = 256 * 1024,
) -> str:
    """Потоково скачивает файл по HTTPS в случайный .part с расчётом SHA-256 и лимитом 1 ГБ."""
    parsed_initial = urlparse(url)
    if parsed_initial.scheme.lower() != "https":
        raise ValueError(f"Загрузка моделей разрешена только по HTTPS: {redact_urls(url)}")

    target = Path(dst_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if stop_event is not None and stop_event.is_set():
        raise CancelledError("Загрузка моделей остановлена пользователем")

    hasher = hashlib.sha256()
    downloaded = 0
    part_path: Path | None = None
    fd = -1
    current_url = url

    try:
        for _hop in range(_MAX_REDIRECT_HOPS + 1):
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Загрузка моделей остановлена пользователем")

            with requests.get(
                current_url,
                stream=True,
                timeout=timeout,
                allow_redirects=False,
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = (resp.headers.get("Location") or "").strip()
                    if not loc:
                        raise RuntimeError("Сервер вернул пустой заголовок Location при перенаправлении")
                    next_url = urljoin(current_url, loc)
                    if urlparse(next_url).scheme.lower() != "https":
                        raise ValueError(
                            f"Заблокировано небезопасное перенаправление загрузки (не HTTPS): "
                            f"{redact_urls(next_url)}"
                        )
                    current_url = next_url
                    continue

                resp.raise_for_status()
                raw_len = resp.headers.get("Content-Length")
                total: int | None = None
                if raw_len and raw_len.isdigit():
                    total = int(raw_len)
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise ValueError(
                            f"Размер архива моделей превышает допустимый лимит "
                            f"({total} > {_MAX_DOWNLOAD_BYTES} байт)"
                        )

                fd, tmp_name = tempfile.mkstemp(
                    dir=to_long_path(os.fspath(target.parent)),
                    prefix=".dl.",
                    suffix=".part",
                )
                part_path = Path(tmp_name)
                with os.fdopen(fd, "wb") as f:
                    fd = -1
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if stop_event is not None and stop_event.is_set():
                            raise CancelledError("Загрузка моделей остановлена пользователем")
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > _MAX_DOWNLOAD_BYTES:
                            raise ValueError(
                                f"Размер загружаемого файла превысил лимит {_MAX_DOWNLOAD_BYTES} байт"
                            )
                        f.write(chunk)
                        hasher.update(chunk)
                        if progress_cb is not None:
                            progress_cb(downloaded, total)
                break
        else:
            raise RuntimeError(
                f"Превышен лимит перенаправлений ({_MAX_REDIRECT_HOPS}) при загрузке моделей"
            )
    except CancelledError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if part_path is not None:
            _unlink_quietly(part_path)
        raise
    except requests.RequestException as exc:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if part_path is not None:
            _unlink_quietly(part_path)
        raise RuntimeError(f"Ошибка загрузки моделей: {redact_urls(str(exc))}") from exc
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if part_path is not None:
            _unlink_quietly(part_path)
        raise

    assert part_path is not None
    digest = hasher.hexdigest()
    if expected_sha256 is not None and digest.lower() != expected_sha256.strip().lower():
        _unlink_quietly(part_path)
        raise ValueError(
            f"Контрольная сумма SHA-256 не совпала для {target.name}: "
            f"ожидалось {expected_sha256}, получено {digest}"
        )

    os.replace(to_long_path(str(part_path)), to_long_path(str(target)))
    return digest


def _unlink_quietly(path: Path) -> None:
    try:
        lp = to_long_path(str(path))
        if os.path.lexists(lp):
            os.unlink(lp)
    except OSError:
        pass


def _write_manifest(
    pack_dir: Path,
    *,
    pack_name: str,
    url: str,
    zip_sha256: str | None,
    file_hashes: dict[str, str],
) -> Path:
    manifest_path = pack_dir / "manifest.json"
    payload = {
        "pack_name": pack_name,
        "url": url,
        "zip_sha256": zip_sha256,
        "files": dict(sorted(file_hashes.items())),
    }
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        prefix=".manifest.", suffix=".part", dir=os.fspath(pack_dir)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(to_long_path(str(tmp_path)), to_long_path(str(manifest_path)))
    finally:
        if tmp_path.exists():
            try:
                os.unlink(to_long_path(str(tmp_path)))
            except OSError:
                pass
    return manifest_path


def _read_manifest(pack_dir: Path) -> dict[str, object] | None:
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _pack_paths_for(spec: ModelPackSpec, pack_dir: Path) -> ModelPackPaths:
    return ModelPackPaths(
        pack_name=spec.name,
        pack_dir=pack_dir,
        detector_path=pack_dir / spec.detector_file,
        embedder_path=pack_dir / spec.embedder_file,
        genderage_path=pack_dir / spec.genderage_file,
    )


def is_model_pack_ready(
    pack_name: str = "antelopev2",
    models_dir: Path | str | None = None,
    *,
    verify_manifest: bool = False,
) -> bool:
    """Проверяет, что обязательные ONNX-файлы пакета скачаны и соответствуют SHA-256."""
    spec = MODEL_PACKS.get(pack_name)
    if spec is None:
        return False
    pack_dir = _resolve_models_dir(models_dir) / spec.name
    file_stats: dict[str, os.stat_result] = {}
    for fname in spec.required_files:
        fpath = pack_dir / fname
        try:
            st = os.lstat(to_long_path(str(fpath)))
        except OSError:
            return False
        if is_symlink_or_junction(fpath, st):
            return False
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
            return False
        file_stats[fname] = st

    manifest_path = pack_dir / "manifest.json"
    manifest_exists = manifest_path.is_file()
    files_map: dict[str, object] | None = None
    if verify_manifest or manifest_exists:
        manifest = _read_manifest(pack_dir)
        raw_map = manifest.get("files") if manifest else None
        if not isinstance(raw_map, dict):
            if manifest_exists:
                log.warning("Повреждённый manifest.json в %s — пакет требует проверки", pack_dir)
            return False
        files_map = raw_map

    need_pinned = spec.file_sha256s is not None
    need_manifest = verify_manifest or (files_map is not None)
    if not need_pinned and not need_manifest:
        return True

    for fname in spec.required_files:
        fpath = pack_dir / fname
        try:
            actual = _cached_or_fresh_sha256(
                fpath,
                file_stats[fname],
                bypass_cache=verify_manifest,
            )
        except OSError:
            return False

        if spec.file_sha256s is not None:
            pinned_hash = spec.file_sha256s.get(fname)
            if pinned_hash and actual.lower() != pinned_hash.lower():
                return False

        if files_map is not None:
            expected = files_map.get(fname)
            if not isinstance(expected, str) or not expected:
                if verify_manifest:
                    return False
            elif actual.lower() != expected.lower():
                return False
        elif verify_manifest:
            return False

    return True


def ensure_model_pack(
    pack_name: str = "antelopev2",
    models_dir: Path | str | None = None,
    *,
    expected_zip_sha256: str | None = None,
    expected_file_sha256s: dict[str, str] | None = None,
    progress_cb: Callable[[int, int | None], None] | None = None,
    stop_event: threading.Event | None = None,
    timeout: float = 30.0,
) -> ModelPackPaths:
    """Гарантирует наличие проверенных ONNX-моделей на диске: скачивает, сверяет SHA-256 и распаковывает."""
    spec = _get_pack_spec(pack_name)
    root = _resolve_models_dir(models_dir)
    pack_dir = root / spec.name

    manifest_exists = (pack_dir / "manifest.json").is_file()
    if is_model_pack_ready(pack_name, root, verify_manifest=manifest_exists):
        if expected_file_sha256s:
            for fname, exp in expected_file_sha256s.items():
                target = pack_dir / fname
                if not target.is_file():
                    raise ValueError(f"Ожидаемый файл модели не найден: {fname}")
                actual = compute_sha256(str(target), stop_event=stop_event)
                if actual.lower() != exp.strip().lower():
                    raise ValueError(
                        f"Контрольная сумма SHA-256 не совпала для {fname}: "
                        f"ожидалось {exp}, получено {actual}"
                    )
        if not manifest_exists:
            hashes = {
                fname: compute_sha256(str(pack_dir / fname), stop_event=stop_event)
                for fname in spec.required_files
            }
            if spec.file_sha256s is not None:
                mismatch = any(
                    spec.file_sha256s.get(fname, "").lower() != digest.lower()
                    for fname, digest in hashes.items()
                    if fname in spec.file_sha256s
                )
                if mismatch:
                    log.warning(
                        "Файлы пакета моделей '%s' в %s не совпадают с эталонными SHA-256 — "
                        "выполняется повторная загрузка",
                        pack_name,
                        pack_dir,
                    )
                else:
                    _write_manifest(
                        pack_dir,
                        pack_name=spec.name,
                        url=spec.url,
                        zip_sha256=spec.zip_sha256,
                        file_hashes=hashes,
                    )
                    return _pack_paths_for(spec, pack_dir)
            else:
                _write_manifest(
                    pack_dir,
                    pack_name=spec.name,
                    url=spec.url,
                    zip_sha256=None,
                    file_hashes=hashes,
                )
                return _pack_paths_for(spec, pack_dir)
        else:
            return _pack_paths_for(spec, pack_dir)
    elif pack_dir.exists():
        log.warning(
            "Пакет моделей '%s' в %s повреждён или не прошёл проверку контрольных сумм — "
            "выполняется повторная загрузка",
            pack_name,
            pack_dir,
        )

    root.mkdir(parents=True, exist_ok=True)
    zip_path = root / f".{spec.name}.download.zip"
    staging_dir = root / f".{spec.name}.staging"

    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)

    effective_file_shas: Mapping[str, str] | None = (
        expected_file_sha256s if expected_file_sha256s is not None else spec.file_sha256s
    )

    try:
        zip_digest = download_file(
            spec.url,
            zip_path,
            timeout=timeout,
            expected_sha256=expected_zip_sha256 or spec.zip_sha256,
            progress_cb=progress_cb,
            stop_event=stop_event,
        )
        safe_extract_zip(
            zip_path,
            staging_dir,
            required_files=spec.required_files,
            stop_event=stop_event,
        )

        # Проверяем наличие обязательных файлов и считаем их SHA-256
        missing = [f for f in spec.required_files if not (staging_dir / f).is_file()]
        if missing:
            raise ValueError(
                f"В архиве пакета '{spec.name}' отсутствуют обязательные файлы: {', '.join(missing)}"
            )

        file_hashes: dict[str, str] = {}
        for fpath in sorted(staging_dir.rglob("*.onnx")):
            if stop_event is not None and stop_event.is_set():
                raise CancelledError("Проверка моделей остановлена пользователем")
            rel = fpath.relative_to(staging_dir).as_posix()
            file_hashes[rel] = compute_sha256(str(fpath), stop_event=stop_event)

        if effective_file_shas:
            for fname, exp in effective_file_shas.items():
                actual_hash = file_hashes.get(fname)
                if actual_hash is None:
                    raise ValueError(f"В архиве отсутствует файл для проверки SHA-256: {fname}")
                if actual_hash.lower() != exp.strip().lower():
                    raise ValueError(
                        f"Контрольная сумма SHA-256 не совпала для {fname}: "
                        f"ожидалось {exp}, получено {actual_hash}"
                    )

        _write_manifest(
            staging_dir,
            pack_name=spec.name,
            url=spec.url,
            zip_sha256=zip_digest,
            file_hashes=file_hashes,
        )

        if pack_dir.exists():
            shutil.rmtree(pack_dir)
        os.replace(to_long_path(str(staging_dir)), to_long_path(str(pack_dir)))
    finally:
        _unlink_quietly(zip_path)
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)

    return _pack_paths_for(spec, pack_dir)
