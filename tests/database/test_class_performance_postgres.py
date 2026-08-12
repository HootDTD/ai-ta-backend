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
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from apollo.persistence.models import (
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    StudentProgress,
    TutoringMessage,
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


async def _seed_messages(
    db, *, session_id: int, search_space_id: int, turns: list[tuple[str, str]]
) -> None:
    """Seed ``turns`` = ``[(role, content), ...]`` onto one session."""
    for turn_index, (role, content) in enumerate(turns):
        db.add(
            TutoringMessage(
                session_id=session_id,
                course_id=search_space_id,
                role=role,
                content=content,
                turn_index=turn_index,
            )
        )
    await db.flush()


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

    # user_a teaching turns: two student messages (5 + 2 words -> median 3.5)
    # plus an apollo message that MUST be excluded from the student-turn count.
    await _seed_messages(
        db,
        session_id=sess_a,
        search_space_id=sid,
        turns=[
            ("student", "one two three four five"),
            ("apollo", "Can you say more about why?"),
            ("student", "hello world"),
        ],
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
    # v2 drops the misconception_corrected axis entirely.
    assert set(payload["rubric_averages"]) == {"procedure", "justification", "simplification"}
    assert payload["rubric_averages"]["procedure"] == 80.0
    assert payload["rubric_averages"]["justification"] == pytest.approx(53.3, abs=0.05)
    assert payload["rubric_averages"]["simplification"] == pytest.approx(33.3, abs=0.05)

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
    assert a["last_active"] is not None
    assert "xp" not in a and "level" not in a  # v2 removed the XP stat
    # user_a taught two student turns (5 + 2 words -> median 3.5); the apollo
    # message is excluded. problem_a was retried (40 -> 85), gain 45.
    assert a["engagement"] == {
        "teaching_turns": 2,
        "median_words": 3.5,
        "problems_retried": 1,
        "avg_gain": 45.0,
    }
    assert a["flags"] == []  # 2 turns < low_effort floor; best grades clear 60

    b = students[user_b]
    assert (b["attempts"], b["graded"], b["avg_best"], b["letter"]) == (1, 0, None, None)
    assert b["engagement"] == {
        "teaching_turns": 0,
        "median_words": None,
        "problems_retried": 0,
        "avg_gain": None,
    }
    assert b["flags"] == []  # active (1 attempt), no messages, no graded problems

    c = students[user_c]
    assert (c["attempts"], c["problems_tried"], c["avg_best"]) == (0, 0, None)
    assert c["flags"] == ["not_started"]  # signed in, never attempted

    # Graded-first, best-score-first ordering.
    assert payload["students"][0]["user_id"] == user_a

    # Per-problem best-wins rollup, grouped/ordered by concept_name then code.
    problems = payload["problems"]
    assert [(p["problem_id"], p["concept_name"], p["problem_code"]) for p in problems] == [
        (problem_a, "Ca", "pa"),
        (problem_b, "Cb", "pb"),
    ]
    pa_block = problems[0]
    assert (pa_block["students_graded"], pa_block["avg_best"]) == (1, 85.0)
    pa_dist = {row["letter"]: row["count"] for row in pa_block["distribution"]}
    assert pa_dist["A-"] == 1 and pa_dist["F"] == 0

    # P3.3: the per-problem student row carries the PAIR's attempt count and
    # median gap (user_a's two graded attempts on problem_a, 86400 s apart).
    assert pa_block["students"][0]["attempts"] == 2
    assert pa_block["students"][0]["median_gap_seconds"] == 86400.0

    # Insights: only user_a is graded (n=1) -> correlation & quartiles suppressed;
    # but user_a retried problem_a, so retry_payoff is populated.
    assert payload["insights"]["correlation"] is None
    assert payload["insights"]["effort_quartiles"] is None
    assert payload["insights"]["retry_payoff"] == {
        "students_retried": 1,
        "avg_first": 40.0,
        "avg_best": 85.0,
        "avg_gain": 45.0,
    }

    # P3.3: the loader's `pa.created_at` column drives per-pair spacing.
    # user_a's two graded attempts on problem_a are a day apart (86400 s), so
    # the retry is timed but is NOT a rapid flip.
    assert payload["insights"]["retry_timing"] == {
        "pairs_retried": 1,
        "median_gap_seconds": 86400.0,
        "min_gap_seconds": 86400.0,
        "rapid_flips": 0,
    }

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


async def test_insights_populated_with_enough_students(db_session):
    """>= MIN_CORRELATION_N graded students -> correlation + effort_quartiles
    materialize end-to-end (loaders + assembler), and a retry populates
    retry_payoff. The exact statistics are anchored by the pure unit tests; here
    we prove the real-DB chain wires them together."""
    sid, concept_a, _concept_b, problem_a, _problem_b = await _seed_course_with_problems(db_session)
    day = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)

    users = [str(uuid.uuid4()) for _ in range(8)]
    for i, uid in enumerate(users):
        sess = await _seed_session(
            db_session, user_id=uid, search_space_id=sid, concept_id=concept_a
        )
        await _seed_attempt(
            db_session,
            session_id=sess,
            user_id=uid,
            search_space_id=sid,
            problem_id=problem_a,
            result="graded",
            report=_report(served=(30 + i * 8, "C")),
            created_at=day,
        )
        # teaching effort rises with the student index (i+1 student turns).
        await _seed_messages(
            db_session,
            session_id=sess,
            search_space_id=sid,
            turns=[("student", "a b c") for _ in range(i + 1)],
        )
        db_session.add(CourseMembership(user_id=uid, course_id=sid, role="student"))

    # user 0 retries problem_a (30 -> 66) so retry_payoff is populated.
    sess0 = await _seed_session(
        db_session, user_id=users[0], search_space_id=sid, concept_id=concept_a
    )
    await _seed_attempt(
        db_session,
        session_id=sess0,
        user_id=users[0],
        search_space_id=sid,
        problem_id=problem_a,
        result="graded",
        report=_report(served=(66, "C+")),
        created_at=day,
    )
    await db_session.flush()

    payload = await class_performance(db_session, search_space_id=sid)

    corr = payload["insights"]["correlation"]
    assert corr is not None
    assert corr["n"] == 8
    assert -1.0 <= corr["pearson_r"] <= 1.0
    assert len(corr["points"]) == 8

    quartiles = payload["insights"]["effort_quartiles"]
    assert quartiles is not None
    assert [q["quartile"] for q in quartiles] == [1, 2, 3, 4]
    assert sum(q["students"] for q in quartiles) == 8

    assert payload["insights"]["retry_payoff"]["students_retried"] == 1

    problem_row = next(p for p in payload["problems"] if p["problem_id"] == problem_a)
    assert problem_row["students_graded"] == 8


async def test_gave_up_flag_respects_still_ungraded_retry(db_session):
    """``gave_up`` must not fire while a student is mid-retry. The scoring path is
    graded-only, but ``best_is_last`` is decided over ALL attempts, so a later
    still-ungraded attempt after a sub-60 best clears the flag — whereas a
    student who never attempted again is flagged."""
    sid, concept_a, _cb, problem_a, _pb = await _seed_course_with_problems(db_session)
    quitter, retrier = str(uuid.uuid4()), str(uuid.uuid4())
    day = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)

    # quitter: best graded 45 (< 60) on problem_a and nothing after -> gave_up.
    sq = await _seed_session(db_session, user_id=quitter, search_space_id=sid, concept_id=concept_a)
    await _seed_attempt(
        db_session,
        session_id=sq,
        user_id=quitter,
        search_space_id=sid,
        problem_id=problem_a,
        result="graded",
        report=_report(served=(45, "F")),
        created_at=day,
    )

    # retrier: same sub-60 best, THEN a still-ungraded attempt (result NULL,
    # higher id) -> best is no longer last -> NOT gave_up.
    sr = await _seed_session(db_session, user_id=retrier, search_space_id=sid, concept_id=concept_a)
    await _seed_attempt(
        db_session,
        session_id=sr,
        user_id=retrier,
        search_space_id=sid,
        problem_id=problem_a,
        result="graded",
        report=_report(served=(45, "F")),
        created_at=day,
    )
    await _seed_attempt(
        db_session,
        session_id=sr,
        user_id=retrier,
        search_space_id=sid,
        problem_id=problem_a,
        result=None,
        created_at=day,
    )

    for uid in (quitter, retrier):
        db_session.add(CourseMembership(user_id=uid, course_id=sid, role="student"))
    await db_session.flush()

    payload = await class_performance(db_session, search_space_id=sid)
    students = {s["user_id"]: s for s in payload["students"]}
    assert "gave_up" in students[quitter]["flags"]
    assert "gave_up" not in students[retrier]["flags"]


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
    }
    assert payload["concepts"] == []
    assert payload["problems"] == []
    assert payload["students"] == []
    assert payload["insights"] == {
        "correlation": None,
        "effort_quartiles": None,
        "retry_payoff": None,
        "retry_timing": None,
    }


# --- v2.1 per-problem drill-down (problem_text / students / node breakdown) ---

# _credit_for_node verdicts (the SAME helper the served topic score uses):
#   covered → per_step[node]=="covered" AND node in procedure_scores
#   partial → node in procedure_scores, NOT covered, in per_step, credit>0
#   missing → node NOT in procedure_scores (and not covered)
_COV_COVERED = {
    "per_step": {"eq1": "covered", "c1": "covered"},
    "procedure_scores": {"eq1": 1.0, "c1": 1.0},
}
_COV_PARTIAL = {
    "per_step": {"eq1": "missing", "c1": "covered"},
    "procedure_scores": {"eq1": 0.5, "c1": 1.0},
}
_COV_MISSED = {"per_step": {"c1": "covered"}, "procedure_scores": {"c1": 1.0}}

_GRADED_PROBLEM_TEXT = "Full statement: derive the outlet pressure using Bernoulli."


def _graded_problem_payload(code: str = "pg") -> dict:
    """A problem whose reference solution carries GRADED node types (equation +
    condition) so ``to_kg_graph`` yields a non-empty graded-node set — unlike the
    definition-only ``minimal_problem_payload``."""
    return {
        "id": code,
        "concept_id": "test_concept",
        "difficulty": "intro",
        "problem_text": _GRADED_PROBLEM_TEXT,
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "eq1",
                "content": {"symbolic": "P + rho*v = c", "label": "Bernoulli"},
            },
            {
                "step": 2,
                "entry_type": "condition",
                "id": "c1",
                "content": {"applies_when": "steady, incompressible", "label": "Steady"},
            },
        ],
    }


async def test_problem_drilldown_text_students_and_node_breakdown(db_session):
    """The v2.1 ``problems[]`` enrichment over real PG: full ``problem_text``, the
    per-problem best-wins ``students`` list, and the per-reference-node
    understood/partial/missed counts derived by REUSING the served topic score's
    own ``_credit_for_node`` over the stored ``diagnostic_report -> 'coverage'``.
    Also proves the ``#> '{coverage}'`` JSONB read decodes to a dict end-to-end."""
    sid = await seed_search_space(db_session)
    concept = await seed_concept(
        db_session,
        search_space_id=sid,
        subject_slug=f"subj-{uuid.uuid4().hex[:8]}",
        concept_slug="cg",
    )
    await seed_problems(
        db_session, concept_id=concept, payloads=[_graded_problem_payload(code="pg")]
    )
    problem = await problem_database_id(db_session, concept_id=concept, problem_code="pg")
    day = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)

    # Four graded students, one best attempt each. u4's coverage is EMPTY (a
    # pre-topic-score snapshot shape) → excluded from node tallies but still a
    # graded student in the roster + distribution.
    plan = [
        ("u1", (100, "A+"), _COV_COVERED),
        ("u2", (60, "C"), _COV_PARTIAL),
        ("u3", (40, "F"), _COV_MISSED),
        ("u4", (30, "F"), {}),
    ]
    users = {}
    for label, served, coverage in plan:
        uid = str(uuid.uuid4())
        users[label] = uid
        sess = await _seed_session(db_session, user_id=uid, search_space_id=sid, concept_id=concept)
        report = _report(served=served, overall=served)
        report["coverage"] = coverage
        await _seed_attempt(
            db_session,
            session_id=sess,
            user_id=uid,
            search_space_id=sid,
            problem_id=problem,
            result="graded",
            report=report,
            created_at=day,
        )
        db_session.add(CourseMembership(user_id=uid, course_id=sid, role="student"))
    await db_session.flush()

    payload = await class_performance(db_session, search_space_id=sid)
    block = next(p for p in payload["problems"] if p["problem_id"] == problem)

    # Full problem text is carried verbatim.
    assert block["problem_text"] == _GRADED_PROBLEM_TEXT
    assert block["students_graded"] == 4

    # Per-problem best-wins student list, ordered by score desc; email degrades to
    # None (auth.users absent from this test schema).
    assert [(s["score"], s["letter"]) for s in block["students"]] == [
        (100.0, "A+"),
        (60.0, "C"),
        (40.0, "F"),
        (30.0, "F"),
    ]
    assert {s["user_id"] for s in block["students"]} == set(users.values())
    assert all(s["email"] is None for s in block["students"])

    # Node breakdown: u4's empty coverage is excluded, so graded=3; eq1 spreads
    # across covered/partial/missing, c1 covered for all three.
    nodes = {n["node_id"]: n for n in block["nodes"]}
    assert set(nodes) == {"eq1", "c1"}
    assert nodes["eq1"] == {
        "node_id": "eq1",
        "display_name": "Bernoulli",
        "node_type": "equation",
        "understood": 1,
        "partial": 1,
        "missed": 1,
        "unprobed": 0,
        "graded": 3,
    }
    assert (nodes["c1"]["display_name"], nodes["c1"]["node_type"]) == ("Steady", "condition")
    assert (nodes["c1"]["understood"], nodes["c1"]["partial"], nodes["c1"]["missed"]) == (3, 0, 0)
    assert nodes["c1"]["graded"] == 3


async def test_rapid_retry_timing_and_flag_from_real_created_at(db_session):
    """P3.3 end-to-end over real PG: a 42-second reword that jumped F -> A-
    surfaces as `rapid_retry` on the student row and a `rapid_flips` tally in
    `insights.retry_timing`. This is the assertion that covers the added
    `pa.created_at` SELECT column (the loader is integration-only)."""
    sid, concept_a, _cb, problem_a, _pb = await _seed_course_with_problems(db_session)
    user = str(uuid.uuid4())
    sess = await _seed_session(db_session, user_id=user, search_space_id=sid, concept_id=concept_a)
    first_at = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)

    await _seed_attempt(
        db_session,
        session_id=sess,
        user_id=user,
        search_space_id=sid,
        problem_id=problem_a,
        result="graded",
        report=_report(served=(40, "F")),
        created_at=first_at,
    )
    await _seed_attempt(
        db_session,
        session_id=sess,
        user_id=user,
        search_space_id=sid,
        problem_id=problem_a,
        result="graded",
        report=_report(served=(85, "A-")),
        created_at=first_at + timedelta(seconds=42),
    )
    db_session.add(CourseMembership(user_id=user, course_id=sid, role="student"))
    await db_session.flush()

    payload = await class_performance(db_session, search_space_id=sid)

    assert payload["insights"]["retry_timing"] == {
        "pairs_retried": 1,
        "median_gap_seconds": 42.0,
        "min_gap_seconds": 42.0,
        "rapid_flips": 1,
    }
    student = next(s for s in payload["students"] if s["user_id"] == user)
    assert student["flags"] == ["rapid_retry"]
    # Best-wins is unchanged by timing: the higher-scoring later attempt wins,
    # and its SERVED letter is carried verbatim.
    assert (student["avg_best"], student["best_grades"][0]["letter"]) == (85.0, "A-")

    block = next(p for p in payload["problems"] if p["problem_id"] == problem_a)
    assert block["students"] == [
        {
            "user_id": user,
            "email": None,
            "score": 85.0,
            "letter": "A-",
            "attempts": 2,
            "median_gap_seconds": 42.0,
        }
    ]
