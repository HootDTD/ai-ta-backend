"""WU-5B3a-1 — the learner-update retry janitor DRAIN state machine.

``drain_pending_attempts`` claims one due ``learner_update_pending`` (not-dead)
attempt with ``FOR UPDATE SKIP LOCKED`` + a work-lease (one committed claim-txn),
re-runs the FROZEN ``build_rerun_inputs`` -> ``run_graph_simulation`` ->
(LAYER3-gated) ``run_learner_update`` from durable state in a SEPARATE work
session, and in a THIRD record-txn (re-SELECT by id) clears the flag on success /
dead-letters on a terminal ``LearnerUpdateUnreconstructableError`` / backs off on a
transient failure. It re-implements NO belief fold — it ORCHESTRATES frozen
primitives.

Three short-lived sessions mirror the upload-worker idiom
(``knowledge/teacher_weekly.py``: ``_claim_upload_job_async`` /
``_handle_job_failure_async``):

  * Phase A CLAIM-txn — short, commits immediately; the ``FOR UPDATE`` lock is
    NEVER held across the LLM window. The claim bumps ``learner_update_attempts``
    and writes a ``CLAIM_LEASE_S`` work-lease into ``learner_update_next_attempt_at``
    so a second drainer cannot re-claim the SAME row mid-work.
  * Phase B WORK-session — a fresh session per claimed attempt; it DOES hold a
    connection across the 2 LLM calls (the frozen ``run_graph_simulation`` opens
    its txn before the LLM calls and commits internally). Bounded, minutes-scale,
    recovered by ``pool_pre_ping`` at the record-txn's next checkout. A mid-LLM
    connection drop surfaces to the outer ``except`` -> a clean record-txn backoff.
  * Phase C RECORD-txn — short; RE-SELECT by id (never the work-session object);
    clears ``learner_update_pending`` on success / sets
    ``learner_update_failed_permanently`` on terminal / backs off
    ``learner_update_next_attempt_at`` on transient (dead-letters at
    ``attempts >= MAX_ATTEMPTS``).

The clear-flag lives HERE (not in the frozen ``run_learner_update``) to preserve
the WU-5A2 byte-identity guardrail; a crash between the fold-commit and the clear
can re-increment ``evidence_count`` by at most +1/entity (belief stays correct —
accepted, documented bound). The belief write is interlocked on
``_graph_sim_layer3_enabled()`` (the back-door guard): LAYER3 OFF ->
``run_graph_simulation`` re-runs (supersede-idempotent) but the belief write is
DEFERRED (row left pending, deferral does NOT consume a retry).

NO worker loop / Procfile / opportunistic backstop here — that is WU-5B3b.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import NoResultFound

from apollo.errors import LearnerUpdateUnreconstructableError
from apollo.handlers.done import _graph_sim_layer3_enabled
from apollo.handlers.done_grading import run_graph_simulation
from apollo.handlers.done_inputs import build_rerun_inputs
from apollo.handlers.learner_update import run_learner_update
from apollo.persistence.models import ApolloSession, ProblemAttempt
from database.session import get_async_session

_LOG = logging.getLogger(__name__)

# Length cap on the persisted ``learner_update_last_error`` (mirrors
# ``teacher_weekly._truncate_error`` — re-implemented locally, NOT imported across
# domains).
_MAX_ERROR_LEN = 500


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ORCHESTRATOR-ADJUDICATED constants (split-proposal §3 + adjudication #5),
# env-overridable. Schedule: 300 / 1200 / 3600 (capped).
MAX_ATTEMPTS: int = _int_env("APOLLO_JANITOR_MAX_ATTEMPTS", 3)
BASE_DELAY_S: int = _int_env("APOLLO_JANITOR_BASE_DELAY_S", 300)
BACKOFF_FACTOR: int = _int_env("APOLLO_JANITOR_BACKOFF_FACTOR", 4)
MAX_DELAY_S: int = _int_env("APOLLO_JANITOR_MAX_DELAY_S", 3600)
JITTER: float = _float_env("APOLLO_JANITOR_JITTER", 0.10)
CLAIM_LEASE_S: int = _int_env("APOLLO_JANITOR_CLAIM_LEASE_S", 600)

# Module-level jitter source so tests can monkeypatch a deterministic value
# (``random.uniform`` is also injectable per-call via ``_backoff_delay_s``).
_jitter_fn = random.uniform


@dataclass(frozen=True)
class DrainResult:
    """Immutable per-sweep summary (observability). Counts are over the rows this
    sweep CLAIMED. ``backlog_remaining`` is a post-sweep COUNT of still-pending,
    not-dead rows (served by the migration-028 partial index)."""

    claimed: int
    succeeded: int
    dead_lettered: int
    retried: int            # transient failure -> backoff (not terminal)
    deferred: int           # LAYER3 interlock OFF -> belief write skipped, row left pending
    backlog_remaining: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _truncate(message: str | None) -> str | None:
    if message is None:
        return None
    cleaned = " ".join(str(message).split()).strip()
    return cleaned[:_MAX_ERROR_LEN] if cleaned else None


def _backoff_delay_s(attempts: int, *, jitter_fn=None) -> float:
    """Exponential backoff with a hard cap + symmetric jitter band.

    ``delay = min(MAX_DELAY_S, BASE_DELAY_S * BACKOFF_FACTOR**(attempts-1))`` then
    scaled by ``(1 + uniform(-JITTER, +JITTER))``. ``jitter_fn`` is injectable so
    tests pin the center (``lambda lo, hi: 0.0``) and assert the exact schedule.
    """
    fn = jitter_fn if jitter_fn is not None else _jitter_fn
    base = min(MAX_DELAY_S, BASE_DELAY_S * (BACKOFF_FACTOR ** (attempts - 1)))
    return base * (1.0 + fn(-JITTER, JITTER))


@dataclass(frozen=True)
class _Claim:
    """Immutable claim snapshot captured BEFORE the claim session closes. The
    work/record phases re-SELECT by id; they never reuse the claim ORM object."""

    attempt_id: int
    session_id: int
    attempts_after_claim: int


async def _claim_due(
    *, limit: int, user_id: str | None
) -> list[_Claim]:
    """Phase A — claim up to ``limit`` due rows (FOR UPDATE SKIP LOCKED), bump
    ``learner_update_attempts``, write the work-lease, and COMMIT immediately."""
    async with get_async_session() as claim_db:
        now = _utc_now()
        stmt = (
            select(ProblemAttempt)
            .where(ProblemAttempt.learner_update_pending.is_(True))
            .where(ProblemAttempt.learner_update_failed_permanently.is_(False))
            .where(
                or_(
                    ProblemAttempt.learner_update_next_attempt_at.is_(None),
                    ProblemAttempt.learner_update_next_attempt_at <= now,
                )
            )
        )
        if user_id is not None:
            stmt = stmt.join(
                ApolloSession, ApolloSession.id == ProblemAttempt.session_id
            ).where(ApolloSession.user_id == user_id)
        stmt = (
            stmt.order_by(ProblemAttempt.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (await claim_db.execute(stmt)).scalars().all()
        claims: list[_Claim] = []
        for attempt in rows:
            attempt.learner_update_attempts = (attempt.learner_update_attempts or 0) + 1
            attempt.learner_update_next_attempt_at = now + timedelta(seconds=CLAIM_LEASE_S)
            claims.append(
                _Claim(
                    attempt_id=attempt.id,
                    session_id=attempt.session_id,
                    attempts_after_claim=attempt.learner_update_attempts,
                )
            )
        await claim_db.commit()
        return claims


async def _work_one(neo, claim: _Claim) -> tuple[str, str | None]:
    """Phase B — re-run the frozen chain from durable state in a FRESH session.

    Returns ``(outcome, detail)`` where ``outcome`` is
    ``success`` | ``deferred`` | ``dead_letter`` | ``retry`` and ``detail`` is the
    dead-letter reason or the truncated transient error (None on success/defer).
    """
    async with get_async_session() as work_db:
        attempt = await work_db.get(ProblemAttempt, claim.attempt_id)
        sess = await work_db.get(ApolloSession, claim.session_id)
        try:
            rerun = await build_rerun_inputs(
                work_db, neo, attempt=attempt, sess=sess
            )  # terminal pre-flight, NO LLM
            shadow = await run_graph_simulation(
                work_db,
                neo,
                attempt=attempt,
                sess=sess,
                student_graph=rerun.student_graph,
                problem_payload=rerun.problem_payload,
                old_rubric=rerun.old_rubric,
            )
            if shadow is not None and _graph_sim_layer3_enabled():
                done_ts = datetime.fromisoformat(rerun.graded_at_iso)
                if done_ts.tzinfo is None:
                    raise LearnerUpdateUnreconstructableError(
                        attempt_id=claim.attempt_id, reason="graded_at_missing"
                    )
                await run_learner_update(
                    work_db,
                    sess=sess,
                    attempt=attempt,
                    shadow=shadow,
                    done_ts=done_ts,
                    parser_confidence=rerun.parser_confidence,
                )
                return "success", None
            # LAYER3 OFF (or no shadow): belief write deferred, row stays pending.
            return "deferred", None
        except LearnerUpdateUnreconstructableError as e:
            return "dead_letter", e.reason
        except NoResultFound:
            # A vanished ConceptProblem (_find_problem_payload.scalar_one()) can
            # never be reconstructed -> terminal dead-letter, NOT a crash-loop.
            return "dead_letter", "problem_vanished"
        except Exception as e:  # noqa: BLE001 — transient infra failure
            return "retry", _truncate(str(e))


async def _record(
    claim: _Claim, outcome: str, detail: str | None, *, max_attempts: int
) -> str:
    """Phase C — short record-txn; RE-SELECT by id and apply the outcome.

    Returns the FINAL terminal classification: ``success`` / ``deferred`` /
    ``dead_letter`` / ``retry``. A transient ``retry`` that hits the
    ``attempts >= max_attempts`` cap is recorded AND returned as ``dead_letter``
    so the sweep accounting reflects the on-disk reality.
    """
    async with get_async_session() as rec_db:
        attempt = await rec_db.get(ProblemAttempt, claim.attempt_id)
        now = _utc_now()
        final = outcome
        if outcome == "success":
            attempt.learner_update_pending = False
        elif outcome == "deferred":
            # A LAYER3 configuration deferral is NOT a failed attempt: undo the
            # at-claim retry consumption and re-arm so the row is immediately due
            # (worker pacing throttles the re-drain spin in WU-5B3b).
            attempt.learner_update_attempts = max(
                0, (attempt.learner_update_attempts or 1) - 1
            )
            attempt.learner_update_next_attempt_at = None
        elif outcome == "dead_letter":
            attempt.learner_update_failed_permanently = True
            attempt.learner_update_failed_at = now
            attempt.learner_update_last_error = detail
            _LOG.warning(
                "learner_janitor_dead_letter",
                extra={"attempt_id": claim.attempt_id, "reason": detail},
            )
        else:  # transient "retry"
            if (attempt.learner_update_attempts or 0) >= max_attempts:
                attempt.learner_update_failed_permanently = True
                attempt.learner_update_failed_at = now
                attempt.learner_update_last_error = _truncate(detail)
                final = "dead_letter"
                _LOG.warning(
                    "learner_janitor_dead_letter",
                    extra={
                        "attempt_id": claim.attempt_id,
                        "reason": "max_attempts_exhausted",
                    },
                )
            else:
                attempt.learner_update_next_attempt_at = now + timedelta(
                    seconds=_backoff_delay_s(attempt.learner_update_attempts)
                )
                attempt.learner_update_last_error = _truncate(detail)
        await rec_db.commit()
        return final


async def _backlog_remaining() -> int:
    """Post-sweep COUNT of still-pending, not-dead rows (partial index)."""
    async with get_async_session() as db:
        return (
            await db.execute(
                select(func.count())
                .select_from(ProblemAttempt)
                .where(ProblemAttempt.learner_update_pending.is_(True))
                .where(ProblemAttempt.learner_update_failed_permanently.is_(False))
            )
        ).scalar_one()


async def drain_pending_attempts(
    neo,
    *,
    limit: int = 1,
    max_attempts: int = MAX_ATTEMPTS,
    user_id: str | None = None,
) -> DrainResult:
    """Drain up to ``limit`` due ``learner_update_pending`` attempts.

    ``neo`` is the ``Neo4jClient`` (positional, mirroring
    ``run_graph_simulation(db, neo, ...)``). ``user_id`` scopes the claim to one
    student's due rows (WU-5B3b backstop); ``None`` is global (the worker loop).
    Returns a frozen :class:`DrainResult` the caller logs.
    """
    claims = await _claim_due(limit=limit, user_id=user_id)

    succeeded = dead_lettered = retried = deferred = 0
    for claim in claims:
        outcome, detail = await _work_one(neo, claim)
        if outcome == "deferred":
            _LOG.warning(
                "learner_janitor_layer3_deferred",
                extra={"attempt_id": claim.attempt_id},
            )
        # _record returns the FINAL classification: a transient retry that hits the
        # MAX_ATTEMPTS cap comes back as "dead_letter".
        final = await _record(claim, outcome, detail, max_attempts=max_attempts)
        if final == "success":
            succeeded += 1
        elif final == "deferred":
            deferred += 1
        elif final == "dead_letter":
            dead_lettered += 1
        else:  # "retry" -> backed off, still pending
            retried += 1

    backlog = await _backlog_remaining()
    result = DrainResult(
        claimed=len(claims),
        succeeded=succeeded,
        dead_lettered=dead_lettered,
        retried=retried,
        deferred=deferred,
        backlog_remaining=backlog,
    )
    _LOG.info(
        "learner_janitor_drain",
        extra={
            "claimed": result.claimed,
            "succeeded": result.succeeded,
            "dead_lettered": result.dead_lettered,
            "retried": result.retried,
            "deferred": result.deferred,
            "backlog_remaining": result.backlog_remaining,
        },
    )
    return result
