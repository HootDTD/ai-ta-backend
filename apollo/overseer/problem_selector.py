"""Overseer.problem_selector — pick a problem from the authored bank.

Loads problems from apollo/problems/<cluster_id>/*.json on-demand. No
caching at Slice 0; refresh on every call. Deterministic: sorted by id.
Raises PoolExhaustedError if no unattempted problem at the requested
difficulty remains."""
from __future__ import annotations

from pathlib import Path
from typing import List, Sequence

from apollo.errors import PoolExhaustedError
from apollo.schemas.problem import Problem, load_problem

_PROBLEMS_ROOT = Path(__file__).resolve().parent.parent / "problems"

_CLUSTER_DIRS = {
    "fluid_mechanics": "bernoulli",
}


def list_problems_for_cluster(cluster_id: str) -> List[Problem]:
    subdir = _CLUSTER_DIRS.get(cluster_id)
    if subdir is None:
        return []
    dir_path = _PROBLEMS_ROOT / subdir
    if not dir_path.exists():
        return []
    return [load_problem(p) for p in sorted(dir_path.glob("problem_*.json"))]


def select_problem(
    *,
    cluster_id: str,
    difficulty: str,
    attempted_ids: Sequence[str],
) -> Problem:
    pool = list_problems_for_cluster(cluster_id)
    candidates = [
        p for p in pool
        if p.difficulty == difficulty and p.id not in set(attempted_ids)
    ]
    if not candidates:
        raise PoolExhaustedError(concept_cluster_id=cluster_id, difficulty=difficulty)
    return candidates[0]
