"""Пакет кластеризации, возрастных мостов (Child-Aging), прототипов и обратимых действий Студии."""

from __future__ import annotations

from sorter.clustering.age_bridge import AgeBridgeCandidate, find_age_bridge_candidates
from sorter.clustering.engine import ClusterConfig, run_initial_clustering
from sorter.clustering.prototypes import (
    assign_unassigned_faces,
    list_person_faces_outlier_first,
    rebuild_person_prototypes,
    year_from_ts,
)
from sorter.clustering.studio import (
    ignore_person_action,
    merge_persons_action,
    reject_age_pair_action,
    reject_face_action,
    rename_person_action,
    split_faces_action,
    undo_last_action,
)

__all__ = [
    "AgeBridgeCandidate",
    "ClusterConfig",
    "assign_unassigned_faces",
    "find_age_bridge_candidates",
    "ignore_person_action",
    "list_person_faces_outlier_first",
    "merge_persons_action",
    "rebuild_person_prototypes",
    "reject_age_pair_action",
    "reject_face_action",
    "rename_person_action",
    "run_initial_clustering",
    "split_faces_action",
    "undo_last_action",
    "year_from_ts",
]
