"""Real-Postgres round-trip for the emergent store write+read (memo increment 1).

Exercises the Postgres ``INSERT ... ON CONFLICT DO NOTHING`` idempotency path
(the SQLite unit tests cover the sqlite dialect branch) and the derived-on-read
aggregation against real ``TIMESTAMPTZ`` values, on the session pgvector
container.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apollo.conftest import TEST_USER_ID, TEST_USER_ID_2
from apollo.emergent import store
from apollo.persistence.models import (
    ApolloSession,
    Concept,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    Subject,
)
from database.models import SearchSpace

pytestmark = pytest.mark.integration


async def _seed(db, *, slug, user_id=TEST_USER_ID):
    space = SearchSpace(name=slug, slug=slug, subject_name="X")
    db.add(space)
    await db.flush()
    subj = Subject(slug=f"s_{slug}", display_name="Sub", search_space_id=space.id)
    db.add(subj)
    await db.flush()
    concept = Concept(subject_id=subj.id, slug=f"k_{slug}", display_name="C")
    db.add(concept)
    await db.flush()
    sess = ApolloSession(
        user_id=user_id,
        search_space_id=space.id,
        concept_id=concept.id,
        status=SessionStatus.active.value,
        phase=SessionPhase.SOLVING.value,
        current_problem_id=f"p_{slug}",
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id, problem_id=f"p_{slug}", difficulty="standard", result="graded"
    )
    db.add(attempt)
    await db.flush()
    return space.id, concept.id, sess.id, attempt.id


def _payload(key, conf, opposes, span):
    return {
        "misconceptions": [
            {"canonical_key": key, "confidence": conf, "opposes": opposes, "evidence_span": span}
        ],
        "node_ledger": [],
    }


@pytest.mark.asyncio
async def test_pg_write_idempotent_and_promotes(db_session):
    space_id, concept_id, sess_id, attempt_a = await _seed(db_session, slug="emg-a")

    n1 = await store.record_observations_from_canonical(
        db_session,
        search_space_id=space_id,
        concept_id=concept_id,
        user_id=TEST_USER_ID,
        attempt_id=attempt_a,
        canonical_payload=_payload("misc.sign", 1.0, "eq.n2", "flip"),
    )
    # Idempotent re-run of the SAME attempt inserts zero (ON CONFLICT).
    n1_again = await store.record_observations_from_canonical(
        db_session,
        search_space_id=space_id,
        concept_id=concept_id,
        user_id=TEST_USER_ID,
        attempt_id=attempt_a,
        canonical_payload=_payload("misc.sign", 1.0, "eq.n2", "flip"),
    )
    # Two more distinct students to reach K=3 distinct.
    for uid in (TEST_USER_ID_2, "d0000000-0000-4000-8000-000000000004"):
        att = ProblemAttempt(
            session_id=sess_id, problem_id="p_emg-a", difficulty="standard", result="graded"
        )
        db_session.add(att)
        await db_session.flush()
        await store.record_observations_from_canonical(
            db_session,
            search_space_id=space_id,
            concept_id=concept_id,
            user_id=uid,
            attempt_id=att.id,
            canonical_payload=_payload("misc.sign", 1.0, "eq.n2", "flip2"),
        )
    await db_session.commit()

    assert n1 == 1
    assert n1_again == 0

    aggs = await store.aggregate_signatures(
        db_session, search_space_id=space_id, concept_id=concept_id
    )
    assert len(aggs) == 1
    assert aggs[0].distinct_students == 3
    assert aggs[0].mean_confidence == pytest.approx(1.0)

    promoted = await store.load_promoted_misconceptions_dict(
        db_session, search_space_id=space_id, concept_id=concept_id, now=datetime.now(UTC)
    )
    keys = {m["key"] for m in promoted["misconceptions"]}
    assert keys == {"misc.sign"}


@pytest.mark.asyncio
async def test_pg_record_observation_idempotent_and_new_sources(db_session):
    """T6: the new single-row write path against real Postgres, for both new
    source values (the capture seams' write path)."""
    space_id, concept_id, sess_id, attempt_id = await _seed(db_session, slug="emg-obs")

    n1 = await store.record_observation(
        db_session,
        search_space_id=space_id,
        concept_id=concept_id,
        user_id=TEST_USER_ID,
        attempt_id=attempt_id,
        signature="emergent.def.real_basis",
        confidence=0.8,
        opposes="def.real_basis",
        evidence_span="span",
        source="detector_unkeyed",
    )
    n1_again = await store.record_observation(
        db_session,
        search_space_id=space_id,
        concept_id=concept_id,
        user_id=TEST_USER_ID,
        attempt_id=attempt_id,
        signature="emergent.def.real_basis",
        confidence=0.8,
        opposes="def.real_basis",
        evidence_span="span",
        source="detector_unkeyed",
    )
    await db_session.commit()

    assert n1 == 1
    assert n1_again == 0

    aggs = await store.aggregate_signatures(
        db_session, search_space_id=space_id, concept_id=concept_id
    )
    assert len(aggs) == 1
    assert aggs[0].signature == "emergent.def.real_basis"
