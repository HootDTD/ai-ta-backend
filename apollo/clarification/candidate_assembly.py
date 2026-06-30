"""Shared candidate-set assembly. The chat (clarification) path and the Done
(grading) path build the SAME closed candidate set; this centralizes the recipe
that previously lived inline in done_grading.py (load bank -> dict -> specs ->
build_problem_candidates) so both call one function (DRY)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.graph_compare.problem_inputs import ProblemInputs, build_problem_candidates
from apollo.handlers.done_grading import _misconceptions_dict
from apollo.knowledge_graph.canon_projection import load_entity_specs
from apollo.overseer.misconception_bank import load_for_concept


async def load_problem_candidates(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
    problem_payload: dict,
) -> ProblemInputs:
    """Assemble the closed candidate set (reference nodes + course misconceptions)
    plus the per-problem symbolic mappings, exactly as the grading path does."""
    entries = await load_for_concept(db, concept_id=concept_id)
    misconceptions = _misconceptions_dict(entries)
    specs = await load_entity_specs(db, search_space_id=search_space_id, concept_id=concept_id)
    canon_key_by_canonical_key = {spec.canonical_key: spec.key for spec in specs}
    return build_problem_candidates(
        problem_payload, misconceptions, canon_key_by_canonical_key=canon_key_by_canonical_key
    )
