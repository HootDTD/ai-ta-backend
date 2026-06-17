"""WU-4A2 Task 6 — grade_attempt + GradeResult orchestration (core.py).

RED-first. These are the §6.11 pure-score fixtures (the subset that needs NO
audit/abstention/events — those are WU-4B). Every input is a hand-built frozen
CanonicalGraph/ReferenceGraph; nothing here touches Neo4j/Postgres/LLM/resolver.
"""

from __future__ import annotations

import dataclasses
import math

from apollo.graph_compare.core import COMPARISON_VERSION, GradeResult, grade_attempt
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.ontology.edges import EdgeType
from apollo.persistence.models import GraphComparisonRun

from ._builders import cedge, cnode, empty_snorm, path, rgraph, rnode, snorm

_SCORE_FIELDS = (
    "coverage_score",
    "soundness_score",
    "bisimilarity_score",
    "node_coverage_score",
    "edge_coverage_score",
    "scoping_score",
    "usage_score",
    "procedure_order_score",
    "dependency_score",
    "contradiction_score",
)


def _ref_three():
    return rgraph(
        nodes=(rnode("eq.a"), rnode("eq.b"), rnode("eq.c")),
        paths=(path("eq.a", "eq.b", "eq.c"),),
    )


def _kinds(result: GradeResult, kind: FindingKind) -> list[Finding]:
    return [f for f in result.findings if f.kind is kind]


def test_grade_result_fields_map_to_run_columns():
    field_names = {f.name for f in dataclasses.fields(GradeResult)}
    run_cols = {c.name for c in GraphComparisonRun.__table__.columns}
    for score_field in _SCORE_FIELDS:
        assert score_field in field_names
        assert score_field in run_cols, f"{score_field} not a runs column"
    # the carried (non-persisted-by-name) fields
    assert "comparison_confidence" in field_names
    assert "findings" in field_names
    assert "comparison_version" in field_names


def test_comparison_confidence_is_one_in_v1():
    student = snorm(nodes=(cnode("eq.a"),))
    assert grade_attempt(student, _ref_three()).comparison_confidence == 1.0


def test_comparison_version_constant():
    student = snorm(nodes=(cnode("eq.a"),))
    result = grade_attempt(student, _ref_three())
    assert result.comparison_version == COMPARISON_VERSION == "graph-compare-v1"


def test_empty_student_graph_degenerate():
    result = grade_attempt(empty_snorm(), _ref_three())
    assert result.coverage_score == 0.0
    assert result.soundness_score == 1.0
    assert result.bisimilarity_score == 0.0
    for f in _SCORE_FIELDS:
        assert not math.isnan(getattr(result, f))


def test_correct_answer_thin_explanation():
    # Covers 1 of 3 keys, no misconception -> low coverage, high soundness.
    student = snorm(nodes=(cnode("eq.a"),))
    result = grade_attempt(student, _ref_three())
    assert result.coverage_score == 1 / 3
    assert result.soundness_score == 1.0
    assert result.bisimilarity_score < 0.6
    assert _kinds(result, FindingKind.CONTRADICTION) == []


def test_wrong_answer_mostly_correct_concepts():
    # Covers several keys AND one misconception -> covered findings + exactly one
    # contradiction; soundness penalized.
    student = snorm(
        nodes=(
            cnode("eq.a"),
            cnode("eq.b"),
            cnode("misc.density_ignored", node_type="misconception"),
        )
    )
    result = grade_attempt(student, _ref_three())
    assert len(_kinds(result, FindingKind.COVERED_NODE)) == 2
    assert len(_kinds(result, FindingKind.CONTRADICTION)) == 1
    assert result.soundness_score == 0.5


def test_reference_omits_valid_assumption_student_states():
    # Extra non-reference non-misconception node -> one UNSUPPORTED_EXTRA finding,
    # ZERO soundness penalty.
    student = snorm(
        nodes=(cnode("eq.a"), cnode("cond.steady_flow", node_type="condition")),
    )
    result = grade_attempt(student, _ref_three())
    extras = _kinds(result, FindingKind.UNSUPPORTED_EXTRA)
    assert len(extras) == 1
    assert extras[0].canonical_key == "cond.steady_flow"
    assert result.soundness_score == 1.0


def test_misconception_not_in_bank_is_unsupported_extra_not_contradiction():
    # Extra node with a non-misc key matching nothing -> UNSUPPORTED_EXTRA, NOT
    # CONTRADICTION (honest non-detection).
    student = snorm(
        nodes=(
            cnode("eq.a"),
            cnode("eq.wrong_unenumerated"),
        )
    )
    result = grade_attempt(student, _ref_three())
    assert len(_kinds(result, FindingKind.UNSUPPORTED_EXTRA)) == 1
    assert _kinds(result, FindingKind.CONTRADICTION) == []
    assert result.soundness_score == 1.0


def _two_path_ref():
    return rgraph(
        nodes=(rnode("a"), rnode("b"), rnode("c"), rnode("d"), rnode("e")),
        paths=(path("a", "b", "c"), path("a", "d", "e")),
    )


def test_valid_alternative_path_via_path_b():
    student = snorm(nodes=(cnode("a"), cnode("d"), cnode("e")))
    result = grade_attempt(student, _two_path_ref())
    assert result.coverage_score == 1.0
    assert len(_kinds(result, FindingKind.ALTERNATIVE_PATH)) == 1
    assert _kinds(result, FindingKind.MISSING_NODE) == []  # zero false missings


def test_missing_findings_only_against_winning_path():
    # Student partial on the winning path (covers a,b of a,b,c) -> missing only c.
    student = snorm(nodes=(cnode("a"), cnode("b")))
    ref = rgraph(
        nodes=(rnode("a"), rnode("b"), rnode("c"), rnode("x"), rnode("y")),
        paths=(path("a", "b", "c"), path("a", "x", "y")),
    )
    result = grade_attempt(student, ref)
    missing = {f.canonical_key for f in _kinds(result, FindingKind.MISSING_NODE)}
    assert missing == {"c"}  # never path B's x,y


def test_no_event_kinds_in_findings():
    student = snorm(
        nodes=(cnode("eq.a"), cnode("misc.density_ignored", node_type="misconception")),
    )
    result = grade_attempt(student, _ref_three())
    for f in result.findings:
        assert isinstance(f.kind, FindingKind)
    finding_fields = {fld.name for fld in dataclasses.fields(Finding)}
    grade_fields = {fld.name for fld in dataclasses.fields(GradeResult)}
    assert "event_kind" not in finding_fields and "event" not in finding_fields
    assert "event_kind" not in grade_fields and "events" not in grade_fields


def test_edge_gaps_are_diagnostic_findings_only():
    ref = rgraph(
        nodes=(rnode("proc.x", "procedure_step"), rnode("eq.y")),
        edges=(cedge(EdgeType.USES, "proc.x", "eq.y"),),
        paths=(path("proc.x", "eq.y"),),
    )
    # Student covers both nodes but states no edge -> missing edge, full node cov.
    student = snorm(nodes=(cnode("proc.x", "procedure_step"), cnode("eq.y")))
    result = grade_attempt(student, ref)
    assert len(_kinds(result, FindingKind.MISSING_EDGE)) == 1
    assert result.edge_coverage_score == 0.0
    # Edge gap does NOT move the three top-line scores: coverage is full (both
    # nodes present), soundness 1.0, bisimilarity 1.0.
    assert result.coverage_score == 1.0
    assert result.soundness_score == 1.0
    assert result.bisimilarity_score == 1.0


def test_procedure_order_inversion_end_to_end():
    ref = rgraph(
        nodes=(rnode("proc.a", "procedure_step"), rnode("proc.b", "procedure_step")),
        paths=(path("proc.a", "proc.b"),),
    )
    inverted = snorm(
        nodes=(cnode("proc.a", "procedure_step"), cnode("proc.b", "procedure_step")),
        edges=(cedge(EdgeType.PRECEDES, "proc.b", "proc.a"),),
    )
    assert grade_attempt(inverted, ref).procedure_order_score < 1.0
    no_order = snorm(
        nodes=(cnode("proc.a", "procedure_step"), cnode("proc.b", "procedure_step")),
    )
    assert grade_attempt(no_order, ref).procedure_order_score == 1.0


def test_matched_edge_finding_emitted():
    ref = rgraph(
        nodes=(rnode("proc.x", "procedure_step"), rnode("eq.y")),
        edges=(cedge(EdgeType.USES, "proc.x", "eq.y"),),
        paths=(path("proc.x", "eq.y"),),
    )
    student = snorm(
        nodes=(cnode("proc.x", "procedure_step"), cnode("eq.y")),
        edges=(cedge(EdgeType.USES, "proc.x", "eq.y"),),
    )
    result = grade_attempt(student, ref)
    assert len(_kinds(result, FindingKind.MATCHED_EDGE)) == 1


def test_unresolved_findings_carried():
    student = snorm(
        nodes=(cnode("eq.a"),),
        unresolved_nodes=(("n_raw", "garbled surface"),),
    )
    result = grade_attempt(student, _ref_three())
    unresolved = _kinds(result, FindingKind.UNRESOLVED)
    assert len(unresolved) == 1
    assert unresolved[0].student_node_ids == ("n_raw",)


def test_findings_deterministically_ordered():
    student = snorm(
        nodes=(
            cnode("eq.b"),
            cnode("eq.a"),
            cnode("misc.density_ignored", node_type="misconception"),
            cnode("eq.extra"),
        ),
        unresolved_nodes=(("n2", "s2"), ("n1", "s1")),
    )
    r1 = grade_attempt(student, _ref_three())
    r2 = grade_attempt(student, _ref_three())
    assert r1.findings == r2.findings


def test_grade_attempt_is_pure():
    student = snorm(nodes=(cnode("eq.a"), cnode("eq.b")))
    ref = _ref_three()
    assert grade_attempt(student, ref) == grade_attempt(student, ref)


def test_grade_result_default_comparison_version():
    # A bare GradeResult uses the COMPARISON_VERSION default.
    result = GradeResult(
        coverage_score=1.0,
        soundness_score=1.0,
        bisimilarity_score=1.0,
        node_coverage_score=1.0,
        edge_coverage_score=1.0,
        scoping_score=1.0,
        usage_score=1.0,
        procedure_order_score=1.0,
        dependency_score=1.0,
        contradiction_score=1.0,
        comparison_confidence=1.0,
        findings=(),
    )
    assert result.comparison_version == COMPARISON_VERSION
