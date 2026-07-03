"""A1 iteration 2 — the default-OFF ``equivalence`` tier
(``match_algebraic_equivalence``) that resolves an equation-variant student
node (subscript notation, rearranged terms, or numeric instantiation of a
credited reference equation) that failed every existing tier.

Gated behind env flag ``APOLLO_EQUIV_RESOLUTION`` (default OFF, mirrors
``apollo.handlers.chat._clarification_enabled``'s idiom). Dormant by default:
every test that exercises the resolver-level wiring explicitly sets/unsets the
flag via ``monkeypatch.setenv`` / ``monkeypatch.delenv`` and restores it.

Precision guardrails (encoded + tested):
  - equation node types only — never attempted for condition/definition/
    procedure_step/simplification/variable_mapping nodes, flag or no flag.
  - a misconception-shaped equation (algebraically DIFFERENT from the
    reference) must NEVER gain credit via this tier.
  - flag OFF => byte-identical resolver output to pre-tier behavior; the tier
    function is never even invoked.
"""

from __future__ import annotations

import pytest

from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import Node, build_node
from apollo.resolution import resolver
from apollo.resolution.candidates import METHOD_CONFIDENCE_CAP, Candidate
from apollo.resolution.equation_alignment import match_algebraic_equivalence
from apollo.resolution.resolver import _EQUIV_RESOLUTION_FLAG, resolve_attempt

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eq_candidate(canonical_key: str, symbolic: str, *, is_misc: bool = False) -> Candidate:
    return Candidate(
        canonical_key=canonical_key,
        canon_key=1,
        node_type="equation",
        is_misconception=is_misc,
        symbolic=symbolic,
        aliases=(),
        display_name=canonical_key,
        opposes_key=None,
    )


def _eq_node(node_id: str, symbolic: str) -> Node:
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=1,
        source="parser",
        content={"symbolic": symbolic, "label": "", "variables": []},
    )


def _cond_node(node_id: str, applies_when: str) -> Node:
    return build_node(
        node_type="condition",
        node_id=node_id,
        attempt_id=1,
        source="parser",
        content={"applies_when": applies_when, "label": ""},
    )


def _cond_candidate(canonical_key: str, alias: str) -> Candidate:
    return Candidate(
        canonical_key=canonical_key,
        canon_key=2,
        node_type="condition",
        is_misconception=False,
        symbolic=None,
        aliases=(alias,),
        display_name=canonical_key,
        opposes_key=None,
    )


# ---------------------------------------------------------------------------
# Pure-unit tests for match_algebraic_equivalence itself (flag-independent —
# the flag gate lives in resolver.py, not in the tier function).
# ---------------------------------------------------------------------------


def test_variant_subscript_match():
    """'A_1*v_1 - (A_2*v_2)' (underscore subscript notation) must equal
    the credited continuity form 'A1*v1 - A2*v2' after subscript normalization.
    A non-equation candidate ahead of the target must be skipped, not crash
    (the closed candidate set always mixes node types)."""
    cond = _cond_candidate("cond.incompressibility", "density is constant")
    cand = _eq_candidate("eq.continuity", "A1*v1 - A2*v2")
    node = _eq_node("stu", "A_1*v_1 - (A_2*v_2)")
    hit = match_algebraic_equivalence(node, (cond, cand), mappings={})
    assert hit is not None
    assert hit[0].canonical_key == "eq.continuity"
    assert hit[1] == "equivalence"
    assert hit[2] == METHOD_CONFIDENCE_CAP["equivalence"]


def test_rearranged_terms_match():
    """A student moves a term across the '=' (same isolated variable, same
    orientation — NOT a full side-swap, which stays sign-guarded) AND uses
    underscore subscript notation the reference doesn't. Neither the symbolic
    nor the derived tier catches this (both choke on the name mismatch before
    ever reaching the rearrangement); the equivalence tier normalizes THEN
    compares zero-forms directly."""
    cand = _eq_candidate("eq.bernoulli_simple", "P2 - P1 = rho*g*(h1 - h2)")
    node = _eq_node("stu", "P_2 = P_1 + rho*g*h_1 - rho*g*h_2")
    hit = match_algebraic_equivalence(node, (cand,), mappings={})
    assert hit is not None
    assert hit[0].canonical_key == "eq.bernoulli_simple"
    assert hit[1] == "equivalence"


def test_numeric_instantiation_match():
    """A fully-numeric student equation that instantiates the reference form
    under the problem's DECLARED given_values must align — no invented data."""
    cand = _eq_candidate("eq.sum_rule", "y - (a + b)")
    node = _eq_node("stu", "y = 8")
    hit = match_algebraic_equivalence(node, (cand,), mappings={}, given_values={"a": "5", "b": "3"})
    assert hit is not None
    assert hit[0].canonical_key == "eq.sum_rule"
    assert hit[1] == "equivalence"


def test_numeric_instantiation_requires_declared_values_not_invented():
    """Without the declared given_values, the same numeric form must NOT align
    (no undeclared-value guessing)."""
    cand = _eq_candidate("eq.sum_rule", "y - (a + b)")
    node = _eq_node("stu", "y = 8")
    hit = match_algebraic_equivalence(node, (cand,), mappings={}, given_values=None)
    assert hit is None


def test_misconception_equation_form_does_not_match():
    """A canonical misconception restated as an equation (algebraically
    DIFFERENT from the correct reference) must NEVER resolve via equivalence —
    equivalence only credits genuine restatements, never wrong physics."""
    correct = _eq_candidate("eq.continuity", "A1*v1 - A2*v2")
    # The classic continuity misconception: student multiplies velocities
    # instead of conserving the volumetric flow rate.
    node = _eq_node("stu", "A_1*v_1 - A_2/v_2")
    hit = match_algebraic_equivalence(node, (correct,), mappings={})
    assert hit is None


def test_non_equation_node_type_returns_none():
    """The tier function itself is a no-op for any non-equation node type —
    independent of the flag (the flag gate lives in the resolver call site)."""
    cand = _cond_candidate("cond.incompressibility", "density is constant")
    node = _cond_node("stu", "density is constant")
    hit = match_algebraic_equivalence(node, (cand,), mappings={})
    assert hit is None


# ---------------------------------------------------------------------------
# Resolver-level wiring: flag gate + byte-identity when off.
# ---------------------------------------------------------------------------


def test_equivalence_cap_is_conservative_and_below_symbolic():
    assert METHOD_CONFIDENCE_CAP["equivalence"] <= METHOD_CONFIDENCE_CAP["symbolic"]
    assert METHOD_CONFIDENCE_CAP["equivalence"] < METHOD_CONFIDENCE_CAP["derived"]


def test_flag_off_by_default_equivalence_matchable_node_stays_unresolved(monkeypatch):
    monkeypatch.delenv(_EQUIV_RESOLUTION_FLAG, raising=False)
    cand = _eq_candidate("eq.continuity", "A1*v1 - A2*v2")
    graph = KGGraph(nodes=[_eq_node("stu", "A_1*v_1 - (A_2*v_2)")])
    result = resolve_attempt(graph, (cand,))
    rn = result.resolved[0]
    assert rn.resolution == "unresolved"


def test_flag_off_never_invokes_equivalence_tier(monkeypatch):
    monkeypatch.delenv(_EQUIV_RESOLUTION_FLAG, raising=False)

    def _boom(*args, **kwargs):  # pragma: no cover - only invoked if the gate is broken
        raise AssertionError("equivalence tier must not be invoked when the flag is off")

    monkeypatch.setattr(resolver, "match_algebraic_equivalence", _boom)
    cand = _eq_candidate("eq.continuity", "A1*v1 - A2*v2")
    graph = KGGraph(nodes=[_eq_node("stu", "A_1*v_1 - (A_2*v_2)")])
    result = resolve_attempt(graph, (cand,))
    assert result.resolved[0].resolution == "unresolved"


@pytest.mark.parametrize("flag_value", ["1", "true", "yes"])
def test_flag_on_resolves_variant_subscript_node(monkeypatch, flag_value):
    monkeypatch.setenv(_EQUIV_RESOLUTION_FLAG, flag_value)
    cand = _eq_candidate("eq.continuity", "A1*v1 - A2*v2")
    graph = KGGraph(nodes=[_eq_node("stu", "A_1*v_1 - (A_2*v_2)")])
    result = resolve_attempt(graph, (cand,))
    rn = result.resolved[0]
    assert rn.resolution == "resolved"
    assert rn.resolved_key == "eq.continuity"
    assert rn.method == "equivalence"
    assert rn.confidence == METHOD_CONFIDENCE_CAP["equivalence"]


def test_flag_on_still_gates_out_non_equation_node_types(monkeypatch):
    """Flag ON, but the unresolved node is a condition, not an equation — the
    equivalence tier must never be attempted for it (no resolution appears)."""
    monkeypatch.setenv(_EQUIV_RESOLUTION_FLAG, "1")
    cand = _cond_candidate("cond.incompressibility", "density is constant")
    graph = KGGraph(nodes=[_cond_node("stu", "totally unrelated prose")])
    result = resolve_attempt(graph, (cand,))
    rn = result.resolved[0]
    assert rn.resolution == "unresolved"
    assert rn.method != "equivalence"


def test_flag_on_does_not_credit_misconception_equation_form(monkeypatch):
    monkeypatch.setenv(_EQUIV_RESOLUTION_FLAG, "1")
    correct = _eq_candidate("eq.continuity", "A1*v1 - A2*v2")
    graph = KGGraph(nodes=[_eq_node("stu", "A_1*v_1 - A_2/v_2")])
    result = resolve_attempt(graph, (correct,))
    rn = result.resolved[0]
    assert rn.resolution == "unresolved"


def test_flag_on_uses_given_values_for_numeric_instantiation(monkeypatch):
    monkeypatch.setenv(_EQUIV_RESOLUTION_FLAG, "1")
    cand = _eq_candidate("eq.sum_rule", "y - (a + b)")
    graph = KGGraph(nodes=[_eq_node("stu", "y = 8")])
    result = resolve_attempt(graph, (cand,), given_values={"a": "5", "b": "3"})
    rn = result.resolved[0]
    assert rn.resolution == "resolved"
    assert rn.resolved_key == "eq.sum_rule"
    assert rn.method == "equivalence"
