import pytest
import pytest_asyncio

from apollo.errors import PoolExhaustedError
from apollo.schemas.problem import ReferenceStep
from apollo.subjects import CanonicalSymbols, SolverHints
from apollo.textbook_ingest.types import ConceptRegistryEntry, ValidatedProblem
from apollo.textbook_ingest import writer
from apollo.overseer.problem_selector import (
    cluster_to_concept, list_problems_for_cluster, select_problem,
)


async def _seed(neo, *, pid, authored, difficulty="intro"):
    await writer.write_problem(neo, ValidatedProblem(
        source_document_id="seed", source_chunk_id=pid, source_page=1,
        problem_text="t", given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[ReferenceStep(step=1, entry_type="equation", id="e1",
            content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]},
            depends_on=[])],
        concept_id="bernoulli_principle", subject_id="fluid_mechanics",
        difficulty=difficulty, problem_id=pid), authored=authored)


@pytest_asyncio.fixture
async def seeded(neo4j_test):
    await writer.write_concept(neo4j_test, ConceptRegistryEntry(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        scope_summary="b", canonical_symbols=CanonicalSymbols(symbols=["P"]),
        normalization_map={}, parser_prompt_template="P",
        solver_hints=SolverHints()), source_document_id="seed",
        scope_embedding=[0.0] * 3072, policy_frozen=True)
    await writer.write_cluster_alias(neo4j_test, "fluid_mechanics",
                                     "fluid_mechanics", "bernoulli_principle")
    await _seed(neo4j_test, pid="zzz_extracted", authored=False)
    await _seed(neo4j_test, pid="bernoulli_authored", authored=True)
    return neo4j_test


@pytest.mark.asyncio
async def test_cluster_to_concept_reads_alias(seeded):
    assert await cluster_to_concept("fluid_mechanics", seeded) == \
        ("fluid_mechanics", "bernoulli_principle")


@pytest.mark.asyncio
async def test_authored_problem_sorts_first(seeded):
    p = await select_problem(cluster_id="fluid_mechanics", difficulty="intro",
                             attempted_ids=[], neo=seeded)
    assert p.id == "bernoulli_authored"


@pytest.mark.asyncio
async def test_excludes_attempted(seeded):
    p = await select_problem(cluster_id="fluid_mechanics", difficulty="intro",
                             attempted_ids=["bernoulli_authored"], neo=seeded)
    assert p.id == "zzz_extracted"


@pytest.mark.asyncio
async def test_pool_exhausted_raises(seeded):
    with pytest.raises(PoolExhaustedError):
        await select_problem(cluster_id="fluid_mechanics", difficulty="hard",
                             attempted_ids=[], neo=seeded)


@pytest.mark.asyncio
async def test_loaded_problem_reconstructs_reference_solution(seeded):
    p = await select_problem(cluster_id="fluid_mechanics", difficulty="intro",
                             attempted_ids=[], neo=seeded)
    assert p.reference_solution[0].content["symbolic"] == "P1 - P2"
    assert p.given_values == {"P1": 1.0}
