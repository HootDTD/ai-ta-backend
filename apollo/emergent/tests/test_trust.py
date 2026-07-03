"""Pure trust-math tests (memo §6 / OQ2) — no DB, no container.

Covers the K-support boundary, the tau_assert / tau_project band boundaries,
recency half-life decay, and the unkeyed-never-promotes rule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apollo.emergent import trust
from apollo.emergent.config import (
    K_DISTINCT_STUDENTS,
    RECENCY_HALFLIFE_DAYS,
    TAU_ASSERT,
    TAU_PROJECT,
)

_NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def test_default_constants_match_memo_oq2():
    assert K_DISTINCT_STUDENTS == 3
    assert TAU_ASSERT == 0.5
    assert TAU_PROJECT == 0.2
    assert RECENCY_HALFLIFE_DAYS == 30.0


def test_full_support_recent_high_confidence_is_trust_one():
    t = trust.trust_score(
        distinct_students=3, mean_confidence=1.0, last_seen=_NOW, now=_NOW
    )
    assert t == pytest.approx(1.0)
    assert trust.band(t) == "promoted"


def test_k_support_saturates_at_k_students():
    # 1 student -> support 1/3; 2 -> 2/3; 3 and 4 both -> 1.0 (saturates).
    one = trust.trust_score(distinct_students=1, mean_confidence=1.0, last_seen=_NOW, now=_NOW)
    two = trust.trust_score(distinct_students=2, mean_confidence=1.0, last_seen=_NOW, now=_NOW)
    three = trust.trust_score(distinct_students=3, mean_confidence=1.0, last_seen=_NOW, now=_NOW)
    four = trust.trust_score(distinct_students=4, mean_confidence=1.0, last_seen=_NOW, now=_NOW)
    assert one == pytest.approx(1 / 3)
    assert two == pytest.approx(2 / 3)
    assert three == pytest.approx(1.0)
    assert four == pytest.approx(1.0)


def test_one_student_is_below_assert_but_above_project():
    t = trust.trust_score(distinct_students=1, mean_confidence=1.0, last_seen=_NOW, now=_NOW)
    assert TAU_PROJECT <= t < TAU_ASSERT
    assert trust.band(t) == "observed"
    assert trust.is_promoted(t, "misc.foo") is False


def test_tau_assert_boundary_is_inclusive():
    # distinct=3, conf=0.5, recent -> trust exactly 0.5 == TAU_ASSERT -> promoted.
    t = trust.trust_score(distinct_students=3, mean_confidence=0.5, last_seen=_NOW, now=_NOW)
    assert t == pytest.approx(0.5)
    assert trust.band(t) == "promoted"
    assert trust.is_promoted(t, "misc.foo") is True

    # just under -> observed, not promoted.
    t2 = trust.trust_score(distinct_students=3, mean_confidence=0.49, last_seen=_NOW, now=_NOW)
    assert trust.band(t2) == "observed"
    assert trust.is_promoted(t2, "misc.foo") is False


def test_tau_project_boundary_is_inclusive():
    # trust exactly 0.2 -> observed (>= tau_project); just under -> candidate.
    assert trust.band(0.2) == "observed"
    assert trust.band(0.199) == "candidate"


def test_recency_halflife_halves_trust_at_one_halflife():
    old = _NOW - timedelta(days=RECENCY_HALFLIFE_DAYS)
    t = trust.trust_score(distinct_students=3, mean_confidence=1.0, last_seen=old, now=_NOW)
    assert t == pytest.approx(0.5)
    assert trust.is_promoted(t, "misc.foo") is True  # exactly on the boundary


def test_recency_decay_can_demote_below_assert():
    two_halflives = _NOW - timedelta(days=2 * RECENCY_HALFLIFE_DAYS)
    t = trust.trust_score(distinct_students=3, mean_confidence=1.0, last_seen=two_halflives, now=_NOW)
    assert t == pytest.approx(0.25)
    assert trust.is_promoted(t, "misc.foo") is False


def test_none_confidence_is_zero_trust():
    t = trust.trust_score(distinct_students=5, mean_confidence=None, last_seen=_NOW, now=_NOW)
    assert t == 0.0


def test_zero_students_is_zero_trust():
    t = trust.trust_score(distinct_students=0, mean_confidence=1.0, last_seen=_NOW, now=_NOW)
    assert t == 0.0


def test_none_last_seen_is_zero_recency():
    assert trust.recency_factor(None, _NOW) == 0.0


def test_nonpositive_halflife_disables_decay():
    old = _NOW - timedelta(days=1000)
    assert trust.recency_factor(old, _NOW, halflife_days=0) == 1.0


def test_unkeyed_signature_never_promotes_even_at_max_trust():
    assert trust.is_promotable_signature("misc.foo") is True
    assert trust.is_promotable_signature("unkeyed:5") is False
    # max trust, but unkeyed -> is_promoted False regardless.
    assert trust.is_promoted(1.0, "unkeyed:5") is False
    assert trust.is_promoted(1.0, "misc.foo") is True


def test_muted_suppresses_promotion():
    assert trust.is_promoted(1.0, "misc.foo", muted=True) is False
    assert trust.is_promoted(1.0, "misc.foo", muted=False) is True
