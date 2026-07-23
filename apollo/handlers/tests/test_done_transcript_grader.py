"""Transcript-grader lane coverage for ``apollo/handlers/done.py``.

The transcript grader is the SOLE, unconditional grading lane (2026-07 flag
reset — the transcript-grader flag was deleted). Same no-Docker harness as the
other Done unit tests: every collaborator is mocked deterministically via
``_old_path_patches``, Neo4j is a MagicMock, and
``compute_transcript_coverage_with_spans`` is patched so no live LLM / DB write
runs.

Covers:
  * the transcript grader is always the grading lane (provenance reports it);
  * a ``CoverageGradingError`` from the grader PROPAGATES (no legacy fallback)
    so the CoverageGradingError -> 503 handler fires;
  * the per-attempt narrative spans thread into ``compute_topic_score``.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.errors import CoverageGradingError
from apollo.handlers.tests._done_fixtures import _old_path_patches

pytestmark = pytest.mark.unit

_VALID_VERDICT = {
    "per_step": {},
    "procedure_scores": {},
    "confidences": {},
    "negotiation_counts": {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0},
}


def _drop(patches, *attributes):
    return [p for p in patches if getattr(p, "attribute", None) not in attributes]


async def _run(*, coverage_mock):
    """Drive ``handle_done`` through the OLD-path harness with
    ``compute_transcript_coverage_with_spans`` patched (``coverage_mock`` must
    return the ``(coverage, spans)`` pair, or raise)."""
    db, _sess, _attempt, patches = _old_path_patches()
    # Drop the base golden's transcript-coverage stub so this test's own wins.
    patches = _drop(patches, "compute_transcript_coverage_with_spans")

    from apollo.handlers.done import handle_done

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "apollo.handlers.done.compute_transcript_coverage_with_spans",
                new=coverage_mock,
            )
        )
        for p in patches:
            stack.enter_context(p)
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    return out


async def test_transcript_grader_is_the_grading_lane():
    """``compute_transcript_coverage_with_spans`` is awaited once and
    provenance reports the transcript lane."""
    coverage_mock = AsyncMock(return_value=(_VALID_VERDICT, {}))
    out = await _run(coverage_mock=coverage_mock)

    coverage_mock.assert_awaited_once()

    provenance = out["grading_provenance"]
    assert provenance["grader_used"] == "llm_transcript"
    assert provenance["evidence_source"] == "transcript"
    # The fallback failure field is gone with the fallback lane it recorded.
    assert "transcript_grader_failure" not in provenance


async def test_narrative_spans_thread_into_topic_score():
    """The per-attempt student quotes returned by the transcript grader flow
    into ``compute_topic_score`` as ``evidence_spans`` — the seam that lets the
    diagnostic narrative quote only what the student said THIS session."""
    spans = {"p1": "future shock occurs when things move too quickly"}
    coverage_mock = AsyncMock(return_value=(_VALID_VERDICT, spans))
    topic_score_mock = MagicMock(return_value=None)

    db, _sess, _attempt, patches = _old_path_patches()
    # Drop the base golden's transcript-coverage + topic-score stubs so this
    # test's own patches win.
    patches = _drop(patches, "compute_transcript_coverage_with_spans", "compute_topic_score")

    from apollo.handlers.done import handle_done

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "apollo.handlers.done.compute_transcript_coverage_with_spans",
                new=coverage_mock,
            )
        )
        stack.enter_context(patch("apollo.handlers.done.compute_topic_score", new=topic_score_mock))
        for p in patches:
            stack.enter_context(p)
        await handle_done(db=db, neo=MagicMock(), session_id=11)

    topic_score_mock.assert_called_once()
    assert topic_score_mock.call_args.kwargs["evidence_spans"] == spans


async def test_coverage_grading_error_propagates_no_fallback():
    """A ``CoverageGradingError`` from the transcript grader is NOT swallowed:
    it propagates out of ``handle_done`` so the CoverageGradingError -> 503
    retryable handler serves "try again" instead of a fabricated grade."""
    coverage_mock = AsyncMock(
        side_effect=CoverageGradingError(stage="transcript_adjudication", last_error="boom")
    )
    with pytest.raises(CoverageGradingError) as exc_info:
        await _run(coverage_mock=coverage_mock)

    assert exc_info.value.stage == "transcript_adjudication"
