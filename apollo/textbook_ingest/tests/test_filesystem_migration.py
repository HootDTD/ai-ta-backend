# apollo/textbook_ingest/tests/test_filesystem_migration.py
import pytest

from apollo.overseer.problem_selector import list_problems_for_cluster
from apollo.subjects import load_concept
from apollo.textbook_ingest.scripts.migrate_filesystem_concept import migrate_bernoulli

_STUB_EMBED = lambda text: [0.0] * 3072


@pytest.mark.asyncio
async def test_migration_loads_five_problems_and_concept(neo4j_test):
    summary = await migrate_bernoulli(neo4j_test, embed=_STUB_EMBED)
    assert summary["problems_written"] == 5
    problems = await list_problems_for_cluster("fluid_mechanics", neo4j_test)
    assert len(problems) == 5
    ids = {p.id for p in problems}
    assert "bernoulli_horizontal_pipe_find_p2" in ids
    cdef = await load_concept("fluid_mechanics", "bernoulli_principle", neo4j_test)
    assert cdef.canonical_symbols.symbols == ["P", "rho", "v", "A", "h", "g", "Q"]


@pytest.mark.asyncio
async def test_migration_is_idempotent(neo4j_test):
    await migrate_bernoulli(neo4j_test, embed=_STUB_EMBED)
    second = await migrate_bernoulli(neo4j_test, embed=_STUB_EMBED)
    assert second["problems_written"] == 0
    problems = await list_problems_for_cluster("fluid_mechanics", neo4j_test)
    assert len(problems) == 5
