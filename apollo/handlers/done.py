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

from apollo.errors import (
    CoverageGradingError,
    KGUnavailableError,
    RetentionError,
)
from apollo.grading.artifact_build import GRADER_USED_LLM_FALLBACK
from apollo.handlers.artifact_writer import write_artifacts
from apollo.knowledge_graph.store import KGStore
from apollo.ontology import KGGraph
from apollo.overseer.coverage import compute_coverage
from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.grading_flags import (
    topic_score_served_enabled,
    transcript_grader_enabled,
)
from apollo.overseer.misconception import (
    MisconceptionSignal,
    summarize_for_rubric,
)
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.overseer.rubric import compute_rubric
from apollo.overseer.topic_score import TopicScoreResult, compute_centrality, compute_topic_score
from apollo.overseer.topic_score_serialize import serialize_topics
from apollo.overseer.transcript_coverage import compute_transcript_coverage_with_spans
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

# WU-5A2 — the Layer-3 belief-PERSIST flag (default OFF EVERYWHERE incl. prod +
# staging). A7 removed its former Done-time producer; the helper remains only
# for compatibility with the artifact-derived mastery interlock.
_GRAPH_SIM_LAYER3_FLAG: str = "APOLLO_GRAPH_SIM_LAYER3_ENABLED"

# Campaign-plan Task A3 — the canonical-grading-artifact PERSIST flag (default
# OFF everywhere). When OFF, `write_artifacts` is never called and `handle_done`
# writes no `apollo_grading_artifacts` rows (byte-identical to pre-A3). When ON,
# ONE canonical row is written every Done-click (`grader_used="llm_fallback"` —
# the transcript/topic grade), so campaign runs have a durable grading record.
_GRAPH_SIM_ARTIFACT_FLAG: str = "APOLLO_GRADING_ARTIFACT_ENABLED"

# T13 — the raw student-turn role for `_student_utterances` (R6, RESOLVED): live
# transcript roles are exactly {"apollo", "student"}; the Apollo learner turns
# (read by `_attempt_misconception_scores`) are "apollo", the student's raw
# teaching utterances (which feed the bank_pattern tier) are "student".
_STUDENT_ROLE: str = "student"

def _graph_sim_layer3_enabled() -> bool:
    return os.environ.get(_GRAPH_SIM_LAYER3_FLAG, "").lower() in ("1", "true", "yes")


def _grading_artifact_enabled() -> bool:
    return os.environ.get(_GRAPH_SIM_ARTIFACT_FLAG, "").lower() in ("1", "true", "yes")


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


def _compute_topic_score_safe(
    *,
    coverage: dict,
    reference_graph: KGGraph,
    attempt_id: int,
    evidence_spans: dict[str, str] | None = None,
) -> TopicScoreResult | None:
    """Soft-failing wrapper around ``compute_topic_score`` (2026-07-10 spec
    §3): computed ALWAYS (flag-independent — the artifact gets telemetry
    before any serving flip), but any exception here must never break a Done.
    Centrality is computed from the reference graph. Any exception here
    is logged and swallowed — the caller receives ``None`` and proceeds with
    ``topic_score`` absent from both the artifact and the served payload."""
    try:
        return compute_topic_score(
            coverage=coverage,
            reference_nodes=reference_graph.nodes,
            centrality=compute_centrality(reference_graph),
            evidence_spans=evidence_spans,
        )
    except Exception:
        _LOG.exception("topic_score_computation_failed attempt_id=%s", attempt_id)
        return None


async def _find_problem(db: AsyncSession, concept_id: int, problem_code: str) -> Problem:
    for p in await list_problems_for_concept(db, concept_id=concept_id):
        if p.id == problem_code:
            return p
    raise RuntimeError(f"problem {problem_code!r} not in bank for cluster {concept_id!r}")


async def _fetch_attempt_transcript(
    db: AsyncSession, attempt_id: int
) -> list[dict[str, Any]]:
    """Return the graded attempt's ordered chat turns for report display."""
    try:
        messages = (
            (
                await db.execute(
                    select(Message)
                    .where(Message.attempt_id == attempt_id)
                    .where(Message.role.in_(("student", "apollo")))
                    .order_by(Message.turn_index)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "role": message.role,
                "content": message.content,
                "turn_index": message.turn_index,
            }
            for message in messages
        ]
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "transcript fetch soft-fail for attempt %s: %s",
            attempt_id,
            exc,
            exc_info=True,
        )
        return []


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
    # Per-attempt student quotes for the diagnostic narrative (transcript lane
    # only — the KG lane has no per-node quotes, so this stays empty there).
    # Verbatim-gated in `narrative_evidence_spans`, so the narrative can only
    # ever attribute to the student words they typed THIS attempt.
    narrative_spans: dict[str, str] = {}
    if use_transcript_grader:
        try:
            transcript = await _full_transcript(db, attempt_id=int(attempt.id))
            coverage, narrative_spans = await compute_transcript_coverage_with_spans(
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
    rubric = compute_rubric(
        coverage,
        reference_graph.nodes,
        misconception_scores=misconception_scores,
    )

    # Topic-score (2026-07-10 spec §2/§3) — COMPUTED ALWAYS, flag-independent:
    # the canonical artifact (below) gets `scores.topic_score` telemetry
    # before `APOLLO_TOPIC_SCORE_SERVED` is ever flipped. Soft-fail contract:
    # `_compute_topic_score_safe` never raises — `topic_score` is `None` on
    # any failure, and every downstream use below is guarded on that.
    topic_score: TopicScoreResult | None = _compute_topic_score_safe(
        coverage=coverage,
        reference_graph=reference_graph,
        attempt_id=int(attempt.id),
        evidence_spans=narrative_spans,
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

    # Narrative grounding (2026-07-14): feed the narrator the verbatim student
    # transcript so credit statements quote what the student actually said
    # instead of expanding topic names into claims they never made. Best-effort:
    # a fetch failure logs and degrades to the ungrounded prompt — it must
    # never block grading.
    try:
        narrative_utterances: tuple[str, ...] = await _student_utterances(
            db, attempt_id=attempt.id
        )
    except Exception:  # noqa: BLE001
        _LOG.warning(
            "apollo_narrative_utterances_fetch_failed attempt_id=%s", attempt.id
        )
        narrative_utterances = ()

    diagnostic_narrative = generate_diagnostic(
        coverage=coverage,
        reference_steps=[s.model_dump() for s in problem.reference_solution],
        problem_text=problem.problem_text,
        rubric=rubric,
        topic_score=topic_score,
        student_utterances=narrative_utterances,
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

    student_response["transcript"] = await _fetch_attempt_transcript(db, int(attempt.id))

    # Canonical transcript/topic artifact capture (default OFF). Task B1 —
    # student scorecard projection (spec §2). Additive
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
            coverage=coverage,
            rubric=rubric,
            latency_ms=artifact_latency_ms,
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

    # The topic/dock payload (which quotes the student) may only ship when
    # APOLLO_TOPIC_SCORE_SERVED allows topics into the response at all.
    transcript_served = use_transcript_grader
    serialized_topics = (
        serialize_topics(topic_score)
        if topic_score is not None and serve_topic_score
        else []
    )
    student_response["grading_provenance"] = {
        "grader_used": (
            "llm_transcript" if transcript_served else GRADER_USED_LLM_FALLBACK
        ),
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
        "graph_lane": None,
    }

    return student_response
