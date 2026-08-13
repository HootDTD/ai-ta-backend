"""Persist the canonical transcript/topic grading artifact for a Done click."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.grading.artifact_build import build_llm_artifact
from apollo.overseer.topic_score import TopicScoreResult
from apollo.persistence.models import GradingRun, ProblemAttempt, TutoringSession

_LOG = logging.getLogger(__name__)


def _artifact_row(
    *,
    attempt: ProblemAttempt,
    sess: TutoringSession,
    payload: dict,
    shadow_misconceptions: Sequence[Mapping[str, Any]] | None = None,
) -> GradingRun:
    """Map the LLM-fallback builder's payload dict
    (``apollo.grading.artifact_build.build_llm_artifact``) onto
    ``internal.grading_runs`` columns (DB-14/A7 artifacts-only merge — see
    ``GradingRun``'s docstring for the full column mapping). ``versions``/
    ``scores``/``abstention`` are stored whole in their ``*_details`` JSONB
    columns AND have their query-friendly scalars lifted into typed columns;
    ``misconceptions``/``clarification_trace`` have no dedicated columns in
    the target DDL, so they nest under ``grader_payload``.

    ``shadow_misconceptions`` (P3.2 §2.3 L1 "produce + persist + shadow-log")
    is the ONLY thing that ever differs between the row written to the database
    and the payload returned to the caller, and it exists because those two have
    genuinely different gates: the served payload is what `render_scorecard`
    turns into the student's *Watch out* list (level >= 3), while the persisted
    column is what the NEXT attempt reads back through
    ``attempt_history.prior_wrongness_findings`` for L2c cross-attempt question
    memory — a level-**2** feature. Persisting only at level 3 would leave L2c
    reading an array nothing had written and silently no-op at the level it
    ships at. ``None`` (levels 0 and >= 3) stores the payload's own array, so
    level 0 is byte-identical and level >= 3 keeps ONE producer.

    Always stored as a LIST: both readers unroll it with
    ``jsonb_array_elements`` and ``prior_wrongness_findings`` guards on
    ``jsonb_typeof(...) = 'array'``, so an object here yields zero rows."""
    versions = payload["versions"]
    scores = payload["scores"]
    abstention = payload["abstention"]
    misconceptions = (
        payload["misconceptions"]
        if shadow_misconceptions is None
        else [dict(entry) for entry in shadow_misconceptions]
    )
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
            "misconceptions": misconceptions,
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
    shadow_misconceptions: Sequence[Mapping[str, Any]] | None = None,
) -> dict | None:
    """Write one canonical artifact without affecting the served grade on failure.

    ``shadow_misconceptions`` overrides ONLY the persisted
    ``grader_payload -> 'misconceptions'`` array (see :func:`_artifact_row`);
    the returned payload — which becomes the student's scorecard — is never
    touched by it."""
    # Captured BEFORE any failure can expire ORM instances: reading
    # `attempt.id` after a failed flush would re-enter the broken session and
    # raise (PendingRollbackError in prod, 2026-08-05) out of the soft-fail path.
    attempt_id = int(attempt.id)
    try:
        payload = build_llm_artifact(
            coverage=coverage,
            rubric=rubric,
            latency_ms=latency_ms,
            clarification_trace=[],
            topic_score=topic_score,
        )
        # The INSERT runs inside a SAVEPOINT: on failure (e.g. a re-clicked
        # Done violating the append-only UNIQUE(attempt_id, role,
        # grader_version)) only the savepoint rolls back and the failed row is
        # expunged. The outer transaction stays healthy and `attempt`/`sess`
        # stay UNEXPIRED — done.py keeps reading them after this returns, and
        # a full rollback here would expire them into lazy-load failures.
        async with db.begin_nested():
            db.add(
                _artifact_row(
                    attempt=attempt,
                    sess=sess,
                    payload=payload,
                    shadow_misconceptions=shadow_misconceptions,
                )
            )
            await db.flush()
    except Exception:
        _LOG.exception("artifact_write_failed attempt_id=%s", attempt_id)
        return None
    try:
        await db.commit()
        return payload
    except Exception:
        _LOG.exception("artifact_commit_failed attempt_id=%s", attempt_id)
        try:
            await db.rollback()
        except Exception:  # pragma: no cover - defensive
            _LOG.exception("artifact_write_rollback_failed attempt_id=%s", attempt_id)
        return None
