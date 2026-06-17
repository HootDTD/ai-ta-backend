"""POST /apollo/sessions/{id}/next — advance to a new problem at the student's chosen difficulty.

Unified endpoint: handles both post-Done advance (phase=REPORT) and mid-problem
abandon (phase=TEACHING or PROBLEM_REVEAL). Blocked with SessionFrozenError
during SOLVING. INIT / BETWEEN raise InvalidPhaseError.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import InvalidPhaseError, SessionFrozenError
from apollo.overseer.problem_selector import select_problem
from apollo.persistence.models import (
    ApolloSession,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)

_ABANDON_PHASES = {SessionPhase.TEACHING.value, SessionPhase.PROBLEM_REVEAL.value}
_ADVANCE_PHASES = {SessionPhase.REPORT.value}
_FROZEN_PHASES = {SessionPhase.SOLVING.value}


async def handle_next(
    *,
    db: AsyncSession,
    session_id: int,
    difficulty: str,
) -> dict[str, Any]:
    # with_for_update() takes a row lock on Postgres so a double-clicked
    # /next can't race into two ProblemAttempt rows. SQLite ignores it.
    sess = (
        await db.execute(
            select(ApolloSession).where(ApolloSession.id == session_id).with_for_update()
        )
    ).scalar_one()

    if sess.status != SessionStatus.active.value:
        raise InvalidPhaseError(session_id=session_id, phase=f"status={sess.status}")

    phase = sess.phase
    if phase in _FROZEN_PHASES:
        raise SessionFrozenError(session_id=str(session_id))
    if phase not in _ABANDON_PHASES and phase not in _ADVANCE_PHASES:
        raise InvalidPhaseError(session_id=session_id, phase=phase)

    current_attempt = (
        (
            await db.execute(
                select(ProblemAttempt)
                .where(ProblemAttempt.session_id == session_id)
                .where(ProblemAttempt.problem_id == sess.current_problem_id)
                .order_by(ProblemAttempt.id.desc())
            )
        )
        .scalars()
        .first()
    )

    if phase in _ABANDON_PHASES and current_attempt is not None and current_attempt.result is None:
        current_attempt.result = "abandoned"
        await db.flush()

    attempted_ids = list(
        (
            await db.execute(
                select(ProblemAttempt.problem_id).where(ProblemAttempt.session_id == session_id)
            )
        )
        .scalars()
        .all()
    )

    problem = await select_problem(
        db,
        concept_id=sess.concept_id,
        difficulty=difficulty,
        attempted_ids=attempted_ids,
    )

    new_attempt = ProblemAttempt(
        session_id=session_id,
        problem_id=problem.id,
        difficulty=difficulty,
    )
    db.add(new_attempt)
    await db.flush()

    sess.current_problem_id = problem.id
    sess.phase = SessionPhase.TEACHING.value
    await db.commit()

    return {
        "session_id": session_id,
        "attempt_id": new_attempt.id,
        "problem": {
            "id": problem.id,
            "concept_id": problem.concept_id,
            "difficulty": problem.difficulty,
            "problem_text": problem.problem_text,
            "given_values": problem.given_values,
            "target_unknown": problem.target_unknown,
        },
    }
