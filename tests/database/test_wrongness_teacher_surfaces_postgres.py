"""Apollo P3.2 W3-B — the teacher surfaces' LEVEL MATRIX, against real Postgres.

The wrongness misconception array is persisted from level 1 so the level-2
carried challenge and the XP dedup have a record to read, but S10 puts every
teacher surface on rung 3. The discriminator is the `shadow` marker
`done._shadow_misconceptions` writes below level 3; the two teacher readers
exclude it, and the S9 cross-attempt read must NOT.

That is a claim about SQL — `IS DISTINCT FROM` against a `->>` projection of a
free-form JSONB column — so it is asserted here on the real engine, not against
a Python stand-in. Both readers are driven at levels 0/1/2/3 over rows the REAL
producer wrote, so a marker written one way and read another fails here.

Structural/pure coverage: `apollo/projections/tests/test_misconception_surfaces_light_up.py`
and `apollo/handlers/tests/test_done_shadow_marker.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from apollo.handlers import done
from apollo.overseer import wrongness
from apollo.persistence.attempt_history import prior_wrongness_findings
from apollo.persistence.models import (
    GradingRun,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringSession,
)
from apollo.projections.classroom import struggle_signals
from apollo.projections.performance_insights import load_repeated_misconception_pairs
from apollo.subjects.tests._curriculum_fixtures import (
    minimal_problem_payload,
    problem_database_id,
    seed_concept,
    seed_problems,
    seed_search_space,
)

pytestmark = pytest.mark.integration

_NODE = "eq.bernoulli"


def _finding(node_id: str = _NODE, *, resolved: bool = False) -> Any:
    return wrongness.WrongnessFinding(
        node_id=node_id,
        quote="pressure rises when speed rises",
        contradicts="p + q = c",
        kind="opposite_direction",
        corroborated=not resolved,
        resolved=resolved,
        apollo_elicited=resolved,
        would_ceiling=False,
    )


async def _seed_course_and_problem(db) -> tuple[int, int, int]:
    """``(search_space_id, concept_id, problem_id)`` — one real problem row,
    because ``GradingRun.problem_id`` is a NOT NULL FK to ``app.problems.id``."""
    sid = await seed_search_space(db)
    cid = await seed_concept(
        db, search_space_id=sid, subject_slug=f"subj-{uuid.uuid4().hex[:8]}", concept_slug="c1"
    )
    code = f"p-{uuid.uuid4().hex[:8]}"
    await seed_problems(db, concept_id=cid, payloads=[minimal_problem_payload(code=code)])
    problem_id = await problem_database_id(db, concept_id=cid, problem_code=code)
    return sid, cid, problem_id


async def _seed_graded_attempt(db, *, sid: int, cid: int, problem_id: int, user_id: str) -> int:
    sess = TutoringSession(
        user_id=user_id,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.ended.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=1,
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id,
        problem_id=problem_id,
        difficulty="intro",
        result="graded",
        user_id=user_id,
        course_id=sid,
        diagnostic_report={"served_overall": {"score": 70.0, "letter": "B-"}},
    )
    db.add(attempt)
    await db.flush()
    return int(attempt.id)


def _artifact(
    *, attempt_id: int, sid: int, cid: int, problem_id: int, user_id: str, misconceptions: Any
) -> GradingRun:
    return GradingRun(
        attempt_id=attempt_id,
        role="canonical",
        grader_used="llm_fallback",
        grader_version="v1",
        user_id=user_id,
        search_space_id=sid,
        concept_id=cid,
        problem_id=problem_id,
        version_details={"grader": "v1"},
        node_ledger=[],
        edge_ledger=[],
        score_details={"composite": 0.7},
        composite_score=0.7,
        abstained=False,
        grader_payload={"misconceptions": misconceptions or [], "clarification_trace": []},
        grading_latency_ms=None,
    )


async def _seed_two_attempts_at_level(
    db, *, level: int, resolved: bool = False
) -> tuple[int, str, int, list[int]]:
    """One student, one problem, TWO graded attempts, both persisting the same
    finding through the REAL producer at ``level``. Returns
    ``(search_space_id, user_id, problem_id, attempt_ids)``."""
    sid, cid, problem_id = await _seed_course_and_problem(db)
    user_id = str(uuid.uuid4())
    persisted = done._shadow_misconceptions([_finding(resolved=resolved)], level=level)
    attempt_ids: list[int] = []
    for _ in range(2):
        attempt_id = await _seed_graded_attempt(
            db, sid=sid, cid=cid, problem_id=problem_id, user_id=user_id
        )
        attempt_ids.append(attempt_id)
        db.add(
            _artifact(
                attempt_id=attempt_id,
                sid=sid,
                cid=cid,
                problem_id=problem_id,
                user_id=user_id,
                # `None` at level 0 is exactly what `artifact_writer` turns into
                # the payload's own (empty) array.
                misconceptions=persisted if persisted is not None else [],
            )
        )
    await db.flush()
    await db.commit()
    return sid, user_id, problem_id, attempt_ids


# --- the level matrix -------------------------------------------------------


@pytest.mark.parametrize("level", [0, 1, 2])
async def test_teacher_surfaces_stay_dark_below_level_3(db_session, level: int):
    """Levels 0-2: the classroom aggregate counts nothing and no student is
    flagged, even though levels 1-2 DID write rows to the column."""
    sid, user_id, problem_id, _ids = await _seed_two_attempts_at_level(db_session, level=level)

    assert (await struggle_signals(db_session, search_space_id=sid))["top_misconceptions"] == []
    assert await load_repeated_misconception_pairs(db_session, search_space_id=sid) == set()


@pytest.mark.parametrize("level", [3, 4])
async def test_teacher_surfaces_light_up_at_level_3(db_session, level: int):
    sid, user_id, problem_id, _ids = await _seed_two_attempts_at_level(db_session, level=level)

    assert (await struggle_signals(db_session, search_space_id=sid))["top_misconceptions"] == [
        {"key": _NODE, "count": 2}
    ]
    assert await load_repeated_misconception_pairs(db_session, search_space_id=sid) == {
        (user_id, problem_id)
    }


async def test_a_resolved_finding_is_never_a_teacher_signal(db_session):
    """At level 3 the persisted array is a SUPERSET of the served one: it also
    carries the `resolved AND apollo_elicited` rows the XP dedup subtracts. A
    contradiction the student FIXED is a success, not a struggle signal."""
    sid, user_id, problem_id, _ids = await _seed_two_attempts_at_level(
        db_session, level=3, resolved=True
    )

    assert (await struggle_signals(db_session, search_space_id=sid))["top_misconceptions"] == []
    assert await load_repeated_misconception_pairs(db_session, search_space_id=sid) == set()


async def test_one_uncorrected_attempt_is_not_a_repeat(db_session):
    """The flag needs the SAME key across two attempts; a single graded attempt
    carrying it is a misconception, not a repeated one."""
    sid, cid, problem_id = await _seed_course_and_problem(db_session)
    user_id = str(uuid.uuid4())
    attempt_id = await _seed_graded_attempt(
        db_session, sid=sid, cid=cid, problem_id=problem_id, user_id=user_id
    )
    db_session.add(
        _artifact(
            attempt_id=attempt_id,
            sid=sid,
            cid=cid,
            problem_id=problem_id,
            user_id=user_id,
            misconceptions=done._shadow_misconceptions([_finding()], level=3),
        )
    )
    await db_session.commit()

    assert (await struggle_signals(db_session, search_space_id=sid))["top_misconceptions"] == [
        {"key": _NODE, "count": 1}
    ]
    assert await load_repeated_misconception_pairs(db_session, search_space_id=sid) == set()


async def test_a_pre_p32_row_with_no_marker_keys_still_counts(db_session):
    """`IS DISTINCT FROM 'true'` and not `!= 'true'`: `->>` on an absent key is
    NULL, and `NULL != 'true'` is NULL — which would silently drop every artifact
    row written before P3.2 out of the teacher's count."""
    sid, cid, problem_id = await _seed_course_and_problem(db_session)
    user_id = str(uuid.uuid4())
    for _ in range(2):
        attempt_id = await _seed_graded_attempt(
            db_session, sid=sid, cid=cid, problem_id=problem_id, user_id=user_id
        )
        db_session.add(
            _artifact(
                attempt_id=attempt_id,
                sid=sid,
                cid=cid,
                problem_id=problem_id,
                user_id=user_id,
                misconceptions=[{"canonical_key": _NODE, "evidence_span": "legacy"}],
            )
        )
    await db_session.commit()

    assert (await struggle_signals(db_session, search_space_id=sid))["top_misconceptions"] == [
        {"key": _NODE, "count": 2}
    ]
    assert await load_repeated_misconception_pairs(db_session, search_space_id=sid) == {
        (user_id, problem_id)
    }


# --- the S9 read must NOT filter -------------------------------------------


async def test_the_s9_cross_attempt_read_returns_shadow_marked_entries(db_session):
    """The other side of the same marker. S9 powers the LEVEL-2 carried
    challenge, whose whole input is what levels 1-2 wrote — so the entries the
    teacher surfaces exclude are exactly the ones it must return. Verified on the
    real query, not by reading its text."""
    sid, user_id, problem_id, attempt_ids = await _seed_two_attempts_at_level(db_session, level=1)

    prior = await prior_wrongness_findings(
        db_session,
        attempt_id=attempt_ids[1],
        problem_id=problem_id,
        course_id=sid,
    )
    assert [row["canonical_key"] for row in prior] == [_NODE]
    assert prior[0]["resolved"] is False
    assert prior[0]["attempt_id"] == attempt_ids[0]
