"""Campaign-plan Task A3 — the paired canonical-artifact writer in the
Done-click path.

``write_artifacts`` is telemetry, not grading: it runs AFTER the OLD grade
(and, when the shadow chain ran, the shadow run-txn) are already durable, and
it owns its own ``try/except Exception`` — an artifact-write failure is
logged (``artifact_write_failed``) and swallowed, NEVER raised into the Done
response. This is a SOFTER posture than the shadow chain's NO-FALLBACK
contract (``run_graph_simulation``): the artifact is a record of the grade,
not the grade itself.

Row shape: at most two rows per attempt, respecting the
``uq_grading_artifact_attempt_role`` UNIQUE(attempt_id, role):

- ``role="canonical"``: the grade the student was actually served
  (``grader_used=served``).
- ``role="pair"``: the OTHER grader's artifact on the same input, written
  only when both grades exist (i.e. the shadow chain ran and produced a
  result). When ``shadow is None`` (shadow flag off, or a shadow abstention
  routed straight to LLM with no graph result at all) only the canonical row
  is written.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.grading.artifact_build import (
    GRADER_USED_GRAPH,
    GRADER_USED_LLM_FALLBACK,
    build_graph_artifact,
    build_llm_artifact,
)
from apollo.grading.composite import load_weights
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.overseer.topic_score import TopicScoreResult
from apollo.persistence.models import ApolloSession, GradingArtifact, ProblemAttempt

_LOG = logging.getLogger(__name__)

_ROLE_CANONICAL = "canonical"
_ROLE_PAIR = "pair"


def _artifact_row(
    *,
    attempt: ProblemAttempt,
    sess: ApolloSession,
    role: str,
    payload: dict,
) -> GradingArtifact:
    return GradingArtifact(
        attempt_id=int(attempt.id),
        role=role,
        grader_used=payload["grader_used"],
        user_id=str(sess.user_id),
        search_space_id=int(sess.search_space_id),
        concept_id=sess.concept_id,
        problem_id=str(attempt.problem_id),
        versions=payload["versions"],
        node_ledger=payload["node_ledger"],
        edge_ledger=payload["edge_ledger"],
        misconceptions=payload["misconceptions"],
        clarification_trace=payload["clarification_trace"],
        scores=payload["scores"],
        abstention=payload["abstention"],
        grading_latency_ms=payload["grading_latency_ms"],
    )


async def write_artifacts(
    db: AsyncSession,
    *,
    attempt: ProblemAttempt,
    sess: ApolloSession,
    shadow: ShadowGradeResult | None,
    coverage: dict,
    rubric: dict,
    served: str,
    graph_failure: str | None,
    latency_ms: int | None,
    topic_score: TopicScoreResult | None = None,
) -> dict | None:
    """Write the canonical (+ paired, when both grades exist) artifact rows for
    this Done-click. ``served`` is the ``grader_used`` value of the grade
    actually shown to the student (``"graph"`` or ``"llm_fallback"``) — Task
    A4 is the only caller that can pass ``"graph"``; this build always passes
    ``"llm_fallback"``.

    Returns the CANONICAL artifact payload dict (the same shape persisted to
    the ``canonical`` row) on success, so the caller can hand it straight to
    ``apollo.projections.scorecard.render_scorecard`` (Task B1) without a
    round-trip read of what was just written. Returns ``None`` when the write
    failed (own failure domain below) — the caller renders no scorecard in
    that case rather than templating over a payload that never made it to
    disk.

    2026-07-10 topic-score spec §2/§3: ``topic_score`` is a NEW OPT-IN
    keyword, threaded UNCONDITIONALLY (flag-independent — the point is
    telemetry ahead of any serving flip) into BOTH ``build_graph_artifact``
    and ``build_llm_artifact``, so whichever payload becomes ``canonical``
    (or ``pair``) carries the same ``scores.topic_score`` block. ``None``
    (the default, or whenever ``done.py``'s own soft-fail wrapper returned
    ``None``) leaves both builders' ``scores`` byte-identical to today.

    Own failure domain: ANY exception (payload build, DB write) is logged and
    swallowed. The artifact is telemetry — it must never cost a student their
    already-committed grade."""
    try:
        weights = load_weights()
        # Keep the JSON field for historical artifact compatibility. The
        # retired clarification loop contributes no rows to new artifacts.
        clarification_trace: list[dict] = []

        graph_payload: dict | None = None
        if shadow is not None:
            graph_payload = build_graph_artifact(
                shadow=shadow,
                weights=weights,
                clarification_trace=clarification_trace,
                latency_ms=latency_ms,
                topic_score=topic_score,
            )

        llm_payload = build_llm_artifact(
            coverage=coverage,
            rubric=rubric,
            weights=weights,
            graph_failure=graph_failure,
            latency_ms=latency_ms,
            clarification_trace=clarification_trace,
            topic_score=topic_score,
        )

        payloads_by_grader = {
            GRADER_USED_GRAPH: graph_payload,
            GRADER_USED_LLM_FALLBACK: llm_payload,
        }

        canonical_payload = payloads_by_grader.get(served)
        if canonical_payload is None:
            # `served` names a grade that was never computed (defensive —
            # should not happen: A4 only passes "graph" when a shadow result
            # exists). Fall back to the LLM payload so a row still lands.
            _LOG.warning(
                "artifact_writer_served_grade_missing served=%s attempt_id=%s",
                served,
                int(attempt.id),
            )
            served = GRADER_USED_LLM_FALLBACK
            canonical_payload = llm_payload

        rows = [
            _artifact_row(
                attempt=attempt, sess=sess, role=_ROLE_CANONICAL, payload=canonical_payload
            )
        ]
        pair_grader = GRADER_USED_LLM_FALLBACK if served == GRADER_USED_GRAPH else GRADER_USED_GRAPH
        pair_payload = payloads_by_grader.get(pair_grader)
        if pair_payload is not None:
            rows.append(
                _artifact_row(attempt=attempt, sess=sess, role=_ROLE_PAIR, payload=pair_payload)
            )

        db.add_all(rows)
        await db.flush()
        await db.commit()

        return canonical_payload
    except Exception:
        _LOG.exception("artifact_write_failed attempt_id=%s", int(attempt.id))
        # Leave the session usable for any caller code that runs after this
        # (e.g. the WU-5A2 Layer-3 persist, when both flags are on) — a failed
        # flush/commit here must not poison the shared AsyncSession.
        try:
            await db.rollback()
        except Exception:  # pragma: no cover - defensive, rollback itself failing
            _LOG.exception("artifact_write_rollback_failed attempt_id=%s", int(attempt.id))
        return None
