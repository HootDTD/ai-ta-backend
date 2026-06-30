"""Portability gate for the derived-equation resolution fix (handoff D/E/F).

The contract LOCKED DOWN here: each ``simplification`` MAY carry an explicit
symbolic ``substitution`` map in its ``content``; ``build_problem_candidates``
collects every simplification's ``substitution`` (plus any pre-existing
top-level ``symbolic_mappings``) into the resolver's ``symbolic_mappings`` — so a
student equation stated in the *derived* (post-simplification) form resolves to
the governing equation entity. ``applies_when`` stays a human-facing trigger
(symbolic OR a natural-language concept) and is NEVER parsed for derivation —
natural language can't be turned into a substitution deterministically.

The mechanism is shared/generic; the per-simplification substitution is
structured authored data (the provisioner emits it, like ``reference_solution``
itself), so the fix is subject-agnostic by construction. These tests prove it
generalizes across:

  * SUBSTITUTIONS within a subject — problem_01's horizontal case is authored
    with a ``{"h2": "h1"}`` substitution (DIFFERENT from problem_02's pressure
    ``{"P2": "P1"}``), guarding against a pressure-special-cased fix; and
  * SUBJECTS — a synthetic DC-circuit problem whose simplification declares a
    natural-language ``applies_when`` (a CONCEPT) plus an explicit
    ``{"Rb": "Ra"}`` substitution, with variables (``Va, Vb, Ix, Ra, Rb``)
    entirely OUTSIDE the solver's fluid ``_CANONICAL_SYMBOLS`` set.

All RED today (build_problem_candidates does not yet collect simplification
substitutions, so the derived forms stay unresolved). They go green once the fix
lands in the ``build_problem_candidates`` seam AND the two real fluid problems
are authored with their substitution fields. Deterministic + CI-safe: no live OpenAI call fires.
Note (Task 4): the LLM adjudication path has been removed. Tests that formerly
used a ``_resolve_proc_only`` stub now assert the equation resolves via the
derived tier (llm_calls == 0); the proc node stays unresolved, so USES edges
from it are dropped.
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

_PROBLEM_01 = (
    Path(__file__).resolve().parents[2]
    / "subjects"
    / "fluid_mechanics"
    / "concepts"
    / "bernoulli_principle"
    / "problems"
    / "problem_01.json"
)

# problem_01 (bernoulli_horizontal_pipe_find_p2) declares
# simp.horizontal_simplification; the fix authors its explicit substitution
# {"h2": "h1"} into that real problem. The student reaches the horizontal-derived
# Bernoulli with the ρgh terms already cancelled:
#   reference :  P1 + ½ρv1² + ρgh1 − (P2 + ½ρv2² + ρgh2)
#   derived   :  P1 + ½ρv1²        − (P2 + ½ρv2²)
# differing by exactly ρg(h1 − h2) — zero only UNDER h1 == h2.
HORIZONTAL_DERIVED_BERNOULLI = "P1 + Rational(1,2)*rho*v1**2 - (P2 + Rational(1,2)*rho*v2**2)"

# A synthetic, NON-fluid subject: a balanced two-resistor loop. The governing
# equation balances source/drop terms; the declared simplification states the
# two resistances are equal, so the I·R drops cancel:
#   reference :  Va + Ix*Ra − (Vb + Ix*Rb)
#   derived   :  Va        − Vb
# differing by exactly Ix·(Rb − Ra) — zero only UNDER Ra == Rb. None of
# Va/Vb/Ix/Ra/Rb are in the solver's fluid _CANONICAL_SYMBOLS.
CIRCUIT_GOVERNING = "Va + Ix*Ra - (Vb + Ix*Rb)"
CIRCUIT_DERIVED = "Va - Vb"

CIRCUIT_PROBLEM = {
    "id": "balanced_resistor_loop_find_vout",
    "reference_solution": [
        {
            "id": "loop",
            "entry_type": "equation",
            "entity_key": "eq.balanced_loop",
            "content": {
                "label": "balanced loop",
                "symbolic": CIRCUIT_GOVERNING,
                "variables": ["Va", "Ix", "Ra", "Vb", "Rb"],
            },
            "depends_on": [],
        },
        {
            "id": "equal_resistance",
            "entry_type": "simplification",
            "entity_key": "simp.equal_resistance",
            "content": {
                # applies_when is a CONCEPT here (not a symbolic equality) — it is
                # never parsed; the explicit `substitution` is the only source.
                "applies_when": "the two arms of the bridge are balanced",
                "transformation": "the I*R drops cancel",
                "substitution": {"Rb": "Ra"},
            },
            "depends_on": ["loop"],
        },
        {
            "id": "apply_equal_resistance",
            "entry_type": "procedure_step",
            "entity_key": "proc.apply_equal_resistance",
            "content": {
                "order": 1,
                "action": "set Ra equal to Rb so the resistive drops cancel",
                "purpose": "eliminate the I*R terms, leaving the node-voltage difference",
                "uses_equations": ["loop"],
            },
            "depends_on": ["loop", "equal_resistance"],
        },
    ],
    "declared_paths": [["loop", "equal_resistance", "apply_equal_resistance"]],
}


def _load_problem_01() -> dict:
    return json.loads(_PROBLEM_01.read_text(encoding="utf-8"))


def _inputs(problem: dict):
    return build_problem_candidates(problem, {"misconceptions": []}, canon_key_by_canonical_key={})


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
# Test 1 (ACCEPTANCE, RED) — SAME subject, DIFFERENT substitution: problem_01's
# horizontal-derived Bernoulli (h1 == h2) must resolve to eq.bernoulli. Proves
# the fix is not special-cased to the pressure (P1 == P2) substitution, and that
# the derived mapping MERGES with problem_01's pre-declared symbolic_mappings.
# ---------------------------------------------------------------------------


def test_horizontal_derived_bernoulli_resolves_via_declared_simplification():
    inputs = _inputs(_load_problem_01())
    student = KGGraph(nodes=[_eq_node("stu_eq", HORIZONTAL_DERIVED_BERNOULLI)], edges=[])

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
# Test 2 (ACCEPTANCE, RED) — DIFFERENT subject: a non-fluid balanced-resistor
# loop's derived form (Ra == Rb) must resolve to its governing equation.
# ---------------------------------------------------------------------------


def test_nonfluid_circuit_derived_form_resolves_via_declared_simplification():
    inputs = _inputs(CIRCUIT_PROBLEM)
    student = KGGraph(nodes=[_eq_node("stu_eq", CIRCUIT_DERIVED)], edges=[])

    result = resolve_attempt(
        student,
        inputs.candidates,
        symbolic_mappings=inputs.symbolic_mappings,
    )

    assert result.llm_calls == 0
    rn = result.resolved[0]
    assert rn.resolution == "resolved"
    assert rn.resolved_key == "eq.balanced_loop"


# ---------------------------------------------------------------------------
# Test 3 — the equation endpoint resolves via the derived tier; the USES edge
# drops because the procedure step is unresolved (no adjudicator, Task 4).
# ---------------------------------------------------------------------------


def test_nonfluid_circuit_equation_resolves_via_derived_tier():
    """The equation endpoint resolves via the derived tier. The procedure step
    stays unresolved (no LLM adjudicator, Task 4), so the incident USES edge
    drops. The core proof is the equation-node resolution method."""
    inputs = _inputs(CIRCUIT_PROBLEM)
    eq_node = _eq_node("stu_eq", CIRCUIT_DERIVED)
    proc_node = _proc_node("stu_proc", "set Ra equal to Rb so the resistive drops cancel")
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

    # The equation resolves via the derived tier.
    eq_rn = next(rn for rn in resolution.resolved if rn.node_id == "stu_eq")
    assert eq_rn.resolution == "resolved"
    assert eq_rn.resolved_key == "eq.balanced_loop"
    assert resolution.llm_calls == 0

    # The proc node stays unresolved; the USES edge drops.
    proc_rn = next(rn for rn in resolution.resolved if rn.node_id == "stu_proc")
    assert proc_rn.resolution == "unresolved"

    s_norm = build_student_canonical(student, resolution)
    assert s_norm.dropped_edge_count == 1
    assert "stu_eq" not in {nid for nid, _ in s_norm.unresolved_nodes}
