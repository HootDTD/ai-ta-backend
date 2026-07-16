"""Campaign-plan Task A3 — ``write_artifacts`` unit tests (H1 savepoint
harness: real pgvector ``db_session``, rolled back per test, mirroring
``apollo/handlers/tests/test_learner_janitor.py``).

Builds a real ``ApolloSession``/``ProblemAttempt`` pair + a ``ShadowGradeResult``
fixture (same shape as ``apollo/grading/tests/test_artifact_build.py``'s
``shadow_fixture``) and drives ``write_artifacts`` directly — no HTTP, no Neo4j.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from apollo.conftest import TEST_USER_ID
from apollo.grading.artifact_build import GRADER_USED_GRAPH, GRADER_USED_LLM_FALLBACK
from apollo.grading.audited_grade import AuditedGrade
from apollo.graph_compare.core import COMPARISON_VERSION, GradeResult
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.handlers.artifact_writer import write_artifacts
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.persistence.models import (
    ApolloSession,
    Clarification,
    GradingArtifact,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.resolution.result import ResolutionResult, ResolvedNode
from apollo.subjects.tests._curriculum_fixtures import seed_search_space

pytestmark = pytest.mark.integration

_COVERAGE = {"covered": ["eq.a"], "missing": ["eq.b"]}
_RUBRIC = {"overall": {"score": 0.7, "letter": "B"}}


def _grade(findings: tuple[Finding, ...]) -> GradeResult:
    return GradeResult(
        coverage_score=0.5,
        soundness_score=0.5,
        bisimilarity_score=0.5,
        node_coverage_score=0.5,
        edge_coverage_score=0.5,
        scoping_score=1.0,
        usage_score=1.0,
        procedure_order_score=1.0,
        dependency_score=1.0,
        contradiction_score=1.0,
        comparison_confidence=1.0,
        findings=findings,
        comparison_version=COMPARISON_VERSION,
    )


def _findings() -> tuple[Finding, ...]:
    return (
        Finding(
            kind=FindingKind.COVERED_NODE,
            canonical_key="eq.a",
            student_node_ids=("n_a",),
            evidence_spans=("eq a is conserved",),
            confidence=0.92,
        ),
        Finding(kind=FindingKind.MISSING_NODE, canonical_key="eq.b", score=0.0),
    )


def _resolution() -> ResolutionResult:
    return ResolutionResult(
        resolved=(
            ResolvedNode(
                node_id="n_a",
                resolution="resolved",
                resolved_key="eq.a",
                resolved_canon_key=1,
                method="alias",
                confidence=0.92,
            ),
        ),
        tier_counts={"alias": 1},
        llm_calls=0,
    )


def _shadow() -> ShadowGradeResult:
    findings = _findings()
    grade = _grade(findings)
    audited = AuditedGrade(
        grade=grade,
        findings=findings,
        abstention_reasons=(),
        abstained=False,
        suppressed_event_kinds=frozenset(),
        alias_candidates=(),
    )
    return ShadowGradeResult(
        run_id=1,
        grade=grade,
        audited=audited,
        normalization_confidence=0.8,
        reference_graph_hash="refhash-v1:deadbeef",
        opposes_map={},
        turn_order={},
        graph_sim_rubric={},
        calibration=object(),  # type: ignore[arg-type]
        diagnostic=object(),  # type: ignore[arg-type]
        resolution=_resolution(),
    )


async def _seed_session_attempt(
    db, *, concept_id: int | None = None
) -> tuple[ApolloSession, ProblemAttempt]:
    sid = await seed_search_space(db)
    sess = ApolloSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=concept_id,
        status=SessionStatus.active.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id="p1",
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id, problem_id="p1", difficulty="intro", result="graded"
    )
    db.add(attempt)
    await db.flush()
    return sess, attempt


async def test_shadow_present_writes_canonical_graph_and_pair_llm(db_session):
    """A4 will one day pass ``served='graph'`` once the graph grade is
    promoted; ``write_artifacts`` must already assign roles generically."""
    sess, attempt = await _seed_session_attempt(db_session)

    result = await write_artifacts(
        db_session,
        attempt=attempt,
        sess=sess,
        shadow=_shadow(),
        coverage=_COVERAGE,
        rubric=_RUBRIC,
        served=GRADER_USED_GRAPH,
        graph_failure=None,
        latency_ms=123,
    )
    assert result is not None
    assert result["grader_used"] == GRADER_USED_GRAPH

    rows = (
        (
            await db_session.execute(
                select(GradingArtifact).where(GradingArtifact.attempt_id == attempt.id)
            )
        )
        .scalars()
        .all()
    )
    by_role = {r.role: r for r in rows}
    assert set(by_role) == {"canonical", "pair"}
    assert by_role["canonical"].grader_used == GRADER_USED_GRAPH
    assert by_role["pair"].grader_used == GRADER_USED_LLM_FALLBACK
    assert by_role["canonical"].grading_latency_ms == 123


async def test_shadow_none_writes_single_llm_canonical_row_with_graph_failure(db_session):
    sess, attempt = await _seed_session_attempt(db_session)

    await write_artifacts(
        db_session,
        attempt=attempt,
        sess=sess,
        shadow=None,
        coverage=_COVERAGE,
        rubric=_RUBRIC,
        served=GRADER_USED_LLM_FALLBACK,
        graph_failure="boom",
        latency_ms=45,
    )

    rows = (
        (
            await db_session.execute(
                select(GradingArtifact).where(GradingArtifact.attempt_id == attempt.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.role == "canonical"
    assert row.grader_used == GRADER_USED_LLM_FALLBACK
    assert row.abstention["graph_failure"] == "boom"


async def test_clarification_trace_included_when_rows_exist(db_session):
    sess, attempt = await _seed_session_attempt(db_session)
    db_session.add_all(
        [
            Clarification(
                attempt_id=attempt.id,
                session_id=sess.id,
                user_id=TEST_USER_ID,
                search_space_id=sess.search_space_id,
                concept_id=None,
                node_id="n_a",
                candidate_key="eq.a",
                state="confirmed",
                probe_question="Do you mean eq.a?",
                original_statement="something vague",
                clarification_text="yes, eq.a",
                asked_turn=1,
                answered_turn=2,
            ),
            Clarification(
                attempt_id=attempt.id,
                session_id=sess.id,
                user_id=TEST_USER_ID,
                search_space_id=sess.search_space_id,
                concept_id=None,
                node_id="n_b",
                candidate_key="misc.wrong",
                state="refuted",
                probe_question="Do you mean misc.wrong?",
                original_statement="something else vague",
                clarification_text="no, not that",
                asked_turn=3,
                answered_turn=4,
            ),
            Clarification(
                attempt_id=attempt.id,
                session_id=sess.id,
                user_id=TEST_USER_ID,
                search_space_id=sess.search_space_id,
                concept_id=None,
                node_id="n_c",
                candidate_key="eq.c",
                state="asked_waiting",
                probe_question="Do you mean eq.c?",
                original_statement="yet another vague statement",
                asked_turn=5,
            ),
        ]
    )
    await db_session.flush()

    await write_artifacts(
        db_session,
        attempt=attempt,
        sess=sess,
        shadow=_shadow(),
        coverage=_COVERAGE,
        rubric=_RUBRIC,
        served=GRADER_USED_LLM_FALLBACK,
        graph_failure=None,
        latency_ms=None,
    )

    row = (
        await db_session.execute(
            select(GradingArtifact).where(
                GradingArtifact.attempt_id == attempt.id,
                GradingArtifact.role == "pair",
            )
        )
    ).scalar_one()
    assert row.clarification_trace == [
        {
            "node_id": "n_a",
            "candidate_key": "eq.a",
            "probe_question": "Do you mean eq.a?",
            "original_statement": "something vague",
            "clarification_text": "yes, eq.a",
            "state": "confirmed",
            "credit": "granted",
        },
        {
            "node_id": "n_b",
            "candidate_key": "misc.wrong",
            "probe_question": "Do you mean misc.wrong?",
            "original_statement": "something else vague",
            "clarification_text": "no, not that",
            "state": "refuted",
            "credit": "denied",
        },
        {
            "node_id": "n_c",
            "candidate_key": "eq.c",
            "probe_question": "Do you mean eq.c?",
            "original_statement": "yet another vague statement",
            "clarification_text": None,
            "state": "asked_waiting",
            "credit": None,
        },
    ]


async def test_flush_error_is_swallowed_and_never_propagates(db_session):
    sess, attempt = await _seed_session_attempt(db_session)

    with patch.object(db_session, "flush", new=AsyncMock(side_effect=RuntimeError("db down"))):
        # Must not raise.
        result = await write_artifacts(
            db_session,
            attempt=attempt,
            sess=sess,
            shadow=None,
            coverage=_COVERAGE,
            rubric=_RUBRIC,
            served=GRADER_USED_LLM_FALLBACK,
            graph_failure=None,
            latency_ms=None,
        )
    assert result is None

    rows = (
        (
            await db_session.execute(
                select(GradingArtifact).where(GradingArtifact.attempt_id == attempt.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_served_grade_missing_falls_back_to_llm(db_session):
    """Defensive branch: ``served='graph'`` with no shadow computed falls back
    to the LLM payload rather than writing nothing."""
    sess, attempt = await _seed_session_attempt(db_session)

    await write_artifacts(
        db_session,
        attempt=attempt,
        sess=sess,
        shadow=None,
        coverage=_COVERAGE,
        rubric=_RUBRIC,
        served=GRADER_USED_GRAPH,
        graph_failure=None,
        latency_ms=None,
    )

    row = (
        await db_session.execute(
            select(GradingArtifact).where(GradingArtifact.attempt_id == attempt.id)
        )
    ).scalar_one()
    assert row.role == "canonical"
    assert row.grader_used == GRADER_USED_LLM_FALLBACK
