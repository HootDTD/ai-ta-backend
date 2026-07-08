"""Solution-grounded reference-graph derivation (reversed provisioning).

Replaces the ungrounded tag_and_mint graph generation (48% on the S1 judge)
with the dress-rehearsal procedure that produced apollo/subjects/calculus_2
(60 graphs, zero structural defects): derive the graph FROM the paired worked
solution, anchored to the MATCHED concept's vocabulary, gold subject format
enforced by a pure defect validator.

LEAK GUARD: the derivation prompt sees ONLY (a) the problem text, (b) the
paired SOLUTION document's grounding spans, (c) the matched concept's
vocabulary. No other course material, no learner state. Every node's content
must trace to the solution text; the stage-3 pairing gate (validate_pair)
independently judges faithfulness against the same spans.

Validation (``find_derivation_defects``, pure):
  * Problem-schema validity (``Problem.model_validate``)
  * 5-9 typed nodes; unique, MEANINGFUL snake_case ids (entity keys derive
    from entry_type + id, so "meaningful keys like eq.ibp_formula" == the id)
  * concrete equations parse under BOTH ``sympy.sympify`` and
    ``parse_zero_form``, with a local_dict built from the concept's canonical
    symbols (reserved names N, S, E, I collide otherwise; explicit ``*``
    everywhere — ``x(x+1)`` silently misparses as a function call)
  * operator-identity/pedagogical formulas carry ``content.display=true`` and
    are exempt from the parse rule (never forced into fake zero-forms; lint
    gate 6 skips them)
  * no variable fragmentation (one physical quantity -> one node; no two
    equations stating the same relation)
  * ``depends_on`` is a DAG (Kahn's algorithm, mirroring
    ``campaign.judges.s1_reference_graph.find_structural_defects``) — the
    mint-time acyclicity guard for reference-graph edges
  * ``bound_variables`` declared for function-valued answers (lint gate 7
    subtracts them from the closure check)

One retry with the defect list fed back at reasoning_effort='high'; still
defective -> ``DerivationError`` (fail-closed; the orchestrator rejects the
candidate). ``chat_fn`` is injected (Tier-1 tests: NO network).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

import sympy
from pydantic import BaseModel, Field

from apollo.provisioning.solution import GroundingSpan
from apollo.schemas.problem import Problem
from apollo.solver.sympy_exec import MalformedEquationError, parse_zero_form

__all__ = [
    "DerivationError",
    "DerivedGraph",
    "derive_reference_graph",
    "find_derivation_defects",
]

_LOG = logging.getLogger(__name__)

_MIN_NODES = 5
_MAX_NODES = 9

# Tokens whose presence in a symbolic string marks a pedagogical operator
# identity (display content) rather than a concrete algebraic equation. The
# committed calc-2 gold graphs carry such identities WITHOUT an explicit
# display flag, so this heuristic keeps the validator faithful to the corpus.
_OPERATOR_TOKENS = ("integral", "Integral", "sum ", "Sum(", "lim ", "lim_", "d/dx")

# Textbook trig notation (sin^2 x, cos(2x) applied by juxtaposition) — the gold
# corpus states power-reduction/Pythagorean identities this way. They are
# pedagogical display content, not machine-checked zero-forms (the runtime
# symbolic tier already treats a non-parse as a non-match and falls back to
# the lexical tiers).
_TRIG_TEXTBOOK_RE = re.compile(r"\b(sin|cos|tan|sec|csc|cot)\s*(\^|\s+[A-Za-z0-9(])")

# Meaningful snake_case, allowing an interior UPPERCASE symbol reference the
# way the gold corpus writes them (e.g. "solve_for_I").
_ID_RE = re.compile(r"^[a-z][A-Za-z0-9_]*$")


class DerivationError(RuntimeError):
    """Fail-closed: the derivation could not produce a defect-free gold-format
    graph (no solution spans, unparseable LLM output, or defects survived the
    feedback retry)."""


class DerivedGraph(BaseModel):
    reference_solution: list[dict]
    target_unknown: str = ""
    symbolic_mappings: dict[str, str] = Field(default_factory=dict)
    bound_variables: list[str] = Field(default_factory=list)
    retried: bool = False


# --------------------------------------------------------------------------- #
# The derivation prompt — the productized dress-rehearsal procedure.
# --------------------------------------------------------------------------- #

_DERIVATION_SYSTEM_PROMPT = (
    "You convert ONE problem and its TEACHER-PROVIDED worked solution into a "
    "teachable reference graph a student will later reconstruct by teaching it "
    "back. Derive the graph FROM THE WORKED SOLUTION ONLY — every node's "
    "content must trace to something the solution actually states or does. Do "
    "NOT invent alternative methods, extra steps, or facts absent from the "
    "solution.\n\n"
    "OUTPUT: a single JSON object with EXACTLY these keys:\n"
    '  "reference_solution": array of 5 to 9 step objects (see below)\n'
    '  "target_unknown": the symbol or short phrase the problem solves for\n'
    '  "symbolic_mappings": object mapping declared substitutions the solution '
    'uses (e.g. {"u": "sin(x)"}); {} if none\n'
    '  "bound_variables": array of symbols that remain free in the final '
    "answer because they are the answer's argument or an index — e.g. the "
    "integration variable x of an antiderivative, a series index n, sample "
    "points x0..xn and an opaque integrand symbol f in numerical rules; [] "
    "for a fully numeric answer\n\n"
    'Each step object has EXACTLY: "step" (int >= 1, file order), '
    '"entry_type", "id", "content" (object), "depends_on" (array of step ids, '
    "[] if none).\n\n"
    "entry_type and its content fields:\n"
    '- "equation": {"symbolic", "label", "variables"} — a governing equation '
    "or a concrete algebraic relation the solution uses.\n"
    '- "condition": {"applies_when", "label"} — an assumption/applicability '
    "test the solution invokes (e.g. a convergence criterion, factorability).\n"
    '- "simplification": {"applies_when", "transformation"} — an algebraic '
    "reduction the solution performs.\n"
    '- "definition": {"concept", "meaning"} — a choice/assignment the solution '
    "makes (e.g. the u/dv split).\n"
    '- "variable_mapping": {"term", "symbol"} — a prose quantity bound to a '
    "symbol.\n"
    '- "procedure_step": {"order", "action", "purpose", "uses_equations", '
    '"label"} — what the student DOES at this stage, in solution order. '
    '"order" is 1..N contiguous across procedure_steps; "uses_equations" lists '
    "equation step ids this step applies ([] if it grounds in a "
    "condition/simplification instead — put that id in depends_on).\n\n"
    "RULES — a mechanical validator rejects violations:\n"
    '1. IDs are meaningful snake_case English (e.g. "ibp_formula", '
    '"parts_assignment", "solve_for_recurring_integral"). NEVER opaque or '
    'mechanical ids like "vm_a", "eq1", "ps_select_eq", "step_2".\n'
    '2. Concrete algebraic equations: "symbolic" must be machine-parseable — '
    '"LHS = RHS" with EXPLICIT multiplication everywhere (write 2*x and '
    "A*(x+1); x(x+1) would parse as a function call), ** for powers, no "
    "unicode, no absolute-value bars (use Abs(...)), only symbols from the "
    "concept vocabulary or the problem itself.\n"
    "3. Operator-identity/pedagogical formulas (integral u dv = u*v - "
    "integral v du, a summation template, a limit definition) are DISPLAY "
    'content: keep the readable form and add "display": true inside content. '
    "Do NOT rewrite them into fake zero-forms. Use display sparingly — a "
    "relation between plain symbols is a concrete equation, not display.\n"
    "4. One physical/mathematical quantity = ONE node. Never mint two nodes "
    "for the same quantity under different names, and never restate the same "
    "equation twice.\n"
    '5. "depends_on" points at the steps whose result this step consumes. The '
    "graph must be acyclic. procedure_steps chain the solution's actual "
    "order.\n"
    "6. Include the governing equation/criterion the solution relies on, the "
    "choices it makes, the reductions it performs, and 2-4 procedure_steps "
    "that walk its execution. 5-9 nodes total.\n"
    "Return the JSON object ONLY — no prose, no markdown fences."
)


def _spans_text(spans: Sequence[GroundingSpan]) -> str:
    return "\n\n".join(s.text for s in spans if (s.text or "").strip())


def _vocab_block(canonical_symbols: dict, normalization_map: dict) -> dict:
    return {
        "symbols": list(canonical_symbols.get("symbols") or []),
        "symbol_meanings": dict(canonical_symbols.get("description") or {}),
        "subscript_convention": canonical_symbols.get("subscript_convention") or "",
        "normalization_map": dict(normalization_map or {}),
    }


# --------------------------------------------------------------------------- #
# Pure defect validator
# --------------------------------------------------------------------------- #


def _is_display(step: dict) -> bool:
    content = step.get("content") or {}
    if content.get("display") is True:
        return True
    symbolic = str(content.get("symbolic") or "")
    if any(tok in symbolic for tok in _OPERATOR_TOKENS):
        return True
    return bool(_TRIG_TEXTBOOK_RE.search(symbolic))


def _local_dict(canonical_symbols: dict, graph: dict) -> dict[str, Any]:
    """Symbols for parsing: concept vocabulary + given/target/bound names.

    Overrides SymPy singletons (N, S, E, I, O, Q) that collide with domain
    symbols; Rational stays callable (the gold graphs use it)."""
    names: set[str] = set(canonical_symbols.get("symbols") or [])
    names |= set((graph.get("given_values") or {}).keys())
    names |= {str(b) for b in (graph.get("bound_variables") or [])}
    target = str(graph.get("target_unknown") or "")
    if target.isidentifier():
        names.add(target)
    local: dict[str, Any] = {n: sympy.Symbol(n) for n in names if n.isidentifier()}
    local["Rational"] = sympy.Rational
    return local


def _equation_parse_defect(step: dict, local: dict[str, Any]) -> str | None:
    symbolic = str((step.get("content") or {}).get("symbolic") or "")
    step_id = str(step.get("id"))
    if not symbolic:
        return f"equation_parse: {step_id}: empty symbolic"
    try:
        parse_zero_form(symbolic, entry_id=step_id, local_dict=local)
    except MalformedEquationError as exc:
        return f"equation_parse: {step_id}: parse_zero_form failed: {exc}"
    # Double-parse under sympify (the second parser the runtime uses); each
    # '='-side must sympify with the same locals. x(x+1)-style function-call
    # misparses surface here even when parse_expr tolerated them.
    for side in symbolic.split("=")[:2]:
        try:
            sympy.sympify(side, locals=local)
        except Exception as exc:  # noqa: BLE001 — any parse blowup is the defect signal
            return f"equation_parse: {step_id}: sympify failed on {side.strip()!r}: {exc}"
    return None


def _kahn_cycle(steps: list[dict]) -> str | None:
    """Kahn's algorithm over depends_on (mirrors the campaign judge's
    find_structural_defects cycle check) — the mint-time acyclicity guard."""
    ids = [str(s.get("id")) for s in steps]
    id_set = set(ids)
    indeg = dict.fromkeys(ids, 0)
    out: dict[str, list[str]] = {i: [] for i in ids}
    for s in steps:
        sid = str(s.get("id"))
        for raw_dep in s.get("depends_on") or []:
            dep = str(raw_dep)
            if dep in id_set and dep != sid:
                out[dep].append(sid)
                indeg[sid] += 1
    queue = [i for i in ids if indeg[i] == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for nxt in out[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if seen != len(ids):
        cyclic = sorted(i for i in ids if indeg[i] > 0)
        return f"cycle: depends_on cycle among {cyclic}"
    return None


def _opaque_id_defect(step_id: str) -> str | None:
    if not _ID_RE.match(step_id):
        return f"opaque_id: {step_id!r} is not snake_case"
    tokens = [t for t in step_id.split("_") if t]
    if not any(len(t) >= 3 and t.isalpha() for t in tokens):
        return f"opaque_id: {step_id!r} carries no meaningful word"
    return None


def find_derivation_defects(
    graph: dict, *, canonical_symbols: dict, normalization_map: dict
) -> list[str]:
    """Pure gold-format defect check. ``[]`` == clean; each entry is
    "category: detail" (categories: schema, node_count, duplicate_id,
    opaque_id, equation_parse, fragmentation, cycle)."""
    defects: list[str] = []
    steps = list(graph.get("reference_solution") or [])

    try:
        Problem.model_validate(graph)
    except Exception as exc:  # noqa: BLE001 — any schema violation is one defect line
        defects.append(f"schema: {exc}")
        return defects  # structure unusable; deeper checks would throw

    if not (_MIN_NODES <= len(steps) <= _MAX_NODES):
        defects.append(
            f"node_count: {len(steps)} nodes (gold standard is {_MIN_NODES}-{_MAX_NODES})"
        )

    seen_ids: set[str] = set()
    for s in steps:
        sid = str(s.get("id"))
        if sid in seen_ids:
            defects.append(f"duplicate_id: {sid}")
        seen_ids.add(sid)
        opaque = _opaque_id_defect(sid)
        if opaque:
            defects.append(opaque)

    local = _local_dict(canonical_symbols, graph)
    zero_forms: dict[str, Any] = {}
    for s in steps:
        if s.get("entry_type") != "equation" or _is_display(s):
            continue
        parse_defect = _equation_parse_defect(s, local)
        if parse_defect:
            defects.append(parse_defect)
            continue
        expr = parse_zero_form(
            str(s["content"]["symbolic"]), entry_id=str(s.get("id")), local_dict=local
        )
        for other_id, other_expr in zero_forms.items():
            try:
                if sympy.simplify(expr - other_expr) == 0 or sympy.simplify(expr + other_expr) == 0:
                    defects.append(
                        "fragmentation: equations "
                        f"{other_id!r} and {s.get('id')!r} state the same relation"
                    )
            except Exception:  # noqa: BLE001 — a simplify blowup is a non-defect
                pass
        zero_forms[str(s.get("id"))] = expr

    symbols_seen: dict[str, str] = {}
    for s in steps:
        if s.get("entry_type") != "variable_mapping":
            continue
        symbol = str((s.get("content") or {}).get("symbol") or "")
        if symbol and symbol in symbols_seen:
            defects.append(
                f"fragmentation: quantity {symbol!r} minted twice "
                f"({symbols_seen[symbol]!r} and {s.get('id')!r})"
            )
        if symbol:
            symbols_seen[symbol] = str(s.get("id"))

    cycle = _kahn_cycle(steps)
    if cycle:
        defects.append(cycle)
    return defects


# --------------------------------------------------------------------------- #
# Derivation call
# --------------------------------------------------------------------------- #


def _parse_derivation(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("reference_solution"), list):
        return None
    return parsed


def _problem_shaped(candidate: Any, parsed: dict, concept_slug: str) -> dict:
    """Assemble the Problem-shaped dict the validator (and later the promote
    payload) consumes — the derived top-level extras ride along."""
    return {
        "id": f"derived.{getattr(candidate, 'chunk_content_hash', '') or ''}",
        "concept_id": concept_slug,
        "difficulty": getattr(candidate, "difficulty", "standard") or "standard",
        "problem_text": getattr(candidate, "problem_text", "") or "",
        "given_values": dict(getattr(candidate, "given_values", {}) or {}),
        "target_unknown": str(parsed.get("target_unknown") or ""),
        "reference_solution": list(parsed.get("reference_solution") or []),
        "symbolic_mappings": dict(parsed.get("symbolic_mappings") or {}),
        "bound_variables": [str(b) for b in (parsed.get("bound_variables") or [])],
    }


async def derive_reference_graph(
    candidate: Any,
    spans: Sequence[GroundingSpan],
    *,
    concept_slug: str,
    concept_display_name: str,
    canonical_symbols: dict,
    normalization_map: dict,
    chat_fn: Callable[..., str],
) -> DerivedGraph:
    """One derivation call (+ one defect-feedback retry at higher effort).

    ``chat_fn`` is main-tier-shaped (inject ``metered_chat.main``). Raises
    ``DerivationError`` fail-closed; never returns a defective graph."""
    solution_text = _spans_text(spans)
    if not solution_text:
        raise DerivationError(
            "no paired-solution grounding spans — derivation is solution-grounded only"
        )

    base_user: dict[str, Any] = {
        "problem_text": getattr(candidate, "problem_text", "") or "",
        "given_values": dict(getattr(candidate, "given_values", {}) or {}),
        "concept": {"slug": concept_slug, "display_name": concept_display_name},
        "vocabulary": _vocab_block(canonical_symbols, normalization_map),
        "worked_solution": solution_text,
    }

    def _ask(effort: str, feedback: list[str] | None) -> dict | None:
        user: dict[str, Any] = dict(base_user)
        if feedback:
            user["validator_defects_from_previous_attempt"] = feedback
        raw = chat_fn(
            purpose="graph_derivation",
            messages=[
                {"role": "system", "content": _DERIVATION_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user, sort_keys=True)},
            ],
            response_format={"type": "json_object"},
            reasoning_effort=effort,
        )
        return _parse_derivation(raw)

    def _defects_of(parsed: dict | None) -> list[str]:
        if parsed is None:
            return ["unparseable derivation response"]
        return find_derivation_defects(
            _problem_shaped(candidate, parsed, concept_slug),
            canonical_symbols=canonical_symbols,
            normalization_map=normalization_map,
        )

    parsed = _ask("medium", None)
    defects = _defects_of(parsed)
    retried = False
    if defects:
        retried = True
        _LOG.info(
            "graph_derivation_retry",
            extra={"event": "graph_derivation_retry", "defects": defects[:8]},
        )
        parsed = _ask("high", defects)
        defects = _defects_of(parsed)
    if parsed is None or defects:
        raise DerivationError(f"derivation defective after retry: {defects[:8]}")

    return DerivedGraph(
        reference_solution=list(parsed["reference_solution"]),
        target_unknown=str(parsed.get("target_unknown") or ""),
        symbolic_mappings=dict(parsed.get("symbolic_mappings") or {}),
        bound_variables=[str(b) for b in (parsed.get("bound_variables") or [])],
        retried=retried,
    )
