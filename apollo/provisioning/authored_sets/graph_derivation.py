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
  * 5-9 typed nodes in legacy mode, or 3-15 knowledge-component-grained nodes
    when ``APOLLO_KC_GRANULARITY`` is enabled; unique, MEANINGFUL snake_case ids
    (entity keys derive from entry_type + id, so "meaningful keys like
    eq.ibp_formula" == the id)
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
import os
import re
from collections.abc import Callable, Sequence
from typing import Any

import sympy
from pydantic import BaseModel, Field

from apollo.ontology import NODE_CONTENT_TYPES, EdgeType, build_node
from apollo.persistence.learner_model_seed import _ENTRY_TYPE_TO_KIND_PREFIX
from apollo.provisioning.generation_contract import (
    GENERIC_ID_TOKENS,
    SHARED_CONSTANT_SYMBOLS,
    ontology_block,
)
from apollo.provisioning.solution import GroundingSpan
from apollo.schemas.problem import Problem
from apollo.solver.sympy_exec import MalformedEquationError, parse_zero_form

__all__ = [
    "ALL_DEFECT_CLASSES",
    "GENERATION_DEFECT_CLASSES",
    "DerivationError",
    "DerivedGraph",
    "TypedConstructionDefect",
    "build_ordered_problem",
    "derive_reference_graph",
    "find_derivation_defects",
    "kc_granularity_enabled",
]

_LOG = logging.getLogger(__name__)

_KC_GRANULARITY_FLAG = "APOLLO_KC_GRANULARITY"
_LEGACY_MIN_NODES = 5
_LEGACY_MAX_NODES = 9
_KC_MIN_NODES = 3
_KC_MAX_NODES = 15

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


class TypedConstructionDefect(ValueError):
    """Mechanical defect in a manual typed ordered-step response.

    The exception carries every concrete validator diagnostic from one attempt so
    the construction caller can feed it back verbatim without inventing a second
    validation vocabulary.
    """

    def __init__(self, diagnostics: Sequence[str]):
        self.diagnostics = tuple(str(item) for item in diagnostics)
        super().__init__("; ".join(self.diagnostics))


class DerivedGraph(BaseModel):
    reference_solution: list[dict]
    target_unknown: str = ""
    symbolic_mappings: dict[str, str] = Field(default_factory=dict)
    bound_variables: list[str] = Field(default_factory=list)
    retried: bool = False


def _declared_references(step: dict) -> list[str]:
    raw = step.get("references")
    if raw is None and isinstance(step.get("content"), dict):
        raw = step["content"].get("references")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TypedConstructionDefect(
            [f"step_schema: {step.get('id')!r} references must be an array of prior ids"]
        )
    references: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value:
            raise TypedConstructionDefect(
                [f"step_schema: {step.get('id')!r} references must contain non-empty ids"]
            )
        references.append(value)
    return references


def _definition_symbol(step: dict) -> str | None:
    """Return the case-sensitive symbol introduced by an ordered step, if any."""
    content = step.get("content") or {}
    entry_type = step.get("entry_type")
    if entry_type == "variable_mapping":
        value = str(content.get("symbol") or "")
        return value if value.isidentifier() else None
    if entry_type == "definition":
        for field in ("symbol", "term", "concept"):
            value = str(content.get(field) or "")
            if value.isidentifier():
                return value
    if entry_type == "equation":
        symbolic = str(content.get("symbolic") or "")
        if symbolic.count("=") == 1:
            lhs = symbolic.split("=", 1)[0].strip()
            if lhs.isidentifier():
                return lhs
    return None


def _ordered_step_symbols(step: dict, local: dict[str, Any]) -> tuple[set[str], str | None]:
    """Return symbols consumed by a concrete equation and its optional LHS definition."""
    if step.get("entry_type") != "equation" or _is_display(step):
        return set(), _definition_symbol(step)
    symbolic = str((step.get("content") or {}).get("symbolic") or "")
    step_id = str(step.get("id") or "")
    defect = _equation_parse_defect(step, local)
    if defect:
        raise TypedConstructionDefect([defect])
    expr = parse_zero_form(symbolic, entry_id=step_id, local_dict=local)
    used = {symbol.name for symbol in expr.free_symbols}
    defined = _definition_symbol(step)
    if defined and symbolic.count("=") == 1:
        rhs = symbolic.split("=", 1)[1]
        try:
            rhs_expr = sympy.sympify(rhs, locals=local)
            used = {symbol.name for symbol in rhs_expr.free_symbols}
        except Exception as exc:  # noqa: BLE001 - reported as the construction defect
            raise TypedConstructionDefect(
                [f"equation_parse: {step_id}: sympify failed on {rhs.strip()!r}: {exc}"]
            ) from exc
    return used, defined


def build_ordered_problem(
    authored: Any,
    ordered_steps: Sequence[dict],
    *,
    symbol_table: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a schema-valid ``Problem`` from dependency-free ordered typed steps.

    Symbolic dependencies are definition/use edges to prior definers. Equationless
    nodes use declared ``references`` when supplied, otherwise adjacent precedence.
    Procedure order is reassigned contiguously, which makes ``PRECEDES`` an
    unbroken list-order chain in ``Problem.to_kg_graph``. The function either
    returns a fully validated problem dict or raises ``TypedConstructionDefect``;
    callers never receive a partial graph.
    """
    if not isinstance(ordered_steps, Sequence) or isinstance(ordered_steps, (str, bytes)):
        raise TypedConstructionDefect(["step_schema: steps must be a non-empty array"])
    raw_steps = list(ordered_steps)
    if not raw_steps:
        raise TypedConstructionDefect(["step_schema: steps must be a non-empty array"])
    if symbol_table is not None and (
        not isinstance(symbol_table, dict)
        or not all(
            isinstance(key, str) and key and isinstance(value, dict)
            for key, value in symbol_table.items()
        )
    ):
        raise TypedConstructionDefect(
            ["symbol_table: must map each case-sensitive symbol to an entry object"]
        )

    diagnostics: list[str] = []
    seen: set[str] = set()
    allowed_types = set(_ENTRY_TYPE_TO_KIND_PREFIX)
    for index, step in enumerate(raw_steps, start=1):
        if not isinstance(step, dict):
            diagnostics.append(f"step_schema: step {index} must be an object")
            continue
        if "depends_on" in step:
            diagnostics.append(
                f"step_schema: {step.get('id')!r} must not declare depends_on; dependencies are derived"
            )
        extra_keys = set(step) - {"entry_type", "id", "content", "references"}
        if extra_keys:
            diagnostics.append(
                f"step_schema: {step.get('id')!r} has unsupported keys {sorted(extra_keys)!r}"
            )
        step_id = str(step.get("id") or "")
        if not step_id:
            diagnostics.append(f"step_schema: step {index} has no id")
        elif step_id in seen:
            diagnostics.append(f"duplicate_id: {step_id}")
        else:
            seen.add(step_id)
            opaque = _opaque_id_defect(step_id)
            if opaque:
                diagnostics.append(opaque)
            semantic = _semantic_key_defect(step)
            if semantic:
                diagnostics.append(semantic)
        if step.get("entry_type") not in allowed_types:
            diagnostics.append(
                f"step_schema: {step_id or index!r} entry_type must be one of {sorted(allowed_types)}"
            )
        if not isinstance(step.get("content"), dict):
            diagnostics.append(f"step_schema: {step_id or index!r} content must be an object")
        elif step.get("entry_type") in NODE_CONTENT_TYPES:
            try:
                build_node(
                    node_type=step["entry_type"],
                    node_id=step_id or str(index),
                    attempt_id=0,
                    source="reference",
                    content=step["content"],
                )
            except Exception as exc:  # noqa: BLE001 - normalized construction surface
                diagnostics.append(f"step_schema: {step_id or index!r} content invalid: {exc}")
    if diagnostics:
        raise TypedConstructionDefect(diagnostics)

    problem_seed = authored.to_problem_dict([])
    table_names = set(symbol_table or {})
    local_names = (
        table_names
        | set(problem_seed.get("given_values") or {})
        | set(problem_seed.get("bound_variables") or [])
        | set(SHARED_CONSTANT_SYMBOLS)
    )
    target = str(problem_seed.get("target_unknown") or "")
    if target.isidentifier():
        local_names.add(target)
    local = {name: sympy.Symbol(name) for name in local_names if name.isidentifier()}
    local["Rational"] = sympy.Rational

    derived: list[dict[str, Any]] = []
    prior_ids: list[str] = []
    definers: dict[str, str] = {}
    procedure_order = 0
    for index, raw_step in enumerate(raw_steps, start=1):
        step = dict(raw_step)
        step_id = str(step["id"])
        content = dict(step["content"])
        content.pop("references", None)
        references = _declared_references(step)
        unknown_refs = [ref for ref in references if ref not in prior_ids]
        if unknown_refs:
            diagnostics.append(
                f"declared_reference: {step_id!r} references non-prior ids {unknown_refs!r}"
            )
        try:
            used_symbols, defined_symbol = _ordered_step_symbols(step, local)
        except TypedConstructionDefect as exc:
            diagnostics.extend(exc.diagnostics)
            used_symbols, defined_symbol = set(), _definition_symbol(step)

        symbolic = step.get("entry_type") == "equation" and not _is_display(step)
        if symbolic:
            undefined = sorted(
                name for name in used_symbols if name not in local_names and name not in definers
            )
            if undefined:
                diagnostics.append(
                    f"symbol_closure: {step_id!r} uses undefined case-sensitive symbols {undefined!r}"
                )
            dependencies = []
            for name in sorted(used_symbols):
                definer = definers.get(name)
                if definer is not None and definer not in dependencies:
                    dependencies.append(definer)
        else:
            dependencies = list(dict.fromkeys(references))
            if not references and prior_ids:
                dependencies = [prior_ids[-1]]

        if step.get("entry_type") == "procedure_step":
            procedure_order += 1
            content["order"] = procedure_order
            used_equations = content.get("uses_equations", []) or []
            if not isinstance(used_equations, list):
                diagnostics.append(
                    f"step_schema: procedure {step_id!r} uses_equations must be an array"
                )
                used_equations = []
            prior_equations = {item["id"] for item in derived if item["entry_type"] == "equation"}
            bad_equations = [str(eq) for eq in used_equations if str(eq) not in prior_equations]
            if bad_equations:
                diagnostics.append(
                    f"declared_reference: procedure {step_id!r} uses non-prior equations {bad_equations!r}"
                )

        clean = {
            "step": index,
            "entry_type": step["entry_type"],
            "id": step_id,
            "content": content,
            "depends_on": dependencies,
        }
        if step.get("entity_key") is not None:
            clean["entity_key"] = step["entity_key"]
        derived.append(clean)
        prior_ids.append(step_id)
        if defined_symbol:
            definers[defined_symbol] = step_id
            local_names.add(defined_symbol)
            local.setdefault(defined_symbol, sympy.Symbol(defined_symbol))

    if diagnostics:
        raise TypedConstructionDefect(diagnostics)

    problem_dict = {**problem_seed, "reference_solution": derived}
    if symbol_table is not None:
        problem_dict["symbol_table"] = symbol_table
    try:
        problem = Problem.model_validate(problem_dict)
        problem.to_kg_graph(attempt_id=0).topological_order(EdgeType.DEPENDS_ON)
    except Exception as exc:  # noqa: BLE001 - one typed construction failure surface
        raise TypedConstructionDefect([f"graph_derivation: {exc}"]) from exc
    return {
        **problem_dict,
        **problem.model_dump(exclude={"reference_solution"}),
        "reference_solution": [step.model_dump() for step in problem.reference_solution],
    }


# --------------------------------------------------------------------------- #
# The derivation prompt — the productized dress-rehearsal procedure.
# --------------------------------------------------------------------------- #

_DERIVATION_SYSTEM_PROMPT_LEGACY = (
    "You convert ONE problem and its TEACHER-PROVIDED worked solution into a "
    "teachable reference graph a student will later reconstruct by teaching it "
    "back. Derive the graph FROM THE WORKED SOLUTION ONLY — every node's "
    "content must trace to something the solution actually states or does. Do "
    "NOT invent alternative methods, extra steps, or facts absent from the "
    "solution.\n\n"
    "OUTPUT: a single JSON object with EXACTLY these keys (plus, optionally, "
    'the "symbol_table" key described below):\n'
    '  "reference_solution": array of 5 to 9 step objects (see below)\n'
    '  "target_unknown": the symbol or short phrase the problem solves for\n'
    '  "symbolic_mappings": object mapping declared substitutions the solution '
    'uses (e.g. {"u": "sin(x)"}); {} if none\n'
    '  "bound_variables": array of symbols that remain free in the final '
    "answer OR that the method introduces and the procedure determines — the "
    "integration variable x of an antiderivative, a series index n, sample "
    "points x0..xn / sampled values f0..fn and an opaque integrand symbol f "
    "in numerical rules, undetermined template coefficients like A, B, C in "
    "a partial-fraction decomposition, an error term and derivative bound "
    "like Et, K; [] for a fully numeric answer\n\n"
    + ontology_block()
    + "\n\nDERIVATION-SPECIFIC RULES — the validator also rejects violations:\n"
    '1. "uses_equations" lists equation step ids a procedure_step applies '
    "([] if it grounds in a condition/simplification instead — put that id "
    "in depends_on).\n"
    '2. Concrete algebraic equations: "symbolic" must be machine-parseable — '
    '"LHS = RHS" with EXPLICIT multiplication everywhere (write 2*x and '
    "A*(x+1); x(x+1) would parse as a function call), ** for powers, no "
    "unicode, no absolute-value bars (use Abs(...)). Every symbol in a "
    "concrete equation must come from the concept vocabulary, the problem's "
    "givens, your declared bound_variables, or the target. NEVER coin "
    "multi-word identifiers as symbols, and NEVER write a differential "
    "(dx, dtheta, du) in a concrete equation unless it is a listed vocabulary "
    "symbol — a substitution relation involving non-vocabulary differentials "
    "belongs in symbolic_mappings and prose, or as a display:true formula.\n"
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
    "that walk its execution. 5-9 nodes total."
)
# Backward-compatible private alias used by the DAG-3 prompt contract tests.
_DERIVATION_SYSTEM_PROMPT = _DERIVATION_SYSTEM_PROMPT_LEGACY

_DERIVATION_SYSTEM_PROMPT_KC = _DERIVATION_SYSTEM_PROMPT_LEGACY.replace(
    '"reference_solution": array of 5 to 9 step objects',
    '"reference_solution": array of 3 to 15 step objects',
).replace(
    "6. Include the governing equation/criterion the solution relies on, the "
    "choices it makes, the reductions it performs, and 2-4 procedure_steps "
    "that walk its execution. 5-9 nodes total.",
    "6. Granularity: one node per KNOWLEDGE COMPONENT — a single fact, concept "
    "application, or procedure step a learner masters (and can be assessed on) "
    "separately. Split a step when its parts are separately assessable; merge "
    "two statements only when they are inseparable as evidence of "
    "understanding. Include the governing equation/criterion the solution "
    "relies on, the choices it makes, the reductions it performs, and the "
    "procedure_steps that walk its execution. Let the solution determine the "
    "natural node count; stay within 3-15 nodes.",
)


def kc_granularity_enabled() -> bool:
    """Read per call so a flag flip does not require a process restart."""
    return os.getenv(_KC_GRANULARITY_FLAG, "").lower() in ("1", "true", "yes")


def _node_bounds() -> tuple[int, int]:
    if kc_granularity_enabled():
        return _KC_MIN_NODES, _KC_MAX_NODES
    return _LEGACY_MIN_NODES, _LEGACY_MAX_NODES


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


def _semantic_key_defect(step: dict) -> str | None:
    """DAG-3 semantic-entity-key defect: a ``step_3`` / ``eq1``-style id passes
    the opaque-id shape check but is a type/position echo — entity keys derive
    from ids at mint time, and ``eq.step_3`` defeats cross-problem resolution.
    An id (and an ``entity_key`` slug, when the step carries one) must contain
    at least one meaningful NON-GENERIC word."""

    def _echo(slug: str) -> bool:
        tokens = [t for t in slug.split("_") if t]
        alpha = [t.lower() for t in tokens if t.isalpha()]
        return bool(tokens) and all(t in GENERIC_ID_TOKENS for t in alpha)

    step_id = str(step.get("id"))
    if _echo(step_id):
        return f"semantic_key: id {step_id!r} is a type/position echo, not a meaningful slug"
    entity_key = str(step.get("entity_key") or "")
    if entity_key:
        slug = entity_key.split(".", 1)[-1]
        if _echo(slug) or slug == "":
            return (
                f"semantic_key: entity_key {entity_key!r} echoes a step position "
                "instead of naming the entity"
            )
    return None


def _transitive_depends_on(steps: list[dict]) -> dict[str, set[str]]:
    """step id -> the FULL transitive depends_on closure (ids only)."""
    direct: dict[str, set[str]] = {
        str(s.get("id")): {str(d) for d in (s.get("depends_on") or [])} for s in steps
    }
    closure: dict[str, set[str]] = {}

    def _walk(sid: str, seen: frozenset[str]) -> set[str]:
        if sid in closure:
            return closure[sid]
        out: set[str] = set()
        for dep in direct.get(sid, ()):  # unknown deps already flagged by schema
            if dep in seen:  # cycle — the cycle defect reports it; don't recurse
                continue
            out.add(dep)
            out |= _walk(dep, seen | {dep})
        closure[sid] = out
        return out

    for sid in direct:
        _walk(sid, frozenset({sid}))
    return closure


def _symbol_definers(steps: list[dict]) -> dict[str, set[str]]:
    """symbol -> ids of variable_mapping/definition steps that bind it."""
    definers: dict[str, set[str]] = {}
    for s in steps:
        sid = str(s.get("id"))
        content = s.get("content") or {}
        if s.get("entry_type") == "variable_mapping":
            symbol = str(content.get("symbol") or "")
            if symbol:
                definers.setdefault(symbol, set()).add(sid)
        elif s.get("entry_type") == "definition":
            # A definition binds a symbol only when it names it as a whole
            # token in its concept/meaning prose.
            text = f"{content.get('concept', '')} {content.get('meaning', '')}"
            for token in set(re.findall(r"[^\W\d]\w*", text)):
                definers.setdefault(token, set()).add(sid)
    return definers


def _dependency_completeness_defects(
    steps: list[dict],
    zero_forms: dict[str, Any],
    graph: dict,
) -> list[str]:
    """DAG-3: an equation using symbol X must have a variable_mapping/definition
    binding X in its TRANSITIVE depends_on closure — when the graph binds X at
    all. Whitelisted (never demand an upstream node; a shared constant must not
    fabricate a false dep): problem givens, declared bound variables, the
    target, symbolic_mappings keys, and ``SHARED_CONSTANT_SYMBOLS``. Symbols
    the graph never binds are the foreign-symbol check's business, not this
    one. Condition→equation edges carry no symbols, so they neither provide
    nor require coverage."""
    whitelist: set[str] = set((graph.get("given_values") or {}).keys())
    whitelist |= {str(b) for b in (graph.get("bound_variables") or [])}
    whitelist |= {str(k) for k in (graph.get("symbolic_mappings") or {}).keys()}
    whitelist |= set(SHARED_CONSTANT_SYMBOLS)
    target = str(graph.get("target_unknown") or "")
    if target:
        whitelist.add(target)

    definers = _symbol_definers(steps)
    closures = _transitive_depends_on(steps)
    defects: list[str] = []
    for s in steps:
        sid = str(s.get("id"))
        expr = zero_forms.get(sid)
        if expr is None:  # not a parsed concrete equation
            continue
        closure = closures.get(sid, set())
        for name in sorted(sym.name for sym in expr.free_symbols):
            if name in whitelist or name not in definers:
                continue
            if not (definers[name] & closure):
                binder = sorted(definers[name])[0]
                defects.append(
                    f"dependency_completeness: equation {sid!r} uses {name!r}, "
                    f"bound by {binder!r}, but {binder!r} is not in its "
                    f"transitive depends_on — add the dependency"
                )
    return defects


def _symbol_table_defects(steps: list[dict], zero_forms: dict[str, Any], graph: dict) -> list[str]:
    """DAG-3 problem-local symbol table (symbol -> role/ontology key/unit).

    OPTIONAL and backward-compatible: an absent table is legacy content and
    produces NO defect. When present, it must be a dict of dict entries, cover
    every symbol a concrete equation uses (case-sensitively — m and M are two
    quantities and need two entries), and not silently alias casings."""
    table = graph.get("symbol_table")
    if table is None:
        return []
    if not isinstance(table, dict) or not all(isinstance(v, dict) for v in table.values()):
        return ["symbol_table: must be an object mapping each symbol to an entry object"]
    defects: list[str] = []
    used: set[str] = set()
    for expr in zero_forms.values():
        used |= {sym.name for sym in expr.free_symbols}
    for name in sorted(used - set(SHARED_CONSTANT_SYMBOLS)):
        if name in table:
            continue
        case_variants = [k for k in table if k.lower() == name.lower()]
        if case_variants:
            defects.append(
                f"symbol_table: {name!r} is used but only {case_variants!r} is "
                f"tabled — symbols are case-sensitive (m and M are DIFFERENT "
                "quantities); add a separate entry"
            )
        else:
            defects.append(f"symbol_table: symbol {name!r} used in equations has no entry")
    return defects


# Every defect category ``find_derivation_defects`` can emit. The derivation
# path runs ALL of them (gold-corpus discipline); ``find_or_generate`` runs
# ``GENERATION_DEFECT_CLASSES`` — the vocabulary-independent subset (it has no
# concept vocabulary, so foreign_symbol would flag every legitimate symbol,
# and node_count is a derivation-gold rule, not a generation contract).
ALL_DEFECT_CLASSES: frozenset[str] = frozenset(
    {
        "schema",
        "node_count",
        "duplicate_id",
        "opaque_id",
        "semantic_key",
        "equation_parse",
        "foreign_symbol",
        "fragmentation",
        "dependency_completeness",
        "symbol_table",
        "cycle",
    }
)
GENERATION_DEFECT_CLASSES: frozenset[str] = ALL_DEFECT_CLASSES - {"node_count", "foreign_symbol"}


def find_derivation_defects(
    graph: dict,
    *,
    canonical_symbols: dict,
    normalization_map: dict,
    classes: frozenset[str] | None = None,
) -> list[str]:
    """Pure gold-format defect check. ``[]`` == clean; each entry is
    "category: detail" (categories: ``ALL_DEFECT_CLASSES``). ``classes``
    selects which categories run (default: all — the derivation-path
    behavior); every SYMBOLIC category self-deactivates on prose content
    (no parseable equations -> no symbolic defects) regardless of selection."""
    active = ALL_DEFECT_CLASSES if classes is None else classes
    defects: list[str] = []
    steps = list(graph.get("reference_solution") or [])

    try:
        Problem.model_validate(graph)
    except Exception as exc:  # noqa: BLE001 — any schema violation is one defect line
        if "schema" in active:
            defects.append(f"schema: {exc}")
        return defects  # structure unusable; deeper checks would throw

    min_nodes, max_nodes = _node_bounds()
    if "node_count" in active and not (min_nodes <= len(steps) <= max_nodes):
        defects.append(f"node_count: {len(steps)} nodes (gold standard is {min_nodes}-{max_nodes})")

    seen_ids: set[str] = set()
    for s in steps:
        sid = str(s.get("id"))
        if "duplicate_id" in active and sid in seen_ids:
            defects.append(f"duplicate_id: {sid}")
        seen_ids.add(sid)
        if "opaque_id" in active:
            opaque = _opaque_id_defect(sid)
            if opaque:
                defects.append(opaque)
        if "semantic_key" in active:
            echo = _semantic_key_defect(s)
            if echo:
                defects.append(echo)

    local = _local_dict(canonical_symbols, graph)
    # Foreign-symbol pre-check (mirrors lint gate 4's intent so the defect
    # feeds the derivation RETRY instead of a post-mint gate rejection): a
    # concrete equation may only use vocabulary symbols, problem givens,
    # declared bound variables, the target, normalizable names, or declared
    # substitution symbols.
    allowed_symbols: set[str] = set(local)
    allowed_symbols |= {str(v) for v in (normalization_map or {}).values()}
    allowed_symbols |= {str(k) for k in (graph.get("symbolic_mappings") or {}).keys()}
    # symbols the graph itself binds via variable_mapping nodes are graph-defined
    allowed_symbols |= {
        str((s.get("content") or {}).get("symbol") or "")
        for s in steps
        if s.get("entry_type") == "variable_mapping"
    }
    zero_forms: dict[str, Any] = {}
    for s in steps:
        if s.get("entry_type") != "equation" or _is_display(s):
            continue
        parse_defect = _equation_parse_defect(s, local)
        if parse_defect:
            if "equation_parse" in active:
                defects.append(parse_defect)
            continue
        expr = parse_zero_form(
            str(s["content"]["symbolic"]), entry_id=str(s.get("id")), local_dict=local
        )
        if "foreign_symbol" in active:
            foreign = sorted(
                sym.name for sym in expr.free_symbols if sym.name not in allowed_symbols
            )
            if foreign:
                defects.append(
                    f"foreign_symbol: {s.get('id')}: {foreign} not in the concept "
                    "vocabulary/givens/bound_variables/target — use vocabulary "
                    "symbols or mark the formula display:true"
                )
                continue
        if "fragmentation" in active:
            for other_id, other_expr in zero_forms.items():
                try:
                    if (
                        sympy.simplify(expr - other_expr) == 0
                        or sympy.simplify(expr + other_expr) == 0
                    ):
                        defects.append(
                            "fragmentation: equations "
                            f"{other_id!r} and {s.get('id')!r} state the same relation"
                        )
                except Exception:  # noqa: BLE001 — a simplify blowup is a non-defect
                    pass
        zero_forms[str(s.get("id"))] = expr

    if "fragmentation" in active:
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

    if "dependency_completeness" in active:
        defects.extend(_dependency_completeness_defects(steps, zero_forms, graph))
    if "symbol_table" in active:
        defects.extend(_symbol_table_defects(steps, zero_forms, graph))

    if "cycle" in active:
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
    system_prompt = (
        _DERIVATION_SYSTEM_PROMPT_KC
        if kc_granularity_enabled()
        else _DERIVATION_SYSTEM_PROMPT_LEGACY
    )

    def _ask(effort: str, feedback: list[str] | None) -> dict | None:
        user: dict[str, Any] = dict(base_user)
        if feedback:
            user["validator_defects_from_previous_attempt"] = feedback
        raw = chat_fn(
            purpose="graph_derivation",
            messages=[
                {"role": "system", "content": system_prompt},
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
