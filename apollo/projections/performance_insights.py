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
    best_is_last: bool  # no graded attempt came after the best-producing one


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


def problem_aggregates(
    attempt_rows: list[tuple[str, int, int, float]],
) -> dict[str, list[ProblemAgg]]:
    """Fold ``(user_id, problem_id, attempt_id, score)`` graded-attempt rows
    into per-student ``ProblemAgg`` lists (one per (student, problem))."""
    grouped: dict[tuple[str, int], list[tuple[int, float]]] = {}
    for user_id, problem_id, attempt_id, score in attempt_rows:
        grouped.setdefault((user_id, problem_id), []).append((attempt_id, float(score)))
    by_student: dict[str, list[ProblemAgg]] = {}
    for (user_id, problem_id), attempts in grouped.items():
        first_score = min(attempts, key=lambda a: a[0])[1]
        # best-wins: max score, latest id breaks ties (matches _SCORE_EXPR order).
        best_id, best_score = max(attempts, key=lambda a: (a[1], a[0]))
        last_id = max(a[0] for a in attempts)
        by_student.setdefault(user_id, []).append(
            ProblemAgg(
                problem_id=problem_id,
                graded_count=len(attempts),
                first_score=first_score,
                best_score=best_score,
                best_is_last=best_id == last_id,
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
    each carrying its mean grade. None below the minimum population."""
    n = len(students)
    if n < MIN_CORRELATION_N:
        return None
    ordered = sorted(students, key=lambda s: (s["turns"], s["avg_best"]))
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
    input) — passed in so the expression lives in exactly one place."""
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
    return problem_aggregates(
        [
            (str(r["user_id"]), int(r["problem_id"]), int(r["attempt_id"]), float(r["score"]))
            for r in rows
        ]
    )
