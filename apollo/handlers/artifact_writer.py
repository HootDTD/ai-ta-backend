"""Persist the canonical transcript/topic grading artifact for a Done click."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.grading.artifact_build import build_llm_artifact
from apollo.overseer.topic_score import TopicScoreResult
from apollo.persistence.models import TutoringSession, GradingArtifact, ProblemAttempt

_LOG = logging.getLogger(__name__)


def _artifact_row(
    *, attempt: ProblemAttempt, sess: TutoringSession, payload: dict
) -> GradingArtifact:
    return GradingArtifact(
        attempt_id=int(attempt.id),
        role="canonical",
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
