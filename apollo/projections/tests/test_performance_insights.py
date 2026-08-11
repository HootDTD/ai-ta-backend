"""Pure-function validity anchors for ``apollo.projections.performance_insights``.

Every statistic here is hand-computed in the test body (see the arithmetic in
each docstring/comment) so these tests are the ground truth the implementation
is measured against — NOT a mirror of the code. The two DB loaders
(``load_engagement`` / ``load_problem_aggregates``) are exercised against real
Postgres in ``tests/database/test_class_performance_postgres.py``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from apollo.projections import performance_insights as pi
from apollo.projections.performance_insights import ProblemAgg

pytestmark = pytest.mark.unit


# --- stat helpers -----------------------------------------------------------


def test_mean():
    assert pi.mean([2.0, 4.0, 6.0]) == 4.0


def test_median_odd_even_empty_single():
    assert pi.median([3.0, 1.0, 2.0]) == 2.0  # sorted [1,2,3] -> middle 2
    assert pi.median([1.0, 2.0, 3.0, 4.0]) == 2.5  # (2+3)/2
    assert pi.median([]) is None
    assert pi.median([5.0]) == 5.0


def test_word_count_whitespace_split():
    assert pi.word_count("hello world") == 2
    assert pi.word_count("  a  b   c ") == 3  # collapses runs, strips ends
    assert pi.word_count("") == 0
    assert pi.word_count("   ") == 0


def test_pearson_hand_computed():
    """xs=[1,2,3,4,5], ys=[2,4,5,4,5]: mean_x=3, mean_y=4.
    num = (-2)(-2)+(-1)(0)+0+0+(2)(1) = 6
    dx = sqrt(4+1+0+1+4)=sqrt(10); dy = sqrt(4+0+1+0+1)=sqrt(6)
    r = 6 / sqrt(60) = 0.7745966..."""
    r = pi.pearson([1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
    assert r == pytest.approx(6 / math.sqrt(60), abs=1e-9)
    assert round(r, 3) == 0.775


def test_pearson_zero_variance_is_zero():
    # constant x -> dx == 0 -> defined as no signal (0.0), not a ZeroDivisionError.
    assert pi.pearson([5, 5, 5, 5], [1, 2, 3, 4]) == 0.0
    assert pi.pearson([1, 2, 3, 4], [7, 7, 7, 7]) == 0.0


def test_pearson_perfect_correlation():
    assert pi.pearson([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert pi.pearson([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_pearson_empty():
    assert pi.pearson([], []) == 0.0


def test_spearman_with_ties_hand_computed():
    """xs=[1,2,2,3] -> avg ranks [1, 2.5, 2.5, 4] (the two 2's share (1-based
    positions 2,3 averaged = 2.5)); ys=[10,20,30,40] -> ranks [1,2,3,4].
    pearson([1,2.5,2.5,4],[1,2,3,4]): mean 2.5 each.
    num = (-1.5)(-1.5)+0+0+(1.5)(1.5) = 4.5
    dx = sqrt(2.25+0+0+2.25)=sqrt(4.5); dy = sqrt(2.25+0.25+0.25+2.25)=sqrt(5)
    rho = 4.5 / sqrt(22.5) = 0.9486832..."""
    rho = pi.spearman([1, 2, 2, 3], [10, 20, 30, 40])
    assert rho == pytest.approx(4.5 / math.sqrt(22.5), abs=1e-9)
    assert round(rho, 3) == 0.949


def test_average_ranks_ties():
    # values [40, 10, 10, 30]: sorted -> 10,10 (positions 1,2 -> avg rank 1.5),
    # 30 (rank 3), 40 (rank 4). Original order preserved.
    assert pi._average_ranks([40, 10, 10, 30]) == [4.0, 1.5, 1.5, 3.0]


# --- aggregations -----------------------------------------------------------


def test_engagement_by_student():
    rows = [("u1", "a b c"), ("u1", "one"), ("u2", "x y")]
    result = pi.engagement_by_student(rows)
    # u1: word counts [3, 1] -> 2 turns, median of [1,3] = 2.0
    assert result["u1"] == {"teaching_turns": 2, "median_words": 2.0}
    # u2: [2] -> 1 turn, median 2.0
    assert result["u2"] == {"teaching_turns": 1, "median_words": 2.0}


def test_engagement_by_student_empty():
    assert pi.engagement_by_student([]) == {}


def test_problem_aggregates_first_best_and_best_is_last():
    rows = [
        ("u1", 10, 1, 40.0),  # first (lowest id)
        ("u1", 10, 2, 85.0),  # best (highest score)
        ("u1", 10, 3, 70.0),  # a later, lower attempt -> best is NOT last
        ("u1", 20, 5, 55.0),  # single attempt
        ("u2", 10, 4, 90.0),
    ]
    aggs = pi.problem_aggregates(rows)
    by_problem = {a.problem_id: a for a in aggs["u1"]}
    p10 = by_problem[10]
    assert (p10.graded_count, p10.first_score, p10.best_score) == (3, 40.0, 85.0)
    assert p10.best_is_last is False
    p20 = by_problem[20]
    assert (p20.graded_count, p20.first_score, p20.best_score, p20.best_is_last) == (
        1,
        55.0,
        55.0,
        True,
    )
    u2p10 = aggs["u2"][0]
    assert (u2p10.graded_count, u2p10.best_is_last) == (1, True)


def test_problem_aggregates_tie_score_latest_id_wins_and_best_is_last():
    # Two attempts tie at 50; best-wins picks the latest id (2). It is the last
    # attempt -> best_is_last True.
    aggs = pi.problem_aggregates([("u1", 10, 1, 50.0), ("u1", 10, 2, 50.0)])
    a = aggs["u1"][0]
    assert (a.best_score, a.best_is_last) == (50.0, True)
    # Now a lower attempt lands AFTER the tie -> best (id 2) is not last (id 3).
    aggs2 = pi.problem_aggregates([("u1", 10, 1, 50.0), ("u1", 10, 2, 50.0), ("u1", 10, 3, 40.0)])
    a2 = aggs2["u1"][0]
    assert (a2.best_score, a2.best_is_last) == (50.0, False)


def test_problem_aggregates_gave_up_ignores_later_ungraded_attempt():
    """Fix: a still-ungraded retry AFTER the best (surfaced via
    ``latest_attempt_ids`` over ALL attempts) means the student has not stopped,
    so ``best_is_last`` is False and ``gave_up`` must NOT fire even when the best
    graded score is below the floor. The graded rows here end at id 2, but a
    later attempt (id 9) — absent from those graded rows — came after."""
    aggs = pi.problem_aggregates(
        [("u1", 10, 1, 30.0), ("u1", 10, 2, 45.0)],
        latest_attempt_ids={("u1", 10): 9},
    )
    a = aggs["u1"][0]
    assert (a.best_score, a.best_is_last) == (45.0, False)
    assert pi.student_flags(attempts=2, teaching_turns=0, median_words=None, aggs=[a]) == []


def test_problem_aggregates_gave_up_when_best_is_last_overall():
    """Fix (control): best 45 (< 60) and the best-producing attempt is also the
    latest attempt of any result -> ``gave_up`` (the existing behavior holds)."""
    aggs = pi.problem_aggregates(
        [("u1", 10, 1, 30.0), ("u1", 10, 2, 45.0)],
        latest_attempt_ids={("u1", 10): 2},
    )
    a = aggs["u1"][0]
    assert (a.best_score, a.best_is_last) == (45.0, True)
    assert pi.student_flags(attempts=2, teaching_turns=0, median_words=None, aggs=[a]) == [
        "gave_up"
    ]


def test_retry_fields():
    aggs = [ProblemAgg(10, 3, 40.0, 85.0, False), ProblemAgg(20, 1, 55.0, 55.0, True)]
    # only the >=2-attempt problem counts: gain 85-40 = 45
    assert pi.retry_fields(aggs) == {"problems_retried": 1, "avg_gain": 45.0}


def test_retry_fields_no_retries():
    assert pi.retry_fields([ProblemAgg(20, 1, 55.0, 55.0, True)]) == {
        "problems_retried": 0,
        "avg_gain": None,
    }


# --- flags (every branch) ---------------------------------------------------


def test_flag_not_started():
    assert pi.student_flags(attempts=0, teaching_turns=0, median_words=None, aggs=[]) == [
        "not_started"
    ]


def test_flag_low_effort_and_its_negatives():
    # engaged but terse -> low_effort
    assert pi.student_flags(attempts=5, teaching_turns=3, median_words=5.0, aggs=[]) == [
        "low_effort"
    ]
    # too few turns -> no flag
    assert pi.student_flags(attempts=5, teaching_turns=2, median_words=5.0, aggs=[]) == []
    # words at/above the floor -> no flag (8 is NOT < 8)
    assert pi.student_flags(attempts=5, teaching_turns=9, median_words=8.0, aggs=[]) == []


def test_flag_gave_up_and_its_negatives():
    # best < 60 AND no attempt after the best -> gave_up
    assert pi.student_flags(
        attempts=2, teaching_turns=0, median_words=None, aggs=[ProblemAgg(10, 2, 40.0, 50.0, True)]
    ) == ["gave_up"]
    # best < 60 but a later attempt exists -> NOT gave_up
    assert (
        pi.student_flags(
            attempts=3,
            teaching_turns=0,
            median_words=None,
            aggs=[ProblemAgg(10, 3, 40.0, 50.0, False)],
        )
        == []
    )
    # best >= 60 -> NOT gave_up
    assert (
        pi.student_flags(
            attempts=2,
            teaching_turns=0,
            median_words=None,
            aggs=[ProblemAgg(10, 2, 40.0, 65.0, True)],
        )
        == []
    )


def test_flag_grinding_and_its_negatives():
    # >=3 attempts, gain <= 2, best >= 60 (so no gave_up) -> grinding only
    assert pi.student_flags(
        attempts=3, teaching_turns=0, median_words=None, aggs=[ProblemAgg(10, 3, 70.0, 72.0, False)]
    ) == ["grinding"]
    # only 2 attempts -> no grinding
    assert (
        pi.student_flags(
            attempts=2,
            teaching_turns=0,
            median_words=None,
            aggs=[ProblemAgg(10, 2, 70.0, 72.0, False)],
        )
        == []
    )
    # improved by more than the margin -> no grinding
    assert (
        pi.student_flags(
            attempts=3,
            teaching_turns=0,
            median_words=None,
            aggs=[ProblemAgg(10, 3, 70.0, 80.0, False)],
        )
        == []
    )


def test_flag_order_is_stable():
    flags = pi.student_flags(
        attempts=4,
        teaching_turns=5,
        median_words=3.0,
        aggs=[ProblemAgg(10, 3, 70.0, 71.0, True)],
    )
    assert flags == ["low_effort", "grinding"]  # not_started, low_effort, gave_up, grinding order


def test_student_extras_none_core_and_no_attempts():
    extras = pi.student_extras(attempts=0, engagement_core=None, aggs=[])
    assert extras == {
        "engagement": {
            "teaching_turns": 0,
            "median_words": None,
            "problems_retried": 0,
            "avg_gain": None,
        },
        "flags": ["not_started"],
    }


def test_student_extras_populated():
    extras = pi.student_extras(
        attempts=2,
        engagement_core={"teaching_turns": 5, "median_words": 3.0},
        aggs=[ProblemAgg(10, 2, 40.0, 90.0, True)],
    )
    assert extras["engagement"] == {
        "teaching_turns": 5,
        "median_words": 3.0,
        "problems_retried": 1,
        "avg_gain": 50.0,
    }
    assert extras["flags"] == ["low_effort"]  # turns 5>=3, median 3<8


# --- insight builders -------------------------------------------------------


def _points(turns_and_grades):
    # Distinct user_id per point so the quartile tie-break is deterministic; the
    # correlation/quartile values these anchor never depend on the id itself.
    return [
        {"turns": t, "avg_best": g, "email": None, "user_id": f"u{i}"}
        for i, (t, g) in enumerate(turns_and_grades)
    ]


def test_build_correlation_suppressed_below_min_n():
    assert pi.build_correlation(_points([(i, i * 10.0) for i in range(7)])) is None


def test_build_correlation_perfect_positive():
    points = _points([(i, i * 10.0) for i in range(1, 9)])  # 8 points, perfectly linear
    result = pi.build_correlation(points)
    assert result["n"] == 8
    assert result["pearson_r"] == 1.0
    assert result["spearman_rho"] == 1.0
    assert result["points"] is points


def test_build_correlation_zero_variance():
    # constant turns -> pearson & spearman both 0.0 (no signal), still not None.
    result = pi.build_correlation(_points([(5, i * 10.0) for i in range(8)]))
    assert result["pearson_r"] == 0.0
    assert result["spearman_rho"] == 0.0


def test_build_effort_quartiles_suppressed_below_min_n():
    assert pi.build_effort_quartiles(_points([(i, 50.0) for i in range(7)])) is None


def test_build_effort_quartiles_hand_computed():
    # 8 students, distinct ascending turns 1..8 (so the sort is by turns).
    # grades chosen so each quartile's mean is obvious.
    students = _points(
        [(1, 50.0), (2, 60.0), (3, 70.0), (4, 80.0), (5, 90.0), (6, 100.0), (7, 40.0), (8, 30.0)]
    )
    quartiles = pi.build_effort_quartiles(students)
    assert [q["quartile"] for q in quartiles] == [1, 2, 3, 4]
    assert [q["students"] for q in quartiles] == [2, 2, 2, 2]
    # Q1 = turns 1,2 -> (50+60)/2 = 55 ; Q4 = turns 7,8 -> (40+30)/2 = 35
    assert [q["avg_grade"] for q in quartiles] == [55.0, 75.0, 95.0, 35.0]
    assert quartiles[0]["label"] == "Least effort"
    assert quartiles[3]["label"] == "Most effort"


def test_build_effort_quartiles_tie_break_by_user_id_never_grade():
    """Regression: equal-effort students must bucket by user_id, NEVER by grade.
    All 8 have turns=5 (constant effort); grades are distinct. In user_id order
    (u0..u7) the grades are [10,80,20,70,30,60,40,50] — each quartile pairs a
    low+high summing to 90, so every quartile mean is 45.0 (flat). Were the
    tie-break the grade, the buckets would be the fabricated staircase
    [15,35,55,75]; meanwhile Pearson over the constant turns correctly reads 0.0
    (no effort->grade gradient exists)."""
    students = [
        {"turns": 5, "avg_best": g, "email": None, "user_id": uid}
        for uid, g in [
            ("u0", 10.0),
            ("u1", 80.0),
            ("u2", 20.0),
            ("u3", 70.0),
            ("u4", 30.0),
            ("u5", 60.0),
            ("u6", 40.0),
            ("u7", 50.0),
        ]
    ]
    quartiles = pi.build_effort_quartiles(students)
    assert [q["students"] for q in quartiles] == [2, 2, 2, 2]
    # Bucketing follows id order -> flat means, not the grade-sorted staircase.
    assert [q["avg_grade"] for q in quartiles] == [45.0, 45.0, 45.0, 45.0]
    assert [q["avg_grade"] for q in quartiles] != [15.0, 35.0, 55.0, 75.0]
    assert pi.pearson([s["turns"] for s in students], [s["avg_best"] for s in students]) == 0.0


def test_build_retry_payoff_none_when_no_retries():
    aggs = {"u1": [ProblemAgg(10, 1, 50.0, 50.0, True)]}
    assert pi.build_retry_payoff(aggs) is None


def test_build_retry_payoff_hand_computed():
    aggs = {
        "u1": [ProblemAgg(10, 3, 40.0, 85.0, False), ProblemAgg(20, 1, 55.0, 55.0, True)],
        "u2": [ProblemAgg(10, 2, 60.0, 90.0, True)],
    }
    # retried pairs: u1/10 (40->85), u2/10 (60->90). u1/20 is single-attempt.
    assert pi.build_retry_payoff(aggs) == {
        "students_retried": 2,
        "avg_first": 50.0,  # (40+60)/2
        "avg_best": 87.5,  # (85+90)/2
        "avg_gain": 37.5,  # (45+30)/2
    }


def test_build_insights_all_null():
    assert pi.build_insights([], {}) == {
        "correlation": None,
        "effort_quartiles": None,
        "retry_payoff": None,
    }


def test_build_insights_composes_all_three():
    points = _points([(i, i * 10.0) for i in range(1, 9)])
    aggs = {"u1": [ProblemAgg(10, 2, 40.0, 90.0, True)]}
    insights = pi.build_insights(points, aggs)
    assert insights["correlation"]["n"] == 8
    assert insights["effort_quartiles"] is not None
    assert insights["retry_payoff"]["students_retried"] == 1


# --- P3.3 retry spacing: gap_seconds ----------------------------------------

_T0 = datetime(2026, 8, 11, 9, 0, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    """A timestamp ``seconds`` after the fixture epoch."""
    return _T0 + timedelta(seconds=seconds)


def test_gap_seconds_needs_two_stamps_to_have_a_gap():
    # 0 stamps -> no gaps; 1 stamp -> no gaps (a single attempt has no spacing).
    assert pi.gap_seconds([]) == []
    assert pi.gap_seconds([_at(0)]) == []


def test_gap_seconds_two_stamps_is_one_delta():
    assert pi.gap_seconds([_at(0), _at(42)]) == [42.0]


def test_gap_seconds_n_stamps_is_n_minus_one_consecutive_deltas():
    # stamps at 0, 42, 342, 1000 -> deltas 42, 300, 658 (consecutive, not
    # cumulative: never [42, 342, 1000] measured from the first stamp).
    assert pi.gap_seconds([_at(0), _at(42), _at(342), _at(1000)]) == [42.0, 300.0, 658.0]


def test_gap_seconds_unordered_input_yields_absolute_magnitudes():
    """The caller's contract is ascending attempt id, NEVER a sort by time
    (``created_at`` is display-only). A clock-skewed / out-of-order pair must
    therefore still report a real duration, never a negative one."""
    assert pi.gap_seconds([_at(300), _at(0), _at(60)]) == [300.0, 60.0]
    assert all(gap >= 0.0 for gap in pi.gap_seconds([_at(300), _at(0), _at(60)]))
