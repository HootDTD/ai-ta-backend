"""Closed candidate-set assembly for the remaining clarification path.

Authored and emergent misconception candidates were retired by cleanup T-D.
The graph candidate set now contains only the problem and canonical entities.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.graph_compare.problem_inputs import ProblemInputs, build_problem_candidates
from apollo.knowledge_graph.canon_projection import load_entity_specs


async def load_problem_candidates_with_soundness(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
    problem_payload: dict,
) -> tuple[ProblemInputs, bool]:
    """Build candidates with no misconception-bank soundness channel."""
    specs = await load_entity_specs(db, search_space_id=search_space_id, concept_id=concept_id)
    canon_key_by_canonical_key = {spec.canonical_key: spec.key for spec in specs}
    inputs = build_problem_candidates(
        problem_payload,
        {"misconceptions": []},
        canon_key_by_canonical_key=canon_key_by_canonical_key,
    )
    return inputs, False


async def load_problem_candidates(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
    problem_payload: dict,
) -> ProblemInputs:
    inputs, _ = await load_problem_candidates_with_soundness(
        db,
        search_space_id=search_space_id,
        concept_id=concept_id,
        problem_payload=problem_payload,
    )
    return inputs


__all__ = ["load_problem_candidates", "load_problem_candidates_with_soundness"]
