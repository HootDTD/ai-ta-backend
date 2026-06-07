"""Apollo subject + concept registry (Neo4j-backed).

Concept policy data lives in the Neo4j :Concept subgraph (symbols,
normalization, solver constants, forbidden terms) written by
`apollo.textbook_ingest.writer.write_concept`.

`await load_concept(subject_id, concept_id, neo)` rebuilds a
`ConceptDefinition` from that subgraph via
`apollo.textbook_ingest.concept_schema_map.rows_to_concept_definition`.
All hardcoded fluid-mechanics knowledge in the parser / solver / done
handler should be replaced with reads from this registry.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class ConceptNotFoundError(LookupError):
    """Raised when a subject/concept pair has no registry entry."""


class CanonicalSymbols(BaseModel):
    symbols: list[str]
    description: dict[str, str] = Field(default_factory=dict)
    subscript_convention: str | None = None


class SolverHints(BaseModel):
    constants: dict[str, float] = Field(default_factory=dict)
    augmented_givens: dict[str, float] = Field(default_factory=dict)
    non_trivial_keywords: list[str] = Field(default_factory=list)
    plan_markers: list[str] = Field(default_factory=list)


class ForbiddenNamedLaws(BaseModel):
    """Concept-scoped pre-filter list for the deterministic stage of the
    output filter (item #3). The LLM-judge stage handles paraphrases.

    All four lists are unioned at filter time; the structure exists for
    authoring clarity, not for differential semantics.
    """

    named_laws: list[str] = Field(default_factory=list)
    forbidden_concepts: list[str] = Field(default_factory=list)
    forbidden_domains: list[str] = Field(default_factory=list)
    forbidden_units: list[str] = Field(default_factory=list)

    def all_terms(self) -> list[str]:
        return [
            t.lower()
            for t in (
                *self.named_laws,
                *self.forbidden_concepts,
                *self.forbidden_domains,
                *self.forbidden_units,
            )
        ]


class ConceptDefinition(BaseModel):
    """All on-disk registry data for a (subject, concept)."""

    subject_id: str
    concept_id: str
    canonical_symbols: CanonicalSymbols
    normalization_map: dict[str, str]
    parser_prompt_template: str
    solver_hints: SolverHints
    forbidden_named_laws: ForbiddenNamedLaws = Field(
        default_factory=ForbiddenNamedLaws,
    )
    problems_dir: Path

    model_config = {"arbitrary_types_allowed": True}


async def load_concept(subject_id: str, concept_id: str, neo) -> ConceptDefinition:
    """Reconstruct a ConceptDefinition from the Neo4j concept subgraph."""
    from apollo.textbook_ingest.concept_schema_map import rows_to_concept_definition

    async with neo.session() as s:
        crec = await (await s.run(
            "MATCH (c:Concept {subject_id:$s, concept_id:$c}) "
            "RETURN c.subject_id AS subject_id, c.concept_id AS concept_id, "
            "c.scope_summary AS scope_summary, c.parser_prompt_template AS parser_prompt_template, "
            "c.non_trivial_keywords AS non_trivial_keywords, c.plan_markers AS plan_markers",
            s=subject_id, c=concept_id)).single()
        if crec is None:
            raise ConceptNotFoundError(f"{subject_id}/{concept_id}")
        symbols = await (await s.run(
            "MATCH (:Concept {subject_id:$s, concept_id:$c})-[:HAS_SYMBOL]->(x) "
            "RETURN x.symbol AS symbol, x.description AS description, "
            "x.subscript_convention AS subscript_convention "
            "ORDER BY x.ord", s=subject_id, c=concept_id)).data()
        normalization = await (await s.run(
            "MATCH (:Concept {subject_id:$s, concept_id:$c})-[:HAS_NORMALIZATION]->(x) "
            "RETURN x.natural_language AS natural_language, x.canonical_symbol AS canonical_symbol",
            s=subject_id, c=concept_id)).data()
        constants = await (await s.run(
            "MATCH (:Concept {subject_id:$s, concept_id:$c})-[:HAS_CONSTANT]->(x) "
            "RETURN x.name AS name, x.value AS value, x.kind AS kind", s=subject_id, c=concept_id)).data()
        forbidden = await (await s.run(
            "MATCH (:Concept {subject_id:$s, concept_id:$c})-[:FORBIDS]->(x) "
            "RETURN x.term AS term, x.category AS category", s=subject_id, c=concept_id)).data()
    rows = {"concept": dict(crec), "symbols": symbols, "normalization": normalization,
            "solver_constants": constants, "forbidden_terms": forbidden}
    return rows_to_concept_definition(rows, problems_dir=None)


async def list_subjects(neo) -> list[str]:
    async with neo.session() as s:
        rows = await (await s.run(
            "MATCH (c:Concept) RETURN DISTINCT c.subject_id AS s ORDER BY s")).data()
    return [r["s"] for r in rows]


async def list_concepts(subject_id: str, neo) -> list[str]:
    async with neo.session() as s:
        rows = await (await s.run(
            "MATCH (c:Concept {subject_id:$s}) RETURN c.concept_id AS c ORDER BY c",
            s=subject_id)).data()
    return [r["c"] for r in rows]


__all__ = [
    "ConceptDefinition",
    "ConceptNotFoundError",
    "CanonicalSymbols",
    "SolverHints",
    "ForbiddenNamedLaws",
    "list_subjects",
    "list_concepts",
    "load_concept",
]
