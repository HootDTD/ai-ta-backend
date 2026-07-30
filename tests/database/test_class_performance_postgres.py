"""Real-PG behavioral gate for ``apollo.projections.performance.class_performance``.

Seeds ``app.problem_attempts`` rows carrying the SERVED grade snapshot shapes
the Done-time grading path actually persists in ``diagnostic_report``
(``served_overall {score, letter}`` on current attempts, ``rubric.overall``
only on pre-snapshot attempts) plus roster / progress rows, and asserts the
full teacher payload: best-attempt-wins per (student, problem), the served
letter carried verbatim (never re-derived), the rubric-axis loss signal over
ALL graded attempts, day bucketing, concept rollup, roster/progress joins,
and the failure-isolated ``auth.users`` identity lookup (absent from this
test schema by design — Supabase-managed, outside ``Base.metadata``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from apollo.persistence.models import (
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    StudentProgress,
    TutoringSession,
)
from apollo.projections.performance import class_performance
from apollo.subjects.tests._curriculum_fixtures import (
    minimal_problem_payload,
    problem_database_id,
    seed_concept,
    seed_problems,
    seed_search_space,
)
from database.models import CourseMembership

pytestmark = pytest.mark.integration


def _report(
    *,
    served: tuple[float, str] | None = None,
    overall: tuple[float, str] | None = None,
    procedure: float = 0.0,
    justification: float = 0.0,
    simplification: float = 0.0,
    misconception: float = 0.0,
) -> dict:
    """A ``diagnostic_report`` in the persisted shape: ``served_overall`` when
    the topic-score snapshot exists, ``rubric.overall`` always."""
    report: dict = {
        "narrative": "n/a",
        "coverage": {},
        "rubric": {
            "overall": {"score": overall[0], "letter": overall[1]} if overall else None,
            "procedure": {"score": procedure},
            "justification": {"score": justification},
            "simplification": {"score": simplification},
            "misconception_corrected": {"score": misconception},
        },
    }
    if served is not None:
        report["served_overall"] = {"score": served[0], "letter": served[1]}
    return report


async def _seed_course_with_problems(db) -> tuple[int, int, int, int, int]:
    """Returns ``(sid, concept_a, concept_b, problem_a, problem_b)`` —
    problem_a under concept_a, problem_b under concept_b."""
    sid = await seed_search_space(db)
    concept_a = await seed_concept(
        db, search_space_id=sid, subject_slug=f"subj-{uuid.uuid4().hex[:8]}", concept_slug="ca"
    )
    concept_b = await seed_concept(
        db, search_space_id=sid, subject_slug=f"subj-{uuid.uuid4().hex[:8]}", concept_slug="cb"
    )
    await seed_problems(db, concept_id=concept_a, payloads=[minimal_problem_payload(code="pa")])
    await seed_problems(db, concept_id=concept_b, payloads=[minimal_problem_payload(code="pb")])
    problem_a = await problem_database_id(db, concept_id=concept_a, problem_code="pa")
    problem_b = await problem_database_id(db, concept_id=concept_b, problem_code="pb")
    return sid, concept_a, concept_b, problem_a, problem_b


async def _seed_session(db, *, user_id: str, search_space_id: int, concept_id: int) -> int:
    sess = TutoringSession(
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        status=SessionStatus.ended.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=1,
    )
    db.add(sess)
    await db.flush()
    return int(sess.id)


async def _seed_attempt(
    db,
    *,
    session_id: int,
    user_id: str,
    search_space_id: int,
    problem_id: int,
    result: str | None,
    report: dict | None = None,
    created_at: datetime | None = None,
) -> int:
    attempt = ProblemAttempt(
        session_id=session_id,
        problem_id=problem_id,
        difficulty="intro",
        result=result,
        user_id=user_id,
        course_id=search_space_id,
        diagnostic_report=report,
        created_at=created_at or datetime.now(UTC),
    )
    db.add(attempt)
    await db.flush()
    return int(attempt.id)


async def _seed_full_scenario(db):
    """Two attempting students + one signed-in-only student + roster rows."""
    sid, concept_a, concept_b, problem_a, problem_b = await _seed_course_with_problems(db)
    user_a, user_b, user_c = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    sess_a = await _seed_session(db, user_id=user_a, search_space_id=sid, concept_id=concept_a)
    sess_b = await _seed_session(db, user_id=user_b, search_space_id=sid, concept_id=concept_a)

    day1 = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    day2 = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

    # user_a, problem_a: graded twice — best-wins must pick 85/A- (served
    # snapshot), not the earlier 40/F, and never the rubric.overall 80/B+.
    await _seed_attempt(
        db,
        session_id=sess_a,
        user_id=user_a,
        search_space_id=sid,
        problem_id=problem_a,
        result="graded",
        report=_report(served=(40, "F"), overall=(35, "F"), procedure=40),
        created_at=day1,
    )
    await _seed_attempt(
        db,
        session_id=sess_a,
        user_id=user_a,
        search_space_id=sid,
        problem_id=problem_a,
        result="graded",
        report=_report(served=(85, "A-"), overall=(80, "B+"), procedure=100, justification=60),
        created_at=day2,
    )
    # user_a, problem_b: pre-snapshot attempt — rubric.overall is the fallback.
    await _seed_attempt(
        db,
        session_id=sess_a,
        user_id=user_a,
        search_space_id=sid,
        problem_id=problem_b,
        result="graded",
        report=_report(overall=(100, "A+"), procedure=100, justification=100, simplification=100),
        created_at=day2,
    )
    # user_a: an in-progress attempt (result NULL, no report yet).
    await _seed_attempt(
        db,
        session_id=sess_a,
        user_id=user_a,
        search_space_id=sid,
        problem_id=problem_b,
        result=None,
        created_at=day2,
    )
    # user_b: one in-progress attempt only — active, but ungraded.
    await _seed_attempt(
        db,
        session_id=sess_b,
        user_id=user_b,
        search_space_id=sid,
        problem_id=problem_a,
        result=None,
        created_at=day1,
    )

    db.add(StudentProgress(user_id=user_a, course_id=sid, xp_total=387, level=2))
    # user_c: signed in (progress row exists) but never attempted.
    db.add(StudentProgress(user_id=user_c, course_id=sid, xp_total=0, level=1))
    for uid in (user_a, user_b, user_c):
        db.add(CourseMembership(user_id=uid, course_id=sid, role="student"))
    db.add(CourseMembership(user_id=str(uuid.uuid4()), course_id=sid, role="teacher"))
    await db.flush()
    return sid, user_a, user_b, user_c, problem_a, problem_b, concept_a, concept_b


async def test_full_payload_aggregates(db_session):
    (
        sid,
        user_a,
        user_b,
        user_c,
        problem_a,
        problem_b,
        concept_a,
        concept_b,
    ) = await _seed_full_scenario(db_session)

    payload = await class_performance(db_session, search_space_id=sid)

    assert payload["roster"] == {"students": 3, "teachers": 1}
    assert payload["totals"] == {
        "attempts": 5,
        "graded": 3,
        "active_students": 2,
        "signed_in_only": 1,
    }

    # Class average: only user_a has graded problems — mean(85, 100) = 92.5.
    assert payload["class_average"] == {"score": 92.5, "letter": "A", "students_graded": 1}

    # Distribution covers every band; best-wins letters are the SERVED letters.
    dist = {row["letter"]: row["count"] for row in payload["grade_distribution"]}
    assert dist["A-"] == 1 and dist["A+"] == 1
    assert sum(dist.values()) == 2
    assert "F" in dist and dist["F"] == 0

    days = {row["day"]: row for row in payload["activity_by_day"]}
    assert days["2026-07-26"] == {"day": "2026-07-26", "graded": 1, "in_progress": 1}
    assert days["2026-07-27"] == {"day": "2026-07-27", "graded": 2, "in_progress": 1}

    # Rubric loss signal averages ALL graded attempts: (40+100+100)/3 etc.
    assert payload["rubric_averages"]["procedure"] == 80.0
    assert payload["rubric_averages"]["justification"] == pytest.approx(53.3, abs=0.05)
    assert payload["rubric_averages"]["simplification"] == pytest.approx(33.3, abs=0.05)
    assert payload["rubric_averages"]["misconception_corrected"] == 0.0

    concepts = {row["concept_id"]: row for row in payload["concepts"]}
    assert concepts[concept_a]["attempts"] == 3  # user_a graded x2 + user_b in-progress
    assert concepts[concept_a]["graded"] == 2
    assert concepts[concept_a]["problems_graded"] == 1
    assert concepts[concept_a]["avg_best"] == 85.0
    assert concepts[concept_b]["attempts"] == 2
    assert concepts[concept_b]["graded"] == 1
    assert concepts[concept_b]["problems_graded"] == 1
    assert concepts[concept_b]["avg_best"] == 100.0

    students = {s["user_id"]: s for s in payload["students"]}
    a = students[user_a]
    assert (a["attempts"], a["graded"], a["problems_tried"]) == (4, 3, 2)
    assert a["avg_best"] == 92.5
    assert a["letter"] == "A"
    assert {(b["problem_id"], b["score"], b["letter"]) for b in a["best_grades"]} == {
        (problem_a, 85.0, "A-"),
        (problem_b, 100.0, "A+"),
    }
    assert (a["xp"], a["level"]) == (387, 2)
    assert a["last_active"] is not None

    b = students[user_b]
    assert (b["attempts"], b["graded"], b["avg_best"], b["letter"]) == (1, 0, None, None)
    assert (b["xp"], b["level"]) == (0, 1)  # no progress row -> defaults

    c = students[user_c]
    assert (c["attempts"], c["problems_tried"], c["avg_best"]) == (0, 0, None)
    assert (c["xp"], c["level"]) == (0, 1)

    # Graded-first, best-score-first ordering.
    assert payload["students"][0]["user_id"] == user_a

    # auth.users does not exist in this schema -> identity degrades to None.
    assert a["email"] is None and a["full_name"] is None


async def test_identity_lookup_joins_auth_users_when_present(db_session):
    sid, user_a, *_ = await _seed_full_scenario(db_session)

    # Stand up the Supabase-managed table shape inside the rolled-back test
    # transaction — proving the happy path without a real auth service.
    await db_session.execute(text("CREATE SCHEMA IF NOT EXISTS auth"))
    await db_session.execute(
        text("CREATE TABLE auth.users (id uuid PRIMARY KEY, email text, raw_user_meta_data jsonb)")
    )
    await db_session.execute(
        text(
            "INSERT INTO auth.users (id, email, raw_user_meta_data) "
            "VALUES (CAST(:uid AS uuid), :email, CAST(:meta AS jsonb))"
        ),
        {"uid": user_a, "email": "student@example.edu", "meta": '{"full_name": "Ada Student"}'},
    )

    payload = await class_performance(db_session, search_space_id=sid)
    students = {s["user_id"]: s for s in payload["students"]}
    assert students[user_a]["email"] == "student@example.edu"
    assert students[user_a]["full_name"] == "Ada Student"


async def test_empty_course_returns_zeroed_payload(db_session):
    sid = await seed_search_space(db_session)

    payload = await class_performance(db_session, search_space_id=sid)

    assert payload["roster"] == {"students": 0, "teachers": 0}
    assert payload["totals"] == {
        "attempts": 0,
        "graded": 0,
        "active_students": 0,
        "signed_in_only": 0,
    }
    assert payload["class_average"] == {"score": None, "letter": None, "students_graded": 0}
    assert all(row["count"] == 0 for row in payload["grade_distribution"])
    assert payload["activity_by_day"] == []
    assert payload["rubric_averages"] == {
        "procedure": None,
        "justification": None,
        "simplification": None,
        "misconception_corrected": None,
    }
    assert payload["concepts"] == []
    assert payload["students"] == []
