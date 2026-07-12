"""Tests for the deterministic topic-score module (workspace spec
``docs/superpowers/specs/2026-07-10-apollo-topic-score-design.md`` section 2).

Pure module: no IO, no LLM, no DB, no flags. Every test builds its inputs
in-process (``build_node`` for reference nodes, a plain ``ConceptFinding``
builder mirroring ``test_misconception_detector_merge.py``'s ``_docked``
convention, and hand-built ``coverage``/``centrality`` dicts).
"""

from __future__ import annotations

import dataclasses

import pytest

from apollo.ontology import Node, build_node
from apollo.overseer.misconception_detector.config import (
    CENTRALITY_W_MIN,
    SEVERITY_CLAMP,
)
from apollo.overseer.misconception_detector.types import ConceptFinding, MergeOutcome
from apollo.overseer.rubric import score_to_letter
from apollo.overseer.topic_score import (
    TopicCredit,
    TopicMisconception,
    TopicScoreResult,
    _dedup_findings_by_canonical_key,
    _finite_score,
    _weights_for,
    compute_topic_score,
)


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #
def _eq_node(node_id: str, *, label: str = "", attempt_id: int = 1) -> Node:
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"symbolic": "x = y", "label": label},
    )


def _cond_node(node_id: str, *, label: str = "", attempt_id: int = 1) -> Node:
    return build_node(
        node_type="condition",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"applies_when": "steady state", "label": label},
    )


def _simp_node(node_id: str, *, attempt_id: int = 1) -> Node:
    return build_node(
        node_type="simplification",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"applies_when": "x", "transformation": "y"},
    )


def _proc_node(node_id: str, *, action: str = "solve for x", attempt_id: int = 1) -> Node:
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"action": action, "purpose": ""},
    )


def _def_node(node_id: str, *, attempt_id: int = 1) -> Node:
    return build_node(
        node_type="definition",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"concept": "c", "meaning": "m"},
    )


def _var_node(node_id: str, *, attempt_id: int = 1) -> Node:
    return build_node(
        node_type="variable_mapping",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"term": "t", "symbol": "s"},
    )


def _finding(
    *,
    concept_key: str,
    confidence: float = 0.9,
    signature: str = "misc.example",
    evidence_span: str = "student said X",
    source: str = "judge",
) -> ConceptFinding:
    """Mirrors ``test_misconception_detector_merge.py``'s ``_docked`` builder —
    a gate-cleared (docked) finding, the shape ``ledger_findings`` holds."""
    return ConceptFinding(
        concept_key=concept_key,
        verdict="misconception",
        confidence=confidence,
        severity=0.0,
        evidence_span=evidence_span,
        signature=signature,
        source=source,  # type: ignore[arg-type]
        corroborated=True,
        ceiling_eligible=False,
    )


def _outcome(*findings: ConceptFinding) -> MergeOutcome:
    return MergeOutcome(
        misconception_penalty=0.0,
        misconceptions=(),
        ceiling_applied=False,
        ledger_findings=tuple(findings),
    )


# --------------------------------------------------------------------------- #
# Empty reference graph
# --------------------------------------------------------------------------- #
def test_empty_reference_graph_yields_zero_score_no_crash():
    result = compute_topic_score(
        coverage={"per_step": {}, "procedure_scores": {}},
        reference_nodes=[],
        centrality={},
        detection_outcome=None,
    )

    assert result == TopicScoreResult(
        score=0,
        letter="F",
        coverage_component=0.0,
        misconception_dock=0.0,
        topics=(),
    )


def test_reference_graph_with_only_ungraded_types_is_empty():
    """definition/variable_mapping nodes never become topics (mirrors
    rubric.py::_axis_for's exclusion — coverage's per_step never touches them
    either)."""
    refs = [_def_node("d1"), _var_node("v1")]

    result = compute_topic_score(
        coverage={"per_step": {}, "procedure_scores": {}},
        reference_nodes=refs,
        centrality={"d1": 1.0, "v1": 1.0},
        detection_outcome=None,
    )

    assert result.topics == ()
    assert result.score == 0


# --------------------------------------------------------------------------- #
# Per-topic credit: full / partial / missing
# --------------------------------------------------------------------------- #
def test_full_credit_when_covered_and_absent_from_procedure_scores():
    """A node absent from procedure_scores falls back to the binary per_step
    signal: covered -> 1.0."""
    refs = [_eq_node("eq1")]
    coverage = {"per_step": {"eq1": "covered"}, "procedure_scores": {}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"eq1": 1.0},
        detection_outcome=None,
    )

    assert len(result.topics) == 1
    topic = result.topics[0]
    assert topic.credit == 1.0
    assert topic.status == "covered"
    assert topic.weight == pytest.approx(1.0)
    assert result.coverage_component == pytest.approx(1.0)
    assert result.score == 100
    assert result.letter == score_to_letter(100)


def test_covered_with_lower_procedure_score_yields_that_credit_not_one():
    """The grader's number IS the grade: a covered node whose
    procedure_scores entry is 0.7 yields credit 0.7 (status stays "covered"),
    not a promoted 1.0."""
    refs = [_eq_node("eq1")]
    coverage = {"per_step": {"eq1": "covered"}, "procedure_scores": {"eq1": 0.7}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"eq1": 1.0},
        detection_outcome=None,
    )

    topic = result.topics[0]
    assert topic.credit == pytest.approx(0.7)
    assert topic.status == "covered"
    assert result.coverage_component == pytest.approx(0.7)
    assert result.score == 70


def test_covered_credit_flows_continuously_into_topic_weighting():
    """A covered node with procedure_scores 0.79 propagates that exact value
    into the weighted score arithmetic."""
    refs = [_eq_node("eq1"), _eq_node("eq2")]
    coverage = {
        "per_step": {"eq1": "covered", "eq2": "covered"},
        "procedure_scores": {"eq1": 0.79},
    }

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"eq1": 1.0, "eq2": 1.0},
        detection_outcome=None,
    )

    by_key = {t.canonical_key: t for t in result.topics}
    assert by_key["eq1"].credit == pytest.approx(0.79)
    assert by_key["eq1"].status == "covered"
    assert by_key["eq2"].credit == pytest.approx(1.0)

    expected_coverage = by_key["eq1"].weight * 0.79 + by_key["eq2"].weight * 1.0
    assert result.coverage_component == pytest.approx(expected_coverage)
    assert result.score == int(round(expected_coverage * 100))


def test_partial_credit_from_procedure_scores():
    refs = [_proc_node("p1")]
    coverage = {"per_step": {"p1": "missing"}, "procedure_scores": {"p1": 0.6}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"p1": 1.0},
        detection_outcome=None,
    )

    topic = result.topics[0]
    assert topic.credit == pytest.approx(0.6)
    assert topic.status == "partial"
    assert result.coverage_component == pytest.approx(0.6)
    assert result.score == 60


def test_partial_credit_zero_is_missing_not_partial():
    """0 < credit < 1 is 'partial'; credit == 0.0 (even if procedure_scores
    has an explicit 0.0 entry) is 'missing'."""
    refs = [_proc_node("p1")]
    coverage = {"per_step": {"p1": "missing"}, "procedure_scores": {"p1": 0.0}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"p1": 1.0},
        detection_outcome=None,
    )

    topic = result.topics[0]
    assert topic.credit == 0.0
    assert topic.status == "missing"


def test_missing_credit_when_absent_from_both_maps():
    refs = [_cond_node("c1")]
    coverage = {"per_step": {}, "procedure_scores": {}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"c1": 1.0},
        detection_outcome=None,
    )

    topic = result.topics[0]
    assert topic.credit == 0.0
    assert topic.status == "missing"
    assert result.coverage_component == 0.0
    assert result.score == 0


def test_malformed_procedure_score_coerces_to_zero():
    """A non-numeric procedure_scores value degrades to 0.0 rather than
    raising (mirrors rubric.py::_finite_score's NaN/non-numeric guard)."""
    refs = [_proc_node("p1")]
    coverage = {"per_step": {"p1": "missing"}, "procedure_scores": {"p1": float("nan")}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"p1": 1.0},
        detection_outcome=None,
    )

    topic = result.topics[0]
    assert topic.credit == 0.0
    assert topic.status == "missing"


# --------------------------------------------------------------------------- #
# Weights: centrality floor + normalization
# --------------------------------------------------------------------------- #
def test_weights_normalize_to_sum_one():
    refs = [_eq_node("a"), _eq_node("b")]
    coverage = {"per_step": {"a": "covered", "b": "covered"}, "procedure_scores": {}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 1.0, "b": 0.5},
        detection_outcome=None,
    )

    total_weight = sum(t.weight for t in result.topics)
    assert total_weight == pytest.approx(1.0)
    by_key = {t.canonical_key: t.weight for t in result.topics}
    # 1.0 / (1.0 + 0.5) and 0.5 / (1.0 + 0.5)
    assert by_key["a"] == pytest.approx(1.0 / 1.5)
    assert by_key["b"] == pytest.approx(0.5 / 1.5)


def test_centrality_below_floor_is_raised_to_centrality_w_min():
    """A node scored below CENTRALITY_W_MIN in the input map is floored, not
    passed through — mirrors merge.py's own floor semantics."""
    refs = [_eq_node("a"), _eq_node("b")]
    coverage = {"per_step": {"a": "covered", "b": "covered"}, "procedure_scores": {}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 0.01, "b": 1.0},
        detection_outcome=None,
    )

    by_key = {t.canonical_key: t.weight for t in result.topics}
    expected_a = CENTRALITY_W_MIN / (CENTRALITY_W_MIN + 1.0)
    assert by_key["a"] == pytest.approx(expected_a)


def test_missing_centrality_key_floors_at_centrality_w_min():
    refs = [_eq_node("a"), _eq_node("b")]
    coverage = {"per_step": {"a": "covered", "b": "covered"}, "procedure_scores": {}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"b": 1.0},  # "a" absent entirely
        detection_outcome=None,
    )

    by_key = {t.canonical_key: t.weight for t in result.topics}
    expected_a = CENTRALITY_W_MIN / (CENTRALITY_W_MIN + 1.0)
    assert by_key["a"] == pytest.approx(expected_a)


def test_single_topic_weight_is_one():
    refs = [_eq_node("a")]
    coverage = {"per_step": {"a": "covered"}, "procedure_scores": {}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 0.37},
        detection_outcome=None,
    )

    assert result.topics[0].weight == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Misconception dock: dedup, resolved-docks-zero, clamp
# --------------------------------------------------------------------------- #
def test_detection_outcome_none_docks_zero():
    refs = [_eq_node("a")]
    coverage = {"per_step": {"a": "covered"}, "procedure_scores": {}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 1.0},
        detection_outcome=None,
    )

    assert result.misconception_dock == 0.0
    assert result.topics[0].misconceptions == ()
    assert result.score == 100


def test_single_finding_docks_centrality_weighted_share():
    refs = [_eq_node("a")]
    coverage = {"per_step": {"a": "covered"}, "procedure_scores": {}}
    finding = _finding(concept_key="a", confidence=0.5, signature="misc.x")

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 0.4},
        detection_outcome=_outcome(finding),
    )

    # share = centrality(a) * confidence = 0.4 * 0.5 = 0.20
    assert result.misconception_dock == pytest.approx(0.20)
    assert result.coverage_component == pytest.approx(1.0)
    assert result.score == 80
    topic = result.topics[0]
    assert len(topic.misconceptions) == 1
    misc = topic.misconceptions[0]
    assert misc.canonical_key == "misc.x"
    assert misc.dock_points == pytest.approx(0.20)
    assert misc.resolved is False
    assert misc.evidence_span == "student said X"


def test_finding_missing_from_centrality_map_floors_at_centrality_w_min():
    refs = [_eq_node("a")]
    coverage = {"per_step": {"a": "covered"}, "procedure_scores": {}}
    finding = _finding(concept_key="a", confidence=1.0, signature="misc.x")

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={},
        detection_outcome=_outcome(finding),
    )

    assert result.misconception_dock == pytest.approx(CENTRALITY_W_MIN)


def test_dedup_by_canonical_key_keeps_max_confidence():
    """Two findings sharing a canonical_key (signature) — even if upstream
    merge dedup is bypassed — dock only once, at the max-confidence share.

    Centrality is deliberately sub-1.0 so the combined share (0.2*0.1 +
    0.2*0.3 = 0.08 if double-counted) stays well under SEVERITY_CLAMP either
    way — isolating the dedup law from the clamp law (covered separately by
    ``test_dock_clamped_at_severity_clamp``)."""
    refs = [_eq_node("a")]
    coverage = {"per_step": {"a": "covered"}, "procedure_scores": {}}
    low = _finding(concept_key="a", confidence=0.1, signature="misc.dup", evidence_span="low")
    high = _finding(concept_key="a", confidence=0.3, signature="misc.dup", evidence_span="high")

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 0.2},
        detection_outcome=_outcome(low, high),
    )

    # Only ONE dock, at confidence 0.3 (not 0.1+0.3): share = 0.2 * 0.3 = 0.06.
    assert result.misconception_dock == pytest.approx(0.06)
    topic = result.topics[0]
    assert len(topic.misconceptions) == 1
    assert topic.misconceptions[0].evidence_span == "high"


def test_distinct_canonical_keys_both_dock():
    """Two DIFFERENT canonical_keys both contribute (not deduped away);
    centrality kept low enough that the combined share stays under
    SEVERITY_CLAMP so this test isolates "distinct keys both dock" from the
    clamp law (covered separately)."""
    refs = [_eq_node("a")]
    coverage = {"per_step": {"a": "covered"}, "procedure_scores": {}}
    f1 = _finding(concept_key="a", confidence=0.3, signature="misc.one")
    f2 = _finding(concept_key="a", confidence=0.2, signature="misc.two")

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 0.5},
        detection_outcome=_outcome(f1, f2),
    )

    # shares: 0.5*0.3 + 0.5*0.2 = 0.15 + 0.10 = 0.25 (< SEVERITY_CLAMP 0.30)
    assert result.misconception_dock == pytest.approx(0.25)
    assert len(result.topics[0].misconceptions) == 2


def test_localized_dock_is_capped_by_the_topic_contribution():
    refs = [_eq_node("a")]
    coverage = {"per_step": {"a": "covered"}, "procedure_scores": {}}
    findings = tuple(
        _finding(concept_key="a", confidence=1.0, signature=f"misc.{i}") for i in range(5)
    )

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 1.0},
        detection_outcome=_outcome(*findings),
    )

    assert result.misconception_dock == pytest.approx(1.0)


def test_dock_points_scale_to_reconcile_with_localized_total():
    """When the clamp binds, per-finding dock_points are scaled
    proportionally so the "−N pts" lines a student sees sum EXACTLY to the
    dock actually subtracted — unscaled shares would over-claim. Raw shares
    here are 1.0*1.0 + 1.0*0.5 = 1.5 > SEVERITY_CLAMP, so each is scaled by
    clamp/1.5 while keeping their 2:1 ratio."""
    refs = [_eq_node("a")]
    coverage = {"per_step": {"a": "covered"}, "procedure_scores": {}}
    big = _finding(concept_key="a", confidence=1.0, signature="misc.big")
    small = _finding(concept_key="a", confidence=0.5, signature="misc.small")

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 1.0},
        detection_outcome=_outcome(big, small),
    )

    assert result.misconception_dock == pytest.approx(1.0)
    shown = {m.canonical_key: m.dock_points for m in result.topics[0].misconceptions}
    assert sum(shown.values()) == pytest.approx(result.misconception_dock)
    assert shown["misc.big"] == pytest.approx(shown["misc.small"] * 2)


def test_dock_points_unscaled_when_clamp_does_not_bind():
    """Below the clamp, dock_points are the raw shares untouched (scale=1)."""
    refs = [_eq_node("a")]
    coverage = {"per_step": {"a": "covered"}, "procedure_scores": {}}
    f1 = _finding(concept_key="a", confidence=0.3, signature="misc.one")

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 0.5},
        detection_outcome=_outcome(f1),
    )

    assert result.misconception_dock == pytest.approx(0.15)
    assert result.topics[0].misconceptions[0].dock_points == pytest.approx(0.15)


def test_score_floor_clamps_at_zero_when_dock_exceeds_coverage():
    """A near-zero coverage_component with the max dock never yields a
    negative score — clamp01 floors the pre-scale value at 0."""
    refs = [_eq_node("a")]
    coverage = {"per_step": {}, "procedure_scores": {}}  # missing -> credit 0
    findings = tuple(
        _finding(concept_key="a", confidence=1.0, signature=f"misc.{i}") for i in range(5)
    )

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 1.0},
        detection_outcome=_outcome(*findings),
    )

    assert result.coverage_component == 0.0
    assert result.misconception_dock == pytest.approx(0.0)
    assert result.score == 0
    assert result.letter == "F"


# --------------------------------------------------------------------------- #
# _general bucket: unmappable findings
# --------------------------------------------------------------------------- #
def test_unmappable_finding_goes_to_general_bucket():
    refs = [_eq_node("a")]
    coverage = {"per_step": {"a": "covered"}, "procedure_scores": {}}
    finding = _finding(concept_key="nonexistent_node", confidence=0.5, signature="misc.stray")

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 1.0},
        detection_outcome=_outcome(finding),
    )

    # The stray finding still docks (centrality falls back to CENTRALITY_W_MIN
    # since "nonexistent_node" is absent from the map).
    assert result.misconception_dock == pytest.approx(CENTRALITY_W_MIN * 0.5)

    keys = [t.canonical_key for t in result.topics]
    assert keys[-1] == "_general"
    general = result.topics[-1]
    assert general.credit == 0.0
    assert general.weight == 0.0
    assert general.status == "missing"
    assert general.display_name is None
    assert len(general.misconceptions) == 1
    assert general.misconceptions[0].canonical_key == "misc.stray"

    # The real topic "a" is untouched by the stray finding.
    topic_a = next(t for t in result.topics if t.canonical_key == "a")
    assert topic_a.misconceptions == ()


def test_no_general_bucket_when_all_findings_map_cleanly():
    refs = [_eq_node("a")]
    coverage = {"per_step": {"a": "covered"}, "procedure_scores": {}}
    finding = _finding(concept_key="a", confidence=0.5, signature="misc.x")

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"a": 1.0},
        detection_outcome=_outcome(finding),
    )

    keys = [t.canonical_key for t in result.topics]
    assert "_general" not in keys


# --------------------------------------------------------------------------- #
# Band mapping — reuses rubric.py::score_to_letter, no duplicated thresholds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "credit,expected_score,expected_letter",
    [
        (1.0, 100, "A+"),
        (0.97, 97, "A+"),
        (0.90, 90, "A"),
        (0.85, 85, "A-"),
        (0.80, 80, "B+"),
        (0.75, 75, "B"),
        (0.70, 70, "B-"),
        (0.65, 65, "C+"),
        (0.60, 60, "C"),
        (0.50, 50, "D"),
        (0.0, 0, "F"),
    ],
)
def test_band_mapping_matches_rubric_letter_bands(credit, expected_score, expected_letter):
    refs = [_proc_node("p1")]
    coverage = {"per_step": {"p1": "missing"}, "procedure_scores": {"p1": credit}}

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality={"p1": 1.0},
        detection_outcome=None,
    )

    assert result.score == expected_score
    assert result.letter == expected_letter
    # Cross-check against rubric.py's own mapping directly (no duplicated
    # thresholds in this module).
    assert result.letter == score_to_letter(result.score)


# --------------------------------------------------------------------------- #
# Multi-topic mixed scenario (integration-ish, still fully offline)
# --------------------------------------------------------------------------- #
def test_mixed_topics_full_partial_missing_with_dock():
    refs = [_eq_node("eq1"), _cond_node("c1"), _simp_node("s1"), _proc_node("p1")]
    coverage = {
        "per_step": {"eq1": "covered", "c1": "missing", "s1": "missing", "p1": "missing"},
        "procedure_scores": {"p1": 0.5},
    }
    centrality = {"eq1": 1.0, "c1": 0.8, "s1": 0.6, "p1": 0.4}
    finding = _finding(concept_key="eq1", confidence=0.5, signature="misc.sign_flip")

    result = compute_topic_score(
        coverage=coverage,
        reference_nodes=refs,
        centrality=centrality,
        detection_outcome=_outcome(finding),
    )

    assert len(result.topics) == 4
    by_key = {t.canonical_key: t for t in result.topics}
    assert by_key["eq1"].status == "covered"
    assert by_key["c1"].status == "missing"
    assert by_key["s1"].status == "missing"
    assert by_key["p1"].status == "partial"
    assert by_key["p1"].credit == pytest.approx(0.5)
    assert len(by_key["eq1"].misconceptions) == 1

    total_weight = sum(t.weight for t in result.topics)
    assert total_weight == pytest.approx(1.0)

    expected_coverage = (
        by_key["eq1"].weight * 1.0
        + by_key["c1"].weight * 0.0
        + by_key["s1"].weight * 0.0
        + by_key["p1"].weight * 0.5
    )
    assert result.coverage_component == pytest.approx(expected_coverage)
    expected_dock = by_key["eq1"].weight * 0.5
    assert result.misconception_dock == pytest.approx(expected_dock)
    expected_score = int(round(max(0.0, min(1.0, expected_coverage - expected_dock)) * 100))
    assert result.score == expected_score


# --------------------------------------------------------------------------- #
# Dataclass shape sanity (frozen, hashable-safe tuples)
# --------------------------------------------------------------------------- #
def test_dataclasses_are_frozen():
    misc = TopicMisconception(
        canonical_key="misc.x",
        resolved=False,
        dock_points=0.1,
        evidence_span=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        misc.dock_points = 0.5  # type: ignore[misc]

    credit = TopicCredit(
        canonical_key="a",
        display_name=None,
        credit=1.0,
        status="covered",
        weight=1.0,
        misconceptions=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        credit.credit = 0.5  # type: ignore[misc]

    result = TopicScoreResult(
        score=100,
        letter="A+",
        coverage_component=1.0,
        misconception_dock=0.0,
        topics=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.score = 0  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Private helper unit tests (direct, for full line coverage of edge branches
# that compute_topic_score's own guards make unreachable end-to-end)
# --------------------------------------------------------------------------- #
def test_finite_score_rejects_non_numeric_string():
    """A non-castable string degrades to 0.0 (float(raw) raises ValueError),
    mirroring rubric.py::_finite_score's own contract."""
    assert _finite_score("not-a-number") == 0.0


def test_finite_score_rejects_none():
    """None is not castable to float (raises TypeError) -> 0.0."""
    assert _finite_score(None) == 0.0


def test_finite_score_rejects_infinity():
    assert _finite_score(float("inf")) == 0.0
    assert _finite_score(float("-inf")) == 0.0


def test_finite_score_clamps_out_of_range_values():
    assert _finite_score(1.5) == 1.0
    assert _finite_score(-0.5) == 0.0


def test_weights_for_empty_node_ids_returns_empty_dict():
    assert _weights_for([], {}) == {}


def test_dedup_keeps_first_when_later_confidence_not_greater():
    """The ``elif finding.confidence > current.confidence`` branch's False
    arm: a later duplicate with EQUAL or LOWER confidence does not replace
    the kept instance."""
    first = ConceptFinding(
        concept_key="a",
        verdict="misconception",
        confidence=0.5,
        severity=0.0,
        evidence_span="first",
        signature="misc.dup",
        source="judge",
        corroborated=True,
        ceiling_eligible=False,
    )
    same_confidence = ConceptFinding(
        concept_key="a",
        verdict="misconception",
        confidence=0.5,
        severity=0.0,
        evidence_span="second",
        signature="misc.dup",
        source="judge",
        corroborated=True,
        ceiling_eligible=False,
    )
    lower_confidence = ConceptFinding(
        concept_key="a",
        verdict="misconception",
        confidence=0.2,
        severity=0.0,
        evidence_span="third",
        signature="misc.dup",
        source="judge",
        corroborated=True,
        ceiling_eligible=False,
    )

    result = _dedup_findings_by_canonical_key((first, same_confidence, lower_confidence))

    assert len(result) == 1
    assert result[0].evidence_span == "first"
