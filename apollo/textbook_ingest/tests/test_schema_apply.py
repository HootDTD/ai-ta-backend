# apollo/textbook_ingest/tests/test_schema_apply.py
import pytest


@pytest.mark.asyncio
async def test_concept_and_problem_constraints_exist(neo4j_test):
    async with neo4j_test.session() as s:
        rows = await (await s.run("SHOW CONSTRAINTS YIELD name RETURN name")).data()
    names = {r["name"] for r in rows}
    assert "concept_id_unique" in names
    assert "problem_id_unique" in names
    assert "cluster_alias_unique" in names


@pytest.mark.asyncio
async def test_vector_index_is_3072(neo4j_test):
    async with neo4j_test.session() as s:
        rows = await (await s.run(
            "SHOW INDEXES YIELD name, options RETURN name, options"
        )).data()
    idx = {r["name"]: r["options"] for r in rows}
    assert "concept_scope_embedding_idx" in idx
    cfg = idx["concept_scope_embedding_idx"]["indexConfig"]
    assert cfg["vector.dimensions"] == 3072
