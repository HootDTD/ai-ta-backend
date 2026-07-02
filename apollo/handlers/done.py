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

import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import ReviewRequiredError
from apollo.grading.abstention import min_parser_confidence_of
from apollo.grading.artifact_build import GRADER_USED_GRAPH, GRADER_USED_LLM_FALLBACK
from apollo.handlers.artifact_writer import write_artifacts
from apollo.handlers.done_grading import ShadowGradeResult, run_graph_simulation
from apollo.handlers.done_inputs import (
    _find_problem_payload,  # noqa: F401 — re-export (relocated to done_inputs, WU-5B3a-0)
    build_rerun_inputs,
)
from apollo.handlers.learner_update import run_learner_update
from apollo.knowledge_graph.store import KGStore
from apollo.ontology import KGGraph, Node
from apollo.overseer.coverage import compute_coverage
from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.misconception import (
    MisconceptionSignal,
    summarize_for_rubric,
)
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.overseer.rubric import compute_rubric
from apollo.overseer.xp import compute_progress_envelope, compute_xp_earned
from apollo.persistence.attempt_history import has_prior_graded_attempt
from apollo.persistence.models import (
    ApolloSession,
    GradingArtifact,
    KGNegotiation,
    Message,
    ProblemAttempt,
    SessionPhase,
)
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.persistence.progress_repo import apply_xp
from apollo.projections.mastery import update_mastery_from_artifact
from apollo.projections.scorecard import render_scorecard
from apollo.schemas.problem import Problem

_LOG = logging.getLogger(__name__)

# P3.6 — Done-gate constants. The conf threshold (0.6) is intentionally
# below the OLM-invite threshold (0.7): the invite is opportunistic;
# the Done-gate is the final brake. Dropping below 0.6 means "the parser
# was unsure enough that it'd be reckless to grade against it without
# the student's eyes."
_DONE_GATE_LOW_CONF: float = 0.6
_DONE_GATE_FLAG: str = "APOLLO_DONE_GATE_ENABLED"

# WU-4C1 — the SHADOW graph-simulation flag (default OFF in prod, ON in test).
# When OFF, handle_done is byte-identical to today (the chain is never called).
# When ON, the chain runs AFTER the OLD grade/XP/retention commit and persists a
# comparison run + findings ALONGSIDE the unchanged student-facing grade. This is
# NOT the promote-to-live flag (that is WU-4C2's APOLLO_GRAPH_SIM_LIVE_ENABLED).
_GRAPH_SIM_SHADOW_FLAG: str = "APOLLO_GRAPH_SIM_SHADOW_ENABLED"

# WU-4C2 — the PROMOTE-to-live flag (default OFF EVERYWHERE incl. test; flipped
# only after human calibration review, NEVER in this build). When OFF, the
# student-facing rubric/diagnostic are the OLD-path values (byte-identical to
# WU-4C1). When ON, the graph-sim rubric + constrained diagnostic from the shadow
# chain REPLACE them. This gates only PROMOTION, NOT the shadow computation.
_GRAPH_SIM_LIVE_FLAG: str = "APOLLO_GRAPH_SIM_LIVE_ENABLED"

# WU-5A2 — the Layer-3 belief-PERSIST flag (default OFF EVERYWHERE incl. prod +
# staging). When OFF (the only build state), the gated `run_learner_update` call
# NEVER fires and `handle_done` is byte-identical to WU-4C2 (the shadow-flag-off
# regression guard `test_done_shadow_route_postgres.py` me==0/ls==0 stays green).
# When ON, the Done txn appends `apollo_mastery_events` + upserts
# `apollo_learner_state` (the §3 Bayesian belief) all-or-nothing AFTER the shadow
# persist. Flipping it ON is a later HUMAN calibration decision (same posture as
# APOLLO_GRAPH_SIM_LIVE_ENABLED), NOT part of this build.
_GRAPH_SIM_LAYER3_FLAG: str = "APOLLO_GRAPH_SIM_LAYER3_ENABLED"

# Campaign-plan Task A3 — the canonical-grading-artifact PERSIST flag (default
# OFF everywhere). When OFF, `write_artifacts` is never called and `handle_done`
# writes no `apollo_grading_artifacts` rows (byte-identical to pre-A3). When ON,
# ONE canonical row is written every Done-click (`grader_used="llm_fallback"` —
# this build never serves the graph grade; A4's `APOLLO_GRAPH_GRADER_LIVE` is
# the flag that flips `served`), plus a `pair` row with the graph-grader's
# artifact whenever the shadow chain ran and produced a result (paired-capture,
# spec section 5). This is orthogonal to `APOLLO_GRAPH_SIM_SHADOW_ENABLED`:
# artifact capture with NO shadow run still writes the single LLM canonical row
# so campaign runs always have a record, even on subjects/attempts where the
# shadow chain itself is off.
_GRAPH_SIM_ARTIFACT_FLAG: str = "APOLLO_GRADING_ARTIFACT_ENABLED"

# Campaign-plan Task A4 — the LIVE PROMOTION flag (default OFF everywhere; spec
# §3/§5). This is the spec's actual promotion switch and OPERATIONALLY
# SUPERSEDES the older dormant `APOLLO_GRAPH_SIM_LIVE_ENABLED` (WU-4C2), which
# stays in the codebase untouched but is not the flag the e2e campaign flips.
# Semantics: OFF -> `handle_done` is byte-identical to pre-A4 (this build's
# only state); ON + a successful, non-abstained shadow result -> the graph
# rubric/diagnostic are served (`grader_used="graph"` in the artifact) exactly
# like the WU-4C2 promotion; ON + an abstained shadow -> the (session-time)
# clarification loop already ran, so we fall back to the OLD/LLM values
# (`grader_used="llm_fallback"`) rather than re-run anything at Done time; ON +
# ANY exception anywhere in the graph path -> logged, `graph_failure` recorded
# on the artifact, OLD/LLM values served, HTTP 200 (a graph bug must never
# cost a student their grade). When OFF, a named infra error in the shadow
# chain still re-raises (today's NO-FALLBACK diagnostics, unchanged).
_GRAPH_GRADER_LIVE_FLAG: str = "APOLLO_GRAPH_GRADER_LIVE"

# Lane B1 / G3 — the shadow-failure marker prefix stamped onto the canonical
# LLM artifact's `abstention.graph_failure` when a SHADOW-mode (LIVE off)
# shadow-chain exception is caught and isolated (see the `except` in
# `handle_done`). Deliberately DISTINCT from the LIVE-mode fallback's bare
# `repr(e)`: paired analysis greps this prefix to tell "the shadow chain
# crashed during calibration, so there is no `pair` row" apart from "the live
# graph grader crashed and fell back to LLM" — the canonical row is the only
# artifact written in the shadow-crash case, and this marker is why.
_SHADOW_FAILURE_MARKER: str = "shadow_failure: "


def _done_gate_enabled() -> bool:
    return os.environ.get(_DONE_GATE_FLAG, "").lower() in ("1", "true", "yes")


def _graph_sim_shadow_enabled() -> bool:
    return os.environ.get(_GRAPH_SIM_SHADOW_FLAG, "").lower() in ("1", "true", "yes")


def _graph_sim_live_enabled() -> bool:
    return os.environ.get(_GRAPH_SIM_LIVE_FLAG, "").lower() in ("1", "true", "yes")


def _graph_sim_layer3_enabled() -> bool:
    return os.environ.get(_GRAPH_SIM_LAYER3_FLAG, "").lower() in ("1", "true", "yes")


def _grading_artifact_enabled() -> bool:
    return os.environ.get(_GRAPH_SIM_ARTIFACT_FLAG, "").lower() in ("1", "true", "yes")


def _graph_grader_live_enabled() -> bool:
    return os.environ.get(_GRAPH_GRADER_LIVE_FLAG, "").lower() in ("1", "true", "yes")


async def _project_mastery(db: AsyncSession, *, attempt_id: int) -> None:
    """Campaign-plan Task B2 — the composite-EWMA mastery projection, run
    AFTER `write_artifacts` has durably committed the canonical row. Reads
    that row back (its id/created_at only exist post-commit) and hands it to
    `update_mastery_from_artifact`, then owns its OWN commit — mirroring
    `write_artifacts`' own-failure-domain posture: this is telemetry-derived
    bookkeeping, not the grade itself, so ANY exception here is logged and
    swallowed rather than raised into the Done response. Guarded at the call
    site so this NEVER runs alongside the dormant WU-5A2
    `run_learner_update` (both write `apollo_mastery_events` /
    `apollo_learner_state`; running both would double-apply evidence)."""
    try:
        row = (
            await db.execute(
                select(GradingArtifact).where(
                    GradingArtifact.attempt_id == attempt_id,
                    GradingArtifact.role == "canonical",
                )
            )
        ).scalar_one_or_none()
        if row is None:  # defensive — write_artifacts already returned non-None
            return
        await update_mastery_from_artifact(db, artifact_row=row)
        await db.commit()
    except Exception:
        _LOG.exception("mastery_projection_failed attempt_id=%s", attempt_id)
        try:
            await db.rollback()
        except Exception:  # pragma: no cover - defensive, rollback itself failing
            _LOG.exception("mastery_projection_rollback_failed attempt_id=%s", attempt_id)


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


async def _find_problem(db: AsyncSession, concept_id: int, problem_code: str) -> Problem:
    for p in await list_problems_for_concept(db, concept_id=concept_id):
        if p.id == problem_code:
            return p
    raise RuntimeError(f"problem {problem_code!r} not in bank for cluster {concept_id!r}")


async def handle_done(
    *,
    db: AsyncSession,
    neo: Neo4jClient,
    session_id: int,
) -> dict[str, Any]:
    store = KGStore(db, neo)

    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    problem = await _find_problem(db, sess.concept_id, sess.current_problem_id)

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

    # Task A3 — grading-latency clock. Captured here (before the OLD grader
    # runs) so a persisted artifact's `grading_latency_ms` covers the WHOLE
    # grading pipeline for this Done-click (OLD coverage/rubric + the shadow
    # chain, when it runs) — not just one half of it.
    _artifact_t0 = time.monotonic()

    coverage = await compute_coverage(student_graph, reference_graph)

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
        reference_steps=[s.model_dump() for s in problem.reference_solution],
        problem_text=problem.problem_text,
        rubric=rubric,
    )

    # Re-attempt detection (unchanged from V2).
    is_reattempt_in_session = attempt.result is not None
    is_reattempt_cross_session = await has_prior_graded_attempt(
        db=db,
        user_id=sess.user_id,
        problem_id=problem.id,
        exclude_attempt_id=attempt.id,
    )
    is_reattempt = is_reattempt_in_session or is_reattempt_cross_session

    xp_earned = compute_xp_earned(
        overall_score=rubric["overall"]["score"],
        difficulty=attempt.difficulty,
        is_reattempt=is_reattempt,
    )

    attempt.result = "graded"
    attempt.solver_trace = None
    attempt.diagnostic_report = {
        "narrative": diagnostic_narrative,
        "rubric": rubric,
        "coverage": coverage,
    }
    sess.phase = SessionPhase.REPORT.value
    await db.commit()

    progress = await apply_xp(
        db=db,
        user_id=sess.user_id,
        xp_delta=xp_earned,
    )

    envelope = compute_progress_envelope(
        xp_earned=xp_earned,
        xp_before=progress["xp_before"],
        xp_after=progress["xp_after"],
    )

    # Retention (§7 / §6.4, WU-3C1): stamp `graded_at` on the now-frozen
    # subgraph. This is the FINAL, idempotent, post-commit retention write —
    # the student-facing grade + XP are already durable (committed above), so a
    # RetentionError here surfaces (NO FALLBACK) WITHOUT voiding the grade; the
    # next Done / retry / janitor re-stamps idempotently. Δt-anchoring in
    # Layer-3 (§3) reads this stored value, never now().
    #
    # WU-5A2: capture ONE `done_ts` and thread it into BOTH `stamp_graded_at`
    # (Neo4j `graded_at`) AND `run_learner_update` (Postgres `last_evidence_at`)
    # so the two stores stamp the IDENTICAL freeze instant (no second clock).
    done_ts = datetime.now(UTC)
    await store.stamp_graded_at(attempt_id=attempt.id, ts=done_ts)

    # The student-facing payload is constructed from OLD-path values ONLY. It is
    # byte-identical whether the shadow flag is on or off and whether the shadow
    # chain succeeds — the shadow result is NEVER merged into it (WU-4C1).
    student_response = {
        "rubric": rubric,
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

    # WU-4C1 — SHADOW graph-simulation chain. Runs AFTER the OLD grade/XP/retention
    # are fully durable, so any failure here surfaces a named error (the right HTTP
    # status) WITHOUT voiding the already-committed student grade (NO-FALLBACK,
    # mirrors RetentionError). When LIVE is off (the only build state) the
    # student_response above is NOT modified by it.
    #
    # Task A3 — `shadow` starts `None` so the artifact-writer call below (which
    # runs whether or not the shadow chain ran at all) can tell a "shadow flag
    # off" Done-click apart from one where the chain ran and returned a result.
    #
    # Task A4 — `graph_failure` carries the any-exception fallback reason (LIVE
    # mode only; see `_GRAPH_GRADER_LIVE_FLAG` above) and `served_grade` is the
    # `grader_used` value threaded into `write_artifacts` below. Both stay at
    # their OLD-path defaults (`None` / `llm_fallback`) unless LIVE promotes.
    shadow: ShadowGradeResult | None = None
    graph_failure: str | None = None
    served_grade = GRADER_USED_LLM_FALLBACK
    if _graph_sim_shadow_enabled():
        live = _graph_grader_live_enabled()
        try:
            # WU-5B3a-0: source the shadow problem_payload through the SHARED builder
            # (single source of truth with the future retry janitor). The builder keys
            # on attempt.problem_id (== problem.id at LIVE Done, since `attempt` was
            # found by ProblemAttempt.problem_id == problem.id), so this is
            # behavior-preserving here while the janitor reconstructs the OLD problem
            # later. student_graph + old_rubric stay the LIVE values (unchanged grade).
            rerun = await build_rerun_inputs(db, neo, attempt=attempt, sess=sess)
            shadow = await run_graph_simulation(
                db, neo,
                attempt=attempt,
                sess=sess,
                student_graph=student_graph,
                problem_payload=rerun.problem_payload,
                old_rubric=rubric,  # the OLD student-facing rubric, for §6.7 calibration
            )
            # WU-4C2 — LIVE promotion (DORMANT; flag OFF in this build). Built + tested,
            # never active. When ON, the graph-sim rubric + constrained-diagnostic
            # narrative REPLACE the two student-facing keys; coverage/progress/XP stay
            # OLD-path. Reached only AFTER a successful shadow chain (a raised shadow —
            # e.g. pending — never reaches here, so the OLD grade stands; §6.4).
            if shadow is not None and _graph_sim_live_enabled():
                student_response["rubric"] = shadow.graph_sim_rubric
                student_response["diagnostic_narrative"] = shadow.diagnostic.narrative

            # WU-5A2 — Layer-3 belief PERSIST (DORMANT; flag OFF in this build). When
            # ON, after the shadow persist the Done txn appends `apollo_mastery_events`
            # + upserts `apollo_learner_state` (the §3 Bayesian belief) all-or-nothing
            # with `done_ts` as the single `last_evidence_at`/`updated_at` instant. The
            # shadow result carries `audited`/`opposes_map`/`turn_order`; `parser_confidence`
            # is the §6.6 MIN over the student graph's parser confidences; `grader_confidence`
            # is derived in `persist_learner_update` from `shadow.normalization_confidence`.
            # A raised shadow (e.g. pending) never reaches here, so the gate is guarded
            # on `shadow is not None`; a Layer-3 failure sets `learner_update_pending`
            # without voiding the already-committed grade (NO-FALLBACK).
            if shadow is not None and _graph_sim_layer3_enabled():
                parser_confidence = min_parser_confidence_of(student_graph.nodes)
                await run_learner_update(
                    db,
                    sess=sess,
                    attempt=attempt,
                    shadow=shadow,
                    done_ts=done_ts,
                    parser_confidence=parser_confidence,
                )

            # Task A4 — the LIVE promotion (spec §3 steps 2-3): a successful,
            # non-abstained shadow result REPLACES the OLD-path rubric/
            # diagnostic exactly like the dormant WU-4C2 block above, and
            # `served_grade` flips so the artifact records `grader_used=
            # "graph"`. An abstained shadow means the (session-time)
            # clarification loop already ran and the graph grader is still
            # under-confident -> fall straight to the OLD/LLM values (spec
            # step 3); `served_grade` stays "llm_fallback" and the paired
            # graph artifact (written by `write_artifacts` below) carries the
            # abstention reasons for the record. This block is independent of
            # the dormant `APOLLO_GRAPH_SIM_LIVE_ENABLED` promotion above —
            # the two flags are never both on in any real deployment.
            if live and shadow is not None and not shadow.audited.abstained:
                student_response["rubric"] = shadow.graph_sim_rubric
                student_response["diagnostic_narrative"] = shadow.diagnostic.narrative
                served_grade = GRADER_USED_GRAPH
        except Exception as e:
            if live:
                # LIVE mode: ANY graph-path exception must never cost the student
                # their grade (spec §3 error handling). Log, record the failure on
                # the artifact, and serve the already-committed OLD/LLM values.
                # (UNCHANGED — pre-G3 Task A4 semantics; the shadow-isolation
                # branch below is the ONLY behavioral change in G3.)
                _LOG.exception(
                    "apollo_graph_grader_live_failure attempt_id=%s", int(attempt.id)
                )
                graph_failure = repr(e)[:500]
                shadow = None
                served_grade = GRADER_USED_LLM_FALLBACK
            else:
                # Lane B1 / G3 — SHADOW-mode crash isolation. During calibration
                # staging/prod run SHADOW on but LIVE off, so the shadow chain
                # computes a comparison run ALONGSIDE the student grade without
                # promoting it. Pre-G3 an exception here re-raised (the old
                # "NO-FALLBACK diagnostics for shadow-only mode"), which 500'd the
                # Done request and cost the student their ALREADY-COMMITTED LLM
                # grade — a shadow bug killing the live answer. That is wrong: the
                # shadow is telemetry, not the grade. So mirror the LIVE fallback's
                # posture WITHOUT promoting anything — log with full context, stamp
                # a shadow-failure marker on the canonical LLM artifact (so paired
                # analysis sees the missing `pair` row and WHY), drop the shadow
                # result, and serve the OLD/LLM values BYTE-IDENTICAL to a
                # shadow-off Done-click. No re-raise → HTTP 200 with the LLM grade.
                # (The shadow chain's OWN internal NO-FALLBACK bookkeeping —
                # `learner_update_pending` set + committed on a cross-store failure
                # — has already run before the error reached here; we only stop it
                # from reaching the HTTP layer.)
                _LOG.exception(
                    "apollo_graph_shadow_failure attempt_id=%s session_id=%s "
                    "served=llm_fallback (shadow isolated; live grade unaffected)",
                    int(attempt.id),
                    int(session_id),
                )
                graph_failure = f"{_SHADOW_FAILURE_MARKER}{repr(e)}"[:500]
                shadow = None
                served_grade = GRADER_USED_LLM_FALLBACK

    # Task A3 — paired canonical-artifact capture (DEFAULT OFF). Orthogonal to
    # `_graph_sim_shadow_enabled()`: with the shadow flag off, `shadow` is
    # `None` and exactly one LLM canonical row is written; with it on and a
    # shadow result present, a `pair` row with the graph-grader's artifact is
    # ALSO written (spec §5 paired-capture). Task A4 — `served_grade` is
    # `"graph"` only when `APOLLO_GRAPH_GRADER_LIVE` promoted a healthy,
    # non-abstained shadow result above; otherwise it is `"llm_fallback"`
    # (LIVE off, LIVE-on-but-abstained, or LIVE-on-with-a-caught exception).
    # `graph_failure` carries the any-exception fallback reason (LIVE only);
    # `None` in every other build state.
    # Task B1 — student scorecard projection (spec §2). Additive
    # `student_response["scorecard"]` key, attached only when artifact capture
    # is on (there is nothing to template over otherwise): `write_artifacts`
    # returns the CANONICAL payload it just persisted — the exact grade the
    # student was served, graph or LLM — and `render_scorecard` is a pure
    # template over it (no recomputation; same shape either way, spec §3
    # step 3). A failed artifact write returns `None`, so no scorecard is
    # attached rather than templating over a payload that was never durable.
    if _grading_artifact_enabled():
        artifact_latency_ms = int((time.monotonic() - _artifact_t0) * 1000)
        canonical_payload = await write_artifacts(
            db,
            attempt=attempt,
            sess=sess,
            shadow=shadow,
            coverage=coverage,
            rubric=rubric,
            served=served_grade,
            graph_failure=graph_failure,
            latency_ms=artifact_latency_ms,
        )
        if canonical_payload is not None:
            student_response["scorecard"] = render_scorecard(canonical_payload)
            # Task B2 — mastery ledger projection (spec section 2/3). Guarded off
            # whenever the dormant WU-5A2 Bayesian path is live (see
            # `_project_mastery`'s docstring): the two write paths must never
            # both fire for the same attempt.
            if _graph_sim_layer3_enabled():
                _LOG.info(
                    "mastery_projection_skipped_layer3_active attempt_id=%s",
                    int(attempt.id),
                )
            else:
                await _project_mastery(db, attempt_id=int(attempt.id))

    return student_response
