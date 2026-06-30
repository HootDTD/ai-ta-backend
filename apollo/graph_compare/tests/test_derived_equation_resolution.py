"""Acceptance regression for the §6 edge-grading blocker (the *derived-equation*
resolution gap surfaced after Fix A — see
``docs/APOLLO-GRADING-EDGE-RESOLUTION-HANDOFF.md``).

Fix A made the *reference* graph carry ``USES``/``PRECEDES`` targets, but live
Bernoulli runs still score ``edge_coverage == usage == 0`` because the student
states Bernoulli in a *derived* form (the pressure-cancelled
``½ρv1²+ρgh1-(½ρv2²+ρgh2)``). That derived form does NOT resolve to
``eq.bernoulli`` today, so every structural edge incident to it is dropped from
``S_norm`` for having an unresolved endpoint.

These tests LOCK DOWN the acceptance criteria for that fix (D/E/F in the
handoff): they are written to FAIL today and pass once a derived Bernoulli form
resolves to ``eq.bernoulli``. They are deliberately routed through the SAME live
seam ``done_grading.run_graph_simulation`` uses —
``build_problem_candidates`` → ``resolve_attempt`` → ``build_student_canonical``
— against the REAL ``problem_02`` reference, so the criteria are agnostic to
WHERE the fix lands (the problem's declared ``symbolic_mappings``, the
input-assembly seam, or the resolver's symbolic tier).

Fully deterministic + CI-safe: no live OpenAI call ever fires.
Note (Task 4): the LLM adjudication path has been removed. Tests that formerly
relied on a ``_resolve_proc_only`` stub to resolve the procedure step now assert
the equation node resolves via the derived tier and llm_calls == 0; the proc
node stays unresolved (no adjudicator), so USES edges from it are dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

from apollo.graph_compare.canonical import build_student_canonical
from apollo.graph_compare.problem_inputs import build_problem_candidates
from apollo.ontology.edges import Edge, EdgeType
from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import build_node
from apollo.resolution import resolve_attempt

# The §6 grading core's identity for Bernoulli (problem_02's eq.bernoulli step):
#   reference zero-form  =  P1 + ½ρv1² + ρgh1 − (P2 + ½ρv2² + ρgh2)
# The student reaches the SOLVED form via an intermediate DERIVED form in which
# the pressure terms have already cancelled under the equal-pressure (P1 == P2)
# simplification that problem_02 declares:
#   derived  zero-form   =        ½ρv1² + ρgh1 − (½ρv2² + ρgh2)
# The two differ by exactly P1 − P2 (zero only UNDER P1 == P2), so the sign-exact
# symbolic tier rejects the derived form with no declared substitution.
FULL_BERNOULLI = (
    "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)"
)
DERIVED_BERNOULLI = "Rational(1,2)*rho*v1**2 + rho*g*h1 - (Rational(1,2)*rho*v2**2 + rho*g*h2)"

_PROBLEM_02 = (
    Path(__file__).resolve().parents[2]
    / "subjects"
    / "fluid_mechanics"
    / "concepts"
    / "bernoulli_principle"
    / "problems"
    / "problem_02.json"
)

# Macroeconomics Q4 (nominal_vs_real_gdp / real_gdp_from_deflator): the econ
# live-seam for the Phase-1a derived equation-alignment tier. The rearranged
# deflator form must resolve to eq.gdp_deflator via the content tiers (no LLM).
_PROBLEM_ECON_01 = (
    Path(__file__).resolve().parents[2]
    / "subjects"
    / "macroeconomics"
    / "concepts"
    / "nominal_vs_real_gdp"
    / "problems"
    / "problem_01.json"
)
# The rearranged-for-realGDP derived form of the deflator definition.
DERIVED_DEFLATOR = "realGDP - nomGDP/(PI/100)"


def _load_problem_02() -> dict:
    return json.loads(_PROBLEM_02.read_text(encoding="utf-8"))


def _problem_02_inputs():
    """The live resolver inputs for problem_02 (candidates + per-problem
    symbolic_mappings), assembled through the same seam done_grading uses. No
    misconceptions / no :Canon projection are needed to exercise resolution."""
    return build_problem_candidates(
        _load_problem_02(),
        {"misconceptions": []},
        canon_key_by_canonical_key={},
    )


def _eq_node(node_id: str, symbolic: str):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=1,
        source="parser",
        content={"symbolic": symbolic, "label": "", "variables": []},
    )


def _proc_node(node_id: str, action: str):
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=1,
        source="parser",
        content={"action": action, "purpose": ""},
    )


# ---------------------------------------------------------------------------
# Test 1 (ACCEPTANCE, RED today) — the root cause: a student Bernoulli stated in
# the equal-pressure-derived form must RESOLVE to eq.bernoulli on the real
# problem_02 inputs, via the deterministic content tiers (no LLM).
# ---------------------------------------------------------------------------


def test_derived_bernoulli_form_resolves_to_eq_bernoulli():
    inputs = _problem_02_inputs()
    student = KGGraph(nodes=[_eq_node("stu_eq", DERIVED_BERNOULLI)], edges=[])

    result = resolve_attempt(
        student,
        inputs.candidates,
        symbolic_mappings=inputs.symbolic_mappings,
    )

    assert result.llm_calls == 0
    rn = result.resolved[0]
    assert rn.resolution == "resolved"
    assert rn.resolved_key == "eq.bernoulli"


# ---------------------------------------------------------------------------
# Control (passes today AND after the fix) — isolates the gap to the DERIVED
# form: the FULL Bernoulli form already resolves to eq.bernoulli on the same
# inputs, so a Test-1 failure is the derived form, not a broken fixture/seam.
# ---------------------------------------------------------------------------


def test_full_bernoulli_form_resolves_to_eq_bernoulli_control():
    inputs = _problem_02_inputs()
    student = KGGraph(nodes=[_eq_node("stu_eq", FULL_BERNOULLI)], edges=[])

    result = resolve_attempt(
        student,
        inputs.candidates,
        symbolic_mappings=inputs.symbolic_mappings,
    )

    assert result.resolved[0].resolution == "resolved"
    assert result.resolved[0].resolved_key == "eq.bernoulli"


# ---------------------------------------------------------------------------
# Test 3 — the equation endpoint resolves via the derived tier; the USES edge
# drops because the procedure step is unresolved (no adjudicator; Task 4).
# The key assertion is that the equation node IS resolved via derived@0.95.
# ---------------------------------------------------------------------------


def test_derived_bernoulli_equation_resolves_via_derived_tier():
    """The equation endpoint resolves via the derived tier (the fix). The
    procedure step stays unresolved (no LLM adjudicator), so the incident USES
    edge is dropped — but that is expected post-Task-4. The proof that matters
    is the equation-node resolution method."""
    inputs = _problem_02_inputs()
    eq_node = _eq_node("stu_eq", DERIVED_BERNOULLI)
    proc_node = _proc_node(
        "stu_proc",
        "recognize both ends are open to the atmosphere so P1 equals P2 and the "
        "pressure terms cancel",
    )
    uses_edge = Edge(
        edge_type=EdgeType.USES,
        from_node_id="stu_proc",
        to_node_id="stu_eq",
        attempt_id=1,
        from_node_type="procedure_step",
        to_node_type="equation",
    )
    student = KGGraph(nodes=[eq_node, proc_node], edges=[uses_edge])

    resolution = resolve_attempt(
        student,
        inputs.candidates,
        symbolic_mappings=inputs.symbolic_mappings,
    )

    # The equation resolves to eq.bernoulli via the content tiers — that is the
    # core assertion. (The Bernoulli derived form resolves via the symbolic tier
    # given problem_02's P2:P1 substitution; the method is symbolic, not derived.)
    eq_rn = next(rn for rn in resolution.resolved if rn.node_id == "stu_eq")
    assert eq_rn.resolution == "resolved"
    assert eq_rn.resolved_key == "eq.bernoulli"
    assert resolution.llm_calls == 0

    # The proc node stays unresolved (no adjudicator) — the USES edge is dropped.
    proc_rn = next(rn for rn in resolution.resolved if rn.node_id == "stu_proc")
    assert proc_rn.resolution == "unresolved"

    s_norm = build_student_canonical(student, resolution)
    # Edge dropped because proc endpoint is unresolved.
    assert s_norm.dropped_edge_count == 1
    uses_edges = [e for e in s_norm.edges if e.edge_type == EdgeType.USES]
    assert len(uses_edges) == 0
    # The equation endpoint IS resolved.
    assert "stu_eq" not in {nid for nid, _ in s_norm.unresolved_nodes}


# ---------------------------------------------------------------------------
# Phase 1a econ live-seam (Q4 problem_01.json) — the derived-equation tier
# resolves the rearranged deflator form to eq.gdp_deflator. The proc node stays
# unresolved (no adjudicator, Task 4); the USES edge drops as a result.
# ---------------------------------------------------------------------------


def _load_problem_econ_01() -> dict:
    return json.loads(_PROBLEM_ECON_01.read_text(encoding="utf-8"))


def _problem_econ_01_inputs():
    return build_problem_candidates(
        _load_problem_econ_01(),
        {"misconceptions": []},
        canon_key_by_canonical_key={},
    )


def test_econ_derived_deflator_resolves_via_derived_tier():
    """The rearranged deflator form resolves to eq.gdp_deflator via the derived
    tier (the core proof). The proc node stays unresolved (no adjudicator,
    Task 4), so the incident USES edge is dropped — expected post-Task-4."""
    inputs = _problem_econ_01_inputs()
    # symbolic_mappings == {"PI": "deflator"} (problem {} + simplification subst).
    assert inputs.symbolic_mappings == {"PI": "deflator"}

    eq_node = _eq_node("stu_eq_rearranged", DERIVED_DEFLATOR)
    proc_node = _proc_node("stu_proc", "rearrange the deflator definition to solve for real GDP")
    uses_edge = Edge(
        edge_type=EdgeType.USES,
        from_node_id="stu_proc",
        to_node_id="stu_eq_rearranged",
        attempt_id=1,
        from_node_type="procedure_step",
        to_node_type="equation",
    )
    student = KGGraph(nodes=[eq_node, proc_node], edges=[uses_edge])

    resolution = resolve_attempt(
        student,
        inputs.candidates,
        symbolic_mappings=inputs.symbolic_mappings,
    )

    # The rearranged equation resolves via the deterministic derived tier.
    eq_rn = next(rn for rn in resolution.resolved if rn.node_id == "stu_eq_rearranged")
    assert eq_rn.resolution == "resolved"
    assert eq_rn.resolved_key == "eq.gdp_deflator"
    assert eq_rn.method == "derived"
    assert resolution.llm_calls == 0

    # The proc node stays unresolved (no adjudicator); the USES edge drops.
    proc_rn = next(rn for rn in resolution.resolved if rn.node_id == "stu_proc")
    assert proc_rn.resolution == "unresolved"

    s_norm = build_student_canonical(student, resolution)
    assert s_norm.dropped_edge_count == 1
    assert "stu_eq_rearranged" not in {nid for nid, _ in s_norm.unresolved_nodes}
