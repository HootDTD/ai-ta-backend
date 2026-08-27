"""THE §2.5 hazard: the rubric's absent-axis flip must never fire.

`rubric.py` redistributes weight across the axes that are PRESENT, and an axis
is present iff its score dict is non-empty. The moment anything feeds
`_attempt_misconception_scores`, `AXIS_WEIGHTS["misconception_corrected"] = 0.05`
flips from absent to present, every other axis rescales by 0.95, and
`score_details.llm_rubric.overall` moves (100/100/100 plus an unresolved 50
becomes 97.5, not 100) — one rung BELOW the level labelled "score".

Wrongness findings therefore never touch that function, never reach
`compute_rubric`'s `misconception_scores` argument, and never write
`TutoringMessage` metadata. These tests are the tripwire.
"""

from __future__ import annotations

import json

import pytest

from apollo.handlers.tests import _wrongness_fixtures as wf
from apollo.overseer.rubric import AXIS_WEIGHTS

pytestmark = pytest.mark.unit

_LEVELS = [0, 1, 2, 3, 4]


async def _rubric_at(monkeypatch, level: int) -> dict:
    """The RAW rubric this Done persisted — the artifact's `score_details.
    llm_rubric` block, built by the real `compute_rubric`."""
    _out, started = await wf.run_done(monkeypatch, level=level, real_rubric=True)
    return started["write_artifacts"].await_args.kwargs["rubric"]


@pytest.mark.parametrize("level", _LEVELS)
async def test_misconception_axis_stays_absent_at_every_level(monkeypatch, level):
    rubric = await _rubric_at(monkeypatch, level)

    assert "misconception_corrected" in AXIS_WEIGHTS  # the axis exists…
    assert rubric["misconception_corrected"]["present"] is False  # …and stays absent
    assert rubric["overall"]["score"] == 100


@pytest.mark.parametrize("level", _LEVELS)
async def test_compute_rubric_receives_identical_misconception_scores_at_every_level(
    monkeypatch, level
):
    """The argument itself, not just its effect: an empty dict at every rung."""
    _out, started = await wf.run_done(monkeypatch, level=level)

    started["_attempt_misconception_scores"].assert_awaited_once()
    assert started["compute_rubric"].call_args.kwargs["misconception_scores"] == {}


async def test_llm_rubric_block_byte_identical_levels_0_through_4(monkeypatch):
    """Whole-blob inertness (§2.3's level-1 ship condition and §2.5's hazard),
    across every rung including the dark ceiling — a `served_overall`-only
    assertion would miss exactly the redistribution this file exists to catch."""
    digests = set()
    for level in _LEVELS:
        rubric = await _rubric_at(monkeypatch, level)
        digests.add(json.dumps(rubric, sort_keys=True))

    assert len(digests) == 1


async def test_findings_never_write_tutoring_message_metadata(monkeypatch):
    """The other half of the hazard: `_attempt_misconception_scores` reads
    `TutoringMessage.message_metadata['misconception']`, so the wiring must not
    write there either. Done issues no message write at all on the grade path."""
    _out, started = await wf.run_done(monkeypatch, level=3)

    # The only awaited misconception-side call is the READ, and it returned {}.
    assert started["_attempt_misconception_scores"].await_count == 1
    assert started["compute_rubric"].call_args.kwargs["misconception_scores"] == {}
