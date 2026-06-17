"""Overseer.problem_selector — pick a problem from the DB problem bank.

WU-3D §8A cutover: problems are loaded from ``apollo_concept_problems`` rows by
``concept_id`` (+difficulty), NOT from the filesystem and NOT via a legacy
``cluster_id`` map. Each row's ``payload`` is validated through
``Problem.model_validate`` (pydantic).

Deterministic: sorted by ``Problem.id`` (== ``payload['id']`` == ``problem_code``).
Refresh on every call (no caching). Raises ``PoolExhaustedError`` if no
unattempted problem at the requested difficulty remains.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import PoolExhaustedError
from apollo.persistence.models import ConceptProblem
from apollo.schemas.problem import Problem


async def list_problems_for_concept(db: AsyncSession, *, concept_id: int) -> list[Problem]:
    """Load every ``apollo_concept_problems`` row for a concept and validate each
    row's ``payload`` through ``Problem.model_validate``, sorted by ``Problem.id``
    for determinism. NO filesystem read."""
    rows = (
        (
            await db.execute(
                select(ConceptProblem.payload).where(ConceptProblem.concept_id == concept_id)
            )
        )
        .scalars()
        .all()
    )
    problems = [Problem.model_validate(payload) for payload in rows]
    return sorted(problems, key=lambda p: p.id)


async def select_problem(
    db: AsyncSession,
    *,
    concept_id: int,
    difficulty: str,
    attempted_ids: Sequence[str],
) -> Problem:
    """Pick the first unattempted ``Problem`` at ``difficulty`` for ``concept_id``.

    Raises ``PoolExhaustedError`` (with ``concept_cluster_id=str(concept_id)`` for
    API back-compat) when none remain.
    """
    pool = await list_problems_for_concept(db, concept_id=concept_id)
    attempted = set(attempted_ids)
    candidates = [p for p in pool if p.difficulty == difficulty and p.id not in attempted]
    if not candidates:
        raise PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)
    return candidates[0]
