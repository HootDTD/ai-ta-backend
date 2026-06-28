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
# Content-derived gate applicability (spec §4 — replaces the stored profile)
# --------------------------------------------------------------------------- #


# The rigor gates whose validity theory is "a closed system of symbolic
# equations": they SELF-ACTIVATE only when the graph carries >=1 parseable
# equation step. Gate 5 is SPLIT (structural half always-on, symbolic half
# self-activates INSIDE the gate), so it is NOT in this set — it lives in the
# always-on structural core.
_SYMBOLIC_GATES: frozenset[int] = frozenset({4, 6, 7})


def _has_parseable_equation(graph: dict) -> bool:
    """True iff the problem carries at least one ``equation`` step with a
    non-empty, parseable ``symbolic`` — the content precondition for the symbolic
    rigor gates (spec §4 tier 2). A malformed ``symbolic`` does NOT count as
    parseable here, but gate 6 STILL runs on any equation present (it is in the
    self-activated set whenever ANY parseable equation exists), so a graph mixing a
    good and a malformed equation is still caught. A schema-broken graph that can't
    even validate returns True (keep ALL gates active so gate 1 fires) — fail-closed."""
    try:
        problem = Problem.model_validate(graph)
    except (ValidationError, ValueError):
        return True
    return any(_equation_free_symbols(s) for s in _equation_steps(problem))


def content_active_gates(graph: dict) -> frozenset[int]:
    """The CONTENT-DERIVED active-gate set the caller (``promote``) passes to
    ``run_promotion_lint``. The structural core {1,2,3,5,8} ALWAYS applies; the
    symbolic rigor gates {4,6,7} apply ONLY when the graph carries a parseable
    equation (spec §3/§4). This replaces the stored subject profile: a rigor gate
    can only ever REJECT content it applies to — it physically cannot block a
    subject it does not apply to."""
    always = {1, 2, 3, 5, 8}
    if _has_parseable_equation(graph):
        return frozenset(always | set(_SYMBOLIC_GATES))
    return frozenset(always)


# --------------------------------------------------------------------------- #
# Graph-derived symbolic answer (spec §4.1, Option 2) — shared by gate 5 + gate 7
# --------------------------------------------------------------------------- #


def _free_symbols_by_equation(problem: Problem) -> dict[str, set[str]]:
    """``{equation_id: free_symbol_names}`` for every equation step."""
    return {s.id: _equation_free_symbols(s) for s in _equation_steps(problem)}


def _all_equation_symbols(problem: Problem) -> set[str]:
    """Union of every equation step's free symbols."""
    out: set[str] = set()
    for syms in _free_symbols_by_equation(problem).values():
        out |= syms
    return out


def _intermediate_symbols(problem: Problem) -> set[str]:
    """COUPLING INTERMEDIATES: a free symbol of an equation used by a NON-terminal
    procedure step that ALSO appears in >=1 OTHER equation — a variable one step
    solves for and a later step consumes (e.g. ``v2`` solved via continuity and
    consumed by bernoulli). The ``appears_in >= 2`` conjunct keeps this
    INTENTIONALLY CONSERVATIVE (rejects-on-doubt — the safe direction). Shared by
    gate 7's closure check and ``_derive_symbolic_answer`` so both read ONE
    definition of "intermediate"."""
    free_by_eq = _free_symbols_by_equation(problem)
    procs = sorted(_proc_steps(problem), key=_proc_order)
    nonterminal_eq_ids: set[str] = set()
    for step in procs[:-1]:  # non-terminal procedure steps
        for u in step.content.get("uses_equations", []) or []:
            if u in free_by_eq:
                nonterminal_eq_ids.add(u)
    intermediates: set[str] = set()
    for sym in _all_equation_symbols(problem):
        in_nonterminal = any(sym in free_by_eq[e] for e in nonterminal_eq_ids)
        appears_in = sum(1 for syms in free_by_eq.values() if sym in syms)
        if in_nonterminal and appears_in >= 2:
            intermediates.add(sym)
    return intermediates


def _derive_symbolic_answer(problem: Problem) -> set[str]:
    """The GRAPH-DERIVED answer (spec §4.1, Option 2): every free symbol of the
    equation system that the problem does NOT give, compute as a coupling
    intermediate, or cancel. For a closed system this is size 0 or 1; the single
    element is the answer the chain terminates in.

    DELIBERATELY independent of the prose ``target_unknown`` — a symbolic system
    with a PROSE target (the live AAE 333 shape: "boundary layer thickness") still
    resolves to its lone unknown symbol, so the gate-5 symbolic half and gate 7 key
    off the GRAPH, not a label. Byte-identical to the old behavior on the anchor:
    there the lone remaining symbol IS the target, so the verdicts do not move (the
    differential test is the mechanical proof)."""
    return (
        _all_equation_symbols(problem)
        - set(problem.given_values.keys())
        - _intermediate_symbols(problem)
        - _cancelled_symbols(problem)
    )


def _defined_symbols(problem: Problem) -> set[str]:
    """Symbols a ``definition`` / ``variable_mapping`` step INTRODUCES (table-less
    grounding). Reads ``content['symbol']`` / ``content['term']`` and tokenizes
    ``content['meaning']`` / ``content['definition']``. Lenient token match mirrors
    ``_cancelled_symbols`` — superset-accept is the safe direction for a promotion
    gate (a false-RED only quarantines; a false-GREEN never ships)."""
    out: set[str] = set()
    for step in problem.reference_solution:
        if step.entry_type not in ("definition", "variable_mapping"):
            continue
        for key in ("symbol", "term"):
            val = step.content.get(key)
            if isinstance(val, str) and val:
                out.add(val)
        for tok_field in ("meaning", "definition"):
            text = step.content.get(tok_field) or ""
            for tok in re.findall(r"[A-Za-z][A-Za-z0-9]*", str(text)):
                out.add(tok)
    return out


def _internal_grounded_symbols(problem: Problem) -> set[str]:
    """The problem's OWN symbol closure (spec §4.2), used ONLY when no seeded
    ``canonical_symbols`` table exists. A symbol is non-foreign iff the problem
    GIVES it, DEFINES it (definition / variable_mapping), COMPUTES it (a coupling
    intermediate), CANCELS it (a simplification), or it is the lone graph-derived
    ANSWER. SUPERSET-accept: anything a real seeded table would accept the problem
    itself also introduces, so a seeded concept's verdicts never move."""
    return (
        set(problem.given_values.keys())
        | _defined_symbols(problem)
        | _intermediate_symbols(problem)
        | _cancelled_symbols(problem)
        | _derive_symbolic_answer(problem)
    )


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
    # Option 2: the prose ``target_unknown`` is NO LONGER added as a symbol — a
    # prose target ("boundary layer thickness") is not a symbol to ground. For the
    # back-compat anchor the target IS already a free symbol of the terminal
    # equation, so dropping the add is byte-identical (the differential proves it).
    canon = set(canonical_symbols)
    if not canon and not normalization_map:
        # TABLE-LESS internal grounding (a fresh auto-minted concept). A symbol is
        # non-foreign iff the problem itself gives / defines / computes / cancels it
        # or it is the lone graph-derived answer (spec §4.2). An unexplained extra
        # symbol survives gate 4 as a candidate answer but is then caught by gate 7
        # as under-determination — so a foreign symbol is never silently promoted.
        grounded = _internal_grounded_symbols(problem)
        for name in sorted(symbols):
            if name not in grounded:  # pragma: no cover - currently unreachable: the
                # graph-derived answer term of ``_internal_grounded_symbols`` absorbs
                # every otherwise-ungrounded symbol, so a foreign symbol is never
                # rejected HERE — it inflates the free-unknown count and gate 7
                # (under-determination) rejects it. This defensive arm stays so a
                # future grounded-set change (e.g. dropping the answer term) fails
                # CLOSED at gate 4 rather than silently promoting a foreign symbol.
                return (
                    f"gate 4: foreign symbol {name!r} is not given, defined, "
                    f"computed, or cancelled by the problem (internal grounding)"
                )
        return None
    # SEEDED-TABLE path — UNCHANGED (byte-identical to today; the 41 ride this).
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

    # --- Universal terminal-sink property (kind-AGNOSTIC graph property, §4.1):
    # the answer-carrying node is what the chain terminates in. The chain-coverage
    # check above already proves a unique terminal sink (chain[-1]) — that IS the
    # structural target-reachability property, enough for an equation-less graph
    # (faithfulness confirms the terminal step PRODUCES the asked-for answer). The
    # symbolic half below ADDS "and the terminal equation computes the single
    # GRAPH-DERIVED answer symbolically" — and ONLY when the terminal step
    # references a parseable equation AND the system closes to exactly one unknown
    # (a prose ``target_unknown`` no longer blocks it; the answer comes from the
    # graph, not the label — Option 2).
    terminal_id = chain[-1].node_id
    terminal = next((s for s in _proc_steps(problem) if s.id == terminal_id), None)
    if terminal is None:  # pragma: no cover - defense in depth: gate 1 builds the
        # KG from the validated problem, so chain[-1] is always a real proc step.
        return f"gate 5: terminal step {terminal_id!r} not found among procedure steps"
    used = terminal.content.get("uses_equations", []) or []
    eq_by_id = {s.id: s for s in _equation_steps(problem)}
    # SYMBOLIC HALF self-activates: only when the terminal step references >=1
    # equation that PARSES (a real symbolic system). No parseable terminal equation
    # -> the structural sink property already passed; ride faithfulness (§4.1).
    parseable_used = [u for u in used if u in eq_by_id and _equation_free_symbols(eq_by_id[u])]
    if not parseable_used:
        return None
    answer = _derive_symbolic_answer(problem)
    if len(answer) != 1:
        # 0 or >1 graph-derived unknowns: gate 7 owns under-determination; gate 5
        # cannot point at a single answer symbol, so it defers (no symbolic reject).
        return None
    (answer_symbol,) = tuple(answer)
    reaches_answer = any(
        answer_symbol in _equation_free_symbols(eq_by_id[u]) for u in parseable_used
    )
    if not reaches_answer:
        return (
            f"gate 5: terminal step {terminal_id!r} does not compute the answer "
            f"{answer_symbol!r} (used equations: {sorted(used)})"
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
    """Equation-system closure (Option 2, spec §4.1). The system is CLOSED iff the
    GRAPH-DERIVED answer — every free symbol that is not a given, a coupling
    intermediate, or cancelled (see ``_derive_symbolic_answer``) — has AT MOST ONE
    element: the single unknown the chain solves for. MORE than one free unknown
    means the system is under-determined (a paper check, NOT an end-to-end solve;
    honest v1 limit §8B.4:1347).

    Keys off the GRAPH, not the prose ``target_unknown`` — a closed symbolic system
    with a prose target (the live AAE 333 shape) is no longer falsely rejected.
    Byte-identical to the old paper-closure check on the back-compat anchor: there
    the lone remaining symbol IS the target, so |answer| == 1 (the differential
    test is the mechanical proof).

    The ``appears_in >= 2`` conjunct on the intermediate rule (in
    ``_intermediate_symbols``) keeps this INTENTIONALLY CONSERVATIVE — it
    rejects-on-doubt, the safe direction for a promotion gate (a false-RED
    quarantines a good problem; never a false-GREEN)."""
    answer = _derive_symbolic_answer(problem)
    if len(answer) > 1:
        return (
            f"gate 7: equation system is under-determined (paper check): "
            f"{len(answer)} free unknowns remain {sorted(answer)}"
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
