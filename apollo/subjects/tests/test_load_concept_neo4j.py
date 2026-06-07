import pytest

from apollo.subjects import CanonicalSymbols, SolverHints, ForbiddenNamedLaws, load_concept
from apollo.textbook_ingest.types import ConceptRegistryEntry
from apollo.textbook_ingest import writer


@pytest.mark.asyncio
async def test_load_concept_reconstructs_definition(neo4j_test):
    await writer.write_concept(neo4j_test, ConceptRegistryEntry(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        scope_summary="b",
        canonical_symbols=CanonicalSymbols(symbols=["P", "rho"],
            description={"P": "pressure", "rho": "density"}, subscript_convention="conv"),
        normalization_map={"pressure": "P"}, parser_prompt_template="TEMPLATE",
        solver_hints=SolverHints(constants={"g": 9.81}, augmented_givens={"g": 9.81},
                                 non_trivial_keywords=["pressure"], plan_markers=["first"]),
        forbidden_named_laws=ForbiddenNamedLaws(named_laws=["bernoulli"])),
        source_document_id="seed", scope_embedding=[0.0] * 3072, policy_frozen=True)

    cdef = await load_concept("fluid_mechanics", "bernoulli_principle", neo4j_test)
    assert cdef.canonical_symbols.symbols == ["P", "rho"]
    assert cdef.parser_prompt_template == "TEMPLATE"
    assert cdef.solver_hints.augmented_givens == {"g": 9.81}
    assert cdef.solver_hints.non_trivial_keywords == ["pressure"]
    assert "bernoulli" in cdef.forbidden_named_laws.named_laws


@pytest.mark.asyncio
async def test_missing_concept_raises(neo4j_test):
    from apollo.subjects import ConceptNotFoundError
    with pytest.raises(ConceptNotFoundError):
        await load_concept("nope", "nope", neo4j_test)
