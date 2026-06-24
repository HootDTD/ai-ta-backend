"""§8B.4 PURE eight-gate promotion lint (WU-3B2b) — the auto-provisioning SAFETY CORE.

Before an auto-scraped problem is promoted Tier-1 -> Tier-2 (teachable), it must
pass eight gates run IN ORDER. ``run_promotion_lint`` short-circuits on the FIRST
failure and returns a frozen ``PromotionResult(ok, failed_gate, diagnostic)``;
the orchestrator (3B2g, NOT this unit) maps a failure to a rejection row and a
pass to promotion. The gates:

  1. Schema     — ``Problem.model_validate`` (depends_on resolution, uses_equations
                  -> equation id, procedure order 1..N) + every edge in
                  ``EDGE_ALLOWED_PAIRS`` (via ``to_kg_graph``) + a mint-map
                  membership sub-check: any ``entry_type`` NOT in the frozen
                  ``_ENTRY_TYPE_TO_KIND_PREFIX`` fails CLOSED (ADJ #5
                  defense-in-depth — a ``variable_mapping`` step rejects until
                  3B2d additively extends the map).
  2. Closure    — ``validate_reference_graph`` VERBATIM (§6.1; NOT the whole lint).
  3. DAG        — ``KGGraph.topological_order(DEPENDS_ON)`` raises on a cycle.
  4. Symbols    — the SOLE foreign-symbol guard. Gate 6 (``parse_zero_form`` ->
                  ``sympy.parse_expr``) does NOT reject foreign symbols: it
                  auto-creates unknown symbols (§9 FEAS-2 / ADJ #4). Gate 4 reads
                  the PASSED-IN ``canonical_symbols`` / ``normalization_map``
                  (populated by 3B2d) so the core stays pure / DB-free.
  5. Procedure  — one PRECEDES chain covering every procedure step AND the
                  terminal step computes ``target_unknown``.
  6. SymPy      — ``parse_zero_form`` catches MALFORMED equation syntax only.
  7. System     — equation-system closure: a PAPER check (every free symbol is
                  given / target / an intermediate computed by a non-terminal
                  procedure step / cancelled by a simplification), NOT an
                  end-to-end solve (honest v1 limit §8B.4:1347; the per-problem
                  quarantine 3B2h is the runtime catch).
  8. Duplicate  — ``problem_dup_hash(problem)`` NOT in the caller-supplied
                  concept-scoped ``existing_problem_hashes`` (keyed on the BIGINT
                  concept; the lint never queries the DB).

PURE / DB-free / LLM-free: ``canonical_symbols`` / ``normalization_map`` (gate 4)
and ``existing_problem_hashes`` (gate 8) are PASSED IN by the caller. This unit
owns the gate logic + diagnostic ONLY — it does NOT promote, call
``project_canon``, or write ``rejected_problems`` (that wiring is 3B2g's).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pydantic import ValidationError
from sympy.core.cache import clear_cache

from apollo.errors import MalformedEquationError
from apollo.ontology.edges import EdgeType
from apollo.ontology.graph import KGGraph
from apollo.persistence.learner_model_seed import (
    _ENTRY_TYPE_TO_KIND_PREFIX,
    validate_reference_graph,
)
from apollo.provisioning.problem_hash import problem_dup_hash
from apollo.schemas.problem import Problem
from apollo.solver.sympy_exec import parse_zero_form

# Attempt-agnostic: ``to_kg_graph`` uses attempt_id only to stamp nodes/edges,
# never for gate logic. Any fixed int is fine.
_LINT_ATTEMPT_ID = 0

# The full gate universe (1..8). The DEFAULT ``active_gates`` for
# ``run_promotion_lint`` — passing it (or omitting it) reproduces the original
# all-8-gates behavior EXACTLY, so every pre-profile caller and test is unchanged.
# A subject profile passes a SUBSET (e.g. {1,2,3,8} for an argument graph) to turn
# the symbolic gates 4/5 OFF; gate 1 is the structural foundation and ALWAYS runs
# (it builds the Problem + KGGraph the later gates reuse), so it is not gated here.
# NOTE: this module declares its own constant rather than importing it from
# ``subject_profile`` — the lint stays ORM-free / import-light (the spec's
# pure / DB-free / LLM-free contract).
ALL_PROMOTION_GATES: frozenset[int] = frozenset({1, 2, 3, 4, 5, 6, 7, 8})


@dataclass(frozen=True)
class PromotionResult:
    """Outcome of the 8-gate lint. ``failed_gate`` is 1..8 on failure, None on
    pass; ``diagnostic`` is human-readable ("" on pass)."""

    ok: bool
    failed_gate: int | None
    diagnostic: str


# --------------------------------------------------------------------------- #
# Gate-4 helper — symbol normalization (exact -> subscript base -> alias map)
# --------------------------------------------------------------------------- #


def _normalize_symbol(
    name: str,
    canonical_symbols: Iterable[str],
    normalization_map: Mapping[str, str],
) -> str | None:
    """Resolve a raw symbol/alias to its canonical BASE, or None if foreign.

    (a) exact membership in ``canonical_symbols``;
    (b) strip a trailing digit run (``P1`` -> ``P``, ``v2`` -> ``v``,
        ``h12`` -> ``h``) and test the base;
    (c) ``normalization_map`` lookup (phrase/alias -> canonical).
    """
    canon = set(canonical_symbols)
    if name in canon:
        return name
    base = re.sub(r"\d+$", "", name)
    if base in canon:
        return base
    mapped = normalization_map.get(name)
    if mapped is not None:
        return mapped
    return None


# --------------------------------------------------------------------------- #
# Internal accessors over the validated Problem
# --------------------------------------------------------------------------- #


def _equation_steps(problem: Problem) -> list:
    return [s for s in problem.reference_solution if s.entry_type == "equation"]


def _proc_steps(problem: Problem) -> list:
    return [s for s in problem.reference_solution if s.entry_type == "procedure_step"]


def _proc_order(step) -> int:
    return int(step.content.get("order", 0))


def _equation_free_symbols(step) -> set[str]:
    """Free-symbol names of an equation step's ``symbolic``. Returns () when the
    equation is malformed (gate 6 owns that verdict) or has no ``symbolic``.

    DETERMINISM PIN (load-bearing for the sole foreign-symbol guard, gate 4):
    ``parse_zero_form`` -> ``sympy.parse_expr`` auto-creates any symbol not in the
    local dict (e.g. a foreign ``x``) from SymPy's PROCESS-GLOBAL symbol cache. If
    another test (or any earlier parse in the same process) cached that name with
    different assumptions (``zero=True``, ``positive=True``, ...), assumption-driven
    simplification can drop the symbol from ``free_symbols`` — making gate 4's
    verdict order-dependent (a foreign ``x`` could slip through). Clearing the cache
    here forces every symbol to be reconstructed with default assumptions, so the
    free-symbol set depends ONLY on the equation text — never on global cache state.
    Cost is negligible at fixture/lint scale and the cache simply repopulates."""
    symbolic = step.content.get("symbolic")
    if not symbolic:
        return set()
    clear_cache()
    try:
        expr = parse_zero_form(symbolic, entry_id=step.id)
    except MalformedEquationError:
        return set()
    return {s.name for s in expr.free_symbols}


# --------------------------------------------------------------------------- #
# The eight gates — each returns a diagnostic str on FAIL, None on PASS
# --------------------------------------------------------------------------- #


def _gate_1_mint_map(problem: Problem) -> str | None:
    """Mint-map membership sub-check (ADJ #5). Re-derived from the LIVE frozen
    map (NOT hardcoded), so when 3B2d additively extends it this gate
    auto-accepts the new entry_type with no 3B2b edit."""
    for step in problem.reference_solution:
        if step.entry_type not in _ENTRY_TYPE_TO_KIND_PREFIX:
            return (
                f"gate 1: step {step.id!r} entry_type {step.entry_type!r} is not "
                f"in the mint map (fail-closed; allowed: "
                f"{sorted(_ENTRY_TYPE_TO_KIND_PREFIX)})"
            )
    return None


def _gate_2(problem: Problem, graph: dict) -> str | None:
    result = validate_reference_graph(graph)
    if not result.ok:
        return "gate 2: reference closure failed: " + "; ".join(result.errors)
    return None


def _gate_3(problem: Problem, kg: KGGraph) -> str | None:
    try:
        kg.topological_order(EdgeType.DEPENDS_ON)
    except ValueError as exc:
        return f"gate 3: DEPENDS_ON is not acyclic: {exc}"
    return None


def _gate_4(
    problem: Problem,
    canonical_symbols: Iterable[str],
    normalization_map: Mapping[str, str],
) -> str | None:
    symbols: set[str] = set()
    for step in _equation_steps(problem):
        symbols |= _equation_free_symbols(step)
    symbols |= set(problem.given_values.keys())
    symbols.add(problem.target_unknown)
    for name in sorted(symbols):
        if _normalize_symbol(name, canonical_symbols, normalization_map) is None:
            return (
                f"gate 4: foreign symbol {name!r} is not canonical and not "
                f"normalizable (the sole foreign-symbol guard)"
            )
    return None


def _gate_5(problem: Problem, kg: KGGraph) -> str | None:
    procs = kg.by_type("procedure_step")
    heads = [n for n in procs if not kg.incoming(n.node_id, EdgeType.PRECEDES)]
    if len(heads) != 1:
        return f"gate 5: expected exactly one PRECEDES chain head, found {len(heads)}"
    chain = kg.precedes_chain()
    if len(chain) != len(procs):
        return f"gate 5: PRECEDES chain covers {len(chain)}/{len(procs)} procedure steps"

    # Terminal-computes-target: the last procedure step must use >=1 equation
    # whose free symbols include target_unknown.
    terminal_id = chain[-1].node_id
    terminal = next((s for s in _proc_steps(problem) if s.id == terminal_id), None)
    if terminal is None:  # pragma: no cover - defense in depth: gate 1 builds the
        # KG from the validated problem, so chain[-1] is always a real proc step.
        return f"gate 5: terminal step {terminal_id!r} not found among procedure steps"
    used = terminal.content.get("uses_equations", []) or []
    if not used:
        return f"gate 5: terminal step {terminal_id!r} uses no equation"
    eq_by_id = {s.id: s for s in _equation_steps(problem)}
    target = problem.target_unknown
    reaches_target = any(
        target in _equation_free_symbols(eq_by_id[u]) for u in used if u in eq_by_id
    )
    if not reaches_target:
        return (
            f"gate 5: terminal step {terminal_id!r} does not compute the target "
            f"{target!r} (used equations: {sorted(used)})"
        )
    return None


def _gate_6(problem: Problem) -> str | None:
    for step in _equation_steps(problem):
        symbolic = step.content.get("symbolic")
        if not symbolic:
            continue
        try:
            parse_zero_form(symbolic, entry_id=step.id)
        except MalformedEquationError as exc:
            return f"gate 6: malformed equation in {step.id!r}: {exc}"
    return None


def _gate_7(problem: Problem) -> str | None:
    """Paper equation-system closure. A free symbol is CLOSED iff it is a given,
    the target, a COUPLING INTERMEDIATE (a free symbol of an equation used by a
    NON-terminal procedure step that ALSO appears in >=1 OTHER equation — i.e. a
    variable one step solves for and a later step consumes, like ``v2`` solved
    via continuity and consumed by bernoulli), or cancelled (named in a
    simplification's ``transformation`` / ``content.variables``). A symbol that
    lives in a single equation and is neither given/target/cancelled is UNCLOSED.
    NOT an end-to-end solve (honest v1 paper check, §8B.4:1347).

    DIRECTION (for 3B2d/3B2g, which author the symbols this gate consumes): the
    ``appears_in >= 2`` conjunct on the intermediate rule makes gate 7
    INTENTIONALLY CONSERVATIVE — it rejects-on-doubt. A legitimately-closed system
    whose intermediate is consumed by a SIMPLIFICATION (not a second equation)
    could be flagged unclosed; that errs toward rejection, the safe direction for a
    promotion gate (a false-RED quarantines a good problem; never a false-GREEN)."""
    eq_steps = _equation_steps(problem)
    free_by_eq: dict[str, set[str]] = {s.id: _equation_free_symbols(s) for s in eq_steps}
    all_symbols: set[str] = set()
    for syms in free_by_eq.values():
        all_symbols |= syms

    givens = set(problem.given_values.keys())
    target = {problem.target_unknown}

    procs = sorted(_proc_steps(problem), key=_proc_order)
    nonterminal_eq_ids: set[str] = set()
    for step in procs[:-1]:  # non-terminal procedure steps
        for u in step.content.get("uses_equations", []) or []:
            if u in free_by_eq:
                nonterminal_eq_ids.add(u)

    intermediates: set[str] = set()
    for sym in all_symbols:
        in_nonterminal = any(sym in free_by_eq[e] for e in nonterminal_eq_ids)
        appears_in = sum(1 for syms in free_by_eq.values() if sym in syms)
        if in_nonterminal and appears_in >= 2:
            intermediates.add(sym)

    cancelled = _cancelled_symbols(problem)

    closed = givens | target | intermediates | cancelled
    unclosed = sorted(all_symbols - closed)
    if unclosed:
        return (
            f"gate 7: equation system is not closed (paper check): unclosed free symbols {unclosed}"
        )
    return None


def _cancelled_symbols(problem: Problem) -> set[str]:
    """Whole-token symbols named in any simplification step's ``transformation``
    string OR its ``content.variables`` list (lenient v1 token match)."""
    cancelled: set[str] = set()
    for step in problem.reference_solution:
        if step.entry_type != "simplification":
            continue
        transformation = step.content.get("transformation", "") or ""
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9]*", transformation):
            cancelled.add(tok)
        for var in step.content.get("variables", []) or []:
            cancelled.add(var)
    return cancelled


def _gate_8(problem: Problem, existing_problem_hashes: Iterable[str]) -> str | None:
    h = problem_dup_hash(problem)
    if h in set(existing_problem_hashes):
        return f"gate 8: duplicate problem (dup hash {h} already exists)"
    return None


# --------------------------------------------------------------------------- #
# Orchestration — build/validate once at gate 1, thread (problem, kg) downstream
# --------------------------------------------------------------------------- #


def run_promotion_lint(
    graph: dict,
    *,
    canonical_symbols: set[str] | frozenset[str],
    normalization_map: Mapping[str, str],
    existing_problem_hashes: set[str] | frozenset[str],
    active_gates: set[int] | frozenset[int] = ALL_PROMOTION_GATES,
) -> PromotionResult:
    """Run the §8B.4 gates in order, short-circuiting on the first failure.

    ``graph`` is the ANNOTATED problem DICT (a ``Problem``-validatable dict that
    ALSO carries per-step ``entity_key`` + top-level ``declared_paths`` — the
    minted reference graph the 3B2g orchestrator holds at promotion time).

    ``active_gates`` is the SUBJECT-PROFILE'S gate set (subject-fluid Apollo). It
    defaults to all eight, so a pre-profile caller is unchanged. A profile passes a
    subset to skip the gates that are subject-specific: the
    ``qualitative_argumentative`` profile passes ``{1, 2, 3, 8}`` so the symbolic
    gates 4 (foreign-symbol) and 5 (terminal-computes-symbolic-target) — the only
    two that actively break on a prose argument graph — do not run, and the
    equation-only gates 6/7 are skipped rather than relied on to pass vacuously.
    Gate 1 ALWAYS runs regardless of ``active_gates``: it validates the schema and
    builds the ``Problem`` + ``KGGraph`` every later gate consumes. The active-gate
    SET is passed in by the caller (``promote``), so this unit stays PURE / DB-free
    / LLM-free — it never reads the subject profile from the DB itself.
    """
    # Gate 1 is special: it validates AND produces the Problem + KGGraph reused
    # by gates 3/5/6/7. Build once; a malformed problem (bad schema or forbidden
    # edge pair) fails AT gate 1 rather than surfacing as a wrong-gate error.
    try:
        problem = Problem.model_validate(graph)
    except (ValidationError, ValueError) as exc:
        return PromotionResult(ok=False, failed_gate=1, diagnostic=f"gate 1: schema: {exc}")

    mint_diag = _gate_1_mint_map(problem)
    if mint_diag is not None:
        return PromotionResult(ok=False, failed_gate=1, diagnostic=mint_diag)

    try:
        kg = problem.to_kg_graph(attempt_id=_LINT_ATTEMPT_ID)
    except (ValidationError, ValueError) as exc:  # pragma: no cover
        # DEFENSE-IN-DEPTH, currently unreachable: a Problem that already passed
        # ``model_validate`` above cannot produce a forbidden edge here. The only
        # edge types ``to_kg_graph`` emits are DEPENDS_ON (generic — every pair is
        # allowed), USES (hardcoded procedure_step->equation), and PRECEDES
        # (hardcoded procedure_step->procedure_step); a procedure_step pointing
        # ``uses_equations`` at a non-equation is rejected by ``_resolve_references``
        # at ``model_validate`` (the earlier try), never reaching this call. This
        # guard stays so a FUTURE ``to_kg_graph`` change that emits a typed edge for
        # a forbidden pair still fails CLOSED at gate 1 rather than crashing the lint.
        return PromotionResult(ok=False, failed_gate=1, diagnostic=f"gate 1: forbidden edge: {exc}")

    # Gates 2-8, ordered. The loop returns on the first non-None diagnostic, so
    # the short-circuit ORDER guarantee is explicit and testable.
    gates: list[tuple[int, object]] = [
        (2, lambda: _gate_2(problem, graph)),
        (3, lambda: _gate_3(problem, kg)),
        (4, lambda: _gate_4(problem, canonical_symbols, normalization_map)),
        (5, lambda: _gate_5(problem, kg)),
        (6, lambda: _gate_6(problem)),
        (7, lambda: _gate_7(problem)),
        (8, lambda: _gate_8(problem, existing_problem_hashes)),
    ]
    for number, gate in gates:
        if number not in active_gates:
            continue  # subject profile turned this gate OFF (e.g. 4/5 for a prose argument graph)
        diagnostic = gate()  # type: ignore[operator]
        if diagnostic is not None:
            return PromotionResult(ok=False, failed_gate=number, diagnostic=diagnostic)

    return PromotionResult(ok=True, failed_gate=None, diagnostic="")
