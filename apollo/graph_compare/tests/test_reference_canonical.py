"""WU-4A1 Task 5 — build_reference_canonical (R_norm).

R_norm is built from the problem's OWN ``reference_solution`` +
``depends_on`` + ``declared_paths`` — NEVER the deduped entity payload
(Decision 3). The regression guard (test 17) is the load-bearing one: two
problems both minting ``eq.continuity`` with DIFFERENT symbolic forms must each
carry their OWN symbolic, never a shared/deduped value.

Real ``problem_01.json`` from disk pins the builder against the authored data;
hand-built dicts exercise the multi-path + invalid-reference branches the single
real problem cannot.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apollo.graph_compare.canonical import (
    CanonicalNode,
    ReferenceGraph,
    build_reference_canonical,
)
from apollo.graph_compare.validator import ReferenceGraphInvalidError
from apollo.ontology.edges import EdgeType

_BERNOULLI = (
    Path(__file__).resolve().parents[2]
    / "subjects"
    / "fluid_mechanics"
    / "concepts"
    / "bernoulli_principle"
)


def _problem01() -> dict:
    return json.loads((_BERNOULLI / "problems" / "problem_01.json").read_text(encoding="utf-8"))


def _by_key(graph: ReferenceGraph) -> dict[str, CanonicalNode]:
    return {n.canonical_key: n for n in graph.nodes}


def test_reference_canonical_from_problem01():
    """7 nodes keyed by the 7 entity_keys; eq.continuity carries its symbolic;
    node_types map (simp.* -> simplification, proc.* -> procedure_step)."""
    graph = build_reference_canonical(_problem01())
    nodes = _by_key(graph)
    assert len(graph.nodes) == 7
    assert set(nodes) == {
        "eq.continuity",
        "cond.incompressibility",
        "eq.bernoulli",
        "simp.horizontal_simplification",
        "proc.plan_apply_continuity",
        "proc.plan_apply_horizontal_simplification",
        "proc.plan_solve_bernoulli_for_p2",
    }
    assert nodes["eq.continuity"].symbolic == "rho*A1*v1 - rho*A2*v2"
    assert nodes["eq.continuity"].node_type == "equation"
    assert nodes["simp.horizontal_simplification"].node_type == "simplification"
    assert nodes["proc.plan_apply_continuity"].node_type == "procedure_step"
    assert nodes["cond.incompressibility"].node_type == "condition"
    # Reference nodes carry NO student evidence and NO method/confidence.
    assert nodes["eq.continuity"].evidence_spans == ()
    assert nodes["eq.continuity"].method is None
    assert nodes["eq.continuity"].confidence is None
    # source_node_ids is the reference step id.
    assert nodes["eq.continuity"].source_node_ids == ("continuity",)


def test_reference_canonical_depends_on_becomes_edges():
    """eq.bernoulli depends_on incompressibility -> a canonical dependency edge
    cond.incompressibility -> eq.bernoulli."""
    graph = build_reference_canonical(_problem01())
    edge_pairs = {(e.from_key, e.to_key) for e in graph.edges}
    assert ("cond.incompressibility", "eq.bernoulli") in edge_pairs
    # Dependency edges use the generic DEPENDS_ON direction.
    dep = next(
        e
        for e in graph.edges
        if (e.from_key, e.to_key) == ("cond.incompressibility", "eq.bernoulli")
    )
    assert dep.edge_type == EdgeType.DEPENDS_ON
    assert dep.provenance == "explicit"


def test_reference_canonical_single_declared_path_view():
    """problem_01 yields exactly one ReferencePathView with the 7 entity keys in
    declared-path order."""
    graph = build_reference_canonical(_problem01())
    assert len(graph.paths) == 1
    assert graph.paths[0].canonical_keys == (
        "eq.continuity",
        "cond.incompressibility",
        "eq.bernoulli",
        "simp.horizontal_simplification",
        "proc.plan_apply_continuity",
        "proc.plan_apply_horizontal_simplification",
        "proc.plan_solve_bernoulli_for_p2",
    )


def test_reference_canonical_multi_path_views():
    """A SYNTHETIC two-path problem yields two ReferencePathViews, each mapping
    its own node-id sequence to canonical keys (covers the multi-path branch)."""
    problem = {
        "reference_solution": [
            {
                "id": "a",
                "entry_type": "equation",
                "entity_key": "eq.a",
                "content": {"symbolic": "x = y"},
                "depends_on": [],
            },
            {
                "id": "b",
                "entry_type": "equation",
                "entity_key": "eq.b",
                "content": {"symbolic": "y = z"},
                "depends_on": [],
            },
            {
                "id": "c",
                "entry_type": "condition",
                "entity_key": "cond.c",
                "content": {"applies_when": "always"},
                "depends_on": [],
            },
        ],
        "declared_paths": [
            ["a", "c"],
            ["b", "c"],
        ],
    }
    graph = build_reference_canonical(problem)
    assert len(graph.paths) == 2
    assert graph.paths[0].canonical_keys == ("eq.a", "cond.c")
    assert graph.paths[1].canonical_keys == ("eq.b", "cond.c")


def test_reference_canonical_object_path_maps_strategy_milestones_and_g4_drops():
    problem = {
        "reference_solution": [
            {
                "id": "known",
                "entry_type": "equation",
                "entity_key": "eq.known",
                "content": {"symbolic": "x = 1"},
                "depends_on": [],
            },
            {
                "id": "degraded",
                "entry_type": "future_type",
                "entity_key": "future.degraded",
                "content": {},
                "depends_on": [],
            },
        ],
        "declared_paths": [
            {
                "strategy_id": "direct",
                "nodes": ["known", "degraded"],
                "milestones": ["known", "degraded"],
            }
        ],
    }
    graph = build_reference_canonical(problem)
    assert graph.paths[0].strategy_id == "direct"
    assert graph.paths[0].canonical_keys == ("eq.known",)
    assert graph.paths[0].milestone_keys == ("eq.known",)


def test_reference_canonical_uses_own_reference_solution_not_dedup():
    """REGRESSION GUARD (Decision 3): two problems both minting eq.continuity
    with DIFFERENT symbolic forms -> each R_norm carries ITS OWN symbolic, never
    a shared/deduped value."""
    problem_a = {
        "reference_solution": [
            {
                "id": "continuity",
                "entry_type": "equation",
                "entity_key": "eq.continuity",
                "content": {"symbolic": "rho*A1*v1 - rho*A2*v2"},
                "depends_on": [],
            },
        ],
        "declared_paths": [["continuity"]],
    }
    problem_b = {
        "reference_solution": [
            {
                "id": "continuity",
                "entry_type": "equation",
                "entity_key": "eq.continuity",
                "content": {"symbolic": "A1*v1 - A2*v2"},
                "depends_on": [],
            },
        ],
        "declared_paths": [["continuity"]],
    }
    graph_a = build_reference_canonical(problem_a)
    graph_b = build_reference_canonical(problem_b)
    assert _by_key(graph_a)["eq.continuity"].symbolic == "rho*A1*v1 - rho*A2*v2"
    assert _by_key(graph_b)["eq.continuity"].symbolic == "A1*v1 - A2*v2"


def test_reference_canonical_invalid_reference_raises():
    """A problem failing validate_reference_graph (missing entity link) ->
    build_reference_canonical raises ReferenceGraphInvalidError BEFORE any build
    (the §6.6 block-grading gate)."""
    problem = {
        "reference_solution": [
            {
                "id": "a",
                "entry_type": "equation",
                "content": {"symbolic": "x = y"},
                "depends_on": [],
            },
        ],
        "declared_paths": [["a"]],
    }
    with pytest.raises(ReferenceGraphInvalidError):
        build_reference_canonical(problem)


def test_reference_canonical_returns_frozen_graph():
    """The returned ReferenceGraph + its nodes are immutable tuples."""
    graph = build_reference_canonical(_problem01())
    assert isinstance(graph.nodes, tuple)
    assert isinstance(graph.edges, tuple)
    assert isinstance(graph.paths, tuple)


def test_reference_canonical_definition_node_type_round_trip():
    """A reference solution carrying a definition step maps to NodeType
    'definition' (extra coverage over the entry_type table)."""
    problem = {
        "reference_solution": [
            {
                "id": "d",
                "entry_type": "definition",
                "entity_key": "def.d",
                "content": {"label": "a def"},
                "depends_on": [],
            },
        ],
        "declared_paths": [["d"]],
    }
    graph = build_reference_canonical(problem)
    node = _by_key(graph)["def.d"]
    assert node.node_type == "definition"
    # definitions carry no symbolic.
    assert node.symbolic is None


def test_reference_canonical_emits_uses_edges():
    """Each procedure step's ``uses_equations`` becomes a USES edge
    (procedure_step entity_key -> equation entity_key), mirroring
    ``Problem.to_kg_graph`` so the student's real USES edges can score the
    ``usage`` dimension instead of it being vacuously 1.0 (the 'structural edge
    scoring is dead' regression)."""
    graph = build_reference_canonical(_problem01())
    uses = {(e.from_key, e.to_key) for e in graph.edges if e.edge_type == EdgeType.USES}
    assert uses == {
        ("proc.plan_apply_continuity", "eq.continuity"),
        ("proc.plan_apply_horizontal_simplification", "eq.bernoulli"),
        ("proc.plan_solve_bernoulli_for_p2", "eq.bernoulli"),
    }
    # USES reference edges carry the same 'explicit' provenance as DEPENDS_ON.
    assert all(e.provenance == "explicit" for e in graph.edges if e.edge_type == EdgeType.USES)


def test_reference_canonical_emits_precedes_chain():
    """Procedure steps ordered by ``content.order`` form a consecutive PRECEDES
    chain (prev -> next over canonical keys), mirroring ``Problem.to_kg_graph``;
    this gives ``edge_coverage`` real procedure-order structure to match the
    student's PRECEDES edges against."""
    graph = build_reference_canonical(_problem01())
    precedes = [(e.from_key, e.to_key) for e in graph.edges if e.edge_type == EdgeType.PRECEDES]
    assert precedes == [
        ("proc.plan_apply_continuity", "proc.plan_apply_horizontal_simplification"),
        ("proc.plan_apply_horizontal_simplification", "proc.plan_solve_bernoulli_for_p2"),
    ]


def test_reference_canonical_no_uses_or_precedes_without_procedure_steps():
    """A reference with no procedure steps emits neither USES nor PRECEDES edges
    (the new edge families are scoped to procedure steps; guards over-emission)."""
    problem = {
        "reference_solution": [
            {
                "id": "a",
                "entry_type": "equation",
                "entity_key": "eq.a",
                "content": {"symbolic": "x = y"},
                "depends_on": [],
            },
        ],
        "declared_paths": [["a"]],
    }
    graph = build_reference_canonical(problem)
    assert not [e for e in graph.edges if e.edge_type in (EdgeType.USES, EdgeType.PRECEDES)]


# ---------------------------------------------------------------------------
# G4 — variable_mapping contract. A WU-AAS-minted reference_solution can carry a
# ``variable_mapping`` (``varmap.*``) step; R_norm must build it as a first-class
# node (it used to raise ``KeyError: 'variable_mapping'`` — the F1c crash). An
# UNKNOWN entry_type degrades (the node + its edges/paths are dropped) instead.
# ---------------------------------------------------------------------------


def test_reference_canonical_variable_mapping_node_first_class():
    """A minted ``variable_mapping`` step becomes a canonical node with the
    ``variable_mapping`` type, and an equation depending on it links normally —
    no KeyError."""
    problem = {
        "reference_solution": [
            {
                "id": "m1",
                "entry_type": "variable_mapping",
                "entity_key": "varmap.m1",
                "content": {"term": "mass", "symbol": "m"},
                "depends_on": [],
            },
            {
                "id": "e1",
                "entry_type": "equation",
                "entity_key": "eq.e1",
                "content": {"symbolic": "v - a*t"},
                "depends_on": ["m1"],
            },
        ],
        "declared_paths": [["m1", "e1"]],
    }
    graph = build_reference_canonical(problem)
    nodes = _by_key(graph)
    assert nodes["varmap.m1"].node_type == "variable_mapping"
    assert nodes["varmap.m1"].symbolic is None
    depends = [(e.from_key, e.to_key) for e in graph.edges if e.edge_type == EdgeType.DEPENDS_ON]
    assert ("varmap.m1", "eq.e1") in depends
    assert graph.paths[0].canonical_keys == ("varmap.m1", "eq.e1")


def test_reference_canonical_degrades_unmapped_entry_type():
    """An entry_type outside the node-type map is DROPPED from R_norm — its node,
    every depends_on/USES edge touching it, and its slot in a declared path are
    removed — rather than raising. The recognized steps are unaffected."""
    problem = {
        "reference_solution": [
            {
                "id": "p1",
                "entry_type": "proof_sketch",  # unmapped (map/mint-map drift)
                "entity_key": "proof.p1",
                "content": {},
                "depends_on": [],
            },
            {
                "id": "e1",
                "entry_type": "equation",
                "entity_key": "eq.e1",
                "content": {"symbolic": "a - b"},
                "depends_on": ["p1"],  # depends on the degraded node -> edge dropped
            },
        ],
        "declared_paths": [["p1", "e1"]],
    }
    graph = build_reference_canonical(problem)
    keys = {n.canonical_key for n in graph.nodes}
    assert keys == {"eq.e1"}  # degraded node dropped
    # the depends_on edge onto the dropped node is gone (no dangling endpoint)
    assert not [e for e in graph.edges if e.edge_type == EdgeType.DEPENDS_ON]
    # the declared path keeps only the known subsequence
    assert graph.paths[0].canonical_keys == ("eq.e1",)


def test_reference_canonical_drops_uses_edge_to_degraded_step():
    """A procedure_step whose ``uses_equations`` references a DEGRADED (unmapped)
    step drops that USES edge rather than KeyError-ing on the missing key."""
    problem = {
        "reference_solution": [
            {
                "id": "x1",
                "entry_type": "diagram",  # unmapped -> degraded, absent from key map
                "entity_key": "diag.x1",
                "content": {},
                "depends_on": [],
            },
            {
                "id": "pr1",
                "entry_type": "procedure_step",
                "entity_key": "proc.pr1",
                "content": {"order": 1, "action": "do it", "uses_equations": ["x1"]},
                "depends_on": [],
            },
        ],
        # x1 must appear on a declared path (validate_reference rule (d)); it is
        # then degraded away inside the builder, exercising the USES-drop guard.
        "declared_paths": [["x1", "pr1"]],
    }
    graph = build_reference_canonical(problem)
    assert {n.canonical_key for n in graph.nodes} == {"proc.pr1"}
    # the USES edge onto the degraded step is dropped (no dangling endpoint)
    assert not [e for e in graph.edges if e.edge_type == EdgeType.USES]
