"""Algorithmic engagement insights for the teacher class-performance payload.

Everything here is **deterministic and LLM/Neo4j-free** — plain statistics over
rows the live tutoring / grading paths already persist:

- engagement (``app.tutoring_messages``, ``role='student'``, course-scoped, the
  student teaching turns — the Apollo side is ``role='apollo'``);
- retry / first-vs-best over graded attempts (first = lowest ``pa.id``, best =
  the best-wins row, the same served-grade semantics `performance.py` uses
  everywhere).

The stat helpers (`pearson`, `spearman`, `median`, …) and the aggregation /
flag / insight builders are **pure functions on plain lists**, so the validity
anchors (`tests/`) exercise them with hand-computed fixtures and no database.
Only the two thin ``load_*`` coroutines touch the session; they own no grading
logic — they read rows and hand them to the pure functions. `performance.py`
passes its own ``_SCORE_EXPR`` into `load_problem_aggregates` so the served-grade
expression is defined in exactly one place (no import cycle, no duplication).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# --- flag / suppression thresholds (all algorithmic, one authority) ---------

# Correlation + effort quartiles are statistically meaningless on a handful of
# students; both are suppressed (whole block null) below this population size.
MIN_CORRELATION_N = 8

# low_effort: engaged (>= this many teaching turns) but one-liner explanations
# (median words per message below the floor).
LOW_EFFORT_MIN_TURNS = 3
LOW_EFFORT_MAX_MEDIAN_WORDS = 8

# gave_up: a problem whose best graded score never cleared this floor, abandoned
# (no further attempt after the one that produced that best).
GAVE_UP_MAX_BEST = 60

# grinding: sustained retries (>= this many graded attempts) with effectively no
# improvement (best - first at or below this margin).
GRINDING_MIN_ATTEMPTS = 3
GRINDING_MAX_GAIN = 2


class ProblemAgg(NamedTuple):
    """One (student, problem) pair's graded-attempt shape."""

    problem_id: int
    graded_count: int
    first_score: float  # score of the lowest-id graded attempt
    best_score: float  # best-wins score (max, latest id breaks ties)
    best_is_last: bool  # no attempt of ANY result came after the best-producing one
    # P3.3 DISPLAY-ONLY spacing (None unless a created_at side map is threaded
    # in): median/min consecutive gap between this pair's graded attempts, and
    # first-attempt-to-best-attempt elapsed seconds. Defaulted so every
    # pre-P3.3 construction and fixture stays valid.
    median_gap_seconds: float | None = None
    min_gap_seconds: float | None = None
    first_to_best_seconds: float | None = None


def _round1(value: float) -> float:
    return round(float(value), 1)


# --- pure statistics (validity anchors) -------------------------------------


def mean(values: list[float]) -> float:
    """Arithmetic mean; caller guarantees a non-empty list."""
    return sum(values) / len(values)


def median(values: list[float]) -> float | None:
    """Median of the values, or None for an empty list."""
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson product-moment correlation. Returns 0.0 when either variable is
    constant (zero variance — correlation undefined, reported as no signal)."""
    n = len(xs)
    if n == 0:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0.0 or dy == 0.0:
        return 0.0
    return num / (dx * dy)


def _average_ranks(values: list[float]) -> list[float]:
    """1-based ranks with ties assigned the average of the tied positions."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # positions i..j (0-based) -> 1-based mean
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation = Pearson on average ranks (ties averaged)."""
    return pearson(_average_ranks(xs), _average_ranks(ys))


def word_count(content: str) -> int:
    """Whitespace-split word count of a message body."""
    return len(content.split())


def gap_seconds(timestamps: list[datetime]) -> list[float]:
    """Consecutive gaps, in seconds, between one (student, problem) pair's
    graded-attempt timestamps: N stamps -> N-1 gaps (0 or 1 stamp -> none).

    Deltas are ABSOLUTE magnitudes. The caller's contract is ascending attempt
    id — never a sort by time, because ``created_at`` is display-only and must
    never become an ordering key (best-wins is keyed on ``pa.id``) — so a
    clock-skewed row reports a real duration instead of a negative one. The
    deltas are TZ-free absolute durations; never bucket one by local date."""
    return [
        abs((timestamps[i] - timestamps[i - 1]).total_seconds()) for i in range(1, len(timestamps))
    ]


# --- pure aggregations ------------------------------------------------------


def engagement_by_student(
    message_rows: list[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    """Fold raw ``(user_id, content)`` student messages into per-student
    ``{teaching_turns, median_words}``."""
    words_by_user: dict[str, list[int]] = {}
    for user_id, content in message_rows:
        words_by_user.setdefault(user_id, []).append(word_count(content))
    result: dict[str, dict[str, Any]] = {}
    for user_id, words in words_by_user.items():
        med = median([float(w) for w in words])
        result[user_id] = {
            "teaching_turns": len(words),
            "median_words": _round1(med) if med is not None else None,
        }
    return result


def _pair_timings(
    attempt_ids: list[int],
    best_id: int,
    created_at_by_attempt: dict[int, datetime] | None,
) -> dict[str, float | None]:
    """The three DISPLAY-ONLY timing fields for ONE (student, problem) pair.

    Stamps are read in ascending ATTEMPT-ID order — id order is the best-wins
    authority, and ``created_at`` must never become an ordering key. All-None
    when no side map is threaded in (every pure fixture) or the pair's stamps
    are absent, so timing can never fabricate a number it doesn't have."""
    timings: dict[str, float | None] = {
        "median_gap_seconds": None,
        "min_gap_seconds": None,
        "first_to_best_seconds": None,
    }
    if not created_at_by_attempt:
        return timings
    ordered_ids = sorted(attempt_ids)
    stamps = [created_at_by_attempt[aid] for aid in ordered_ids if aid in created_at_by_attempt]
    gaps = gap_seconds(stamps)
    if gaps:
        med = median(gaps)
        timings["median_gap_seconds"] = _round1(med) if med is not None else None
        timings["min_gap_seconds"] = _round1(min(gaps))
    first_at = created_at_by_attempt.get(ordered_ids[0])
    best_at = created_at_by_attempt.get(best_id)
    if first_at is not None and best_at is not None:
        timings["first_to_best_seconds"] = _round1(abs((best_at - first_at).total_seconds()))
    return timings


def problem_aggregates(
    attempt_rows: list[tuple[str, int, int, float]],
    latest_attempt_ids: dict[tuple[str, int], int] | None = None,
    created_at_by_attempt: dict[int, datetime] | None = None,
) -> dict[str, list[ProblemAgg]]:
    """Fold ``(user_id, problem_id, attempt_id, score)`` graded-attempt rows
    into per-student ``ProblemAgg`` lists (one per (student, problem)).

    ``latest_attempt_ids`` maps each ``(user_id, problem_id)`` to the id of its
    latest attempt of **any result** (graded, ungraded, or in-progress). When
    supplied it drives ``best_is_last``, so a student who has since STARTED a
    new (still-ungraded) attempt after their best is not counted as having
    stopped — ``gave_up`` must not fire mid-retry. When omitted, ``best_is_last``
    falls back to the latest attempt among the graded rows given here.

    ``created_at_by_attempt`` maps a graded attempt id to its ``pa.created_at``.
    Supplied it fills the DISPLAY-ONLY timing fields (P3.3); omitted they stay
    None — an optional SIDE MAP, exactly like ``latest_attempt_ids``, so the
    4-tuple row shape (and every fixture built on it) is unchanged."""
    grouped: dict[tuple[str, int], list[tuple[int, float]]] = {}
    for user_id, problem_id, attempt_id, score in attempt_rows:
        grouped.setdefault((user_id, problem_id), []).append((attempt_id, float(score)))
    by_student: dict[str, list[ProblemAgg]] = {}
    for (user_id, problem_id), attempts in grouped.items():
        first_score = min(attempts, key=lambda a: a[0])[1]
        # best-wins: max score, latest id breaks ties (matches _SCORE_EXPR order).
        best_id, best_score = max(attempts, key=lambda a: (a[1], a[0]))
        graded_last_id = max(a[0] for a in attempts)
        # "Came after the best" counts a later attempt of ANY result, so a
        # still-ungraded retry (absent from these graded rows) clears the flag.
        last_id = (
            latest_attempt_ids.get((user_id, problem_id), graded_last_id)
            if latest_attempt_ids is not None
            else graded_last_id
        )
        timings = _pair_timings([a[0] for a in attempts], best_id, created_at_by_attempt)
        by_student.setdefault(user_id, []).append(
            ProblemAgg(
                problem_id=problem_id,
                graded_count=len(attempts),
                first_score=first_score,
                best_score=best_score,
                best_is_last=best_id == last_id,
                median_gap_seconds=timings["median_gap_seconds"],
                min_gap_seconds=timings["min_gap_seconds"],
                first_to_best_seconds=timings["first_to_best_seconds"],
            )
        )
    return by_student


def retry_fields(aggs: list[ProblemAgg]) -> dict[str, Any]:
    """Per-student ``{problems_retried, avg_gain}`` over retried problems
    (>= 2 graded attempts)."""
    retried = [a for a in aggs if a.graded_count >= 2]
    avg_gain = _round1(mean([a.best_score - a.first_score for a in retried])) if retried else None
    return {"problems_retried": len(retried), "avg_gain": avg_gain}


def student_extras(
    *, attempts: int, engagement_core: dict[str, Any] | None, aggs: list[ProblemAgg]
) -> dict[str, Any]:
    """The per-student ``{engagement, flags}`` add-on. ``engagement_core``
    (teaching_turns / median_words) is None when the student has no tutoring
    messages; ``aggs`` is empty when they have no graded attempts."""
    core = engagement_core or {"teaching_turns": 0, "median_words": None}
    retry = retry_fields(aggs)
    return {
        "engagement": {
            "teaching_turns": core["teaching_turns"],
            "median_words": core["median_words"],
            "problems_retried": retry["problems_retried"],
            "avg_gain": retry["avg_gain"],
        },
        "flags": student_flags(
            attempts=attempts,
            teaching_turns=core["teaching_turns"],
            median_words=core["median_words"],
            aggs=aggs,
        ),
    }


def student_flags(
    *,
    attempts: int,
    teaching_turns: int,
    median_words: float | None,
    aggs: list[ProblemAgg],
) -> list[str]:
    """The four algorithmic attention flags, in a stable order."""
    flags: list[str] = []
    if attempts == 0:
        flags.append("not_started")
    if (
        teaching_turns >= LOW_EFFORT_MIN_TURNS
        and median_words is not None
        and median_words < LOW_EFFORT_MAX_MEDIAN_WORDS
    ):
        flags.append("low_effort")
    if any(a.best_score < GAVE_UP_MAX_BEST and a.best_is_last for a in aggs):
        flags.append("gave_up")
    if any(
        a.graded_count >= GRINDING_MIN_ATTEMPTS
        and (a.best_score - a.first_score) <= GRINDING_MAX_GAIN
        for a in aggs
    ):
        flags.append("grinding")
    return flags


# --- pure insight builders --------------------------------------------------


def build_correlation(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Correlate teaching_turns vs avg_best over graded students. None below
    the minimum population (statistically meaningless)."""
    if len(points) < MIN_CORRELATION_N:
        return None
    turns = [float(p["turns"]) for p in points]
    grades = [float(p["avg_best"]) for p in points]
    return {
        "n": len(points),
        "pearson_r": round(pearson(turns, grades), 3),
        "spearman_rho": round(spearman(turns, grades), 3),
        "points": points,
    }


_QUARTILE_LABELS = {
    1: "Least effort",
    2: "Below average",
    3: "Above average",
    4: "Most effort",
}


def build_effort_quartiles(students: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Split graded students into teaching-turn quartiles (Q1 = fewest turns),
    each carrying its mean grade. None below the minimum population.

    Ties (equal ``teaching_turns``) break on ``user_id`` — a neutral,
    deterministic key that must NEVER be the grade. Sorting equal-effort
    students by grade would smear them across quartile boundaries and fabricate
    exactly the monotonic effort->grade gradient this chart exists to test for
    (constant-effort data would then show a rising staircase while Pearson r
    correctly reads 0)."""
    n = len(students)
    if n < MIN_CORRELATION_N:
        return None
    ordered = sorted(students, key=lambda s: (s["turns"], s["user_id"]))
    buckets: dict[int, list[dict[str, Any]]] = {1: [], 2: [], 3: [], 4: []}
    for i, student in enumerate(ordered):
        quartile = min(4, i * 4 // n + 1)
        buckets[quartile].append(student)
    return [
        {
            "quartile": q,
            "label": _QUARTILE_LABELS[q],
            "students": len(buckets[q]),
            "avg_grade": _round1(mean([s["avg_best"] for s in buckets[q]])),
        }
        for q in (1, 2, 3, 4)
    ]


def build_retry_payoff(
    aggregates: dict[str, list[ProblemAgg]],
) -> dict[str, Any] | None:
    """Class-wide first-vs-best over every retried (student, problem) pair.
    None when nobody retried a problem."""
    retried: list[ProblemAgg] = []
    students: set[str] = set()
    for user_id, aggs in aggregates.items():
        for agg in aggs:
            if agg.graded_count >= 2:
                retried.append(agg)
                students.add(user_id)
    if not retried:
        return None
    return {
        "students_retried": len(students),
        "avg_first": _round1(mean([a.first_score for a in retried])),
        "avg_best": _round1(mean([a.best_score for a in retried])),
        "avg_gain": _round1(mean([a.best_score - a.first_score for a in retried])),
    }


def build_insights(
    graded_students: list[dict[str, Any]],
    aggregates: dict[str, list[ProblemAgg]],
) -> dict[str, Any]:
    """Assemble the top-level ``insights`` block from the graded-student points
    (``{turns, avg_best, email}``) and the per-student problem aggregates."""
    return {
        "correlation": build_correlation(graded_students),
        "effort_quartiles": build_effort_quartiles(graded_students),
        "retry_payoff": build_retry_payoff(aggregates),
    }


# --- thin DB loaders (no grading logic) -------------------------------------


async def load_engagement(db: AsyncSession, *, search_space_id: int) -> dict[str, dict[str, Any]]:
    """Per-student engagement from ``app.tutoring_messages`` (student role,
    course-scoped). Attributed to the student via the owning session
    (``app.learning_activities.user_id`` — messages carry no user_id)."""
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT la.user_id AS user_id, tm.content AS content
                FROM app.tutoring_messages tm
                JOIN app.learning_activities la ON la.id = tm.learning_activity_id
                WHERE tm.course_id = :search_space_id
                  AND tm.role = 'student'
                """
                ),
                {"search_space_id": search_space_id},
            )
        )
        .mappings()
        .all()
    )
    return engagement_by_student([(str(r["user_id"]), r["content"] or "") for r in rows])


async def load_problem_aggregates(
    db: AsyncSession, *, search_space_id: int, score_expr: str
) -> dict[str, list[ProblemAgg]]:
    """Per-student (student, problem) graded-attempt aggregates. ``score_expr``
    is `performance.py`'s served-grade SQL fragment (a module constant, not user
    input) — passed in so the expression lives in exactly one place.

    Scoring rows are graded-only, but ``best_is_last`` (the ``gave_up`` signal)
    must recognise retries of ANY result, so a second pass surfaces the latest
    attempt id per (student, problem) over ALL attempts (graded or not)."""
    rows = (
        (
            await db.execute(
                text(
                    f"""
                SELECT pa.user_id AS user_id, pa.problem_id AS problem_id,
                       pa.id AS attempt_id, {score_expr} AS score
                FROM app.problem_attempts pa
                WHERE pa.course_id = :search_space_id
                  AND pa.result = 'graded'
                  AND {score_expr} IS NOT NULL
                ORDER BY pa.user_id, pa.problem_id, pa.id
                """
                ),
                {"search_space_id": search_space_id},
            )
        )
        .mappings()
        .all()
    )
    latest_rows = (
        (
            await db.execute(
                text(
                    """
                SELECT pa.user_id AS user_id, pa.problem_id AS problem_id,
                       max(pa.id) AS latest_id
                FROM app.problem_attempts pa
                WHERE pa.course_id = :search_space_id
                GROUP BY pa.user_id, pa.problem_id
                """
                ),
                {"search_space_id": search_space_id},
            )
        )
        .mappings()
        .all()
    )
    latest_attempt_ids = {
        (str(r["user_id"]), int(r["problem_id"])): int(r["latest_id"]) for r in latest_rows
    }
    return problem_aggregates(
        [
            (str(r["user_id"]), int(r["problem_id"]), int(r["attempt_id"]), float(r["score"]))
            for r in rows
        ],
        latest_attempt_ids,
    )
