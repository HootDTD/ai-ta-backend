"""Attempt-history queries used by the XP awarder.

A 're-attempt' for XP purposes is any Done event on a problem the user
has previously been graded on — across all their sessions. We detect it
by looking for any other ProblemAttempt row (joined through TutoringSession
to the user_id) for the same problem_id whose `result` is a graded
terminal value (`GRADED_ATTEMPT_RESULTS`: the legacy solver outcomes plus
`graded`, the current diff+rubric outcome) —
`abandoned` is excluded because it represents a mid-problem switch, not a
completed grading. The current attempt id is excluded so a within-session
retry — which overwrites the same row after Phase 1's /retry endpoint —
is handled upstream by checking the attempt's own `result` before this call."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import (
    GRADED_ATTEMPT_RESULTS,
    ProblemAttempt,
)


async def has_prior_graded_attempt(
    *,
    db: AsyncSession,
    user_id: str,
    course_id: int,
    problem_id: int,
    exclude_attempt_id: int,
) -> bool:
    """True iff another graded ProblemAttempt exists for this (user, problem)."""
    stmt = (
        select(func.count())
        .select_from(ProblemAttempt)
        .where(
            ProblemAttempt.user_id == user_id,
            ProblemAttempt.course_id == course_id,
            ProblemAttempt.problem_id == problem_id,
            ProblemAttempt.result.in_(GRADED_ATTEMPT_RESULTS),
            ProblemAttempt.id != exclude_attempt_id,
        )
    )
    count = (await db.execute(stmt)).scalar_one()
    return count > 0
