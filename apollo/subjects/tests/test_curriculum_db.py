"""Real-PG behavioral tests for the DB-backed curriculum loader (WU-3D Task 1).

`apollo/subjects/curriculum_db.py` replaces the filesystem registry reads on the
SELECTION path: concepts and their pydantic ``ConceptDefinition`` come from the
``apollo_concepts`` rows, scoped to a course via ``apollo_subjects.search_space_id``.
These run on real Postgres (the JSONB scoping JOIN must run on PG, not SQLite).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apollo.persistence.models import Concept
from apollo.subjects import ConceptDefinition
from apollo.subjects.curriculum_db import (
    ConceptNotFoundError,
    ConceptRow,
    RegisteredConcept,
    list_course_concepts,
    list_registered_concepts,
    load_concept_definition,
)
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_concept_payloads,
    minimal_problem_payload,
    seed_concept,
    seed_course,
    seed_problems,
    seed_search_space,
)

pytestmark = pytest.mark.integration


async def test_list_course_concepts_returns_only_that_courses_concepts(db_session):
    """T1.1 — same concept slug in two courses; the JOIN-on-search_space_id scoping
    returns ONLY the queried course's concept_id.

    Reconciled for the teachable-pool filter: each concept is now seeded with one
    tier-2 problem so it stays a candidate; the assertion still pins the SCOPING
    behavior (course A's concept only), not pool contents."""
    sid_a, cid_a, _ = await seed_course(
        db_session,
        subject_slug="physics_a",
        concept_slug="bernoulli",
        problems=[minimal_problem_payload()],
    )
    sid_b, cid_b, _ = await seed_course(
        db_session,
        subject_slug="physics_b",
        concept_slug="bernoulli",
        problems=[minimal_problem_payload()],
    )

    rows_a = await list_course_concepts(db_session, search_space_id=sid_a)
    assert [r.concept_id for r in rows_a] == [cid_a]
    assert cid_b not in {r.concept_id for r in rows_a}
    assert isinstance(rows_a[0], ConceptRow)
    assert rows_a[0].slug == "bernoulli"
    assert rows_a[0].display_name == "Bernoulli"


async def test_list_course_concepts_empty_when_no_curriculum(db_session):
    """T1.2 — a course with a search-space row but no subjects -> []."""
    sid = await seed_search_space(db_session)
    assert await list_course_concepts(db_session, search_space_id=sid) == []


async def test_list_course_concepts_ordered_by_id(db_session):
    """T1.3 — two concepts in one course are returned ascending by concept_id.

    Reconciled for the teachable-pool filter: each concept now gets one tier-2
    problem so both remain candidates; the assertion still pins the ORDERING
    (ascending by concept_id), which is the behavior under test."""
    sid = await seed_search_space(db_session)
    cid_1 = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s1", concept_slug="c_alpha"
    )
    await seed_problems(db_session, concept_id=cid_1, payloads=[minimal_problem_payload("a1")])
    cid_2 = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s2", concept_slug="c_beta"
    )
    await seed_problems(db_session, concept_id=cid_2, payloads=[minimal_problem_payload("b1")])

    rows = await list_course_concepts(db_session, search_space_id=sid)
    ids = [r.concept_id for r in rows]
    assert ids == sorted(ids)
    assert ids == [cid_1, cid_2]


# ---------------------------------------------------------------------------
# Teachable-pool filter (G6 fix). list_course_concepts must return ONLY concepts
# that have at least one TEACHABLE problem, using the EXACT predicate the
# downstream pool query (overseer.problem_selector.list_problems_for_concept)
# applies: ProblemRecord.tier == 2 AND quarantined_at IS NULL. Otherwise an
# autoprovisioned decoy concept with an empty pool can be inferred and then blow
# up select_problem with PoolExhaustedError (the fluids-grading 409).
# ---------------------------------------------------------------------------


async def test_list_course_concepts_includes_concept_with_teachable_problem(db_session):
    """T1.7 (a) — a concept with a tier-2, non-quarantined problem IS returned.
    Positive control: green both before and after the filter lands."""
    sid, cid, _ = await seed_course(
        db_session,
        subject_slug="s1",
        concept_slug="teachable",
        problems=[minimal_problem_payload()],
    )

    rows = await list_course_concepts(db_session, search_space_id=sid)
    assert [r.concept_id for r in rows] == [cid]


async def test_list_course_concepts_excludes_concept_with_no_problems(db_session):
    """T1.8 (b) — a concept with ZERO problems is EXCLUDED (the G6 decoy shape: an
    autoprovisioned concept with an empty pool must never be a candidate)."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s1", concept_slug="empty_decoy"
    )

    rows = await list_course_concepts(db_session, search_space_id=sid)
    assert cid not in {r.concept_id for r in rows}
    assert rows == []


async def test_list_course_concepts_excludes_concept_with_only_tier1_problems(db_session):
    """T1.9 (c) — a concept whose only problems are Tier-1 inventory is EXCLUDED
    (Tier-1 is not teachable; mirrors list_problems_for_concept's tier == 2 gate)."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s1", concept_slug="inventory_only"
    )
    await seed_problems(db_session, concept_id=cid, payloads=[minimal_problem_payload()], tier=1)

    rows = await list_course_concepts(db_session, search_space_id=sid)
    assert cid not in {r.concept_id for r in rows}
    assert rows == []


async def test_list_course_concepts_excludes_concept_with_only_quarantined_problem(db_session):
    """T1.10 (d) — a concept whose only tier-2 problem is quarantined
    (quarantined_at set) is EXCLUDED (mirrors the quarantined_at IS NULL gate)."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s1", concept_slug="quarantined_only"
    )
    await seed_problems(
        db_session,
        concept_id=cid,
        payloads=[minimal_problem_payload()],
        tier=2,
        quarantined_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    rows = await list_course_concepts(db_session, search_space_id=sid)
    assert cid not in {r.concept_id for r in rows}
    assert rows == []


async def test_list_course_concepts_includes_concept_with_mixed_tier_problems(db_session):
    """T1.11 — a concept with BOTH a tier-1 inventory row and a live tier-2 problem
    IS returned: the EXISTS finds the teachable row even amid non-teachable rows
    (proves the filter is 'has a teachable problem', not merely 'has any row')."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s1", concept_slug="mixed"
    )
    await seed_problems(
        db_session, concept_id=cid, payloads=[minimal_problem_payload("t1")], tier=1
    )
    await seed_problems(
        db_session, concept_id=cid, payloads=[minimal_problem_payload("t2")], tier=2
    )

    rows = await list_course_concepts(db_session, search_space_id=sid)
    assert [r.concept_id for r in rows] == [cid]


async def test_list_course_concepts_excludes_unteachable_decoy_beside_real(db_session):
    """T1.12 (G6 regression) — in ONE course, a real concept (teachable pool) and a
    decoy concept (zero problems) coexist; only the real concept is a candidate, so
    infer_concept_id can never pick the empty decoy and blow up select_problem."""
    sid = await seed_search_space(db_session)
    real_cid = await seed_concept(
        db_session,
        search_space_id=sid,
        subject_slug="fluids_real",
        concept_slug="bernoulli_principle",
    )
    await seed_problems(
        db_session, concept_id=real_cid, payloads=[minimal_problem_payload("real1")]
    )
    decoy_cid = await seed_concept(
        db_session,
        search_space_id=sid,
        subject_slug="fluids_decoy",
        concept_slug="bernoulli-equation",
    )

    rows = await list_course_concepts(db_session, search_space_id=sid)
    ids = {r.concept_id for r in rows}
    assert real_cid in ids
    assert decoy_cid not in ids
    assert [r.concept_id for r in rows] == [real_cid]


async def test_load_concept_definition_builds_from_db_columns(db_session):
    """T1.4 — a ConceptDefinition round-trips the seeded JSONB/TEXT columns."""
    payloads = load_bernoulli_concept_payloads()
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session,
        search_space_id=sid,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        concept_payloads=payloads,
    )

    cd = await load_concept_definition(db_session, concept_id=cid, search_space_id=sid)
    assert isinstance(cd, ConceptDefinition)
    assert cd.canonical_symbols.symbols == payloads["canonical_symbols"]["symbols"]
    assert cd.parser_prompt_template == payloads["parser_prompt_template"]
    assert cd.normalization_map == payloads["normalization_map"]
    assert cd.solver_hints.constants["g"] == 9.81
    assert cd.forbidden_named_laws.all_terms()  # non-empty when seeded
    assert "bernoulli" in cd.forbidden_named_laws.all_terms()


async def test_load_concept_definition_raises_on_missing_concept(db_session):
    """T1.5 — a non-existent concept_id raises ConceptNotFoundError."""
    with pytest.raises(ConceptNotFoundError):
        await load_concept_definition(
            db_session, concept_id=999999, search_space_id=999999
        )


async def test_load_concept_definition_problems_dir_is_sentinel_not_globbed(db_session):
    """T1.6 — problems_dir is a sentinel non-existent path: the runtime never
    globs the filesystem for problems (criterion #2)."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s1", concept_slug="c1")
    cd = await load_concept_definition(db_session, concept_id=cid, search_space_id=sid)
    assert cd.problems_dir.exists() is False


async def test_list_registered_concepts_includes_problemless_and_description(db_session):
    """Reversed provisioning — the matcher's closed list is EVERY registered
    concept (a fresh premade-list course has zero problems yet), with the
    reserved provisional-inventory concept excluded and description carried."""
    sid = await seed_search_space(db_session)
    cid_teachable = await seed_concept(
        db_session, search_space_id=sid, subject_slug="calc2", concept_slug="u_substitution"
    )
    await seed_problems(db_session, concept_id=cid_teachable, payloads=[minimal_problem_payload()])
    # a problemless registered concept — must STILL appear (no tier-2 EXISTS)
    db_session.add(
        Concept(
            course_id=sid,
            subject_slug="calc2",
            subject_display_name="Calculus II",
            slug="integration-by-parts",
            display_name="Integration by Parts",
            description="u dv = uv - v du",
        )
    )
    db_session.add(
        Concept(
            course_id=sid,
            subject_slug="calc2",
            subject_display_name="Calculus II",
            slug="provisional.inventory",
            display_name="Provisional Inventory",
        )
    )
    await db_session.flush()

    rows = await list_registered_concepts(db_session, search_space_id=sid)
    slugs = {r.slug for r in rows}
    assert "integration-by-parts" in slugs  # problemless included
    assert "u_substitution" in slugs
    assert "provisional.inventory" not in slugs  # reserved concept excluded
    ibp = next(r for r in rows if r.slug == "integration-by-parts")
    assert ibp.description == "u dv = uv - v du"
    assert isinstance(ibp, RegisteredConcept)


async def test_list_registered_concepts_scoped_to_course(db_session):
    sid_a = await seed_search_space(db_session)
    sid_b = await seed_search_space(db_session)
    await seed_concept(
        db_session, search_space_id=sid_a, subject_slug="ca", concept_slug="only_in_a"
    )
    await seed_concept(
        db_session, search_space_id=sid_b, subject_slug="cb", concept_slug="only_in_b"
    )
    rows = await list_registered_concepts(db_session, search_space_id=sid_a)
    assert {r.slug for r in rows} == {"only_in_a"}
