"""WU-3C1 — real Postgres + real Neo4j tests for the :Canon projection.

The load-bearing isolation file. Seeds Layer-1 ``apollo_kg_entities`` via direct
ORM inserts on the real pgvector ``db_session`` (lighter than the full WU-3B
bernoulli harness — these tests only need a handful of entities), then runs
``project_canon`` against the Testcontainers ``neo4j_client`` (from
``tests/conftest.py`` — local container, never Aura).

THE central guarantee (test_two_concepts_same_canonical_key_*): two different
concepts each minting ``eq.continuity`` project to TWO DISTINCT ``:Canon`` nodes
because the key is the surrogate entity id, not a ``canonical_key``-derived key.
"""

from __future__ import annotations

import pytest

from apollo.knowledge_graph.canon_projection import load_entity_specs, project_canon
from apollo.persistence.models import Concept, LearnerEntity
from database.models import Course

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Direct-ORM seed helpers (real Postgres via db_session)
# ---------------------------------------------------------------------------


async def _make_course(db, *, slug: str) -> int:
    space = Course(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    return space.id


async def _make_concept(
    db, *, course_id: int, subject_slug: str, slug: str
) -> int:
    concept = Concept(
        course_id=course_id,
        subject_slug=subject_slug,
        subject_display_name=subject_slug,
        slug=slug,
        display_name=slug,
    )
    db.add(concept)
    await db.flush()
    return concept.id


async def _make_entity(
    db,
    *,
    course_id: int,
    concept_id: int,
    canonical_key: str,
    kind: str,
    display_name: str,
) -> int:
    ent = LearnerEntity(
        course_id=course_id,
        concept_id=concept_id,
        canonical_key=canonical_key,
        kind=kind,
        display_name=display_name,
        payload={},
        aliases=[],
    )
    db.add(ent)
    await db.flush()
    return ent.id


async def _seed_concept_with_entities(
    db,
    *,
    course_slug: str,
    subject_slug: str,
    concept_slug: str,
    entities,
) -> tuple[int, int, list[int]]:
    """Create course/subject/concept + the given (canonical_key, kind, name)
    entities. Returns (search_space_id, concept_id, [entity_id...])."""
    ss = await _make_course(db, slug=course_slug)
    concept = await _make_concept(
        db, course_id=ss, subject_slug=subject_slug, slug=concept_slug
    )
    ids = [
        await _make_entity(
            db,
            course_id=ss,
            concept_id=concept,
            canonical_key=ck,
            kind=kind,
            display_name=name,
        )
        for (ck, kind, name) in entities
    ]
    return ss, concept, ids


async def _count_canon(neo, *, canonical_key: str | None = None) -> int:
    async with neo.session() as s:
        if canonical_key is None:
            rec = await (await s.run("MATCH (c:Canon) RETURN count(c) AS n")).single()
        else:
            rec = await (
                await s.run(
                    "MATCH (c:Canon {canonical_key: $ck}) RETURN count(c) AS n",
                    ck=canonical_key,
                )
            ).single()
        return int(rec["n"])


async def _canon_keys(neo, *, canonical_key: str) -> list[int]:
    async with neo.session() as s:
        result = await s.run(
            "MATCH (c:Canon {canonical_key: $ck}) RETURN c.key AS key ORDER BY c.key",
            ck=canonical_key,
        )
        return [int(record["key"]) async for record in result]


# ---------------------------------------------------------------------------
# load_entity_specs — scope selectors (real Postgres)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_entity_specs_scoped_by_concept(db_session):
    _, c1, ids1 = await _seed_concept_with_entities(
        db_session,
        course_slug="c1",
        subject_slug="s1",
        concept_slug="k1",
        entities=[("eq.a", "equation", "A"), ("eq.b", "equation", "B")],
    )
    # A second concept (different entities) under a different course.
    _, c2, _ = await _seed_concept_with_entities(
        db_session,
        course_slug="c2",
        subject_slug="s2",
        concept_slug="k2",
        entities=[("eq.c", "equation", "C")],
    )

    specs = await load_entity_specs(db_session, concept_id=c1)
    assert {s.key for s in specs} == set(ids1)
    assert all(s.concept_id == c1 for s in specs)
    # search_space_id is carried directly by the folded concept row.
    assert len({s.search_space_id for s in specs}) == 1


@pytest.mark.asyncio
async def test_load_entity_specs_scoped_by_search_space(db_session):
    ss1, c1, ids1 = await _seed_concept_with_entities(
        db_session,
        course_slug="cc1",
        subject_slug="ss1",
        concept_slug="kk1",
        entities=[("eq.a", "equation", "A")],
    )
    # Same course, second subject+concept -> its entities are also in scope.
    c1b = await _make_concept(
        db_session, course_id=ss1, subject_slug="ss1b", slug="kk1b"
    )
    id_b = await _make_entity(
        db_session,
        course_id=ss1,
        concept_id=c1b,
        canonical_key="eq.b",
        kind="equation",
        display_name="B",
    )
    # A different course's entity must NOT appear.
    await _seed_concept_with_entities(
        db_session,
        course_slug="cc2",
        subject_slug="ss2",
        concept_slug="kk2",
        entities=[("eq.c", "equation", "C")],
    )

    specs = await load_entity_specs(db_session, search_space_id=ss1)
    assert {s.key for s in specs} == set(ids1) | {id_b}
    assert all(s.search_space_id == ss1 for s in specs)


@pytest.mark.asyncio
async def test_load_entity_specs_refuses_unscoped(db_session):
    with pytest.raises(ValueError):
        await load_entity_specs(db_session)


# test_load_entity_specs_includes_misconceptions DELETED (DB-13): it seeded a
# LearnerEntity with kind="misconception", which learner_entities__kind__check
# no longer admits on real Postgres (see canon_projection.load_entity_specs's
# docstring — "kind='misconception' is no longer a row this query can ever
# return"). The scenario is now impossible; load_entity_specs's no-kind-
# filtering behavior is still covered by the surviving scope-selector tests.


# ---------------------------------------------------------------------------
# Full projection (real Postgres + real Neo4j)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concepts_same_canonical_key_project_to_distinct_canon_nodes(
    db_session,
    neo4j_client,
):
    """THE fusion-isolation guarantee: two DIFFERENT concepts (here, two
    different courses) that each mint canonical_key='eq.continuity' must project
    to TWO DISTINCT :Canon nodes — distinct surrogate-id keys prevent fusion."""
    ss_a, concept_a, ids_a = await _seed_concept_with_entities(
        db_session,
        course_slug="fa",
        subject_slug="sa",
        concept_slug="fluid_a",
        entities=[("eq.continuity", "equation", "Continuity A")],
    )
    ss_b, concept_b, ids_b = await _seed_concept_with_entities(
        db_session,
        course_slug="fb",
        subject_slug="sb",
        concept_slug="fluid_b",
        entities=[("eq.continuity", "equation", "Continuity B")],
    )
    # Distinct surrogate ids despite identical canonical_key (allowed:
    # uniqueness is per-concept).
    assert ids_a[0] != ids_b[0]

    await project_canon(db_session, neo4j_client, concept_id=concept_a)
    await project_canon(db_session, neo4j_client, concept_id=concept_b)

    # TWO distinct :Canon nodes for eq.continuity, keyed on the two entity ids.
    assert await _count_canon(neo4j_client, canonical_key="eq.continuity") == 2
    assert set(await _canon_keys(neo4j_client, canonical_key="eq.continuity")) == {
        ids_a[0],
        ids_b[0],
    }


@pytest.mark.asyncio
async def test_load_then_project_carries_db_properties_end_to_end(
    db_session,
    neo4j_client,
):
    ss, concept_id, ids = await _seed_concept_with_entities(
        db_session,
        course_slug="e2e",
        subject_slug="se2e",
        concept_slug="ke2e",
        entities=[("eq.bernoulli", "equation", "Bernoulli Equation")],
    )
    entity_id = ids[0]

    result = await project_canon(db_session, neo4j_client, concept_id=concept_id)
    assert result.entity_count == 1

    async with neo4j_client.session() as s:
        rec = await (
            await s.run("MATCH (c:Canon {key: $key}) RETURN c AS c", key=entity_id)
        ).single()
    node = dict(rec["c"])
    assert node["key"] == entity_id
    assert node["canonical_key"] == "eq.bernoulli"
    assert node["kind"] == "equation"
    assert node["display_name"] == "Bernoulli Equation"
    assert node["concept_id"] == concept_id
    assert node["search_space_id"] == ss


@pytest.mark.asyncio
async def test_project_is_idempotent_against_real_postgres(db_session, neo4j_client):
    _, concept_id, _ = await _seed_concept_with_entities(
        db_session,
        course_slug="idem",
        subject_slug="sidem",
        concept_slug="kidem",
        entities=[("eq.a", "equation", "A"), ("eq.b", "equation", "B")],
    )
    await project_canon(db_session, neo4j_client, concept_id=concept_id)
    first = await _count_canon(neo4j_client)
    await project_canon(db_session, neo4j_client, concept_id=concept_id)
    second = await _count_canon(neo4j_client)
    assert first == 2
    assert second == 2  # no duplicates on re-run


@pytest.mark.asyncio
async def test_canon_key_unique_constraint_blocks_duplicate_key(neo4j_client):
    """Apply the canon_key_unique constraint to the container, then two CREATEs
    with the same key: the second must error (defense-in-depth behind MERGE)."""
    from neo4j.exceptions import ClientError

    async with neo4j_client.session() as s:
        await s.run(
            "CREATE CONSTRAINT canon_key_unique IF NOT EXISTS FOR (c:Canon) REQUIRE c.key IS UNIQUE"
        )
        await s.run("CREATE (c:Canon {key: 9001})")
        with pytest.raises(ClientError):
            await s.run("CREATE (c:Canon {key: 9001})")
        # cleanup the constraint so it doesn't leak into other tests sharing
        # the session container.
        await s.run("MATCH (c:Canon {key: 9001}) DETACH DELETE c")
        await s.run("DROP CONSTRAINT canon_key_unique IF EXISTS")
