"""WU-3B2g — the THIN content-key-idempotent enqueue at the indexing-completion seam.

``enqueue_provisioning_job`` runs INSIDE the teacher-upload finalize session
(``knowledge/teacher_weekly.py``), riding the SAME commit so a provisioning job
is never visible for a document that did not finish indexing. It does the LEAST
possible inside that upload session — a single short-circuit SELECT plus (at
most) two inserts — and it can NEVER raise out of the caller's commit: a
partial-unique-index collision (an OPEN job already exists for this document) is
caught inside a SAVEPOINT (``begin_nested``) and turned into a ``None`` return.

Three nested idempotency layers gate a duplicate (§2 of the plan); this seam owns
the OUTERMOST one — the document-level no-op (§9 OPS-2):

  * **content-hash short-circuit** — if an ``apollo_ingest_runs`` row already
    exists for ``(document_id, content_hash)`` with ``status='succeeded'``, the
    document is unchanged since a successful run, so we enqueue ZERO new jobs and
    return ``None``. (Removing this SELECT makes an unchanged re-upload enqueue a
    duplicate job — the mutation-discriminating property, T-EQ2 / T-DB3.)
  * **partial-unique-index collapse** — if the short-circuit was missed (e.g. the
    first run is still ``running``), the migration-030 partial-unique-index
    ``apollo_provisioning_jobs_open_uniq`` (``WHERE state IN ('pending','running')``)
    rejects the second OPEN insert with an ``IntegrityError``; we swallow it via
    the savepoint and return ``None`` (treat as "already enqueued").

A ``content_hash`` of ``None`` (a legacy ``aita_documents`` row) cannot
short-circuit; the job runs once and a later real hash short-circuits. This is
safe (no double-run risk because the job dedup index still collapses opens).

The flag-OFF non-regression: this enqueue ALWAYS runs (it only writes the two new
provisioning rows), but with ``APOLLO_AUTOPROVISION_ENABLED`` OFF NO worker drains
the job, so the teacher upload behaves byte-identically to today.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import IngestRun, ProvisioningJob

__all__ = ["enqueue_provisioning_job"]

_LOG = logging.getLogger(__name__)

_RUN_STATUS_QUEUED = "queued"
_RUN_STATUS_SUCCEEDED = "succeeded"
_JOB_STATE_PENDING = "pending"


async def enqueue_provisioning_job(
    session: AsyncSession,
    *,
    search_space_id: int,
    document_id: int,
    content_hash: str | None,
) -> int | None:
    """Idempotent enqueue at the indexing-completion seam.

    Returns the new ``apollo_provisioning_jobs.id``, or ``None`` when
    short-circuited (an unchanged re-upload already has a ``status='succeeded'``
    run for ``(document_id, content_hash)``, OR a partial-unique-index collision
    means an open job already exists, OR any enqueue write failed).

    Runs INSIDE the caller's (``teacher_weekly``) committed session — does NOT
    commit/rollback the OUTER transaction itself (the finalize block owns the
    commit). The ENTIRE enqueue runs inside a nested SAVEPOINT so it can NEVER
    wedge the upload commit: a partial-unique-index collision OR any other failure
    (e.g. a missing table in a degraded environment) rolls back ONLY the enqueue's
    own writes and returns ``None``, leaving the upload-facing writes intact (the
    §14 headline non-regression — the enqueue must do the LEAST possible and never
    take down a teacher upload)."""
    # The whole enqueue rides ONE nested SAVEPOINT. On the happy path it commits
    # (releases the savepoint) with the two new rows pending in the outer txn; on
    # ANY failure the savepoint rolls back, the outer upload commit is untouched.
    savepoint = await session.begin_nested()
    try:
        # --- 1. Document-level short-circuit (§9 OPS-2) -------------------- #
        # Only a SUCCEEDED run with the SAME content hash means "unchanged"; a
        # None hash can never match (a legacy doc enqueues once). A
        # failed/queued/running prior run does NOT short-circuit.
        if content_hash is not None:
            existing_succeeded = (
                await session.execute(
                    select(IngestRun.id)
                    .where(IngestRun.document_id == document_id)
                    .where(IngestRun.content_hash == content_hash)
                    .where(IngestRun.status == _RUN_STATUS_SUCCEEDED)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing_succeeded is not None:
                await savepoint.commit()
                _LOG.info(
                    "provisioning_enqueue_short_circuit",
                    extra={
                        "event": "provisioning_enqueue_short_circuit",
                        "document_id": document_id,
                        "ingest_run_id": int(existing_succeeded),
                    },
                )
                return None

        # --- 2. Insert the run + the open job ----------------------------- #
        run = IngestRun(
            search_space_id=search_space_id,
            document_id=document_id,
            content_hash=content_hash,
            status=_RUN_STATUS_QUEUED,
        )
        session.add(run)
        await session.flush()  # assign run.id for the FK

        job = ProvisioningJob(
            search_space_id=search_space_id,
            document_id=document_id,
            state=_JOB_STATE_PENDING,
            ingest_run_id=run.id,
        )
        session.add(job)
        await session.flush()  # may raise IntegrityError on the open-uniq index
        job_id = int(job.id)
        await savepoint.commit()
    except IntegrityError:
        # An OPEN job already exists for this document (the migration-030
        # partial-unique-index). Roll back the savepoint (un-registers the pending
        # rows) and treat as "already enqueued" — a no-op.
        await savepoint.rollback()
        _LOG.info(
            "provisioning_enqueue_open_collision",
            extra={
                "event": "provisioning_enqueue_open_collision",
                "document_id": document_id,
            },
        )
        return None
    except Exception:  # noqa: BLE001 - the enqueue must NEVER break the upload commit
        # A degraded environment (e.g. the apollo provisioning tables not yet
        # migrated) must not take down a teacher upload. Roll back the savepoint
        # and no-op; production has the tables, so this is a defensive backstop.
        await savepoint.rollback()
        _LOG.warning(
            "provisioning_enqueue_skipped",
            extra={
                "event": "provisioning_enqueue_skipped",
                "document_id": document_id,
            },
            exc_info=True,
        )
        return None

    _LOG.info(
        "provisioning_enqueue",
        extra={
            "event": "provisioning_enqueue",
            "document_id": document_id,
            "ingest_run_id": int(run.id),
            "job_id": job_id,
        },
    )
    return job_id
