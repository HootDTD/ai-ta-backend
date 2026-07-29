"""Non-Docker unit coverage for ``apollo/handlers/done.py`` branches that were
previously exercised only under Docker (testcontainers) integration tests:

  * ``_find_problem`` bank resolution (match + RuntimeError on miss);
  * the narrative-grounding soft-fail (``_student_utterances`` raising must
    never block grading — the narrator runs with empty utterances);
  * the unconditional scorecard + mastery-projection block (scorecard rendered
    from the persisted canonical payload; mastery projected unless the dormant
    WU-5A2 layer-3 flag is live).

Uses the shared ``_old_path_patches`` harness — no Docker, no live LLM.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers.done import _find_problem, handle_done
from apollo.handlers.tests._done_fixtures import _old_path_patches

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _find_problem — bank resolution
# ---------------------------------------------------------------------------


def _bank_problem(database_id: int) -> MagicMock:
    problem = MagicMock()
    problem.database_id = database_id
    return problem


async def test_find_problem_returns_matching_bank_problem():
    bank = [_bank_problem(41), _bank_problem(42)]
    lister = AsyncMock(return_value=bank)
    with patch("apollo.handlers.done.list_problems_for_concept", new=lister):
        out = await _find_problem(MagicMock(), 3, 42, course_id=7)

    assert out is bank[1]
    lister.assert_awaited_once()
    assert lister.await_args.kwargs == {"concept_id": 3, "search_space_id": 7}


async def test_find_problem_raises_runtime_error_on_miss():
    lister = AsyncMock(return_value=[_bank_problem(41)])
    with patch("apollo.handlers.done.list_problems_for_concept", new=lister):
        with pytest.raises(RuntimeError, match="not in bank"):
            await _find_problem(MagicMock(), 3, 42, course_id=7)


# ---------------------------------------------------------------------------
# Narrative grounding soft-fail — _student_utterances raising never blocks
# ---------------------------------------------------------------------------


async def test_student_utterances_failure_soft_fails_to_empty_tuple():
    db, _sess, _attempt, patches = _old_path_patches()
    # Replace the harness's generate_diagnostic stub with a spy so the
    # empty-utterances degradation is observable.
    patches = [p for p in patches if getattr(p, "attribute", None) != "generate_diagnostic"]
    diag_spy = MagicMock(return_value="narrative")

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "apollo.handlers.done._student_utterances",
                new=AsyncMock(side_effect=RuntimeError("db down")),
            )
        )
        stack.enter_context(patch("apollo.handlers.done.generate_diagnostic", new=diag_spy))
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)

    # Done completed and the narrator ran with EMPTY utterances (degraded,
    # ungrounded prompt) instead of raising.
    assert out["diagnostic_narrative"] == "narrative"
    diag_spy.assert_called_once()
    assert diag_spy.call_args.kwargs["student_utterances"] == ()


# ---------------------------------------------------------------------------
# Unconditional scorecard + mastery projection block
# ---------------------------------------------------------------------------

_CANONICAL_SENTINEL = {"grader_used": "llm_fallback", "sentinel": True}


async def _run_with_persisted_artifact():
    """Drive handle_done with write_artifacts returning a non-None canonical
    payload; return (response, render_spy, mastery_spy, db)."""
    db, _sess, _attempt, patches = _old_path_patches()
    patches = [p for p in patches if getattr(p, "attribute", None) != "write_artifacts"]
    render_spy = MagicMock(return_value={"score_0_100": 50})
    mastery_spy = AsyncMock()

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "apollo.handlers.done.write_artifacts",
                new=AsyncMock(return_value=dict(_CANONICAL_SENTINEL)),
            )
        )
        stack.enter_context(patch("apollo.handlers.done.render_scorecard", new=render_spy))
        stack.enter_context(patch("apollo.handlers.done._project_mastery", new=mastery_spy))
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    return out, render_spy, mastery_spy, db


async def test_scorecard_attached_and_mastery_projected(monkeypatch):
    monkeypatch.delenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", raising=False)

    out, render_spy, mastery_spy, db = await _run_with_persisted_artifact()

    # Scorecard templated over the persisted canonical payload.
    render_spy.assert_called_once_with(_CANONICAL_SENTINEL)
    assert out["scorecard"] == {"score_0_100": 50}
    # Mastery projection follows the artifact write (attempt fixture id = 99).
    mastery_spy.assert_awaited_once_with(db, attempt_id=99)


async def test_mastery_projection_skipped_when_layer3_live(monkeypatch):
    """The dormant WU-5A2 Bayesian path being live must suppress the artifact
    mastery projection (never double-apply evidence) — the scorecard still
    ships."""
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")

    out, render_spy, mastery_spy, _db = await _run_with_persisted_artifact()

    render_spy.assert_called_once_with(_CANONICAL_SENTINEL)
    assert out["scorecard"] == {"score_0_100": 50}
    mastery_spy.assert_not_awaited()
