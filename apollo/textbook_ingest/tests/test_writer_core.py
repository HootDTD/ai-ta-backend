# apollo/textbook_ingest/tests/test_writer_core.py
import pytest

from apollo.schemas.problem import ReferenceStep
from apollo.subjects import CanonicalSymbols, ForbiddenNamedLaws, SolverHints
from apollo.textbook_ingest.types import ConceptRegistryEntry, ValidatedProblem
from apollo.textbook_ingest import writer


def _entry():
    return ConceptRegistryEntry(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        scope_summary="Bernoulli.", canonical_symbols=CanonicalSymbols(symbols=["P"]),
        normalization_map={"pressure": "P"}, parser_prompt_template="P",
        solver_hints=SolverHints(constants={"g": 9.81}),
        forbidden_named_laws=ForbiddenNamedLaws(named_laws=["bernoulli"]),
    )


def _problem(pid="p1"):
    return ValidatedProblem(
        source_document_id="seed", source_chunk_id="c1", source_page=1,
        problem_text="t", given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[ReferenceStep(step=1, entry_type="equation", id="e1",
            content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]},
            depends_on=[])],
        concept_id="bernoulli_principle", subject_id="fluid_mechanics",
        difficulty="intro", problem_id=pid)


@pytest.mark.asyncio
async def test_write_concept_then_problem_and_alias(neo4j_test):
    await writer.write_concept(neo4j_test, _entry(), source_document_id="seed",
                               scope_embedding=[0.0] * 3072, policy_frozen=True)
    assert await writer.concept_exists(neo4j_test, "fluid_mechanics", "bernoulli_principle")
    await writer.write_cluster_alias(neo4j_test, "fluid_mechanics",
                                     "fluid_mechanics", "bernoulli_principle")
    await writer.write_problem(neo4j_test, _problem(), authored=True)
    assert await writer.problem_exists(neo4j_test, "p1")

    async with neo4j_test.session() as s:
        rec = await (await s.run(
            "MATCH (p:Problem {problem_id:'p1'})-[:HAS_REFERENCE_NODE]->(n:_ProblemNode) "
            "RETURN count(n) AS n")).single()
        assert rec["n"] == 1


@pytest.mark.asyncio
async def test_write_concept_is_idempotent_and_frozen(neo4j_test):
    await writer.write_concept(neo4j_test, _entry(), source_document_id="seed",
                               scope_embedding=[0.0] * 3072, policy_frozen=True)
    await writer.write_concept(neo4j_test, _entry(), source_document_id="other",
                               scope_embedding=[0.0] * 3072, policy_frozen=True)
    async with neo4j_test.session() as s:
        rec = await (await s.run(
            "MATCH (c:Concept {concept_id:'bernoulli_principle'})-[:HAS_SYMBOL]->(x) "
            "RETURN count(x) AS n")).single()
        assert rec["n"] == 1
