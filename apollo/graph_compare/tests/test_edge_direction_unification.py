"""DAG-0 regression replay — DEPENDS_ON direction unification (2026-07-12).

This is the post-fix regression form of the DAG-0 hypothesis script. It replays
REAL captured attempts through the REAL normalization code and asserts the
unified prerequisite -> dependent semantics. No mocks of the code under test.

Data provenance (all real, captured 2026-07-12):
- Reference problem ``bernoulli_height_change_find_v2``: staging Supabase
  (hjevtxdt...) ``apollo_concept_problems`` id=2 payload.
- Student graphs: local Docker Neo4j ``apollo-neo4j`` (the store the overnight
  E2E runs graded against; staging ``apollo_graph_comparison_runs`` rows 1-3
  are attempts 4/5/7 in this store).
- Resolution: the resolver's persisted per-node ``resolution``/``resolved_key``
  fields from the same Neo4j store (RESOLVES_TO audit trail).
- Pre-fix finding counts were cross-checked against staging
  ``apollo_graph_comparison_findings`` for run_id 1 and 2. Post-fix, attempt 4
  still has one DEPENDS_ON tuple match, but credit is sourced from the student's
  semantically correct raw parser edge; attempt 5 remains at zero.

Non-equation node contents are minimal placeholders — canonical edge identity
is (edge_type, from_key, to_key); node content never enters edge matching.

Original capture: repository-root ``hypothesis_dag0_edge_direction.py``.
"""

from __future__ import annotations

from apollo.graph_compare.canonical import (
    build_reference_canonical,
    build_student_canonical,
)
from apollo.graph_compare.core import _edge_findings
from apollo.ontology.edges import Edge, EdgeType
from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import build_node
from apollo.resolution.result import ResolutionResult, ResolvedNode

# --------------------------------------------------------------------------- #
# Reference problem — staging apollo_concept_problems id=2 (verbatim fields
# build_reference_canonical reads: reference_solution + declared_paths).
# --------------------------------------------------------------------------- #
PROBLEM = {
    "id": "bernoulli_height_change_find_v2",
    "reference_solution": [
        {
            "id": "bernoulli",
            "step": 1,
            "content": {
                "label": "Bernoulli's equation",
                "symbolic": "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)",
                "variables": ["P1", "rho", "v1", "g", "h1", "P2", "v2", "h2"],
            },
            "depends_on": [],
            "entity_key": "eq.bernoulli",
            "entry_type": "equation",
        },
        {
            "id": "equal_pressure_simplification",
            "step": 2,
            "content": {"applies_when": "P1 == P2", "transformation": "pressure terms cancel"},
            "depends_on": ["bernoulli"],
            "entity_key": "simp.equal_pressure_simplification",
            "entry_type": "simplification",
        },
        {
            "id": "plan_apply_equal_pressure_simplification",
            "step": 3,
            "content": {
                "order": 1,
                "action": "recognize that both ends are open to the atmosphere so P1 equals P2, causing the pressure terms in bernoulli to cancel",
                "purpose": "eliminate P1 and P2 from bernoulli, leaving only the kinetic and potential energy terms",
                "uses_equations": ["bernoulli"],
            },
            "depends_on": ["bernoulli", "equal_pressure_simplification"],
            "entity_key": "proc.plan_apply_equal_pressure_simplification",
            "entry_type": "procedure_step",
        },
        {
            "id": "plan_set_v1_zero_and_solve_bernoulli",
            "step": 4,
            "content": {
                "order": 2,
                "action": "substitute v1 = 0 (wide reservoir approximation), h1 = 20, h2 = 0, and g = 9.81 into the simplified bernoulli and solve for v2",
                "purpose": "obtain the exit velocity v2 at the bottom of the pipe",
                "uses_equations": ["bernoulli"],
            },
            "depends_on": ["plan_apply_equal_pressure_simplification"],
            "entity_key": "proc.plan_set_v1_zero_and_solve_bernoulli",
            "entry_type": "procedure_step",
        },
    ],
    "declared_paths": [
        [
            "bernoulli",
            "equal_pressure_simplification",
            "plan_apply_equal_pressure_simplification",
            "plan_set_v1_zero_and_solve_bernoulli",
        ]
    ],
}

# --------------------------------------------------------------------------- #
# Attempt 4 (staging comparison run 1 — the ONLY matched_edge in staging).
# Nodes/edges: local Neo4j attempt_id=4. Resolution: persisted resolver fields.
# --------------------------------------------------------------------------- #
_A4 = 4
ATTEMPT4_NODES = [
    (
        "stu_d348d3c91c94",
        "definition",
        {"concept": "bernoulli", "meaning": "energy conservation along a streamline"},
    ),
    ("stu_a13a91e6b7b1", "condition", {"applies_when": "steady flow"}),
    (
        "stu_770825200223",
        "equation",
        {"symbolic": "P + Rational(1,2)*rho*v**2 + rho*g*h - constant"},
    ),
    ("stu_a099691b4914", "equation", {"symbolic": "P + Rational(1,2)*rho*v**2 - constant"}),
    ("stu_3505a649dd21", "condition", {"applies_when": "incompressible"}),
    (
        "stu_8c6af8e04826",
        "simplification",
        {"applies_when": "P1 == P2", "transformation": "pressure terms cancel"},
    ),
    ("stu_9f3bddc2f683", "equation", {"symbolic": "A1*v1 - A2*v2"}),
    ("stu_7045c7699e95", "condition", {"applies_when": "along a streamline"}),
    (
        "stu_0820e52a924e",
        "definition",
        {"concept": "pressure-velocity", "meaning": "pressure and velocity move together"},
    ),
    (
        "stu_fbab0a848253",
        "equation",
        {"symbolic": "P1 + Rational(1,2)*rho*v1**2 - (P2 + Rational(1,2)*rho*v2**2)"},
    ),
    ("stu_0ac12190d250", "procedure_step", {"action": "set v1 to zero and solve for v2"}),
]
ATTEMPT4_EDGES = [
    ("stu_7045c7699e95", EdgeType.SCOPES, "stu_770825200223"),
    ("stu_a13a91e6b7b1", EdgeType.SCOPES, "stu_770825200223"),
    ("stu_8c6af8e04826", EdgeType.DEPENDS_ON, "stu_770825200223"),
    ("stu_8c6af8e04826", EdgeType.SCOPES, "stu_770825200223"),
    ("stu_3505a649dd21", EdgeType.SCOPES, "stu_770825200223"),
    ("stu_0ac12190d250", EdgeType.DEPENDS_ON, "stu_a099691b4914"),
    ("stu_7045c7699e95", EdgeType.SCOPES, "stu_a099691b4914"),
    ("stu_a13a91e6b7b1", EdgeType.SCOPES, "stu_a099691b4914"),
    ("stu_a099691b4914", EdgeType.DEPENDS_ON, "stu_8c6af8e04826"),
    ("stu_0ac12190d250", EdgeType.USES, "stu_fbab0a848253"),
]
ATTEMPT4_RESOLUTION = {
    "stu_d348d3c91c94": "eq.bernoulli",
    "stu_770825200223": "eq.bernoulli",
    "stu_a099691b4914": "eq.bernoulli",
    "stu_fbab0a848253": "eq.bernoulli",
    "stu_8c6af8e04826": "simp.equal_pressure_simplification",
    "stu_0ac12190d250": "proc.plan_set_v1_zero_and_solve_bernoulli",
    "stu_0820e52a924e": "pressure_velocity_same_direction",  # misconception key
    # unresolved: stu_a13a91e6b7b1, stu_3505a649dd21, stu_9f3bddc2f683, stu_7045c7699e95
}

# --------------------------------------------------------------------------- #
# Attempt 5 (staging run 2 — node coverage 1.0, edge coverage 0.0).
# --------------------------------------------------------------------------- #
_A5 = 5
ATTEMPT5_NODES = [
    (
        "stu_3490bf1a0bd5",
        "definition",
        {"concept": "bernoulli", "meaning": "energy conservation along a streamline"},
    ),
    ("stu_443746a79dd5", "condition", {"applies_when": "steady flow"}),
    ("stu_b7be9a7e957d", "equation", {"symbolic": "P + Rational(1,2)*rho*v**2 + rho*g*h - C"}),
    ("stu_e95c9d797750", "equation", {"symbolic": "P + Rational(1,2)*rho*v**2 - C"}),
    ("stu_ec9d6a6bafa0", "condition", {"applies_when": "open to atmosphere at both ends"}),
    (
        "stu_a21ba3cbaaea",
        "simplification",
        {"applies_when": "P1 == P2", "transformation": "pressure terms cancel"},
    ),
    ("stu_b1fa1bde38f8", "equation", {"symbolic": "A1*v1 - A2*v2"}),
    ("stu_ee2926c8dc51", "condition", {"applies_when": "incompressible"}),
    (
        "stu_67e7fefd70bb",
        "definition",
        {"concept": "pressure-velocity", "meaning": "pressure and velocity move together"},
    ),
    (
        "stu_040ca3889113",
        "equation",
        {"symbolic": "P1 + Rational(1,2)*rho*v1**2 - (P2 + Rational(1,2)*rho*v2**2)"},
    ),
    ("stu_e18d5469aa6b", "procedure_step", {"action": "set v1 to zero and solve for v2"}),
]
ATTEMPT5_EDGES = [
    ("stu_67e7fefd70bb", EdgeType.DEPENDS_ON, "stu_3490bf1a0bd5"),
    ("stu_a21ba3cbaaea", EdgeType.DEPENDS_ON, "stu_443746a79dd5"),
    ("stu_e95c9d797750", EdgeType.DEPENDS_ON, "stu_b7be9a7e957d"),
    ("stu_a21ba3cbaaea", EdgeType.SCOPES, "stu_b7be9a7e957d"),
    ("stu_ec9d6a6bafa0", EdgeType.SCOPES, "stu_b7be9a7e957d"),
    ("stu_040ca3889113", EdgeType.DEPENDS_ON, "stu_e95c9d797750"),
    ("stu_a21ba3cbaaea", EdgeType.SCOPES, "stu_e95c9d797750"),
    ("stu_e18d5469aa6b", EdgeType.USES, "stu_040ca3889113"),
]
ATTEMPT5_RESOLUTION = {
    "stu_3490bf1a0bd5": "eq.bernoulli",
    "stu_b7be9a7e957d": "eq.bernoulli",
    "stu_e95c9d797750": "eq.bernoulli",
    "stu_040ca3889113": "eq.bernoulli",
    "stu_ec9d6a6bafa0": "simp.equal_pressure_simplification",
    "stu_a21ba3cbaaea": "proc.plan_apply_equal_pressure_simplification",
    "stu_e18d5469aa6b": "proc.plan_set_v1_zero_and_solve_bernoulli",
    "stu_67e7fefd70bb": "pressure_velocity_same_direction",
    # unresolved: stu_443746a79dd5, stu_b1fa1bde38f8, stu_ee2926c8dc51
}


def _kg(attempt_id, node_rows, edge_rows) -> KGGraph:
    nodes = [
        build_node(node_type=t, node_id=nid, attempt_id=attempt_id, source="parser", content=c)
        for nid, t, c in node_rows
    ]
    idx = {n.node_id: n for n in nodes}
    edges = [
        Edge(
            edge_type=et,
            from_node_id=f,
            to_node_id=t,
            attempt_id=attempt_id,
            source="parser",
            from_node_type=idx[f].node_type,
            to_node_type=idx[t].node_type,
        )
        for f, et, t in edge_rows
    ]
    return KGGraph(nodes=nodes, edges=edges)


def _resolution(node_rows, resolved_map) -> ResolutionResult:
    resolved = tuple(
        ResolvedNode(
            node_id=nid,
            resolution="resolved" if nid in resolved_map else "unresolved",
            resolved_key=resolved_map.get(nid),
            resolved_canon_key=None,
            method="embedding",
            confidence=0.9,
        )
        for nid, _t, _c in node_rows
    )
    return ResolutionResult(resolved=resolved, tier_counts={}, llm_calls=0)


def _canonical_edges(attempt_id, node_rows, edge_rows, resolved_map):
    student = build_student_canonical(
        _kg(attempt_id, node_rows, edge_rows),
        _resolution(node_rows, resolved_map),
    )
    return {(edge.edge_type, edge.from_key, edge.to_key) for edge in student.edges}


def _depends_on_match_count(attempt_id, node_rows, edge_rows, resolved_map) -> int:
    reference = build_reference_canonical(PROBLEM)
    student = build_student_canonical(
        _kg(attempt_id, node_rows, edge_rows),
        _resolution(node_rows, resolved_map),
    )
    matched, _ = _edge_findings(student, reference)
    return sum("DEPENDS_ON" in (finding.message or "") for finding in matched)


def test_attempt_4_credits_correct_raw_edge_not_backwards_assertion():
    target = (
        EdgeType.DEPENDS_ON,
        "eq.bernoulli",
        "simp.equal_pressure_simplification",
    )

    # Raw parser convention: the simplification relies on Bernoulli. Flipping
    # dependent -> prerequisite yields the canonical prerequisite -> dependent
    # tuple that the reference contains.
    correct_raw = [
        row
        for row in ATTEMPT4_EDGES
        if row[0] == "stu_8c6af8e04826"
        and row[1] == EdgeType.DEPENDS_ON
        and row[2] == "stu_770825200223"
    ]
    assert _canonical_edges(_A4, ATTEMPT4_NODES, correct_raw, ATTEMPT4_RESOLUTION) == {target}

    # Raw backwards assertion: Bernoulli depends on the simplification. It now
    # normalizes to the inverse tuple and cannot source the reference match.
    backwards_raw = [
        row
        for row in ATTEMPT4_EDGES
        if row[0] == "stu_a099691b4914"
        and row[1] == EdgeType.DEPENDS_ON
        and row[2] == "stu_8c6af8e04826"
    ]
    assert _canonical_edges(_A4, ATTEMPT4_NODES, backwards_raw, ATTEMPT4_RESOLUTION) == {
        (
            EdgeType.DEPENDS_ON,
            "simp.equal_pressure_simplification",
            "eq.bernoulli",
        )
    }

    reference_edges = {
        (edge.edge_type, edge.from_key, edge.to_key)
        for edge in build_reference_canonical(PROBLEM).edges
    }
    assert target in reference_edges
    assert _depends_on_match_count(_A4, ATTEMPT4_NODES, ATTEMPT4_EDGES, ATTEMPT4_RESOLUTION) == 1


def test_attempt_5_still_has_zero_depends_on_matches():
    assert _depends_on_match_count(_A5, ATTEMPT5_NODES, ATTEMPT5_EDGES, ATTEMPT5_RESOLUTION) == 0
