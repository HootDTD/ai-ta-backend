"""Task 9 — grading-time NLI injection behind APOLLO_NLI_ENABLED.

Tests for the ``_nli_context()`` helper and the module-global singleton in
``apollo.handlers.done_grading``.  NO real model is ever loaded — ``_build_adjudicator``
is always patched to return a lightweight sentinel.

Singleton-isolation contract
----------------------------
``_NLI_ADJUDICATOR`` is a mutable module global mutated via ``global``.
``monkeypatch.setattr`` restores module attributes, so *each* test that touches
the flag-on path resets ``_NLI_ADJUDICATOR`` to ``None`` at the start to avoid
any ordering dependency on other tests in the suite.
"""

from __future__ import annotations

import threading

import pytest

import apollo.handlers.done_grading as dg
from apollo.handlers.tests.test_done_grading_unit import (
    _all_callee_patches,
    _Attempt,
    _db,
    _payload,
    _read_transcript_patch,
    _Sess,
)
from apollo.ontology import KGGraph
from apollo.ontology.nodes import EquationNode
from apollo.resolution.nli_resolution import NLIContext

pytestmark = __import__("pytest").mark.unit


def test_nli_context_none_when_flag_explicitly_off(monkeypatch):
    """The KILL SWITCH: with APOLLO_NLI_ENABLED=0, ``_nli_context()`` returns
    ``None`` and grading is byte-identical to the pre-NLI path. (NLI is now
    default-ON, so OFF must be set explicitly.)"""
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "0")
    assert dg._nli_context() is None


def test_nli_context_built_when_flag_unset(monkeypatch):
    """NLI is DEFAULT-ON: with APOLLO_NLI_ENABLED UNSET and a patched builder,
    ``_nli_context()`` returns a populated ``NLIContext`` (the enablement wiring)."""
    monkeypatch.delenv("APOLLO_NLI_ENABLED", raising=False)
    monkeypatch.setattr(dg, "_NLI_ADJUDICATOR", None)
    sentinel = object()
    monkeypatch.setattr(dg, "_build_adjudicator", lambda: sentinel)
    ctx = dg._nli_context()
    assert isinstance(ctx, NLIContext)
    assert ctx.nli is sentinel


def test_nli_context_built_when_flag_on(monkeypatch):
    """With APOLLO_NLI_ENABLED=1 and a patched builder, ``_nli_context()``
    returns a populated ``NLIContext`` whose ``.nli`` is not None."""
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "1")
    # Reset singleton so the builder is called fresh in THIS test.
    monkeypatch.setattr(dg, "_NLI_ADJUDICATOR", None)
    sentinel = object()
    monkeypatch.setattr(dg, "_build_adjudicator", lambda: sentinel)
    ctx = dg._nli_context()
    assert isinstance(ctx, NLIContext)
    assert ctx.nli is sentinel


def test_nli_context_reuses_singleton(monkeypatch):
    """Calling ``_nli_context()`` twice with the flag on reuses the SAME
    adjudicator object — the builder must be called exactly once.

    This test covers the ``if _NLI_ADJUDICATOR is None:`` False branch,
    which is otherwise unreachable by the two tests above.
    """
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "1")
    # Reset so we start from a known-empty state.
    monkeypatch.setattr(dg, "_NLI_ADJUDICATOR", None)

    call_count = 0

    def counting_builder():
        nonlocal call_count
        call_count += 1
        return object()

    monkeypatch.setattr(dg, "_build_adjudicator", counting_builder)

    ctx1 = dg._nli_context()
    ctx2 = dg._nli_context()

    # Builder called only once (singleton reuse).
    assert call_count == 1
    # Both calls return a context whose .nli is the SAME instance.
    assert ctx1 is not None and ctx2 is not None
    assert ctx1.nli is ctx2.nli


# ---------------------------------------------------------------------------
# L4 — missing ``transformers`` degrades gracefully (never re-arms the retry).
# ---------------------------------------------------------------------------


def test_nli_context_degrades_on_missing_transformers_at_construction(monkeypatch, caplog):
    """L4: if adjudicator CONSTRUCTION itself fails on ImportError/
    ModuleNotFoundError (a missing ``transformers`` install), ``_nli_context()``
    returns ``None`` (degrade to no-NLI) instead of propagating the exception."""
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "1")
    monkeypatch.setattr(dg, "_NLI_ADJUDICATOR", None)
    monkeypatch.setattr(dg, "_NLI_IMPORT_UNAVAILABLE_LOGGED", False)

    def boom():
        raise ModuleNotFoundError("No module named 'transformers'")

    monkeypatch.setattr(dg, "_build_adjudicator", boom)

    with caplog.at_level("WARNING", logger="apollo.handlers.done_grading"):
        ctx = dg._nli_context()

    assert ctx is None
    assert "apollo_nli_transformers_unavailable" in caplog.text


def test_nli_import_failure_logged_only_once(monkeypatch, caplog):
    """The missing-``transformers`` warning fires ONCE per process, not once
    per call, so a sustained-missing install doesn't spam every request."""
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "1")
    monkeypatch.setattr(dg, "_NLI_IMPORT_UNAVAILABLE_LOGGED", False)

    with caplog.at_level("WARNING", logger="apollo.handlers.done_grading"):
        dg._log_nli_import_failure_once(ImportError("boom"))
        dg._log_nli_import_failure_once(ImportError("boom again"))

    records = [r for r in caplog.records if "apollo_nli_transformers_unavailable" in r.getMessage()]
    assert len(records) == 1


# ---------------------------------------------------------------------------
# M3 — grading-path node budget cap (mirrors the chat cap).
# ---------------------------------------------------------------------------


def test_nli_grading_node_cap_default_when_unset(monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_GRADING_MAX_NODES", raising=False)
    assert dg._nli_grading_node_cap() == 40


def test_nli_grading_node_cap_reads_env(monkeypatch):
    monkeypatch.setenv("APOLLO_NLI_GRADING_MAX_NODES", "3")
    assert dg._nli_grading_node_cap() == 3


def test_nli_grading_node_cap_falls_back_on_malformed_env(monkeypatch):
    monkeypatch.setenv("APOLLO_NLI_GRADING_MAX_NODES", "not-a-number")
    assert dg._nli_grading_node_cap() == 40


# ---------------------------------------------------------------------------
# M3(a) — resolve_attempt offloaded to a worker thread when NLI is active;
# inline (byte-identical to the pre-NLI path) when it is not.
# ---------------------------------------------------------------------------


class _FakeNLI:
    """A non-None sentinel standing in for the real adjudicator — never
    actually invoked in these wiring tests."""


def _sentinel_nli_ctx() -> NLIContext:
    from apollo.resolution.embedding import CandidateEmbeddingCache, default_embedder
    from apollo.resolution.nli_config import load_nli_params

    return NLIContext(
        nli=_FakeNLI(),
        embedder=default_embedder,
        cache=CandidateEmbeddingCache(),
        params=load_nli_params(),
    )


@pytest.mark.asyncio
async def test_resolve_attempt_async_inline_when_nli_inactive(monkeypatch):
    """nli_ctx=None -> resolve_attempt runs INLINE (same thread, no to_thread
    hop) — byte-identical wiring to the pre-NLI code path."""
    main_ident = threading.get_ident()
    captured: dict = {}

    def fake_resolve(student_graph, candidates, **kwargs):
        captured["ident"] = threading.get_ident()
        captured["kwargs"] = kwargs
        return "resolved-inline"

    monkeypatch.setattr(dg, "resolve_attempt", fake_resolve)

    result = await dg._resolve_attempt_async(
        KGGraph(),
        (),
        confirmed_resolutions={},
        fuzzy_threshold=0.9,
        symbolic_mappings={},
        nli_ctx=None,
    )

    assert result == "resolved-inline"
    assert captured["ident"] == main_ident
    assert captured["kwargs"]["nli_ctx"] is None


@pytest.mark.asyncio
async def test_resolve_attempt_async_offloads_to_worker_thread_when_nli_active(monkeypatch):
    """nli_ctx active (nli is not None) -> the CPU-bound resolver call runs OFF
    the event-loop thread (asyncio.to_thread), never blocking the loop."""
    loop_ident = threading.get_ident()
    captured: dict = {}

    def fake_resolve(student_graph, candidates, **kwargs):
        captured["ident"] = threading.get_ident()
        captured["kwargs"] = kwargs
        return "resolved-offloaded"

    monkeypatch.setattr(dg, "resolve_attempt", fake_resolve)

    ctx = _sentinel_nli_ctx()
    result = await dg._resolve_attempt_async(
        KGGraph(),
        (),
        confirmed_resolutions={},
        fuzzy_threshold=0.9,
        symbolic_mappings={},
        nli_ctx=ctx,
    )

    assert result == "resolved-offloaded"
    assert captured["ident"] != loop_ident
    assert captured["kwargs"]["nli_ctx"] is ctx


@pytest.mark.asyncio
async def test_resolve_attempt_async_grade_math_identical_under_cap(monkeypatch):
    """M3: the offload boundary changes ONLY where the call runs, never what it
    returns — the same deterministic resolver call yields the same result
    whether inline (nli off) or offloaded (nli on)."""

    def deterministic_resolve(student_graph, candidates, **kwargs):
        # Pure function of the (non-NLI) inputs only — independent of thread.
        return {"n_candidates": len(candidates), "fuzzy_threshold": kwargs["fuzzy_threshold"]}

    monkeypatch.setattr(dg, "resolve_attempt", deterministic_resolve)

    inline_result = await dg._resolve_attempt_async(
        KGGraph(),
        ("c1", "c2"),
        confirmed_resolutions={},
        fuzzy_threshold=0.9,
        symbolic_mappings={},
        nli_ctx=None,
    )
    offloaded_result = await dg._resolve_attempt_async(
        KGGraph(),
        ("c1", "c2"),
        confirmed_resolutions={},
        fuzzy_threshold=0.9,
        symbolic_mappings={},
        nli_ctx=_sentinel_nli_ctx(),
    )

    assert inline_result == offloaded_result == {"n_candidates": 2, "fuzzy_threshold": 0.9}


@pytest.mark.asyncio
async def test_resolve_attempt_async_degrades_on_import_error(monkeypatch, caplog):
    """L4 (deep path): if the offloaded resolver call itself raises
    ImportError/ModuleNotFoundError (the lazy ``transformers`` load inside the
    real adjudicator's ``classify()``), degrade to a no-NLI retry rather than
    letting the failure propagate to the caller's NO-FALLBACK except clauses."""
    monkeypatch.setattr(dg, "_NLI_IMPORT_UNAVAILABLE_LOGGED", False)
    calls: list = []

    def flaky_resolve(student_graph, candidates, **kwargs):
        calls.append(kwargs["nli_ctx"])
        if kwargs["nli_ctx"] is not None:
            raise ModuleNotFoundError("No module named 'transformers'")
        return "resolved-without-nli"

    monkeypatch.setattr(dg, "resolve_attempt", flaky_resolve)

    with caplog.at_level("WARNING", logger="apollo.handlers.done_grading"):
        result = await dg._resolve_attempt_async(
            KGGraph(),
            (),
            confirmed_resolutions={},
            fuzzy_threshold=0.9,
            symbolic_mappings={},
            nli_ctx=_sentinel_nli_ctx(),
        )

    assert result == "resolved-without-nli"
    # First call attempted WITH nli_ctx (raised), second call fell back to None.
    assert len(calls) == 2
    assert calls[0] is not None
    assert calls[1] is None
    assert "apollo_nli_transformers_unavailable" in caplog.text


# ---------------------------------------------------------------------------
# End-to-end wiring through run_graph_simulation: cap + import-degrade never
# re-arm the retry loop and the happy path threads nli_ctx through unchanged.
# ---------------------------------------------------------------------------


def _graph_with_nodes(n: int) -> KGGraph:
    return KGGraph(
        nodes=[
            EquationNode(
                node_id=f"n{i}",
                attempt_id=1,
                source="parser",
                content={"symbolic": f"x{i}", "label": ""},
            )
            for i in range(n)
        ]
    )


@pytest.mark.asyncio
async def test_run_graph_simulation_caps_large_attempt_and_skips_nli(monkeypatch, caplog):
    """M3(b): an attempt with more student nodes than
    ``APOLLO_NLI_GRADING_MAX_NODES`` grades WITHOUT NLI (nli_ctx=None reaches
    resolve_attempt) even though the flag is on — never blocks on a huge
    utterance's worth of synchronous inference."""
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "1")
    monkeypatch.setenv("APOLLO_NLI_GRADING_MAX_NODES", "2")
    monkeypatch.setattr(dg, "_NLI_ADJUDICATOR", None)
    monkeypatch.setattr(dg, "_build_adjudicator", lambda: _FakeNLI())

    db = _db()
    patches, mocks = _all_callee_patches()
    sess = _Sess()
    attempt = _Attempt()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            with caplog.at_level("INFO", logger="apollo.handlers.done_grading"):
                await dg.run_graph_simulation(
                    db,
                    None,
                    attempt=attempt,
                    sess=sess,
                    student_graph=_graph_with_nodes(3),  # > cap of 2
                    problem_payload=_payload(),
                    old_rubric={"overall": {"score": 70, "letter": "B-"}},
                )
        finally:
            for p in reversed(patches):
                p.stop()

    rkwargs = mocks["resolve_attempt"].call_args.kwargs
    assert rkwargs["nli_ctx"] is None
    assert "nli_grading_skipped_budget" in caplog.text
    assert attempt.learner_update_pending is False


@pytest.mark.asyncio
async def test_run_graph_simulation_under_cap_threads_nli_ctx(monkeypatch):
    """Below the cap, nli_ctx is threaded through unchanged (the flag/cap gate
    only skips when OVER budget)."""
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "1")
    monkeypatch.setenv("APOLLO_NLI_GRADING_MAX_NODES", "10")
    monkeypatch.setattr(dg, "_NLI_ADJUDICATOR", None)
    monkeypatch.setattr(dg, "_build_adjudicator", lambda: _FakeNLI())

    db = _db()
    patches, mocks = _all_callee_patches()
    sess = _Sess()
    attempt = _Attempt()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            await dg.run_graph_simulation(
                db,
                None,
                attempt=attempt,
                sess=sess,
                student_graph=_graph_with_nodes(3),  # under the cap of 10
                problem_payload=_payload(),
                old_rubric={"overall": {"score": 70, "letter": "B-"}},
            )
        finally:
            for p in reversed(patches):
                p.stop()

    rkwargs = mocks["resolve_attempt"].call_args.kwargs
    assert rkwargs["nli_ctx"] is not None
    assert attempt.learner_update_pending is False


@pytest.mark.asyncio
async def test_run_graph_simulation_25_nodes_runs_nli_at_raised_default(monkeypatch):
    """Fix 1 (2026-07 routing fixes): a 25-node attempt (attempt-1 shape, per
    ``.superpowers/sdd/misc-node-routing-diagnosis.md`` Q3) was previously
    ALWAYS skipped by the old default cap of 15 — a whole-attempt binary
    switch, not per-node truncation. At the raised default of 40, NLI now
    threads through for this attempt with NO env override needed."""
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "1")
    monkeypatch.delenv("APOLLO_NLI_GRADING_MAX_NODES", raising=False)
    monkeypatch.setattr(dg, "_NLI_ADJUDICATOR", None)
    monkeypatch.setattr(dg, "_build_adjudicator", lambda: _FakeNLI())

    db = _db()
    patches, mocks = _all_callee_patches()
    sess = _Sess()
    attempt = _Attempt()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            await dg.run_graph_simulation(
                db,
                None,
                attempt=attempt,
                sess=sess,
                student_graph=_graph_with_nodes(25),  # under the new default cap of 40
                problem_payload=_payload(),
                old_rubric={"overall": {"score": 70, "letter": "B-"}},
            )
        finally:
            for p in reversed(patches):
                p.stop()

    rkwargs = mocks["resolve_attempt"].call_args.kwargs
    assert rkwargs["nli_ctx"] is not None
    assert attempt.learner_update_pending is False


@pytest.mark.asyncio
async def test_run_graph_simulation_import_failure_does_not_arm_retry(monkeypatch):
    """L4: a missing-``transformers`` failure surfacing from the resolver must
    NOT set learner_update_pending / re-raise — grading completes normally,
    degraded to no-NLI, instead of re-arming the retry loop forever."""
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "1")
    monkeypatch.setattr(dg, "_NLI_ADJUDICATOR", None)
    monkeypatch.setattr(dg, "_build_adjudicator", lambda: _FakeNLI())
    monkeypatch.setattr(dg, "_NLI_IMPORT_UNAVAILABLE_LOGGED", False)

    db = _db()
    patches, mocks = _all_callee_patches()

    def flaky_resolve(student_graph, candidates, **kwargs):
        if kwargs.get("nli_ctx") is not None:
            raise ModuleNotFoundError("No module named 'transformers'")
        return mocks["resolve_attempt"].return_value

    # Override the auto-patched MagicMock with real degrade-then-succeed logic.
    for p in patches:
        if p.attribute == "resolve_attempt":
            p.new = flaky_resolve

    sess = _Sess()
    attempt = _Attempt()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            result = await dg.run_graph_simulation(
                db,
                None,
                attempt=attempt,
                sess=sess,
                student_graph=_graph_with_nodes(1),
                problem_payload=_payload(),
                old_rubric={"overall": {"score": 70, "letter": "B-"}},
            )
        finally:
            for p in reversed(patches):
                p.stop()

    assert result is not None
    assert attempt.learner_update_pending is False
