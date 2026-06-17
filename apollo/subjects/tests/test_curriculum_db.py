"""Real-PG behavioral tests for the DB-backed curriculum loader (WU-3D Task 1).

`apollo/subjects/curriculum_db.py` replaces the filesystem registry reads on the
SELECTION path: concepts and their pydantic ``ConceptDefinition`` come from the
``apollo_concepts`` rows, scoped to a course via ``apollo_subjects.search_space_id``.
These run on real Postgres (the JSONB scoping JOIN must run on PG, not SQLite).
"""

from __future__ import annotations

import pytest

from apollo.subjects import ConceptDefinition
from apollo.subjects.curriculum_db import (
    ConceptNotFoundError,
    ConceptRow,
    list_course_concepts,
    load_concept_definition,
)
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_concept_payloads,
    seed_concept,
    seed_course,
    seed_search_space,
)

pytestmark = pytest.mark.integration


async def test_list_course_concepts_returns_only_that_courses_concepts(db_session):
    """T1.1 — same concept slug in two courses; the JOIN-on-search_space_id scoping
    returns ONLY the queried course's concept_id."""
    sid_a, cid_a, _ = await seed_course(
        db_session, subject_slug="physics_a", concept_slug="bernoulli", problems=[]
    )
    sid_b, cid_b, _ = await seed_course(
        db_session, subject_slug="physics_b", concept_slug="bernoulli", problems=[]
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
    """T1.3 — two concepts in one course are returned ascending by concept_id."""
    sid = await seed_search_space(db_session)
    cid_1 = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s1", concept_slug="c_alpha"
    )
    cid_2 = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s2", concept_slug="c_beta"
    )

    rows = await list_course_concepts(db_session, search_space_id=sid)
    ids = [r.concept_id for r in rows]
    assert ids == sorted(ids)
    assert ids == [cid_1, cid_2]


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

    cd = await load_concept_definition(db_session, concept_id=cid)
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
        await load_concept_definition(db_session, concept_id=999999)


async def test_load_concept_definition_problems_dir_is_sentinel_not_globbed(db_session):
    """T1.6 — problems_dir is a sentinel non-existent path: the runtime never
    globs the filesystem for problems (criterion #2)."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s1", concept_slug="c1")
    cd = await load_concept_definition(db_session, concept_id=cid)
    assert cd.problems_dir.exists() is False
