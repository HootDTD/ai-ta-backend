"""POST /apollo/sessions/{id}/done — freeze, solve, grade, narrate, award XP.

V3: KGStore.read_graph returns a typed KGGraph; reference graph is derived
from the problem via Problem.to_kg_graph(); coverage walks both graphs;
rubric consumes Node objects directly. Hardcoded `g=9.81` and per-problem
augmentations come from the concept registry, not from this file.

"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.emergent.capture import record_detector_births
from apollo.emergent.config import emergent_map_capture_enabled
from apollo.emergent.materialize import materialize_if_promotable
from apollo.errors import (
    CoverageGradingError,
    KGUnavailableError,
    ResolutionUnavailableError,
    RetentionError,
    TranscriptAuditUnavailableError,
)
from apollo.grading.abstention import min_parser_confidence_of
from apollo.grading.artifact_build import GRADER_USED_GRAPH, GRADER_USED_LLM_FALLBACK
from apollo.graph_compare.validator import (
    ReferenceGraphInvalidError,
    StudentGraphInvalidError,
)
from apollo.handlers.artifact_writer import write_artifacts
from apollo.handlers.done_grading import ShadowGradeResult, run_graph_simulation
from apollo.handlers.done_inputs import (
    _find_problem_payload,  # noqa: F401 — re-export (relocated to done_inputs, WU-5B3a-0)
    build_rerun_inputs,
)
from apollo.handlers.learner_update import run_learner_update
from apollo.knowledge_graph.store import KGStore
from apollo.ontology import KGGraph
from apollo.overseer.coverage import compute_coverage
from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.misconception import (
    MisconceptionSignal,
    summarize_for_rubric,
)
from apollo.overseer.misconception_bank import MisconceptionEntry, load_for_concept
from apollo.overseer.misconception_detector.apply import rubric_overall_after_penalty
from apollo.overseer.misconception_detector.centrality import compute_centrality
from apollo.overseer.misconception_detector.config import (
    detector_enabled,
    grader_positive_focus_enabled,
    struct_cokey_enabled,
    topic_score_served_enabled,
    trace_enabled,
    transcript_grader_enabled,
)
from apollo.overseer.misconception_detector.detector import detect_misconceptions
from apollo.overseer.misconception_detector.gate import gate_findings
from apollo.overseer.misconception_detector.judge import make_openai_judge
from apollo.overseer.misconception_detector.merge import merge_detections
from apollo.overseer.misconception_detector.types import JudgeFn, MergeOutcome
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.overseer.rubric import compute_rubric
from apollo.overseer.topic_score import TopicScoreResult, compute_topic_score
from apollo.overseer.topic_score_serialize import serialize_topics
from apollo.overseer.transcript_coverage import compute_transcript_coverage
from apollo.overseer.xp import compute_progress_envelope, compute_xp_earned
from apollo.persistence.attempt_history import has_prior_graded_attempt
from apollo.persistence.models import (
    ApolloSession,
    GradingArtifact,
    Message,
    ProblemAttempt,
    SessionPhase,
)
from apollo.persistence.neo4j_client import KG_DEGRADED_ERRORS, Neo4jClient
from apollo.persistence.progress_repo import apply_xp
from apollo.projections.mastery import update_mastery_from_artifact
from apollo.projections.scorecard import render_scorecard
from apollo.schemas.problem import Problem

_LOG = logging.getLogger(__name__)

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

# T13 — the misconception-detector flag (default OFF everywhere). Pinned here so
# the route/config key matches the spec; the reader lives in
# `apollo/overseer/misconception_detector/config.py::detector_enabled` (imported
# above). When OFF, the parallel detection stage in `handle_done` never runs and
# the student-facing payload is byte-identical to today (design invariant #1).
_MISCONCEPTION_DETECTOR_FLAG: str = "APOLLO_MISCONCEPTION_DETECTOR"

# T13 — the raw student-turn role for `_student_utterances` (R6, RESOLVED): live
# transcript roles are exactly {"apollo", "student"}; the Apollo learner turns
# (read by `_attempt_misconception_scores`) are "apollo", the student's raw
# teaching utterances (which feed the bank_pattern tier) are "student".
_STUDENT_ROLE: str = "student"

# Lane B1 / G3 — the shadow-failure marker prefix stamped onto the canonical
# LLM artifact's `abstention.graph_failure` when a SHADOW-mode (LIVE off)
# shadow-chain exception is caught and isolated (see the `except` in
# `handle_done`). Deliberately DISTINCT from the LIVE-mode fallback's bare
# `repr(e)`: paired analysis greps this prefix to tell "the shadow chain
# crashed during calibration, so there is no `pair` row" apart from "the live
# graph grader crashed and fell back to LLM" — the canonical row is the only
# artifact written in the shadow-crash case, and this marker is why.
_SHADOW_FAILURE_MARKER: str = "shadow_failure: "

# Lane B1 / G3 (narrowed) — the CONTRACTUAL typed failure modes that must keep
# propagating out of the shadow chain in SHADOW mode. Authoritative source:
# `apollo/api.py::register_exception_handlers` — these are exactly the
# shadow-chain error types the done route explicitly maps to a NON-500
# response, each with load-bearing commit semantics the route tests pin
# (`tests/database/test_done_shadow_route_postgres.py`):
#   * ResolutionUnavailableError      -> 503 (pending set + committed; retryable)
#   * TranscriptAuditUnavailableError -> 503 (pending set + committed; retryable)
#   * StudentGraphInvalidError        -> 422 (raised pre-cross-store; no pending)
#   * ReferenceGraphInvalidError      -> 409 (raised pre-cross-store; no pending)
# `ResolutionInvalidOutputError` is deliberately ABSENT: its handler maps to
# 500 — exactly the "shadow bug 500s the live grade" class G3 isolates — so it
# falls to the isolation branch with everything else unexpected (e.g. the G3
# KeyError, CanonProjectionError). LIVE mode is untouched by this list: the A4
# any-exception fallback still catches ALL types, including these four.
_SHADOW_PROPAGATE_ERRORS: tuple[type[Exception], ...] = (
    ResolutionUnavailableError,
    TranscriptAuditUnavailableError,
    StudentGraphInvalidError,
    ReferenceGraphInvalidError,
)


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


async def _attempt_misconception_scores(
    db: AsyncSession,
    *,
    attempt_id: int,
) -> dict[str, float]:
    """Read every Apollo turn for this attempt and reduce misconception
    signals to a per-bank-code score map for the rubric's axis.

    Reads from `apollo_messages.metadata` (migration 020). Skips messages
    whose metadata is null or has no misconception payload. Returns an
    empty dict when nothing fired — the rubric treats that as
    axis-absent and falls back to the pre-P2.8 60/25/15 weights.
    """
    rows = (
        (
            await db.execute(
                select(Message.message_metadata)
                .where(Message.attempt_id == attempt_id)
                .where(Message.role == "apollo")
                .order_by(Message.turn_index)
            )
        )
        .scalars()
        .all()
    )

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
        signals.append(
            MisconceptionSignal(
                fired=bool(raw.get("fired", False)),
                state=state,  # type: ignore[arg-type]
                bank_code=raw.get("bank_code"),
                confidence=float(raw.get("confidence", 0.0) or 0.0),
            )
        )

    return summarize_for_rubric(signals)


async def _student_utterances(
    db: AsyncSession,
    *,
    attempt_id: int,
) -> tuple[str, ...]:
    """T13 — the raw student teaching utterances for this attempt, in turn
    order, that feed the misconception detector's ``bank_pattern`` tier.

    Reads ``Message.content`` where ``Message.role == "student"`` (R6 —
    the CONFIRMED student-turn role, distinct from the "apollo" learner
    turns ``_attempt_misconception_scores`` reads) ordered by
    ``turn_index``. Returns a tuple (immutable) so the detector's frozen
    value objects stay list-free end to end. Empty tuple when the student
    never spoke (a valid, common case — the detector's tiers all abstain on
    empty utterances)."""
    rows = (
        (
            await db.execute(
                select(Message.content)
                .where(Message.attempt_id == attempt_id)
                .where(Message.role == _STUDENT_ROLE)
                .order_by(Message.turn_index)
            )
        )
        .scalars()
        .all()
    )
    return tuple(rows)


async def _full_transcript(
    db: AsyncSession,
    *,
    attempt_id: int,
) -> tuple[tuple[str, str], ...]:
    """Return both-role attempt messages in canonical turn order."""
    rows = (
        await db.execute(
            select(Message.role, Message.content)
            .where(Message.attempt_id == attempt_id)
            .order_by(Message.turn_index)
        )
    ).all()
    return tuple((role, content) for role, content in rows)


async def _load_bank_entries(
    db: AsyncSession,
    *,
    concept_id: int | None,
) -> tuple[MisconceptionEntry, ...]:
    """F-struct — soft-failing bank load for ``build_opposes_index``, mirroring
    ``misconception_detector.detector._load_bank`` (that helper is private to
    its module, so this is a small local copy rather than an import of a
    leading-underscore name). No ``concept_id`` -> empty bank; any load error
    (transient DB failure) also degrades to an empty bank rather than raising
    — an empty bank makes ``build_opposes_index`` return ``{}``, which is the
    same no-op ``gate_findings`` already tolerates via its own
    ``opposes_index or {}`` default."""
    if concept_id is None:
        return ()
    try:
        entries = await load_for_concept(db, concept_id=concept_id)
    except Exception:  # noqa: BLE001
        _LOG.exception("misconception_struct_cokey_bank_load_failed concept_id=%s", concept_id)
        return ()
    return tuple(entries)


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Lazy indirection over the project-wide batched embedder so importing
    ``done`` never pulls in the OpenAI SDK (mirrors
    ``apollo/resolution/embedding.py::default_embedder``). Split out as its
    own module-level name so tests can patch the batched call without
    touching ``_default_embed_fn``'s single-vector wrapper logic."""
    from indexing.document_embedder import embed_texts

    return embed_texts(texts)


def _default_embed_fn(text: str) -> list[float]:
    """Production ``EmbedFn`` (DI seam for the ``bank_pattern`` tier). Wraps the
    batched ``embed_texts`` (which returns a list of vectors) into the
    single-text -> single-vector shape the ``EmbedFn`` Protocol declares
    (``types.py``: ``__call__(text: str) -> list[float]``). Degrades to an
    empty vector on an empty batch result rather than raising IndexError —
    the ``bank_pattern`` tier treats a zero-norm/empty vector as "no match"."""
    vectors = _embed_texts([text])
    return vectors[0] if vectors else []


def _default_judge_fn() -> JudgeFn:
    """Production ``JudgeFn`` factory for the detector's Tier-2 judge. Thin
    indirection over ``make_openai_judge`` so the call site in ``handle_done``
    reads symmetrically with ``_default_embed_fn`` and so a test can patch the
    factory without patching the OpenAI-touching builder itself."""
    return make_openai_judge()


def _compute_topic_score_safe(
    *,
    coverage: dict,
    reference_graph: KGGraph,
    centrality: dict[str, float] | None,
    detection_outcome: MergeOutcome | None,
    attempt_id: int,
) -> TopicScoreResult | None:
    """Soft-failing wrapper around ``compute_topic_score`` (2026-07-10 spec
    §3): computed ALWAYS (flag-independent — the artifact gets telemetry
    before any serving flip), but any exception here must never break a Done.
    ``centrality`` may be ``None`` when the detector stage never ran; it is
    then computed fresh from ``reference_graph`` here (``compute_centrality``
    is pure and cheap, so recomputing rather than threading an Optional
    through the detector-off branch keeps the call site simple). Any
    exception anywhere in this function (including centrality recomputation)
    is logged and swallowed — the caller receives ``None`` and proceeds with
    ``topic_score`` absent from both the artifact and the served payload."""
    try:
        resolved_centrality = (
            centrality if centrality is not None else compute_centrality(reference_graph)
        )
        return compute_topic_score(
            coverage=coverage,
            reference_nodes=reference_graph.nodes,
            centrality=resolved_centrality,
            detection_outcome=detection_outcome,
        )
    except Exception:
        _LOG.exception("topic_score_computation_failed attempt_id=%s", attempt_id)
        return None


async def _find_problem(db: AsyncSession, concept_id: int, problem_code: str) -> Problem:
    for p in await list_problems_for_concept(db, concept_id=concept_id):
        if p.id == problem_code:
            return p
    raise RuntimeError(f"problem {problem_code!r} not in bank for cluster {concept_id!r}")


async def handle_done(
    *,
    db: AsyncSession,
    neo: Neo4jClient | None,
    session_id: int,
) -> dict[str, Any]:
    store = KGStore(db, neo)

    sess = (
        await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))
    ).scalar_one()
    problem = await _find_problem(db, sess.concept_id, sess.current_problem_id)

    attempt = (
        (
            await db.execute(
                select(ProblemAttempt)
                .where(ProblemAttempt.session_id == session_id)
                .where(ProblemAttempt.problem_id == problem.id)
                .order_by(ProblemAttempt.id.desc())
            )
        )
        .scalars()
        .first()
    )
    if attempt is None:
        raise RuntimeError(f"no ProblemAttempt for session {session_id} / problem {problem.id}")

    # Read the student graph before freezing. Degraded mode falls back to an
    # empty graph; downstream transcript-grader branches check `kg_degraded`
    # so an unavailable KG is never silently graded as a false F.
    kg_degraded = False
    try:
        pre_freeze_graph = await store.read_graph(attempt_id=attempt.id)
    except KG_DEGRADED_ERRORS as exc:
        kg_degraded = True
        pre_freeze_graph = KGGraph()
        _LOG.warning(
            "apollo_neo4j_degraded stage=pre_freeze_graph attempt_id=%s error=%s",
            attempt.id, exc,
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

    use_transcript_grader = transcript_grader_enabled()
    transcript_grader_failure: str | None = None
    if use_transcript_grader:
        try:
            transcript = await _full_transcript(db, attempt_id=int(attempt.id))
            coverage = await compute_transcript_coverage(
                transcript=transcript,
                reference_graph=reference_graph,
                problem=problem,
            )
        except CoverageGradingError as exc:
            # A transcript-grader failure must never cost the student their
            # Done: fall back to the legacy lane and say so in provenance.
            transcript_grader_failure = str(exc)
            use_transcript_grader = False
            _LOG.exception(
                "apollo_transcript_grader_fallback attempt_id=%s", int(attempt.id)
            )
    if not use_transcript_grader:
        if kg_degraded:
            # Neo4j is down AND the transcript grader is off/failed: grading
            # `compute_coverage` against the empty `student_graph` above would
            # silently produce a false F (every reference node reads as
            # "missing"). Raise a NAMED, retryable error instead — the
            # existing CoverageGradingError -> 503 handler tells the student
            # "try again" rather than serving a fabricated zero grade.
            raise CoverageGradingError(
                stage="kg_unavailable_fallback",
                last_error="Neo4j unavailable and no transcript grader result",
            )
        coverage = await compute_coverage(student_graph, reference_graph)

    # Class 2 Phase 2 (P2.8): pull per-attempt misconception signals from
    # apollo_messages.metadata and reduce them to the per-bank-code score
    # map the rubric expects. The axis enters at 5% taken from the
    # existing 60/25/15. When no misconceptions fired, the dict is empty
    # and the rubric is byte-identical to its pre-P2.8 output.
    misconception_scores = await _attempt_misconception_scores(
        db,
        attempt_id=attempt.id,
    )
    # T-W5a (P4) — grader positive-focus. Default OFF: byte-identical, every
    # detected code (resolved 1.0 or unresolved 0.5) enters the axis exactly
    # as before. When ON, drop unresolved (0.5) contributions before the
    # axis is computed — the "you corrected it" resolved credit (1.0) still
    # counts, but an unresolved misconception no longer drags `overall` down
    # relative to the axis being absent (credit-only, memo §3).
    if grader_positive_focus_enabled():
        misconception_scores = {
            code: score for code, score in misconception_scores.items() if score >= 1.0
        }
    rubric = compute_rubric(
        coverage,
        reference_graph.nodes,
        misconception_scores=misconception_scores,
    )

    # T13 — the DEFAULT-OFF parallel misconception-detection stage. Runs after
    # the rubric so it can dock its LIVE score, and before the diagnostic so
    # the narrative already reflects the penalized band. When
    # `detector_enabled()` is OFF (the only prod state today) this whole block
    # is skipped and `handle_done` is byte-identical to pre-T13 (design
    # invariant #1). SOFT-FAIL (invariant #5): ANY exception in the
    # detect -> gate -> merge chain is logged and swallowed — the grade then
    # proceeds with the UNPENALIZED rubric and a `None` outcome, served HTTP
    # 200. `detection_outcome` (the MergeOutcome) is threaded into
    # `write_artifacts` below so the artifact's `misconception_penalty` /
    # `misconceptions[]` are populated and the emergent ledger feed picks them
    # up. The reassigned `rubric` (a NEW dict from `rubric_overall_after_penalty`
    # — never a mutation of the original) flows into `xp_earned`, the
    # diagnostic, `student_response["rubric"]`, and `attempt.diagnostic_report`,
    # moving the real band + XP a student sees.
    detection_outcome: MergeOutcome | None = None
    # Topic-score (2026-07-10 spec §2/§3) reuses the detector's own centrality
    # computation when the detector stage ran, rather than paying for it
    # twice. `None` here means "not computed yet" — `_compute_topic_score_safe`
    # (called after this block, flag-independent) recomputes it fresh when
    # the detector never ran (flag off, or the detector's own soft-fail fired
    # before reaching the centrality line below).
    centrality_for_topic_score: dict[str, float] | None = None
    if detector_enabled():
        try:
            utterances = await _student_utterances(db, attempt_id=attempt.id)
            detection = await detect_misconceptions(
                db,
                attempt_id=attempt.id,
                concept_id=sess.concept_id,
                student_graph=student_graph,
                reference_graph=reference_graph,
                problem_text=problem.problem_text,
                student_utterances=utterances,
                judge_fn=_default_judge_fn(),
                embed_fn=_default_embed_fn,
            )
            # F-struct (structural co-key) — DEFAULT OFF sub-flag, independent
            # of `detector_enabled()`. When OFF, `opposes_index` stays `{}` and
            # `gate_findings`/`trace_attempt` see the exact same empty map they
            # always defaulted to — byte-identical. When ON, resolve the
            # concept's misconception bank's `opposes` (an `entity_key`, F-struct
            # migration 038) against `reference_graph` node `entity_key`s into a
            # `node_id -> bank_code` map, so a judge finding that localizes an
            # error (`wrong`/`misconception`, no named code) to a node the GRAPH
            # itself names via `opposes` can still dock (gate.py's structural
            # co-key branch, row3s_struct_cokey_dock).
            opposes_index: dict[str, str] = {}
            if struct_cokey_enabled():
                from apollo.overseer.misconception_detector.opposes_index import (
                    build_opposes_index,
                )

                bank_entries = await _load_bank_entries(
                    db,
                    concept_id=sess.concept_id,
                )
                opposes_index = build_opposes_index(reference_graph, bank_entries)
            gated = gate_findings(detection.per_concept, opposes_index=opposes_index)
            centrality = compute_centrality(reference_graph)
            centrality_for_topic_score = centrality
            detection_outcome = merge_detections(gated, centrality=centrality)
            # T-W5a (P1) — grader positive-focus (2026-07-10 design memo).
            # Default OFF: byte-identical, the served rubric band is docked
            # exactly as before. When ON, this dock is skipped — the served
            # rubric/band/XP become credit-only, while `detection_outcome`
            # is STILL threaded to `write_artifacts` below unchanged, so the
            # composite dock (P2, `artifact_build.py`, UNCONDITIONAL — the
            # single retained penalty channel) and the feedback record
            # (`misconceptions[]`) both retain full fidelity.
            if not grader_positive_focus_enabled():
                rubric = rubric_overall_after_penalty(rubric, detection_outcome)
            # Phase-1 diagnostic trace (default OFF, APOLLO_MISC_TRACE). When
            # OFF this branch never imports `trace` — flag-OFF is byte-identical.
            # Instrumentation only: it re-derives per-node judge/gate rows from
            # the artifacts just produced and never touches the grade above. A
            # live grade has no persona/control label, so `is_control=False` and
            # `final_band` is the (letter) rubric band — the labeled false-Strong
            # roll-up is the campaign harness's job (it has the scorecard band).
            # Isolated in its OWN try/except so a trace defect can never perturb
            # the already-computed rubric/outcome (instrumentation must not
            # change the grade, even by soft-failing it) — the grade proceeds
            # penalized-as-computed and only the trace is skipped.
            if trace_enabled():
                try:
                    from apollo.overseer.misconception_detector.trace import (
                        trace_attempt,
                    )

                    trace_attempt(
                        attempt_id=int(attempt.id),
                        reference_graph=reference_graph,
                        detection=detection,
                        gated=gated,
                        outcome=detection_outcome,
                        centrality=centrality,
                        final_band=rubric["overall"].get("letter"),
                        is_control=False,
                        opposes_index=opposes_index,
                    )
                except Exception:
                    _LOG.exception("misconception_trace_failed attempt_id=%s", int(attempt.id))
            # Emergent misconception map — capture seam 1: detector-unkeyed
            # birth (2026-07-10 design §5.3.1, plan Wave 2 T2). Independently
            # flag-gated from `detector_enabled()` (that flag only gates
            # whether the detector STAGE runs at all; this flag gates only
            # whether a birth observation is WRITTEN once it does). When OFF
            # (the only prod state today) this branch never runs — no
            # collector call, no store write, byte-identical. `collect_
            # unkeyed_births` replays the gate's own row7/row8 unkeyed
            # predicate (pure, no IO) over the SAME `detection.per_concept`
            # + `opposes_index` the gate just consumed; `record_detector_
            # births` resolves each birth's `concept_key` (a reference-graph
            # node_id) to its `entity_key` via a map built from `reference_
            # graph.nodes` (the inverse of opposes_index.py's
            # `key_to_node_id` — ConceptFinding carries no entity_key of its
            # own, design correction #2) and appends the observation.
            # OWN failure domain (own try/except -> log + rollback, own
            # commit on success — artifact_writer.py:236-256 pattern): a
            # capture-write failure must NEVER affect the returned grade,
            # which is already fully computed above this point.
            if emergent_map_capture_enabled():
                try:
                    from apollo.overseer.misconception_detector.gate import (
                        collect_unkeyed_births,
                    )

                    node_entity_key = {
                        n.node_id: n.entity_key for n in reference_graph.nodes if n.entity_key
                    }
                    births = collect_unkeyed_births(
                        detection.per_concept, opposes_index=opposes_index
                    )
                    await record_detector_births(
                        db,
                        search_space_id=int(sess.search_space_id),
                        concept_id=sess.concept_id,
                        user_id=str(sess.user_id),
                        attempt_id=int(attempt.id),
                        births=births,
                        node_entity_key=node_entity_key,
                    )
                    # T7 (plan Wave 3, spec §5.5 Q3): eager tau_project
                    # materialization, INSIDE this same failure domain, right
                    # after the observation write succeeds and before the
                    # commit. One signature per birth's resolved entity_key —
                    # a no-op below TAU_PROJECT, idempotent at/above it. `neo`
                    # is the handler's own client (already threaded in) — the
                    # :Canon map materializes eagerly from this seam.
                    for entity_key in {
                        node_entity_key[b.concept_key]
                        for b in births
                        if b.concept_key in node_entity_key
                    }:
                        await materialize_if_promotable(
                            db,
                            neo,
                            search_space_id=int(sess.search_space_id),
                            concept_id=sess.concept_id,
                            signature=f"emergent.{entity_key}",
                            opposes_entity_key=entity_key,
                        )
                    await db.commit()
                except Exception:
                    _LOG.exception("emergent_birth_capture_failed attempt_id=%s", int(attempt.id))
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover - defensive
                        _LOG.exception(
                            "emergent_birth_capture_rollback_failed attempt_id=%s",
                            int(attempt.id),
                        )
        except Exception:
            _LOG.exception("misconception_detector_failed attempt_id=%s", int(attempt.id))
            detection_outcome = None  # soft-fail: grade proceeds unpenalized

    # Topic-score (2026-07-10 spec §2/§3) — COMPUTED ALWAYS, flag-independent:
    # the canonical artifact (below) gets `scores.topic_score` telemetry
    # before `APOLLO_TOPIC_SCORE_SERVED` is ever flipped. Soft-fail contract:
    # `_compute_topic_score_safe` never raises — `topic_score` is `None` on
    # any failure, and every downstream use below is guarded on that.
    topic_score: TopicScoreResult | None = _compute_topic_score_safe(
        coverage=coverage,
        reference_graph=reference_graph,
        centrality=centrality_for_topic_score,
        detection_outcome=detection_outcome,
        attempt_id=int(attempt.id),
    )

    # Serving (spec §3): under the flag, `served_rubric` REPLACES `overall`
    # with the topic score/letter while every legacy axis block is carried
    # over UNCHANGED (mid-deploy safety for older UI clients). This builds a
    # NEW dict — `rubric` itself (the object `attempt.diagnostic_report` and
    # `write_artifacts` below both still receive) is never mutated. Flag off,
    # or a soft-failed `topic_score`, leaves `served_rubric is rubric`
    # (byte-identical downstream).
    serve_topic_score = topic_score is not None and topic_score_served_enabled()
    if serve_topic_score:
        served_rubric = {
            **rubric,
            "overall": {"score": topic_score.score, "letter": topic_score.letter},
        }
    else:
        served_rubric = rubric

    diagnostic_narrative = generate_diagnostic(
        coverage=coverage,
        reference_steps=[s.model_dump() for s in problem.reference_solution],
        problem_text=problem.problem_text,
        rubric=rubric,
        topic_score=topic_score,
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

    # XP ordering (spec §3: "XP continues to derive from rubric.overall (now
    # the topic score)"): `served_rubric` is already the REPLACED overall by
    # this point, so under the flag XP is earned against the topic score, not
    # the axis blend — this line MUST stay after the `served_rubric`
    # assignment above and before any use of `xp_earned`.
    xp_earned = compute_xp_earned(
        overall_score=served_rubric["overall"]["score"],
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
    #
    # Degraded-mode relaxation (NEO4J-DEGRADED, deliberate NO-FALLBACK carve-
    # out — documented in the owner doc): catch (KGUnavailableError,
    # RetentionError) UNCONDITIONALLY, not just when kg_degraded — the real
    # failure mode is a connection dying DURING the ~4-minute grading
    # pipeline, so a HEALTHY read at the top of this function does not
    # guarantee a healthy stamp at the end. RetentionError has no registered
    # HTTP handler today, so letting it propagate 500s a fully successful,
    # already-committed grade; log-and-continue instead.
    done_ts = datetime.now(UTC)
    try:
        await store.stamp_graded_at(attempt_id=attempt.id, ts=done_ts)
    except (KGUnavailableError, RetentionError) as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=stamp_graded_at attempt_id=%s error=%s",
            attempt.id, exc,
        )

    # The student-facing payload is constructed from OLD-path values ONLY,
    # EXCEPT for `rubric`, which is `served_rubric` — byte-identical to
    # `rubric` (same object) unless `APOLLO_TOPIC_SCORE_SERVED` is on AND
    # `topic_score` computed successfully (spec §3). It is otherwise
    # byte-identical whether the shadow flag is on or off and whether the
    # shadow chain succeeds — the shadow result is NEVER merged into it
    # (WU-4C1).
    student_response = {
        "rubric": served_rubric,
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
    # Spec §3: `student_response["topics"]` is served ONLY under the flag +
    # a successfully-computed topic_score — same shape as the artifact's
    # `scores.topic_score.topics` (serialize_topics is the single shared
    # serializer, `topic_score_serialize.py`). Absent (not null) otherwise.
    if serve_topic_score:
        student_response["topics"] = serialize_topics(topic_score)

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
    if _graph_sim_shadow_enabled() and (kg_degraded or neo is None):
        # Degraded mode: Neo4j is down (either the pre-freeze read already
        # failed, or the client never constructed at all). The shadow chain
        # is Neo4j-native end to end (build_rerun_inputs/run_graph_simulation
        # both read/write the graph) — skip it entirely rather than let it
        # crash mid-chain, and stamp the SAME shadow-failure marker the
        # mid-chain isolation branch below uses, so paired analysis sees a
        # consistent reason for the missing `pair` row.
        _LOG.warning(
            "apollo_neo4j_degraded stage=shadow_chain_skipped attempt_id=%s session_id=%s",
            int(attempt.id), int(session_id),
        )
        graph_failure = f"{_SHADOW_FAILURE_MARKER}neo4j_unavailable"
    elif _graph_sim_shadow_enabled():
        # Reached only when NOT (kg_degraded or neo is None) — see the
        # sibling `if` above — so `neo` is a healthy client here. Bind a
        # locally-narrowed name: mypy cannot narrow `Neo4jClient | None`
        # across the `if`/`elif` split of a compound `or` condition.
        assert neo is not None
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
                db,
                neo,
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
                _LOG.exception("apollo_graph_grader_live_failure attempt_id=%s", int(attempt.id))
                graph_failure = repr(e)[:500]
                shadow = None
                served_grade = GRADER_USED_LLM_FALLBACK
            else:
                # SHADOW mode's CONTRACTUAL typed failures keep propagating
                # unchanged: the route maps each to a deliberate NON-500
                # response (503/503/422/409, see `_SHADOW_PROPAGATE_ERRORS`)
                # with pinned commit semantics — pending set+committed for the
                # retryable 503s, no pending for the two validator errors. The
                # student-facing message on the 503s already reads "your grade
                # is saved" (the OLD grade is durable), so surfacing them is
                # the contract, not a bug.
                if isinstance(e, _SHADOW_PROPAGATE_ERRORS):
                    raise
                # Lane B1 / G3 — SHADOW-mode crash isolation for everything
                # UNEXPECTED. During calibration staging/prod run SHADOW on but
                # LIVE off, so the shadow chain computes a comparison run
                # ALONGSIDE the student grade without promoting it. Pre-G3 an
                # unexpected exception here re-raised as a raw 500, which killed
                # the Done request and cost the student their ALREADY-COMMITTED
                # LLM grade — a shadow bug voiding the live answer. That is
                # wrong: the shadow is telemetry, not the grade. So mirror the
                # LIVE fallback's posture WITHOUT promoting anything — log with
                # full context, stamp a shadow-failure marker on the canonical
                # LLM artifact (so paired analysis sees the missing `pair` row
                # and WHY), drop the shadow result, and serve the OLD/LLM values
                # BYTE-IDENTICAL to a shadow-off Done-click. No re-raise → HTTP
                # 200 with the LLM grade. (The shadow chain's OWN internal
                # NO-FALLBACK bookkeeping — `learner_update_pending` set +
                # committed on a cross-store failure — has already run before
                # the error reached here; we only stop it from reaching the
                # HTTP layer.)
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
            detection_outcome=detection_outcome,
            topic_score=topic_score,
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

    # Provenance must report what was ACTUALLY served: the GRAPH_GRADER_LIVE
    # promotion branch can replace the served rubric even when the transcript
    # grader fed the legacy lane, and the topic/dock payload (which quotes the
    # student) may only ship when APOLLO_TOPIC_SCORE_SERVED allows topics into
    # the response at all — provenance must not bypass that serve gate.
    transcript_served = use_transcript_grader and served_grade != GRADER_USED_GRAPH
    serialized_topics = (
        serialize_topics(topic_score)
        if topic_score is not None and serve_topic_score
        else []
    )
    student_response["grading_provenance"] = {
        "grader_used": "llm_transcript" if transcript_served else served_grade,
        "evidence_source": "transcript" if transcript_served else "graph_nodes",
        "transcript_grader_failure": transcript_grader_failure,
        "score_before_dock": (
            topic_score.coverage_component if topic_score is not None else None
        ),
        "topics": serialized_topics,
        "docks": [
            {
                "key": misconception["canonical_key"],
                "points": misconception["dock_points"],
                "evidence_span": misconception["evidence_span"],
                "resolved": misconception["resolved"],
            }
            for topic in serialized_topics
            for misconception in topic["misconceptions"]
        ],
        "graph_lane": (
            {
                "abstained": shadow.audited.abstained,
                "reasons": list(shadow.audited.abstention_reasons),
            }
            if shadow is not None
            else None
        ),
    }

    return student_response
