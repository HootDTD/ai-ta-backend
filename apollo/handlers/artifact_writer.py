"""Persist the canonical transcript/topic grading artifact for a Done click."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.grading.artifact_build import build_llm_artifact
from apollo.overseer.topic_score import TopicScoreResult
from apollo.persistence.models import GradingRun, ProblemAttempt, TutoringSession

_LOG = logging.getLogger(__name__)


def _artifact_row(
    *, attempt: ProblemAttempt, sess: TutoringSession, payload: dict
) -> GradingRun:
    """Map the LLM-fallback builder's payload dict
    (``apollo.grading.artifact_build.build_llm_artifact``) onto
    ``internal.grading_runs`` columns (DB-14/A7 artifacts-only merge — see
    ``GradingRun``'s docstring for the full column mapping). ``versions``/
    ``scores``/``abstention`` are stored whole in their ``*_details`` JSONB
    columns AND have their query-friendly scalars lifted into typed columns;
    ``misconceptions``/``clarification_trace`` have no dedicated columns in
    the target DDL, so they nest under ``grader_payload``."""
    versions = payload["versions"]
    scores = payload["scores"]
    abstention = payload["abstention"]
    return GradingRun(
        attempt_id=int(attempt.id),
        role="canonical",
        grader_used=payload["grader_used"],
        grader_version=versions["grader"],
        reference_graph_hash=versions.get("reference_graph_hash"),
        user_id=str(sess.user_id),
        search_space_id=int(sess.search_space_id),
        concept_id=sess.concept_id,
        problem_id=int(attempt.problem_id),
        version_details=versions,
        node_ledger=payload["node_ledger"],
        edge_ledger=payload["edge_ledger"],
        score_details=scores,
        composite_score=scores.get("composite"),
        node_coverage_score=scores.get("node_coverage"),
        abstained=bool(abstention.get("abstained") or False),
        abstention_details=abstention,
        grader_payload={
            "misconceptions": payload["misconceptions"],
            "clarification_trace": payload["clarification_trace"],
        },
        grading_latency_ms=payload["grading_latency_ms"],
    )


async def write_artifacts(
    db: AsyncSession,
    *,
    attempt: ProblemAttempt,
    sess: TutoringSession,
    coverage: dict,
    rubric: dict,
    latency_ms: int | None,
    topic_score: TopicScoreResult | None = None,
) -> dict | None:
    """Write one canonical artifact without affecting the served grade on failure."""
    try:
        payload = build_llm_artifact(
            coverage=coverage,
            rubric=rubric,
            latency_ms=latency_ms,
            clarification_trace=[],
            topic_score=topic_score,
        )
        db.add(_artifact_row(attempt=attempt, sess=sess, payload=payload))
        await db.flush()
        await db.commit()
        return payload
    except Exception:
        _LOG.exception("artifact_write_failed attempt_id=%s", int(attempt.id))
        try:
            await db.rollback()
        except Exception:  # pragma: no cover - defensive
            _LOG.exception("artifact_write_rollback_failed attempt_id=%s", int(attempt.id))
        return None
