"""Campaign-plan Task B3 — classroom aggregation projections (spec
2026-07-01 section 2, "classroom (heatmap + struggle signals)").

Both functions are PURE READ-SIDE aggregation over already-durable rows
(``app.learner_state`` / ``internal.grading_runs``, DB-14/A7 artifacts-only
merge) -- no new grading, no new inference, no LLM/Neo4j calls. They back the
teacher-facing classroom endpoints registered in ``apollo/api.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.grading.artifact_build import LEDGER_STATUS_UNPROBED

# Spec §5 "windowed" struggle signals: default lookback, overridable per call
# (route reads it from a query param -- see api.py).
DEFAULT_WINDOW_DAYS: int = 14

# Bounded result sizes -- these are "worst offenders" lists for a teacher
# dashboard, not a full export.
_LOWEST_COVERAGE_LIMIT: int = 10
_TOP_MISCONCEPTIONS_LIMIT: int = 10

# node_ledger statuses that carry a REFERENCE canonical_key (i.e. can be
# meaningfully attributed to a specific concept-graph node for a "coverage"
# signal). An "unresolved" row from an audited UNRESOLVED finding is keyed on
# a STUDENT node id (see apollo.grading.artifact_build._unresolved_ledger_entry)
# -- including THOSE here would surface opaque per-student ids instead of a
# reusable per-concept signal, so they stay excluded via this status list.
#
# A "unresolved" row from a MISSING_NODE finding
# (apollo.grading.artifact_build._missing_ledger_entry) is the other case: it
# IS keyed on a real reference canonical_key (a concept the student never
# mentioned at all) and is distinguished from the student-id case by
# ``evidence_span IS NULL`` (a real failed resolution attempt always carries a
# span string, even if empty -- ``None`` means "never attempted"). Those rows
# are 0.0-coverage contributions in their own right (see the lowest-coverage
# query below) so a never-taught concept can surface as a worst offender.
#
# 2026-08-07 P1.2b + review fix: a graded node the questioning loop never raised
# now lands under its OWN ledger status (``unprobed``) instead of that
# ``unresolved`` + NULL-span branch. Dropping it from the query would silently
# delete the very signal the branch exists for — "a graded node of this course
# is never being taught or asked about" — so it is matched explicitly and
# contributes 0.0 exactly as it did before, with its own ``n_unprobed`` count so
# the teacher can tell "the class got this wrong" from "Apollo never asked".
_LEDGER_STATUSES_WITH_REFERENCE_KEY = ("credited", "misconception", LEDGER_STATUS_UNPROBED)

# Apollo P3.2 §2.5 / S10 — which ``grader_payload -> 'misconceptions'`` entries a
# TEACHER is allowed to see. The array is persisted from wrongness level 1 (see
# ``apollo.handlers.done._shadow_misconceptions``) so cross-attempt question
# memory and the XP dedup have a record to read, but the teacher surfaces only
# serve from level 3. Two exclusions, for two different reasons:
#
# - ``shadow``: the marker every entry written BELOW level 3 carries. Without
#   this predicate this aggregate would light up at level >= 1 — a deviation
#   from S10, on exactly the corpus that exists to be invisible.
# - ``resolved``: a contradiction the student FIXED. It is persisted only so the
#   decision-7 XP bonus can dedup itself once per (user x problem x node); it is
#   a success, not a struggle signal, and counting it inflates the very list a
#   teacher reads as "what is the class getting wrong".
#
# The key is RE-SPELLED here rather than imported, to keep this pure projection
# out of the ``done`` -> Neo4j import chain (same convention as
# ``projections/scorecard``'s status markers); the spellings are pinned equal by
# ``tests/test_misconception_surfaces_light_up.py``. Both markers render through
# ``->>`` as TEXT, so an absent key is NULL and ``IS DISTINCT FROM 'true'``
# KEEPS it — every pre-P3.2 row stays counted.
SHADOW_MISCONCEPTION_KEY = "shadow"
_TEACHER_VISIBLE_MISCONCEPTION_SQL = f"""
                  AND (misc ->> '{SHADOW_MISCONCEPTION_KEY}') IS DISTINCT FROM 'true'
                  AND (misc ->> 'resolved') IS DISTINCT FROM 'true'
"""


async def mastery_heatmap(db: AsyncSession, *, search_space_id: int) -> list[dict[str, Any]]:
    """Roster x concept mastery grid (spec §2): one row per ``(user_id,
    concept_id)`` pair, aggregating every ``apollo_learner_state`` row for
    entities under that concept into a single mastery/confidence cell (a
    concept can own more than one Layer-1 entity; the heatmap is drawn at
    concept granularity). Pure aggregation -- no new inference.

    Leverages the migration-035 ``(search_space_id, entity_id)`` index on
    ``apollo_learner_state`` for the course-wide scan.
    """
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT
                    ls.user_id AS user_id,
                    e.concept_id AS concept_id,
                    avg(ls.mastery) AS mastery,
                    avg(ls.confidence) AS confidence
                FROM app.learner_state ls
                JOIN app.learner_entities e ON e.id = ls.entity_id
                WHERE ls.course_id = :search_space_id
                GROUP BY ls.user_id, e.concept_id
                ORDER BY ls.user_id, e.concept_id
                """
                ),
                {"search_space_id": search_space_id},
            )
        )
        .mappings()
        .all()
    )

    return [
        {
            "user_id": str(row["user_id"]),
            "concept_id": int(row["concept_id"]),
            "mastery": float(row["mastery"]),
            "confidence": float(row["confidence"]),
        }
        for row in rows
    ]


async def struggle_signals(
    db: AsyncSession,
    *,
    search_space_id: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Windowed class-level struggle signals (spec §2) over
    ``internal.grading_runs`` rows for a course: abstention count,
    LLM-fallback count, the reference nodes with the lowest coverage, and the
    most frequently asserted misconceptions. Pure aggregation via
    ``jsonb_array_elements`` lateral expansion over ``node_ledger`` /
    ``grader_payload -> 'misconceptions'`` -- no new inference.

    ``top_misconceptions``'s LATERAL is `jsonb_typeof(...) = 'array'`-guarded,
    matching `persistence.attempt_history.prior_wrongness_findings` and
    `performance_insights.load_repeated_misconception_pairs`: `grader_payload`
    is free-form JSONB with no CHECK, and Apollo P3.2 is what starts writing
    that key, so a non-array value from any writer must yield zero rows rather
    than 500 the whole teacher dashboard.

    ``top_misconceptions`` counts only TEACHER-VISIBLE misconception entries
    (see ``_TEACHER_VISIBLE_MISCONCEPTION_SQL``): the array is persisted from
    Apollo P3.2 wrongness level 1 for internal readers, so entries below level 3
    carry a ``shadow`` marker and are excluded here, as are ``resolved`` ones
    (a contradiction the student fixed is not a struggle signal).

    ``abstention_count`` and ``fallback_count`` read different row shapes
    (see ``apollo.handlers.artifact_writer.write_artifacts`` /
    ``apollo.grading.artifact_build.build_llm_artifact``/``build_graph_artifact``):

    - ``build_llm_artifact`` hardcodes ``abstention.abstained = None`` --
      abstention is a GRAPH-grader-only concept -- which
      ``artifact_writer._artifact_row`` lifts into the typed, NOT NULL
      ``abstained`` column as ``false``. An abstained shadow grade always
      falls back to LLM for the served (``role='canonical'``) grade, so
      ``abstained = true`` only ever lands on the GRAPH artifact row
      (``grader_used = 'graph'``), which is ``role='pair'`` whenever the
      shadow abstained (the canonical row is the LLM fallback in that case).
      ``abstention_count`` therefore counts by ``grader_used = 'graph'``
      directly, not by ``role`` -- scoping to ``role='canonical'`` (the old
      query) is structurally dead, since a promoted, non-abstained graph
      grade (the only case where ``role='canonical'`` AND
      ``grader_used='graph'``) can never have ``abstained = true``.
    - ``fallback_count`` counts a "real" fallback: a served
      (``role='canonical'``) LLM-fallback row that exists BECAUSE a graph
      shadow attempt ran and fell back (i.e. a paired ``role='pair'``
      ``grader_used='graph'`` row exists for the same ``attempt_id``) --
      not every LLM-served attempt (the shadow-chain-off build state serves
      LLM for every attempt with no graph row at all, which is not a
      "fallback").

    Neither signal is scoped to a single ``role`` value up front; each
    ``FILTER`` clause states its own row shape. Leverages
    ``grading_runs__course_user__idx`` ``(course_id, user_id)`` for the
    ``course_id`` equality prefix -- the target DDL has no composite
    ``(course_id, concept_id, created_at)`` index (DB-14 retarget: the old
    ``ix_grading_artifacts_space_concept_time`` shape is gone), so the
    ``created_at`` window filter is not itself index-backed.
    """
    window_start = datetime.now(UTC) - timedelta(days=window_days)
    params: dict[str, Any] = {"search_space_id": search_space_id, "window_start": window_start}

    counts = (
        (
            await db.execute(
                text(
                    """
                SELECT
                    count(*) FILTER (
                        WHERE a.grader_used = 'graph'
                          AND a.abstained
                    ) AS abstention_count,
                    count(*) FILTER (
                        WHERE a.role = 'canonical'
                          AND a.grader_used = 'llm_fallback'
                          AND EXISTS (
                              SELECT 1
                              FROM internal.grading_runs p
                              WHERE p.attempt_id = a.attempt_id
                                AND p.role = 'pair'
                                AND p.grader_used = 'graph'
                          )
                    ) AS fallback_count
                FROM internal.grading_runs a
                WHERE a.course_id = :search_space_id
                  AND a.created_at >= :window_start
                """
                ),
                params,
            )
        )
        .mappings()
        .one()
    )

    lowest_coverage_rows = (
        (
            await db.execute(
                text(
                    """
                SELECT
                    node ->> 'canonical_key' AS key,
                    avg(
                        CASE WHEN node ->> 'status' = 'credited' THEN 1.0 ELSE 0.0 END
                    ) AS mean_coverage,
                    count(*) AS n,
                    count(*) FILTER (
                        WHERE node ->> 'status' = :unprobed_status
                    ) AS n_unprobed
                FROM internal.grading_runs a,
                     LATERAL jsonb_array_elements(a.node_ledger) AS node
                WHERE a.course_id = :search_space_id
                  AND a.role = 'canonical'
                  AND a.created_at >= :window_start
                  AND node ->> 'canonical_key' IS NOT NULL
                  AND (
                      node ->> 'status' = ANY(:statuses)
                      OR (
                          node ->> 'status' = 'unresolved'
                          AND node ->> 'evidence_span' IS NULL
                      )
                  )
                GROUP BY node ->> 'canonical_key'
                ORDER BY mean_coverage ASC, key ASC
                LIMIT :limit
                """
                ),
                {
                    **params,
                    "statuses": list(_LEDGER_STATUSES_WITH_REFERENCE_KEY),
                    "unprobed_status": LEDGER_STATUS_UNPROBED,
                    "limit": _LOWEST_COVERAGE_LIMIT,
                },
            )
        )
        .mappings()
        .all()
    )

    top_misconception_rows = (
        (
            await db.execute(
                text(
                    f"""
                SELECT
                    misc ->> 'canonical_key' AS key,
                    count(*) AS n
                FROM internal.grading_runs a,
                     LATERAL jsonb_array_elements(
                         CASE
                             WHEN jsonb_typeof(a.grader_payload -> 'misconceptions') = 'array'
                             THEN a.grader_payload -> 'misconceptions'
                             ELSE '[]'::jsonb
                         END
                     ) AS misc
                WHERE a.course_id = :search_space_id
                  AND a.role = 'canonical'
                  AND a.created_at >= :window_start
                  AND misc ->> 'canonical_key' IS NOT NULL
                  {_TEACHER_VISIBLE_MISCONCEPTION_SQL}
                GROUP BY misc ->> 'canonical_key'
                ORDER BY n DESC, key ASC
                LIMIT :limit
                """
                ),
                {**params, "limit": _TOP_MISCONCEPTIONS_LIMIT},
            )
        )
        .mappings()
        .all()
    )

    return {
        "abstention_count": int(counts["abstention_count"]),
        "fallback_count": int(counts["fallback_count"]),
        "lowest_coverage_nodes": [
            {
                "key": row["key"],
                "mean_coverage": float(row["mean_coverage"]),
                "n": int(row["n"]),
                # How many of those n were never raised with the student at all
                # (P1.2b `unprobed`) — a low mean_coverage with a high
                # n_unprobed is a broken rubric or ask budget, not a class that
                # failed to learn the concept.
                "n_unprobed": int(row["n_unprobed"]),
            }
            for row in lowest_coverage_rows
        ],
        "top_misconceptions": [
            {"key": row["key"], "count": int(row["n"])} for row in top_misconception_rows
        ],
    }
