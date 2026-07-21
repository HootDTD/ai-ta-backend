"""Resolve durable problem-bank linkage for mastery-event writes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import Problem


async def resolve_problem_id(
    db: AsyncSession,
    *,
    concept_id: int,
    course_id: int,
    problem_identity: str | int,
) -> int | None:
    """Resolve either a public code or internal bigint within one course/concept.

    Tier-2 teachable rows win over tier-1 inventory twins; the newest row id is
    the deterministic tiebreak. Legacy or fixture problem codes may have no bank
    row, in which case the mastery event remains valid with a NULL linkage.
    """
    return (
        await db.execute(
            select(Problem.id)
            .where(
                Problem.course_id == course_id,
                Problem.concept_id == concept_id,
                (Problem.id == problem_identity)
                if isinstance(problem_identity, int)
                else (Problem.problem_code == problem_identity),
                Problem.quarantined_at.is_(None),
            )
            .order_by(Problem.tier.desc(), Problem.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
