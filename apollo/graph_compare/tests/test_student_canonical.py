"""WU-4A1 Task 6 — build_student_canonical (S_norm).

The three at-risk branches each get a dedicated test (§6.3):
  * MERGE — two student nodes resolving to the same key collapse into ONE node
    carrying both raw node-ids + evidence spans;
  * RETAIN — an unresolved student node is NOT a CanonicalNode but APPEARS in
    unresolved_nodes (findings-input, not dropped);
  * DROP — an edge incident to an unresolved node is dropped (counted), an edge
    between two resolved nodes is kept and normalized to canonical endpoints.

All inputs are hand-built (frozen ResolutionResult + KGGraph); the resolver is
NEVER run here, so no LLM/network is involved at all.
"""

from __future__ import annotations

from apollo.graph_compare.canonical import build_student_canonical
from apollo.ontology.edges import Edge, EdgeType
from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import build_node
from apollo.resolution.result import ResolutionResult, ResolvedNode


def _node(node_id, node_type, content, *, attempt_id=1):
    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content=content,
    )


def _edge(edge_type, frm, to, *, attempt_id=1, provenance="explicit"):
    return Edge(
        edge_type=edge_type,
        from_node_id=frm,
        to_node_id=to,
        attempt_id=attempt_id,
        provenance=provenance,
    )


def _resolved(node_id, key, method="alias", conf=0.92):
    return ResolvedNode(
        node_id=node_id,
        resolution="resolved",
        resolved_key=key,
        resolved_canon_key=None,
        method=method,
        confidence=conf,
    )


def _unresolved(node_id):
    return ResolvedNode(
        node_id=node_id,
        resolution="unresolved",
        resolved_key=None,
        resolved_canon_key=None,
        method="unresolved",
        confidence=0.0,
    )


def _resolution(*rns) -> ResolutionResult:
    return ResolutionResult(resolved=tuple(rns), tier_counts={}, llm_calls=0)


def test_two_nodes_same_target_merge_into_one_node():
    """KEY (§6.3): two student nodes both resolving to cond.incompressibility ->
    ONE CanonicalNode with source_node_ids == ('d1','d2') (sorted) and
    evidence_spans carrying BOTH surface texts."""
    graph = KGGraph(
        nodes=[
            _node("d1", "condition", {"applies_when": "density stays the same", "label": ""}),
            _node("d2", "condition", {"applies_when": "constant density", "label": ""}),
        ],
        edges=[],
    )
    resolution = _resolution(
        _resolved("d1", "cond.incompressibility"),
        _resolved("d2", "cond.incompressibility"),
    )
    s = build_student_canonical(graph, resolution)
    assert len(s.nodes) == 1
    node = s.nodes[0]
    assert node.canonical_key == "cond.incompressibility"
    assert node.source_node_ids == ("d1", "d2")
    assert set(node.evidence_spans) == {"density stays the same", "constant density"}


def test_unresolved_node_retained_not_dropped():
    """KEY (§6.3): an unresolved student node is NOT a CanonicalNode but APPEARS
    in unresolved_nodes as (node_id, surface_text)."""
    graph = KGGraph(
        nodes=[
            _node("u1", "equation", {"symbolic": "garbled = ?", "label": "", "variables": []}),
        ],
        edges=[],
    )
    s = build_student_canonical(graph, _resolution(_unresolved("u1")))
    assert s.nodes == ()
    assert s.unresolved_nodes == (("u1", "garbled = ?"),)


def test_unresolved_incident_edges_dropped():
    """KEY (§6.3): an edge incident to an unresolved node is dropped from edges
    and counted; an edge between two resolved nodes is kept."""
    graph = KGGraph(
        nodes=[
            _node("p1", "procedure_step", {"action": "a", "purpose": ""}),
            _node("p2", "procedure_step", {"action": "b", "purpose": ""}),
            _node("p3", "procedure_step", {"action": "c", "purpose": ""}),
        ],
        edges=[
            _edge(EdgeType.PRECEDES, "p1", "p2"),  # both resolved -> kept
            _edge(EdgeType.PRECEDES, "p2", "p3"),  # p3 unresolved -> dropped
        ],
    )
    resolution = _resolution(
        _resolved("p1", "proc.a"),
        _resolved("p2", "proc.b"),
        _unresolved("p3"),
    )
    s = build_student_canonical(graph, resolution)
    assert s.dropped_edge_count == 1
    assert len(s.edges) == 1
    assert (s.edges[0].from_key, s.edges[0].to_key) == ("proc.a", "proc.b")


def test_resolved_edge_endpoints_normalized_to_keys():
    """A PRECEDES edge p1->p2 (p1->proc.a, p2->proc.b) becomes
    CanonicalEdge(PRECEDES, proc.a, proc.b) carrying the student edge's
    provenance."""
    graph = KGGraph(
        nodes=[
            _node("p1", "procedure_step", {"action": "a", "purpose": ""}),
            _node("p2", "procedure_step", {"action": "b", "purpose": ""}),
        ],
        edges=[_edge(EdgeType.PRECEDES, "p1", "p2", provenance="inferred")],
    )
    resolution = _resolution(_resolved("p1", "proc.a"), _resolved("p2", "proc.b"))
    s = build_student_canonical(graph, resolution)
    assert len(s.edges) == 1
    edge = s.edges[0]
    assert edge.edge_type == EdgeType.PRECEDES
    assert (edge.from_key, edge.to_key) == ("proc.a", "proc.b")
    assert edge.provenance == "inferred"


def test_merge_carries_highest_confidence_method_deterministically():
    """Two nodes resolving to the same key via different methods (alias 0.92 vs
    fuzzy 0.80) -> the merged node's method/confidence is the higher-confidence
    one, deterministic across runs."""
    graph = KGGraph(
        nodes=[
            _node("c1", "condition", {"applies_when": "constant density", "label": ""}),
            _node("c2", "condition", {"applies_when": "density is unchanging", "label": ""}),
        ],
        edges=[],
    )
    resolution = _resolution(
        _resolved("c1", "cond.incompressibility", method="fuzzy", conf=0.80),
        _resolved("c2", "cond.incompressibility", method="alias", conf=0.92),
    )
    s = build_student_canonical(graph, resolution)
    assert len(s.nodes) == 1
    assert s.nodes[0].method == "alias"
    assert s.nodes[0].confidence == 0.92


def test_equation_merge_carries_symbolic_surface():
    """A merged equation node carries the (first member's) symbolic surface text;
    a non-equation node carries None symbolic."""
    graph = KGGraph(
        nodes=[
            _node("e1", "equation", {"symbolic": "A1*v1 = A2*v2", "label": "", "variables": []}),
        ],
        edges=[],
    )
    s = build_student_canonical(graph, _resolution(_resolved("e1", "eq.continuity")))
    assert s.nodes[0].symbolic == "A1*v1 = A2*v2"


def test_empty_student_graph_yields_empty_canonical():
    """Empty KGGraph + empty ResolutionResult -> empty tuples, drop count 0."""
    s = build_student_canonical(KGGraph(nodes=[], edges=[]), _resolution())
    assert s.nodes == ()
    assert s.edges == ()
    assert s.unresolved_nodes == ()
    assert s.dropped_edge_count == 0


def test_builder_is_pure_same_input_same_output():
    """Two calls on identical inputs produce equal CanonicalGraphs (immutable,
    deterministic ordering — node ids/keys are intentionally out of sorted order
    in the input to exercise the sort)."""
    graph = KGGraph(
        nodes=[
            _node("d2", "condition", {"applies_when": "constant density", "label": ""}),
            _node("d1", "condition", {"applies_when": "density stays the same", "label": ""}),
        ],
        edges=[_edge(EdgeType.DEPENDS_ON, "d1", "d2")],
    )
    resolution = _resolution(
        _resolved("d1", "cond.b"),
        _resolved("d2", "cond.a"),
    )
    a = build_student_canonical(graph, resolution)
    b = build_student_canonical(graph, resolution)
    assert a == b
    # Nodes ordered by canonical_key deterministically (cond.a before cond.b).
    assert [n.canonical_key for n in a.nodes] == ["cond.a", "cond.b"]


def test_unresolved_nodes_sorted_deterministically():
    """Multiple unresolved nodes are returned in a deterministic (sorted) order."""
    graph = KGGraph(
        nodes=[
            _node("z9", "equation", {"symbolic": "z = 9", "label": "", "variables": []}),
            _node("a1", "equation", {"symbolic": "a = 1", "label": "", "variables": []}),
        ],
        edges=[],
    )
    s = build_student_canonical(graph, _resolution(_unresolved("z9"), _unresolved("a1")))
    assert [nid for nid, _ in s.unresolved_nodes] == ["a1", "z9"]
