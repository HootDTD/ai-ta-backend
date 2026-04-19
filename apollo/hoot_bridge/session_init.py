"""Hoot → Apollo handoff initialization.

1. End any existing active session for this student (stale handoffs don't block new ones).
2. Overseer infers concept cluster from Hoot transcript.
3. Overseer picks the first problem at 'intro' difficulty.
4. Session row created (phase=TEACHING), first ProblemAttempt row created.
5. Return {session_id, problem} to the frontend.

Raises NoMatchingConceptError or PoolExhaustedError — these are mapped
to 409s by the FastAPI exception handlers.
"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.concept_inference import infer_concept_cluster
from apollo.overseer.problem_selector import select_problem
from apollo.persistence.models import ApolloSession, ProblemAttempt, SessionPhase, SessionStatus

_AVAILABLE_CLUSTERS = ["fluid_mechanics"]
_DEFAULT_FIRST_DIFFICULTY = "intro"


async def init_session_from_hoot(
    *,
    db: AsyncSession,
    student_id: str,
    hoot_transcript: str,
) -> Dict[str, Any]:
    cluster_id = infer_concept_cluster(
        transcript=hoot_transcript,
        available_clusters=_AVAILABLE_CLUSTERS,
    )

    problem = select_problem(
        cluster_id=cluster_id,
        difficulty=_DEFAULT_FIRST_DIFFICULTY,
        attempted_ids=[],
    )

    await db.execute(
        update(ApolloSession)
        .where(
            ApolloSession.student_id == student_id,
            ApolloSession.status == SessionStatus.active.value,
        )
        .values(status=SessionStatus.ended.value)
    )
    await db.flush()

    session = ApolloSession(
        student_id=student_id,
        concept_cluster_id=cluster_id,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id=problem.id,
    )
    db.add(session)
    await db.flush()

    attempt = ProblemAttempt(
        session_id=session.id,
        problem_id=problem.id,
        difficulty=_DEFAULT_FIRST_DIFFICULTY,
    )
    db.add(attempt)
    await db.commit()

    return {
        "session_id": session.id,
        "problem": {
            "id": problem.id,
            "concept_id": problem.concept_id,
            "difficulty": problem.difficulty,
            "problem_text": problem.problem_text,
            "given_values": problem.given_values,
            "target_unknown": problem.target_unknown,
        },
    }
