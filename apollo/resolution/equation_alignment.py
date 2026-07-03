"""Phase 1a — the deterministic ``derived`` equation-alignment tier.

A student equation stated as a *solved/rearranged form* of a reference equation
(e.g. ``realGDP = nomGDP/(PI/100)`` for the deflator definition
``deflator = (nomGDP/realGDP)*100``) is NOT sign-exact-equal to the reference, so
the existing symbolic tier (:func:`apollo.resolution.tiers.match_symbolic`)
rejects it and it lands ``unresolved``.

:func:`match_equation_alignment` adds ONE pure tier that, for each free symbol
``x`` in the reference zero-form ``r``, computes ``solve(Eq(r, 0), x)`` and tests
whether any solved branch's zero-form matches the student zero-form ``s``
symbolically — under DECLARED ``mappings`` ONLY. First candidate with any
matching branch aligns -> method ``derived``, cap ``0.95``.

Precision guardrails (encoded + tested):
  (i)  NO numeric-given substitution — ``given_values`` are never read here, so a
       purely-numeric student form has no symbolic tie to ``r`` and stays
       unresolved.
  (ii) Only DECLARED ``mappings`` may be applied — a form matching only after an
       UNDECLARED simplification does NOT align.
  (iii) Floats are Rational-ized before comparison (canonicalization, NOT value
       injection); determinism is pinned to sympy 1.14.0.

REUSES ``_extended_locals`` / ``_zero_form`` / ``student_surface_text`` from
:mod:`apollo.resolution.tiers` (imported, never reimplemented). Every SymPy call
is wrapped ``try/except -> non-match`` (mirrors ``tiers._symbolic_equiv``): a
pathological solve degrades to a non-match, never an exception.
"""

from __future__ import annotations

import re
from typing import Any

from sympy import Eq, Symbol, nsimplify, simplify, solve

from apollo.ontology.nodes import Node
from apollo.resolution.candidates import Candidate
from apollo.resolution.tiers import (
    TierHit,
    _extended_locals,
    _zero_form,
    student_surface_text,
)

# The single method/cap this tier reports. The resolver re-derives the reported
# confidence from METHOD_CONFIDENCE_CAP["derived"]; the raw score == the cap.
_DERIVED_METHOD = "derived"
_DERIVED_CAP = 0.95


# ---------------------------------------------------------------------------
# Typed wrappers around SymPy's Any-typed returns (mypy, L9 BLOCKING).
# ---------------------------------------------------------------------------


def _rationalize(expr: Any) -> Any | None:
    """``nsimplify(expr, rational=True)`` (float -> Rational canonicalization),
    or None on any SymPy failure (a non-canonicalizable expr is a non-match)."""
    try:
        return nsimplify(expr, rational=True)
    except Exception:  # noqa: BLE001 - non-canonicalization is a non-match  # pragma: no cover - defensive
        return None


def _apply_mappings(expr: Any, mappings: dict[str, str], local_dict: dict) -> Any | None:
    """Substitute every DECLARED ``mappings`` symbol in ``expr`` with its parsed
    replacement zero-form (mirrors ``_symbolic_equiv``). None on failure."""
    out = expr
    for sym, repl in mappings.items():
        repl_expr = _zero_form(repl, local_dict)
        if repl_expr is None:  # pragma: no cover - declared mappings parse in practice
            continue
        try:
            out = out.subs(Symbol(sym), repl_expr)
        except Exception:  # noqa: BLE001 - sub failure is a non-match  # pragma: no cover - defensive
            return None
    return out


def _free_symbols_sorted(expr: Any) -> list[Any]:
    """The expression's free symbols in a deterministic (str-sorted) order."""
    try:
        return sorted(expr.free_symbols, key=str)
    except Exception:  # noqa: BLE001 - no free symbols is a non-match  # pragma: no cover - defensive
        return []


def _solve_for(expr: Any, symbol: Any) -> list[Any]:
    """``solve(Eq(expr, 0), symbol)`` branches in a deterministic (str-sorted)
    order; [] on any SymPy failure (an unsolvable form is a non-match)."""
    try:
        branches = solve(Eq(expr, 0), symbol)
    except Exception:  # noqa: BLE001 - unsolvable is a non-match  # pragma: no cover - defensive
        return []
    try:
        return sorted(branches, key=str)
    except Exception:  # noqa: BLE001 - unorderable is a non-match  # pragma: no cover - defensive
        return []


def _is_zero(expr: Any) -> bool:
    """``simplify(expr) == 0`` wrapped (mirrors ``_symbolic_equiv``)."""
    try:
        return bool(simplify(expr) == 0)
    except Exception:  # noqa: BLE001 - comparison failure is a non-match  # pragma: no cover - defensive
        return False


def _aligns(student_zero: Any, reference_zero: Any) -> bool:
    """True iff some solved branch of the reference zero-form reproduces the
    student zero-form. For each free symbol ``x`` of the reference, solve for
    ``x`` and test whether ``(x - branch)`` is structurally equal to the student
    zero-form ``s`` (i.e. ``simplify((x - branch) - s) == 0``)."""
    for x in _free_symbols_sorted(reference_zero):
        for branch in _solve_for(reference_zero, x):
            if _is_zero((x - branch) - student_zero):
                return True
    return False


def match_equation_alignment(
    node: Node,
    candidates: tuple[Candidate, ...],
    *,
    mappings: dict[str, str],
) -> TierHit | None:
    """The ``derived`` tier: equation nodes only.

    Returns ``(cand, "derived", 0.95)`` for the FIRST candidate whose reference
    equation, solved for one of its free symbols (under the DECLARED ``mappings``
    only), reproduces the student's rearranged form; else None.
    """
    if node.node_type != "equation":
        return None
    student_sym = student_surface_text(node)
    if not student_sym:  # pragma: no cover - defensive: valid equation nodes always have symbolic
        return None
    maps = mappings or {}

    for cand in candidates:
        if cand.node_type != "equation" or cand.symbolic is None:
            continue
        ld = _extended_locals(student_sym, cand.symbolic, *maps.values())

        s = _zero_form(student_sym, ld)
        if s is None:  # pragma: no cover - defensive: valid equation nodes parse
            continue
        s = _apply_mappings(s, maps, ld)
        if s is None:  # pragma: no cover - defensive
            continue
        s = _rationalize(s)
        if s is None:  # pragma: no cover - defensive
            continue

        r = _zero_form(cand.symbolic, ld)
        if r is None:  # pragma: no cover - defensive: reference equations parse
            continue
        r = _apply_mappings(r, maps, ld)
        if r is None:  # pragma: no cover - defensive
            continue
        r = _rationalize(r)
        if r is None:  # pragma: no cover - defensive
            continue

        if _aligns(s, r):
            return (cand, _DERIVED_METHOD, _DERIVED_CAP)

    return None


# ---------------------------------------------------------------------------
# A1-iter2 — the default-OFF ``equivalence`` tier (APOLLO_EQUIV_RESOLUTION).
#
# The biggest single unresolved-student-node failure class in the A1 taxonomy
# (19/53) is "equation variants": a student restates an already-credited
# reference equation with (a) underscore/notation subscript variants
# (``A_1*v_1`` vs the reference's ``A1*v1``), (b) terms rearranged across the
# ``=``, or (c) a fully numeric instantiation of the reference form under the
# problem's OWN declared ``given_values`` (never invented data).
#
# Unlike the ``derived`` tier above (which solves the reference for ONE free
# symbol and tests a single rearranged branch — the "solved-for-variable"
# shape), this tier tests direct zero-form equality after (i) normalizing
# underscore-subscript notation and (ii) substituting BOTH the declared
# ``mappings`` (symbolic_mappings) AND the declared ``given_values`` (numeric
# knowns already known to the graph-sim — plumbed in via ProblemInputs). A
# single ``simplify(student_zero - reference_zero) == 0`` check subsumes
# rearrangement (algebra) and numeric instantiation (arithmetic) — no solve()
# branching needed. It reuses every SymPy helper the derived tier already
# has: ``_extended_locals`` / ``_zero_form`` / ``_apply_mappings`` /
# ``_rationalize`` / ``_is_zero`` — no new SymPy call site, no new failure
# mode, no new timeout mechanism (the module has none to reuse; every SymPy
# call here is wrapped ``try/except -> non-match`` exactly like the tiers it
# borrows from — a pathological expression degrades to a non-match, never
# hangs the resolver any differently than the derived/symbolic tiers already
# can).
#
# Deliberately conservative: cap 0.93 (< derived's 0.95, < symbolic's 0.98 —
# see candidates.py). It is a LAST-RESORT tier, tried only after every other
# content tier has already failed for the node (wired in resolver.py's
# ``_content_match``), and ONLY for ``equation`` nodes — a misconception
# equation form is algebraically DIFFERENT from the correct reference, so
# ``simplify`` correctly reports non-zero and this tier never credits it.
# ---------------------------------------------------------------------------

_EQUIVALENCE_METHOD = "equivalence"
_EQUIVALENCE_CAP = 0.93

# Subscript notation normalization: 'A_1' / 'v_12' -> 'A1' / 'v12'. Applied to
# BOTH the student and reference symbolic text (and to mapping/given_values
# keys) before parsing, so notation alone never blocks a genuine restatement.
_SUBSCRIPT_RE = re.compile(r"([A-Za-z]+)_([0-9]+)")


def _normalize_subscripts(expr: str) -> str:
    return _SUBSCRIPT_RE.sub(r"\1\2", expr)


def match_algebraic_equivalence(
    node: Node,
    candidates: tuple[Candidate, ...],
    *,
    mappings: dict[str, str],
    given_values: dict[str, str] | None = None,
) -> TierHit | None:
    """The ``equivalence`` tier: equation nodes only, gated by the caller on
    ``APOLLO_EQUIV_RESOLUTION`` (resolver.py) — this function itself performs
    NO env read, so it stays a pure function like every other tier.

    Returns ``(cand, "equivalence", 0.93)`` for the FIRST candidate whose
    subscript-normalized, mapping-and-given-values-substituted zero-form is
    identically equal (``simplify(diff) == 0``) to the student's; else None.
    ``given_values`` defaults to ``{}`` — with none supplied, a purely numeric
    student form has nothing to instantiate against and stays unresolved (no
    invented data, mirrors the derived tier's guardrail (i))."""
    if node.node_type != "equation":
        return None
    student_sym = student_surface_text(node)
    if not student_sym:  # pragma: no cover - defensive: valid equation nodes always have symbolic
        return None
    student_norm = _normalize_subscripts(student_sym)

    maps = mappings or {}
    knowns = given_values or {}
    # Declared symbolic_mappings win over given_values on a key collision (the
    # explicit substitution table is the more authoritative declared datum).
    merged = {_normalize_subscripts(k): v for k, v in knowns.items()}
    merged.update({_normalize_subscripts(k): v for k, v in maps.items()})

    for cand in candidates:
        if cand.node_type != "equation" or cand.symbolic is None:
            continue
        cand_norm = _normalize_subscripts(cand.symbolic)
        ld = _extended_locals(student_norm, cand_norm, *merged.values())

        s = _zero_form(student_norm, ld)
        if s is None:  # pragma: no cover - defensive: valid equation nodes parse
            continue
        s = _apply_mappings(s, merged, ld)
        if s is None:  # pragma: no cover - defensive
            continue
        s = _rationalize(s)
        if s is None:  # pragma: no cover - defensive
            continue

        r = _zero_form(cand_norm, ld)
        if r is None:  # pragma: no cover - defensive: reference equations parse
            continue
        r = _apply_mappings(r, merged, ld)
        if r is None:  # pragma: no cover - defensive
            continue
        r = _rationalize(r)
        if r is None:  # pragma: no cover - defensive
            continue

        if _is_zero(s - r):
            return (cand, _EQUIVALENCE_METHOD, _EQUIVALENCE_CAP)

    return None
