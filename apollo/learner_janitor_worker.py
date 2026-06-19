"""Dormant ``apollo-janitor`` worker process (WU-5B3b).

A thin, async-native poll loop that wraps the FROZEN ``drain_pending_attempts``
(WU-5B3a-1) behind the default-OFF ``APOLLO_LEARNER_JANITOR_ENABLED`` flag, with
cooperative SIGTERM/SIGINT shutdown.

This module owns ONLY the process shell:
  - the poll loop (``_loop`` / ``_run_one_iteration``),
  - the per-iteration flag read (``_janitor_enabled``),
  - the SIGTERM/SIGINT handler registration (``_install_signal_handlers``),
  - the single ``Neo4jClient`` lifetime (``main``),
  - the third Procfile process line.

It adds NO drain logic, NO claim/lease/backoff/dead-letter, NO SQL, NO migration.
The drain is FROZEN and reused by import. The LAYER3 belief-write interlock lives
INSIDE the drain — the worker gates ONLY on the janitor flag.

The worker runs entirely inside a SINGLE ``asyncio.run(main())`` loop so that
``database.session``'s ``id(loop)``-keyed engine registry resolves the SAME
engine for every drain phase. Do NOT bridge through the sync ``run_async``
daemon — that would put the drain on a different loop than the signal handlers.

Activation is a HUMAN deploy step (scale the Railway process from 0 replicas and
flip the flag); see ``docs/architecture/_overview.md``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from apollo.handlers.learner_janitor import (
    MAX_ATTEMPTS,  # REUSE — do NOT redefine the backoff cap
    _int_env,  # REUSE the frozen env-parse helper (avoids a duplicate)
    drain_pending_attempts,  # the FROZEN drain
)
from apollo.persistence.neo4j_client import Neo4jClient

# Default OFF EVERYWHERE — activation is a human calibration/deploy decision.
_JANITOR_ENABLED_FLAG: str = "APOLLO_LEARNER_JANITOR_ENABLED"

# Small, sequential sweep bounds LLM spend; poll interval >= per-row LLM time
# (the drain does ~2 LLM calls per row). Both env-overridable for tuning.
SWEEP_LIMIT: int = _int_env("APOLLO_JANITOR_SWEEP_LIMIT", 1)
POLL_SECONDS: int = _int_env("APOLLO_JANITOR_POLL_SECONDS", 60)

_LOG = logging.getLogger(__name__)


def _janitor_enabled() -> bool:
    """Mirror of ``done.py:_graph_sim_layer3_enabled`` — read per-iteration so a
    flag flip is observed without a process restart. Default OFF."""
    return os.environ.get(_JANITOR_ENABLED_FLAG, "").lower() in ("1", "true", "yes")


async def _run_one_iteration(neo, *, stop_event: asyncio.Event) -> None:
    """ONE loop pass — flag-read -> (drain | skip) -> sleep.

    Extracted from ``_loop`` so the loop BODY is unit-testable WITHOUT a process
    and never hides under the ``while``-shell pragma. The drain is wrapped in a
    ``try/except Exception`` (R3) so one bad sweep (e.g. a Neo4j blip) logs and
    is survived rather than killing the worker; the sleep still runs afterwards
    so a flag flip is picked up next pass. ``KeyboardInterrupt``/``SystemExit``
    are NOT ``Exception`` subclasses, so they still propagate.
    """
    if _janitor_enabled():
        try:
            result = await drain_pending_attempts(
                neo, limit=SWEEP_LIMIT, max_attempts=MAX_ATTEMPTS
            )
            _LOG.info(
                "apollo_janitor_sweep",
                extra={
                    "claimed": result.claimed,
                    "succeeded": result.succeeded,
                    "dead_lettered": result.dead_lettered,
                    "retried": result.retried,
                    "deferred": result.deferred,
                    "backlog_remaining": result.backlog_remaining,
                },
            )
        except Exception:  # noqa: BLE001 - one bad sweep must not kill the worker
            _LOG.exception("apollo_janitor_sweep_failed")
    else:
        _LOG.debug("apollo_janitor_disabled")  # flag OFF -> NO drain (early skip)
    # Cooperative sleep: still sleeps when disabled so a flag flip is picked up
    # next pass; granularity is POLL_SECONDS (shutdown latency accepted for v1).
    await asyncio.sleep(POLL_SECONDS)


async def _loop(neo, *, stop_event: asyncio.Event) -> None:
    """The poll loop.

    Cooperative cancel: ``stop_event`` is checked at the TOP of each pass, so a
    set event exits AFTER the current iteration (never mid-drain — the drain is
    awaited to completion inside ``_run_one_iteration`` before the next check).
    A row killed between sweeps re-drains safely via the drain's claim-lease.
    """
    _LOG.info(
        "apollo_janitor_worker_started",
        extra={"poll_seconds": POLL_SECONDS, "sweep_limit": SWEEP_LIMIT},
    )
    while not stop_event.is_set():  # pragma: no cover - loop shell (body is _run_one_iteration)
        await _run_one_iteration(neo, stop_event=stop_event)
    _LOG.info("apollo_janitor_worker_stopped")


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event
) -> None:
    """Register SIGINT + SIGTERM to set ``stop_event`` (cooperative cancel).

    ``stop_event.set`` (the bound, no-arg method) is the callback —
    ``add_signal_handler`` calls it with no args. Wrapped in try/except so
    Windows (ProactorEventLoop raises ``NotImplementedError``) and non-main
    threads (``RuntimeError``) fall back to a warning instead of crashing.
    """
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            _LOG.warning(
                "apollo_janitor_signal_handler_unsupported", extra={"signal": sig}
            )


async def main() -> None:
    """Entrypoint coroutine: build ONE ``Neo4jClient.from_env()`` for the whole
    process, install signal handlers on the running loop, run ``_loop``, and
    ALWAYS ``await neo.close()`` in a ``finally`` so a crash or a stop still
    releases the driver. Stays on ONE asyncio loop so ``database.session``'s
    per-loop engine registry is stable."""
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
