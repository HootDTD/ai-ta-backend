"""Sufficiency check — is the student's KG enough to solve the problem yet?

Class 2, Phase 1 (Apollo Gap D). Wraps the existing forward-chainer with a
reference-graph diff so every chat turn gets a cheap, deterministic
signal:

    sufficient   — the KG entails the target (SymPy can solve)
    almost       — only one missing variable, and a defining equation
                   exists in the reference KG
    insufficient — neither of the above

Apollo's confused-question persona is conditioned on this verdict
(handlers/chat.py wires it through). The signal is the per-turn analog
of what coverage already does at Done time, without any new LLM call on
the happy path.

Algorithmic anchor: FiDeLiS halt-condition pattern (arXiv 2405.13873) —
"halt once the question is deducible." Adapted to use SymPy as the
ground-truth deducer (Apollo already has it) and the reference KG diff
as the missing-premise signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from apollo.ontology import EdgeType, KGGraph
from apollo.solver.forward_chain import solve_kg_against_problem


SufficiencyState = Literal["sufficient", "almost", "insufficient"]


@dataclass(frozen=True)
class SufficiencyVerdict:
    """Per-turn sufficiency signal. Pure value object, no I/O."""

    state: SufficiencyState
    # Symbol names SymPy still needs to solve for. Empty when sufficient.
    missing_variables: tuple[str, ...] = ()
    # Reference KG node ids the student has not taught yet (when
    # reference_graph is provided).
    missing_kg_nodes: tuple[str, ...] = ()
    # Best single-fact-to-teach hint, derived from missing_variables /
    # missing_kg_nodes. None when sufficient.
    next_premise_hint: str | None = None
    # 1.0 when SymPy is decisive; lower when we fell back to heuristics.
    confidence: float = 1.0
    # Explainability log for offline analysis.
    trace: tuple[dict, ...] = field(default_factory=tuple)


def check_sufficiency(
    *,
    kg: dict,
    problem: dict,
    reference_graph: KGGraph | None = None,
) -> SufficiencyVerdict:
    """Compose existing SymPy forward-chainer with a reference-graph diff.

    Inputs match the existing solver contract:
    - `kg`: bag-shaped {"equation": [{symbolic, label, ...}, ...]} as
      already produced by the chat handler before calling the solver.
    - `problem`: dict with `target_unknown` and `given_values`, same
      shape forward_chain consumes today.
    - `reference_graph`: optional. When supplied, its nodes and PRECEDES
      chain are used to compute `missing_kg_nodes` and rank
      `next_premise_hint`. Without it, the verdict only carries the
      symbolic SymPy signal.

    No DB, no I/O. LLM verifier is intentionally NOT called here — it
    can be layered on a follow-up phase if eval shows the SymPy-only
    signal misses non-symbolic gaps.
    """

    trace: list[dict] = []

    # 1) SymPy forward-chain — soft-fail to insufficient on parse error so a
    #    single malformed equation doesn't break the chat turn.
    try:
        solver_out = solve_kg_against_problem(kg, problem)
    except Exception as exc:  # noqa: BLE001
        trace.append({"op": "solver_error", "error": str(exc)})
        return SufficiencyVerdict(
            state="insufficient",
            missing_variables=(problem.get("target_unknown", ""),),
            missing_kg_nodes=_diff_missing(kg, reference_graph),
            next_premise_hint=_pick_hint(
                missing_variables=(problem.get("target_unknown", ""),),
                missing_kg_nodes=_diff_missing(kg, reference_graph),
                reference_graph=reference_graph,
            ),
            confidence=0.0,
            trace=tuple(trace),
        )

    status = solver_out.get("status")
    missing_variables = tuple(solver_out.get("missing_variables") or ())
    trace.append({"op": "solver_status", "status": status,
                  "missing_variables": list(missing_variables)})

    missing_kg_nodes = _diff_missing(kg, reference_graph)

    # 2) sufficient — SymPy decisive.
    if status == "solved":
        # Even if SymPy is decisive symbolically, a non-empty reference
        # diff means the rubric expects more (e.g. unmatched
        # procedure_steps). Downgrade to `almost` when that's the case so
        # Apollo doesn't prematurely signal readiness on an under-justified
        # KG.
        if missing_kg_nodes:
            hint = _pick_hint(
                missing_variables=(),
                missing_kg_nodes=missing_kg_nodes,
                reference_graph=reference_graph,
            )
            return SufficiencyVerdict(
                state="almost",
                missing_variables=(),
                missing_kg_nodes=missing_kg_nodes,
                next_premise_hint=hint,
                confidence=0.7,
                trace=tuple(trace + [{"op": "solver_solved_but_rubric_gap"}]),
            )
        return SufficiencyVerdict(
            state="sufficient",
            missing_variables=(),
            missing_kg_nodes=(),
            next_premise_hint=None,
            confidence=1.0,
            trace=tuple(trace),
        )

    # 3) almost — exactly one missing variable AND a defining equation
    #    exists somewhere in the reference graph that mentions it AND the
    #    student doesn't already have it AND the student has already
    #    taught most of the reference (≤1 missing reference equation).
    #    The "≤1 missing reference" gate prevents an empty KG from
    #    flipping to `almost` just because some reference equation
    #    happens to mention the target symbol.
    if (
        len(missing_variables) == 1
        and reference_graph is not None
        and len(missing_kg_nodes) <= 1
        and _reference_has_unmet_equation_using(
            reference_graph, missing_variables[0], missing_kg_nodes,
        )
    ):
        hint = _pick_hint(
            missing_variables=missing_variables,
            missing_kg_nodes=missing_kg_nodes,
            reference_graph=reference_graph,
        )
        return SufficiencyVerdict(
            state="almost",
            missing_variables=missing_variables,
            missing_kg_nodes=missing_kg_nodes,
            next_premise_hint=hint,
            confidence=0.7,
            trace=tuple(trace + [{"op": "almost_one_missing"}]),
        )

    # 4) insufficient — anything else.
    hint = _pick_hint(
        missing_variables=missing_variables,
        missing_kg_nodes=missing_kg_nodes,
        reference_graph=reference_graph,
    )
    return SufficiencyVerdict(
        state="insufficient",
        missing_variables=missing_variables,
        missing_kg_nodes=missing_kg_nodes,
        next_premise_hint=hint,
        confidence=1.0,
        trace=tuple(trace),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _diff_missing(
    kg: dict,
    reference_graph: KGGraph | None,
) -> tuple[str, ...]:
    """Reference KG node ids the student hasn't taught.

    Match by content equivalence (symbolic for equations, label or
    applies_when for the others) — node_ids differ between authored
    reference and parser-extracted student entries.
    """
    if reference_graph is None:
        return ()

    student_signatures: set[str] = set()
    for e in kg.get("equation", []) or []:
        sig = _equation_signature(e.get("symbolic", ""))
        if sig:
            student_signatures.add(("equation", sig))  # type: ignore[arg-type]

    missing: list[tuple[Any, str]] = []
    for ref in reference_graph.by_type("equation"):
        sig = _equation_signature(getattr(ref.content, "symbolic", "") or "")
        key = ("equation", sig)
        if sig and key not in student_signatures:
            missing.append((_precedes_rank(ref.node_id, reference_graph), ref.node_id))

    # Stable sort: PRECEDES rank ascending (earliest unmet step first).
    missing.sort(key=lambda x: x[0])
    return tuple(nid for _, nid in missing)


def _equation_signature(symbolic: str) -> str:
    """Cheap canonical key for an equation. Whitespace + case stripped."""
    return "".join((symbolic or "").lower().split())


def _precedes_rank(node_id: str, graph: KGGraph) -> int:
    """Position in the PRECEDES topological order. Non-procedure_step
    nodes have no PRECEDES position; return a large constant so they
    sort to the end."""
    try:
        order = graph.topological_order(EdgeType.PRECEDES, node_type="procedure_step")
    except ValueError:
        return 10_000
    for idx, n in enumerate(order):
        if n.node_id == node_id:
            return idx
    return 10_000


def _reference_has_unmet_equation_using(
    graph: KGGraph,
    symbol: str,
    missing_kg_nodes: tuple[str, ...],
) -> bool:
    """True if some unmet (student doesn't have it yet) reference equation
    mentions `symbol` in its symbolic form.

    Approximate but cheap: treats symbolic as a tokenised string. Good
    enough for `almost`-band detection; a defining equation that mentions
    `v2` will contain "v2" verbatim.

    Important: only considers equations the student hasn't taught yet
    (i.e. those in `missing_kg_nodes`). An equation the student already
    has cannot, by definition, be the equation that would unblock the
    missing variable — SymPy would have used it.
    """
    needle = symbol.strip()
    if not needle:
        return False
    missing_set = set(missing_kg_nodes)
    for n in graph.by_type("equation"):
        if n.node_id not in missing_set:
            continue
        sym = getattr(n.content, "symbolic", "") or ""
        if needle in sym:
            return True
    return False


def _pick_hint(
    *,
    missing_variables: tuple[str, ...],
    missing_kg_nodes: tuple[str, ...],
    reference_graph: KGGraph | None,
) -> str | None:
    """Choose the single most-helpful next fact for the student to teach.

    Priority:
    1. The earliest missing reference KG node (by PRECEDES order). This
       is the structural answer — what the rubric expects next.
    2. The first SymPy missing_variable. This is the symbolic answer —
       what the solver would unblock with one more equation.
    3. None.
    """
    if missing_kg_nodes and reference_graph is not None:
        first_id = missing_kg_nodes[0]
        idx = reference_graph.node_index()
        node = idx.get(first_id)
        if node is not None:
            return _summarize_node_for_hint(node)
    if missing_variables:
        return missing_variables[0]
    return None


def _summarize_node_for_hint(node: Any) -> str:
    """Compact human-readable hint for one reference KG node.

    The hint is consumed by Apollo's system prompt — it should be specific
    enough to bias the next ignorant question, but not leak the full
    canonical formulation.
    """
    nt = node.node_type
    c = node.content
    if nt == "equation":
        label = getattr(c, "label", "") or "(unnamed equation)"
        return f"equation: {label}"
    if nt == "condition":
        return f"condition: {getattr(c, 'label', '') or getattr(c, 'applies_when', '')}"
    if nt == "simplification":
        return f"simplification: {getattr(c, 'applies_when', '')}"
    if nt == "definition":
        return f"definition: {getattr(c, 'concept', '')}"
    if nt == "variable_mapping":
        return f"variable: {getattr(c, 'term', '')}"
    if nt == "procedure_step":
        return f"procedure step: {getattr(c, 'action', '')}"
    return str(getattr(c, "label", "") or "")
