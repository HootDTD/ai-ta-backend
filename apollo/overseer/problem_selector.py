"""Overseer.problem_selector — pick a problem from the authored bank.

Loads problems from the V3 concept registry:
    apollo/subjects/<subject>/concepts/<concept>/problems/*.json

`cluster_id` (legacy single-string identifier) is mapped to a
(subject_id, concept_id) pair via _CLUSTER_TO_CONCEPT.

Deterministic: sorted by id. Refresh on every call (no caching).
Raises PoolExhaustedError if no unattempted problem at the requested
difficulty remains.
"""
from __future__ import annotations

from typing import List, Sequence

from apollo.errors import PoolExhaustedError
from apollo.schemas.problem import Problem, load_problem
from apollo.subjects import ConceptNotFoundError, load_concept

# Legacy cluster_id -> (subject_id, concept_id). Apollo handlers still pass
# the single cluster_id; this map is the only place the mapping lives.
_CLUSTER_TO_CONCEPT: dict[str, tuple[str, str]] = {
    "fluid_mechanics": ("fluid_mechanics", "bernoulli_principle"),
}


def cluster_to_concept(cluster_id: str) -> tuple[str, str]:
    """Resolve a legacy cluster_id to its (subject_id, concept_id) registry pair.

    Raises KeyError if the cluster is not mapped.
    """
    return _CLUSTER_TO_CONCEPT[cluster_id]


def list_problems_for_cluster(cluster_id: str) -> List[Problem]:
    pair = _CLUSTER_TO_CONCEPT.get(cluster_id)
    if pair is None:
        return []
    subject_id, concept_id = pair
    try:
        concept = load_concept(subject_id, concept_id)
    except ConceptNotFoundError:
        return []
    if not concept.problems_dir.exists():
        return []
    return [
        load_problem(p)
        for p in sorted(concept.problems_dir.glob("problem_*.json"))
    ]


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
