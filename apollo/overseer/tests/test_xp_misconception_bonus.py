"""Decision D7 (P3.2 §8): the +10 XP bonus for a corrected misconception.

Arithmetic only — dedup of the AWARD across attempts and the Apollo-elicited
guard belong to the caller (`done.py` at level >= 3, using
`attempt_history.prior_wrongness_findings`). What is pinned here is the
invariant that outlives any caller: **XP only ever goes up**.
"""

from __future__ import annotations

import pytest

from apollo.overseer.xp import (
    MISCONCEPTION_CORRECTED_BONUS_XP,
    compute_misconception_bonus,
    compute_xp_earned,
)

pytestmark = pytest.mark.unit


def test_bonus_is_ten_per_distinct_key():
    assert MISCONCEPTION_CORRECTED_BONUS_XP == 10
    assert compute_misconception_bonus(newly_resolved_keys=["eq.one"]) == 10
    assert compute_misconception_bonus(newly_resolved_keys=["eq.one", "c.two"]) == 20
    assert compute_misconception_bonus(newly_resolved_keys=["a", "b", "c"]) == 30


def test_empty_is_zero():
    assert compute_misconception_bonus(newly_resolved_keys=[]) == 0
    assert compute_misconception_bonus(newly_resolved_keys=()) == 0


def test_duplicate_keys_counted_once():
    """One node, one bonus — a student cannot farm the same corrected claim by
    restating it, however many findings the caller hands over."""
    assert compute_misconception_bonus(newly_resolved_keys=["eq.one", "eq.one", "eq.one"]) == 10
    assert compute_misconception_bonus(newly_resolved_keys=["eq.one", "eq.two", "eq.one"]) == 20


@pytest.mark.parametrize(
    "keys",
    [[], ["a"], ["a", "a"], ["a", "b"], [""], ["a", "b", "c", "d", "e"]],
)
def test_bonus_never_negative(keys: list[str]):
    """`progress_repo.apply_xp` RAISES on a negative delta — the bonus is
    additive-only by construction, so it can never trip that guard."""
    bonus = compute_misconception_bonus(newly_resolved_keys=keys)
    assert bonus >= 0
    assert bonus % MISCONCEPTION_CORRECTED_BONUS_XP == 0


def test_bonus_is_additive_on_top_of_the_base_award():
    """The award shape the caller builds: base XP + bonus, never a replacement
    and never a subtraction."""
    base = compute_xp_earned(overall_score=84, difficulty="standard", is_reattempt=False)
    total = base + compute_misconception_bonus(newly_resolved_keys=["eq.one"])
    assert base == 126
    assert total == 136
    assert total > base
