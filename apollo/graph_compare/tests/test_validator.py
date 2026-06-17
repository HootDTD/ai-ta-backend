"""WU-4A1 Task 4 — raw-graph validation (the §6.4 step-4 + §6.6 gates).

Student side: ``validate_student_graph`` re-derives edge endpoint types from the
graph's node_index (parser edges may carry ``None`` types), checks
EDGE_ALLOWED_PAIRS, endpoint existence, attempt_id uniformity, and no PRECEDES
cycle. Reference side: ``validate_reference`` DELEGATES to the WU-3B
``validate_reference_graph`` (REUSE, never reimplement §6.1) and re-raises a
failure as :class:`ReferenceGraphInvalidError`.

All in-memory; no DB, no LLM. The illegal-endpoint test deliberately bypasses
the ``Edge`` Pydantic pair-validator by leaving both type fields ``None`` so the
bad pair reaches ``validate_student_graph``'s own re-derivation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apollo.graph_compare.validator import (
    ReferenceGraphInvalidError,
    StudentGraphInvalidError,
    validate_reference,
    validate_student_graph,
)
from apollo.ontology.edges import Edge, EdgeType
from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import build_node

_BERNOULLI = (
    Path(__file__).resolve().parents[2]
    / "subjects"
    / "fluid_mechanics"
    / "concepts"
    / "bernoulli_principle"
)


def _node(node_id, node_type, content, *, attempt_id=1):
    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content=content,
    )


def _edge(edge_type, frm, to, *, attempt_id=1, from_type=None, to_type=None):
    return Edge(
        edge_type=edge_type,
        from_node_id=frm,
        to_node_id=to,
        attempt_id=attempt_id,
        from_node_type=from_type,
        to_node_type=to_type,
    )


def _load_problem01() -> dict:
    return json.loads(
        (_BERNOULLI / "problems" / "problem_01.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# Student-graph validation
# ---------------------------------------------------------------------------


def test_valid_student_graph_passes():
    """proc PRECEDES proc, proc USES equation, condition SCOPES equation —
    a well-formed graph returns None (no raise)."""
    graph = KGGraph(
        nodes=[
            _node("p1", "procedure_step", {"action": "first", "purpose": ""}),
            _node("p2", "procedure_step", {"action": "second", "purpose": ""}),
            _node("e1", "equation", {"symbolic": "A1*v1 = A2*v2", "label": "", "variables": []}),
            _node("c1", "condition", {"applies_when": "density is constant", "label": ""}),
        ],
        edges=[
            _edge(EdgeType.PRECEDES, "p1", "p2"),
            _edge(EdgeType.USES, "p1", "e1"),
            _edge(EdgeType.SCOPES, "c1", "e1"),
        ],
    )
    assert validate_student_graph(graph) is None


def test_illegal_edge_endpoint_raises():
    """An ``equation SCOPES condition`` edge violates EDGE_ALLOWED_PAIRS (SCOPES
    must originate from condition/simplification). The bad edge is built with
    BOTH type fields None so the Edge Pydantic validator does NOT reject it at
    construction — the bad pair must reach validate_student_graph, which
    re-derives the endpoint types from the graph and rejects it."""
    graph = KGGraph(
        nodes=[
            _node("e1", "equation", {"symbolic": "A1*v1 = A2*v2", "label": "", "variables": []}),
            _node("c1", "condition", {"applies_when": "density is constant", "label": ""}),
        ],
        edges=[_edge(EdgeType.SCOPES, "e1", "c1")],  # equation -> condition: illegal
    )
    with pytest.raises(StudentGraphInvalidError) as exc:
        validate_student_graph(graph)
    assert any("SCOPES" in r for r in exc.value.reasons)


def test_edge_endpoint_missing_to_node_raises():
    """An edge referencing a to_node_id absent from nodes -> error."""
    graph = KGGraph(
        nodes=[
            _node("p1", "procedure_step", {"action": "first", "purpose": ""}),
        ],
        edges=[_edge(EdgeType.PRECEDES, "p1", "p_missing")],
    )
    with pytest.raises(StudentGraphInvalidError) as exc:
        validate_student_graph(graph)
    assert any("p_missing" in r for r in exc.value.reasons)


def test_edge_endpoint_missing_from_node_raises():
    """An edge referencing a from_node_id absent from nodes -> error (covers the
    from-endpoint branch distinctly from the to-endpoint branch)."""
    graph = KGGraph(
        nodes=[
            _node("p2", "procedure_step", {"action": "second", "purpose": ""}),
        ],
        edges=[_edge(EdgeType.PRECEDES, "p_missing", "p2")],
    )
    with pytest.raises(StudentGraphInvalidError) as exc:
        validate_student_graph(graph)
    assert any("p_missing" in r and "from_node_id" in r for r in exc.value.reasons)


def test_precedes_cycle_raises():
    """PRECEDES A->B and B->A is a cycle (caught from topological_order's
    ValueError)."""
    graph = KGGraph(
        nodes=[
            _node("p1", "procedure_step", {"action": "a", "purpose": ""}),
            _node("p2", "procedure_step", {"action": "b", "purpose": ""}),
        ],
        edges=[
            _edge(EdgeType.PRECEDES, "p1", "p2"),
            _edge(EdgeType.PRECEDES, "p2", "p1"),
        ],
    )
    with pytest.raises(StudentGraphInvalidError) as exc:
        validate_student_graph(graph)
    assert any("cycle" in r.lower() for r in exc.value.reasons)


def test_mixed_attempt_id_raises():
    """Nodes/edges carrying two different attempt_ids -> scoping-uniformity
    violation."""
    graph = KGGraph(
        nodes=[
            _node("p1", "procedure_step", {"action": "a", "purpose": ""}, attempt_id=1),
            _node("p2", "procedure_step", {"action": "b", "purpose": ""}, attempt_id=2),
        ],
        edges=[_edge(EdgeType.PRECEDES, "p1", "p2", attempt_id=1)],
    )
    with pytest.raises(StudentGraphInvalidError) as exc:
        validate_student_graph(graph)
    assert any("attempt" in r.lower() for r in exc.value.reasons)


def test_mixed_attempt_id_on_edge_raises():
    """An EDGE carrying a different attempt_id than the (uniform) nodes is also
    a scoping-uniformity violation (covers the edge branch of the check)."""
    graph = KGGraph(
        nodes=[
            _node("p1", "procedure_step", {"action": "a", "purpose": ""}, attempt_id=1),
            _node("p2", "procedure_step", {"action": "b", "purpose": ""}, attempt_id=1),
        ],
        edges=[_edge(EdgeType.PRECEDES, "p1", "p2", attempt_id=7)],
    )
    with pytest.raises(StudentGraphInvalidError) as exc:
        validate_student_graph(graph)
    assert any("attempt" in r.lower() for r in exc.value.reasons)


def test_empty_student_graph_passes():
    """A degenerate empty student graph is grammatically valid (it is scored
    0/1/0 in WU-4A2, not rejected at the grammar gate)."""
    assert validate_student_graph(KGGraph(nodes=[], edges=[])) is None


# ---------------------------------------------------------------------------
# Reference-graph validation (delegated)
# ---------------------------------------------------------------------------


def test_validate_reference_delegates_ok():
    """A fully-annotated problem_01.json passes (validate_reference_graph is pure
    — no DB)."""
    assert validate_reference(_load_problem01()) is None


def test_validate_reference_missing_entity_link_raises():
    """A reference step lacking entity_key -> ReferenceGraphInvalidError whose
    reasons are EXACTLY validate_reference_graph(...).errors (delegation, not a
    reimplementation)."""
    from apollo.persistence.learner_model_seed import validate_reference_graph

    problem = {
        "reference_solution": [
            {"id": "a", "entry_type": "equation", "content": {"symbolic": "x = y"}},
        ],
        "declared_paths": [["a"]],
    }
    with pytest.raises(ReferenceGraphInvalidError) as exc:
        validate_reference(problem)
    assert exc.value.reasons == validate_reference_graph(problem).errors
    assert exc.value.reasons  # non-empty


def test_validate_reference_undeclared_paths_raises():
    """Empty declared_paths -> ReferenceGraphInvalidError."""
    problem = {
        "reference_solution": [
            {"id": "a", "entry_type": "equation", "entity_key": "eq.a", "content": {"symbolic": "x = y"}},
        ],
        "declared_paths": [],
    }
    with pytest.raises(ReferenceGraphInvalidError) as exc:
        validate_reference(problem)
    assert any("declared_paths" in r for r in exc.value.reasons)


def test_student_graph_error_carries_reasons_tuple():
    """The named error carries a frozen reasons tuple (NO-FALLBACK convention:
    structured reason fields, raised loudly)."""
    err = StudentGraphInvalidError(reasons=("a", "b"))
    assert err.reasons == ("a", "b")
    assert isinstance(err, ValueError)


def test_reference_graph_error_carries_reasons_tuple():
    err = ReferenceGraphInvalidError(reasons=("x",))
    assert err.reasons == ("x",)
    assert isinstance(err, ValueError)
