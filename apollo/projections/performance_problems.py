"""Per-problem drill-down block for the teacher class-performance payload.

Owns the whole ``problems[]`` block: the best-wins letter distribution, the
per-problem metadata (code, full text, concept), the per-problem best-wins
student list, and the per-reference-node right/wrong breakdown.

The node breakdown **REUSES the exact per-node credit helper the served topic
score uses** — ``topic_score._credit_for_node`` over ``coverage.per_step`` +
``coverage.procedure_scores`` — plus the same graded-node-type set and the same
``_display_name_for`` the scorer resolves node names with. It is a shared
function, never a copy, so the drill-down can never disagree with the grade the
student was actually shown (this mirrors ``transcript_coverage.py``, which
already imports the same private scorer helpers).

Everything but the two thin ``load_*`` coroutines is a **pure function on plain
rows / prebuilt nodes**, so the projection tests exercise it with hand-computed
fixtures and no database. ``performance.py``'s assembler composes this block and
reuses ``letter_distribution`` for the class-level ``grade_distribution`` so the
two never disagree.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.ontology import Node
from apollo.overseer.rubric import LETTER_BANDS
from apollo.overseer.topic_score import (
    _GRADED_NODE_TYPES,
    _credit_for_node,
    _display_name_for,
)
from apollo.persistence.models import Concept, Problem
from apollo.schemas.problem import Problem as ProblemSchema

logger = logging.getLogger(__name__)

# ``_credit_for_node`` status → the drill-down count bucket the UI renders as a
# green/blue/red stacked bar. Covered/partial/missing is exactly the served
# topic-score status vocabulary (topic_score.TopicStatus).
_STATUS_TO_BUCKET: dict[str, str] = {
    "covered": "understood",
    "partial": "partial",
    "missing": "missed",
}


def _round1(value: Any) -> float:
    return round(float(value), 1)


def letter_distribution(best_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-wins letter counts over every band the grader can emit (``LETTER_BANDS``
    is the grader's own table), zeros included so the class shape is visible even
    where a bucket is (still) empty. Verbatim served letters — never re-derived."""
    counts: dict[str, int] = {}
    for row in best_rows:
        counts[row["letter"]] = counts.get(row["letter"], 0) + 1
    return [
        {"letter": letter, "count": counts.get(letter, 0)} for _threshold, letter in LETTER_BANDS
    ]


def _usable_coverage(coverage: Any) -> dict[str, Any] | None:
    """A coverage dict is usable for node tallies only when it actually carries
    graded verdicts (a non-empty ``per_step`` or ``procedure_scores``). An absent
    or empty coverage (e.g. a pre-topic-score attempt whose snapshot carried no
    coverage) is EXCLUDED, so a node is never counted ``missed`` merely because
    the attempt has nothing to read."""
    if not isinstance(coverage, dict):
        return None
    if coverage.get("per_step") or coverage.get("procedure_scores"):
        return coverage
    return None


def aggregate_nodes(
    graded_nodes: list[Node], coverages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per graded reference node, tally understood/partial/missed over each
    included attempt's coverage using the SAME ``_credit_for_node`` the served
    topic score uses. ``graded`` = number of included attempts (every coverage
    here is already usable). Node order follows the reference graph exactly as
    the scorer walks it; ``display_name`` is the scorer's own resolution."""
    result: list[dict[str, Any]] = []
    for node in graded_nodes:
        counts = {"understood": 0, "partial": 0, "missed": 0}
        for coverage in coverages:
            _credit, status = _credit_for_node(node.node_id, coverage)
            counts[_STATUS_TO_BUCKET[status]] += 1
        result.append(
            {
                "node_id": node.node_id,
                "display_name": _display_name_for(node),
                "node_type": node.node_type,
                "understood": counts["understood"],
                "partial": counts["partial"],
                "missed": counts["missed"],
                "graded": len(coverages),
            }
        )
    return result


def students_for(
    rows: list[dict[str, Any]], identities: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """The per-problem best-wins student list — one row per distinct student,
    email from the shared identities map, ordered by score desc (id as the stable
    tie-break so equal scores render deterministically)."""
    ordered = sorted(rows, key=lambda r: (-r["score"], r["user_id"]))
    return [
        {
            "user_id": r["user_id"],
            "email": identities.get(r["user_id"], {}).get("email"),
            "score": r["score"],
            "letter": r["letter"],
        }
        for r in ordered
    ]


def build_problems(
    best_rows: list[dict[str, Any]],
    meta_by_problem: dict[int, dict[str, Any]],
    identities: dict[str, dict[str, Any]],
    graded_nodes_by_problem: dict[int, list[Node]],
) -> list[dict[str, Any]]:
    """One row per problem with >=1 graded best attempt: full text, best-wins
    letter distribution (all bands incl. zeros), mean best score, distinct graded
    students, the per-student best-wins list, and the per-node right/wrong
    breakdown. Ordered by concept_name, then problem_code."""
    by_problem: dict[int, list[dict[str, Any]]] = {}
    for row in best_rows:
        by_problem.setdefault(row["problem_id"], []).append(row)
    problems: list[dict[str, Any]] = []
    for problem_id, rows in by_problem.items():
        meta = meta_by_problem.get(problem_id)
        if meta is None:  # pragma: no cover - best_rows FK-guaranteed to a problem
            continue
        scores = [r["score"] for r in rows]
        usable = [cov for r in rows if (cov := _usable_coverage(r.get("coverage"))) is not None]
        problems.append(
            {
                "problem_id": problem_id,
                "problem_code": meta["problem_code"],
                "problem_text": meta["problem_text"],
                "concept_id": meta["concept_id"],
                "concept_name": meta["concept_name"],
                "students_graded": len(rows),  # one best row per distinct student
                "avg_best": _round1(sum(scores) / len(scores)),
                "distribution": letter_distribution(rows),
                "students": students_for(rows, identities),
                "nodes": aggregate_nodes(graded_nodes_by_problem.get(problem_id, []), usable),
            }
        )
    problems.sort(key=lambda p: (p["concept_name"], p["problem_code"]))
    return problems


async def load_problem_meta(
    db: AsyncSession, *, problem_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """``problem_id -> {problem_code, problem_text, concept_id, concept_name}`` for
    the given problems (those with >=1 graded best row), joined through their
    concept. ``problem_text`` is the full statement (the payload is roster-bounded,
    so full text is fine — the UI renders its own snippet)."""
    if not problem_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(
                    Problem.id,
                    Problem.problem_code,
                    Problem.problem_text,
                    Concept.id.label("concept_id"),
                    Concept.display_name.label("concept_name"),
                )
                .join(Concept, Concept.id == Problem.concept_id)
                .where(Problem.id.in_(problem_ids))
            )
        )
        .mappings()
        .all()
    )
    return {
        int(row["id"]): {
            "problem_code": row["problem_code"],
            "problem_text": row["problem_text"],
            "concept_id": int(row["concept_id"]),
            "concept_name": row["concept_name"],
        }
        for row in rows
    }


async def load_graded_reference_nodes(
    db: AsyncSession, *, problem_ids: list[int]
) -> dict[int, list[Node]]:
    """``problem_id -> [graded reference Node, ...]`` — the reference graph derived
    exactly the way ``done.py`` / ``topic_score`` obtain it (``Problem.to_kg_graph``),
    filtered to the graded node types the scorer grades. The ``attempt_id`` stamp is
    irrelevant to node identity/type/display, so a placeholder is passed.

    Failure-isolated per problem: a malformed reference solution (which would fail
    ``ProblemSchema`` validation) degrades to an empty node list for that problem —
    the drill-down shows no node breakdown rather than voiding the whole payload."""
    if not problem_ids:
        return {}
    rows = (
        await db.execute(
            select(Problem, Concept.slug)
            .join(Concept, Concept.id == Problem.concept_id)
            .where(Problem.id.in_(problem_ids))
        )
    ).all()
    result: dict[int, list[Node]] = {}
    for problem, concept_slug in rows:
        try:
            schema = ProblemSchema.model_validate(
                {
                    **problem.to_pydantic_payload(concept_slug=concept_slug),
                    "database_id": problem.id,
                }
            )
            graph = schema.to_kg_graph(attempt_id=0)
        except Exception as exc:  # defensive: a bad reference graph never voids the panel
            logger.warning(
                "class_performance: reference-graph load failed for problem %s: %s",
                problem.id,
                exc,
            )
            result[int(problem.id)] = []
            continue
        result[int(problem.id)] = [
            node for node in graph.nodes if node.node_type in _GRADED_NODE_TYPES
        ]
    return result
