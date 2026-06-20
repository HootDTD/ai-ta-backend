"""WU-3B2f — the SKIP-LOCKED claim/lease drain over ``apollo_provisioning_jobs``.

Mirrors the proven ``knowledge/teacher_weekly.py:_claim_upload_job_async`` lease
shape: a short ``SELECT … FOR UPDATE SKIP LOCKED`` transaction that flips
``state='running'``, sets ``lease_owner`` / ``lease_expires_at``, bumps
``attempt_count``, and COMMITs. Two concurrent claimers CANNOT lock the same row
(SKIP-LOCKED skips an already-locked row), so they claim DISJOINT jobs or return
``None``. A worker that dies mid-work leaves the row ``running`` with an EXPIRED
lease; the predicate ``state='running' AND lease_expires_at < now()`` makes it
re-claimable (``attempt_count`` bumps again), and ``fail_job`` dead-letters a
job to terminal ``failed`` once ``attempt_count >= MAX_ATTEMPTS`` so a poison job
cannot loop forever.

This unit owns ONLY the claim/lease + terminal transitions. The trigger/enqueue
(WU-3B2g), the worker-loop shell (WU-3B2g), the orchestrator/stage logic
(3B2b–e), and the ``apollo_ingest_errors`` production WRITE (3B2g) are out of
scope. The caller owns the ``session`` (it is committed here — claim-then-commit-
before-work, matching ``learner_janitor._claim_due``).

The provisioning ``state`` vocabulary is ``pending/running/completed/failed`` —
DISTINCT from the run ``status`` vocabulary (``queued/running/succeeded/failed``).
``now()`` is computed in Python (``datetime.now(UTC)``) to match the template and
keep the lease arithmetic test-controllable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ProvisioningJob
from apollo.provisioning.cost_constants import MAX_ATTEMPTS

_LOG = logging.getLogger(__name__)

_STATE_PENDING = "pending"
_STATE_RUNNING = "running"
_STATE_COMPLETED = "completed"
_STATE_FAILED = "failed"

# Defensive cap so a runaway upstream error string never bloats the row / log.
_MAX_ERROR_LEN = 2000


@dataclass(frozen=True)
class ClaimedJob:
    """The frozen claim DTO returned to the orchestrator. ``attempt_count`` is the
    value AFTER the claim bump."""

    job_id: int
    search_space_id: int
    document_id: int
    ingest_run_id: int | None
    attempt_count: int


def _now() -> datetime:
    return datetime.now(UTC)


async def claim_provisioning_job(
    session: AsyncSession,
    *,
    lease_owner: str,
    lease_seconds: int,
) -> ClaimedJob | None:
    """Claim the next runnable job under ``FOR UPDATE SKIP LOCKED``.

    Selects the FIFO-earliest row that is ``pending`` OR a ``running`` row whose
    lease has expired, flips it to ``running`` with a fresh lease, bumps
    ``attempt_count``, COMMITs, and returns a ``ClaimedJob``. Returns ``None``
    when nothing is claimable. The ``skip_locked=True`` clause is load-bearing:
    dropping it lets two concurrent claimers grab the same row.
    """
    now = _now()
    stmt = (
        select(ProvisioningJob)
        .where(
            or_(
                ProvisioningJob.state == _STATE_PENDING,
                (ProvisioningJob.state == _STATE_RUNNING)
                & (ProvisioningJob.lease_expires_at.is_not(None))
                & (ProvisioningJob.lease_expires_at < now),
            )
        )
        .order_by(ProvisioningJob.created_at.asc(), ProvisioningJob.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await session.execute(stmt)).scalars().first()
    if job is None:
        return None

    job.state = _STATE_RUNNING
    job.lease_owner = lease_owner
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.attempt_count = int(job.attempt_count or 0) + 1
    job.updated_at = now

    await session.commit()

    _LOG.info(
        "provisioning_claim",
        extra={
            "event": "provisioning_claim",
            "job_id": int(job.id),
            "state": job.state,
            "attempt_count": job.attempt_count,
            "ingest_run_id": job.ingest_run_id,
        },
    )
    return ClaimedJob(
        job_id=int(job.id),
        search_space_id=int(job.search_space_id),
        document_id=int(job.document_id),
        ingest_run_id=(
            int(job.ingest_run_id) if job.ingest_run_id is not None else None
        ),
        attempt_count=int(job.attempt_count),
    )


async def _load(session: AsyncSession, job_id: int) -> ProvisioningJob:
    job = await session.get(ProvisioningJob, job_id)
    if job is None:  # pragma: no cover - caller always holds a live job_id
        raise ValueError(f"provisioning job {job_id} not found")
    return job


def _clear_lease(job: ProvisioningJob, now: datetime) -> None:
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = now


async def complete_job(session: AsyncSession, *, job_id: int) -> None:
    """Move a job to terminal ``completed`` and clear its lease, then COMMIT."""
    now = _now()
    job = await _load(session, job_id)
    job.state = _STATE_COMPLETED
    _clear_lease(job, now)
    await session.commit()
    _LOG.info(
        "provisioning_complete",
        extra={"event": "provisioning_complete", "job_id": job_id, "state": job.state},
    )


async def fail_job(session: AsyncSession, *, job_id: int, error: str) -> str:
    """Fail a job: dead-letter to ``failed`` when ``attempt_count >= MAX_ATTEMPTS``,
    else back to ``pending`` for retry. Always clears the lease + sets
    ``last_error``, then COMMITs. Returns the resulting state ('failed'|'pending').
    """
    now = _now()
    job = await _load(session, job_id)
    if int(job.attempt_count or 0) >= MAX_ATTEMPTS:
        job.state = _STATE_FAILED
    else:
        job.state = _STATE_PENDING
    job.last_error = (error or "")[:_MAX_ERROR_LEN]
    _clear_lease(job, now)
    await session.commit()
    _LOG.info(
        "provisioning_fail",
        extra={
            "event": "provisioning_fail",
            "job_id": job_id,
            "state": job.state,
            "attempt_count": job.attempt_count,
        },
    )
    return job.state


async def release_job(session: AsyncSession, *, job_id: int) -> None:
    """Cooperatively release a job back to ``pending`` (e.g. a graceful shutdown):
    clear the lease, leave ``attempt_count`` UNCHANGED (release is not a failure),
    then COMMIT."""
    now = _now()
    job = await _load(session, job_id)
    job.state = _STATE_PENDING
    _clear_lease(job, now)
    await session.commit()
    _LOG.info(
        "provisioning_release",
        extra={"event": "provisioning_release", "job_id": job_id, "state": job.state},
    )


__all__ = [
    "ClaimedJob",
    "claim_provisioning_job",
    "complete_job",
    "fail_job",
    "release_job",
]
