"""Юнит-тесты фундамента (Волна 0): SQLite WAL state, media_scan, даты EXIF/MP4 и утилиты."""

from __future__ import annotations

import calendar
import os
import struct
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sorter import paths
from sorter.media_scan import (
    classify_media_file,
    date_from_filename,
    extract_shot_time_with_source,
    scan_directory,
)
from sorter.state import SorterState, decode_embedding, encode_embedding
from sorter.utils import (
    format_age_ru,
    sanitize_filename,
    sanitize_segment,
    set_file_time,
    validate_dir_safe,
)


def _make_jpeg_with_exif(path: Path, dt_str: str = "2023:07:15 14:30:00") -> None:
    img = Image.new("RGB", (64, 64), color=(120, 180, 200))
    exif = img.getexif()
    exif[0x9003] = dt_str
    exif[0x0132] = dt_str
    img.save(path, format="JPEG", exif=exif)


def _make_minimal_mp4(path: Path, unix_ts: int) -> None:
    """Создаёт минимальный валидный MP4 контейнер (ftyp + moov/mvhd v0)."""
    ftyp = struct.pack(">I4s4sI4s", 20, b"ftyp", b"isom", 512, b"isom")
    mp4_ctime = unix_ts + 2_082_844_800
    mvhd_body = struct.pack(">B3sIIII", 0, b"\x00\x00\x00", mp4_ctime, mp4_ctime, 1000, 5000)
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    path.write_bytes(ftyp + moov)


def test_embedding_roundtrip_normalized() -> None:
    raw = np.arange(512, dtype=np.float32) + 1.0
    blob = encode_embedding(raw)
    assert len(blob) == 512 * 4
    out = decode_embedding(blob)
    assert out.shape == (512,)
    assert abs(float(np.linalg.norm(out)) - 1.0) < 1e-5
    expected = raw / np.linalg.norm(raw)
    assert float(np.dot(out, expected)) > 0.99999


def test_state_media_faces_same_photo_cannot_link_and_merge_undo(tmp_path: Path) -> None:
    with SorterState(tmp_path / "state.db") as st:
        mid, changed = st.upsert_media_file(
            local_path=str(tmp_path / "family.jpg"),
            rel_path="family.jpg",
            source_root=str(tmp_path),
            media_kind="photo",
            size=1234,
            mtime_ns=1_700_000_000_000_000_000,
            partial_hash="abc",
            shot_ts=1_700_000_000.0,
            date_source="exif",
        )
        assert changed is True

        v1 = np.zeros(512, dtype=np.float32)
        v1[0] = 1.0
        v2 = np.zeros(512, dtype=np.float32)
        v2[1] = 1.0
        fids = st.replace_faces_for_media(
            mid,
            [
                {
                    "frame_ms": 0,
                    "bbox": [10, 10, 100, 100],
                    "kps": [[30, 40]] * 5,
                    "det_score": 0.99,
                    "quality_score": 0.9,
                    "est_age": 4.0,
                    "est_gender": 0,
                    "embedding": v1,
                },
                {
                    "frame_ms": 0,
                    "bbox": [120, 10, 210, 100],
                    "kps": [[140, 40]] * 5,
                    "det_score": 0.95,
                    "quality_score": 0.85,
                    "est_age": 32.0,
                    "est_gender": 0,
                    "embedding": v2,
                },
            ],
        )
        assert len(fids) == 2
        cannot_links = st.get_cannot_link_face_pairs()
        assert (min(fids), max(fids)) in cannot_links

        p1 = st.create_person(name="Злата", birth_year=2019, is_named=True)
        p2 = st.create_person(name="", is_named=False)
        st.set_face_assignments(
            [
                (fids[0], 1, p1, 0.99, "cluster"),
                (fids[1], 2, p2, 0.98, "cluster"),
            ]
        )
        st.replace_person_prototypes(
            p1,
            [{"year_bucket": 2023, "face_count": 1, "mean_est_age": 4.0, "centroid": v1}],
        )
        assert len(st.list_prototypes(p1)) == 1

        snap = st.merge_persons(p1, p2)
        assert st.get_person(p2) is None
        assert len(st.list_faces(person_id=p1)) == 2
        assert snap["source_before"]["id"] == p2

        hid = st.push_history("merge", "Объединение", snap)
        last = st.get_last_undoable()
        assert last is not None and last["id"] == hid
        st.mark_history_undone(hid)
        assert st.get_last_undoable() is None

        stats = st.get_stats()
        assert stats["media_total"] == 1
        assert stats["faces_active"] == 2
        assert stats["persons_named"] == 1


def test_media_scan_exif_mp4_filename_and_incremental(tmp_path: Path) -> None:
    src = tmp_path / "photos"
    sub = src / "2023"
    sub.mkdir(parents=True)

    jpg = sub / "IMG_0001.jpg"
    _make_jpeg_with_exif(jpg, "2023:07:15 14:30:00")

    mp4_ts = int(calendar.timegm((2024, 3, 20, 10, 0, 0)))
    mp4 = sub / "clip.mp4"
    _make_minimal_mp4(mp4, mp4_ts)

    png_by_name = src / "PXL_20220509_081500.png"
    Image.new("RGB", (32, 32), color=(255, 0, 0)).save(png_by_name, format="PNG")

    # Поддельный jpg (текст внутри) должен отброситься по magic bytes
    fake = src / "broken.jpg"
    fake.write_text("not a jpeg", encoding="utf-8")

    # Служебный каталог .git должен пропускаться
    git_dir = src / ".git"
    git_dir.mkdir()
    _make_jpeg_with_exif(git_dir / "ignored.jpg")

    ts_jpg, src_jpg = extract_shot_time_with_source(str(jpg), "photo")
    assert src_jpg == "exif"
    assert ts_jpg == float(calendar.timegm((2023, 7, 15, 14, 30, 0)))

    ts_mp4, src_mp4 = extract_shot_time_with_source(str(mp4), "video")
    assert src_mp4 == "mvhd"
    assert ts_mp4 == float(mp4_ts)

    ts_png, src_png = extract_shot_time_with_source(str(png_by_name), "photo")
    assert src_png == "filename"
    assert ts_png == date_from_filename("PXL_20220509_081500.png")

    assert not classify_media_file(str(fake), fake.stat().st_size).accepted

    with SorterState(tmp_path / "scan.db") as st:
        s1 = scan_directory(str(src), st)
        assert s1.discovered == 3
        assert s1.photos == 2
        assert s1.videos == 1
        assert s1.new_or_changed == 3
        assert s1.unsupported_skipped == 1

        for row in st.list_media():
            st.mark_media_scanned(int(row["id"]))

        # Повторный скан без изменений ничего не пересканирует
        s2 = scan_directory(str(src), st)
        assert s2.discovered == 3
        assert s2.new_or_changed == 0
        assert s2.unchanged_skipped == 3


def test_utils_sanitize_security_and_file_time(tmp_path: Path) -> None:
    assert sanitize_segment("CON") == "CON_"
    assert sanitize_segment("  Злата / 2024..  ") == "Злата _ 2024"
    assert sanitize_filename("NUL.jpg") == "NUL_.jpg"
    assert format_age_ru(0) == "до 1 года"
    assert format_age_ru(1) == "1 год"
    assert format_age_ru(4) == "4 года"
    assert format_age_ru(7) == "7 лет"

    with pytest.raises(ValueError):
        validate_dir_safe(r"\\server\share\photos")
    with pytest.raises(ValueError):
        validate_dir_safe("bad\x00path")

    f = tmp_path / "sample.bin"
    f.write_bytes(b"hello")
    target_ts = float(calendar.timegm((2021, 6, 1, 12, 0, 0)))
    assert set_file_time(str(f), target_ts, created=target_ts) is True
    assert abs(os.stat(f).st_mtime - target_ts) <= 2.0

    old_data = paths.DATA_DIR
    try:
        paths.configure_data_dir(tmp_path / "data")
        paths.ensure_runtime_dirs()
        assert paths.MODELS_DIR.is_dir()
        assert paths.CROPS_DIR.is_dir()
    finally:
        paths.configure_data_dir(old_data)
