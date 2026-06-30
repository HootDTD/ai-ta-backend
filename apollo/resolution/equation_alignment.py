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
