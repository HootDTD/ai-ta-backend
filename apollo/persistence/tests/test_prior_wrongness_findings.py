"""`prior_wrongness_findings` — the cross-attempt read (Apollo P3.2 S9 / L2c).

REAL Postgres only: the query is a JSONB `LATERAL jsonb_array_elements` over
`internal.grading_runs`, which SQLite cannot execute at all. The rows are seeded
through the ORM in the shape `artifact_writer.write_artifacts` +
`artifact_build.build_llm_artifact` actually produce, exactly like
`tests/database/test_classroom_projection_postgres.py` does — these tests
exercise the SELECT, not the writers.

Scope is `(same user, same problem, same course, EARLIER attempts)`. Getting any
leg of that wrong is a privacy defect (another student's claim quoted back) or a
groundhog-day defect (re-asking about the attempt in progress).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from apollo.persistence.attempt_history import prior_wrongness_findings
from apollo.persistence.models import (
    GradingRun,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringSession,
)
from apollo.subjects.tests._curriculum_fixtures import (
    minimal_problem_payload,
    problem_database_id,
    seed_concept,
    seed_problems,
    seed_search_space,
)


async def _seed_problem(db: Any, *, search_space_id: int) -> tuple[int, int]:
    """Returns `(problem_id, concept_id)` — `GradingRun.problem_id` is a NOT NULL
    FK to `app.problems.id`."""
    concept_id = await seed_concept(
        db,
        search_space_id=search_space_id,
        subject_slug=f"subj-{uuid.uuid4().hex[:8]}",
        concept_slug=f"concept-{uuid.uuid4().hex[:8]}",
    )
    code = f"p-{uuid.uuid4().hex[:8]}"
    await seed_problems(db, concept_id=concept_id, payloads=[minimal_problem_payload(code=code)])
    return await problem_database_id(db, concept_id=concept_id, problem_code=code), concept_id


async def _seed_attempt(
    db: Any, *, search_space_id: int, concept_id: int, problem_id: int, user_id: str
) -> int:
    sess = TutoringSession(
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        status=SessionStatus.ended.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=problem_id,
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id,
        problem_id=problem_id,
        difficulty="intro",
        result="graded",
        user_id=user_id,
        course_id=sess.course_id,
    )
    db.add(attempt)
    await db.flush()
    return int(attempt.id)


def _artifact(
    *,
    attempt_id: int,
    user_id: str,
    search_space_id: int,
    concept_id: int,
    problem_id: int,
    misconceptions: Any,
    role: str = "canonical",
    created_at: datetime | None = None,
) -> GradingRun:
    return GradingRun(
        attempt_id=attempt_id,
        role=role,
        grader_used="llm_fallback",
        grader_version="v1",
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        problem_id=problem_id,
        version_details={"grader": "v1"},
        node_ledger=[],
        edge_ledger=[],
        score_details={"composite": 0.9},
        composite_score=0.9,
        abstained=False,
        grader_payload={"misconceptions": misconceptions, "clarification_trace": []},
        created_at=created_at or datetime.now(UTC),
    )


def _finding(key: str, *, span: str = "quote", resolved: bool = False) -> dict[str, Any]:
    return {
        "canonical_key": key,
        "resolved": resolved,
        "evidence_span": span,
        "kind": "wrong-condition",
    }


@pytest.mark.integration
async def test_returns_only_same_user_same_problem(db_session) -> None:
    sid = await seed_search_space(db_session)
    problem_id, concept_id = await _seed_problem(db_session, search_space_id=sid)
    other_problem_id, other_concept_id = await _seed_problem(db_session, search_space_id=sid)
    mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())

    common = {"search_space_id": sid, "concept_id": concept_id, "problem_id": problem_id}
    prior = await _seed_attempt(db_session, user_id=mine, **common)
    current = await _seed_attempt(db_session, user_id=mine, **common)
    their_attempt = await _seed_attempt(db_session, user_id=theirs, **common)
    other_problem_attempt = await _seed_attempt(
        db_session,
        search_space_id=sid,
        concept_id=other_concept_id,
        problem_id=other_problem_id,
        user_id=mine,
    )

    db_session.add_all(
        [
            _artifact(
                attempt_id=prior,
                user_id=mine,
                misconceptions=[_finding("eq.momentum", span="energy has to be conserved too")],
                **common,
            ),
            _artifact(
                attempt_id=their_attempt,
                user_id=theirs,
                misconceptions=[_finding("eq.other-student")],
                **common,
            ),
            _artifact(
                attempt_id=other_problem_attempt,
                user_id=mine,
                search_space_id=sid,
                concept_id=other_concept_id,
                problem_id=other_problem_id,
                misconceptions=[_finding("eq.other-problem")],
            ),
        ]
    )
    await db_session.commit()

    found = await prior_wrongness_findings(
        db_session, attempt_id=current, problem_id=problem_id, course_id=sid
    )

    assert [row["canonical_key"] for row in found] == ["eq.momentum"]
    assert found[0]["evidence_span"] == "energy has to be conserved too"
    assert found[0]["resolved"] is False
    assert found[0]["attempt_id"] == prior


@pytest.mark.integration
async def test_excludes_the_current_attempt(db_session) -> None:
    """The consequence is always earned INSIDE the current attempt — a finding
    this very attempt just recorded must never be carried back into it."""
    sid = await seed_search_space(db_session)
    problem_id, concept_id = await _seed_problem(db_session, search_space_id=sid)
    user_id = str(uuid.uuid4())
    common = {"search_space_id": sid, "concept_id": concept_id, "problem_id": problem_id}
    current = await _seed_attempt(db_session, user_id=user_id, **common)

    db_session.add(
        _artifact(
            attempt_id=current, user_id=user_id, misconceptions=[_finding("eq.self")], **common
        )
    )
    await db_session.commit()

    assert (
        await prior_wrongness_findings(
            db_session, attempt_id=current, problem_id=problem_id, course_id=sid
        )
        == ()
    )


@pytest.mark.integration
async def test_newest_first_and_limited(db_session) -> None:
    sid = await seed_search_space(db_session)
    problem_id, concept_id = await _seed_problem(db_session, search_space_id=sid)
    user_id = str(uuid.uuid4())
    common = {"search_space_id": sid, "concept_id": concept_id, "problem_id": problem_id}
    current = await _seed_attempt(db_session, user_id=user_id, **common)

    base = datetime.now(UTC)
    for index in range(3):
        attempt_id = await _seed_attempt(db_session, user_id=user_id, **common)
        db_session.add(
            _artifact(
                attempt_id=attempt_id,
                user_id=user_id,
                misconceptions=[_finding(f"eq.{index}", resolved=index == 0)],
                created_at=base - timedelta(hours=index),
                **common,
            )
        )
    await db_session.commit()

    found = await prior_wrongness_findings(
        db_session, attempt_id=current, problem_id=problem_id, course_id=sid
    )
    assert [row["canonical_key"] for row in found] == ["eq.0", "eq.1", "eq.2"]
    assert [row["resolved"] for row in found] == [True, False, False]

    capped = await prior_wrongness_findings(
        db_session, attempt_id=current, problem_id=problem_id, course_id=sid, limit=1
    )
    assert [row["canonical_key"] for row in capped] == ["eq.0"]


@pytest.mark.integration
async def test_empty_when_no_prior_findings(db_session) -> None:
    """Three ways to have nothing to carry: no prior attempt at all, a prior
    artifact with an empty array, and a shadow (`role='pair'`) row — the served
    grade is the only record of what the student was told."""
    sid = await seed_search_space(db_session)
    problem_id, concept_id = await _seed_problem(db_session, search_space_id=sid)
    user_id = str(uuid.uuid4())
    common = {"search_space_id": sid, "concept_id": concept_id, "problem_id": problem_id}
    current = await _seed_attempt(db_session, user_id=user_id, **common)
    empty_attempt = await _seed_attempt(db_session, user_id=user_id, **common)
    shadow_attempt = await _seed_attempt(db_session, user_id=user_id, **common)

    db_session.add_all(
        [
            _artifact(attempt_id=empty_attempt, user_id=user_id, misconceptions=[], **common),
            _artifact(
                attempt_id=shadow_attempt,
                user_id=user_id,
                misconceptions=[_finding("eq.shadow")],
                role="pair",
                **common,
            ),
        ]
    )
    await db_session.commit()

    assert (
        await prior_wrongness_findings(
            db_session, attempt_id=current, problem_id=problem_id, course_id=sid
        )
        == ()
    )


@pytest.mark.integration
async def test_non_array_payload_yields_no_rows_instead_of_raising(db_session) -> None:
    """`grader_payload` is free-form JSONB. An object (or a missing key) where an
    array is expected must return nothing, not blow up mid-Done with
    "cannot extract elements from an object"."""
    sid = await seed_search_space(db_session)
    problem_id, concept_id = await _seed_problem(db_session, search_space_id=sid)
    user_id = str(uuid.uuid4())
    common = {"search_space_id": sid, "concept_id": concept_id, "problem_id": problem_id}
    current = await _seed_attempt(db_session, user_id=user_id, **common)
    object_attempt = await _seed_attempt(db_session, user_id=user_id, **common)
    absent_attempt = await _seed_attempt(db_session, user_id=user_id, **common)

    absent = _artifact(attempt_id=absent_attempt, user_id=user_id, misconceptions=[], **common)
    absent.grader_payload = {"clarification_trace": []}
    db_session.add_all(
        [
            _artifact(
                attempt_id=object_attempt,
                user_id=user_id,
                misconceptions={"eq.momentum": True},
                **common,
            ),
            absent,
        ]
    )
    await db_session.commit()

    assert (
        await prior_wrongness_findings(
            db_session, attempt_id=current, problem_id=problem_id, course_id=sid
        )
        == ()
    )


@pytest.mark.integration
async def test_entry_without_a_canonical_key_is_skipped(db_session) -> None:
    sid = await seed_search_space(db_session)
    problem_id, concept_id = await _seed_problem(db_session, search_space_id=sid)
    user_id = str(uuid.uuid4())
    common = {"search_space_id": sid, "concept_id": concept_id, "problem_id": problem_id}
    current = await _seed_attempt(db_session, user_id=user_id, **common)
    prior = await _seed_attempt(db_session, user_id=user_id, **common)

    db_session.add(
        _artifact(
            attempt_id=prior,
            user_id=user_id,
            misconceptions=[{"resolved": False}, {"canonical_key": "eq.kept", "resolved": False}],
            **common,
        )
    )
    await db_session.commit()

    found = await prior_wrongness_findings(
        db_session, attempt_id=current, problem_id=problem_id, course_id=sid
    )
    assert [row["canonical_key"] for row in found] == ["eq.kept"]
    assert found[0]["evidence_span"] is None


async def test_soft_fails_to_empty_on_error(caplog: pytest.LogCaptureFixture) -> None:
    """Its own failure domain: a missing memory costs one continuity question,
    while a raise here would reach the Done grade path."""

    class _BrokenSession:
        async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("connection reset")

    with caplog.at_level("WARNING"):
        found = await prior_wrongness_findings(
            _BrokenSession(), attempt_id=1, problem_id=2, course_id=3
        )

    assert found == ()
    assert "apollo_prior_findings_failed" in caplog.text
