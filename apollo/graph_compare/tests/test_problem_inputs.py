"""WU-4A1 Task 7 — build_problem_candidates (closed candidate set + per-problem
symbolic_mappings assembly).

This is the seam where the per-problem ``symbolic_mappings`` table is resolved
to pass into ``resolve_attempt`` — WU-4A1 does NOT call the resolver itself (the
caller / WU-4C does), it only assembles the inputs. Test 29 is the ONE test that
invokes ``resolve_attempt``, with ``llm_adjudicator=None`` (CI-safe: resolves on
the symbolic tier alone, ``llm_calls == 0``, NO live API call), proving the seam
correctly plumbs the table into the resolver's symbolic tier.

Real problem_01.json / misconceptions.json from disk pin the closed-set + the
symbolic_mappings read against the authored data; hand-built dicts cover the
default-empty branch.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from apollo.graph_compare.problem_inputs import ProblemInputs, build_problem_candidates
from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import build_node
from apollo.resolution import Candidate, resolve_attempt

_BERNOULLI = (
    Path(__file__).resolve().parents[2]
    / "subjects"
    / "fluid_mechanics"
    / "concepts"
    / "bernoulli_principle"
)


def _load(name: str) -> dict:
    if name == "problem_01":
        return json.loads(
            (_BERNOULLI / "problems" / "problem_01.json").read_text(encoding="utf-8")
        )
    return json.loads((_BERNOULLI / f"{name}.json").read_text(encoding="utf-8"))


def _canon_key_map() -> dict[str, int]:
    keys = [
        "eq.continuity",
        "cond.incompressibility",
        "eq.bernoulli",
        "simp.horizontal_simplification",
        "proc.plan_apply_continuity",
        "proc.plan_apply_horizontal_simplification",
        "proc.plan_solve_bernoulli_for_p2",
        "def.pressure_velocity_tradeoff",
        "misc.pressure_velocity_same_direction",
        "misc.density_ignored",
    ]
    return {k: i + 1 for i, k in enumerate(keys)}


def test_build_problem_candidates_assembles_closed_set():
    """All 7 reference candidates + both misconceptions appear (delegates to the
    reused candidate builders)."""
    inputs = build_problem_candidates(
        _load("problem_01"),
        _load("misconceptions"),
        canon_key_by_canonical_key=_canon_key_map(),
    )
    keys = {c.canonical_key for c in inputs.candidates}
    assert {
        "eq.continuity",
        "cond.incompressibility",
        "eq.bernoulli",
        "simp.horizontal_simplification",
        "proc.plan_apply_continuity",
        "proc.plan_apply_horizontal_simplification",
        "proc.plan_solve_bernoulli_for_p2",
    } <= keys
    assert "misc.pressure_velocity_same_direction" in keys
    assert "misc.density_ignored" in keys
    # Misconceptions are always appended (the §5 guardrail).
    assert sum(1 for c in inputs.candidates if c.is_misconception) == 2


def test_symbolic_mappings_read_from_problem():
    """KEY (Decision 2): problem_01 (now carrying {'d':'2*r'}) -> inputs carry it."""
    inputs = build_problem_candidates(
        _load("problem_01"),
        _load("misconceptions"),
        canon_key_by_canonical_key=_canon_key_map(),
    )
    assert inputs.symbolic_mappings == {"d": "2*r"}


def test_symbolic_mappings_default_empty_when_absent():
    """A problem with NO symbolic_mappings key -> {} (a NEW dict, not aliasing
    the problem's)."""
    problem = {"reference_solution": [], "declared_paths": [[]]}
    inputs = build_problem_candidates(problem, {"misconceptions": []}, canon_key_by_canonical_key={})
    assert inputs.symbolic_mappings == {}
    # A NEW dict — mutating it must not affect the problem dict.
    inputs.symbolic_mappings["x"] = "y"
    assert "symbolic_mappings" not in problem


def test_symbolic_mappings_returns_new_dict_not_alias():
    """The returned symbolic_mappings is a COPY of the problem's, never an alias
    (immutability: builders never hand back a reference into their input)."""
    problem = {
        "reference_solution": [],
        "declared_paths": [[]],
        "symbolic_mappings": {"d": "2*r"},
    }
    inputs = build_problem_candidates(problem, {"misconceptions": []}, canon_key_by_canonical_key={})
    assert inputs.symbolic_mappings == {"d": "2*r"}
    assert inputs.symbolic_mappings is not problem["symbolic_mappings"]


def test_symbolic_mappings_plumbed_makes_circular_area_resolvable():
    """KEY (Decision 2, §6.9): build inputs from a problem carrying {'d':'2*r'}
    with an eq.circular_area candidate (symbolic 'A = pi*d**2/4'); feed
    inputs.candidates + inputs.symbolic_mappings into resolve_attempt; the
    A = pi*r**2 student node resolves to eq.circular_area via the symbolic tier.

    End-to-end proof that 4A1's seam plumbs the per-problem table into the
    resolver's symbolic tier. CI-safe: llm_adjudicator=None, resolves on the
    symbolic tier alone -> llm_calls == 0 (NO live API call)."""
    problem = {
        "reference_solution": [
            {
                "id": "circular_area",
                "entry_type": "equation",
                "entity_key": "eq.circular_area",
                "content": {"symbolic": "A = pi*d**2/4", "label": "Circular area"},
                "depends_on": [],
            }
        ],
        "declared_paths": [["circular_area"]],
        "symbolic_mappings": {"d": "2*r"},
    }
    inputs = build_problem_candidates(
        problem, {"misconceptions": []}, canon_key_by_canonical_key={"eq.circular_area": 20}
    )
    student = KGGraph(
        nodes=[
            build_node(
                node_type="equation",
                node_id="a1",
                attempt_id=1,
                source="parser",
                content={"symbolic": "A = pi*r**2", "label": "", "variables": []},
            )
        ],
        edges=[],
    )
    result = resolve_attempt(
        student,
        inputs.candidates,
        llm_adjudicator=None,
        symbolic_mappings=inputs.symbolic_mappings,
    )
    assert result.llm_calls == 0
    rn = result.resolved[0]
    assert rn.resolution == "resolved"
    assert rn.resolved_key == "eq.circular_area"
    assert rn.method == "symbolic"


def test_symbolic_mappings_absent_circular_area_does_not_resolve():
    """Control for the plumbing test: WITHOUT the {'d':'2*r'} mapping the same
    student equation does NOT resolve on the symbolic tier (the resolver applies
    no global default), so the seam — not a coincidence — is what enables it."""
    candidates = (
        Candidate(
            canonical_key="eq.circular_area",
            canon_key=20,
            node_type="equation",
            is_misconception=False,
            symbolic="A = pi*d**2/4",
            aliases=(),
            display_name="Circular area",
            opposes_key=None,
        ),
    )
    student = KGGraph(
        nodes=[
            build_node(
                node_type="equation",
                node_id="a1",
                attempt_id=1,
                source="parser",
                content={"symbolic": "A = pi*r**2", "label": "", "variables": []},
            )
        ],
        edges=[],
    )
    result = resolve_attempt(student, candidates, llm_adjudicator=None, symbolic_mappings={})
    assert result.resolved[0].resolution == "unresolved"


def test_problem_inputs_is_frozen():
    """ProblemInputs mutation raises FrozenInstanceError."""
    inputs = ProblemInputs(candidates=(), symbolic_mappings={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        inputs.candidates = ()  # type: ignore[misc]
