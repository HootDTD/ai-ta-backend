"""One-time, idempotent migration of the hand-authored Bernoulli concept from
the on-disk JSON folder into Neo4j. Safe to re-run (writer skips existing nodes).

Run manually:  python -m apollo.textbook_ingest.scripts.migrate_filesystem_concept
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable

from config import settings
from indexing.document_embedder import embed_text
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.schemas.problem import load_problem
from apollo.subjects import CanonicalSymbols, ForbiddenNamedLaws, SolverHints
from apollo.textbook_ingest import writer
from apollo.textbook_ingest.types import ConceptRegistryEntry, ExtractedProblem, ValidatedProblem
from apollo.textbook_ingest.validator import validate_problem
from apollo.textbook_ingest.concept_schema_map import rows_to_concept_definition, entry_to_rows

_ROOT = Path(__file__).resolve().parents[3] / "apollo/subjects/fluid_mechanics/concepts/bernoulli_principle"
_SUBJECT = "fluid_mechanics"
_CONCEPT = "bernoulli_principle"
_CLUSTER = "fluid_mechanics"


def _load_entry() -> ConceptRegistryEntry:
    cs = json.loads((_ROOT / "canonical_symbols.json").read_text())
    nm = json.loads((_ROOT / "normalization_map.json").read_text())
    sh = json.loads((_ROOT / "solver_hints.json").read_text())
    fb = json.loads((_ROOT / "forbidden_named_laws.json").read_text())
    template = (_ROOT / "parser_prompt_template.md").read_text()
    return ConceptRegistryEntry(
        subject_id=_SUBJECT, concept_id=_CONCEPT,
        scope_summary="Bernoulli's principle for incompressible steady flow.",
        canonical_symbols=CanonicalSymbols(**cs),
        normalization_map=nm, parser_prompt_template=template,
        solver_hints=SolverHints(**sh), forbidden_named_laws=ForbiddenNamedLaws(**fb))


async def migrate_bernoulli(neo: Neo4jClient, *, embed: Callable[[str], list[float]] | None = None) -> dict:
    if embed is None:
        embed = lambda t: embed_text(t, model=settings.TEXTBOOK_EMBEDDING_MODEL,
                                     dim=settings.TEXTBOOK_EMBEDDING_DIM)
    entry = _load_entry()
    embedding = embed(entry.scope_summary)
    await writer.write_concept(neo, entry, source_document_id="filesystem_migration",
                               scope_embedding=embedding, policy_frozen=True)
    await writer.write_cluster_alias(neo, _CLUSTER, _SUBJECT, _CONCEPT)

    concept_def = rows_to_concept_definition(entry_to_rows(entry), problems_dir=None)
    written = 0
    for path in sorted((_ROOT / "problems").glob("problem_*.json")):
        prob = load_problem(path)  # apollo.schemas.problem.Problem
        # concept_id is intentionally overridden to _CONCEPT. Some on-disk problems
        # (e.g. problem_03/04) carry their own "natural" concept_id (continuity_equation,
        # volumetric_flow_rate) for authoring context, but every problem in the
        # bernoulli_principle folder is served under this concept — matching the legacy
        # filesystem selector, which loaded the whole folder under the fluid_mechanics cluster.
        extracted = ExtractedProblem(
            source_document_id="filesystem_migration", source_chunk_id=prob.id,
            source_page=0, problem_text=prob.problem_text, given_values=prob.given_values,
            target_unknown=prob.target_unknown, reference_solution=prob.reference_solution,
            concept_id=_CONCEPT, subject_id=_SUBJECT, difficulty=prob.difficulty)
        res = validate_problem(extracted, concept_def)
        if not res.ok:
            raise RuntimeError(
                f"Hand-authored problem {prob.id!r} failed gate {res.gate_failed!r}: "
                f"{res.diagnostic}. Validator is too strict - investigate, do not relax content.")
        validated = ValidatedProblem(**extracted.model_dump(), problem_id=prob.id)
        existed = await writer.problem_exists(neo, validated.problem_id)
        await writer.write_problem(neo, validated, authored=True)
        if not existed:
            written += 1
    return {"problems_written": written}


if __name__ == "__main__":
    asyncio.run(migrate_bernoulli(Neo4jClient.from_env()))
