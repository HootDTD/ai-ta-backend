"""P1.3 — turn-level sufficiency verdict tests.

Covers:
- `sufficient` when SymPy decisively solves and reference diff is empty.
- `almost` when SymPy solves but reference diff is non-empty (rubric gap),
  AND when there's exactly one missing variable with a defining equation.
- `insufficient` for empty / partial KG.
- Hint selection picks the earliest unmet reference node.
- Malformed equations soft-fail to insufficient with confidence 0.0.
"""
from __future__ import annotations

from apollo.ontology import (
    Edge,
    EdgeType,
    KGGraph,
    build_node,
)
from apollo.solver.sufficiency import (
    SufficiencyVerdict,
    check_sufficiency,
)


PROBLEM = {
    "id": "bernoulli_horizontal_pipe_find_p2",
    "given_values": {
        "rho": 1000, "A1": 0.01, "P1": 200000, "v1": 2.0,
        "A2": 0.005, "h1": 0, "h2": 0, "g": 9.81,
    },
    "target_unknown": "P2",
}


def _kg(equations):
    return {
        "equation": [{"symbolic": s, "label": lab} for (s, lab) in equations],
        "definition": [],
        "condition": [],
        "simplification": [],
        "variable_mapping": [],
    }


def _ref_graph(*, complete: bool = True) -> KGGraph:
    """Authored reference for PROBLEM. `complete=False` drops continuity to
    simulate a half-built reference (used by hint-selection tests)."""
    nodes = []
    if complete:
        nodes.append(build_node(
            node_type="equation",
            node_id="ref_continuity",
            attempt_id=0,
            source="reference",
            content={
                "symbolic": "rho*A1*v1 - rho*A2*v2",
                "label": "Continuity",
            },
        ))
    nodes.append(build_node(
        node_type="equation",
        node_id="ref_bernoulli",
        attempt_id=0,
        source="reference",
        content={
            "symbolic": (
                "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 "
                "- (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)"
            ),
            "label": "Bernoulli",
        },
    ))
    return KGGraph(nodes=nodes)


# ---------------------------------------------------------------------------
# sufficient
# ---------------------------------------------------------------------------

def test_sufficient_when_kg_solves_and_reference_complete():
    student = _kg([
        ("rho*A1*v1 - rho*A2*v2", "Continuity"),
        ("P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 "
         "- (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)", "Bernoulli"),
    ])
    verdict = check_sufficiency(
        kg=student, problem=PROBLEM, reference_graph=_ref_graph(complete=True),
    )
    assert isinstance(verdict, SufficiencyVerdict)
    assert verdict.state == "sufficient"
    assert verdict.missing_variables == ()
    assert verdict.missing_kg_nodes == ()
    assert verdict.next_premise_hint is None
    assert verdict.confidence == 1.0


def test_sufficient_without_reference_graph():
    """Reference graph is optional — verdict still works on SymPy alone."""
    student = _kg([
        ("rho*A1*v1 - rho*A2*v2", "Continuity"),
        ("P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 "
         "- (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)", "Bernoulli"),
    ])
    verdict = check_sufficiency(kg=student, problem=PROBLEM)
    assert verdict.state == "sufficient"


# ---------------------------------------------------------------------------
# almost
# ---------------------------------------------------------------------------

def test_almost_when_solver_solves_but_reference_diff_nonempty():
    """SymPy decisive, but rubric still expects more — downgrade to almost."""
    # Student has both solving equations but the reference also includes a
    # different-content equation we haven't taught (synthetic — simulate a
    # rubric-only requirement).
    student = _kg([
        ("rho*A1*v1 - rho*A2*v2", "Continuity"),
        ("P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 "
         "- (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)", "Bernoulli"),
    ])
    # Reference has continuity + bernoulli + a third "energy_balance"
    # the student hasn't taught.
    ref_nodes = _ref_graph(complete=True).nodes + [
        build_node(
            node_type="equation",
            node_id="ref_energy",
            attempt_id=0,
            source="reference",
            content={"symbolic": "E1 - E2", "label": "Energy Balance"},
        )
    ]
    ref = KGGraph(nodes=ref_nodes)
    verdict = check_sufficiency(kg=student, problem=PROBLEM, reference_graph=ref)
    assert verdict.state == "almost"
    assert "ref_energy" in verdict.missing_kg_nodes
    assert verdict.next_premise_hint is not None
    assert "Energy Balance" in verdict.next_premise_hint


def test_almost_when_one_missing_variable_with_defining_equation():
    """Stuck with single missing var → defining-equation lookup → almost."""
    # Student has only Bernoulli; v2 is missing; reference has continuity
    # which mentions v2 — that's the defining equation.
    student = _kg([
        ("P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 "
         "- (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)", "Bernoulli"),
    ])
    verdict = check_sufficiency(
        kg=student, problem=PROBLEM, reference_graph=_ref_graph(complete=True),
    )
    assert verdict.state == "almost"
    assert verdict.missing_variables == ("v2",)
    assert verdict.confidence == 0.7


def test_almost_demoted_to_insufficient_without_defining_equation():
    """One missing variable but reference can't supply a defining equation
    → insufficient, not almost."""
    student = _kg([
        ("P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 "
         "- (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)", "Bernoulli"),
    ])
    # complete=False drops the continuity equation (the v2 definer) from
    # the reference.
    verdict = check_sufficiency(
        kg=student, problem=PROBLEM, reference_graph=_ref_graph(complete=False),
    )
    assert verdict.state == "insufficient"
    assert "v2" in verdict.missing_variables


# ---------------------------------------------------------------------------
# insufficient
# ---------------------------------------------------------------------------

def test_insufficient_for_empty_kg():
    student = _kg([])
    verdict = check_sufficiency(
        kg=student, problem=PROBLEM, reference_graph=_ref_graph(complete=True),
    )
    assert verdict.state == "insufficient"
    # Empty KG; the existing forward_chain returns missing_variables=[target].
    assert "P2" in verdict.missing_variables
    # next_premise_hint should pull from the reference (earliest in
    # PRECEDES order — but with no PRECEDES edges it falls to insertion
    # order, so the first equation node).
    assert verdict.next_premise_hint is not None


# ---------------------------------------------------------------------------
# hint selection
# ---------------------------------------------------------------------------

def test_hint_picks_earliest_missing_reference_node():
    """When several reference nodes are unmet, the hint is the earliest in
    PRECEDES order (procedure-aware), else the first in insertion order."""
    student = _kg([])
    ref = _ref_graph(complete=True)
    verdict = check_sufficiency(kg=student, problem=PROBLEM, reference_graph=ref)
    # Both ref equations are unmet. Insertion order: continuity, bernoulli.
    # The first hint should mention continuity.
    assert verdict.next_premise_hint is not None
    assert "Continuity" in verdict.next_premise_hint


# ---------------------------------------------------------------------------
# soft-fail on parse error
# ---------------------------------------------------------------------------

def test_malformed_equation_soft_fails_to_insufficient():
    student = _kg([("@@ broken @@", "Garbage")])
    verdict = check_sufficiency(
        kg=student, problem=PROBLEM, reference_graph=_ref_graph(complete=True),
    )
    assert verdict.state == "insufficient"
    assert verdict.confidence == 0.0
    # Trace records the solver_error op.
    assert any(t.get("op") == "solver_error" for t in verdict.trace)
