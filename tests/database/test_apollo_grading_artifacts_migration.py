import pytest
from sqlalchemy.exc import IntegrityError

from tests.database._apollo_db_fixtures import seed_attempt_chain

pytestmark = pytest.mark.integration

ARTIFACT_ROW = dict(
    role="canonical",
    grader_used="graph",
    problem_id="p1",
    versions={"grader": "graph-v1"},
    node_ledger=[],
    edge_ledger=[],
    scores={"composite": 0.8, "weights": {"w_n": 0.706, "w_e": 0.294, "p": 0.15}},
)


async def test_insert_and_unique_attempt_role(db_session):
    chain = await seed_attempt_chain(db_session)
    from apollo.persistence.models import GradingArtifact

    row = GradingArtifact(
        attempt_id=chain.attempt_id,
        user_id=chain.user_id,
        search_space_id=chain.search_space_id,
        **ARTIFACT_ROW,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.id is not None

    dup = GradingArtifact(
        attempt_id=chain.attempt_id,
        user_id=chain.user_id,
        search_space_id=chain.search_space_id,
        **ARTIFACT_ROW,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):  # uq_grading_artifact_attempt_role
        await db_session.flush()


async def test_defaults_apply_for_optional_json_columns(db_session):
    chain = await seed_attempt_chain(db_session)
    from apollo.persistence.models import GradingArtifact

    row = GradingArtifact(
        attempt_id=chain.attempt_id,
        user_id=chain.user_id,
        search_space_id=chain.search_space_id,
        role="pair",
        grader_used="llm_fallback",
        problem_id="p1",
        versions={"grader": "llm-v1"},
        node_ledger=[],
        edge_ledger=[],
        scores={"composite": 0.5},
    )
    db_session.add(row)
    await db_session.flush()
    assert row.misconceptions == []
    assert row.clarification_trace == []
    assert row.abstention is None
    assert row.grading_latency_ms is None
