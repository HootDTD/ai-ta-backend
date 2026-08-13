"""Decision-7: +10 XP for FIXING a contradiction Apollo elicited.

The one lever in P3.2 that celebrates rather than penalises, and the counter to
pilot complaint c4 (all-negative feedback). Three rules make it unfarmable and
student-safe, each pinned below:

* the population is ``resolved AND apollo_elicited`` — NOT ``corroborated``,
  which is the empty set here by construction (S2′ requires NOT corrected_later);
* ``apollo_elicited`` (``last_asked_turn < correction_turn``) closes the
  self-assert-then-self-correct farm;
* the ``prior_wrongness_findings`` subtraction makes it once per
  user × problem × node, so a re-roll cannot re-earn it.

XP only ever goes up: the bonus is additive, and its own failure domain awards 0.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from apollo.handlers.done import _wrongness_bonus_xp
from apollo.handlers.tests import _wrongness_fixtures as wf
from apollo.overseer.wrongness import WrongnessFinding

pytestmark = pytest.mark.unit

_BASE_XP = 10  # the harness's fixed `compute_xp_earned`
_BONUS = 10  # `xp.MISCONCEPTION_CORRECTED_BONUS_XP`


def _finding(**overrides) -> WrongnessFinding:
    fields = {
        "node_id": "eq1",
        "quote": wf.MATERIAL_QUOTE,
        "contradicts": wf.CONTRADICTS,
        "kind": "reversal",
        "corroborated": False,
        "resolved": True,
        "apollo_elicited": True,
        "would_ceiling": False,
    }
    return WrongnessFinding(**{**fields, **overrides})


async def _xp_delta(monkeypatch, **run_kwargs) -> int:
    _out, started = await wf.run_done(monkeypatch, **run_kwargs)
    return started["apply_xp"].await_args.kwargs["xp_delta"]


# --- the wiring, end to end ------------------------------------------------


async def test_bonus_awarded_only_when_apollo_elicited(monkeypatch):
    delta = await _xp_delta(
        monkeypatch,
        level=3,
        wrongness_map=wf.second_reader(corrected_later=True),
    )

    assert delta == _BASE_XP + _BONUS


async def test_self_asserted_then_corrected_earns_no_bonus(monkeypatch):
    """The farming path decision-7's amendment closes: the student asserts
    something wrong unprompted (`last_asked_turn is None`), corrects it, and
    collects. `apollo_elicited` is False, so nothing is awarded."""
    delta = await _xp_delta(
        monkeypatch,
        level=3,
        ledger=(wf.LedgerRow("eq1", last_asked_turn=None, evidence=wf.tagged_evidence()),),
        wrongness_map=wf.second_reader(corrected_later=True),
    )

    assert delta == _BASE_XP


async def test_a_probe_after_the_claim_still_earns_the_bonus(monkeypatch):
    """`last_asked_turn` (9) AFTER the quoted claim turn (4) is the ORDINARY
    challenge loop, not a farm: the student errs, L2a sorts the contested node
    to the front, Apollo probes it, the student fixes it. Decision 7 exists to
    celebrate exactly this.

    An earlier reading compared the two turn numbers and denied the bonus here,
    which made the level-2 probe priority defeat the level-3 bonus — the more
    Apollo elicited, the less the guard believed it had. See
    `wrongness.select_findings` for why the guard is a presence test."""
    delta = await _xp_delta(
        monkeypatch,
        level=3,
        ledger=(wf.LedgerRow("eq1", last_asked_turn=9, evidence=wf.tagged_evidence(turn_id=4)),),
        wrongness_map=wf.second_reader(corrected_later=True),
    )

    assert delta == _BASE_XP + _BONUS


async def test_bonus_deduped_across_prior_attempts_same_node(monkeypatch):
    """Once per user × problem × node. Best-grade-wins retries mean a student
    can re-run the same transcript; the bonus must not be a per-attempt annuity."""
    delta = await _xp_delta(
        monkeypatch,
        level=3,
        wrongness_map=wf.second_reader(corrected_later=True),
        prior_findings=(
            {
                "canonical_key": "eq1",
                "evidence_span": wf.MATERIAL_QUOTE,
                "resolved": True,
                "attempt_id": 7,
            },
        ),
    )

    assert delta == _BASE_XP


async def test_a_different_prior_node_does_not_suppress_the_bonus(monkeypatch):
    delta = await _xp_delta(
        monkeypatch,
        level=3,
        wrongness_map=wf.second_reader(corrected_later=True),
        prior_findings=(
            {"canonical_key": "c1", "evidence_span": None, "resolved": True, "attempt_id": 7},
        ),
    )

    assert delta == _BASE_XP + _BONUS


@pytest.mark.parametrize("level", [0, 1, 2])
async def test_bonus_absent_below_level_3(monkeypatch, level):
    delta = await _xp_delta(
        monkeypatch,
        level=level,
        wrongness_map=wf.second_reader(corrected_later=True),
    )

    assert delta == _BASE_XP


async def test_an_uncorrected_finding_earns_nothing(monkeypatch):
    """The bonus rewards the FIX, never the contradiction itself."""
    delta = await _xp_delta(monkeypatch, level=3)

    assert delta == _BASE_XP


async def test_bonus_failure_falls_back_to_base_xp(monkeypatch, caplog):
    """Own failure domain: a broken history read costs a bonus, never a grade."""
    with caplog.at_level("ERROR", logger="apollo.handlers.done"):
        _out, started = await wf.run_done(
            monkeypatch,
            level=3,
            wrongness_map=wf.second_reader(corrected_later=True),
            extra_patches=[
                patch(
                    "apollo.handlers.done.prior_wrongness_findings",
                    new=AsyncMock(side_effect=RuntimeError("history read exploded")),
                )
            ],
        )

    assert started["apply_xp"].await_args.kwargs["xp_delta"] == _BASE_XP
    assert "apollo_wrongness_xp_bonus_failed" in caplog.text


# --- the helper's own contract --------------------------------------------


async def test_bonus_never_makes_xp_delta_negative():
    """`progress_repo.apply_xp` RAISES on a negative delta. The bonus is
    structurally additive-only, so it can never be the cause."""

    class _Attempt:
        id = 99
        problem_id = 42

    for findings in ([], [_finding()], [_finding(), _finding(node_id="c1")]):
        with patch("apollo.handlers.done.prior_wrongness_findings", new=AsyncMock(return_value=())):
            bonus = await _wrongness_bonus_xp(
                AsyncMock(),
                findings=findings,
                attempt=_Attempt(),  # type: ignore[arg-type]
                course_id=7,
            )
        assert bonus >= 0
    assert bonus == 2 * _BONUS


async def test_duplicate_node_findings_are_counted_once():
    """`select_findings` returns one rung per EVIDENCE ENTRY, so two corrected
    entries on one node must not pay twice (integration finding F-06)."""

    class _Attempt:
        id = 99
        problem_id = 42

    with patch("apollo.handlers.done.prior_wrongness_findings", new=AsyncMock(return_value=())):
        bonus = await _wrongness_bonus_xp(
            AsyncMock(),
            findings=[_finding(), _finding()],
            attempt=_Attempt(),  # type: ignore[arg-type]
            course_id=7,
        )

    assert bonus == _BONUS
