"""Unit tests for the dormant ``apollo-janitor`` worker (WU-5B3b).

Pure-unit: NO DB, NO Neo4j, NO network. ``drain_pending_attempts`` is mocked
throughout (``AsyncMock`` returning a canned ``DrainResult``); ``asyncio.sleep``
is patched to an ``AsyncMock`` so tests never wait ``POLL_SECONDS``. Signal
registration is exercised with a fake loop — never a real OS signal (the suite
runs on Windows where ``add_signal_handler`` raises ``NotImplementedError``).
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import signal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import apollo.learner_janitor_worker as worker
from apollo.handlers.learner_janitor import MAX_ATTEMPTS, DrainResult

# Canned drain result reused across the body tests.
_FAKE_RESULT = DrainResult(
    claimed=1,
    succeeded=1,
    dead_lettered=0,
    retried=0,
    deferred=0,
    backlog_remaining=0,
)


class _FakeNeo:
    """The worker never calls methods on ``neo`` directly — it only hands it to
    the (mocked) drain. A trivial sentinel suffices."""


class _FakeLoop:
    """Records ``add_signal_handler(sig, cb)`` calls without touching a real
    event loop. ``_raise`` makes it simulate the Windows/Proactor case."""

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
def test_janitor_enabled_default_off(monkeypatch):
    monkeypatch.delenv(worker._JANITOR_ENABLED_FLAG, raising=False)
    assert worker._janitor_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "YeS"])
def test_janitor_enabled_truthy_values(monkeypatch, raw):
    monkeypatch.setenv(worker._JANITOR_ENABLED_FLAG, raw)
    assert worker._janitor_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "", "no", "off"])
def test_janitor_enabled_falsey_values(monkeypatch, raw):
    monkeypatch.setenv(worker._JANITOR_ENABLED_FLAG, raw)
    assert worker._janitor_enabled() is False


def test_flag_string_and_defaults():
    assert worker._JANITOR_ENABLED_FLAG == "APOLLO_LEARNER_JANITOR_ENABLED"
    assert worker.SWEEP_LIMIT == 1
    assert worker.POLL_SECONDS == 60


# --------------------------------------------------------------------------- #
# _run_one_iteration body tests (the coverage core)
# --------------------------------------------------------------------------- #
async def test_iteration_flag_off_skips_drain(monkeypatch):
    monkeypatch.delenv(worker._JANITOR_ENABLED_FLAG, raising=False)
    drain = AsyncMock(return_value=_FAKE_RESULT)
    sleep = AsyncMock()
    monkeypatch.setattr(worker, "drain_pending_attempts", drain)
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())

    drain.assert_not_awaited()
    sleep.assert_awaited_once_with(worker.POLL_SECONDS)


async def test_iteration_flag_on_calls_drain_with_limit_1(monkeypatch):
    monkeypatch.setenv(worker._JANITOR_ENABLED_FLAG, "1")
    drain = AsyncMock(return_value=_FAKE_RESULT)
    sleep = AsyncMock()
    monkeypatch.setattr(worker, "drain_pending_attempts", drain)
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)
    neo = _FakeNeo()

    await worker._run_one_iteration(neo, stop_event=asyncio.Event())

    drain.assert_awaited_once_with(
        neo, limit=worker.SWEEP_LIMIT, max_attempts=MAX_ATTEMPTS
    )
    # No user_id kwarg — the worker drains globally.
    _, kwargs = drain.call_args
    assert "user_id" not in kwargs
    assert kwargs["limit"] == 1
    sleep.assert_awaited_once_with(worker.POLL_SECONDS)


async def test_iteration_logs_drain_result(monkeypatch, caplog):
    monkeypatch.setenv(worker._JANITOR_ENABLED_FLAG, "1")
    monkeypatch.setattr(
        worker, "drain_pending_attempts", AsyncMock(return_value=_FAKE_RESULT)
    )
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())

    with caplog.at_level(logging.INFO, logger=worker.__name__):
        await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())

    sweep_records = [r for r in caplog.records if r.message == "apollo_janitor_sweep"]
    assert len(sweep_records) == 1
    rec = sweep_records[0]
    assert rec.claimed == _FAKE_RESULT.claimed
    assert rec.succeeded == _FAKE_RESULT.succeeded
    assert rec.dead_lettered == _FAKE_RESULT.dead_lettered
    assert rec.retried == _FAKE_RESULT.retried
    assert rec.deferred == _FAKE_RESULT.deferred
    assert rec.backlog_remaining == _FAKE_RESULT.backlog_remaining


async def test_iteration_flag_off_logs_disabled(monkeypatch, caplog):
    monkeypatch.delenv(worker._JANITOR_ENABLED_FLAG, raising=False)
    monkeypatch.setattr(worker, "drain_pending_attempts", AsyncMock())
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())

    with caplog.at_level(logging.DEBUG, logger=worker.__name__):
        await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())

    assert any(r.message == "apollo_janitor_disabled" for r in caplog.records)


async def test_iteration_drain_exception_is_swallowed_and_still_sleeps(
    monkeypatch, caplog
):
    """R3: one bad sweep must NOT kill the worker. The drain is wrapped in
    ``try/except Exception`` — the error is logged, then the loop still sleeps
    and ``_run_one_iteration`` returns normally."""
    monkeypatch.setenv(worker._JANITOR_ENABLED_FLAG, "1")
    boom = RuntimeError("neo4j blip")
    drain = AsyncMock(side_effect=boom)
    sleep = AsyncMock()
    monkeypatch.setattr(worker, "drain_pending_attempts", drain)
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)

    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        # Must NOT raise.
        await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())

    drain.assert_awaited_once()
    sleep.assert_awaited_once_with(worker.POLL_SECONDS)
    assert any(
        r.message == "apollo_janitor_sweep_failed" and r.levelno >= logging.ERROR
        for r in caplog.records
    )


async def test_iteration_does_not_swallow_keyboard_interrupt(monkeypatch):
    """A ``KeyboardInterrupt``/``SystemExit`` (not an ``Exception``) must
    propagate — the resilience wrapper catches ``Exception`` only."""
    monkeypatch.setenv(worker._JANITOR_ENABLED_FLAG, "1")
    drain = AsyncMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(worker, "drain_pending_attempts", drain)
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())

    with pytest.raises(KeyboardInterrupt):
        await worker._run_one_iteration(_FakeNeo(), stop_event=asyncio.Event())


# --------------------------------------------------------------------------- #
# _loop shell tests
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
    assert "apollo_janitor_worker_started" in messages
    assert "apollo_janitor_worker_stopped" in messages


async def test_loop_runs_body_then_stops(monkeypatch):
    stop_event = asyncio.Event()

    async def _one_pass(neo, *, stop_event):  # noqa: ANN001 - test fake
        stop_event.set()

    run_one = AsyncMock(side_effect=_one_pass)
    monkeypatch.setattr(worker, "_run_one_iteration", run_one)

    await worker._loop(_FakeNeo(), stop_event=stop_event)

    run_one.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Cooperative-cancel semantics
# --------------------------------------------------------------------------- #
async def test_stop_event_set_mid_iteration_finishes_current_drain(monkeypatch):
    """SIGTERM arriving mid-drain: the drain completes (not cancelled), and the
    loop exits AFTER the current iteration — never mid-row."""
    monkeypatch.setenv(worker._JANITOR_ENABLED_FLAG, "1")
    stop_event = asyncio.Event()

    async def _drain_then_signal(neo, *, limit, max_attempts):  # noqa: ANN001
        stop_event.set()  # SIGTERM arrives mid-drain
        return _FAKE_RESULT  # ... but the drain still returns its result

    drain = AsyncMock(side_effect=_drain_then_signal)
    monkeypatch.setattr(worker, "drain_pending_attempts", drain)
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())

    await worker._loop(_FakeNeo(), stop_event=stop_event)

    drain.assert_awaited_once()  # completed once, never cancelled, never re-run


# --------------------------------------------------------------------------- #
# Signal-handler tests
# --------------------------------------------------------------------------- #
def test_install_signal_handlers_registers_sigint_sigterm():
    stop_event = asyncio.Event()
    fake_loop = _FakeLoop()

    worker._install_signal_handlers(fake_loop, stop_event)

    registered_sigs = {sig for sig, _ in fake_loop.registered}
    assert registered_sigs == {signal.SIGINT, signal.SIGTERM}
    for _, cb in fake_loop.registered:
        cb()
    assert stop_event.is_set()


def test_install_signal_handlers_tolerates_not_implemented(caplog):
    stop_event = asyncio.Event()
    fake_loop = _FakeLoop(raise_exc=NotImplementedError())

    with caplog.at_level(logging.WARNING, logger=worker.__name__):
        # Must NOT raise.
        worker._install_signal_handlers(fake_loop, stop_event)

    assert any(
        r.message == "apollo_janitor_signal_handler_unsupported"
        for r in caplog.records
    )
    assert not stop_event.is_set()


# --------------------------------------------------------------------------- #
# main() lifetime tests
# --------------------------------------------------------------------------- #
async def test_main_builds_runs_and_closes(monkeypatch):
    fake_neo = _FakeNeo()
    fake_neo.close = AsyncMock()
    from_env = lambda: fake_neo  # noqa: E731 - terse test stub
    loop_mock = AsyncMock()
    install = []

    def _record_install(loop, stop_event):  # noqa: ANN001 - test fake
        install.append((loop, stop_event))

    monkeypatch.setattr(worker.Neo4jClient, "from_env", staticmethod(from_env))
    monkeypatch.setattr(worker, "_loop", loop_mock)
    monkeypatch.setattr(worker, "_install_signal_handlers", _record_install)

    await worker.main()

    assert len(install) == 1
    _, installed_stop = install[0]
    assert isinstance(installed_stop, asyncio.Event)
    loop_mock.assert_awaited_once()
    args, kwargs = loop_mock.call_args
    assert args[0] is fake_neo
    assert kwargs["stop_event"] is installed_stop
    fake_neo.close.assert_awaited_once()


async def test_main_closes_neo_on_loop_error(monkeypatch):
    fake_neo = _FakeNeo()
    fake_neo.close = AsyncMock()
    monkeypatch.setattr(
        worker.Neo4jClient, "from_env", staticmethod(lambda: fake_neo)
    )
    monkeypatch.setattr(worker, "_loop", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(worker, "_install_signal_handlers", lambda loop, stop: None)

    with pytest.raises(RuntimeError):
        await worker.main()

    fake_neo.close.assert_awaited_once()  # finally always releases the driver


# --------------------------------------------------------------------------- #
# Procfile guard (locks the +1 process line + module name)
# --------------------------------------------------------------------------- #
def test_procfile_declares_apollo_janitor_process():
    repo_root = Path(__file__).resolve().parents[2]
    procfile = repo_root / "Procfile"
    assert procfile.exists(), f"Procfile not found at {procfile}"
    text = procfile.read_text(encoding="utf-8")
    assert "apollo-janitor: python -m apollo.learner_janitor_worker" in text
    assert importlib.util.find_spec("apollo.learner_janitor_worker") is not None
