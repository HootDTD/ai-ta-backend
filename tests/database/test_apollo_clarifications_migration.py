import pytest
from sqlalchemy import select

from apollo.persistence.models import Clarification

pytestmark = pytest.mark.integration


async def _seed_parents(db_session):
    # Minimal parent rows so FKs resolve. Reuse the helpers the sibling
    # persistence tests use (search space, auth user, concept, session, attempt).
    # See tests/database/test_apollo_comparison_run_persistence.py for the exact
    # fixture builders; import and call them here rather than re-deriving.
    from tests.database._apollo_db_fixtures import seed_attempt_chain

    return await seed_attempt_chain(db_session)


async def test_clarification_roundtrip_and_state_transition(db_session):
    ctx = await _seed_parents(db_session)
    row = Clarification(
        attempt_id=ctx.attempt_id,
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        search_space_id=ctx.search_space_id,
        concept_id=ctx.concept_id,
        node_id="s1",
        candidate_key="cond.bernoulli",
        state="asked_waiting",
        probe_question="Which way does pressure go?",
        original_statement="pressure changes",
        asked_turn=2,
    )
    db_session.add(row)
    await db_session.flush()

    fetched = (
        await db_session.execute(
            select(Clarification).where(Clarification.attempt_id == ctx.attempt_id)
        )
    ).scalar_one()
    assert fetched.state == "asked_waiting"
    assert fetched.clarification_text is None

    fetched.state = "confirmed"
    fetched.clarification_text = "pressure is lower where it moves faster"
    fetched.answered_turn = 4
    await db_session.flush()
    assert fetched.answered_turn == 4


async def test_one_clarification_per_node_per_attempt(db_session):
    from sqlalchemy.exc import IntegrityError

    ctx = await _seed_parents(db_session)
    a = Clarification(
        attempt_id=ctx.attempt_id,
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        search_space_id=ctx.search_space_id,
        concept_id=ctx.concept_id,
        node_id="s1",
        candidate_key="k",
        state="asked_waiting",
        probe_question="q",
        original_statement="o",
        asked_turn=1,
    )
    db_session.add(a)
    await db_session.flush()
    b = Clarification(
        attempt_id=ctx.attempt_id,
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        search_space_id=ctx.search_space_id,
        concept_id=ctx.concept_id,
        node_id="s1",
        candidate_key="k2",
        state="asked_waiting",
        probe_question="q2",
        original_statement="o2",
        asked_turn=3,
    )
    db_session.add(b)
    with pytest.raises(IntegrityError):
        await db_session.flush()
