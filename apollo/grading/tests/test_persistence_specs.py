"""WU-4B3 — pure spec-mapping tests (no DB) for persistence.py.

Pins the DB-free 1:1 column mapping: every ``GradeResult`` score lands on the
``RunRowSpec``; abstention comes from the ``AuditedGrade`` (NOT recomputed); the
two scalars + comparison_version land verbatim; ``Finding`` -> ``FindingRowSpec``
(StrEnum -> string, tuple -> JSONB list, edge ids always ``[]``, entity_id NULL);
the persisted finding source is ``audited.findings`` (the audit-REWRITTEN set),
NOT ``grade.findings``; and both specs are frozen.
"""

from __future__ import annotations

import dataclasses

import pytest

from apollo.grading.audited_grade import (
    AUDIT_UPGRADE_MESSAGE,
    build_audited_grade,
)
from apollo.grading.persistence import (
    finding_to_row_spec,
    findings_to_row_specs,
    grade_to_run_spec,
)
from apollo.grading.tests._builders import (
    candidate,
    covered_finding,
    found_audit_fn,
    missing_grade,
    nodes_with_confidences,
    resolution_with,
)
from apollo.graph_compare.core import COMPARISON_VERSION
from apollo.graph_compare.findings import Finding, FindingKind

_ATTEMPT_ID = 7
_USER_ID = "11111111-1111-1111-1111-111111111111"
_SEARCH_SPACE_ID = 3


def _audited_for(grade, *, abstained=False, reasons=(), suppressed=()):
    """A literal AuditedGrade carrying ``grade`` + the given abstention shape."""
    from apollo.grading.audited_grade import AuditedGrade

    return AuditedGrade(
        grade=grade,
        findings=grade.findings,
        abstention_reasons=reasons,
        abstained=abstained,
        suppressed_event_kinds=frozenset(suppressed),
        alias_candidates=(),
    )


def test_grade_to_run_spec_maps_all_ten_scores():
    grade = missing_grade(covered=("k.a",))
    # Make every score distinct so a swapped mapping is caught.
    grade = dataclasses.replace(
        grade,
        coverage_score=0.10,
        soundness_score=0.20,
        bisimilarity_score=0.30,
        node_coverage_score=0.40,
        edge_coverage_score=0.50,
        scoping_score=0.60,
        usage_score=0.70,
        procedure_order_score=0.80,
        dependency_score=0.90,
        contradiction_score=0.99,
    )
    spec = grade_to_run_spec(
        attempt_id=_ATTEMPT_ID,
        user_id=_USER_ID,
        search_space_id=_SEARCH_SPACE_ID,
        grade=grade,
        audited=_audited_for(grade),
        normalization_confidence=0.85,
        reference_graph_hash="refhash-v1:abc",
    )
    assert spec.coverage_score == 0.10
    assert spec.soundness_score == 0.20
    assert spec.bisimilarity_score == 0.30
    assert spec.node_coverage_score == 0.40
    assert spec.edge_coverage_score == 0.50
    assert spec.scoping_score == 0.60
    assert spec.usage_score == 0.70
    assert spec.procedure_order_score == 0.80
    assert spec.dependency_score == 0.90
    assert spec.contradiction_score == 0.99
    assert spec.attempt_id == _ATTEMPT_ID
    assert spec.user_id == _USER_ID
    assert spec.search_space_id == _SEARCH_SPACE_ID


def test_run_spec_takes_abstention_from_audited():
    """abstained + abstention_reasons come from the AuditedGrade, not recomputed."""
    grade = missing_grade(("k.a",))
    spec = grade_to_run_spec(
        attempt_id=_ATTEMPT_ID,
        user_id=_USER_ID,
        search_space_id=_SEARCH_SPACE_ID,
        grade=grade,
        audited=_audited_for(
            grade,
            abstained=True,
            reasons=("unresolved_rate_above_threshold", "transcript_audit_unavailable"),
        ),
        normalization_confidence=1.0,
        reference_graph_hash="refhash-v1:abc",
    )
    assert spec.abstained is True
    assert spec.abstention_reasons == (
        "unresolved_rate_above_threshold",
        "transcript_audit_unavailable",
    )


def test_run_spec_carries_norm_conf_and_hash():
    grade = missing_grade(covered=("k.a",))
    spec = grade_to_run_spec(
        attempt_id=_ATTEMPT_ID,
        user_id=_USER_ID,
        search_space_id=_SEARCH_SPACE_ID,
        grade=grade,
        audited=_audited_for(grade),
        normalization_confidence=0.73,
        reference_graph_hash="refhash-v1:deadbeef",
    )
    assert spec.normalization_confidence == 0.73
    assert spec.reference_graph_hash == "refhash-v1:deadbeef"
    assert spec.comparison_version == COMPARISON_VERSION
    assert spec.comparison_version == grade.comparison_version


@pytest.mark.parametrize(
    "kind",
    [
        FindingKind.COVERED_NODE,
        FindingKind.MISSING_NODE,
        FindingKind.CONTRADICTION,
        FindingKind.UNRESOLVED,
        FindingKind.UNSUPPORTED_EXTRA,
        FindingKind.MATCHED_EDGE,
        FindingKind.MISSING_EDGE,
        FindingKind.ALTERNATIVE_PATH,
    ],
)
def test_finding_to_row_spec_kind_is_string(kind):
    """finding_kind is the plain StrEnum .value string for every kind."""
    finding = Finding(kind=kind, canonical_key="k.x")
    spec = finding_to_row_spec(finding)
    assert spec.finding_kind == kind.value
    assert isinstance(spec.finding_kind, str)


def test_finding_row_spec_jsonb_lists():
    """node-id / span tuples become lists; edge ids always []; nullable preserved."""
    finding = Finding(
        kind=FindingKind.COVERED_NODE,
        canonical_key="k.a",
        student_node_ids=("s1", "s2"),
        reference_node_ids=("r1",),
        evidence_spans=("the student said X",),
        score=0.9,
        confidence=0.92,
        message="hello",
    )
    spec = finding_to_row_spec(finding)
    assert spec.student_node_ids == ["s1", "s2"]
    assert spec.reference_node_ids == ["r1"]
    assert spec.evidence_spans == ["the student said X"]
    assert spec.student_edge_ids == []
    assert spec.reference_edge_ids == []
    assert spec.score == 0.9
    assert spec.confidence == 0.92
    assert spec.message == "hello"
    assert spec.entity_id is None


def test_finding_row_spec_nullables_default_none():
    """A bare missing finding has None score-bearing fields except its own score."""
    finding = Finding(kind=FindingKind.MISSING_NODE, canonical_key="k.a", score=0.0)
    spec = finding_to_row_spec(finding)
    assert spec.score == 0.0
    assert spec.confidence is None
    assert spec.message is None
    assert spec.student_node_ids == []
    assert spec.entity_id is None


def test_persist_uses_audited_findings_not_grade():
    """An audit-upgraded missing->covered appears in findings_to_row_specs(
    audited.findings) as covered_node carrying the span + AUDIT_UPGRADE_MESSAGE,
    while grade.findings still shows missing_node (the audited-not-grade source)."""
    grade = missing_grade(("eq.bernoulli",))
    nodes = nodes_with_confidences(0.9)
    cands = (candidate("eq.bernoulli", display_name="Bernoulli"),)
    audit_fn = found_audit_fn({"eq.bernoulli": "the student wrote p + half rho v^2 = c"})

    audited_grade = build_audited_grade(
        grade,
        transcript="... p + half rho v^2 = c ...",
        resolution=resolution_with(resolved=1),
        student_nodes=nodes,
        candidates=cands,
        audit_fn=audit_fn,
    )

    audited_specs = findings_to_row_specs(audited_grade.findings)
    grade_specs = findings_to_row_specs(grade.findings)

    upgraded = [s for s in audited_specs if s.finding_kind == "covered_node"]
    assert len(upgraded) == 1
    assert upgraded[0].message == AUDIT_UPGRADE_MESSAGE
    assert upgraded[0].evidence_spans == ["the student wrote p + half rho v^2 = c"]
    assert upgraded[0].confidence == 0.75
    # The pre-audit grade.findings STILL shows the missing_node (proof we did not
    # persist the pre-audit set).
    assert [s.finding_kind for s in grade_specs] == ["missing_node"]


def test_specs_are_frozen():
    grade = missing_grade(covered=("k.a",))
    run_spec = grade_to_run_spec(
        attempt_id=_ATTEMPT_ID,
        user_id=_USER_ID,
        search_space_id=_SEARCH_SPACE_ID,
        grade=grade,
        audited=_audited_for(grade),
        normalization_confidence=1.0,
        reference_graph_hash="refhash-v1:abc",
    )
    finding_spec = finding_to_row_spec(covered_finding("k.a"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        run_spec.coverage_score = 0.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding_spec.finding_kind = "x"  # type: ignore[misc]
