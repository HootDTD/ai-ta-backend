"""Dormant ``apollo-provision`` worker process (WU-3B2g).

The 4th Procfile process. A thin, async-native poll loop that drains the
``apollo_provisioning_jobs`` queue behind the default-OFF
``APOLLO_AUTOPROVISION_ENABLED`` flag, with cooperative SIGTERM/SIGINT shutdown.
It MIRRORS ``apollo/learner_janitor_worker.py`` 1:1, swapping the drain body for
the lease-reaper + claim (3B2f) + ``run_provisioning`` (3B2g orchestrator) +
terminal job decision.

This module owns ONLY the process shell + the lease-reaper:
  - the poll loop (``_loop`` / ``_run_one_iteration``),
  - the per-iteration flag read (``_autoprovision_enabled``),
  - the lease-reaper pass (``_reap_expired``) run BEFORE each claim (§9 OPS-5),
  - the single-job drain (``_drain_one``): claim -> run_provisioning ->
    complete_job | fail_job,
  - the SIGTERM/SIGINT handler registration,
  - the single ``Neo4jClient`` lifetime (``main``),
  - the 4th Procfile process line.

It REDEFINES no stage. The claim/lease + terminal transitions are FROZEN
(``apollo/provisioning/queue.py``, 3B2f); the 6-stage orchestrator is
``apollo/provisioning/orchestrator.py``. The headline safety: with the flag OFF
NOTHING drains, so a teacher upload behaves byte-identically to today and no
auto-provisioned content reaches a student without a human deploy step (the
process also ships scaled to 0 replicas).

The worker runs entirely inside a SINGLE ``asyncio.run(main())`` loop so
``database.session``'s ``id(loop)``-keyed engine registry resolves the SAME
engine for every drain phase (the janitor-worker rationale).

THE LEASE-REAPER (§9 OPS-5): a worker that dies mid-document leaves the run
``status='running'`` and the job ``running``. The FROZEN claim re-claims on lease
expiry, but a run row stranded ``'running'`` would wedge re-enqueue against the
partial-unique-index. ``_reap_expired`` runs each sweep BEFORE claiming: for every
``apollo_provisioning_jobs`` row that is ``state='running' AND lease_expires_at <
now()``, it marks the linked ``apollo_ingest_runs.status='failed'`` so the run is
NEVER left ``'running'``; the FROZEN claim then re-claims the job itself.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket

from sqlalchemy import and_, select

from apollo.handlers.learner_janitor import _int_env  # REUSE the frozen helper
from apollo.persistence.models import IngestRun, ProvisioningJob
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.provisioning.metered_chat import MeteredChat
from apollo.provisioning.orchestrator import ProvisioningOutcome, run_provisioning
from apollo.provisioning.queue import (
    claim_provisioning_job,
    complete_job,
    fail_job,
)
from database.session import get_async_session

# Default OFF EVERYWHERE — activation is a human calibration/deploy decision.
_AUTOPROVISION_ENABLED_FLAG: str = "APOLLO_AUTOPROVISION_ENABLED"

# Small, sequential sweep bounds LLM spend (one document per sweep); poll interval
# >= per-document LLM time. Lease covers the (potentially long) per-document run.
SWEEP_LIMIT: int = _int_env("APOLLO_PROVISION_SWEEP_LIMIT", 1)
POLL_SECONDS: int = _int_env("APOLLO_PROVISION_POLL_SECONDS", 60)
LEASE_SECONDS: int = _int_env("APOLLO_PROVISION_LEASE_SECONDS", 1800)

# A stable-per-process lease owner so a re-claim of THIS worker's stuck row is
# distinguishable in the audit (the queue uses it only as an opaque tag).
LEASE_OWNER: str = f"provision-{socket.gethostname()}-{os.getpid()}"

_LOG = logging.getLogger(__name__)

_RUN_STATUS_RUNNING = "running"
_RUN_STATUS_FAILED = "failed"
_JOB_STATE_RUNNING = "running"


def _autoprovision_enabled() -> bool:
    """Read per-iteration so a flag flip is observed without a process restart.
    Default OFF (mirrors ``learner_janitor_worker._janitor_enabled``)."""
    return os.environ.get(_AUTOPROVISION_ENABLED_FLAG, "").lower() in (
        "1",
        "true",
        "yes",
    )


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def _reap_expired(session_factory) -> int:
    """Lease-reaper (§9 OPS-5). For every ``apollo_provisioning_jobs`` row that is
    ``state='running' AND lease_expires_at < now()``, mark its linked
    ``apollo_ingest_runs.status='failed'`` so a crashed mid-document run is NEVER
    left ``'running'`` (which would wedge re-enqueue against the partial-unique-
    index). The FROZEN claim re-claims the job itself on lease expiry. Returns the
    count of runs reaped (marked failed)."""
    now = _now()
    reaped = 0
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ProvisioningJob).where(
                        and_(
                            ProvisioningJob.state == _JOB_STATE_RUNNING,
                            ProvisioningJob.lease_expires_at.is_not(None),
                            ProvisioningJob.lease_expires_at < now,
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in rows:
            if job.ingest_run_id is None:
                continue
            run = await session.get(IngestRun, job.ingest_run_id)
            if run is not None and run.status == _RUN_STATUS_RUNNING:
                run.status = _RUN_STATUS_FAILED
                run.finished_at = now
                reaped += 1
        if reaped:
            await session.commit()
    if reaped:
        _LOG.info(
            "apollo_provision_reaped", extra={"event": "apollo_provision_reaped", "reaped": reaped}
        )
    return reaped


def _default_metered_factory(*, ingest_run, document_id) -> MeteredChat:
    """Build a ``MeteredChat`` bound to the claimed run row (metering accrues on
    that row). The OpenAI client is the SDK default (key from ``OPENAI_API_KEY``);
    no key is an argument here."""
    return MeteredChat(ingest_run=ingest_run, document_id=document_id)


async def _drain_one(
    neo, *, session_factory, metered_chat_factory
) -> ProvisioningOutcome | None:
    """Claim (3B2f) -> run_provisioning -> complete_job | fail_job. Returns the
    outcome, or ``None`` when nothing is claimable. The claim COMMITs the job
    transition; ``run_provisioning`` then runs in its own session bound to the
    claimed run row, and the terminal job decision keys on ``outcome.status``."""
    async with session_factory() as claim_session:
        claimed = await claim_provisioning_job(
            claim_session, lease_owner=LEASE_OWNER, lease_seconds=LEASE_SECONDS
        )
    if claimed is None:
        return None

    async with session_factory() as work_session:
        ingest_run = await work_session.get(IngestRun, claimed.ingest_run_id)
        metered = metered_chat_factory(
            ingest_run=ingest_run, document_id=claimed.document_id
        )
        outcome = await run_provisioning(
            work_session, neo, job=claimed, metered_chat=metered
        )

    async with session_factory() as terminal_session:
        if outcome.status == "succeeded":
            await complete_job(terminal_session, job_id=claimed.job_id)
        else:
            await fail_job(
                terminal_session,
                job_id=claimed.job_id,
                error=f"run {outcome.run_id} status={outcome.status}",
            )
    return outcome


async def _run_one_iteration(neo, *, stop_event: asyncio.Event) -> None:
    """ONE loop pass — flag-read -> (reap + drain | skip) -> sleep.

    Extracted from ``_loop`` so the body is unit-testable WITHOUT a process and
    never hides under the ``while``-shell pragma. The reap+drain is wrapped in a
    ``try/except Exception`` so one bad sweep logs and is survived rather than
    killing the worker; the sleep still runs so a flag flip is picked up next
    pass. ``KeyboardInterrupt``/``SystemExit`` are not ``Exception`` subclasses,
    so they propagate."""
    if _autoprovision_enabled():
        try:
            # Reap BEFORE claiming (§9 OPS-5 ordering): re-open any stranded run.
            await _reap_expired(get_async_session)
            outcome = await _drain_one(
                neo,
                session_factory=get_async_session,
                metered_chat_factory=_default_metered_factory,
            )
            if outcome is not None:
                _LOG.info(
                    "apollo_provision_sweep",
                    extra={
                        "run_id": outcome.run_id,
                        "status": outcome.status,
                        "n_promoted": outcome.n_promoted,
                        "n_rejected": outcome.n_rejected,
                        "llm_cost": None,
                    },
                )
        except Exception:  # noqa: BLE001 - one bad sweep must not kill the worker
            _LOG.exception("apollo_provision_sweep_failed")
    else:
        _LOG.debug("apollo_provision_disabled")  # flag OFF -> NO drain (early skip)
    await asyncio.sleep(POLL_SECONDS)


async def _loop(neo, *, stop_event: asyncio.Event) -> None:
    """The poll loop. ``stop_event`` is checked at the TOP of each pass, so a set
    event exits AFTER the current iteration (never mid-drain — the drain is
    awaited to completion inside ``_run_one_iteration``)."""
    _LOG.info(
        "apollo_provision_worker_started",
        extra={"poll_seconds": POLL_SECONDS, "sweep_limit": SWEEP_LIMIT},
    )
    while not stop_event.is_set():  # pragma: no cover - loop shell (body is _run_one_iteration)
        await _run_one_iteration(neo, stop_event=stop_event)
    _LOG.info("apollo_provision_worker_stopped")


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event
) -> None:
    """Register SIGINT + SIGTERM to set ``stop_event`` (cooperative cancel).
    Wrapped in try/except so Windows (``NotImplementedError``) and non-main
    threads (``RuntimeError``) fall back to a warning instead of crashing."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            _LOG.warning(
                "apollo_provision_signal_handler_unsupported", extra={"signal": sig}
            )


async def main() -> None:
    """Entrypoint coroutine: build ONE ``Neo4jClient.from_env()`` for the whole
    process, install signal handlers on the running loop, run ``_loop``, and
    ALWAYS ``await neo.close()`` in a ``finally``. Stays on ONE asyncio loop so
    ``database.session``'s per-loop engine registry is stable."""
    neo = Neo4jClient.from_env()
    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)
    try:
        await _loop(neo, stop_event=stop_event)
    finally:
        await neo.close()


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(main())
