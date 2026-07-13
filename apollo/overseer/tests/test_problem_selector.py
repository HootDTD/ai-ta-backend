"""Real-PG tests for the DB-backed problem selector (WU-3D Task 2).

`problem_selector` no longer reads the filesystem or maps a legacy cluster_id.
It loads ``apollo_concept_problems`` rows by ``concept_id`` (+difficulty),
validates each row's ``payload`` through ``Problem.model_validate``, and is
deterministic (sorted by ``Problem.id``). The §6 grading core reads
``reference_solution`` from the DB payload (criterion #7).
"""

from __future__ import annotations

import logging

import pytest

from apollo.errors import PoolExhaustedError
from apollo.overseer.problem_selector import (
    list_problems_for_concept,
    select_problem,
    select_problem_personalized,
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
    intro_payloads = [
        p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"
    ]
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


# ---------------------------------------------------------------------------
# WU-3B2a — Tier-2 selection gate (only teachable problems are returned).
# ---------------------------------------------------------------------------


async def test_list_problems_excludes_tier1(db_session):
    """Two problems under one concept, one Tier-1 (auto-provisioned inventory),
    one Tier-2 (teachable): the call returns ONLY the Tier-2 problem.

    MUTATION-PROOF: reverting the ``tier == 2`` WHERE clause in
    ``list_problems_for_concept`` makes the Tier-1 row leak -> this test REDs.
    """
    payloads = load_bernoulli_problem_payloads()
    tier1_payload, tier2_payload = payloads[0], payloads[1]
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s_tier", concept_slug="c_tier"
    )
    # One inventory (Tier-1) row and one teachable (Tier-2) row under ONE concept.
    await seed_problems(db_session, concept_id=cid, payloads=[tier1_payload], tier=1)
    await seed_problems(db_session, concept_id=cid, payloads=[tier2_payload], tier=2)

    problems = await list_problems_for_concept(db_session, concept_id=cid)
    returned_ids = {p.id for p in problems}
    assert tier2_payload["id"] in returned_ids
    assert tier1_payload["id"] not in returned_ids, "Tier-1 inventory must NOT leak"


async def test_list_problems_includes_tier2(db_session):
    """A Tier-2 problem is returned (positive control; proves the filter does not
    over-exclude). With #28, reverting the WHERE either leaks Tier-1 (#28 RED) or,
    if mis-written as ``tier == 1``, drops Tier-2 (#29 RED)."""
    payload = load_bernoulli_problem_payloads()[0]
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s_t2", concept_slug="c_t2"
    )
    await seed_problems(db_session, concept_id=cid, payloads=[payload], tier=2)

    problems = await list_problems_for_concept(db_session, concept_id=cid)
    assert [p.id for p in problems] == [payload["id"]]


async def test_select_problem_personalized_also_tier_gated(db_session):
    """With the personalization flag OFF (default), ``select_problem_personalized``
    delegates to ``select_problem`` -> ``list_problems_for_concept``, so the single
    tier predicate gates the personalized caller too (no separate selector edit).

    Seed one Tier-1 + one Tier-2 intro problem; the personalized selector returns
    the Tier-2 problem.
    """
    intro_payloads = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]
    assert len(intro_payloads) >= 2
    tier1_payload, tier2_payload = intro_payloads[0], intro_payloads[1]
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s_pers", concept_slug="c_pers"
    )
    await seed_problems(db_session, concept_id=cid, payloads=[tier1_payload], tier=1)
    await seed_problems(db_session, concept_id=cid, payloads=[tier2_payload], tier=2)

    chosen = await select_problem_personalized(
        db_session,
        user_id="00000000-0000-4000-8000-000000000001",
        search_space_id=sid,
        concept_id=cid,
        difficulty="intro",
        attempted_ids=[],
    )
    assert chosen.id == tier2_payload["id"]


# ---------------------------------------------------------------------------
# GEN-0 — malformed rows are isolated at the shared selector chokepoint.
# ---------------------------------------------------------------------------


async def test_malformed_problem_is_logged_and_skipped_for_both_selectors(db_session, caplog):
    """One bad Tier-2 payload cannot take down the concept's valid pool."""
    intro_payloads = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"][
        :2
    ]
    assert len(intro_payloads) == 2
    malformed = {
        **intro_payloads[0],
        "id": "malformed.selector.payload",
        "reference_solution": [],
    }
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session,
        search_space_id=sid,
        subject_slug="s_skip",
        concept_slug="c_skip",
    )
    await seed_problems(db_session, concept_id=cid, payloads=[*intro_payloads, malformed])

    with caplog.at_level(logging.WARNING, logger="apollo.overseer.problem_selector"):
        problems = await list_problems_for_concept(db_session, concept_id=cid)
        chosen = await select_problem_personalized(
            db_session,
            user_id="00000000-0000-4000-8000-000000000001",
            search_space_id=sid,
            concept_id=cid,
            difficulty="intro",
            attempted_ids=[],
        )

    assert [problem.id for problem in problems] == sorted(p["id"] for p in intro_payloads)
    assert chosen.id == min(p["id"] for p in intro_payloads)
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "apollo_problem_selector_invalid_payload_skipped"
    ]
    assert len(records) == 2  # direct listing + personalized path share the chokepoint
    assert all(record.concept_id == cid for record in records)
    assert all(record.problem_tier == 2 for record in records)
    assert all(record.concept_problem_id is not None for record in records)
    assert all("validation error" in record.validation_error for record in records)


async def test_all_malformed_problems_yield_pool_exhausted(db_session, caplog):
    """An all-invalid pool degrades to the selector's existing empty-pool error."""
    payload = load_bernoulli_problem_payloads()[0]
    malformed = {**payload, "reference_solution": []}
    _sid, cid, _codes = await seed_course(
        db_session,
        subject_slug="s_all_bad",
        concept_slug="c_all_bad",
        problems=[malformed],
    )

    with caplog.at_level(logging.WARNING, logger="apollo.overseer.problem_selector"):
        with pytest.raises(PoolExhaustedError) as exc_info:
            await select_problem(
                db_session,
                concept_id=cid,
                difficulty=payload["difficulty"],
                attempted_ids=[],
            )

    assert exc_info.value.concept_cluster_id == str(cid)
    assert exc_info.value.difficulty == payload["difficulty"]
    assert any(
        getattr(record, "event", None) == "apollo_problem_selector_invalid_payload_skipped"
        for record in caplog.records
    )
