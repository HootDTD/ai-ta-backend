"""APOLLO_TRANSCRIPT_GRADER seam coverage for ``apollo/handlers/done.py``.

Same no-Docker harness as ``test_done_graph_grader_live.py``: every OLD-path
collaborator is mocked deterministically via ``_old_path_patches`` /
``_rerun_inputs`` (reused wholesale from ``test_done_shadow_flag``), Neo4j is
a MagicMock, and ``run_graph_simulation`` / ``write_artifacts`` are patched on
the ``done`` module so no real chain / DB write / live LLM runs.

Covers the flag-ON branch (``transcript_grader_enabled()`` ->
``_full_transcript`` -> ``compute_transcript_coverage``), the
``CoverageGradingError`` fallback branch (falls back to ``compute_coverage``
and records ``transcript_grader_failure``), and the flag-OFF branch, plus the
``grading_provenance`` fields (``grader_used`` / ``evidence_source`` /
``transcript_grader_failure``) in all three states.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.errors import CoverageGradingError
from apollo.handlers.tests.test_done_shadow_flag import _old_path_patches, _rerun_inputs

pytestmark = pytest.mark.unit

_VALID_VERDICT = {
    "per_step": {},
    "procedure_scores": {},
    "confidences": {},
    "negotiation_counts": {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0},
}


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    monkeypatch.delenv("APOLLO_TRANSCRIPT_GRADER", raising=False)
    monkeypatch.delenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", raising=False)
    monkeypatch.delenv("APOLLO_GRAPH_GRADER_LIVE", raising=False)
    monkeypatch.delenv("APOLLO_GRADING_ARTIFACT_ENABLED", raising=False)
    yield


def _extend_db_execute_for_full_transcript(db: MagicMock, *, transcript_rows: list) -> None:
    """``_old_path_patches`` wires ``db.execute`` with a 2-call side_effect
    (session, then attempt). When the transcript grader is ON and
    ``_full_transcript`` is NOT mocked, it issues a real 3rd ``db.execute``
    call whose result needs an ``.all()`` returning ``(role, content)`` row
    tuples. Extend the existing side_effect chain rather than replacing it so
    the first two calls keep resolving session/attempt exactly as before."""
    original_side_effect = db.execute.side_effect

    class _TranscriptResult:
        def all(self_inner):
            return transcript_rows

    calls = {"n": 0}

    async def _execute(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            return await original_side_effect(*args, **kwargs)
        return _TranscriptResult()

    db.execute = AsyncMock(side_effect=_execute)


async def _run(
    monkeypatch,
    *,
    transcript_grader: str | None,
    coverage_mock,
    full_transcript_return=None,
    compute_coverage_mock=None,
):
    """Drive ``handle_done`` through the OLD-path harness with the transcript
    grader flag set (or unset) and ``compute_transcript_coverage`` patched.

    When ``full_transcript_return`` is given, ``_full_transcript`` itself is
    also patched (used only for tests that don't care about exercising the
    real DB-backed helper); otherwise ``_full_transcript`` executes for real
    against the harness's mocked ``db.execute``.

    ``compute_coverage_mock``, when given, REPLACES the harness's own
    ``compute_coverage`` patch (entered last, so it wins over
    ``_old_path_patches``'s default ``AsyncMock(return_value={})``) — needed
    so callers can assert on call counts for the fallback / flag-off paths.
    """
    if transcript_grader is not None:
        monkeypatch.setenv("APOLLO_TRANSCRIPT_GRADER", transcript_grader)

    db, _sess, _attempt, patches = _old_path_patches()
    payload = {"declared_paths": [["a"]], "symbolic_mappings": {"d": "2*r"}}
    rerun = _rerun_inputs(problem_payload=payload)

    if full_transcript_return is None:
        _extend_db_execute_for_full_transcript(db, transcript_rows=[])
    else:
        patches.append(
            patch(
                "apollo.handlers.done._full_transcript",
                new=AsyncMock(return_value=full_transcript_return),
            )
        )

    if compute_coverage_mock is not None:
        patches.append(patch("apollo.handlers.done.compute_coverage", new=compute_coverage_mock))

    from apollo.handlers.done import handle_done

    with ExitStack() as stack:
        stack.enter_context(
            patch("apollo.handlers.done.compute_transcript_coverage", new=coverage_mock)
        )
        stack.enter_context(
            patch("apollo.handlers.done.build_rerun_inputs", new=AsyncMock(return_value=rerun))
        )
        for p in patches:
            stack.enter_context(p)
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    return out


async def test_flag_on_uses_transcript_grader_and_skips_compute_coverage(monkeypatch):
    """Flag ON: `_full_transcript` executes for real (a real SELECT against
    the harness DB via the extended `db.execute` side_effect), then
    `compute_transcript_coverage` is awaited once and `compute_coverage` is
    never called. Provenance reports the transcript lane."""
    coverage_mock = AsyncMock(return_value=_VALID_VERDICT)
    compute_coverage_mock = AsyncMock()
    out = await _run(
        monkeypatch,
        transcript_grader="1",
        coverage_mock=coverage_mock,
        compute_coverage_mock=compute_coverage_mock,
    )

    coverage_mock.assert_awaited_once()
    compute_coverage_mock.assert_not_awaited()

    provenance = out["grading_provenance"]
    assert provenance["grader_used"] == "llm_transcript"
    assert provenance["evidence_source"] == "transcript"
    assert provenance["transcript_grader_failure"] is None


async def test_flag_on_coverage_grading_error_falls_back_to_compute_coverage(monkeypatch):
    """Flag ON + `compute_transcript_coverage` raising `CoverageGradingError`:
    `handle_done` still succeeds (no re-raise), `compute_coverage` is called
    exactly once as the fallback, and provenance records the fallback lane
    plus a non-empty `transcript_grader_failure` string."""
    coverage_mock = AsyncMock(
        side_effect=CoverageGradingError(stage="adjudication", last_error="boom")
    )
    compute_coverage_mock = AsyncMock(return_value={})
    out = await _run(
        monkeypatch,
        transcript_grader="1",
        coverage_mock=coverage_mock,
        compute_coverage_mock=compute_coverage_mock,
    )

    coverage_mock.assert_awaited_once()
    compute_coverage_mock.assert_awaited_once()

    provenance = out["grading_provenance"]
    assert provenance["grader_used"] == "llm_fallback"
    assert provenance["evidence_source"] == "graph_nodes"
    assert isinstance(provenance["transcript_grader_failure"], str)
    assert provenance["transcript_grader_failure"] != ""
    assert "boom" in provenance["transcript_grader_failure"]


async def test_flag_off_never_calls_transcript_grader(monkeypatch):
    """Flag OFF (env unset): `compute_transcript_coverage` is never called
    (it is patched to raise `AssertionError` if invoked), `compute_coverage`
    serves the grade, and provenance reports the legacy graph-nodes lane."""
    coverage_mock = AsyncMock(side_effect=AssertionError("must not be called"))
    compute_coverage_mock = AsyncMock(return_value={})
    out = await _run(
        monkeypatch,
        transcript_grader=None,
        coverage_mock=coverage_mock,
        full_transcript_return=(),
        compute_coverage_mock=compute_coverage_mock,
    )

    coverage_mock.assert_not_awaited()
    compute_coverage_mock.assert_awaited_once()

    provenance = out["grading_provenance"]
    assert provenance["grader_used"] == "llm_fallback"
    assert provenance["evidence_source"] == "graph_nodes"
    assert provenance["transcript_grader_failure"] is None
