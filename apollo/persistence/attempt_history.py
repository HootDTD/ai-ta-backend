"""Attempt-history queries used by the XP awarder.

A 're-attempt' for XP purposes is any Done event on a problem the student
has previously been graded on — across all their sessions. We detect it
by looking for any other ProblemAttempt row (joined through ApolloSession
to the student_id) for the same problem_id that already has a non-null
`result`. The current attempt id is excluded so a within-session retry —
which overwrites the same row after Phase 1's /retry endpoint — is
handled upstream by checking the attempt's own `result` before this call."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ApolloSession, ProblemAttempt


async def has_prior_graded_attempt(
    *,
    db: AsyncSession,
    student_id: str,
    problem_id: str,
    exclude_attempt_id: int,
) -> bool:
    """True iff another graded ProblemAttempt exists for this (student, problem)."""
    stmt = (
        select(func.count())
        .select_from(ProblemAttempt)
        .join(ApolloSession, ApolloSession.id == ProblemAttempt.session_id)
        .where(
            ApolloSession.student_id == student_id,
            ProblemAttempt.problem_id == problem_id,
            ProblemAttempt.result.is_not(None),
            ProblemAttempt.id != exclude_attempt_id,
        )
    )
    count = (await db.execute(stmt)).scalar_one()
    return count > 0
