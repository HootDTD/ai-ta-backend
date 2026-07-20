"""GET /apollo/progress — course-scoped XP, level, and detail.

The required search_space_id selects the course-specific StudentProgress row.
The response includes that identifier so clients cannot confuse progress from
two enrolled courses.

Detail block (search_space_id given): per-concept mastery averaged over
apollo_learner_state (entity → concept via apollo_kg_entities.concept_id) and
the 10 most recent GRADED attempts read from ProblemAttempt.diagnostic_report
(always written on grade — no dependency on APOLLO_GRADING_ARTIFACT_ENABLED)."""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.xp import next_tier_threshold, title_for_level
from apollo.persistence.models import (
    Concept,
    KGEntity,
    LearnerState,
    ProblemAttempt,
    TutoringSession,
)
from apollo.persistence.progress_repo import load_progress

RECENT_ATTEMPTS_LIMIT = 10


async def handle_get_progress(
    *,
    db: AsyncSession,
    user_id: str,
    search_space_id: int,
) -> Dict[str, Any]:
    row = await load_progress(
        db=db, user_id=user_id, course_id=search_space_id
    )
    return {
        "user_id": row.user_id,
        "search_space_id": row.course_id,
        "xp_total": row.xp_total,
        "level": row.level,
        "title": title_for_level(row.level),
        "next_tier_threshold": next_tier_threshold(row.level),
    }


async def handle_get_progress_detail(
    *,
    db: AsyncSession,
    user_id: str,
    search_space_id: int,
) -> Dict[str, Any]:
    base = await handle_get_progress(
        db=db, user_id=user_id, search_space_id=search_space_id
    )

    mastery_rows = (
        await db.execute(
            select(
                KGEntity.concept_id,
                Concept.display_name,
                func.avg(LearnerState.mastery),
                func.count(LearnerState.entity_id),
            )
            .join(KGEntity, LearnerState.entity_id == KGEntity.id)
            .join(Concept, KGEntity.concept_id == Concept.id)
            .where(
                LearnerState.user_id == user_id,
                LearnerState.search_space_id == search_space_id,
            )
            .group_by(KGEntity.concept_id, Concept.display_name)
            .order_by(Concept.display_name)
        )
    ).all()

    attempt_rows = (
        await db.execute(
            select(ProblemAttempt, TutoringSession.concept_id, Concept.display_name)
            .join(TutoringSession, ProblemAttempt.session_id == TutoringSession.id)
            .outerjoin(Concept, TutoringSession.concept_id == Concept.id)
            .where(
                TutoringSession.user_id == user_id,
                TutoringSession.search_space_id == search_space_id,
                ProblemAttempt.user_id == user_id,
                ProblemAttempt.course_id == search_space_id,
                # Deliberately the narrow "graded" literal: only the Done path
                # path writes both result="graded" AND the {"rubric": {"overall":
                # ...}} shape this block reads. Do NOT widen to
                # GRADED_ATTEMPT_RESULTS or null-score legacy rows pollute the
                # dashboard.
                ProblemAttempt.result == "graded",
            )
            .order_by(ProblemAttempt.created_at.desc())
            .limit(RECENT_ATTEMPTS_LIMIT)
        )
    ).all()

    recent = []
    for attempt, concept_id, display_name in attempt_rows:
        report = attempt.diagnostic_report or {}
        overall = (report.get("rubric") or {}).get("overall") or {}
        recent.append(
            {
                "attempt_id": attempt.id,
                "problem_id": attempt.problem_id,
                "concept_id": concept_id,
                "concept_display_name": display_name,
                "difficulty": attempt.difficulty,
                "score": overall.get("score"),
                "letter": overall.get("letter"),
                "created_at": attempt.created_at.isoformat(),
            }
        )

    base["detail"] = {
        "mastery": [
            {
                "concept_id": concept_id,
                "display_name": display_name,
                "mastery_avg": round(float(avg), 3),
                "entity_count": int(count),
            }
            for concept_id, display_name, avg, count in mastery_rows
        ],
        "recent_attempts": recent,
    }
    return base
