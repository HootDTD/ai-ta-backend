"""WU-4B3 — REAL-PG gate: persist_comparison_run round-trip + supersede + FK.

Drives ``apollo.grading.persist_comparison_run`` against the real pgvector pg16
``db_session`` (``Base.metadata.create_all``, per-test savepoint rollback). The
``_seed_attempt`` helper builds the FK chain ``SearchSpace`` (ORM) ->
``ApolloSession`` -> ``ProblemAttempt`` and returns ``(attempt_id,
search_space_id)``; ``user_id`` is a free-form UUID (runs.user_id has no ORM FK
under ``create_all`` — auth.users is Supabase-managed). These tests MUST RUN
GREEN (not skip) with Docker up — a skip is a FAIL of the gate.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from apollo.grading.persistence import persist_comparison_run
from apollo.grading.tests._builders import (
    audited,
    missing_finding,
    missing_grade,
)
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.persistence.models import (
    ApolloSession,
    GraphComparisonFinding,
    GraphComparisonRun,
    ProblemAttempt,
)
from database.models import SearchSpace

pytestmark = pytest.mark.integration

_USER_ID = "22222222-2222-2222-2222-222222222222"
_REF_HASH = "refhash-v1:deadbeefcafe"


async def _seed_attempt(db) -> tuple[int, int]:
    """Create SearchSpace -> ApolloSession -> ProblemAttempt; return ids."""
    slug = f"course-{uuid.uuid4().hex[:8]}"
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    session = ApolloSession(user_id=_USER_ID, search_space_id=space.id)
    db.add(session)
    await db.flush()
    attempt = ProblemAttempt(session_id=session.id, problem_id="p1", difficulty="easy")
    db.add(attempt)
    await db.flush()
    return attempt.id, space.id


def _grade_with_findings():
    """A grade + audited grade carrying a rich finding set (covered + missing)
    with distinct scores so column-mapping is verifiable on read-back."""
    import dataclasses

    grade = missing_grade(("cond.missing_one",), covered=())
    findings = (
        Finding(
            kind=FindingKind.COVERED_NODE,
            canonical_key="cond.covered_one",
            student_node_ids=("s1", "s2"),
            evidence_spans=("the student said X",),
            score=0.9,
            confidence=0.92,
        ),
        missing_finding("cond.missing_one"),
    )
    grade = dataclasses.replace(
        grade,
        findings=findings,
        coverage_score=0.11,
        soundness_score=0.22,
        bisimilarity_score=0.33,
        node_coverage_score=0.44,
        edge_coverage_score=0.55,
        scoping_score=0.66,
        usage_score=0.77,
        procedure_order_score=0.88,
        dependency_score=0.91,
        contradiction_score=0.99,
    )
    return grade, audited(findings)


async def _count_findings_for_run(db, run_id: int) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(GraphComparisonFinding)
        .where(GraphComparisonFinding.run_id == run_id)
    )
    return result.scalar_one()


async def test_round_trip_all_columns(db_session):
    attempt_id, search_space_id = await _seed_attempt(db_session)
    grade, graded = _grade_with_findings()

    run_id = await persist_comparison_run(
        db_session,
        attempt_id=attempt_id,
        user_id=_USER_ID,
        search_space_id=search_space_id,
        grade=grade,
        audited=graded,
        normalization_confidence=0.83,
        reference_graph_hash=_REF_HASH,
    )

    run = (
        await db_session.execute(
            select(GraphComparisonRun).where(GraphComparisonRun.id == run_id)
        )
    ).scalar_one()

    assert run.attempt_id == attempt_id
    assert run.user_id == _USER_ID
    assert run.search_space_id == search_space_id
    assert run.coverage_score == 0.11
    assert run.soundness_score == 0.22
    assert run.bisimilarity_score == 0.33
    assert run.node_coverage_score == 0.44
    assert run.edge_coverage_score == 0.55
    assert run.scoping_score == 0.66
    assert run.usage_score == 0.77
    assert run.procedure_order_score == 0.88
    assert run.dependency_score == 0.91
    assert run.contradiction_score == 0.99
    assert run.normalization_confidence == 0.83
    assert run.abstained is False
    assert run.abstention_reasons == []
    assert run.comparison_version == grade.comparison_version
    assert run.reference_graph_hash == _REF_HASH

    findings = (
        (
            await db_session.execute(
                select(GraphComparisonFinding)
                .where(GraphComparisonFinding.run_id == run_id)
                .order_by(GraphComparisonFinding.finding_kind)
            )
        )
        .scalars()
        .all()
    )
    by_kind = {f.finding_kind: f for f in findings}
    assert set(by_kind) == {"covered_node", "missing_node"}

    covered = by_kind["covered_node"]
    assert covered.student_node_ids == ["s1", "s2"]
    assert covered.evidence_spans == ["the student said X"]
    assert covered.student_edge_ids == []
    assert covered.reference_edge_ids == []
    assert covered.score == 0.9
    assert covered.confidence == 0.92
    assert covered.entity_id is None

    missing = by_kind["missing_node"]
    assert missing.score == 0.0
    assert missing.confidence is None
    assert missing.message is None


async def test_abstention_reasons_jsonb_roundtrip(db_session):
    attempt_id, search_space_id = await _seed_attempt(db_session)
    grade = missing_grade(("k.a",))
    graded = audited(
        grade.findings,
    )
    # craft a multi-element abstention reasons tuple via a literal AuditedGrade.
    from apollo.grading.audited_grade import AuditedGrade

    graded = AuditedGrade(
        grade=grade,
        findings=grade.findings,
        abstention_reasons=(
            "unresolved_rate_above_threshold",
            "min_parser_confidence_below_threshold",
            "transcript_audit_unavailable",
        ),
        abstained=True,
        suppressed_event_kinds=frozenset({"missing"}),
        alias_candidates=(),
    )

    run_id = await persist_comparison_run(
        db_session,
        attempt_id=attempt_id,
        user_id=_USER_ID,
        search_space_id=search_space_id,
        grade=grade,
        audited=graded,
        normalization_confidence=1.0,
        reference_graph_hash=_REF_HASH,
    )
    run = (
        await db_session.execute(
            select(GraphComparisonRun).where(GraphComparisonRun.id == run_id)
        )
    ).scalar_one()
    assert run.abstention_reasons == [
        "unresolved_rate_above_threshold",
        "min_parser_confidence_below_threshold",
        "transcript_audit_unavailable",
    ]


async def test_nullable_subscores_persist_null(db_session):
    import dataclasses

    attempt_id, search_space_id = await _seed_attempt(db_session)
    grade, graded = _grade_with_findings()
    # NULL out a nullable sub-score and a finding's confidence.
    grade = dataclasses.replace(grade, node_coverage_score=None, scoping_score=None)

    run_id = await persist_comparison_run(
        db_session,
        attempt_id=attempt_id,
        user_id=_USER_ID,
        search_space_id=search_space_id,
        grade=grade,
        audited=graded,
        normalization_confidence=0.5,
        reference_graph_hash=_REF_HASH,
    )
    run = (
        await db_session.execute(
            select(GraphComparisonRun).where(GraphComparisonRun.id == run_id)
        )
    ).scalar_one()
    assert run.node_coverage_score is None
    assert run.scoping_score is None
    # the required top-line scores survived
    assert run.coverage_score == 0.11


async def test_supersede_deletes_prior_run_and_findings(db_session):
    attempt_id, search_space_id = await _seed_attempt(db_session)
    grade, graded = _grade_with_findings()

    run_id_a = await persist_comparison_run(
        db_session,
        attempt_id=attempt_id,
        user_id=_USER_ID,
        search_space_id=search_space_id,
        grade=grade,
        audited=graded,
        normalization_confidence=0.8,
        reference_graph_hash=_REF_HASH,
    )
    count_a = await _count_findings_for_run(db_session, run_id_a)
    assert count_a == 2

    # Re-run at the SAME (attempt_id, comparison_version) -> supersede.
    run_id_b = await persist_comparison_run(
        db_session,
        attempt_id=attempt_id,
        user_id=_USER_ID,
        search_space_id=search_space_id,
        grade=grade,
        audited=graded,
        normalization_confidence=0.8,
        reference_graph_hash=_REF_HASH,
    )

    assert run_id_b != run_id_a

    # Only ONE run row for this (attempt, version).
    total_runs = (
        await db_session.execute(
            select(func.count())
            .select_from(GraphComparisonRun)
            .where(
                GraphComparisonRun.attempt_id == attempt_id,
                GraphComparisonRun.comparison_version == grade.comparison_version,
            )
        )
    ).scalar_one()
    assert total_runs == 1

    # Run A's findings are gone (CASCADE), run B's present.
    assert await _count_findings_for_run(db_session, run_id_a) == 0
    assert await _count_findings_for_run(db_session, run_id_b) == 2


async def test_supersede_is_atomic_single_transaction(db_session):
    """After the second persist there are NO orphaned findings — every finding
    row belongs to the surviving run."""
    attempt_id, search_space_id = await _seed_attempt(db_session)
    grade, graded = _grade_with_findings()

    await persist_comparison_run(
        db_session,
        attempt_id=attempt_id,
        user_id=_USER_ID,
        search_space_id=search_space_id,
        grade=grade,
        audited=graded,
        normalization_confidence=0.8,
        reference_graph_hash=_REF_HASH,
    )
    run_id_b = await persist_comparison_run(
        db_session,
        attempt_id=attempt_id,
        user_id=_USER_ID,
        search_space_id=search_space_id,
        grade=grade,
        audited=graded,
        normalization_confidence=0.8,
        reference_graph_hash=_REF_HASH,
    )

    # Every finding for this attempt's runs joins to the surviving run only.
    orphan_count = (
        await db_session.execute(
            select(func.count())
            .select_from(GraphComparisonFinding)
            .join(
                GraphComparisonRun,
                GraphComparisonFinding.run_id == GraphComparisonRun.id,
            )
            .where(
                GraphComparisonRun.attempt_id == attempt_id,
                GraphComparisonFinding.run_id != run_id_b,
            )
        )
    ).scalar_one()
    assert orphan_count == 0


async def test_abstained_run_still_persists(db_session):
    """An abstained AuditedGrade persists a run (abstained=true) AND its findings
    (persist-ALWAYS, §6.4 step 15)."""
    attempt_id, search_space_id = await _seed_attempt(db_session)
    grade, _ = _grade_with_findings()
    from apollo.grading.audited_grade import AuditedGrade

    graded = AuditedGrade(
        grade=grade,
        findings=grade.findings,
        abstention_reasons=("unresolved_rate_above_threshold",),
        abstained=True,
        suppressed_event_kinds=frozenset(),
        alias_candidates=(),
    )

    run_id = await persist_comparison_run(
        db_session,
        attempt_id=attempt_id,
        user_id=_USER_ID,
        search_space_id=search_space_id,
        grade=grade,
        audited=graded,
        normalization_confidence=1.0,
        reference_graph_hash=_REF_HASH,
    )
    run = (
        await db_session.execute(
            select(GraphComparisonRun).where(GraphComparisonRun.id == run_id)
        )
    ).scalar_one()
    assert run.abstained is True
    assert await _count_findings_for_run(db_session, run_id) > 0


async def test_fk_integrity_real_rows(db_session):
    """A bogus attempt_id (no such row) raises IntegrityError on flush (FK
    enforced); wrapped in a savepoint so the surrounding session stays usable."""
    _real_attempt, search_space_id = await _seed_attempt(db_session)
    grade, graded = _grade_with_findings()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await persist_comparison_run(
                db_session,
                attempt_id=999_999_999,  # no such ProblemAttempt
                user_id=_USER_ID,
                search_space_id=search_space_id,
                grade=grade,
                audited=graded,
                normalization_confidence=1.0,
                reference_graph_hash=_REF_HASH,
            )


async def test_returns_run_id(db_session):
    attempt_id, search_space_id = await _seed_attempt(db_session)
    grade, graded = _grade_with_findings()
    run_id = await persist_comparison_run(
        db_session,
        attempt_id=attempt_id,
        user_id=_USER_ID,
        search_space_id=search_space_id,
        grade=grade,
        audited=graded,
        normalization_confidence=0.9,
        reference_graph_hash=_REF_HASH,
    )
    found = (
        await db_session.execute(
            select(GraphComparisonRun.id).where(GraphComparisonRun.id == run_id)
        )
    ).scalar_one()
    assert found == run_id
