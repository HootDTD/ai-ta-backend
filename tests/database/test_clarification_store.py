import pytest

from apollo.clarification import store
from apollo.persistence.models import Clarification

pytestmark = pytest.mark.integration


async def test_write_load_record_confirm_cycle(db_session):
    from tests.database._apollo_db_fixtures import seed_attempt_chain
    ctx = await seed_attempt_chain(db_session)
    await store.write_asked_waiting(
        db_session, attempt_id=ctx.attempt_id, session_id=ctx.session_id, user_id=ctx.user_id,
        search_space_id=ctx.search_space_id, concept_id=ctx.concept_id, node_id="s1",
        candidate_key="cond.bernoulli", probe_question="which way?", original_statement="p~v", asked_turn=2,
    )
    # Idempotent: a second asked_waiting for the same (attempt, node) is a no-op.
    await store.write_asked_waiting(
        db_session, attempt_id=ctx.attempt_id, session_id=ctx.session_id, user_id=ctx.user_id,
        search_space_id=ctx.search_space_id, concept_id=ctx.concept_id, node_id="s1",
        candidate_key="other", probe_question="again?", original_statement="p~v", asked_turn=2,
    )
    waiting = await store.load_asked_waiting(db_session, attempt_id=ctx.attempt_id)
    assert len(waiting) == 1

    await store.record_outcome(
        db_session, clarification_id=waiting[0].id, state="confirmed",
        clarification_text="lower where faster", answered_turn=4,
    )
    confirmed = await store.load_confirmed_resolutions(db_session, attempt_id=ctx.attempt_id)
    assert confirmed == {"s1": "cond.bernoulli"}
    assert await store.load_asked_waiting(db_session, attempt_id=ctx.attempt_id) == []
