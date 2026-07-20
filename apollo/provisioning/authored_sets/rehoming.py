"""Durable, retryable concept re-homing for confirmed manual typed problems.

Promotion commits the problem as teachable under ``provisional.inventory`` first.
This module then mirrors the repository's provisioning queue: enqueue a durable
row, claim with ``FOR UPDATE SKIP LOCKED`` and a lease, run unchanged tag/mint,
and retain terminal/queryable diagnostics without ever demoting the problem.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.canon_projection import project_canon
from apollo.persistence.models import ConceptProblem, RehomingJob
from apollo.provisioning.cost_constants import MAX_ATTEMPTS
from apollo.provisioning.tag_mint import ApprovedPair, ResolvedConcept, tag_and_mint
from apollo.schemas.problem import Problem

__all__ = [
    "ClaimedRehoming",
    "claim_rehoming_job",
    "complete_rehoming_job",
    "enqueue_rehoming",
    "fail_rehoming_job",
    "run_rehoming",
]

_AUDIT_LIMIT = 20
_MAX_ERROR_LEN = 2000
_PENDING = "pending"
_RUNNING = "running"
_COMPLETED = "completed"
_FAILED = "failed"


def _now() -> datetime:
    return datetime.now(UTC)


def _event(status: str, *, diagnostic: str = "", job_id: int | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "status": status,
        "at": _now().isoformat(),
        "diagnostic": diagnostic,
    }
    if job_id is not None:
        event["job_id"] = job_id
    return event


def _state(row: ConceptProblem) -> dict[str, Any]:
    value = (row.provenance or {}).get("typed_rehoming")
    return dict(value) if isinstance(value, dict) else {}


def _write_state(row: ConceptProblem, state: dict[str, Any]) -> None:
    row.provenance = {**(row.provenance or {}), "typed_rehoming": state}


def _transition_problem(
    row: ConceptProblem,
    status: str,
    *,
    diagnostic: str = "",
    job_id: int | None = None,
    **updates: Any,
) -> None:
    prior = _state(row)
    audit = list(prior.get("audit") or [])[-(_AUDIT_LIMIT - 1) :]
    audit.append(_event(status, diagnostic=diagnostic, job_id=job_id))
    _write_state(
        row,
        {
            **prior,
            **updates,
            "status": status,
            "diagnostic": diagnostic,
            "job_id": job_id if job_id is not None else prior.get("job_id"),
            "audit": audit,
        },
    )


async def enqueue_rehoming(
    db: AsyncSession,
    row: ConceptProblem,
    *,
    requested_concept_id: int | None = None,
) -> int:
    """Create or reuse the one open durable job for ``row`` and mark it pending."""
    if row.tier != 2:
        raise ValueError("re-homing enqueue requires an already promoted Tier-2 problem")
    open_job = (
        await db.execute(
            select(RehomingJob)
            .where(RehomingJob.concept_problem_id == int(row.id))
            .where(RehomingJob.state.in_((_PENDING, _RUNNING)))
            .order_by(RehomingJob.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if open_job is None:
        open_job = RehomingJob(
            search_space_id=int(row.search_space_id),
            concept_problem_id=int(row.id),
            requested_concept_id=requested_concept_id,
            state=_PENDING,
        )
        db.add(open_job)
        await db.flush()
    elif open_job.state == _PENDING:
        open_job.requested_concept_id = requested_concept_id
        open_job.last_error = None
        open_job.updated_at = _now()
    job_id = int(open_job.id)
    _transition_problem(
        row,
        "rehoming_pending",
        job_id=job_id,
        requested_concept_id=requested_concept_id,
        queued_at=_now().isoformat(),
        lease_owner=None,
        lease_expires_at=None,
    )
    await db.flush()
    return job_id


@dataclass(frozen=True)
class ClaimedRehoming:
    job_id: int
    problem_id: int
    search_space_id: int
    requested_concept_id: int | None
    attempt_count: int


async def claim_rehoming_job(
    db: AsyncSession,
    *,
    lease_owner: str,
    lease_seconds: int,
    job_id: int | None = None,
) -> ClaimedRehoming | None:
    """Claim one FIFO pending/expired re-homing job and commit before work."""
    now = _now()
    stmt = (
        select(RehomingJob)
        .where(
            or_(
                RehomingJob.state == _PENDING,
                (RehomingJob.state == _RUNNING)
                & RehomingJob.lease_expires_at.is_not(None)
                & (RehomingJob.lease_expires_at < now),
            )
        )
        .order_by(RehomingJob.created_at.asc(), RehomingJob.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job_id is not None:
        stmt = stmt.where(RehomingJob.id == job_id)
    job = (await db.execute(stmt)).scalars().first()
    if job is None:
        return None

    job.state = _RUNNING
    job.lease_owner = lease_owner
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.attempt_count = int(job.attempt_count or 0) + 1
    job.updated_at = now
    row = await db.get(ConceptProblem, int(job.concept_problem_id))
    if row is not None:
        _transition_problem(
            row,
            "rehoming_running",
            job_id=int(job.id),
            attempt_count=int(job.attempt_count),
            last_attempt_at=now.isoformat(),
            lease_owner=lease_owner,
            lease_expires_at=job.lease_expires_at.isoformat(),
        )
    await db.commit()
    return ClaimedRehoming(
        job_id=int(job.id),
        problem_id=int(job.concept_problem_id),
        search_space_id=int(job.search_space_id),
        requested_concept_id=(
            int(job.requested_concept_id) if job.requested_concept_id is not None else None
        ),
        attempt_count=int(job.attempt_count),
    )


async def complete_rehoming_job(db: AsyncSession, *, job_id: int) -> None:
    job = await db.get(RehomingJob, job_id)
    if job is None:
        return
    job.state = _COMPLETED
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = None
    job.updated_at = _now()
    row = await db.get(ConceptProblem, int(job.concept_problem_id))
    if row is not None:
        prior = _state(row)
        _write_state(row, {**prior, "job_state": _COMPLETED, "retryable": False})
    await db.commit()


async def fail_rehoming_job(db: AsyncSession, *, job_id: int, error: str) -> str:
    """Release for an automatic retry until the existing queue retry cap."""
    job = await db.get(RehomingJob, job_id)
    if job is None:
        return _FAILED
    job.state = _FAILED if int(job.attempt_count or 0) >= MAX_ATTEMPTS else _PENDING
    job.last_error = (error or "")[:_MAX_ERROR_LEN]
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = _now()
    row = await db.get(ConceptProblem, int(job.concept_problem_id))
    if row is not None:
        prior = _state(row)
        _write_state(
            row,
            {
                **prior,
                "job_state": str(job.state),
                "attempt_count": int(job.attempt_count or 0),
                "retryable": job.state == _PENDING,
                "diagnostic": job.last_error or prior.get("diagnostic", ""),
            },
        )
    await db.commit()
    return str(job.state)


async def run_rehoming(
    db: AsyncSession,
    neo: Any,
    *,
    problem_id: int,
    chat_fn: Callable[..., str],
    embed_fn: Callable[[str], Sequence[float]],
    resolved_concept: ResolvedConcept | None = None,
    job_id: int | None = None,
) -> bool:
    """Run unchanged tag/dedup/mint, then idempotently move and project the row.

    Every tag, cost, database, or Neo4j failure is recorded on the already
    teachable problem and returns ``False``. It never rejects, demotes, or deletes
    that problem.
    """
    try:
        row = await db.get(ConceptProblem, problem_id)
        if row is None:
            return False
        if row.tier != 2:
            raise ValueError("re-homing requires an already promoted Tier-2 problem")
        problem = Problem.model_validate(row.payload).model_dump()
        async with db.begin_nested():
            plan = await tag_and_mint(
                db,
                ApprovedPair(
                    problem=problem,
                    search_space_id=int(row.search_space_id),
                    solution_source=str(row.solution_source or "authored"),
                    misconceptions=[],
                ),
                chat_fn=chat_fn,
                embed_fn=embed_fn,
                resolved_concept=resolved_concept,
                diagnose_existing_symbols=True,
            )
            row.concept_id = plan.concept_id
            row.payload = {**(row.payload or {}), "concept_id": plan.concept_slug}
            await project_canon(
                db,
                neo,
                search_space_id=int(row.search_space_id),
                concept_id=plan.concept_id,
            )
        diagnostic = plan.concept_symbol_diagnostic or ""
        _transition_problem(
            row,
            "rehoming_complete",
            diagnostic=diagnostic,
            job_id=job_id,
            concept_id=plan.concept_id,
            concept_slug=plan.concept_slug,
            review_required=bool(diagnostic),
            completed_at=_now().isoformat(),
            lease_owner=None,
            lease_expires_at=None,
        )
        await db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - re-homing is a non-demoting failure domain
        await db.rollback()
        row = await db.get(ConceptProblem, problem_id)
        if row is None:
            return False
        diagnostic = f"{type(exc).__name__}: {exc}"
        _transition_problem(
            row,
            "rehoming_failed",
            diagnostic=diagnostic,
            job_id=job_id,
            failed_at=_now().isoformat(),
            lease_owner=None,
            lease_expires_at=None,
        )
        await db.commit()
        return False
