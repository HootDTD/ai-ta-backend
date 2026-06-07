# apollo/textbook_ingest/types.py
"""Pydantic types for the six textbook-ingest stages.

reference_solution is list[ReferenceStep] (NOT KGGraph) to round-trip with the
locked apollo.schemas.problem.Problem schema; the validator converts to KGGraph
internally (see kg_convert.py).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from apollo.schemas.problem import Difficulty, ReferenceStep
from apollo.subjects import CanonicalSymbols, ForbiddenNamedLaws, SolverHints

__all__ = [
    "ConceptCandidate",
    "ConceptResolution",
    "ConceptRegistryEntry",
    "ProblemCandidate",
    "ExtractedProblem",
    "ValidatedProblem",
    "RejectedProblem",
]


class ConceptCandidate(BaseModel):
    proposed_subject_id: str
    proposed_concept_id: str
    scope_summary: str
    source_chunk_ids: list[str]
    confidence: float


class ConceptResolution(BaseModel):
    kind: Literal["new", "matched_existing"]
    candidate: ConceptCandidate
    matched_concept_id: str | None = None
    matched_subject_id: str | None = None
    resolution_method: Literal["slug", "embedding", "llm_judge"] | None = None
    similarity_score: float | None = None


# Serializable counterpart to apollo.subjects.ConceptDefinition; omits problems_dir so it can round-trip through Neo4j.
class ConceptRegistryEntry(BaseModel):
    subject_id: str
    concept_id: str
    scope_summary: str
    canonical_symbols: CanonicalSymbols
    normalization_map: dict[str, str]
    parser_prompt_template: str
    solver_hints: SolverHints
    forbidden_named_laws: ForbiddenNamedLaws = Field(default_factory=ForbiddenNamedLaws)


class ProblemCandidate(BaseModel):
    source_document_id: str
    source_chunk_id: str
    source_page: int
    raw_text: str
    detected_kind: Literal["worked_example", "exercise"]
    confidence: float


class ExtractedProblem(BaseModel):
    source_document_id: str
    source_chunk_id: str
    source_page: int
    problem_text: str
    given_values: dict[str, float]
    target_unknown: str
    reference_solution: list[ReferenceStep] = Field(min_length=1)
    concept_id: str
    subject_id: str
    difficulty: Difficulty


class ValidatedProblem(ExtractedProblem):
    problem_id: str  # sha256(source_document_id + source_chunk_id)[:16]


class RejectedProblem(BaseModel):
    extracted: ExtractedProblem
    gate_failed: str
    gate_diagnostic: str
