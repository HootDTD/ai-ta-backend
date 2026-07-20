"""Unit tests for the dormant ``apollo-provision`` worker (WU-3B2g).

Pure-unit: NO DB, NO Neo4j, NO network. ``claim_provisioning_job`` /
``run_provisioning`` / ``complete_job`` / ``fail_job`` / ``_reap_expired`` are
mocked; ``asyncio.sleep`` is patched to an ``AsyncMock`` so tests never wait
``POLL_SECONDS``. Signal registration uses a fake loop (the suite runs on Windows
where ``add_signal_handler`` raises ``NotImplementedError``).

Mirrors ``apollo/tests/test_learner_janitor_worker.py`` 1:1 — the worker shell is
the SAME dormant-worker template, swapping the drain body for
reap+claim+run_provisioning+terminal-decision.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import signal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import apollo.provision_worker as worker
from apollo.provisioning.orchestrator import ProvisioningOutcome
from apollo.provisioning.queue import ClaimedJob

# pytest.ini sets asyncio_mode = auto.

_CLAIMED = ClaimedJob(
    job_id=11,
    search_space_id=1,
    document_id=2,
    ingest_run_id=3,
    attempt_count=1,
)
_SUCCEEDED = ProvisioningOutcome(
    run_id=3,
    status="succeeded",
    n_questions_scraped=1,
    n_promoted=1,
    n_rejected=0,
    n_dedup_merged=0,
)
_FAILED = ProvisioningOutcome(
    run_id=3,
    status="failed",
    n_questions_scraped=0,
    n_promoted=0,
    n_rejected=0,
    n_dedup_merged=0,
)


class _FakeNeo:
    """A trivial sentinel — the worker only hands ``neo`` to (mocked) collaborators."""


class _FakeLoop:
    def __init__(self, raise_exc: BaseException | None = None) -> None:
        self.registered: list[tuple[int, object]] = []
        self._raise_exc = raise_exc

    def add_signal_handler(self, sig, cb):  # noqa: ANN001 - test fake
        if self._raise_exc is not None:
            raise self._raise_exc
        self.registered.append((sig, cb))


# --------------------------------------------------------------------------- #
# Flag-read tests
# --------------------------------------------------------------------------- #
def test_autoprovision_enabled_default_off(monkeypatch):
    monkeypatch.delenv(worker._AUTOPROVISION_ENABLED_FLAG, raising=False)
    assert worker._autoprovision_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "YeS"])
def test_autoprovision_enabled_truthy(monkeypatch, raw):
    monkeypatch.setenv(worker._AUTOPROVISION_ENABLED_FLAG, raw)
    assert worker._autoprovision_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "", "no", "off"])
def test_autoprovision_enabled_falsey(monkeypatch, raw):
    monkeypatch.setenv(worker._AUTOPROVISION_ENABLED_FLAG, raw)
    assert worker._autoprovision_enabled() is False


def test_flag_string_and_defaults():
    assert worker._AUTOPROVISION_ENABLED_FLAG == "APOLLO_AUTOPROVISION_ENABLED"
    assert worker.SWEEP_LIMIT == 1
    assert worker.POLL_SECONDS == 60
    assert worker.LEASE_SECONDS == 1800


# --------------------------------------------------------------------------- #
# T-WK6 — env read EACH call (no cached flag)
# --------------------------------------------------------------------------- #
def test_autoprovision_enabled_reads_env_each_call(monkeypatch):
    monkeypatch.delenv(worker._AUTOPROVISION_ENABLED_FLAG, raising=False)
    assert worker._autoprovision_enabled() is False
    monkeypatch.setenv(worker._AUTOPROVISION_ENABLED_FLAG, "1")
    assert worker._autoprovision_enabled() is True


# --------------------------------------------------------------------------- #
# T-WK1 — flag OFF: NO reap, NO claim, still sleeps (the load-bearing non-regr.)
# --------------------------------------------------------------------------- #
async def test_iteration_skips_when_flag_off(monkeypatch):
    monkeypatch.delenv(worker._AUTOPROVISION_ENABLED_FLAG, raising=False)
    reap = AsyncMock(return_value=0)
    drain = AsyncMock(return_value=None)
    sleep = AsyncMock()
    monkeypatch.setattr(worker, "_reap_expired", reap)
    monkeypatch.setattr(worker, "_drain_one", drain)
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())

    reap.assert_not_awaited()
    drain.assert_not_awaited()
    sleep.assert_awaited_once_with(worker.POLL_SECONDS)


# --------------------------------------------------------------------------- #
# T-WK2 — flag ON: drains, logs the sweep line
# --------------------------------------------------------------------------- #
async def test_iteration_drains_when_flag_on(monkeypatch, caplog):
    monkeypatch.setenv(worker._AUTOPROVISION_ENABLED_FLAG, "1")
    reap = AsyncMock(return_value=0)
    drain = AsyncMock(return_value=_SUCCEEDED)
    sleep = AsyncMock()
    monkeypatch.setattr(worker, "_reap_expired", reap)
    monkeypatch.setattr(worker, "_drain_one", drain)
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    with caplog.at_level(logging.INFO, logger=worker.__name__):
        await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())

    drain.assert_awaited_once()
    sweep = [r for r in caplog.records if r.message == "apollo_provision_sweep"]
    assert len(sweep) == 1
    assert sweep[0].run_id == _SUCCEEDED.run_id
    assert sweep[0].status == "succeeded"
    assert sweep[0].n_promoted == 1
    sleep.assert_awaited_once_with(worker.POLL_SECONDS)


# --------------------------------------------------------------------------- #
# T-WK4 — reap runs BEFORE claim (§9 OPS-5 ordering)
# --------------------------------------------------------------------------- #
async def test_iteration_reaps_before_claim(monkeypatch):
    monkeypatch.setenv(worker._AUTOPROVISION_ENABLED_FLAG, "1")
    order: list[str] = []

    async def _reap(_factory):  # noqa: ANN001
        order.append("reap")
        return 0

    async def _drain(neo, *, session_factory, metered_chat_factory):  # noqa: ANN001
        order.append("drain")
        return None

    monkeypatch.setattr(worker, "_reap_expired", _reap)
    monkeypatch.setattr(worker, "_drain_one", _drain)
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())

    await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())

    assert order == ["reap", "drain"]


# --------------------------------------------------------------------------- #
# T-WK5 — one bad sweep is survived; SystemExit propagates
# --------------------------------------------------------------------------- #
async def test_iteration_survives_drain_exception(monkeypatch, caplog):
    monkeypatch.setenv(worker._AUTOPROVISION_ENABLED_FLAG, "1")
    monkeypatch.setattr(worker, "_reap_expired", AsyncMock(return_value=0))
    monkeypatch.setattr(worker, "_drain_one", AsyncMock(side_effect=RuntimeError("boom")))
    sleep = AsyncMock()
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())

    sleep.assert_awaited_once_with(worker.POLL_SECONDS)
    assert any(
        r.message == "apollo_provision_sweep_failed" and r.levelno >= logging.ERROR
        for r in caplog.records
    )


async def test_iteration_does_not_swallow_system_exit(monkeypatch):
    monkeypatch.setenv(worker._AUTOPROVISION_ENABLED_FLAG, "1")
    monkeypatch.setattr(worker, "_reap_expired", AsyncMock(return_value=0))
    monkeypatch.setattr(worker, "_drain_one", AsyncMock(side_effect=SystemExit()))
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())

    with pytest.raises(SystemExit):
        await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())


# --------------------------------------------------------------------------- #
# _drain_one — terminal decision
# --------------------------------------------------------------------------- #
async def test_drain_one_completes_on_succeeded(monkeypatch):
    """T-WK2 detail — a succeeded outcome -> complete_job (not fail_job)."""
    claim = AsyncMock(return_value=_CLAIMED)
    run = AsyncMock(return_value=_SUCCEEDED)
    complete = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(worker, "claim_provisioning_job", claim)
    monkeypatch.setattr(worker, "run_provisioning", run)
    monkeypatch.setattr(worker, "complete_job", complete)
    monkeypatch.setattr(worker, "fail_job", fail)

    outcome = await worker._drain_one(
        _FakeNeo(),
        session_factory=_fake_session_factory(),
        metered_chat_factory=_fake_metered_factory,
    )

    assert outcome is _SUCCEEDED
    complete.assert_awaited_once()
    fail.assert_not_awaited()


async def test_drain_one_fails_job_on_failed_outcome(monkeypatch):
    """T-WK3 — a failed outcome -> fail_job (not complete_job)."""
    monkeypatch.setattr(worker, "claim_provisioning_job", AsyncMock(return_value=_CLAIMED))
    monkeypatch.setattr(worker, "run_provisioning", AsyncMock(return_value=_FAILED))
    complete = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(worker, "complete_job", complete)
    monkeypatch.setattr(worker, "fail_job", fail)

    outcome = await worker._drain_one(
        _FakeNeo(),
        session_factory=_fake_session_factory(),
        metered_chat_factory=_fake_metered_factory,
    )

    assert outcome is _FAILED
    fail.assert_awaited_once()
    complete.assert_not_awaited()


async def test_drain_one_returns_none_when_nothing_claimable(monkeypatch):
    monkeypatch.setattr(worker, "claim_provisioning_job", AsyncMock(return_value=None))
    run = AsyncMock()
    monkeypatch.setattr(worker, "run_provisioning", run)

    outcome = await worker._drain_one(
        _FakeNeo(),
        session_factory=_fake_session_factory(),
        metered_chat_factory=_fake_metered_factory,
    )

    assert outcome is None
    run.assert_not_awaited()


# --------------------------------------------------------------------------- #
# _default_metered_factory — builds a MeteredChat bound to the run row
# --------------------------------------------------------------------------- #
def test_default_metered_factory_binds_run_and_document(monkeypatch):
    from apollo.provisioning import metered_chat as mc

    class _Run:
        id = 5
        llm_calls = 0
        llm_tokens_in = 0
        llm_tokens_out = 0
        llm_cost_usd = 0

    # Avoid constructing a real OpenAI client (no key in CI) — stub the default.
    sentinel = object()
    monkeypatch.setattr(mc, "_make_default_client", lambda: sentinel)

    metered = worker._default_metered_factory(ingest_run=_Run(), document_id=9)
    assert isinstance(metered, mc.MeteredChat)


# --------------------------------------------------------------------------- #
# T-WK7 — signal-handler registration
# --------------------------------------------------------------------------- #
def test_install_signal_handlers_registers_sigint_sigterm():
    stop_event = asyncio.Event()
    fake_loop = _FakeLoop()

    worker._install_signal_handlers(fake_loop, stop_event)

    registered = {sig for sig, _ in fake_loop.registered}
    assert registered == {signal.SIGINT, signal.SIGTERM}
    for _, cb in fake_loop.registered:
        cb()
    assert stop_event.is_set()


def test_install_signal_handlers_tolerates_not_implemented(caplog):
    stop_event = asyncio.Event()
    fake_loop = _FakeLoop(raise_exc=NotImplementedError())

    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        worker._install_signal_handlers(fake_loop, stop_event)

    assert any(r.message == "apollo_provision_signal_handler_unsupported" for r in caplog.records)
    assert not stop_event.is_set()


# --------------------------------------------------------------------------- #
# _loop shell
# --------------------------------------------------------------------------- #
async def test_loop_exits_when_stop_event_preset(monkeypatch, caplog):
    stop_event = asyncio.Event()
    stop_event.set()
    run_one = AsyncMock()
    monkeypatch.setattr(worker, "_run_one_iteration", run_one)

    with caplog.at_level(logging.INFO, logger=worker.__name__):
        await worker._loop(_FakeNeo(), stop_event=stop_event)

    run_one.assert_not_awaited()
    messages = [r.message for r in caplog.records]
    assert "apollo_provision_worker_started" in messages
    assert "apollo_provision_worker_stopped" in messages


async def test_loop_runs_body_then_stops(monkeypatch):
    stop_event = asyncio.Event()

    async def _one_pass(neo, *, stop_event):  # noqa: ANN001 - test fake
        stop_event.set()

    run_one = AsyncMock(side_effect=_one_pass)
    monkeypatch.setattr(worker, "_run_one_iteration", run_one)

    await worker._loop(_FakeNeo(), stop_event=stop_event)

    run_one.assert_awaited_once()


# --------------------------------------------------------------------------- #
# T-WK8 — main() closes neo in finally
# --------------------------------------------------------------------------- #
async def test_main_builds_runs_and_closes(monkeypatch):
    fake_neo = _FakeNeo()
    fake_neo.close = AsyncMock()
    install = []

    def _record_install(loop, stop_event):  # noqa: ANN001 - test fake
        install.append((loop, stop_event))

    monkeypatch.setattr(worker.Neo4jClient, "from_env", staticmethod(lambda: fake_neo))
    loop_mock = AsyncMock()
    monkeypatch.setattr(worker, "_loop", loop_mock)
    monkeypatch.setattr(worker, "_install_signal_handlers", _record_install)

    await worker.main()

    assert len(install) == 1
    loop_mock.assert_awaited_once()
    fake_neo.close.assert_awaited_once()


async def test_main_closes_neo_on_loop_error(monkeypatch):
    fake_neo = _FakeNeo()
    fake_neo.close = AsyncMock()
    monkeypatch.setattr(worker.Neo4jClient, "from_env", staticmethod(lambda: fake_neo))
    monkeypatch.setattr(worker, "_loop", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(worker, "_install_signal_handlers", lambda loop, stop: None)

    with pytest.raises(RuntimeError):
        await worker.main()

    fake_neo.close.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Procfile guard
# --------------------------------------------------------------------------- #
def test_procfile_declares_apollo_provision_process():
    repo_root = Path(__file__).resolve().parents[2]
    procfile = repo_root / "Procfile"
    assert procfile.exists(), f"Procfile not found at {procfile}"
    text = procfile.read_text(encoding="utf-8")
    assert "apollo-provision: python -m apollo.provision_worker" in text
    assert importlib.util.find_spec("apollo.provision_worker") is not None


# --------------------------------------------------------------------------- #
# Test fakes for the session/metered factories the worker threads through.
# --------------------------------------------------------------------------- #
def _fake_session_factory():
    import contextlib

    @contextlib.asynccontextmanager
    async def _factory():
        yield AsyncMock()

    return _factory


def _fake_metered_factory(*, ingest_run, document_id):  # noqa: ANN001
    return AsyncMock()


# --------------------------------------------------------------------------- #
# _drain_one_rehoming — the always-on typed-problem re-homing drain
# --------------------------------------------------------------------------- #
from apollo.provisioning.authored_sets.rehoming import ClaimedRehoming  # noqa: E402

_CLAIMED_REHOMING = ClaimedRehoming(
    job_id=7,
    problem_id=42,
    search_space_id=1,
    requested_concept_id=None,
    attempt_count=1,
)


async def test_drain_one_rehoming_completes_on_success(monkeypatch):
    monkeypatch.setattr(worker, "claim_rehoming_job", AsyncMock(return_value=_CLAIMED_REHOMING))
    monkeypatch.setattr(worker, "_tag_mint_chat_fn", lambda metered: metered)
    monkeypatch.setattr(worker, "embed_text", lambda _t: [0.0])
    monkeypatch.setattr(worker, "run_rehoming", AsyncMock(return_value=True))
    complete = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(worker, "complete_rehoming_job", complete)
    monkeypatch.setattr(worker, "fail_rehoming_job", fail)

    claimed = await worker._drain_one_rehoming(
        _FakeNeo(),
        session_factory=_fake_session_factory(),
        metered_chat_factory=_fake_metered_factory,
    )

    assert claimed is _CLAIMED_REHOMING
    complete.assert_awaited_once()
    fail.assert_not_awaited()


async def test_drain_one_rehoming_resolves_requested_concept(monkeypatch):
    claimed_with_concept = ClaimedRehoming(
        job_id=8, problem_id=43, search_space_id=1, requested_concept_id=99, attempt_count=1
    )
    monkeypatch.setattr(worker, "claim_rehoming_job", AsyncMock(return_value=claimed_with_concept))
    monkeypatch.setattr(worker, "_tag_mint_chat_fn", lambda metered: metered)
    monkeypatch.setattr(worker, "embed_text", lambda _t: [0.0])
    captured: dict = {}

    async def _run(_db, _neo, *, problem_id, chat_fn, embed_fn, resolved_concept, job_id):
        captured["resolved"] = resolved_concept
        return True

    monkeypatch.setattr(worker, "run_rehoming", _run)
    monkeypatch.setattr(worker, "complete_rehoming_job", AsyncMock())
    monkeypatch.setattr(worker, "fail_rehoming_job", AsyncMock())

    await worker._drain_one_rehoming(
        _FakeNeo(),
        session_factory=_fake_session_factory(),
        metered_chat_factory=_fake_metered_factory,
    )

    assert captured["resolved"] is not None
    assert captured["resolved"].concept_id == 99


async def test_drain_one_rehoming_fails_job_on_failure(monkeypatch):
    monkeypatch.setattr(worker, "claim_rehoming_job", AsyncMock(return_value=_CLAIMED_REHOMING))
    monkeypatch.setattr(worker, "_tag_mint_chat_fn", lambda metered: metered)
    monkeypatch.setattr(worker, "embed_text", lambda _t: [0.0])
    monkeypatch.setattr(worker, "run_rehoming", AsyncMock(return_value=False))
    complete = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(worker, "complete_rehoming_job", complete)
    monkeypatch.setattr(worker, "fail_rehoming_job", fail)

    await worker._drain_one_rehoming(
        _FakeNeo(),
        session_factory=_fake_session_factory(),
        metered_chat_factory=_fake_metered_factory,
    )

    fail.assert_awaited_once()
    complete.assert_not_awaited()


async def test_drain_one_rehoming_returns_none_when_nothing_claimable(monkeypatch):
    monkeypatch.setattr(worker, "claim_rehoming_job", AsyncMock(return_value=None))
    run = AsyncMock()
    monkeypatch.setattr(worker, "run_rehoming", run)

    claimed = await worker._drain_one_rehoming(
        _FakeNeo(),
        session_factory=_fake_session_factory(),
        metered_chat_factory=_fake_metered_factory,
    )

    assert claimed is None
    run.assert_not_awaited()


async def test_iteration_runs_rehoming_sweep_when_flag_off(monkeypatch, caplog):
    """Re-homing drains even with autoprovision OFF, and logs its sweep line."""
    monkeypatch.delenv(worker._AUTOPROVISION_ENABLED_FLAG, raising=False)
    monkeypatch.setattr(worker, "_reap_expired", AsyncMock(return_value=0))
    monkeypatch.setattr(worker, "_drain_one", AsyncMock(return_value=None))
    rehome = AsyncMock(return_value=_CLAIMED_REHOMING)
    monkeypatch.setattr(worker, "_drain_one_rehoming", rehome)
    sleep = AsyncMock()
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    with caplog.at_level(logging.INFO, logger=worker.__name__):
        await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())

    rehome.assert_awaited_once()
    sweep = [r for r in caplog.records if r.message == "apollo_rehoming_sweep"]
    assert len(sweep) == 1 and sweep[0].job_id == _CLAIMED_REHOMING.job_id
    sleep.assert_awaited_once_with(worker.POLL_SECONDS)


async def test_iteration_survives_rehoming_sweep_exception(monkeypatch, caplog):
    monkeypatch.delenv(worker._AUTOPROVISION_ENABLED_FLAG, raising=False)
    monkeypatch.setattr(worker, "_drain_one_rehoming", AsyncMock(side_effect=RuntimeError("boom")))
    sleep = AsyncMock()
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())

    sleep.assert_awaited_once_with(worker.POLL_SECONDS)
    assert any(r.message == "apollo_rehoming_sweep_failed" for r in caplog.records)
