"""Pure-unit tests for the Phase-1a deterministic ``derived`` equation-alignment
tier (``match_equation_alignment``).

The tier resolves a student equation stated as a *solved/rearranged form* of a
reference equation to that reference entity, WITHOUT crediting numeric-coincidence,
sign-flipped, or undeclared-simplification forms. It is symbolic-only: it solves
the reference zero-form for each free symbol and tests structural equality against
the student zero-form under DECLARED ``mappings`` only (no numeric-given
substitution).

Determinism is asserted against the pinned sympy 1.14.0.
"""

from __future__ import annotations

import pytest
import sympy

from apollo.ontology.nodes import Node, build_node
from apollo.resolution.candidates import Candidate
from apollo.resolution.equation_alignment import match_equation_alignment

# The tier's solve()-branch-order determinism was validated against this sympy.
# Pinned in requirements; the determinism test below skips (not collection-errors)
# on any other version, so a future sympy bump never breaks this whole module.
_SYMPY_PINNED = "1.14.0"


def _eq_candidate(canonical_key: str, symbolic: str) -> Candidate:
    return Candidate(
        canonical_key=canonical_key,
        canon_key=1,
        node_type="equation",
        is_misconception=False,
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


def _proc_node(node_id: str, action: str) -> Node:
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=1,
        source="parser",
        content={"action": action, "purpose": ""},
    )


# ---------------------------------------------------------------------------
# POSITIVE — the deflator rearrangement (a genuine solve-for-variable form).
# ---------------------------------------------------------------------------


def test_deflator_rearrangement_aligns_via_derived():
    cand = _eq_candidate("eq.gdp_deflator", "deflator - (nomGDP/realGDP)*100")
    node = _eq_node("stu", "realGDP - nomGDP/(PI/100)")
    hit = match_equation_alignment(node, (cand,), mappings={"PI": "deflator"})
    assert hit is not None
    assert hit[0].canonical_key == "eq.gdp_deflator"
    assert hit[1] == "derived"
    assert hit[2] == 0.95


def test_torricelli_solve_for_v2_aligns_via_derived():
    """A genuine solve-for-variable form: ``v2 = sqrt(2*g*h1)`` is the solved form
    of ``v2**2 = 2*g*h1``. It is NOT substitution-collapsible, so the symbolic
    tier rejects it and the derived tier aligns it (no declared mappings)."""
    cand = _eq_candidate("eq.torricelli", "v2**2 - 2*g*h1")
    node = _eq_node("stu", "v2 - sqrt(2*g*h1)")
    hit = match_equation_alignment(node, (cand,), mappings={})
    assert hit is not None
    assert hit[0].canonical_key == "eq.torricelli"
    assert hit[1] == "derived"
    assert hit[2] == 0.95


# ---------------------------------------------------------------------------
# NEGATIVE CONTROLS — every one must stay unresolved (return None).
# ---------------------------------------------------------------------------


def test_sign_flipped_rearrangement_does_not_align():
    """Wrong direction: ``realGDP = nomGDP*(PI/100)`` is the sign/operation-flipped
    rearrangement; no solved branch reproduces it."""
    cand = _eq_candidate("eq.gdp_deflator", "deflator - (nomGDP/realGDP)*100")
    node = _eq_node("stu", "realGDP - nomGDP*(PI/100)")
    assert match_equation_alignment(node, (cand,), mappings={"PI": "deflator"}) is None


def test_unrelated_equation_does_not_align():
    """No shared structure: a heat-equation student form vs the deflator def."""
    cand = _eq_candidate("eq.gdp_deflator", "deflator - (nomGDP/realGDP)*100")
    node = _eq_node("stu", "Q - m*c*dT")
    assert match_equation_alignment(node, (cand,), mappings={"PI": "deflator"}) is None


def test_numeric_coincidence_form_does_not_align():
    """Hard exclusion (i): NO numeric-given substitution. The Q5 real_gdp_growth
    numeric-computed form ``growth - (10739.0/2859.5)*100`` has no symbolic tie to
    the growth reference and stays unresolved (the documented recall gap)."""
    cand = _eq_candidate("eq.real_gdp_growth", "growth - ((realGDP2 - realGDP1)/realGDP1)*100")
    node = _eq_node("stu", "growth - (10739.0/2859.5)*100")
    assert match_equation_alignment(node, (cand,), mappings={}) is None


def test_undeclared_simplification_form_does_not_align():
    """Hard exclusion (ii): only DECLARED mappings may be applied. The deflator
    rearrangement aligns ONLY under ``{PI: deflator}``; with NO declared mapping
    the same student form must NOT align."""
    cand = _eq_candidate("eq.gdp_deflator", "deflator - (nomGDP/realGDP)*100")
    node = _eq_node("stu", "realGDP - nomGDP/(PI/100)")
    assert match_equation_alignment(node, (cand,), mappings={}) is None


# ---------------------------------------------------------------------------
# Type / structural guards.
# ---------------------------------------------------------------------------


def test_non_equation_node_returns_none():
    cand = _eq_candidate("eq.gdp_deflator", "deflator - (nomGDP/realGDP)*100")
    proc = _proc_node("stu", "rearrange the deflator definition")
    assert match_equation_alignment(proc, (cand,), mappings={"PI": "deflator"}) is None


def test_non_equation_candidate_is_skipped():
    """A non-equation candidate (no ``symbolic``) is skipped, not aligned."""
    proc_cand = Candidate(
        canonical_key="proc.x",
        canon_key=1,
        node_type="procedure_step",
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name="proc.x",
        opposes_key=None,
    )
    node = _eq_node("stu", "realGDP - nomGDP/(PI/100)")
    assert match_equation_alignment(node, (proc_cand,), mappings={"PI": "deflator"}) is None


# ---------------------------------------------------------------------------
# Determinism — pinned to sympy 1.14.0; each positive is stable across runs.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sympy.__version__ != _SYMPY_PINNED,
    reason=f"solve()-branch-order determinism validated against sympy {_SYMPY_PINNED}",
)
def test_positives_are_deterministic_across_runs():
    deflator_cand = _eq_candidate("eq.gdp_deflator", "deflator - (nomGDP/realGDP)*100")
    deflator_node = _eq_node("stu", "realGDP - nomGDP/(PI/100)")
    torricelli_cand = _eq_candidate("eq.torricelli", "v2**2 - 2*g*h1")
    torricelli_node = _eq_node("stu", "v2 - sqrt(2*g*h1)")
    for _ in range(2):
        d = match_equation_alignment(deflator_node, (deflator_cand,), mappings={"PI": "deflator"})
        t = match_equation_alignment(torricelli_node, (torricelli_cand,), mappings={})
        assert d is not None and d[0].canonical_key == "eq.gdp_deflator"
        assert d[1] == "derived" and d[2] == 0.95
        assert t is not None and t[0].canonical_key == "eq.torricelli"
        assert t[1] == "derived" and t[2] == 0.95
