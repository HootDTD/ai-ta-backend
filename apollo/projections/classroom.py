"""Campaign-plan Task B3 — classroom aggregation projections (spec
2026-07-01 section 2, "classroom (heatmap + struggle signals)").

Both functions are PURE READ-SIDE aggregation over already-durable rows
(``apollo_learner_state`` / ``apollo_grading_artifacts``) -- no new grading,
no new inference, no LLM/Neo4j calls. They back the teacher-facing classroom
endpoints registered in ``apollo/api.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Spec §5 "windowed" struggle signals: default lookback, overridable per call
# (route reads it from a query param -- see api.py).
DEFAULT_WINDOW_DAYS: int = 14

# Bounded result sizes -- these are "worst offenders" lists for a teacher
# dashboard, not a full export.
_LOWEST_COVERAGE_LIMIT: int = 10
_TOP_MISCONCEPTIONS_LIMIT: int = 10

# node_ledger statuses that carry a REFERENCE canonical_key (i.e. can be
# meaningfully attributed to a specific concept-graph node for a "coverage"
# signal). "unresolved" rows from an audited UNRESOLVED finding key on a
# STUDENT node id (see apollo.grading.artifact_build._unresolved_ledger_entry)
# -- including them here would surface opaque per-student ids instead of a
# reusable per-concept signal, so they are excluded.
_LEDGER_STATUSES_WITH_REFERENCE_KEY = ("credited", "misconception")


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
        await db.execute(
            text(
                """
                SELECT
                    ls.user_id AS user_id,
                    e.concept_id AS concept_id,
                    avg(ls.mastery) AS mastery,
                    avg(ls.confidence) AS confidence
                FROM apollo_learner_state ls
                JOIN apollo_kg_entities e ON e.id = ls.entity_id
                WHERE ls.search_space_id = :search_space_id
                GROUP BY ls.user_id, e.concept_id
                ORDER BY ls.user_id, e.concept_id
                """
            ),
            {"search_space_id": search_space_id},
        )
    ).mappings().all()

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
    """Windowed class-level struggle signals (spec §2) over the CANONICAL
    (served-grade) ``apollo_grading_artifacts`` rows for a course: abstention
    count, LLM-fallback count, the reference nodes with the lowest
    credited-vs-misconception ratio, and the most frequently asserted
    misconceptions. Pure aggregation via ``jsonb_array_elements`` lateral
    expansion over ``node_ledger``/``misconceptions`` -- no new inference.

    Scoped to ``role = 'canonical'`` (the grade actually served to the
    student) so a paired shadow-grader artifact never double-counts a Done
    click. Leverages ``ix_grading_artifacts_space_concept_time``
    ``(search_space_id, concept_id, created_at)`` for the windowed scan
    (``concept_id`` is left unconstrained -- the index's equality prefix on
    ``search_space_id`` is still used).
    """
    window_start = datetime.now(UTC) - timedelta(days=window_days)
    params: dict[str, Any] = {"search_space_id": search_space_id, "window_start": window_start}

    counts = (
        await db.execute(
            text(
                """
                SELECT
                    count(*) FILTER (
                        WHERE (abstention ->> 'abstained') = 'true'
                    ) AS abstention_count,
                    count(*) FILTER (
                        WHERE grader_used = 'llm_fallback'
                    ) AS fallback_count
                FROM apollo_grading_artifacts
                WHERE search_space_id = :search_space_id
                  AND role = 'canonical'
                  AND created_at >= :window_start
                """
            ),
            params,
        )
    ).mappings().one()

    lowest_coverage_rows = (
        await db.execute(
            text(
                """
                SELECT
                    node ->> 'canonical_key' AS key,
                    avg(
                        CASE WHEN node ->> 'status' = 'credited' THEN 1.0 ELSE 0.0 END
                    ) AS mean_coverage,
                    count(*) AS n
                FROM apollo_grading_artifacts a,
                     LATERAL jsonb_array_elements(a.node_ledger) AS node
                WHERE a.search_space_id = :search_space_id
                  AND a.role = 'canonical'
                  AND a.created_at >= :window_start
                  AND node ->> 'canonical_key' IS NOT NULL
                  AND node ->> 'status' = ANY(:statuses)
                GROUP BY node ->> 'canonical_key'
                ORDER BY mean_coverage ASC, key ASC
                LIMIT :limit
                """
            ),
            {
                **params,
                "statuses": list(_LEDGER_STATUSES_WITH_REFERENCE_KEY),
                "limit": _LOWEST_COVERAGE_LIMIT,
            },
        )
    ).mappings().all()

    top_misconception_rows = (
        await db.execute(
            text(
                """
                SELECT
                    misc ->> 'canonical_key' AS key,
                    count(*) AS n
                FROM apollo_grading_artifacts a,
                     LATERAL jsonb_array_elements(a.misconceptions) AS misc
                WHERE a.search_space_id = :search_space_id
                  AND a.role = 'canonical'
                  AND a.created_at >= :window_start
                  AND misc ->> 'canonical_key' IS NOT NULL
                GROUP BY misc ->> 'canonical_key'
                ORDER BY n DESC, key ASC
                LIMIT :limit
                """
            ),
            {**params, "limit": _TOP_MISCONCEPTIONS_LIMIT},
        )
    ).mappings().all()

    return {
        "abstention_count": int(counts["abstention_count"]),
        "fallback_count": int(counts["fallback_count"]),
        "lowest_coverage_nodes": [
            {
                "key": row["key"],
                "mean_coverage": float(row["mean_coverage"]),
                "n": int(row["n"]),
            }
            for row in lowest_coverage_rows
        ],
        "top_misconceptions": [
            {"key": row["key"], "count": int(row["n"])} for row in top_misconception_rows
        ],
    }
