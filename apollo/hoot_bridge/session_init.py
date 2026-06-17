"""Hoot → Apollo handoff initialization.

1. End any existing active session for this user (stale handoffs don't block new ones).
2. Overseer builds the course's candidate concepts (apollo_concepts scoped via
   search_space_id) and infers the concept_id from the Hoot transcript.
3. Overseer picks the first problem at the requested difficulty from the DB.
4. Session row created (phase=TEACHING, concept_id populated), first
   ProblemAttempt row created.
5. Return {session_id, problem} to the frontend.

WU-3D §8A cutover: the candidate set is resolved from the DB (no hard-coded
curriculum list) and ``apollo_sessions.concept_id`` is populated; the legacy
cluster column is no longer written (dropped in migration 027).

Raises NoMatchingConceptError or PoolExhaustedError — these are mapped
to 409s by the FastAPI exception handlers.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.concept_inference import infer_concept_id
from apollo.overseer.problem_selector import select_problem
from apollo.persistence.models import (
    ApolloSession,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.subjects.curriculum_db import list_course_concepts

_ALLOWED_DIFFICULTIES = {"intro", "standard", "hard"}


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

    problem = await select_problem(
        db,
        concept_id=concept_id,
        difficulty=difficulty,
        attempted_ids=[],
    )

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
