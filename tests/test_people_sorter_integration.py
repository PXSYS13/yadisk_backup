"""Интеграционные, security и регрессионные тесты модуля сортировки по людям (/api/people/*) в yadisk_backup."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

import webapp.paths
import webapp.people as people_mod
import webui
from sorter.clustering import find_age_bridge_candidates, rebuild_person_prototypes
from sorter.state import SorterState
from sorter.utils import validate_dir_safe
from sorter.vision import FakeFaceDetector, FakeFaceEmbedder, VisionEngine


def _client() -> TestClient:
    return TestClient(webui.app, headers={"Host": "localhost:8000"})


def _make_portrait(path: Path, color: tuple[int, int, int], dt: str) -> None:
    img = Image.new("RGB", (240, 240), (240, 240, 240))
    for x in range(60, 180):
        for y in range(60, 180):
            img.putpixel((x, y), color)
    exif = img.getexif()
    exif[0x9003] = dt
    img.save(path, format="JPEG", quality=92, exif=exif)


def test_people_security_extra_forbid_crop_ads_and_unc(tmp_path: Path) -> None:
    c = _client()
    # Чужие поля отклоняются (ConfigDict extra='forbid')
    assert c.post("/api/people/age-bridges/decision", json={
        "person_a_id": 1, "person_b_id": 2, "decision": "merge", "name": "Злата",
    }).status_code == 422
    assert c.post("/api/people/settings", json={"bogus": 1}).status_code == 422

    # UNC / Windows extended namespace bypasses отклоняются
    for bad in (
        r"\\?\unc\evil\share",
        r"\\?\GLOBALROOT\Device\Mup\evil\share",
        r"\\??\C:\Users",
        r"\\.\pipe\test",
    ):
        try:
            validate_dir_safe(bad)
            raise AssertionError(f"Expected ValueError for {bad}")
        except ValueError:
            pass
        assert c.post("/api/people/settings", json={"source_dir": bad}).status_code == 400

    # Проверка блокировки path traversal и NTFS ADS ':' в /api/people/faces/{id}/crop
    with SorterState(webapp.paths.PEOPLE_DB_FILE) as st:
        mid, _ = st.upsert_media_file(
            local_path=str(tmp_path / "p.jpg"),
            rel_path="p.jpg",
            source_root=str(tmp_path),
            media_kind="photo",
            size=100,
            mtime_ns=1_700_000_000_000_000_000,
            shot_ts=1_700_000_000.0,
            date_source="exif",
            thumb_rel_path="../people_sorter.db",
        )
        v = np.zeros(512, dtype=np.float32)
        v[0] = 1.0
        fids = st.replace_faces_for_media(
            mid,
            [
                {
                    "frame_ms": 0,
                    "bbox": [10, 10, 100, 100],
                    "kps": [[30, 40]] * 5,
                    "det_score": 0.99,
                    "quality_score": 0.9,
                    "embedding": v,
                    "crop_rel_path": "sub/evil.jpg:ads",
                }
            ],
        )
    assert c.get(f"/api/people/faces/{fids[0]}/crop").status_code == 404
    assert c.get(f"/api/people/media/{mid}/thumb").status_code == 404


def test_people_full_4_step_workflow_inside_yadisk_backup(
    tmp_path: Path, monkeypatch
) -> None:
    src = tmp_path / "archive"
    dst = tmp_path / "sorted"
    src.mkdir()
    _make_portrait(src / "z1.jpg", (210, 80, 80), "2020:07:10 12:00:00")
    _make_portrait(src / "z2.jpg", (212, 82, 80), "2020:08:11 12:00:00")
    _make_portrait(src / "m1.jpg", (60, 140, 210), "2023:05:15 10:00:00")

    monkeypatch.setattr(
        people_mod.manager,
        "_engine_factory",
        lambda _pack, _gpu: VisionEngine(
            FakeFaceDetector(),
            FakeFaceEmbedder(),
            crops_dir=webapp.paths.PEOPLE_CROPS_DIR,
            thumbs_dir=webapp.paths.PEOPLE_THUMBS_DIR,
        ),
    )

    c = _client()
    # Шаг 1: Сканирование + поиск лиц + кластеризация
    r_scan = c.post(
        "/api/people/scan/start",
        json={"source_dir": str(src), "recluster": True, "model_pack": "buffalo_l", "prefer_gpu": False},
    )
    assert r_scan.status_code == 200
    for _ in range(100):
        st_data = c.get("/api/people/status").json()
        if not st_data["running"]:
            break
        time.sleep(0.05)
    assert st_data["phase"] == "done"
    assert st_data["config"]["model_pack"] == "buffalo_l"
    assert st_data["config"]["prefer_gpu"] is False
    assert st_data["stats"]["media_total"] == 3
    assert st_data["stats"]["faces_active"] >= 3

    # Шаг 2: Именование, прототипы в /faces, кроп, Undo
    persons = c.get("/api/people/persons").json()["items"]
    assert len(persons) >= 1
    pid = persons[0]["id"]
    cover_fid = persons[0]["cover_face_id"]
    assert c.get(f"/api/people/faces/{cover_fid}/crop").status_code == 200

    r_ren = c.patch(f"/api/people/persons/{pid}", json={"name": "Злата", "birth_year": 2019})
    assert r_ren.status_code == 200
    r_faces = c.get(f"/api/people/persons/{pid}/faces")
    assert r_faces.status_code == 200
    assert "prototypes" in r_faces.json()["person"]
    assert "local_path" not in r_faces.json()["items"][0]

    # Шаг 3: Dry-Run предпросмотр с возрастом ребёнка
    r_plan = c.post(
        "/api/people/plan/preview",
        json={
            "source_dir": str(src),
            "target_dir": str(dst),
            "mode": "hardlink",
            "include_child_age": True,
        },
    )
    assert r_plan.status_code == 200
    plan = r_plan.json()
    assert plan["total_ops"] >= 3
    assert plan["enough_space"] is True

    # Шаг 4: Сортировка Hardlink + история запусков + полный Откат
    r_sort = c.post(
        "/api/people/sort/start",
        json={"source_dir": str(src), "target_dir": str(dst), "mode": "hardlink"},
    )
    assert r_sort.status_code == 200
    for _ in range(100):
        st_data = c.get("/api/people/status").json()
        if not st_data["running"]:
            break
        time.sleep(0.05)
    assert st_data["phase"] == "done"

    runs = c.get("/api/people/runs").json()["items"]
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["pending_ops"] == 0
    assert runs[0]["rolled_back_ops"] == 0
    run_id = runs[0]["id"]

    # Откат
    r_rb = c.post(f"/api/people/runs/{run_id}/rollback", json={})
    assert r_rb.status_code == 200
    for _ in range(100):
        st_data = c.get("/api/people/status").json()
        if not st_data["running"]:
            break
        time.sleep(0.05)
    assert st_data["phase"] == "done"

    runs_after = c.get("/api/people/runs").json()["items"]
    assert runs_after[0]["status"] == "rolled_back"
    # Повторный resume откатанного запуска должен быть отклонён (HIGH-8)
    assert c.post(f"/api/people/runs/{run_id}/resume", json={}).status_code == 400
    # Оригиналы не тронуты
    assert (src / "z1.jpg").exists() and (src / "z2.jpg").exists()


def test_age_bridge_premerge_birth_year_validation_and_undo_after_recluster(tmp_path: Path) -> None:
    rng = np.random.default_rng(11)

    def unit(v: np.ndarray) -> np.ndarray:
        return (v / np.linalg.norm(v)).astype(np.float32)

    base = unit(rng.standard_normal(512))
    ortho = rng.standard_normal(512)
    ortho -= np.dot(ortho, base) * base
    later = unit(0.55 * base + np.sqrt(1.0 - 0.55**2) * unit(ortho))

    with SorterState(webapp.paths.PEOPLE_DB_FILE) as st:
        m1, _ = st.upsert_media_file(
            local_path=str(tmp_path / "a.jpg"), rel_path="a.jpg", source_root=str(tmp_path),
            media_kind="photo", size=100, mtime_ns=1, shot_ts=1_590_000_000.0, date_source="exif",
        )
        m2, _ = st.upsert_media_file(
            local_path=str(tmp_path / "b.jpg"), rel_path="b.jpg", source_root=str(tmp_path),
            media_kind="photo", size=100, mtime_ns=2, shot_ts=1_685_000_000.0, date_source="exif",
        )
        f1 = st.replace_faces_for_media(m1, [{
            "frame_ms": 0, "bbox": [0, 0, 100, 100], "kps": [[10, 10]] * 5,
            "det_score": 0.99, "quality_score": 0.9, "est_age": 2.0, "est_gender": 0, "embedding": base,
        }])[0]
        f2 = st.replace_faces_for_media(m2, [{
            "frame_ms": 0, "bbox": [0, 0, 100, 100], "kps": [[10, 10]] * 5,
            "det_score": 0.99, "quality_score": 0.9, "est_age": 5.0, "est_gender": 0, "embedding": later,
        }])[0]
        p1 = st.create_person(name="Злата", birth_year=2018, is_named=True)
        p2 = st.create_person(name="", is_named=False)
        st.set_face_assignments([(f1, 1, p1, 0.99, "cluster"), (f2, 2, p2, 0.99, "cluster")])
        rebuild_person_prototypes(st)
        assert len(find_age_bridge_candidates(st)) >= 1

    c = _client()
    # Неверный birth_year отклоняется ДО слияния (p2 остаётся цел!)
    bad = c.post(
        "/api/people/age-bridges/decision",
        json={
            "person_a_id": p1,
            "person_b_id": p2,
            "decision": "merge",
            "merged_name": "Злата",
            "birth_year": 1800,
        },
    )
    assert bad.status_code == 400
    assert len(c.get("/api/people/persons").json()["items"]) == 2

    # Валидное слияние с merged_name проходит и не создаёт дубль-запись «Переименование» в Undo
    ok = c.post(
        "/api/people/age-bridges/decision",
        json={
            "person_a_id": p1,
            "person_b_id": p2,
            "decision": "merge",
            "merged_name": "Злата",
            "birth_year": 2018,
        },
    )
    assert ok.status_code == 200
    items = c.get("/api/people/persons").json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Злата"
    assert items[0]["face_count"] == 2
    last_undo = c.get("/api/people/status").json()["last_undo"]
    assert last_undo is not None
    assert last_undo["action_type"] == "merge"
