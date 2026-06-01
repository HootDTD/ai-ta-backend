"""POST /apollo/sessions/{id}/done — freeze, solve, grade, narrate, award XP.

V3: KGStore.read_graph returns a typed KGGraph; reference graph is derived
from the problem via Problem.to_kg_graph(); coverage walks both graphs;
rubric consumes Node objects directly. Hardcoded `g=9.81` and per-problem
augmentations come from the concept registry, not from this file.

P3.6: before freezing the session, the Done-gate scans the KG for entries
with `parser_confidence < 0.6` or `status == DISPUTED` and refuses to
proceed if any of them have not been touched with a negotiation move
(challenge / paraphrase / skip). Behind env flag
`APOLLO_DONE_GATE_ENABLED` (default off) until manual UX verification.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import ReviewRequiredError
from apollo.knowledge_graph.store import KGStore
from apollo.ontology import KGGraph, Node
from apollo.overseer.coverage import compute_coverage
from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.misconception import (
    MisconceptionSignal,
    summarize_for_rubric,
)
from apollo.overseer.problem_selector import (
    cluster_to_concept,
    list_problems_for_cluster,
)
from apollo.overseer.rubric import compute_rubric
from apollo.overseer.xp import compute_progress_envelope, compute_xp_earned
from apollo.persistence.attempt_history import has_prior_graded_attempt
from apollo.persistence.models import (
    ApolloSession,
    KGNegotiation,
    Message,
    ProblemAttempt,
    SessionPhase,
)
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.persistence.progress_repo import apply_xp
from apollo.schemas.problem import Problem
from apollo.solver.forward_chain import solve_kg_against_problem
from apollo.solver.sympy_exec import _format_value_text
from apollo.subjects import load_concept


# P3.6 — Done-gate constants. The conf threshold (0.6) is intentionally
# below the OLM-invite threshold (0.7): the invite is opportunistic;
# the Done-gate is the final brake. Dropping below 0.6 means "the parser
# was unsure enough that it'd be reckless to grade against it without
# the student's eyes."
_DONE_GATE_LOW_CONF: float = 0.6
_DONE_GATE_FLAG: str = "APOLLO_DONE_GATE_ENABLED"


def _done_gate_enabled() -> bool:
    return os.environ.get(_DONE_GATE_FLAG, "").lower() in ("1", "true", "yes")


def _flagged_entries(graph: KGGraph) -> list[tuple[Node, str]]:
    """Return (node, reason) pairs for every entry that the Done-gate
    cares about. `reason` is "disputed" | "low_confidence" — disputed
    wins when both apply (it's the more specific signal).

    Only parser-sourced nodes are checked: reference and system-sourced
    nodes are never user-authored, so they can't be wrong about what the
    student said.
    """
    flagged: list[tuple[Node, str]] = []
    for n in graph.nodes:
        if n.source != "parser":
            continue
        # DUAL means the student already engaged via challenge / paraphrase /
        # skip, OR via the lower-level kg-store path. Either way, the gate
        # has nothing to add — coverage handles DUAL via student_belief.
        if n.status == "DUAL":
            continue
        if n.status == "DISPUTED":
            flagged.append((n, "disputed"))
        elif n.parser_confidence < _DONE_GATE_LOW_CONF:
            flagged.append((n, "low_confidence"))
    return flagged


def _node_summary_for_review(node: Node) -> str:
    """Short surface form of a node for the FE's review modal. Mirrors
    the OLM-invite summary helper but lives here to avoid the chat
    handler import path. Capped to one line."""
    c = node.content.model_dump()
    if node.node_type == "equation":
        return c.get("symbolic", "")[:120]
    if node.node_type == "condition":
        return (c.get("applies_when") or c.get("label") or "")[:120]
    if node.node_type == "simplification":
        return (c.get("transformation") or "")[:120]
    if node.node_type == "definition":
        return f"{c.get('concept', '')} = {c.get('meaning', '')}"[:120]
    if node.node_type == "variable_mapping":
        return f"{c.get('term', '')} → {c.get('symbol', '')}"[:120]
    if node.node_type == "procedure_step":
        return (c.get("action") or "")[:120]
    return ""


async def _entries_with_moves(
    db: AsyncSession, *, attempt_id: int,
) -> set[str]:
    """Return the set of `entry_id`s that have at least one negotiation
    move recorded for this attempt. The Done-gate clears once every
    flagged entry is in this set."""
    rows = (await db.execute(
        select(KGNegotiation.entry_id)
        .where(KGNegotiation.attempt_id == attempt_id)
    )).scalars().all()
    return set(rows)


async def _enforce_done_gate(
    db: AsyncSession, *, attempt_id: int, graph: KGGraph,
) -> None:
    """Raises ReviewRequiredError if any flagged entry lacks a negotiation
    move. Caller invokes this before freeze so failures don't lock the
    session into an unrecoverable state."""
    flagged = _flagged_entries(graph)
    if not flagged:
        return
    moved = await _entries_with_moves(db, attempt_id=attempt_id)

    review_required = []
    for node, reason in flagged:
        if node.node_id in moved:
            continue
        review_required.append({
            "entry_id": node.node_id,
            "type": node.node_type,
            "reason": reason,
            "summary": _node_summary_for_review(node),
        })
    if review_required:
        raise ReviewRequiredError(entries=review_required)


async def _attempt_misconception_scores(
    db: AsyncSession, *, attempt_id: int,
) -> dict[str, float]:
    """Read every Apollo turn for this attempt and reduce misconception
    signals to a per-bank-code score map for the rubric's axis.

    Reads from `apollo_messages.metadata` (migration 020). Skips messages
    whose metadata is null or has no misconception payload. Returns an
    empty dict when nothing fired — the rubric treats that as
    axis-absent and falls back to the pre-P2.8 60/25/15 weights.
    """
    rows = (await db.execute(
        select(Message.message_metadata)
        .where(Message.attempt_id == attempt_id)
        .where(Message.role == "apollo")
        .order_by(Message.turn_index)
    )).scalars().all()

    signals: list[MisconceptionSignal] = []
    for payload in rows:
        if not isinstance(payload, dict):
            continue
        raw = payload.get("misconception")
        if not isinstance(raw, dict):
            continue
        state = raw.get("state", "default")
        if state not in {"default", "probe", "socratic"}:
            continue
        signals.append(MisconceptionSignal(
            fired=bool(raw.get("fired", False)),
            state=state,  # type: ignore[arg-type]
            bank_code=raw.get("bank_code"),
            confidence=float(raw.get("confidence", 0.0) or 0.0),
        ))

    return summarize_for_rubric(signals)


def _find_problem(cluster_id: str, problem_id: str) -> Problem:
    for p in list_problems_for_cluster(cluster_id):
        if p.id == problem_id:
            return p
    raise RuntimeError(f"problem {problem_id!r} not in bank for cluster {cluster_id!r}")


def _serializable_trace(trace: list) -> list:
    out = []
    for entry in trace:
        out.append({k: (str(v) if k == "value" else v) for k, v in entry.items()})
    return out


def _display_value(val) -> str | None:
    if val is None:
        return None
    return _format_value_text(val)


async def handle_done(
    *,
    db: AsyncSession,
    neo: Neo4jClient,
    session_id: int,
) -> Dict[str, Any]:
    store = KGStore(db, neo)

    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    problem = _find_problem(sess.concept_cluster_id, sess.current_problem_id)

    attempt = (
        await db.execute(
            select(ProblemAttempt)
            .where(ProblemAttempt.session_id == session_id)
            .where(ProblemAttempt.problem_id == problem.id)
            .order_by(ProblemAttempt.id.desc())
        )
    ).scalars().first()
    if attempt is None:
        raise RuntimeError(
            f"no ProblemAttempt for session {session_id} / problem {problem.id}"
        )

    # P3.6 — Done-gate. Read the graph BEFORE freezing so a 422 doesn't
    # lock the student into PROBLEM_REVEAL. When the master flag is off,
    # we skip the gate entirely; behavior is byte-identical to pre-P3.6.
    pre_freeze_graph = await store.read_graph(attempt_id=attempt.id)
    if _done_gate_enabled():
        await _enforce_done_gate(
            db, attempt_id=attempt.id, graph=pre_freeze_graph,
        )

    await store.freeze(session_id)

    student_graph = pre_freeze_graph
    reference_graph = problem.to_kg_graph(attempt_id=attempt.id)

    sess.phase = SessionPhase.SOLVING.value
    await db.commit()

    # Concept-driven augmented givens (was hardcoded `g=9.81` in V2).
    subject_id, concept_id = cluster_to_concept(sess.concept_cluster_id)
    concept = load_concept(subject_id, concept_id)

    augmented_givens = dict(problem.given_values)
    for k, v in concept.solver_hints.augmented_givens.items():
        augmented_givens.setdefault(k, v)

    # Per-simplification augmentation: reference simplification "h1==h2"
    # injects h1=h2=0 if the student didn't supply them. Detection lives
    # here because it depends on the reference solution; the constants live
    # in the concept registry.
    for ref in problem.reference_solution:
        if ref.entry_type == "simplification":
            aw = (ref.content.get("applies_when") or "").lower().replace(" ", "")
            if "h1==h2" in aw:
                augmented_givens.setdefault("h1", 0.0)
                augmented_givens.setdefault("h2", 0.0)

    # Solver still consumes the bag-shaped {"equation": [...]} input.
    solver_kg = {
        "equation": [n.content.model_dump() for n in student_graph.by_type("equation")],
    }
    solver_result = solve_kg_against_problem(solver_kg, {
        "id": problem.id,
        "given_values": augmented_givens,
        "target_unknown": problem.target_unknown,
    })

    coverage = compute_coverage(student_graph, reference_graph)

    # Class 2 Phase 2 (P2.8): pull per-attempt misconception signals from
    # apollo_messages.metadata and reduce them to the per-bank-code score
    # map the rubric expects. The axis enters at 5% taken from the
    # existing 60/25/15. When no misconceptions fired, the dict is empty
    # and the rubric is byte-identical to its pre-P2.8 output.
    misconception_scores = await _attempt_misconception_scores(
        db, attempt_id=attempt.id,
    )
    rubric = compute_rubric(
        coverage,
        reference_graph.nodes,
        misconception_scores=misconception_scores,
    )

    diagnostic_narrative = generate_diagnostic(
        coverage=coverage,
        solver_result=solver_result,
        reference_steps=[s.model_dump() for s in problem.reference_solution],
        problem_text=problem.problem_text,
        rubric=rubric,
    )

    solver_indicator: Dict[str, Any] = {
        "reached": solver_result["status"] == "solved",
    }
    value_str = _display_value(solver_result.get("value"))
    if value_str is not None:
        solver_indicator["value"] = value_str
    if solver_result.get("missing_variables"):
        solver_indicator["missing"] = solver_result["missing_variables"]

    # Re-attempt detection (unchanged from V2).
    is_reattempt_in_session = attempt.result is not None
    is_reattempt_cross_session = await has_prior_graded_attempt(
        db=db,
        student_id=sess.student_id,
        problem_id=problem.id,
        exclude_attempt_id=attempt.id,
    )
    is_reattempt = is_reattempt_in_session or is_reattempt_cross_session

    xp_earned = compute_xp_earned(
        overall_score=rubric["overall"]["score"],
        difficulty=attempt.difficulty,
        is_reattempt=is_reattempt,
    )

    attempt.result = solver_result["status"]
    attempt.solver_trace = {
        "trace": _serializable_trace(solver_result["trace"]),
        "value": value_str,
        "missing_variables": solver_result.get("missing_variables", []),
    }
    attempt.diagnostic_report = {
        "narrative": diagnostic_narrative,
        "rubric": rubric,
        "coverage": coverage,
    }
    sess.phase = SessionPhase.REPORT.value
    await db.commit()

    progress = await apply_xp(
        db=db,
        student_id=sess.student_id,
        xp_delta=xp_earned,
    )

    envelope = compute_progress_envelope(
        xp_earned=xp_earned,
        xp_before=progress["xp_before"],
        xp_after=progress["xp_after"],
    )

    return {
        "rubric": rubric,
        "solver_indicator": solver_indicator,
        "diagnostic_narrative": diagnostic_narrative,
        "coverage": coverage,
        # Item #9: structured progress envelope is the single source of
        # truth for level / threshold display. Flat fields stay during
        # the FE migration window so older clients still render.
        "progress": {
            "xp_earned": envelope.xp_earned,
            "xp_before": envelope.xp_before,
            "xp_after": envelope.xp_after,
            "level_before": envelope.level_before,
            "level_after": envelope.level_after,
            "level_up": envelope.level_up,
            "title_after": envelope.title_after,
            "level_progress_pct": envelope.level_progress_pct,
            "xp_to_next_level": envelope.xp_to_next_level,
        },
        "xp_earned": envelope.xp_earned,
        "xp_before": envelope.xp_before,
        "xp_after": envelope.xp_after,
        "level_before": envelope.level_before,
        "level_after": envelope.level_after,
        "level_up": envelope.level_up,
    }
