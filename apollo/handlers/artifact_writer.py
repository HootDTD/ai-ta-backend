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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.clarification.candidate_assembly import misconception_bank_applicable
from apollo.grading.artifact_build import (
    GRADER_USED_GRAPH,
    GRADER_USED_LLM_FALLBACK,
    build_graph_artifact,
    build_llm_artifact,
)
from apollo.grading.composite import load_weights
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.persistence.models import ApolloSession, Clarification, GradingArtifact, ProblemAttempt

_LOG = logging.getLogger(__name__)

_ROLE_CANONICAL = "canonical"
_ROLE_PAIR = "pair"


async def _load_clarification_trace(db: AsyncSession, *, attempt_id: int) -> list[dict]:
    """The spec §1 clarification-trace block: one row per probed idea for this
    attempt, question + answer + credit outcome. ``credit`` is ``"granted"``
    only for a ``confirmed`` verdict, ``"denied"`` for ``refuted``/``vague``,
    and ``None`` while still ``asked_waiting`` (never resolved before Done)."""
    rows = (
        (
            await db.execute(
                select(Clarification)
                .where(Clarification.attempt_id == attempt_id)
                .order_by(Clarification.asked_turn)
            )
        )
        .scalars()
        .all()
    )

    trace: list[dict] = []
    for row in rows:
        if row.state == "confirmed":
            credit = "granted"
        elif row.state in ("refuted", "vague"):
            credit = "denied"
        else:
            credit = None
        trace.append(
            {
                "node_id": row.node_id,
                "candidate_key": row.candidate_key,
                "probe_question": row.probe_question,
                "original_statement": row.original_statement,
                "clarification_text": row.clarification_text,
                "state": row.state,
                "credit": credit,
            }
        )
    return trace


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

    Own failure domain: ANY exception (payload build, DB write) is logged and
    swallowed. The artifact is telemetry — it must never cost a student their
    already-committed grade."""
    try:
        weights = load_weights()
        clarification_trace = await _load_clarification_trace(db, attempt_id=int(attempt.id))

        graph_payload: dict | None = None
        if shadow is not None:
            graph_payload = build_graph_artifact(
                shadow=shadow,
                weights=weights,
                clarification_trace=clarification_trace,
                latency_ms=latency_ms,
            )

        # Lane B3a/D1 — the empty-bank fact for the LLM artifact's
        # `misconceptions_status` marker. When the shadow chain ran we already
        # have it (`shadow.grade.soundness_applicable`), so reuse it — no extra
        # query. On the shadow-off / LLM-served path (the default build) the
        # fact was never computed, so read it from the SAME source the grading
        # path uses (`load_for_concept`, via `misconception_bank_applicable`).
        if shadow is not None:
            misconceptions_bank_empty = not shadow.grade.soundness_applicable
        else:
            misconceptions_bank_empty = not await misconception_bank_applicable(
                db, concept_id=sess.concept_id
            )

        llm_payload = build_llm_artifact(
            coverage=coverage,
            rubric=rubric,
            weights=weights,
            graph_failure=graph_failure,
            latency_ms=latency_ms,
            misconceptions_bank_empty=misconceptions_bank_empty,
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
