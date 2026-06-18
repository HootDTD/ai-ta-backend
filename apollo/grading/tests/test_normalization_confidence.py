"""WU-4B3 — compute_normalization_confidence (§3 damper input) pure tests.

The §3 damper wants the honest WORST-CASE confidence of the evidence that
actually backed a SCORED finding (covered / contradiction). These tests pin the
MIN (weakest-link) semantics, the scored-vs-diagnostic finding distinction, and
the named empty-set floor knob. No container, no LLM — pure over `_builders`.
"""

from __future__ import annotations

import copy

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
    and a covered backed by a 0.75 node -> MIN == 0.75."""
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        covered_finding_with_nodes("k.b", ("b1",)),
    )
    resolution = resolution_with(resolved_nodes=(("a1", 0.92), ("b1", 0.75)))
    assert compute_normalization_confidence(audited(findings), resolution) == 0.75


def test_single_high_confidence_node():
    findings = (covered_finding_with_nodes("k.a", ("a1",)),)
    resolution = resolution_with(resolved_nodes=(("a1", 0.98),))
    assert compute_normalization_confidence(audited(findings), resolution) == 0.98


def test_contradiction_node_counts_as_scored():
    """A contradiction's backing node IS in the scored set: 0.80 contradiction +
    0.92 covered -> 0.80."""
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        contradiction_finding("misc.x", student_node_ids=("m1",)),
    )
    resolution = resolution_with(resolved_nodes=(("a1", 0.92), ("m1", 0.80)))
    assert compute_normalization_confidence(audited(findings), resolution) == 0.80


def test_unsupported_extra_does_not_lower():
    """An unsupported_extra at 0.10 is NOT scored -> excluded; the 0.92 covered
    wins."""
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        _unsupported_extra("k.x", ("x1",)),
    )
    resolution = resolution_with(resolved_nodes=(("a1", 0.92), ("x1", 0.10)))
    assert compute_normalization_confidence(audited(findings), resolution) == 0.92


def test_unresolved_node_excluded():
    """An unresolved finding has no resolved backer -> never pulls the min to 0.0."""
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        _unresolved("u1"),
    )
    resolution = resolution_with(unresolved=1, resolved_nodes=(("a1", 0.92),))
    assert compute_normalization_confidence(audited(findings), resolution) == 0.92


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


def test_pure_no_mutation():
    findings = (
        covered_finding_with_nodes("k.a", ("a1",)),
        contradiction_finding("misc.x", student_node_ids=("m1",)),
    )
    graded = audited(findings)
    resolution = resolution_with(resolved_nodes=(("a1", 0.92), ("m1", 0.80)))
    graded_copy = copy.deepcopy(graded)
    resolution_copy = copy.deepcopy(resolution)

    compute_normalization_confidence(graded, resolution)

    assert graded == graded_copy
    assert resolution == resolution_copy
