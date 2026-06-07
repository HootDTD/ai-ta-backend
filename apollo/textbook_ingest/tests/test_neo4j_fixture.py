import pytest


@pytest.mark.asyncio
async def test_neo4j_test_fixture_is_empty_and_writable(neo4j_test):
    async with neo4j_test.session() as s:
        await s.run("CREATE (:_ConceptNode:Concept {concept_id: 'probe'})")
        rec = await (await s.run(
            "MATCH (c:Concept {concept_id: 'probe'}) RETURN count(c) AS n"
        )).single()
        assert rec["n"] == 1
