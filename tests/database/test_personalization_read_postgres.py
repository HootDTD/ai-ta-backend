"""WU-6A1 — real-PG acceptance gate: the learner-profile READ path.

Drives ``read_learner_profile`` (the genuinely-first read over
``apollo_learner_state`` — the existing ``persistence._lock_prior_state`` is a
``FOR UPDATE`` write-lock and is NOT reused here) against the real ``db_session``
(Base.metadata.create_all, savepoint rollback per test).

These MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of the gate.
The per-classroom isolation invariant (spec §1.4 — mastery never crosses
courses) is the join semantics under test, so it is pinned on real Postgres, not
SQLite (the SQLite create-all variant lacks the migration-026 CHECKs + FK chain).

No Neo4j, no LLM, no network on this path: the read is pure Postgres. Every test
seeds via direct ORM and asserts the returned ``LearnerProfile``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from apollo.conftest import TEST_USER_ID, TEST_USER_ID_2
from apollo.learner_model.personalization_read import (
    EntityProfile,
    LearnerProfile,
    read_learner_profile,
)
from apollo.persistence.models import (
    Concept,
    EntityPrereq,
    KGEntity,
    LearnerState,
    Subject,
)
from database.models import SearchSpace

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Direct-ORM seed helpers (real Postgres via db_session). Adapted from
# tests/database/test_done_layer3_route_postgres.py:_seed_course_with_entity.
# ---------------------------------------------------------------------------


async def _seed_course(db, *, course_slug) -> tuple[int, int]:
    """Insert SearchSpace -> Subject -> Concept. Returns (search_space_id, concept_id).

    One concept per course; entities are per-concept via the KGEntity.concept_id FK.
    """
    space = SearchSpace(name=course_slug, slug=course_slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    subj = Subject(
        slug=f"s_{course_slug}", display_name="Fluids", search_space_id=space.id
    )
    db.add(subj)
    await db.flush()
    concept = Concept(
        subject_id=subj.id, slug=f"k_{course_slug}", display_name="Continuity"
    )
    db.add(concept)
    await db.flush()
    return space.id, concept.id


async def _seed_entity(db, *, concept_id, canonical_key, kind="equation") -> int:
    """Add one KGEntity under concept_id. Returns its id."""
    ent = KGEntity(
        concept_id=concept_id,
        canonical_key=canonical_key,
        kind=kind,
        display_name=canonical_key,
        payload={},
        aliases=[],
    )
    db.add(ent)
    await db.flush()
    return ent.id


async def _seed_prereq(db, *, from_id, to_id) -> None:
    """Add one EntityPrereq edge (from depends on to)."""
    db.add(EntityPrereq(from_entity_id=from_id, to_entity_id=to_id))
    await db.flush()


async def _seed_state(
    db,
    *,
    sid,
    entity_id,
    mastery,
    confidence,
    misconception_code=None,
    belief=None,
    user_id=TEST_USER_ID,
) -> None:
    """Add one apollo_learner_state row.

    ``belief`` is NOT NULL in the schema, so a faithful length-3 vector is
    supplied. The default belief = [1-mastery, 0, mastery] is in-range and
    length-3; tests that must prove "no recompute" pass an explicit ``belief``
    that does NOT recompute to the seeded ``mastery``.
    """
    if belief is None:
        belief = [round(1.0 - mastery, 4), 0.0, round(mastery, 4)]
    db.add(
        LearnerState(
            user_id=user_id,
            search_space_id=sid,
            entity_id=entity_id,
            belief=belief,
            mastery=mastery,
            confidence=confidence,
            misconception_code=misconception_code,
            evidence_count=1,
            last_evidence_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await db.flush()


# ---------------------------------------------------------------------------
# 1. Course isolation — the §1.4 per-classroom invariant (RECON test 1).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_course_isolation_mastery_never_crosses_courses(db_session):
    """Two courses share the SAME concept canonical_key. A read scoped to course
    A returns ONLY course A's mastery; course B's value never leaks."""
    sid_a, cid_a = await _seed_course(db_session, course_slug="courseA")
    sid_b, cid_b = await _seed_course(db_session, course_slug="courseB")

    # Same canonical_key under each concept => one entity row per course.
    e_a = await _seed_entity(db_session, concept_id=cid_a, canonical_key="eq.continuity")
    e_b = await _seed_entity(db_session, concept_id=cid_b, canonical_key="eq.continuity")

    # Same user, distinct sentinel mastery per course.
    await _seed_state(db_session, sid=sid_a, entity_id=e_a, mastery=0.42, confidence=0.6)
    await _seed_state(db_session, sid=sid_b, entity_id=e_b, mastery=0.91, confidence=0.9)

    profile = await read_learner_profile(
        db_session,
        user_id=TEST_USER_ID,
        search_space_id=sid_a,
        concept_id=cid_a,
    )

    assert profile.by_canonical_key["eq.continuity"].mastery == pytest.approx(
        0.42, abs=1e-6
    )
    # Course B's value (0.91) must never appear anywhere in the result.
    for ep in profile.by_canonical_key.values():
        assert ep.mastery != pytest.approx(0.91, abs=1e-6)
    assert profile.is_empty is False


# ---------------------------------------------------------------------------
# 2. Cold-start — the PROD path (RECON test 2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_start_empty_state_returns_well_formed_empty_profile(db_session):
    """ZERO learner_state rows => is_empty + empty by_canonical_key, but the
    maps/edges STILL populate from kg_entities/prereqs."""
    sid, cid = await _seed_course(db_session, course_slug="cold")
    e1 = await _seed_entity(db_session, concept_id=cid, canonical_key="eq.continuity")
    e2 = await _seed_entity(
        db_session, concept_id=cid, canonical_key="cond.incompressibility"
    )
    await _seed_prereq(db_session, from_id=e1, to_id=e2)  # e1 depends on e2

    profile = await read_learner_profile(
        db_session, user_id=TEST_USER_ID, search_space_id=sid, concept_id=cid
    )

    assert profile.is_empty is True
    assert profile.by_canonical_key == {}
    # Structural maps + edges still available to WU-6A2.
    assert profile.entity_id_by_key == {
        "eq.continuity": e1,
        "cond.incompressibility": e2,
    }
    assert profile.key_by_entity_id == {e1: "eq.continuity", e2: "cond.incompressibility"}
    assert profile.prereq_edges == ((e1, e2),)


# ---------------------------------------------------------------------------
# 3. Column read parity — proves no belief recompute (RECON test 3).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_column_read_parity_no_belief_recompute(db_session):
    """Stored columns are returned VERBATIM. The seeded belief recomputes to a
    DIFFERENT mastery (0.5), so returning 0.42 proves the read takes the COLUMN,
    not the belief vector."""
    sid, cid = await _seed_course(db_session, course_slug="parity")
    e_id = await _seed_entity(db_session, concept_id=cid, canonical_key="eq.bernoulli")

    # belief [0.2, 0.6, 0.2] -> mastery_of = 0.5*0.6 + 0.2 = 0.5 != 0.42.
    await _seed_state(
        db_session,
        sid=sid,
        entity_id=e_id,
        mastery=0.42,
        confidence=0.70,
        misconception_code="misc.something",
        belief=[0.2, 0.6, 0.2],
    )

    profile = await read_learner_profile(
        db_session, user_id=TEST_USER_ID, search_space_id=sid, concept_id=cid
    )

    ep = profile.by_canonical_key["eq.bernoulli"]
    assert ep.mastery == pytest.approx(0.42, abs=1e-6)
    assert ep.confidence == pytest.approx(0.70, abs=1e-6)
    assert ep.misconception_code == "misc.something"
    assert ep.entity_id == e_id
    assert ep.canonical_key == "eq.bernoulli"
    assert profile.is_empty is False


# ---------------------------------------------------------------------------
# 4. Prereq edges + id<->key maps round-trip over a small DAG (RECON test 4).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prereq_edges_and_id_key_maps_round_trip(db_session):
    """3 entities, two prereq edges (a->b, c->b). Edges come back RAW + sorted;
    the id<->key maps are exact inverses; endpoints resolve through key_by_entity_id."""
    sid, cid = await _seed_course(db_session, course_slug="dag")
    a = await _seed_entity(db_session, concept_id=cid, canonical_key="eq.continuity")
    b = await _seed_entity(
        db_session, concept_id=cid, canonical_key="cond.incompressibility"
    )
    c = await _seed_entity(db_session, concept_id=cid, canonical_key="eq.bernoulli")

    await _seed_prereq(db_session, from_id=a, to_id=b)  # a depends on b
    await _seed_prereq(db_session, from_id=c, to_id=b)  # c depends on b

    profile = await read_learner_profile(
        db_session, user_id=TEST_USER_ID, search_space_id=sid, concept_id=cid
    )

    assert profile.prereq_edges == tuple(sorted([(a, b), (c, b)]))
    for f, t in profile.prereq_edges:
        assert f in profile.key_by_entity_id
        assert t in profile.key_by_entity_id
    # id<->key maps are exact inverses over {a, b, c}.
    assert {v: k for k, v in profile.entity_id_by_key.items()} == profile.key_by_entity_id
    assert set(profile.entity_id_by_key) == {
        "eq.continuity",
        "cond.incompressibility",
        "eq.bernoulli",
    }
    assert profile.is_empty is True  # no state seeded => edges/maps independent of state


# ---------------------------------------------------------------------------
# 4b. Cross-concept prereq edges are EXCLUDED — prereq_edges stays self-contained.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_concept_prereq_edge_is_excluded(db_session):
    """A prereq edge with one endpoint in ANOTHER concept is dropped: prereq_edges
    stays within-concept so every endpoint resolves through key_by_entity_id. Under
    a from-OR-to filter these edges would leak a foreign entity_id (failing the
    'endpoint in key_by_entity_id' invariant); the within-concept from-AND-to filter
    excludes them. The wedge does not gate on out-of-concept prerequisites."""
    sid, cid = await _seed_course(db_session, course_slug="xconcept")
    e1 = await _seed_entity(db_session, concept_id=cid, canonical_key="eq.continuity")
    e2 = await _seed_entity(
        db_session, concept_id=cid, canonical_key="cond.incompressibility"
    )
    # A different concept with its own entity (the foreign endpoint).
    _, other_cid = await _seed_course(db_session, course_slug="xconcept_other")
    foreign = await _seed_entity(
        db_session, concept_id=other_cid, canonical_key="eq.foreign"
    )

    await _seed_prereq(db_session, from_id=e1, to_id=e2)  # within-concept: KEPT
    await _seed_prereq(db_session, from_id=e1, to_id=foreign)  # cross-concept: DROPPED
    await _seed_prereq(db_session, from_id=foreign, to_id=e2)  # cross-concept: DROPPED

    profile = await read_learner_profile(
        db_session, user_id=TEST_USER_ID, search_space_id=sid, concept_id=cid
    )

    # Only the within-concept edge survives; the foreign id never appears.
    assert profile.prereq_edges == ((e1, e2),)
    for f, t in profile.prereq_edges:
        assert f in profile.key_by_entity_id
        assert t in profile.key_by_entity_id
    assert foreign not in profile.key_by_entity_id


# ---------------------------------------------------------------------------
# 5. Unknown concept (zero entities) short-circuits before the state query.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_concept_returns_empty_without_state_query(db_session):
    """A concept with NO entities returns a fully-empty profile via the step-1
    short-circuit (exact frozen-dataclass equality)."""
    sid, cid = await _seed_course(db_session, course_slug="unknown")

    # Stray state/entity under a DIFFERENT concept must not leak in.
    _, other_cid = await _seed_course(db_session, course_slug="other")
    stray = await _seed_entity(
        db_session, concept_id=other_cid, canonical_key="eq.stray"
    )
    await _seed_state(db_session, sid=sid, entity_id=stray, mastery=0.99, confidence=0.5)

    profile = await read_learner_profile(
        db_session, user_id=TEST_USER_ID, search_space_id=sid, concept_id=cid
    )

    assert profile == LearnerProfile(
        by_canonical_key={},
        prereq_edges=(),
        entity_id_by_key={},
        key_by_entity_id={},
        is_empty=True,
    )


# ---------------------------------------------------------------------------
# 6. Mixed present/absent entities — present/absent partition of by_canonical_key.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_present_and_absent_entities(db_session):
    """Only one of three entities has a state row. The present entity carries its
    column; the two absent entities are simply ABSENT (not zero-filled), but the
    inventory (entity_id_by_key) is complete."""
    sid, cid = await _seed_course(db_session, course_slug="mixed")
    e1 = await _seed_entity(db_session, concept_id=cid, canonical_key="eq.continuity")
    await _seed_entity(db_session, concept_id=cid, canonical_key="cond.incompressibility")
    await _seed_entity(db_session, concept_id=cid, canonical_key="eq.bernoulli")

    await _seed_state(db_session, sid=sid, entity_id=e1, mastery=0.35, confidence=0.5)

    profile = await read_learner_profile(
        db_session, user_id=TEST_USER_ID, search_space_id=sid, concept_id=cid
    )

    assert set(profile.by_canonical_key) == {"eq.continuity"}
    assert set(profile.entity_id_by_key) == {
        "eq.continuity",
        "cond.incompressibility",
        "eq.bernoulli",
    }
    assert profile.is_empty is False


# ---------------------------------------------------------------------------
# 7. Other-user state does not leak — the user_id predicate branch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_user_state_does_not_leak(db_session):
    """A state row owned by TEST_USER_ID_2 must be filtered out when reading for
    TEST_USER_ID (the read is per-student)."""
    sid, cid = await _seed_course(db_session, course_slug="users")
    e_id = await _seed_entity(db_session, concept_id=cid, canonical_key="eq.continuity")

    await _seed_state(
        db_session,
        sid=sid,
        entity_id=e_id,
        mastery=0.88,
        confidence=0.9,
        user_id=TEST_USER_ID_2,
    )

    profile = await read_learner_profile(
        db_session, user_id=TEST_USER_ID, search_space_id=sid, concept_id=cid
    )

    assert profile.is_empty is True
    assert profile.by_canonical_key == {}


def test_entity_profile_and_learner_profile_are_frozen():
    """The dataclasses are frozen (immutable snapshots) — defense of the contract."""
    ep = EntityProfile(
        entity_id=1,
        canonical_key="eq.x",
        mastery=0.5,
        confidence=0.5,
        misconception_code=None,
    )
    lp = LearnerProfile(
        by_canonical_key={"eq.x": ep},
        prereq_edges=((1, 2),),
        entity_id_by_key={"eq.x": 1},
        key_by_entity_id={1: "eq.x"},
        is_empty=False,
    )
    with pytest.raises(FrozenInstanceError):
        ep.mastery = 0.9  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        lp.is_empty = True  # type: ignore[misc]
