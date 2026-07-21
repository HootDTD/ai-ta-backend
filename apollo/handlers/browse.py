"""Student browse surface: list a course's teachable problems for one concept.

Read-only. The eligibility predicate is the selector's (tier-2 +
non-quarantined, via list_problems_for_concept); the course-scope check is
the same candidate set session entry uses (list_course_concepts), so a
concept_id from another course 409s instead of leaking cross-course problems.

Student-safety invariant: the response carries ONLY {id, difficulty,
problem_text, attempted} — never reference_solution / given_values /
target_unknown."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import NoMatchingConceptError
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.persistence.models import ProblemAttempt
from apollo.subjects.curriculum_db import list_course_concepts


async def handle_list_problems(
    db: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    concept_id: int,
    difficulty: str | None = None,
) -> dict[str, Any]:
    candidates = await list_course_concepts(db, search_space_id=search_space_id)
    if concept_id not in {c.concept_id for c in candidates}:
        raise NoMatchingConceptError(
            f"concept_id={concept_id} is not teachable in course {search_space_id}"
        )

    pool = await list_problems_for_concept(
        db, concept_id=concept_id, search_space_id=search_space_id
    )
    if difficulty is not None:
        pool = [p for p in pool if p.difficulty == difficulty]

    attempted_ids = set(
        (
            await db.execute(
                select(ProblemAttempt.problem_id)
                .where(
                    ProblemAttempt.user_id == user_id,
                    ProblemAttempt.course_id == search_space_id,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    return {
        "problems": [
            {
                "id": p.id,
                "difficulty": p.difficulty,
                "problem_text": p.problem_text,
                "attempted": p.database_id in attempted_ids,
            }
            for p in pool
        ]
    }
