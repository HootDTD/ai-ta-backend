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


@pytest.mark.asyncio
async def test_migration_round_trip_fidelity(neo4j_test):
    import glob
    import json as _json
    from apollo.overseer.problem_selector import _load_problem

    await migrate_bernoulli(neo4j_test, embed=_STUB_EMBED)
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parents[2] / "subjects/fluid_mechanics/concepts/bernoulli_principle/problems"
    paths = sorted(str(p) for p in root.glob("problem_*.json"))
    assert len(paths) == 5
    for path in paths:
        with open(path) as _f:
            disk = _json.load(_f)
        loaded = await _load_problem(disk["id"], neo4j_test)
        assert loaded.id == disk["id"]
        assert loaded.target_unknown == disk["target_unknown"]
        assert loaded.given_values == disk["given_values"]
        loaded_by_id = {s.id: s for s in loaded.reference_solution}
        assert set(loaded_by_id) == {s["id"] for s in disk["reference_solution"]}, path
        for ds in disk["reference_solution"]:
            ls = loaded_by_id[ds["id"]]
            assert ls.step == ds["step"], f"{path}:{ds['id']} step"
            assert ls.entry_type == ds["entry_type"], f"{path}:{ds['id']} entry_type"
            assert set(ls.depends_on) == set(ds.get("depends_on", [])), f"{path}:{ds['id']} depends_on"
            lc, dc = dict(ls.content), dict(ds["content"])
            # uses_equations is reconstructed from USES edges (orderless) -> compare as sets
            assert sorted(lc.pop("uses_equations", [])) == sorted(dc.pop("uses_equations", [])), \
                f"{path}:{ds['id']} uses_equations"
            assert lc == dc, f"{path}:{ds['id']} content mismatch: {lc} != {dc}"
