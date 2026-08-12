"""M1 (P3.4) unit seams for the Done grading claim, on the golden fixture harness.

The claim's exclusivity is proved against real Postgres
(`tests/database/test_apollo_done_claim_postgres.py`); this module covers the
ROUTING decisions around it, which need a deterministic collaborator set rather
than a database: already-graded short-circuit, claim-lost 409, the
compensating release on a pre-grade failure, and the M1b fence (P3.4
controller delta) that guards the terminal `phase='REPORT'` write.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.errors import CoverageGradingError, GradingInProgressError
from apollo.handlers.tests._done_fixtures import _old_path_patches

pytestmark = pytest.mark.unit


def _drop(patches: list, attribute: str) -> list:
    return [p for p in patches if getattr(p, "attribute", None) != attribute]


async def _run(patches, db, *, auto_done: bool = False) -> Any:
    from apollo.handlers.done import handle_done

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        try:
            return await handle_done(db=db, neo=MagicMock(), session_id=11, auto_done=auto_done)
        except Exception as exc:  # noqa: BLE001 — the routing tests assert on it
            return exc


async def test_already_graded_attempt_serves_the_stored_grade_and_never_claims():
    """A re-clicked Done must replay the persisted grade, not re-adjudicate it:
    re-grading spends a second large LLM call, can return a DIFFERENT letter,
    and last-writer-wins would overwrite the report the student was shown."""
    db, sess, attempt, patches = _old_path_patches()
    attempt.result = "graded"
    attempt.diagnostic_report = {
        "narrative": "You explained continuity well.",
        "rubric": {"overall": {"score": 71, "letter": "B-"}},
        "coverage": {"per_step": {"a": "covered"}},
        "served_overall": {"score": 71, "letter": "B-"},
    }

    # `_stored_grade_payload` is REAL (unmocked) here and, on an already-graded
    # attempt, reads `StudentProgress` directly via `db.execute` — a 3rd shape
    # the golden fixture's 2-call (session, attempt) dispatcher does not carry.
    # Extend it locally rather than touching the shared fixture (every OTHER
    # golden-path test never reaches this branch: `_stored_grade_payload`
    # returns before any `db.execute` when `attempt.result is None`).
    class _SessResult:
        def scalar_one(self):
            return sess

    class _AttemptResult:
        def scalars(self):
            result = MagicMock()
            result.first.return_value = attempt
            return result

    class _ProgressResult:
        def scalar_one_or_none(self):
            return MagicMock(xp_total=140)

    calls = {"n": 0}

    async def _execute(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _SessResult()
        if calls["n"] == 2:
            return _AttemptResult()
        return _ProgressResult()

    db.execute = AsyncMock(side_effect=_execute)

    claim = AsyncMock(return_value=True)
    # `_stored_grade_payload` computes its OWN zero-delta envelope
    # (`xp_earned=0, xp_before=xp_after=140`) — drop the shared golden fixture's
    # `compute_progress_envelope` stub (a FIXED `xp_earned=10` mock, meant for
    # the real-grading path) so the real, pure function runs here instead.
    patches = _drop(patches, "compute_progress_envelope") + [
        patch("apollo.handlers.done._claim_grading_slot", new=claim),
    ]

    result = await _run(patches, db)

    assert result["already_graded"] is True
    assert result["rubric"]["overall"] == {"score": 71, "letter": "B-"}
    assert result["diagnostic_narrative"] == "You explained continuity well."
    assert result["xp_earned"] == 0
    assert result["progress"]["xp_before"] == result["progress"]["xp_after"] == 140
    claim.assert_not_awaited()


async def test_losing_the_claim_raises_the_retryable_409():
    db, sess, attempt, patches = _old_path_patches()
    patches = patches + [
        patch("apollo.handlers.done._claim_grading_slot", new=AsyncMock(return_value=False))
    ]

    result = await _run(patches, db)

    assert isinstance(result, GradingInProgressError)
    assert result.session_id == 11
    assert result.attempt_id == attempt.id
    # Nothing was graded: the attempt row is untouched.
    assert attempt.result is None
    assert attempt.diagnostic_report is None


async def test_auto_done_loses_the_claim_the_same_way():
    """`auto_done` gets the SAME exception here — the never-error-the-student
    rule is enforced at the `handlers/chat` dispatch site, not by making
    `handle_done` return a fake payload."""
    db, sess, attempt, patches = _old_path_patches()
    patches = patches + [
        patch("apollo.handlers.done._claim_grading_slot", new=AsyncMock(return_value=False))
    ]

    result = await _run(patches, db, auto_done=True)

    assert isinstance(result, GradingInProgressError)


async def test_a_pre_grade_failure_releases_the_claim():
    """The sole-lane 503 must not brick the attempt: if the claim survived the
    failure, the student's retry would hit "another Done owns this" forever."""
    db, sess, attempt, patches = _old_path_patches()
    release = AsyncMock()
    patches = _drop(patches, "compute_transcript_coverage_with_spans") + [
        patch(
            "apollo.handlers.done.compute_transcript_coverage_with_spans",
            new=AsyncMock(side_effect=CoverageGradingError(stage="adjudication", last_error="boom")),
        ),
        patch("apollo.handlers.done._claim_grading_slot", new=AsyncMock(return_value=True)),
        patch("apollo.handlers.done._release_grading_claim", new=release),
    ]

    result = await _run(patches, db)

    assert isinstance(result, CoverageGradingError)
    release.assert_awaited_once()
    assert release.await_args.kwargs["session_id"] == 11
    assert release.await_args.kwargs["prior_phase"] == "TEACHING"


async def test_a_successful_done_never_releases_the_claim():
    db, sess, attempt, patches = _old_path_patches()
    release = AsyncMock()
    patches = patches + [
        patch("apollo.handlers.done._claim_grading_slot", new=AsyncMock(return_value=True)),
        patch("apollo.handlers.done._release_grading_claim", new=release),
    ]

    result = await _run(patches, db)

    assert "rubric" in result
    assert sess.phase == "REPORT"
    release.assert_not_awaited()


async def test_fenced_out_mid_grade_raises_and_writes_nothing():
    """M1b fence (P3.4 controller delta): the claim succeeded, but ANOTHER
    Done reclaimed the (stale) slot and landed the grade before this Done
    reached its own terminal write. The fence must refuse — no
    `attempt.result`, no `diagnostic_report`, no XP — and it must refuse
    BEFORE either of those writes, not after (real-Postgres proof of the
    rowcount-0 CAS itself lives in `tests/database/test_apollo_done_claim_postgres.py`).
    The exception still propagates through `_grade_claimed_attempt`'s
    except-all wrapper like any other pre-commit failure, so
    `_release_grading_claim` still runs — harmlessly, since its own
    `phase = SOLVING` guard no-ops once the reclaiming Done has already moved
    the phase to REPORT."""
    db, sess, attempt, patches = _old_path_patches()
    release = AsyncMock()
    patches = patches + [
        patch("apollo.handlers.done._claim_grading_slot", new=AsyncMock(return_value=True)),
        patch("apollo.handlers.done._fence_grade_commit", new=AsyncMock(return_value=False)),
        patch("apollo.handlers.done._release_grading_claim", new=release),
    ]

    result = await _run(patches, db)

    assert isinstance(result, GradingInProgressError)
    assert result.session_id == 11
    assert result.attempt_id == attempt.id
    assert attempt.result is None
    assert attempt.diagnostic_report is None
    release.assert_awaited_once()
