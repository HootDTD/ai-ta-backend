"""Apollo session initialization: Hoot handoff + standalone entry.

init_session_from_hoot — the original Hoot→Apollo handoff. The transcript is
used for exactly one thing: infer_concept_id picks the concept. Everything
else is shared with the standalone path.

init_session_direct — WU-E2E standalone entry (2026-07-07 spec). The student
explicitly picks concept_id (validated against the course's teachable set) and
optionally a specific problem_id (validated against the concept's teachable
pool). No LLM call, no transcript.

Both raise NoMatchingConceptError / PoolExhaustedError (409) —
init_session_direct additionally raises ProblemNotFoundError (404).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import NoMatchingConceptError, ProblemNotFoundError
from apollo.overseer.concept_inference import infer_concept_id
from apollo.overseer.problem_selector import (
    list_problems_for_concept,
    select_problem_personalized,
)
from apollo.persistence.models import (
    ApolloSession,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.schemas.problem import Problem
from apollo.subjects.curriculum_db import list_course_concepts

_ALLOWED_DIFFICULTIES = {"intro", "standard", "hard"}


async def _create_session_with_problem(
    db: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    concept_id: int,
    difficulty: str,
    problem: Problem,
) -> dict[str, Any]:
    """Shared tail of both entries: end any active session, create the
    TEACHING session + first attempt, commit, return the FE payload.
    Moved verbatim from init_session_from_hoot (WU-3D shape unchanged)."""
    await db.execute(
        update(ApolloSession)
        .where(
            ApolloSession.user_id == user_id,
            ApolloSession.status == SessionStatus.active.value,
        )
        .values(status=SessionStatus.ended.value)
    )
    await db.flush()

    session = ApolloSession(
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id=problem.id,
    )
    db.add(session)
    await db.flush()

    attempt = ProblemAttempt(
        session_id=session.id,
        problem_id=problem.id,
        difficulty=difficulty,
    )
    db.add(attempt)
    await db.flush()
    attempt_id = attempt.id
    await db.commit()

    return {
        "session_id": session.id,
        "attempt_id": attempt_id,
        "problem": {
            "id": problem.id,
            "concept_id": problem.concept_id,
            "difficulty": problem.difficulty,
            "problem_text": problem.problem_text,
            "given_values": problem.given_values,
            "target_unknown": problem.target_unknown,
        },
    }


async def init_session_from_hoot(
    *,
    db: AsyncSession,
    user_id: str,
    search_space_id: int,
    hoot_transcript: str,
    difficulty: str,
) -> dict[str, Any]:
    if difficulty not in _ALLOWED_DIFFICULTIES:
        raise ValueError(
            f"unknown difficulty {difficulty!r}; expected one of {sorted(_ALLOWED_DIFFICULTIES)}"
        )

    candidates = await list_course_concepts(db, search_space_id=search_space_id)
    concept_id = infer_concept_id(
        transcript=hoot_transcript,
        candidates=candidates,
    )

    problem = await select_problem_personalized(
        db,
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        difficulty=difficulty,
        attempted_ids=[],
    )
    return await _create_session_with_problem(
        db,
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        difficulty=difficulty,
        problem=problem,
    )


async def init_session_direct(
    *,
    db: AsyncSession,
    user_id: str,
    search_space_id: int,
    concept_id: int,
    difficulty: str,
    problem_id: str | None = None,
) -> dict[str, Any]:
    if difficulty not in _ALLOWED_DIFFICULTIES:
        raise ValueError(
            f"unknown difficulty {difficulty!r}; expected one of {sorted(_ALLOWED_DIFFICULTIES)}"
        )

    candidates = await list_course_concepts(db, search_space_id=search_space_id)
    if concept_id not in {c.concept_id for c in candidates}:
        raise NoMatchingConceptError(
            f"concept_id={concept_id} is not teachable in course {search_space_id}"
        )

    if problem_id is not None:
        pool = await list_problems_for_concept(db, concept_id=concept_id)
        problem = next((p for p in pool if p.id == problem_id), None)
        if problem is None:
            raise ProblemNotFoundError(problem_id=problem_id, concept_id=concept_id)
    else:
        problem = await select_problem_personalized(
            db,
            user_id=user_id,
            search_space_id=search_space_id,
            concept_id=concept_id,
            difficulty=difficulty,
            attempted_ids=[],
        )

    return await _create_session_with_problem(
        db,
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        difficulty=difficulty,
        problem=problem,
    )
