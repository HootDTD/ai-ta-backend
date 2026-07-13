"""Apollo Neo4j degraded mode — `handle_done` (apollo/handlers/done.py).

Test matrix (plan §10):
1. `read_graph` raises + transcript grader stubbed OK -> grade served, gate
   skipped, stamp skipped (log-and-continue), shadow-flag ON but the chain
   is skipped, `write_artifacts` receives
   `graph_failure="shadow_failure: neo4j_unavailable"`.
2. Degraded + transcript grader OFF -> `CoverageGradingError` propagates
   with `stage="kg_unavailable_fallback"` (assert `compute_coverage` is
   NEVER called — the empty-graph false-F silent downgrade the plan
   forbids).
3. `stamp_graded_at` raising `RetentionError` with a HEALTHY graph -> the
   response is still served (log-and-continue, UNCONDITIONAL per design
   contract 4c — not gated on `kg_degraded`).
4. Healthy path: the existing `test_done_shadow_flag.py` /
   `test_done_shadow_isolation.py` suites are the byte-identical regression
   gate (unchanged, run as part of the full apollo suite).

Reuses `_old_path_patches` from `test_done_shadow_flag.py` per the repo's
own convention (`test_done_shadow_isolation.py` does the same); the shadow
chain never runs far enough in these tests to need `_rerun_inputs` — it is
skipped entirely by the degraded early-branch under test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from neo4j.exceptions import ServiceUnavailable

from apollo.errors import CoverageGradingError, RetentionError
from apollo.handlers.done import handle_done
from apollo.handlers.tests.test_done_shadow_flag import _old_path_patches

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    for flag in (
        "APOLLO_GRAPH_SIM_SHADOW_ENABLED",
        "APOLLO_GRAPH_SIM_LIVE_ENABLED",
        "APOLLO_GRAPH_GRADER_LIVE",
        "APOLLO_GRADING_ARTIFACT_ENABLED",
        "APOLLO_GRAPH_SIM_LAYER3_ENABLED",
    ):
        monkeypatch.delenv(flag, raising=False)
    yield


# ---------------------------------------------------------------------------
# (1) read_graph degraded + transcript grader OK -> grade served, stamp
#     skipped, shadow chain skipped with the marker.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_graph_degraded_transcript_grader_ok_serves_grade(monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    monkeypatch.setenv("APOLLO_GRADING_ARTIFACT_ENABLED", "true")

    db, _sess, attempt, patches = _old_path_patches()

    # Override the read_graph patch from _old_path_patches with a degraded one.
    degraded_read = patch(
        "apollo.handlers.done.KGStore.read_graph",
        new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
    )
    transcript_coverage = patch(
        "apollo.handlers.done.compute_transcript_coverage",
        new=AsyncMock(return_value={"ok": True}),
    )
    transcript_flag = patch(
        "apollo.handlers.done.transcript_grader_enabled",
        return_value=True,
    )
    full_transcript = patch(
        "apollo.handlers.done._full_transcript",
        new=AsyncMock(return_value=()),
    )
    compute_coverage_spy = patch(
        "apollo.handlers.done.compute_coverage",
        new=AsyncMock(return_value={"MUST_NOT_RUN": True}),
    )
    write_artifacts_mock = AsyncMock(return_value=None)
    write_artifacts = patch("apollo.handlers.done.write_artifacts", new=write_artifacts_mock)
    project_mastery = patch(
        "apollo.handlers.done._project_mastery",
        new=AsyncMock(return_value=None),
    )

    # Start ALL of _old_path_patches, then start `degraded_read` AFTER it —
    # both patch the same `KGStore.read_graph` attribute, and the LAST patch
    # started wins (mock.patch restores in reverse-start order on stop, so
    # stopping `degraded_read` first correctly un-shadows the base patch).
    for p in patches:
        p.start()
    extra = [
        degraded_read,
        transcript_coverage,
        transcript_flag,
        full_transcript,
        compute_coverage_spy,
        write_artifacts,
        project_mastery,
    ]
    for p in extra:
        p.start()
    try:
        out = await handle_done(db=db, neo=None, session_id=11)
    finally:
        for p in reversed(extra):
            p.stop()
        for p in reversed(patches):
            p.stop()

    # Grade served (byte-identical shape to the OLD path).
    assert out["coverage"] == {"ok": True}
    assert "MUST_NOT_RUN" not in str(out["coverage"])

    # write_artifacts received the shadow-failure marker for the skipped chain.
    assert write_artifacts_mock.await_args.kwargs["graph_failure"] == (
        "shadow_failure: neo4j_unavailable"
    )
    assert write_artifacts_mock.await_args.kwargs["shadow"] is None


# ---------------------------------------------------------------------------
# (2) Degraded + transcript grader OFF -> CoverageGradingError propagates.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degraded_no_transcript_grader_raises_coverage_grading_error(monkeypatch):
    db, _sess, attempt, patches = _old_path_patches()

    degraded_read = patch(
        "apollo.handlers.done.KGStore.read_graph",
        new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
    )
    transcript_flag = patch(
        "apollo.handlers.done.transcript_grader_enabled",
        return_value=False,
    )
    compute_coverage_spy = AsyncMock(return_value={"MUST_NOT_RUN": True})
    compute_coverage_patch = patch(
        "apollo.handlers.done.compute_coverage",
        new=compute_coverage_spy,
    )

    for p in patches:
        p.start()
    extra = [degraded_read, transcript_flag, compute_coverage_patch]
    for p in extra:
        p.start()
    try:
        with pytest.raises(CoverageGradingError) as exc_info:
            await handle_done(db=db, neo=None, session_id=11)
    finally:
        for p in reversed(extra):
            p.stop()
        for p in reversed(patches):
            p.stop()

    assert exc_info.value.stage == "kg_unavailable_fallback"
    compute_coverage_spy.assert_not_called()


# ---------------------------------------------------------------------------
# (3) stamp_graded_at raising RetentionError with a HEALTHY graph -> served.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stamp_graded_at_retention_error_healthy_graph_still_serves(monkeypatch):
    """UNCONDITIONAL catch (design contract 4c): even with a HEALTHY
    pre-freeze read (kg_degraded=False), a stamp-time RetentionError must
    log-and-continue rather than 500 an already-committed grade."""
    db, _sess, attempt, patches = _old_path_patches()

    failing_stamp = patch(
        "apollo.handlers.done.KGStore.stamp_graded_at",
        new=AsyncMock(side_effect=RetentionError(attempt_id=99, last_error="mid-pipeline drop")),
    )

    for p in patches:
        p.start()
    failing_stamp.start()
    try:
        out = await handle_done(db=db, neo=None, session_id=11)
    finally:
        failing_stamp.stop()
        for p in reversed(patches):
            p.stop()

    # Grade served byte-identically to the golden OLD-path payload.
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["xp_earned"] == 10
