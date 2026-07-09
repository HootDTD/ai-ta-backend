"""WU-4B3 — compute_normalization_confidence (§3 damper input) pure tests.

The §3 damper wants the honest WORST-CASE confidence of the evidence that
actually backed a SCORED finding (covered / contradiction). These tests pin the
MIN (weakest-link) semantics, the scored-vs-diagnostic finding distinction, and
the named empty-set floor knob. No container, no LLM — pure over `_builders`.
"""

from __future__ import annotations

import copy

import pytest

from apollo.grading.abstention import ABSTENTION_THRESHOLDS
from apollo.grading.normalization_confidence import (
    NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES,
    compute_normalization_confidence,
)
from apollo.grading.tests._builders import (
    audited,
    contradiction_finding,
    covered_finding_with_nodes,
    resolution_with,
)
from apollo.graph_compare.findings import Finding, FindingKind


def _missing_finding(key: str) -> Finding:
    return Finding(kind=FindingKind.MISSING_NODE, canonical_key=key, score=0.0)


def _unsupported_extra(key: str, nids: tuple[str, ...]) -> Finding:
    return Finding(
        kind=FindingKind.UNSUPPORTED_EXTRA,
        canonical_key=key,
        student_node_ids=nids,
    )


def _unresolved(node_id: str) -> Finding:
    return Finding(kind=FindingKind.UNRESOLVED, student_node_ids=(node_id,))


def test_min_over_scored_finding_backers():
    """THE binding weakest-link discriminator: a covered backed by a 0.92 node
    and a covered backed by a 0.75 node -> MIN == 0.75. Both nodes are equations
    (ceiling 1.00), so type-normalization is the identity (0.92->0.92, 0.75->0.75)
    and the weakest-link MIN is unchanged."""
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        covered_finding_with_nodes("k.b", ("b1",)),
    )
    resolution = resolution_with(resolved_nodes=(("a1", 0.92), ("b1", 0.75)))
    node_types = {"a1": "equation", "b1": "equation"}
    assert compute_normalization_confidence(audited(findings), resolution, node_types) == 0.75


def test_single_high_confidence_node():
    """An equation @ symbolic cap (0.98), ceiling 1.00 -> 0.98/1.00 = 0.98."""
    findings = (covered_finding_with_nodes("k.a", ("a1",)),)
    resolution = resolution_with(resolved_nodes=(("a1", 0.98),))
    assert compute_normalization_confidence(audited(findings), resolution, {"a1": "equation"}) == 0.98


def test_contradiction_node_counts_as_scored():
    """A contradiction's backing node IS in the scored set: 0.80 contradiction +
    0.92 covered -> 0.80. Both equations (ceiling 1.00 -> normalize identically)
    so the scored-set membership, not normalization, is what this pins."""
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        contradiction_finding("misc.x", student_node_ids=("m1",)),
    )
    resolution = resolution_with(resolved_nodes=(("a1", 0.92), ("m1", 0.80)))
    node_types = {"a1": "equation", "m1": "equation"}
    assert compute_normalization_confidence(audited(findings), resolution, node_types) == 0.80


def test_unsupported_extra_does_not_lower():
    """An unsupported_extra at 0.10 is NOT scored -> excluded; the 0.92 covered
    (an equation, ceiling 1.00 -> 0.92) wins."""
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        _unsupported_extra("k.x", ("x1",)),
    )
    resolution = resolution_with(resolved_nodes=(("a1", 0.92), ("x1", 0.10)))
    assert compute_normalization_confidence(audited(findings), resolution, {"a1": "equation"}) == 0.92


def test_unresolved_node_excluded():
    """An unresolved finding has no resolved backer -> never pulls the min to 0.0.
    The covered equation (ceiling 1.00 -> 0.92) is the only scored backer."""
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        _unresolved("u1"),
    )
    resolution = resolution_with(unresolved=1, resolved_nodes=(("a1", 0.92),))
    assert compute_normalization_confidence(audited(findings), resolution, {"a1": "equation"}) == 0.92


def test_no_scored_findings_returns_floor():
    """A pure-missing grade (no covered/contradiction backer) -> the floor knob."""
    findings = (_missing_finding("k.a"), _missing_finding("k.b"))
    resolution = resolution_with(resolved=0, unresolved=0)
    assert NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES == 1.0
    assert (
        compute_normalization_confidence(audited(findings), resolution)
        == NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES
    )


def test_scored_finding_with_no_resolved_backer_falls_to_floor():
    """A covered whose backing node id is NOT in resolution -> no scored backer
    found -> the floor (defensive: a real covered always carries a resolution)."""
    findings = (covered_finding_with_nodes("k.a", ("ghost",)),)
    resolution = resolution_with(resolved_nodes=(("a1", 0.92),))
    assert (
        compute_normalization_confidence(audited(findings), resolution)
        == NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES
    )


def test_helper_matches_public_function():
    """The extracted pure helper computes the IDENTICAL value the public
    delegator returns over the same findings -> proves the refactor is value-
    preserving (the persisted nc must stay byte-identical)."""
    from apollo.grading.normalization_confidence import _normalization_confidence_over

    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        covered_finding_with_nodes("k.b", ("b1",)),
    )
    resolution = resolution_with(resolved_nodes=(("a1", 0.92), ("b1", 0.75)))
    node_types = {"a1": "equation", "b1": "equation"}
    assert _normalization_confidence_over(findings, resolution, node_types) == 0.75
    assert compute_normalization_confidence(audited(findings), resolution, node_types) == 0.75


def test_pure_no_mutation():
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        contradiction_finding("misc.x", student_node_ids=("m1",)),
    )
    graded = audited(findings)
    resolution = resolution_with(resolved_nodes=(("a1", 0.92), ("m1", 0.80)))
    node_types = {"a1": "equation", "m1": "equation"}
    graded_copy = copy.deepcopy(graded)
    resolution_copy = copy.deepcopy(resolution)
    node_types_copy = copy.deepcopy(node_types)

    compute_normalization_confidence(graded, resolution, node_types)

    assert graded == graded_copy
    assert resolution == resolution_copy
    assert node_types == node_types_copy  # the threaded map is read, never mutated


# --- G1 fix: type-aware normalization (judge each cap against its type ceiling) --
#
# RESOLUTION_CEILING_BY_TYPE/{equation: 1.00} + RESOLUTION_CEILING_DEFAULT (0.75)
# normalize each scored node's per-tier cap BEFORE the MIN: an equation that falls
# to a weak tier is suspicious (it had exact/symbolic/derived available); a
# conceptual node resolving via llm is at its realistic ceiling, not suspicious.


def test_resolution_ceiling_constants():
    """The two calibration knobs the type-aware fix introduces: equations can
    reach the exact tier (1.00); prose nodes bottom out at the llm tier (0.75)."""
    from apollo.grading.normalization_confidence import (
        RESOLUTION_CEILING_BY_TYPE,
        RESOLUTION_CEILING_DEFAULT,
    )

    assert RESOLUTION_CEILING_BY_TYPE == {"equation": 1.00}
    assert RESOLUTION_CEILING_DEFAULT == 0.75


def test_type_normalized_confidence_helper():
    """The pure per-node normalizer: cap / type-ceiling, clamped to 1.0."""
    from apollo.grading.normalization_confidence import _type_normalized_confidence

    # equation ceiling 1.00 -> raw cap passes through unchanged.
    assert _type_normalized_confidence("equation", 1.00) == 1.0
    assert _type_normalized_confidence("equation", 0.75) == 0.75
    # prose ceiling 0.75 -> llm cap reaches its ceiling (1.0); fuzzy clamps to 1.0.
    assert _type_normalized_confidence("definition", 0.75) == 1.0
    assert _type_normalized_confidence("condition", 0.80) == 1.0
    # an unknown type defaults to the prose ceiling (0.75).
    assert _type_normalized_confidence("", 0.75) == 1.0


def test_equation_resolved_exact_is_full_confidence():
    """equation @ exact (1.00) -> 1.00/1.00 = 1.0."""
    findings = (covered_finding_with_nodes("eq.a", ("a1",)),)
    resolution = resolution_with(resolved_nodes=(("a1", 1.00),))
    nc = compute_normalization_confidence(audited(findings), resolution, {"a1": "equation"})
    assert nc == 1.0


def test_equation_resolved_via_llm_is_below_floor():
    """equation @ llm (0.75) -> 0.75/1.00 = 0.75 < 0.85: an equation that fell to
    llm IS suspicious (it had exact/symbolic/derived paths) -> contributes an
    abstention."""
    findings = (covered_finding_with_nodes("eq.a", ("a1",)),)
    resolution = resolution_with(resolved_nodes=(("a1", 0.75),))
    nc = compute_normalization_confidence(audited(findings), resolution, {"a1": "equation"})
    assert nc == 0.75
    assert nc < ABSTENTION_THRESHOLDS["min_normalization_confidence"]


@pytest.mark.parametrize(
    "node_type", ["procedure_step", "condition", "definition", "simplification", "variable_mapping"]
)
def test_conceptual_node_via_llm_does_not_abstain(node_type):
    """THE G1 fix: a conceptual node @ llm (0.75) -> 0.75/0.75 = 1.0 >= 0.85.
    Prose nodes can ONLY reach the llm tier in production, so llm is their
    ceiling, not a red flag -> must NOT trip the abstention floor."""
    findings = (covered_finding_with_nodes("k.a", ("a1",)),)
    resolution = resolution_with(resolved_nodes=(("a1", 0.75),))
    nc = compute_normalization_confidence(audited(findings), resolution, {"a1": node_type})
    assert nc == 1.0
    assert nc >= ABSTENTION_THRESHOLDS["min_normalization_confidence"]


def test_conceptual_node_via_fuzzy_clamps_to_one():
    """conceptual node @ fuzzy (0.80) -> min(1.0, 0.80/0.75) = 1.0 (no abstain)."""
    findings = (covered_finding_with_nodes("k.a", ("a1",)),)
    resolution = resolution_with(resolved_nodes=(("a1", 0.80),))
    nc = compute_normalization_confidence(audited(findings), resolution, {"a1": "condition"})
    assert nc == 1.0


def test_strong_attempt_mixed_equation_exact_and_concept_llm_is_one():
    """A strong attempt: equation @ exact (1.0) + conceptual @ llm (1.0 after
    normalization) -> nc = min(1.0, 1.0) = 1.0 -> no normalization-gate abstain."""
    findings = (
        covered_finding_with_nodes("eq.a", ("a1",)),
        covered_finding_with_nodes("k.b", ("b1",)),
    )
    resolution = resolution_with(resolved_nodes=(("a1", 1.00), ("b1", 0.75)))
    nc = compute_normalization_confidence(
        audited(findings), resolution, {"a1": "equation", "b1": "definition"}
    )
    assert nc == 1.0


def test_weakest_link_is_min_of_type_normalized_values():
    """MIN (weakest-link) is preserved, but over the TYPE-NORMALIZED values: a
    conceptual node @ llm normalizes to 1.0 while an equation @ llm stays 0.75,
    so the equation is the weak link -> MIN 0.75 (the conceptual node no longer
    sinks the attempt; the equation correctly does)."""
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),  # conceptual @ llm -> 1.0
        covered_finding_with_nodes("eq.b", ("b1",)),  # equation @ llm -> 0.75
    )
    resolution = resolution_with(resolved_nodes=(("a1", 0.75), ("b1", 0.75)))
    nc = compute_normalization_confidence(
        audited(findings), resolution, {"a1": "definition", "b1": "equation"}
    )
    assert nc == 0.75


def test_unknown_node_type_uses_prose_ceiling():
    """A scored backing node absent from the type map (or unmapped type) defaults
    to the prose ceiling (0.75): an unmapped 0.92 node -> min(1.0, 0.92/0.75) =
    1.0. The permissive default (treat-as-prose) keeps the gate from falsely
    abstaining when a node's type is simply unavailable."""
    findings = (covered_finding_with_nodes("k.a", ("a1",)),)
    resolution = resolution_with(resolved_nodes=(("a1", 0.92),))
    nc = compute_normalization_confidence(audited(findings), resolution, {})  # empty map
    assert nc == 1.0


def test_no_scored_backers_floor_holds_with_type_map():
    """The neutral floor is unchanged: a pure-missing grade has no scored backer,
    so nc = 1.0 regardless of the threaded type map."""
    findings = (_missing_finding("k.a"),)
    resolution = resolution_with(resolved=0, unresolved=0)
    nc = compute_normalization_confidence(audited(findings), resolution, {"a1": "equation"})
    assert nc == NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES
