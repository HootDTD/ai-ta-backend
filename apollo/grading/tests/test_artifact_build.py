"""Campaign-plan Task A2 Step 4/5 — the pure canonical-artifact builders."""

from __future__ import annotations

import pytest

from apollo.grading.artifact_build import (
    GRADER_USED_GRAPH,
    GRADER_USED_LLM_FALLBACK,
    build_edge_ledger,
    build_graph_artifact,
    build_llm_artifact,
    build_misconceptions,
    build_node_ledger,
    compute_misconception_penalty,
)
from apollo.grading.audited_grade import AuditedGrade
from apollo.grading.composite import CompositeWeights, composite_score, load_weights
from apollo.graph_compare.core import COMPARISON_VERSION, GradeResult
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.resolution.result import ResolutionResult, ResolvedNode


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
        Finding(
            kind=FindingKind.CONTRADICTION,
            canonical_key="misc.wrong",
            student_node_ids=("n_m",),
            evidence_spans=("pressure always increases",),
            score=0.0,
        ),
        Finding(
            kind=FindingKind.UNRESOLVED,
            student_node_ids=("n_x",),
            evidence_spans=("gibberish",),
        ),
        Finding(kind=FindingKind.MATCHED_EDGE, message="eq.a -PRECEDES-> eq.b (explicit)"),
        Finding(kind=FindingKind.MISSING_EDGE, message="eq.b -USES-> eq.c (explicit)"),
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
            ResolvedNode(
                node_id="n_m",
                resolution="resolved",
                resolved_key="misc.wrong",
                resolved_canon_key=2,
                method="fuzzy",
                confidence=0.80,
            ),
            ResolvedNode(
                node_id="n_x",
                resolution="unresolved",
                resolved_key=None,
                resolved_canon_key=None,
                method="unresolved",
                confidence=0.0,
            ),
        ),
        tier_counts={"alias": 1, "fuzzy": 1, "unresolved": 1},
        llm_calls=0,
    )


@pytest.fixture
def shadow_fixture() -> ShadowGradeResult:
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
        opposes_map={"misc.wrong": "eq.a"},
        turn_order={},
        graph_sim_rubric={},
        calibration=object(),  # type: ignore[arg-type]
        diagnostic=object(),  # type: ignore[arg-type]
        resolution=_resolution(),
    )


# --- node/edge/misconception ledger unit tests ------------------------------


def test_node_ledger_statuses_and_methods():
    findings = _findings()
    ledger = build_node_ledger(findings, _resolution())
    statuses = {e["status"] for e in ledger}
    assert statuses == {"credited", "misconception", "unresolved"}
    credited = [e for e in ledger if e["status"] == "credited"][0]
    assert credited["canonical_key"] == "eq.a"
    assert credited["method"] == "alias"
    assert credited["confidence"] == 0.92
    assert credited["evidence_span"] == "eq a is conserved"

    misc = [e for e in ledger if e["status"] == "misconception"][0]
    assert misc["method"] == "fuzzy"
    assert misc["confidence"] == 0.80
    assert misc["evidence_span"] == "pressure always increases"

    # Two "unresolved" rows now: the real UNRESOLVED student utterance
    # (student-side id, non-null evidence span) and the MISSING_NODE
    # reference row (Task 3 scorecard fix) -- distinguish by canonical_key.
    unresolved_rows = [e for e in ledger if e["status"] == "unresolved"]
    assert len(unresolved_rows) == 2
    utterance = next(e for e in unresolved_rows if e["canonical_key"] != "eq.b")
    assert utterance["canonical_key"] == "n_x"
    assert utterance["method"] is None
    assert utterance["evidence_span"] == "gibberish"
    assert utterance["confidence"] == 0.0


def test_node_ledger_includes_missing_node_as_unresolved_with_reference_key():
    """Scorecard fix (campaign-plan Task 3): a MISSING_NODE finding (a
    reference node with ZERO student evidence) now earns an ``unresolved``
    ledger row keyed by the REFERENCE node's own display-safe canonical_key
    -- never an internal student-side id -- with ``evidence_span``/
    ``confidence`` explicitly ``None`` (no utterance was ever produced, so
    there is nothing to quote and no resolution was ever attempted)."""
    findings = _findings()
    ledger = build_node_ledger(findings, _resolution())
    missing = next(e for e in ledger if e["canonical_key"] == "eq.b")
    assert missing["status"] == "unresolved"
    assert missing["method"] is None
    assert missing["confidence"] is None
    assert missing["evidence_span"] is None
    assert len(ledger) == 4


def test_edge_ledger_matched_and_missing():
    ledger = build_edge_ledger(_findings())
    assert {e["status"] for e in ledger} == {"matched", "missing"}
    matched = [e for e in ledger if e["status"] == "matched"][0]
    assert matched["from_key"] == "eq.a"
    assert matched["edge_type"] == "PRECEDES"
    assert matched["to_key"] == "eq.b"
    assert matched["provenance"] == "explicit"


def test_parse_edge_message_missing_degrades_to_none():
    from apollo.grading.artifact_build import _parse_edge_message

    assert _parse_edge_message(None) == {
        "from_key": None,
        "edge_type": None,
        "to_key": None,
        "provenance": None,
    }


def test_parse_edge_message_malformed_degrades_but_keeps_raw():
    from apollo.grading.artifact_build import _parse_edge_message

    parsed = _parse_edge_message("not a well-formed edge message")
    assert parsed == {
        "from_key": None,
        "edge_type": None,
        "to_key": None,
        "provenance": "not a well-formed edge message",
    }


def test_misconceptions_asserted_carries_opposes():
    findings = _findings()
    misconceptions = build_misconceptions(findings, _resolution(), {"misc.wrong": "eq.a"})
    assert len(misconceptions) == 1
    m = misconceptions[0]
    assert m["canonical_key"] == "misc.wrong"
    assert m["confidence"] == 0.80
    assert m["opposes"] == "eq.a"
    assert m["evidence_span"] == "pressure always increases"


def test_misconception_penalty_formula():
    # 1 asserted misconception (>= floor) / reference_node_count.
    misconceptions = [{"confidence": 0.8}, {"confidence": 0.1}]
    assert compute_misconception_penalty(misconceptions, 2) == 0.5


def test_misconception_penalty_floors_reference_count_at_one():
    assert compute_misconception_penalty([{"confidence": 1.0}], 0) == 1.0


# --- build_graph_artifact ----------------------------------------------------


def test_graph_artifact_node_ledger_statuses(shadow_fixture):
    art = build_graph_artifact(
        shadow=shadow_fixture, weights=load_weights(), clarification_trace=[], latency_ms=1200
    )
    statuses = {e["status"] for e in art["node_ledger"]}
    assert statuses <= {"credited", "misconception", "unresolved"}
    for e in art["node_ledger"]:
        if e["status"] == "credited":
            assert e["method"] in {
                "exact",
                "symbolic",
                "derived",
                "alias",
                "clarification",
                "nli",
                "fuzzy",
            }
            assert e["evidence_span"]  # every credit carries evidence (spec §1)


def test_graph_artifact_grader_used_and_versions(shadow_fixture):
    art = build_graph_artifact(
        shadow=shadow_fixture, weights=load_weights(), clarification_trace=[], latency_ms=1200
    )
    assert art["grader_used"] == GRADER_USED_GRAPH
    assert art["versions"]["grader"] == COMPARISON_VERSION
    assert art["versions"]["reference_graph_hash"] == "refhash-v1:deadbeef"
    assert art["versions"]["weights"] == {"w_n": 0.6, "w_e": 0.25, "p": 0.15}
    assert art["grading_latency_ms"] == 1200


def test_scores_block_records_weights(shadow_fixture):
    art = build_graph_artifact(
        shadow=shadow_fixture, weights=load_weights(), clarification_trace=[], latency_ms=None
    )
    s = art["scores"]
    assert s["composite"] == composite_score(
        s["node_coverage"],
        s["edge_coverage"],
        s["misconception_penalty"],
        CompositeWeights(**s["weights"]),
    )


def test_graph_artifact_abstention_block(shadow_fixture):
    art = build_graph_artifact(
        shadow=shadow_fixture, weights=load_weights(), clarification_trace=[], latency_ms=None
    )
    assert art["abstention"] == {
        "abstained": False,
        "reasons": [],
        "normalization_confidence": 0.8,
        "fallback_grade": None,
        "graph_failure": None,
    }


# --- Lane B3a/D1: empty-bank "no misconceptions asserted" marker ------------


def _empty_bank_shadow() -> ShadowGradeResult:
    """A ShadowGradeResult for a cold-start EMPTY misconception bank: coverage
    was graded (covered + missing findings) but soundness was never checked
    (``soundness_applicable=False``, no CONTRADICTION findings — no misc
    candidate is ever minted for an empty bank)."""
    import dataclasses

    findings = (
        Finding(
            kind=FindingKind.COVERED_NODE,
            canonical_key="eq.a",
            student_node_ids=("n_a",),
            evidence_spans=("eq a is conserved",),
            confidence=0.92,
        ),
        Finding(kind=FindingKind.MISSING_NODE, canonical_key="eq.b", score=0.0),
    )
    base = _grade(findings)
    grade = dataclasses.replace(
        base,
        soundness_applicable=False,
        soundness_score=None,
        contradiction_score=None,
        bisimilarity_score=base.coverage_score,  # coverage-only fallback
    )
    audited = AuditedGrade(
        grade=grade,
        findings=findings,
        abstention_reasons=(),  # empty bank is NOT an abstention reason (D1)
        abstained=False,
        suppressed_event_kinds=frozenset(),
        alias_candidates=(),
    )
    return ShadowGradeResult(
        run_id=2,
        grade=grade,
        audited=audited,
        normalization_confidence=0.9,
        reference_graph_hash="refhash-v1:emptybank",
        opposes_map={},
        turn_order={},
        graph_sim_rubric={},
        calibration=object(),  # type: ignore[arg-type]
        diagnostic=object(),  # type: ignore[arg-type]
        resolution=_resolution(),
    )


def test_empty_bank_artifact_grades_coverage_and_carries_no_assertion_marker():
    """D1: an empty misconception bank grades COVERAGE normally (scores present,
    no abstention) AND the artifact carries an explicit machine-readable
    "no misconceptions asserted (empty bank)" marker — disambiguating an empty
    ``misconceptions: []`` that was NEVER assessed from one that WAS assessed and
    found none. Lane B3a rework: the marker is nested in the PERSISTED
    ``abstention`` JSONB block (a top-level payload key has no artifact column
    and was silently dropped before it reached the row or the scorecard)."""
    art = build_graph_artifact(
        shadow=_empty_bank_shadow(), weights=load_weights(), clarification_trace=[], latency_ms=None
    )
    # Coverage graded normally — NOT silenced, NOT abstained.
    assert art["scores"]["node_coverage"] == 0.5
    assert art["abstention"]["abstained"] is False
    assert art["abstention"]["reasons"] == []
    # No misconceptions were minted (bank empty), and the explicit marker says so
    # — nested in the abstention block so it PERSISTS (not a dropped top-level key).
    assert art["misconceptions"] == []
    assert "misconceptions_status" not in art  # NOT a (dropped) top-level key
    marker = art["abstention"]["misconceptions_status"]
    assert marker["assertable"] is False
    assert marker["reason"] == "empty_bank"
    assert "empty bank" in marker["detail"]


def test_seeded_bank_artifact_has_no_status_marker_byte_identical(shadow_fixture):
    """Paired evidence (D1): a SEEDED bank (``soundness_applicable=True``, the
    fixture default) produces an artifact BYTE-IDENTICAL to today — no
    ``misconceptions_status`` marker key is ever added, at top level OR inside
    the ``abstention`` block, on the seeded path."""
    art = build_graph_artifact(
        shadow=shadow_fixture, weights=load_weights(), clarification_trace=[], latency_ms=1200
    )
    assert "misconceptions_status" not in art
    assert "misconceptions_status" not in art["abstention"]


def test_graph_artifact_clarification_trace_passthrough(shadow_fixture):
    trace = [{"question": "q1", "answer": "a1", "credit_granted": True}]
    art = build_graph_artifact(
        shadow=shadow_fixture, weights=load_weights(), clarification_trace=trace, latency_ms=None
    )
    assert art["clarification_trace"] == trace
    # Immutable input: the builder must never mutate the caller's list.
    assert art["clarification_trace"] is not trace


# --- build_llm_artifact ------------------------------------------------------


def test_llm_artifact_shape():
    # Real `compute_coverage` shape: a `per_step` map, not a covered/missing
    # list pair (that fictional shape was the T1 defect — see artifact_build's
    # `build_llm_artifact` docstring).
    art = build_llm_artifact(
        coverage={
            "per_step": {"k1": "covered", "k2": "missing"},
            "confidences": {"k1": 0.9, "k2": 0.0},
        },
        rubric={"overall": {"score": 71}},
        weights=load_weights(),
        graph_failure="boom",
        latency_ms=5,
    )
    assert art["grader_used"] == GRADER_USED_LLM_FALLBACK
    assert art["abstention"]["graph_failure"] == "boom"
    assert art["abstention"]["fallback_grade"] == 71
    assert art["edge_ledger"] == []
    assert art["scores"]["edge_coverage"] == 0.0
    assert art["scores"]["misconception_penalty"] == 0.0
    assert art["scores"]["node_coverage"] == 0.5
    # The headline composite is the documented LLM-path mapping (spec §1/§3):
    # the rubric's own overall score renormalized to 0-1 — NOT run through the
    # graph path's weighted composite_score formula (which would cap it at
    # w_n with edge_coverage/misconception_penalty both 0).
    assert art["scores"]["composite"] == pytest.approx(0.71)
    statuses = {e["status"] for e in art["node_ledger"]}
    assert statuses == {"credited", "unresolved"}


def test_llm_artifact_node_ledger_evidence_span_is_none_never_empty_string():
    """Q2 fix (lane B4): the LLM-fallback path has NO per-node student
    utterance (``compute_coverage``'s ``per_step`` is ``{ref_id: status}``,
    carrying no surface text), so every node-ledger row's ``evidence_span`` is
    ``None`` — the honest "no span available" value (matching the established
    ``_missing_ledger_entry`` convention) — NEVER the empty string ``""`` that
    rendered as a fake empty quote in every F1c scorecard. The key stays
    PRESENT (value null) so the S3 fidelity judge's ``.get('evidence_span')``
    input shape is unchanged."""
    art = build_llm_artifact(
        coverage={
            "per_step": {"k1": "covered", "k2": "missing"},
            "confidences": {"k1": 0.9, "k2": 0.0},
        },
        rubric={"overall": {"score": 71}},
        weights=load_weights(),
        graph_failure=None,
        latency_ms=None,
    )
    assert art["node_ledger"]  # non-empty
    for entry in art["node_ledger"]:
        assert "evidence_span" in entry  # key present (S3 judge input shape)
        assert entry["evidence_span"] is None
        assert entry["evidence_span"] != ""


def test_llm_artifact_no_attempts_zero_coverage():
    art = build_llm_artifact(
        coverage={"per_step": {}, "confidences": {}},
        rubric={},
        weights=load_weights(),
        graph_failure=None,
        latency_ms=None,
    )
    assert art["scores"]["node_coverage"] == 0.0
    assert art["scores"]["composite"] == 0.0
    assert art["abstention"]["fallback_grade"] is None
    assert art["abstention"]["graph_failure"] is None


def test_llm_artifact_empty_bank_nests_marker_in_abstention():
    """Lane B3a/D1 rework: the LLM path is the SERVED path in the default build
    (shadow off), so the empty-bank marker MUST exist on the LLM artifact for
    the served scorecard to render "not checked". With
    ``misconceptions_bank_empty=True`` the marker is nested in the persisted
    ``abstention`` block, identically to the graph path."""
    art = build_llm_artifact(
        coverage={"per_step": {"k1": "covered"}, "confidences": {"k1": 0.9}},
        rubric={"overall": {"score": 88}},
        weights=load_weights(),
        graph_failure=None,
        latency_ms=None,
        misconceptions_bank_empty=True,
    )
    assert "misconceptions_status" not in art  # NOT a dropped top-level key
    marker = art["abstention"]["misconceptions_status"]
    assert marker["assertable"] is False
    assert marker["reason"] == "empty_bank"
    assert "empty bank" in marker["detail"]


def test_llm_artifact_seeded_bank_default_is_byte_identical():
    """Paired evidence: the default (``misconceptions_bank_empty`` unset ->
    False, the seeded/legacy path) adds NO marker key anywhere — the LLM
    artifact is byte-identical to before this lane."""
    kwargs = dict(
        coverage={"per_step": {"k1": "covered", "k2": "missing"}, "confidences": {}},
        rubric={"overall": {"score": 71}},
        weights=load_weights(),
        graph_failure=None,
        latency_ms=5,
    )
    default = build_llm_artifact(**kwargs)
    seeded = build_llm_artifact(**kwargs, misconceptions_bank_empty=False)
    assert default == seeded
    assert "misconceptions_status" not in default["abstention"]
