"""Teacher classroom performance projection — the aggregation behind
``GET /apollo/teacher/classroom/{search_space_id}/performance``.

PURE READ-SIDE aggregation over already-durable rows (``app.problem_attempts``
/ ``app.course_memberships`` / ``app.student_progress`` / ``app.problems`` /
``app.concepts``, plus — through ``performance_insights`` — the canonical
grading artifacts in ``internal.grading_runs``) — no new grading, no inference,
no LLM/Neo4j calls.

Unlike ``classroom.mastery_heatmap`` (which reads ``app.learner_state``, empty
until ``APOLLO_GRAPH_SIM_LAYER3_ENABLED`` is flipped), everything here reads
the SERVED grade snapshot the live grading path already persists on every
graded attempt: ``diagnostic_report -> 'served_overall'`` (topic score, grade
of record — see ``apollo/overseer/topic-score``) with
``diagnostic_report -> 'rubric' -> 'overall'`` as the pre-snapshot fallback.
"Best-attempt-wins" per (student, problem) matches the student-facing browse
semantics, so teacher and student always see the same grade.

Student identity (email / full name) lives in Supabase-managed ``auth.users``,
which is OUTSIDE ``Base.metadata`` (same convention as the ORM's FK-less
``user_id`` columns) and absent from the Testcontainers test schema — so it is
resolved in a separate, failure-isolated query that degrades to no identities
rather than voiding the aggregation.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.rubric import score_to_letter
from apollo.projections import performance_insights, performance_problems

logger = logging.getLogger(__name__)

# SQL expression for the served score/letter of one graded attempt:
# served_overall (topic score, grade of record) with rubric.overall fallback
# for attempts graded before the served_overall snapshot existed.
_SCORE_EXPR = """
    coalesce(
        (pa.diagnostic_report #>> '{served_overall,score}')::float,
        (pa.diagnostic_report #>> '{rubric,overall,score}')::float
    )
"""
_LETTER_EXPR = """
    coalesce(
        pa.diagnostic_report #>> '{served_overall,letter}',
        pa.diagnostic_report #>> '{rubric,overall,letter}'
    )
"""

# The rubric axes persisted under diagnostic_report -> 'rubric' by the Done-time
# grading path (apollo/overseer/rubric.compute_rubric) that the teacher panel
# surfaces as the "where does the class lose points" signal. The fourth axis the
# grader also writes (misconception_corrected) is deliberately NOT surfaced — v2
# dropped it as noise (design spec 2026-07-30 §2).
_RUBRIC_DIMENSIONS = ("procedure", "justification", "simplification")


def _round1(value: Any) -> float:
    return round(float(value), 1)


async def _best_graded_rows(db: AsyncSession, *, search_space_id: int) -> list[dict[str, Any]]:
    """One row per (user_id, problem_id): the highest-scoring graded attempt,
    carrying that attempt's own served letter (never re-derived, so a teacher
    cell always matches what the student was shown), that attempt's stored
    ``coverage``, and the node ids that attempt's own grade EXCLUDED
    (``unprobed_node_ids``, P1.2b — absent on rows graded before it). Only those
    sub-objects of ``diagnostic_report``, not the whole report: together they are
    exactly what the per-problem node drill-down needs and nothing else."""
    rows = (
        (
            await db.execute(
                text(
                    f"""
                SELECT DISTINCT ON (pa.user_id, pa.problem_id)
                    pa.user_id AS user_id,
                    pa.problem_id AS problem_id,
                    {_SCORE_EXPR} AS score,
                    {_LETTER_EXPR} AS letter,
                    pa.diagnostic_report #> '{{coverage}}' AS coverage,
                    pa.diagnostic_report #> '{{unprobed_node_ids}}' AS unprobed_node_ids
                FROM app.problem_attempts pa
                WHERE pa.course_id = :search_space_id
                  AND pa.result = 'graded'
                  AND {_SCORE_EXPR} IS NOT NULL
                ORDER BY pa.user_id, pa.problem_id, {_SCORE_EXPR} DESC, pa.id DESC
                """
                ),
                {"search_space_id": search_space_id},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "user_id": str(row["user_id"]),
            "problem_id": int(row["problem_id"]),
            "score": _round1(row["score"]),
            "letter": row["letter"] or score_to_letter(round(float(row["score"]))),
            "coverage": row["coverage"],
            "unprobed_node_ids": row["unprobed_node_ids"],
        }
        for row in rows
    ]


async def _attempt_totals(db: AsyncSession, *, search_space_id: int) -> list[dict[str, Any]]:
    """Per-student attempt bookkeeping: totals, graded count, distinct
    problems tried, and last activity timestamp."""
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT
                    pa.user_id AS user_id,
                    count(*) AS attempts,
                    count(*) FILTER (WHERE pa.result = 'graded') AS graded,
                    count(DISTINCT pa.problem_id) AS problems_tried,
                    max(pa.created_at) AS last_active
                FROM app.problem_attempts pa
                WHERE pa.course_id = :search_space_id
                GROUP BY pa.user_id
                """
                ),
                {"search_space_id": search_space_id},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "user_id": str(row["user_id"]),
            "attempts": int(row["attempts"]),
            "graded": int(row["graded"]),
            "problems_tried": int(row["problems_tried"]),
            "last_active": row["last_active"].isoformat() if row["last_active"] else None,
        }
        for row in rows
    ]


async def _activity_by_day(db: AsyncSession, *, search_space_id: int) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT
                    (pa.created_at AT TIME ZONE 'UTC')::date AS day,
                    count(*) FILTER (WHERE pa.result = 'graded') AS graded,
                    count(*) FILTER (WHERE pa.result IS DISTINCT FROM 'graded') AS other
                FROM app.problem_attempts pa
                WHERE pa.course_id = :search_space_id
                GROUP BY 1
                ORDER BY 1
                """
                ),
                {"search_space_id": search_space_id},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "day": row["day"].isoformat(),
            "graded": int(row["graded"]),
            "in_progress": int(row["other"]),
        }
        for row in rows
    ]


async def _rubric_averages(db: AsyncSession, *, search_space_id: int) -> dict[str, float | None]:
    """Mean per rubric axis over ALL graded attempts (retries included — this
    is the 'where does the class lose points' signal, not the served grade)."""
    selects = ",\n".join(
        f"""avg((pa.diagnostic_report #>> '{{rubric,{dim},score}}')::float) AS {dim}"""
        for dim in _RUBRIC_DIMENSIONS
    )
    row = (
        (
            await db.execute(
                text(
                    f"""
                SELECT {selects}
                FROM app.problem_attempts pa
                WHERE pa.course_id = :search_space_id
                  AND pa.result = 'graded'
                  AND pa.diagnostic_report ? 'rubric'
                """
                ),
                {"search_space_id": search_space_id},
            )
        )
        .mappings()
        .one()
    )
    return {
        dim: (_round1(row[dim]) if row[dim] is not None else None) for dim in _RUBRIC_DIMENSIONS
    }


async def _concept_rollup(db: AsyncSession, *, search_space_id: int) -> list[dict[str, Any]]:
    """Per-concept attempt counts + best-wins average, joined through
    ``app.problems`` (an attempt's concept is its problem's concept)."""
    rows = (
        (
            await db.execute(
                text(
                    f"""
                WITH best AS (
                    SELECT DISTINCT ON (pa.user_id, pa.problem_id)
                        pa.problem_id AS problem_id,
                        {_SCORE_EXPR} AS score
                    FROM app.problem_attempts pa
                    WHERE pa.course_id = :search_space_id
                      AND pa.result = 'graded'
                      AND {_SCORE_EXPR} IS NOT NULL
                    ORDER BY pa.user_id, pa.problem_id, {_SCORE_EXPR} DESC, pa.id DESC
                )
                SELECT
                    c.id AS concept_id,
                    c.display_name AS display_name,
                    count(pa.id) AS attempts,
                    count(pa.id) FILTER (WHERE pa.result = 'graded') AS graded,
                    (SELECT count(*) FROM best b
                       JOIN app.problems bp ON bp.id = b.problem_id
                      WHERE bp.concept_id = c.id) AS problems_graded,
                    (SELECT avg(b.score) FROM best b
                       JOIN app.problems bp ON bp.id = b.problem_id
                      WHERE bp.concept_id = c.id) AS avg_best
                FROM app.problem_attempts pa
                JOIN app.problems p ON p.id = pa.problem_id
                JOIN app.concepts c ON c.id = p.concept_id
                WHERE pa.course_id = :search_space_id
                GROUP BY c.id, c.display_name
                ORDER BY attempts DESC, c.display_name ASC
                """
                ),
                {"search_space_id": search_space_id},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "concept_id": int(row["concept_id"]),
            "display_name": row["display_name"],
            "attempts": int(row["attempts"]),
            "graded": int(row["graded"]),
            "problems_graded": int(row["problems_graded"]),
            "avg_best": _round1(row["avg_best"]) if row["avg_best"] is not None else None,
        }
        for row in rows
    ]


async def _roster_counts(db: AsyncSession, *, search_space_id: int) -> dict[str, int]:
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT m.role AS role, count(*) AS n
                FROM app.course_memberships m
                WHERE m.course_id = :search_space_id
                GROUP BY m.role
                """
                ),
                {"search_space_id": search_space_id},
            )
        )
        .mappings()
        .all()
    )
    counts = {row["role"]: int(row["n"]) for row in rows}
    return {"students": counts.get("student", 0), "teachers": counts.get("teacher", 0)}


async def _progress_rows(db: AsyncSession, *, search_space_id: int) -> dict[str, str | None]:
    """``user_id -> last-updated ISO timestamp`` for every student with a
    progress row. v2 no longer surfaces xp/level, but this set still drives the
    ``signed_in_only`` derivation (a progress row with no attempts) and supplies
    those students' ``last_active``."""
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT sp.user_id AS user_id, sp.updated_at AS updated_at
                FROM app.student_progress sp
                WHERE sp.course_id = :search_space_id
                """
                ),
                {"search_space_id": search_space_id},
            )
        )
        .mappings()
        .all()
    )
    return {
        str(row["user_id"]): (row["updated_at"].isoformat() if row["updated_at"] else None)
        for row in rows
    }


async def _identities(db: AsyncSession, user_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve emails / display names from Supabase-managed ``auth.users``.

    Failure-isolated by design: ``auth.users`` is outside ``Base.metadata``
    (absent in the test schema, present in every Supabase deployment), so a
    missing table or revoked grant degrades to an empty mapping — the panel
    then shows shortened user ids instead of failing the whole endpoint.
    """
    if not user_ids:
        return {}
    try:
        # begin_nested = SAVEPOINT: a missing table / revoked grant rolls back
        # only this lookup, leaving the outer transaction (and, in tests, the
        # seeded rows) intact.
        async with db.begin_nested():
            rows = (
                (
                    await db.execute(
                        text(
                            """
                        SELECT u.id AS user_id, u.email AS email,
                               u.raw_user_meta_data ->> 'full_name' AS full_name
                        FROM auth.users u
                        WHERE u.id::text = ANY(:user_ids)
                        """
                        ),
                        {"user_ids": user_ids},
                    )
                )
                .mappings()
                .all()
            )
    except DBAPIError as exc:
        logger.warning("class_performance: auth.users identity lookup unavailable: %s", exc)
        return {}
    return {
        str(row["user_id"]): {"email": row["email"], "full_name": row["full_name"]} for row in rows
    }


def _student_row(
    user_id: str,
    *,
    email: str | None,
    full_name: str | None,
    attempts: int,
    graded: int,
    problems_tried: int,
    best_grades: list[dict[str, Any]],
    avg_best: float | None,
    last_active: str | None,
    engagement_core: dict[str, Any] | None,
    aggs: list[performance_insights.ProblemAgg],
) -> dict[str, Any]:
    """One student row incl. the algorithmic ``engagement`` block + ``flags``
    (composed by ``performance_insights.student_extras`` — a signed-in-only
    student with no messages/attempts still gets ``not_started``)."""
    return {
        "user_id": user_id,
        "email": email,
        "full_name": full_name,
        "attempts": attempts,
        "graded": graded,
        "problems_tried": problems_tried,
        "best_grades": best_grades,
        "avg_best": avg_best,
        "letter": score_to_letter(round(avg_best)) if avg_best is not None else None,
        "last_active": last_active,
        **performance_insights.student_extras(
            attempts=attempts, engagement_core=engagement_core, aggs=aggs
        ),
    }


async def class_performance(db: AsyncSession, *, search_space_id: int) -> dict[str, Any]:
    """Assemble the full teacher classroom-performance payload for a course.

    Every list is roster-bounded (a course is tens of students, not thousands);
    nothing here is a cross-course export.
    """
    best_rows = await _best_graded_rows(db, search_space_id=search_space_id)
    totals = await _attempt_totals(db, search_space_id=search_space_id)
    roster = await _roster_counts(db, search_space_id=search_space_id)
    progress = await _progress_rows(db, search_space_id=search_space_id)
    engagement = await performance_insights.load_engagement(db, search_space_id=search_space_id)
    # P3.2 teacher surface: the `repeated_misconception` attention flag. Read as
    # its own side map off the canonical artifact rows and threaded into the
    # aggregates — an empty set (nothing persisted, or everything still marked
    # `shadow` below wrongness level 3) reproduces today's five flags exactly.
    aggregates = await performance_insights.load_problem_aggregates(
        db,
        search_space_id=search_space_id,
        score_expr=_SCORE_EXPR,
        repeated_pairs=await performance_insights.load_repeated_misconception_pairs(
            db, search_space_id=search_space_id
        ),
    )

    active_ids = {row["user_id"] for row in totals}
    signed_in_only = [uid for uid in progress if uid not in active_ids]
    identities = await _identities(db, sorted(active_ids | set(signed_in_only)))

    best_by_user: dict[str, list[dict[str, Any]]] = {}
    for row in best_rows:
        best_by_user.setdefault(row["user_id"], []).append(
            {"problem_id": row["problem_id"], "score": row["score"], "letter": row["letter"]}
        )

    students: list[dict[str, Any]] = []
    for row in totals:
        uid = row["user_id"]
        best = sorted(best_by_user.get(uid, []), key=lambda b: -b["score"])
        avg_best = _round1(sum(b["score"] for b in best) / len(best)) if best else None
        students.append(
            _student_row(
                uid,
                email=identities.get(uid, {}).get("email"),
                full_name=identities.get(uid, {}).get("full_name"),
                attempts=row["attempts"],
                graded=row["graded"],
                problems_tried=row["problems_tried"],
                best_grades=best,
                avg_best=avg_best,
                last_active=row["last_active"],
                engagement_core=engagement.get(uid),
                aggs=aggregates.get(uid, []),
            )
        )
    for uid in signed_in_only:
        students.append(
            _student_row(
                uid,
                email=identities.get(uid, {}).get("email"),
                full_name=identities.get(uid, {}).get("full_name"),
                attempts=0,
                graded=0,
                problems_tried=0,
                best_grades=[],
                avg_best=None,
                last_active=progress[uid],
                engagement_core=engagement.get(uid),
                aggs=aggregates.get(uid, []),
            )
        )
    students.sort(key=lambda s: (0 if s["avg_best"] is not None else 1, -(s["avg_best"] or 0.0)))

    graded_student_avgs = [s["avg_best"] for s in students if s["avg_best"] is not None]
    class_avg = (
        _round1(sum(graded_student_avgs) / len(graded_student_avgs))
        if graded_student_avgs
        else None
    )

    problem_ids = sorted({row["problem_id"] for row in best_rows})
    problems = performance_problems.build_problems(
        best_rows,
        await performance_problems.load_problem_meta(db, problem_ids=problem_ids),
        identities,
        await performance_problems.load_graded_reference_nodes(db, problem_ids=problem_ids),
        aggregates,
    )
    # Correlation / quartiles run only over students who have a served grade;
    # the point carries the same avg_best the row shows, the email label, and
    # user_id (the quartile tie-break key — grade must never order equal effort).
    graded_points = [
        {
            "turns": s["engagement"]["teaching_turns"],
            "avg_best": s["avg_best"],
            "email": s["email"],
            "user_id": s["user_id"],
        }
        for s in students
        if s["avg_best"] is not None
    ]
    insights = performance_insights.build_insights(graded_points, aggregates)

    return {
        "roster": roster,
        "totals": {
            "attempts": sum(row["attempts"] for row in totals),
            "graded": sum(row["graded"] for row in totals),
            "active_students": len(active_ids),
            "signed_in_only": len(signed_in_only),
        },
        "class_average": {
            "score": class_avg,
            "letter": score_to_letter(round(class_avg)) if class_avg is not None else None,
            "students_graded": len(graded_student_avgs),
        },
        "grade_distribution": performance_problems.letter_distribution(best_rows),
        "activity_by_day": await _activity_by_day(db, search_space_id=search_space_id),
        "rubric_averages": await _rubric_averages(db, search_space_id=search_space_id),
        "concepts": await _concept_rollup(db, search_space_id=search_space_id),
        "problems": problems,
        "students": students,
        "insights": insights,
    }
