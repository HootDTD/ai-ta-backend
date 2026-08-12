"""Attempt-history queries used by the XP awarder.

A 're-attempt' for XP purposes is any Done event on a problem the user
has previously been graded on — across all their sessions. We detect it
by looking for any other ProblemAttempt row (joined through TutoringSession
to the user_id) for the same problem_id whose `result` is a graded
terminal value (`GRADED_ATTEMPT_RESULTS`: the legacy solver outcomes plus
`graded`, the current diff+rubric outcome) —
`abandoned` is excluded because it represents a mid-problem switch, not a
completed grading. The current attempt id is excluded so a within-session
retry — which overwrites the same row after Phase 1's /retry endpoint —
is handled upstream by checking the attempt's own `result` before this call.

The second reader here — `prior_wrongness_findings` (Apollo P3.2 §2.3 L2c) —
answers a different question over the same (user, problem) scope: what did this
student already get CHALLENGED on, in earlier attempts at this problem? It reads
the append-only `internal.grading_runs` artifact, so it needs no table of its own
and specifically must never revive the retired
`apollo_misconception_observations` table (frozen legacy chain)."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import (
    GRADED_ATTEMPT_RESULTS,
    ProblemAttempt,
)

_LOG = logging.getLogger(__name__)

# Newest-first cap on carried prior findings. The CONSUMER caps harder still
# (decision D4: at most ONE carried challenge per attempt); this is only the
# read window, sized so a heavy re-roller's history stays bounded.
_PRIOR_FINDINGS_LIMIT = 8


async def has_prior_graded_attempt(
    *,
    db: AsyncSession,
    user_id: str,
    course_id: int,
    problem_id: int,
    exclude_attempt_id: int,
) -> bool:
    """True iff another graded ProblemAttempt exists for this (user, problem)."""
    stmt = (
        select(func.count())
        .select_from(ProblemAttempt)
        .where(
            ProblemAttempt.user_id == user_id,
            ProblemAttempt.course_id == course_id,
            ProblemAttempt.problem_id == problem_id,
            ProblemAttempt.result.in_(GRADED_ATTEMPT_RESULTS),
            ProblemAttempt.id != exclude_attempt_id,
        )
    )
    count = (await db.execute(stmt)).scalar_one()
    return count > 0


# One query, bound parameters only. The JOIN resolves the CURRENT attempt's
# user_id (the caller has an attempt_id, not a user_id) and pins the artifact
# rows to that same user; the LATERAL unrolls the artifact's `misconceptions`
# array the same way `classroom.top_misconceptions` already does.
#
# `grader_payload` is free-form JSONB, so the array expression is guarded by
# `jsonb_typeof` INSIDE the LATERAL: a payload whose `misconceptions` is an
# object (or absent) yields zero rows instead of raising
# "cannot extract elements from an object" mid-Done.
#
# INDEX NOTE: `internal.grading_runs` holds 159 rows today, so this is a seq
# scan and that is fine. Once it passes a few thousand rows, add
# `(user_id, problem_id)` (or `(user_id, problem_id, role)`) — the WHERE clause
# below is exactly that prefix.
_PRIOR_WRONGNESS_FINDINGS_SQL = text(
    """
    SELECT
        misc ->> 'canonical_key'  AS canonical_key,
        misc ->> 'evidence_span'  AS evidence_span,
        misc ->> 'resolved'       AS resolved,
        g.attempt_id              AS attempt_id
    FROM internal.grading_runs g
    JOIN app.problem_attempts cur
      ON cur.id = :attempt_id
     AND cur.user_id = g.user_id
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE
            WHEN jsonb_typeof(g.grader_payload -> 'misconceptions') = 'array'
            THEN g.grader_payload -> 'misconceptions'
            ELSE '[]'::jsonb
        END
    ) AS misc
    WHERE g.role = 'canonical'
      AND g.problem_id = :problem_id
      AND g.course_id = :course_id
      AND g.attempt_id <> :attempt_id
      AND misc ->> 'canonical_key' IS NOT NULL
    ORDER BY g.created_at DESC, g.id DESC
    LIMIT :limit
    """
)


async def prior_wrongness_findings(
    db: AsyncSession,
    *,
    attempt_id: int,
    problem_id: int,
    course_id: int,
    limit: int = _PRIOR_FINDINGS_LIMIT,
) -> tuple[dict[str, Any], ...]:
    """Findings this SAME user recorded on this SAME problem in EARLIER attempts.

    Apollo P3.2 §2.3 L2c / decision D4 — **carry the question, never the
    punishment**. Best-grade-wins retries defeat any at-Done penalty (a student
    re-rolling an identical transcript eventually draws an A from grader noise
    alone), so the only durable answer is to carry the student's own prior claim
    forward as a QUESTION inside the current attempt, where the consequence is
    earned. Also used to dedup the decision-7 XP bonus once per
    (user × problem × node).

    Returns ``({'canonical_key', 'evidence_span', 'resolved', 'attempt_id'}, ...)``
    newest-first — the exact keys ``artifact_build`` writes and
    ``classroom.top_misconceptions`` reads. ``problem_id`` must be the durable
    ``app.problems.id`` (what ``GradingRun.problem_id`` stores), not a public
    problem code.

    **Its own failure domain.** Any exception logs
    ``apollo_prior_findings_failed`` and returns ``()``: a missing memory costs
    one continuity question, while a raise here would reach the Done grade path.
    """
    try:
        rows = (
            (
                await db.execute(
                    _PRIOR_WRONGNESS_FINDINGS_SQL,
                    {
                        "attempt_id": attempt_id,
                        "problem_id": problem_id,
                        "course_id": course_id,
                        "limit": max(0, int(limit)),
                    },
                )
            )
            .mappings()
            .all()
        )
    except Exception as exc:  # a lost memory must never cost the student a grade
        _LOG.warning(
            "apollo_prior_findings_failed attempt_id=%s problem_id=%s err=%s",
            attempt_id,
            problem_id,
            exc,
        )
        return ()

    return tuple(
        {
            "canonical_key": str(row["canonical_key"]),
            "evidence_span": row["evidence_span"]
            if isinstance(row["evidence_span"], str)
            else None,
            "resolved": row["resolved"] == "true",
            "attempt_id": int(row["attempt_id"]),
        }
        for row in rows
    )
