"""Apollo Neo4j degraded mode — `handle_done` (apollo/handlers/done.py).

The transcript grader is the sole (unconditional) grading lane, so a degraded
Neo4j never blocks grading: the grader reads the transcript, not the frozen
graph.

Test matrix (post flag-reset):
1. Degraded pre-freeze `read_graph` + transcript grader OK -> grade served and
   the canonical artifact is written (no false F from an empty graph).
2. Degraded pre-freeze read + a transcript-grader `CoverageGradingError` ->
   the error PROPAGATES (no legacy fallback / fabricated zero), so the
   CoverageGradingError -> 503 retryable handler fires.
3. `stamp_graded_at` raising `RetentionError` with a HEALTHY graph -> the
   response is still served (log-and-continue, UNCONDITIONAL per design
   contract 4c — not gated on a degraded read).

Reuses `_old_path_patches` per the repo's own convention.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from neo4j.exceptions import ServiceUnavailable

from apollo.errors import CoverageGradingError, RetentionError
from apollo.handlers.done import handle_done
from apollo.handlers.tests._done_fixtures import _old_path_patches

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    monkeypatch.delenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", raising=False)
    yield


# ---------------------------------------------------------------------------
# (1) Degraded read + transcript grader OK -> grade served, artifact written.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_graph_degraded_still_grades_via_transcript(monkeypatch):
    db, _sess, _attempt, patches = _old_path_patches()

    degraded_read = patch(
        "apollo.handlers.done.KGStore.read_graph",
        new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
    )
    transcript_coverage = patch(
        "apollo.handlers.done.compute_transcript_coverage_with_spans",
        new=AsyncMock(return_value=({"ok": True}, {})),
    )
    write_artifacts_mock = AsyncMock(return_value=None)
    write_artifacts = patch("apollo.handlers.done.write_artifacts", new=write_artifacts_mock)

    for p in patches:
        p.start()
    extra = [degraded_read, transcript_coverage, write_artifacts]
    for p in extra:
        p.start()
    try:
        out = await handle_done(db=db, neo=None, session_id=11)
    finally:
        for p in reversed(extra):
            p.stop()
        for p in reversed(patches):
            p.stop()

    # Grade served from the transcript lane despite the degraded read.
    assert out["coverage"] == {"ok": True}
    # Artifact capture is unconditional.
    write_artifacts_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# (2) Degraded read + transcript-grader CoverageGradingError -> propagates.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degraded_read_then_transcript_error_propagates(monkeypatch):
    db, _sess, _attempt, patches = _old_path_patches()

    degraded_read = patch(
        "apollo.handlers.done.KGStore.read_graph",
        new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
    )
    transcript_error = patch(
        "apollo.handlers.done.compute_transcript_coverage_with_spans",
        new=AsyncMock(
            side_effect=CoverageGradingError(stage="transcript_adjudication", last_error="down")
        ),
    )

    for p in patches:
        p.start()
    extra = [degraded_read, transcript_error]
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

    assert exc_info.value.stage == "transcript_adjudication"


# ---------------------------------------------------------------------------
# (3) stamp_graded_at raising RetentionError with a HEALTHY graph -> served.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stamp_graded_at_retention_error_healthy_graph_still_serves(monkeypatch):
    """UNCONDITIONAL catch (design contract 4c): even with a HEALTHY
    pre-freeze read, a stamp-time RetentionError must log-and-continue rather
    than 500 an already-committed grade."""
    db, _sess, _attempt, patches = _old_path_patches()

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

    # Grade served byte-identically to the golden OLD-path payload (topic
    # scoring is neutralized in the base golden, so served_rubric is rubric).
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["xp_earned"] == 10
