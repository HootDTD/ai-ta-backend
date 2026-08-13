"""Apollo P3.2 §2.5 — the 6th attention flag, `repeated_misconception`.

Two claims, and they are separable:

1. **Additive only.** A payload with no P3.2 input reproduces today's FIVE
   flags, byte for byte, in the same order — the new flag is appended last and
   no existing flag's spelling or position moves. The teacher UI keys off these
   strings, so a reordering is a silent breakage nothing else would catch.
2. **Level-gated at 3.** The array the flag reads is PERSISTED from wrongness
   level 1 (`done._shadow_misconceptions`), so the gate cannot be "does a row
   exist" — it is the `shadow` marker those sub-level-3 entries carry. The level
   matrix below drives the REAL producer at levels 0/1/3 and feeds its real
   output to the REAL fold, so a marker either side could not drift apart
   without failing here.

Every statistic is hand-computed in the test body; no database.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apollo.handlers import done
from apollo.overseer import wrongness
from apollo.projections import performance_insights as pi
from apollo.projections.performance_insights import ProblemAgg

pytestmark = pytest.mark.unit

_THE_FIVE = ["not_started", "low_effort", "gave_up", "grinding", "rapid_retry"]


def _entry(key: str = "eq1", *, resolved: Any = False, shadow: Any = None) -> dict[str, Any]:
    """One `grader_payload -> 'misconceptions'` entry as the writer emits it."""
    entry: dict[str, Any] = {"canonical_key": key, "resolved": resolved, "evidence_span": "q"}
    if shadow is not None:
        entry["shadow"] = shadow
    return entry


def _agg(**overrides: Any) -> ProblemAgg:
    base: dict[str, Any] = {
        "problem_id": 1,
        "graded_count": 1,
        "first_score": 90.0,
        "best_score": 90.0,
        "best_is_last": False,
    }
    return ProblemAgg(**{**base, **overrides})


def _flags(aggs: list[ProblemAgg]) -> list[str]:
    """`student_flags` with every OTHER flag's precondition deliberately unmet:
    1 attempt (not not_started), 0 turns (not low_effort)."""
    return pi.student_flags(attempts=1, teaching_turns=0, median_words=None, aggs=aggs)


# --- the flag itself --------------------------------------------------------


def test_flag_absent_when_no_repeat():
    assert _flags([_agg()]) == []
    assert _flags([_agg(repeated_misconception=False)]) == []


def test_flag_present_on_two_uncorrected_same_key():
    """`eq1` uncorrected in attempts 11 and 12 of problem 7 -> the pair repeats,
    so the student's row carries the flag."""
    rows = [
        ("u1", 7, 11, _entry("eq1")),
        ("u1", 7, 12, _entry("eq1")),
    ]
    assert pi.repeated_misconception_pairs(rows) == {("u1", 7)}
    assert _flags([_agg(problem_id=7, repeated_misconception=True)]) == ["repeated_misconception"]


def test_corrected_repeat_does_not_flag():
    """A contradiction the student FIXED is persisted (the XP dedup subtracts
    it) but is a success, not an attention signal."""
    rows = [
        ("u1", 7, 11, _entry("eq1", resolved=True)),
        ("u1", 7, 12, _entry("eq1", resolved=True)),
    ]
    assert pi.repeated_misconception_pairs(rows) == set()


def test_one_attempt_twice_is_not_two_attempts():
    """DISTINCT attempts, never rows — a duplicated key inside one attempt's
    array (or a second artifact row for the same attempt) is still one attempt."""
    rows = [
        ("u1", 7, 11, _entry("eq1")),
        ("u1", 7, 11, _entry("eq1")),
    ]
    assert pi.repeated_misconception_pairs(rows) == set()


def test_two_different_keys_once_each_is_not_a_repeat():
    rows = [
        ("u1", 7, 11, _entry("eq1")),
        ("u1", 7, 12, _entry("eq2")),
    ]
    assert pi.repeated_misconception_pairs(rows) == set()


def test_repeat_is_scoped_to_one_student_and_one_problem():
    rows = [
        ("u1", 7, 11, _entry("eq1")),
        ("u2", 7, 12, _entry("eq1")),  # a different student
        ("u1", 8, 13, _entry("eq1")),  # a different problem
    ]
    assert pi.repeated_misconception_pairs(rows) == set()


def test_entry_without_a_canonical_key_is_never_counted():
    rows = [("u1", 7, i, {"resolved": False}) for i in (11, 12)]
    assert pi.repeated_misconception_pairs(rows) == set()


# --- append-only: the additive-keys rule ------------------------------------


def test_absent_payload_reproduces_the_five_flags():
    """No P3.2 side map anywhere -> the exact pre-P3.2 flag list. Drives every
    one of the five so the assertion covers the whole vocabulary and its order."""
    aggs = [
        _agg(problem_id=1, graded_count=3, first_score=10.0, best_score=11.0, best_is_last=True),
        _agg(
            problem_id=2,
            graded_count=2,
            first_score=10.0,
            best_score=90.0,
            min_gap_seconds=30.0,
        ),
    ]
    assert pi.student_flags(attempts=0, teaching_turns=5, median_words=2.0, aggs=aggs) == _THE_FIVE


def test_flag_order_is_stable_and_appended_last():
    """The 6th flag is APPENDED: the five keep their spelling and position, and
    the new one can only ever be last."""
    aggs = [
        _agg(problem_id=1, graded_count=3, first_score=10.0, best_score=11.0, best_is_last=True),
        _agg(
            problem_id=2,
            graded_count=2,
            first_score=10.0,
            best_score=90.0,
            min_gap_seconds=30.0,
            repeated_misconception=True,
        ),
    ]
    flags = pi.student_flags(attempts=0, teaching_turns=5, median_words=2.0, aggs=aggs)
    assert flags == [*_THE_FIVE, "repeated_misconception"]
    assert flags[: len(_THE_FIVE)] == _THE_FIVE


def test_problem_agg_default_keeps_every_pre_p32_construction_valid():
    """The field is defaulted, so a 5-positional-arg fixture still builds."""
    assert ProblemAgg(1, 2, 50.0, 60.0, True).repeated_misconception is False


def test_absent_side_map_yields_no_repeated_pair():
    """`problem_aggregates` without the side map: today's aggregates exactly."""
    rows = [("u1", 7, 11, 50.0), ("u1", 7, 12, 60.0)]
    without = pi.problem_aggregates(rows)
    with_empty = pi.problem_aggregates(rows, repeated_pairs=set())
    assert without == with_empty
    assert [a.repeated_misconception for a in without["u1"]] == [False]


def test_side_map_marks_only_the_named_pair():
    rows = [("u1", 7, 11, 50.0), ("u1", 8, 12, 60.0)]
    aggs = pi.problem_aggregates(rows, repeated_pairs={("u1", 7)})
    assert {a.problem_id: a.repeated_misconception for a in aggs["u1"]} == {7: True, 8: False}


# --- the level matrix: 0 / 1 / 3 -------------------------------------------


def _finding(node_id: str = "eq1") -> Any:
    """A CORROBORATED finding — the population `_shadow_misconceptions` keeps."""
    return wrongness.WrongnessFinding(
        node_id=node_id,
        quote="pressure rises when speed rises",
        contradicts="bernoulli",
        kind="opposite_direction",
        corroborated=True,
        resolved=False,
        apollo_elicited=False,
        would_ceiling=False,
    )


def _pairs_at_level(level: int) -> set[tuple[str, int]]:
    """Two graded attempts at one problem, both graded at ``level``, both
    recording the same corroborated finding — driven through the REAL producer
    and the REAL fold, with nothing hand-written in between."""
    persisted = done._shadow_misconceptions([_finding()], level=level) or []
    return pi.repeated_misconception_pairs(
        [("u1", 7, attempt_id, entry) for attempt_id in (11, 12) for entry in persisted]
    )


@pytest.mark.parametrize("level", [0, 1, 2])
def test_the_flag_stays_dark_below_level_3(level: int):
    """Level 0 persists nothing; levels 1 and 2 persist a `shadow`-marked entry
    that the teacher surface must exclude. S10 puts this surface on rung 3."""
    assert _pairs_at_level(level) == set()


@pytest.mark.parametrize("level", [3, 4])
def test_the_flag_lights_up_at_level_3(level: int):
    assert _pairs_at_level(level) == {("u1", 7)}


def test_the_shadow_marker_is_the_only_thing_gating_it():
    """Strip the marker off a level-1 entry and the fold counts it — proof the
    gate is the marker and not some incidental shape difference between rungs."""
    marked = done._shadow_misconceptions([_finding()], level=1) or []
    unmarked = [
        {k: v for k, v in entry.items() if k != done.SHADOW_MISCONCEPTION_KEY} for entry in marked
    ]
    assert unmarked == (done._shadow_misconceptions([_finding()], level=3) or [])
    assert pi.repeated_misconception_pairs(
        [("u1", 7, aid, entry) for aid in (11, 12) for entry in unmarked]
    ) == {("u1", 7)}


# --- the visibility predicate ----------------------------------------------


@pytest.mark.parametrize("marked", [True, "true", "True", " TRUE "])
def test_shadow_marker_recognised_as_bool_or_text(marked: Any):
    """`jsonb_array_elements` yields a real bool; the `->>` projection yields the
    string "true". Both must read as marked."""
    assert not pi.teacher_visible_misconception(_entry(shadow=marked))
    assert not pi.teacher_visible_misconception(_entry(resolved=marked))


@pytest.mark.parametrize("unmarked", [None, False, "false", "", 0, 1])
def test_only_a_true_marker_hides_an_entry(unmarked: Any):
    assert pi.teacher_visible_misconception(_entry(shadow=unmarked))


def test_a_pre_p32_entry_with_no_marker_keys_is_still_visible():
    """Every artifact row written before P3.2 carries neither key; excluding
    those would delete the pre-existing teacher signal."""
    assert pi.teacher_visible_misconception({"canonical_key": "eq1", "evidence_span": "q"})


# --- the loader is a thin row reader ---------------------------------------


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> Any:
        return SimpleNamespace(all=lambda: self._rows)


class _FakeDB:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        self.statements.append((statement, params))
        return _FakeResult(self._rows)


async def test_loader_folds_rows_into_pairs_and_scopes_by_course():
    db = _FakeDB(
        [
            {"user_id": "u1", "problem_id": 7, "attempt_id": 11, "entry": _entry("eq1")},
            {"user_id": "u1", "problem_id": 7, "attempt_id": 12, "entry": _entry("eq1")},
            {"user_id": "u1", "problem_id": 7, "attempt_id": 13, "entry": "not-a-mapping"},
        ]
    )
    assert await pi.load_repeated_misconception_pairs(db, search_space_id=3) == {("u1", 7)}
    statement, params = db.statements[0]
    assert params == {"search_space_id": 3}
    sql = str(statement)
    assert "internal.grading_runs" in sql
    assert "role = 'canonical'" in sql
    # The array guard: a payload whose `misconceptions` is an object must yield
    # zero rows, never "cannot extract elements from an object" mid-request.
    assert "jsonb_typeof" in sql
