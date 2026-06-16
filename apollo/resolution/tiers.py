"""WU-3C2 — content tiers: the strong content signal that seeds anchor matches.

Matching order is content-first (§5 step 1): exact key/content -> SymPy
structural equivalence (sign-exact, under declared mappings like ``d = 2r``,
REUSES ``parse_zero_form``) -> normalized alias / ``normalization_map`` ->
RapidFuzz >= 0.9. Each matcher is a pure function
``(student_node, candidates, ...) -> (Candidate, method, raw_score) | None``.

The symbolic tier extends ``sympy_exec._local_dict()`` with the equation's free
variables plus any declared-mapping symbols WITHOUT modifying the solver
(``sympy_exec.py`` is consumed, never edited): the extra symbols are registered
locally in ``_symbolic_equiv`` and the mapping is substituted before the
sign-exact ``simplify(a - b) == 0`` comparison.

``_fuzzy_ratio`` is the ONLY RapidFuzz site: ``token_set_ratio`` normalized to
0..1. ``token_set_ratio`` (not bare ``ratio``) is used because student
paraphrases reorder and pad words ("the density stays constant throughout" vs
"density is constant"); the set ratio is order-insensitive while still
discriminating disjoint phrases below the 0.9 threshold.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz
from sympy import Symbol, simplify
from sympy.parsing.sympy_parser import parse_expr

from apollo.ontology.nodes import Node, NodeType
from apollo.resolution.candidates import Candidate
from apollo.solver.sympy_exec import _local_dict

# Tier result type: (winning candidate, method, raw 0..1 score) or None.
TierHit = tuple[Candidate, str, float]


# ---------------------------------------------------------------------------
# Student surface text — the comparable string for each node type.
# ---------------------------------------------------------------------------

def student_surface_text(node: Node) -> str:
    """The text the alias/fuzzy tiers compare for a student node.

    Equation -> symbolic; condition/simplification -> applies_when (+
    transformation); definition -> concept + meaning; procedure_step -> action;
    variable_mapping -> term. Falls back to an empty string for an unknown
    shape (a non-match is data, never a crash)."""
    c = node.content
    node_type: NodeType = node.node_type
    if node_type == "equation":
        return getattr(c, "symbolic", "") or ""
    if node_type == "condition":
        return getattr(c, "applies_when", "") or ""
    if node_type == "simplification":
        applies = getattr(c, "applies_when", "") or ""
        transform = getattr(c, "transformation", "") or ""
        return f"{applies} {transform}".strip()
    if node_type == "definition":
        concept = getattr(c, "concept", "") or ""
        meaning = getattr(c, "meaning", "") or ""
        return f"{concept} {meaning}".strip()
    if node_type == "procedure_step":
        return getattr(c, "action", "") or ""
    if node_type == "variable_mapping":
        return getattr(c, "term", "") or ""
    return ""  # pragma: no cover - exhaustive over the six node types


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace + strip surrounding punctuation for the
    alias tier's exact-after-normalization comparison."""
    return re.sub(r"\s+", " ", text.strip().lower()).strip(".,;:!?")


# ---------------------------------------------------------------------------
# Tier 1 — exact key / identical content.
# ---------------------------------------------------------------------------

def match_exact(node: Node, candidates: tuple[Candidate, ...]) -> TierHit | None:
    """Exact match: the node's display label OR normalized surface text equals
    a candidate's ``canonical_key`` or (for equations) its ``symbolic``.

    This fires when the parser already emitted a canonical key (label) or the
    student typed the reference equation verbatim."""
    label = _normalize(getattr(node.content, "label", "") or "")
    surface = _normalize(student_surface_text(node))
    for cand in candidates:
        if cand.node_type != node.node_type:
            continue
        if label and label == _normalize(cand.canonical_key):
            return (cand, "exact", 1.0)
        if cand.symbolic is not None and surface and surface == _normalize(cand.symbolic):
            return (cand, "exact", 1.0)
    return None


# ---------------------------------------------------------------------------
# Tier 2 — SymPy structural equivalence (sign-exact, declared mappings).
# ---------------------------------------------------------------------------

# Names parse_expr should resolve to SymPy callables/constants, never plain
# Symbols — keep them OUT of the extended locals so `pi`, `Rational`, etc.
# retain their meaning.
_SYMPY_RESERVED = {"pi", "E", "I", "oo", "Rational", "sqrt", "exp", "log"}


def _extended_locals(*expressions: str) -> dict:
    """``sympy_exec._local_dict()`` extended with every free-looking symbol in
    the given expressions (letters + optional trailing digits). Lets the
    symbolic tier parse equations with variables outside the canonical fluid
    set (e.g. circle ``r`` / ``d``) WITHOUT editing the solver. SymPy reserved
    names (``pi``, ``Rational``, ...) are never shadowed."""
    ld = dict(_local_dict())
    for expr in expressions:
        for name in re.findall(r"[A-Za-z]+[0-9]*", expr):
            if name in ld or name in _SYMPY_RESERVED:
                continue
            ld[name] = Symbol(name)
    return ld


def _zero_form(symbolic: str, local_dict: dict):
    """Parse 'LHS = RHS' (or a bare expression) to the LHS - RHS zero-form
    under ``local_dict``. Returns None on any parse failure (a non-parse is a
    non-match, never a crash)."""
    s = symbolic.strip()
    if "=" in s:
        parts = s.split("=")
        if len(parts) != 2:
            return None
        lhs, rhs = parts
        s = f"({lhs.strip()}) - ({rhs.strip()})"
    try:
        return parse_expr(s, local_dict=local_dict)
    except Exception:  # noqa: BLE001 - non-parse is a non-match, not an error
        return None


def _symbolic_equiv(student: str, reference: str, *, mappings: dict[str, str]) -> bool:
    """True iff the two equations are structurally equivalent (sign-exact)
    after applying the declared variable ``mappings`` (e.g. ``{'d': '2*r'}``).

    Comparison is ``simplify(a - b) == 0`` over the zero-forms — sign-exact, so
    an inverted equation does NOT match."""
    ld = _extended_locals(student, reference, *mappings.values())
    a = _zero_form(student, ld)
    b = _zero_form(reference, ld)
    if a is None or b is None:
        return False
    for sym, repl in mappings.items():
        repl_expr = _zero_form(repl, ld)
        if repl_expr is None:
            continue
        b = b.subs(Symbol(sym), repl_expr)
        a = a.subs(Symbol(sym), repl_expr)
    try:
        return bool(simplify(a - b) == 0)
    except Exception:  # noqa: BLE001 - comparison failure is a non-match  # pragma: no cover - defensive
        return False


def match_symbolic(
    node: Node,
    candidates: tuple[Candidate, ...],
    *,
    mappings: dict[str, str] | None = None,
) -> TierHit | None:
    """Symbolic tier: equation nodes only. Returns the first candidate whose
    ``symbolic`` is structurally equivalent (sign-exact, under ``mappings``)."""
    if node.node_type != "equation":
        return None
    student_sym = student_surface_text(node)
    if not student_sym:  # pragma: no cover - defensive: valid equation nodes always have symbolic
        return None
    maps = mappings or {}
    for cand in candidates:
        if cand.node_type != "equation" or cand.symbolic is None:
            continue
        if _symbolic_equiv(student_sym, cand.symbolic, mappings=maps):
            return (cand, "symbolic", 1.0)
    return None


# ---------------------------------------------------------------------------
# Tier 3 — normalized alias match.
# ---------------------------------------------------------------------------

def match_alias(node: Node, candidates: tuple[Candidate, ...]) -> TierHit | None:
    """Alias tier: the node's normalized surface text equals one of a
    type-compatible candidate's normalized aliases (WU-3B converted the
    ``normalization_map`` + ``trigger_phrases`` into ``entity.aliases``)."""
    surface = _normalize(student_surface_text(node))
    if not surface:  # pragma: no cover - defensive: valid nodes always have surface text
        return None
    for cand in candidates:
        if cand.node_type != node.node_type:
            continue
        for alias in cand.aliases:
            if surface == _normalize(alias):
                return (cand, "alias", 1.0)
    return None


# ---------------------------------------------------------------------------
# Tier 4 — RapidFuzz >= 0.9 (the only RapidFuzz site).
# ---------------------------------------------------------------------------

def _fuzzy_ratio(a: str, b: str) -> float:
    """Order-insensitive token-set similarity, normalized to 0..1."""
    return fuzz.token_set_ratio(a, b) / 100.0


def match_fuzzy(
    node: Node,
    candidates: tuple[Candidate, ...],
    *,
    threshold: float = 0.9,
) -> TierHit | None:
    """Fuzzy tier: the best alias whose ``_fuzzy_ratio`` >= ``threshold``.

    Below the threshold returns None — never snaps (§5 below-threshold ->
    unresolved). Among above-threshold candidates the highest score wins;
    deterministic tie-break on ``canonical_key`` so re-runs are identical."""
    surface = student_surface_text(node)
    if not surface:  # pragma: no cover - defensive: valid nodes always have surface text
        return None
    best: TierHit | None = None
    for cand in candidates:
        if cand.node_type != node.node_type:
            continue
        for alias in cand.aliases:
            score = _fuzzy_ratio(surface, alias)
            if score < threshold:
                continue
            if (
                best is None
                or score > best[2]
                or (score == best[2] and cand.canonical_key < best[0].canonical_key)
            ):
                best = (cand, "fuzzy", score)
    return best
