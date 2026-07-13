"""Resolve durable problem-bank linkage for mastery-event writes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ConceptProblem


async def resolve_concept_problem_id(
    db: AsyncSession, *, concept_id: int, problem_code: str
) -> int | None:
    """Return the preferred live bank row for ``(concept_id, problem_code)``.

    Tier-2 teachable rows win over tier-1 inventory twins; the newest row id is
    the deterministic tiebreak. Legacy or fixture problem codes may have no bank
    row, in which case the mastery event remains valid with a NULL linkage.
    """
    return (
        await db.execute(
            select(ConceptProblem.id)
            .where(
                ConceptProblem.concept_id == concept_id,
                ConceptProblem.problem_code == problem_code,
                ConceptProblem.quarantined_at.is_(None),
            )
            .order_by(ConceptProblem.tier.desc(), ConceptProblem.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
