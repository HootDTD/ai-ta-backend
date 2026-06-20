"""WU-5B3a-0 — the single source of truth for re-assembling the inputs the
graph-simulation chain (``run_graph_simulation``) consumes.

Both the live Done path (``done.py``, this unit) and the WU-5B3a-1 retry janitor
call ``build_rerun_inputs``, so there is ONE builder and no drift. The builder is
keyed on the DURABLE per-attempt key ``attempt.problem_id`` (NEVER
``sess.current_problem_id``, which ``next.py`` advances), so the LATER janitor
rebuilds the OLD problem the pending attempt belongs to. It performs the
unreconstructable pre-flight (missing ``diagnostic_report`` / ``rubric`` /
``overall`` / Neo4j ``graded_at``) BEFORE any downstream LLM call, raising
``LearnerUpdateUnreconstructableError`` (a terminal dead-letter).

``_find_problem_payload`` is RELOCATED here from ``done.py`` (re-exported there)
so the live path and the builder share one reader — avoiding the circular import
``done.py`` -> ``done_inputs.py`` -> ``done.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import LearnerUpdateUnreconstructableError
from apollo.grading.abstention import min_parser_confidence_of
from apollo.knowledge_graph.store import KGStore
from apollo.ontology import KGGraph
from apollo.persistence.models import (
    ApolloSession,
    Concept,
    ConceptProblem,
    ProblemAttempt,
)
from apollo.persistence.neo4j_client import Neo4jClient


async def _find_problem_payload(
    db: AsyncSession,
    *,
    concept_id: int,
    problem_code: str,
) -> dict:
    """Read the RAW ``ConceptProblem.payload`` dict for the chosen problem.

    WU-4C1: the parsed :class:`Problem` schema (``schemas/problem.py``) DROPS the
    ``declared_paths`` / ``symbolic_mappings`` / per-step ``entity_key`` fields the
    graph-simulation chain needs (``Problem.model_validate`` silently discards
    them). So the shadow chain reads the raw payload directly here, while the OLD
    path keeps the parsed :class:`Problem` (``to_kg_graph`` / ``reference_solution``
    / ``problem_text``). ``problem_code`` is the ``ConceptProblem.problem_code``
    join key (same code ``_find_problem`` matches as ``Problem.id``).

    WU-5B3a-0: relocated from ``done.py`` (re-exported there) so this reader is a
    single source of truth shared by the live path and the retry janitor.
    """
    payload = (
        await db.execute(
            select(ConceptProblem.payload)
            .join(Concept, Concept.id == ConceptProblem.concept_id)
            .where(Concept.id == concept_id)
            .where(ConceptProblem.problem_code == problem_code)
        )
    ).scalar_one()
    return payload


@dataclass(frozen=True)
class RerunInputs:
    """Immutable bundle of the inputs ``run_graph_simulation`` consumes, rebuilt
    from durable per-attempt sources. ``graded_at_iso`` is the raw ISO-8601 string
    from Neo4j — the janitor (WU-5B3a-1) parses it tz-aware."""

    problem_payload: dict
    old_rubric: dict
    student_graph: KGGraph
    parser_confidence: float
    graded_at_iso: str  # raw ISO-8601 string from Neo4j; the janitor parses tz-aware


async def build_rerun_inputs(
    db: AsyncSession,
    neo: Neo4jClient,
    *,
    attempt: ProblemAttempt,
    sess: ApolloSession,
) -> RerunInputs:
    """Re-assemble the ``run_graph_simulation`` inputs from DURABLE state.

    MAKE-OR-BREAK: ``problem_payload`` is keyed on ``attempt.problem_id`` (the
    durable per-attempt key), NEVER ``sess.current_problem_id`` — so the LATER
    janitor rebuilds the OLD problem even after ``next.py`` advanced the session.

    Raises :class:`LearnerUpdateUnreconstructableError` (terminal dead-letter, no
    LLM) BEFORE any downstream grading when ``diagnostic_report`` is None / lacks
    ``rubric`` / the rubric lacks ``overall`` (``calibration.py`` dereferences
    ``["overall"]`` unconditionally), or the frozen Neo4j subgraph has no
    ``graded_at`` (``read_node_graded_at`` returned ``{}``).
    """
    # --- old_rubric pre-flight: dead-letter BEFORE any Neo4j read / downstream LLM
    report = attempt.diagnostic_report
    if report is None:
        raise LearnerUpdateUnreconstructableError(
            attempt_id=int(attempt.id), reason="diagnostic_report_missing"
        )
    old_rubric = report.get("rubric")
    if not isinstance(old_rubric, dict) or "overall" not in old_rubric:
        raise LearnerUpdateUnreconstructableError(
            attempt_id=int(attempt.id), reason="rubric_missing"
        )

    # --- problem_payload: KEY ON attempt.problem_id (durable), never current_problem_id
    problem_payload = await _find_problem_payload(
        db,
        concept_id=sess.concept_id,  # type: ignore[arg-type]  # nullable col, bound at done
        problem_code=str(attempt.problem_id),
    )

    store = KGStore(db, neo)

    # --- student_graph: the frozen per-attempt graph (freeze is PG-only, so the
    # frozen read == the original).
    student_graph = await store.read_graph(attempt_id=int(attempt.id))

    # --- graded_at: the durable done_ts source; dead-letter on an empty subgraph.
    graded_at_map = await store.read_node_graded_at(attempt_id=int(attempt.id))
    if not graded_at_map:
        raise LearnerUpdateUnreconstructableError(
            attempt_id=int(attempt.id), reason="graded_at_missing"
        )
    graded_at_iso = next(iter(graded_at_map.values()))  # all nodes share one done_ts

    parser_confidence = min_parser_confidence_of(student_graph.nodes)

    return RerunInputs(
        problem_payload=problem_payload,
        old_rubric=old_rubric,
        student_graph=student_graph,
        parser_confidence=parser_confidence,
        graded_at_iso=graded_at_iso,
    )
