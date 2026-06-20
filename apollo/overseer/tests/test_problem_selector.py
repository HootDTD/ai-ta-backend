"""Real-PG tests for the DB-backed problem selector (WU-3D Task 2).

`problem_selector` no longer reads the filesystem or maps a legacy cluster_id.
It loads ``apollo_concept_problems`` rows by ``concept_id`` (+difficulty),
validates each row's ``payload`` through ``Problem.model_validate``, and is
deterministic (sorted by ``Problem.id``). The §6 grading core reads
``reference_solution`` from the DB payload (criterion #7).
"""

from __future__ import annotations

import pytest

from apollo.errors import PoolExhaustedError
from apollo.overseer.problem_selector import (
    list_problems_for_concept,
    select_problem,
)
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_problem_payloads,
    seed_concept,
    seed_course,
    seed_problems,
    seed_search_space,
)

pytestmark = pytest.mark.integration


async def test_list_problems_for_concept_returns_db_problems(db_session):
    """T2.1 — 3 seeded problems come back as Problems with .id == payload['id'],
    sorted ascending."""
    payloads = load_bernoulli_problem_payloads()[:3]
    _sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=payloads,
    )

    problems = await list_problems_for_concept(db_session, concept_id=cid)
    assert len(problems) == 3
    ids = [p.id for p in problems]
    assert ids == sorted(ids)
    assert set(ids) == set(codes)


async def test_list_problems_for_concept_scoped_to_concept(db_session):
    """T2.2 — two concepts each with a problem; the call for concept A returns
    only A's problem (no cross-concept bleed)."""
    payloads = load_bernoulli_problem_payloads()
    sid = await seed_search_space(db_session)
    cid_a = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s_a", concept_slug="c_a"
    )
    cid_b = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s_b", concept_slug="c_b"
    )
    await seed_problems(db_session, concept_id=cid_a, payloads=[payloads[0]])
    await seed_problems(db_session, concept_id=cid_b, payloads=[payloads[1]])

    a_problems = await list_problems_for_concept(db_session, concept_id=cid_a)
    assert [p.id for p in a_problems] == [payloads[0]["id"]]
    assert payloads[1]["id"] not in {p.id for p in a_problems}


async def test_select_problem_intro_excludes_attempted(db_session):
    """T2.3 — ≥2 intro problems; selecting with the first attempted returns a
    different problem."""
    intro_payloads = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]
    assert len(intro_payloads) >= 2
    _sid, cid, _codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=intro_payloads,
    )

    first = await select_problem(db_session, concept_id=cid, difficulty="intro", attempted_ids=[])
    second = await select_problem(
        db_session, concept_id=cid, difficulty="intro", attempted_ids=[first.id]
    )
    assert second.id != first.id


async def test_select_problem_raises_pool_exhausted(db_session):
    """T2.4 — attempting all intro ids raises PoolExhaustedError carrying
    .difficulty='intro' and .concept_cluster_id == str(concept_id)."""
    intro_payloads = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]
    _sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=intro_payloads,
    )

    with pytest.raises(PoolExhaustedError) as exc_info:
        await select_problem(db_session, concept_id=cid, difficulty="intro", attempted_ids=codes)
    assert exc_info.value.difficulty == "intro"
    assert exc_info.value.concept_cluster_id == str(cid)


async def test_db_problem_payload_carries_reference_solution(db_session):
    """T2.5 (criterion #7) — a problem reloaded from the DB carries a non-empty
    reference_solution AND yields a non-empty reference KG graph (the §6 grading
    core reads this from the DB, not disk)."""
    payloads = load_bernoulli_problem_payloads()[:1]
    _sid, cid, _codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=payloads,
    )

    problems = await list_problems_for_concept(db_session, concept_id=cid)
    problem = problems[0]
    assert problem.reference_solution  # non-empty
    graph = problem.to_kg_graph(attempt_id=-1)
    assert graph.nodes  # non-empty node set
