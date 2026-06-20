"""WU-4C2 §6.4 — rubric mapping (findings + reference graph -> compute_rubric input).

Pure unit tests. No resolver, no Neo4j, no LLM, no container. Every fixture is a
frozen in-memory object built from the shared ``_builders`` helpers + a tiny
local ``ReferenceGraph`` builder.

Pins the seam that bridges graph-sim ``CanonicalNode.canonical_key`` to the
FROZEN ``compute_rubric``'s ``r.node_id``/``r.node_type`` duck-type, and the
``misconception_scores`` 0.5/1.0 derivation (with the structurally-empty
``opposes_map`` of today and a SYNTHETIC opposes_map for the future 1.0 branch).
"""

from __future__ import annotations

from apollo.grading.audited_grade import AUDIT_UPGRADE_MESSAGE
from apollo.grading.rubric_mapping import (
    RubricRefNode,
    build_graph_sim_rubric,
    findings_to_rubric_input,
)
from apollo.grading.tests._builders import (
    audited,
    contradiction_finding,
    covered_finding,
    covered_finding_with_nodes,
    missing_finding,
)
from apollo.graph_compare.canonical import CanonicalNode, ReferenceGraph
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.overseer.rubric import compute_rubric


def _ref_node(canonical_key: str, node_type: str) -> CanonicalNode:
    """A frozen R_norm reference node (empty evidence; None method/confidence)."""
    return CanonicalNode(
        canonical_key=canonical_key,
        node_type=node_type,  # type: ignore[arg-type]
        source_node_ids=(canonical_key,),
        evidence_spans=(),
    )


def _ref_graph(*nodes: CanonicalNode) -> ReferenceGraph:
    return ReferenceGraph(nodes=tuple(nodes), edges=(), paths=())


def _audit_upgraded_covered(key: str) -> Finding:
    """A COVERED_NODE carrying the AUDIT_UPGRADE_MESSAGE marker (the transcript
    audit upgraded a missing reference key to covered)."""
    return Finding(
        kind=FindingKind.COVERED_NODE,
        canonical_key=key,
        evidence_spans=("quoted span",),
        confidence=0.75,
        message=AUDIT_UPGRADE_MESSAGE,
    )


def test_covered_finding_maps_per_step_covered():
    ref = _ref_graph(_ref_node("c1", "condition"))
    aud = audited((covered_finding("c1"),))
    out = findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert out.coverage["per_step"]["c1"] == "covered"


def test_missing_finding_maps_per_step_missing():
    ref = _ref_graph(_ref_node("c1", "condition"))
    aud = audited((missing_finding("c1"),))
    out = findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert out.coverage["per_step"]["c1"] == "missing"


def test_reference_key_with_no_finding_defaults_missing():
    ref = _ref_graph(_ref_node("c1", "condition"))
    aud = audited(())  # no findings at all
    out = findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert out.coverage["per_step"]["c1"] == "missing"


def test_procedure_score_covered_uses_confidence():
    ref = _ref_graph(_ref_node("p1", "procedure_step"), _ref_node("p2", "procedure_step"))
    aud = audited(
        (
            covered_finding("p1", confidence=0.92),
            Finding(kind=FindingKind.COVERED_NODE, canonical_key="p2", confidence=None),
        )
    )
    out = findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert out.coverage["procedure_scores"]["p1"] == 0.92
    assert out.coverage["procedure_scores"]["p2"] == 1.0


def test_procedure_score_missing_is_zero():
    ref = _ref_graph(_ref_node("p1", "procedure_step"))
    aud = audited((missing_finding("p1"),))
    out = findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert out.coverage["procedure_scores"]["p1"] == 0.0


def test_reference_nodes_are_rubricrefnodes_keyed_on_canonical_key():
    ref = _ref_graph(
        _ref_node("p1", "procedure_step"),
        _ref_node("c1", "condition"),
    )
    aud = audited((covered_finding("p1"), covered_finding("c1")))
    out = findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert all(isinstance(r, RubricRefNode) for r in out.reference_nodes)
    by_key = {r.node_id: r for r in out.reference_nodes}
    assert by_key["p1"].node_type == "procedure_step"
    assert by_key["c1"].node_type == "condition"
    # The duck-type passes the REAL frozen compute_rubric (reads only
    # .node_id / .node_type) without raising.
    rubric = compute_rubric(
        out.coverage,
        list(out.reference_nodes),
        misconception_scores=out.misconception_scores,
    )
    assert rubric["procedure"]["present"] is True
    assert rubric["justification"]["present"] is True


def test_misconception_detected_unresolved_scores_half():
    # The real today-state: opposes_map is structurally empty -> 0.5.
    ref = _ref_graph(_ref_node("c1", "condition"))
    aud = audited((contradiction_finding("misc.x", student_node_ids=("n1",)),))
    out = findings_to_rubric_input(
        audited=aud, reference_graph=ref, opposes_map={}, turn_order={"n1": 1}
    )
    assert out.misconception_scores["misc.x"] == 0.5


def test_misconception_resolved_scores_one_with_synthetic_opposes():
    # SYNTHETIC opposes_map: misc.x opposes cond.y, and a COVERED on cond.y came
    # LATER (turn 3) than the contradiction (turn 1) -> resolved -> 1.0.
    ref = _ref_graph(_ref_node("cond.y", "condition"))
    aud = audited(
        (
            contradiction_finding("misc.x", student_node_ids=("n1",)),
            covered_finding_with_nodes("cond.y", ("n2",)),
        )
    )
    out = findings_to_rubric_input(
        audited=aud,
        reference_graph=ref,
        opposes_map={"misc.x": "cond.y"},
        turn_order={"n1": 1, "n2": 3},
    )
    assert out.misconception_scores["misc.x"] == 1.0


def test_synthetic_opposes_but_covered_earlier_scores_half():
    # opposes_map resolves, but the covered came EARLIER (turn 1) than the
    # contradiction (turn 3) -> NOT resolved (misconception is the last word) -> 0.5.
    ref = _ref_graph(_ref_node("cond.y", "condition"))
    aud = audited(
        (
            contradiction_finding("misc.x", student_node_ids=("n1",)),
            covered_finding_with_nodes("cond.y", ("n2",)),
        )
    )
    out = findings_to_rubric_input(
        audited=aud,
        reference_graph=ref,
        opposes_map={"misc.x": "cond.y"},
        turn_order={"n1": 3, "n2": 1},
    )
    assert out.misconception_scores["misc.x"] == 0.5


def test_never_detected_misconception_absent():
    ref = _ref_graph(_ref_node("c1", "condition"))
    aud = audited((covered_finding("c1"),))
    out = findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert out.misconception_scores == {}


def test_build_graph_sim_rubric_matches_compute_rubric():
    ref = _ref_graph(
        _ref_node("p1", "procedure_step"),
        _ref_node("c1", "condition"),
        _ref_node("s1", "simplification"),
    )
    aud = audited(
        (
            covered_finding("p1", confidence=0.8),
            covered_finding("c1"),
            missing_finding("s1"),
            contradiction_finding("misc.x", student_node_ids=("n1",)),
        )
    )
    inp = findings_to_rubric_input(
        audited=aud, reference_graph=ref, opposes_map={}, turn_order={"n1": 1}
    )
    expected = compute_rubric(
        inp.coverage,
        list(inp.reference_nodes),
        misconception_scores=inp.misconception_scores,
    )
    got = build_graph_sim_rubric(
        audited=aud, reference_graph=ref, opposes_map={}, turn_order={"n1": 1}
    )
    assert got == expected


def test_audit_upgraded_covered_counts_as_covered():
    ref = _ref_graph(_ref_node("c1", "condition"))
    aud = audited((_audit_upgraded_covered("c1"),))
    out = findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert out.coverage["per_step"]["c1"] == "covered"


def test_empty_findings_yield_valid_input():
    ref = _ref_graph(
        _ref_node("p1", "procedure_step"),
        _ref_node("c1", "condition"),
    )
    aud = audited(())
    out = findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert out.coverage["per_step"] == {"p1": "missing", "c1": "missing"}
    assert out.misconception_scores == {}
    # compute_rubric does not crash on the all-missing input.
    rubric = compute_rubric(
        out.coverage,
        list(out.reference_nodes),
        misconception_scores=out.misconception_scores,
    )
    assert rubric["overall"]["score"] == 0


def test_diagnostic_only_findings_are_ignored():
    # An edge finding (canonical_key=None, non-contradiction) and an
    # unsupported_extra (keyed, but neither covered/missing/contradiction) both
    # contribute NOTHING to per_step / procedure_scores / misconception_scores.
    ref = _ref_graph(_ref_node("c1", "condition"))
    aud = audited(
        (
            Finding(kind=FindingKind.MATCHED_EDGE, message="a -USES-> b"),
            Finding(kind=FindingKind.UNSUPPORTED_EXTRA, canonical_key="x1"),
            covered_finding("c1"),
        )
    )
    out = findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert out.coverage["per_step"] == {"c1": "covered"}
    assert "x1" not in out.coverage["per_step"]
    assert out.misconception_scores == {}


def test_contradiction_with_none_key_is_skipped():
    # A CONTRADICTION finding with canonical_key=None contributes no axis entry
    # (defensive: a real contradiction always carries a misconception key).
    ref = _ref_graph(_ref_node("c1", "condition"))
    aud = audited(
        (
            Finding(kind=FindingKind.CONTRADICTION, canonical_key=None, student_node_ids=("n1",)),
            covered_finding("c1"),
        )
    )
    out = findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert out.misconception_scores == {}


def test_resolution_uses_sentinel_when_no_turn_ids():
    # opposes resolves, but BOTH the contradiction and the covered carry no
    # student_node_ids -> _turn_position returns the +inf sentinel for both ->
    # covered_turn is NOT strictly greater than contradiction_turn -> 0.5.
    ref = _ref_graph(_ref_node("cond.y", "condition"))
    aud = audited(
        (
            contradiction_finding("misc.x"),  # no student_node_ids
            covered_finding("cond.y"),  # covered_finding sets none either
        )
    )
    out = findings_to_rubric_input(
        audited=aud,
        reference_graph=ref,
        opposes_map={"misc.x": "cond.y"},
        turn_order={},
    )
    assert out.misconception_scores["misc.x"] == 0.5


def test_inputs_not_mutated():
    ref = _ref_graph(_ref_node("c1", "condition"))
    aud = audited((covered_finding("c1"),))
    ref_before = (ref.nodes, ref.edges, ref.paths)
    findings_before = aud.findings
    findings_to_rubric_input(audited=aud, reference_graph=ref, opposes_map={}, turn_order={})
    assert (ref.nodes, ref.edges, ref.paths) == ref_before
    assert aud.findings is findings_before
