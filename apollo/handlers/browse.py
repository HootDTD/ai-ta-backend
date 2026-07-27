"""Student browse surface: list a course's teachable problems for one concept.

Read-only. The eligibility predicate is the selector's (tier-2 +
non-quarantined, via list_problems_for_concept); the course-scope check is
the same candidate set session entry uses (list_course_concepts), so a
concept_id from another course 409s instead of leaking cross-course problems.

Student-safety invariant: the response carries ONLY {id, difficulty,
problem_text, attempted, grade} — never reference_solution / given_values /
target_unknown. `grade` is the student's OWN best served overall
({score, letter, feedback}) across their graded attempts on that problem, or
null. `feedback` is the narrative that attempt already served to this student
at Done-time — never rubric/coverage internals."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import NoMatchingConceptError
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.persistence.models import ProblemAttempt
from apollo.subjects.curriculum_db import list_course_concepts


def served_overall_from_report(report: Any) -> dict[str, Any] | None:
    """Extract the student-facing overall grade from a diagnostic_report.

    Prefers the `served_overall` snapshot the Done path persists (exactly what
    the report panel showed — topic score when it computed); falls back to the
    legacy `rubric.overall` for attempts graded before the snapshot existed.
    Returns None when neither yields a usable score+letter, so a malformed
    legacy row degrades to the plain "Tried" state instead of failing browse.
    """
    if not isinstance(report, dict):
        return None
    overall = report.get("served_overall") or (report.get("rubric") or {}).get("overall")
    if not isinstance(overall, dict):
        return None
    score = overall.get("score")
    letter = overall.get("letter")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not isinstance(letter, str):
        return None
    return {"score": score, "letter": letter}


def feedback_from_report(report: Any) -> str | None:
    """Extract the student-facing feedback text from a diagnostic_report.

    `narrative` is exactly what the Done report panel served this student (for
    topic-score attempts, the flattened structured feedback). Anything but a
    non-empty string degrades to None — the grade chip then renders without a
    feedback panel instead of failing browse or showing an empty box.
    """
    if not isinstance(report, dict):
        return None
    narrative = report.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        return None
    return narrative


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

    attempt_rows = (
        await db.execute(
            select(
                ProblemAttempt.problem_id,
                ProblemAttempt.result,
                ProblemAttempt.diagnostic_report,
            )
            .where(
                ProblemAttempt.user_id == user_id,
                ProblemAttempt.course_id == search_space_id,
            )
            # Ascending so the >= keep-rule below prefers the LATEST attempt
            # among equal best scores (freshest letter wins ties).
            .order_by(ProblemAttempt.created_at.asc(), ProblemAttempt.id.asc())
        )
    ).all()

    attempted_ids = {row.problem_id for row in attempt_rows}

    best_grades: dict[int, dict[str, Any]] = {}
    for row in attempt_rows:
        # Narrow "graded" literal on purpose (same contract as the progress
        # dashboard): only the Done path writes both result="graded" AND a
        # rubric-shaped diagnostic_report.
        if row.result != "graded":
            continue
        grade = served_overall_from_report(row.diagnostic_report)
        if grade is None:
            continue
        best = best_grades.get(row.problem_id)
        if best is None or grade["score"] >= best["score"]:
            # Feedback rides with the SAME attempt whose grade is displayed —
            # never a different attempt's text.
            best_grades[row.problem_id] = {
                **grade,
                "feedback": feedback_from_report(row.diagnostic_report),
            }

    return {
        "problems": [
            {
                "id": p.id,
                "difficulty": p.difficulty,
                "problem_text": p.problem_text,
                "attempted": p.database_id in attempted_ids,
                "grade": best_grades.get(p.database_id),
            }
            for p in pool
        ]
    }
