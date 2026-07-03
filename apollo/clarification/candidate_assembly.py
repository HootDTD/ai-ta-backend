"""Shared candidate-set assembly. The closed candidate set recipe lives HERE
and BOTH the chat (clarification) path and the Done (grading) path call it —
``load_problem_candidates_with_soundness`` is the single entry point (load bank
→ dict → specs → build_problem_candidates); ``load_problem_candidates`` is a
thin chat-path delegate so the candidate set is identical by construction."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.graph_compare.problem_inputs import ProblemInputs, build_problem_candidates
from apollo.knowledge_graph.canon_projection import load_entity_specs
from apollo.overseer.misconception_bank import load_for_concept


def _bank_applicable(entries: list, concept_id: int | None) -> bool:
    """The D5/D6 soundness-applicability predicate in isolation: True iff the
    concept has a non-empty misconception bank AND a non-NULL ``concept_id`` (a
    NULL concept can never have a bank, so soundness would fail-open). The SINGLE
    definition of "bank applicable", shared by the candidate-set recipe below and
    the artifact-path ``misconception_bank_applicable`` helper — they can never
    drift."""
    return bool(entries) and concept_id is not None


def _misconceptions_dict(entries: list) -> dict:
    """Map ``MisconceptionEntry`` rows onto the dict shape
    ``candidates_from_misconceptions`` reads: ``{"misconceptions": [{key,
    trigger_phrases, opposes, display_name}, ...]}``.

    Field translation (§3.1 step 1 / Risk #2): ``code -> key``,
    ``description -> display_name``. The bank carries no ``opposes`` column today,
    so ``opposes`` is ``None`` (a missing opposes-link just disables conflict-pair
    detection for that misconception — tolerated; WU-4C1 writes no events anyway).
    """
    return {
        "misconceptions": [
            {
                "key": e.code,
                "trigger_phrases": list(e.trigger_phrases),
                "opposes": None,
                "display_name": e.description,
            }
            for e in entries
        ]
    }


async def load_problem_candidates_with_soundness(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
    problem_payload: dict,
) -> tuple[ProblemInputs, bool]:
    """THE single candidate-set recipe used by BOTH the chat (clarification) path and
    the Done (grading) path, so both consume a byte-identical closed candidate set.
    Returns (inputs, bank_applicable). ``bank_applicable`` is the D5/D6 soundness
    applicability flag: True iff the misconception bank is non-empty AND concept_id is
    not None (a NULL concept can never have a bank, so soundness would fail-open)."""
    entries = await load_for_concept(db, concept_id=concept_id)  # type: ignore[arg-type]
    misconceptions = _misconceptions_dict(entries)
    specs = await load_entity_specs(db, search_space_id=search_space_id, concept_id=concept_id)
    canon_key_by_canonical_key = {spec.canonical_key: spec.key for spec in specs}
    inputs = build_problem_candidates(
        problem_payload, misconceptions, canon_key_by_canonical_key=canon_key_by_canonical_key
    )
    bank_applicable = _bank_applicable(entries, concept_id)
    return inputs, bank_applicable


async def misconception_bank_applicable(
    db: AsyncSession, *, concept_id: int | None
) -> bool:
    """The D5/D6 soundness-applicability flag in ISOLATION — the bank-empty fact
    without building the closed candidate set. The LLM artifact path
    (``artifact_writer.write_artifacts`` on a shadow-off / LLM-served Done-click)
    needs to know whether the concept's misconception bank is empty so
    ``build_llm_artifact`` can stamp the lane-B3a/D1 ``misconceptions_status``
    marker, but has no ``problem_payload`` to build candidates from and does not
    need one (``bank_applicable`` depends only on ``load_for_concept`` +
    ``concept_id``). Same source, same predicate as
    ``load_problem_candidates_with_soundness`` — so the graph and LLM paths can
    never disagree about whether the bank was empty."""
    if concept_id is None:
        # A NULL concept can never own a bank — short-circuit before the query
        # (``_bank_applicable`` would return False regardless of its result).
        return False
    entries = await load_for_concept(db, concept_id=concept_id)
    return _bank_applicable(entries, concept_id)


async def load_problem_candidates(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
    problem_payload: dict,
) -> ProblemInputs:
    """Chat-path entry point: the closed candidate set only (no soundness flag).
    Delegates to load_problem_candidates_with_soundness so the candidate set is
    identical to the grading path by construction (no recipe duplication)."""
    inputs, _ = await load_problem_candidates_with_soundness(
        db, search_space_id=search_space_id, concept_id=concept_id, problem_payload=problem_payload
    )
    return inputs
