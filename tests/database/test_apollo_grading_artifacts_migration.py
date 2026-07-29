"""Live-ORM behavioral tests for ``GradingRun`` (DB-14/A7 artifacts-only
merge onto ``internal.grading_runs``, formerly ``GradingArtifact`` /
``apollo_grading_artifacts`` from migration 034).

Despite the filename, this module exercises the LIVE ORM/current-schema
shape via the ``db_session`` harness (``Base.metadata.create_all``) — it does
NOT pin the frozen migration 034 SQL chain (that is
``test_apollo_grading_artifacts_constraints.py``, pure asyncpg against the
migration file verbatim, for the CHECK constraints the ORM layer declares
none of — see that module's docstring). This one retargets in place with
DB-14 rather than staying pinned."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from apollo.persistence.models import GradingRun
from apollo.subjects.tests._curriculum_fixtures import (
    minimal_problem_payload,
    problem_database_id,
    seed_concept,
    seed_problems,
)
from tests.database._apollo_db_fixtures import seed_attempt_chain

pytestmark = pytest.mark.integration


async def _seed_problem(db, *, search_space_id: int) -> int:
    """A real ``app.problems`` row -- ``GradingRun.problem_id`` is a NOT NULL
    FK to ``app.problems.id`` (ON DELETE RESTRICT), unlike the pre-merge
    model's unconstrained ``Text`` column."""
    concept_id = await seed_concept(
        db,
        search_space_id=search_space_id,
        subject_slug=f"subj-{uuid.uuid4().hex[:8]}",
        concept_slug=f"c-{uuid.uuid4().hex[:8]}",
    )
    code = f"p-{uuid.uuid4().hex[:8]}"
    await seed_problems(db, concept_id=concept_id, payloads=[minimal_problem_payload(code=code)])
    return await problem_database_id(db, concept_id=concept_id, problem_code=code)


async def test_insert_and_unique_attempt_role(db_session):
    chain = await seed_attempt_chain(db_session)
    problem_id = await _seed_problem(db_session, search_space_id=chain.search_space_id)

    row = GradingRun(
        attempt_id=chain.attempt_id,
        user_id=chain.user_id,
        search_space_id=chain.search_space_id,
        problem_id=problem_id,
        role="canonical",
        grader_used="graph",
        grader_version="graph-v1",
        version_details={"grader": "graph-v1"},
        node_ledger=[],
        edge_ledger=[],
        score_details={"composite": 0.8, "weights": {"w_n": 0.6, "w_e": 0.25, "p": 0.15}},
        composite_score=0.8,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.id is not None

    # Same (attempt_id, role, grader_version) -> the DDL's
    # grading_runs_attempt_id_role_grader_version_key unique constraint.
    dup = GradingRun(
        attempt_id=chain.attempt_id,
        user_id=chain.user_id,
        search_space_id=chain.search_space_id,
        problem_id=problem_id,
        role="canonical",
        grader_used="graph",
        grader_version="graph-v1",
        version_details={"grader": "graph-v1"},
        node_ledger=[],
        edge_ledger=[],
        score_details={"composite": 0.8, "weights": {"w_n": 0.6, "w_e": 0.25, "p": 0.15}},
        composite_score=0.8,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):  # grading_runs_attempt_id_role_grader_version_key
        await db_session.flush()


async def test_defaults_apply_for_optional_json_columns(db_session):
    """``grader_payload``/``node_ledger``/``edge_ledger``/``score_details``/
    ``version_details`` default to their empty JSONB shape;
    ``abstained`` (typed, NOT NULL) defaults false; ``abstention_details``/
    ``grading_latency_ms`` (nullable, no default) stay ``None``."""
    chain = await seed_attempt_chain(db_session)
    problem_id = await _seed_problem(db_session, search_space_id=chain.search_space_id)

    row = GradingRun(
        attempt_id=chain.attempt_id,
        user_id=chain.user_id,
        search_space_id=chain.search_space_id,
        problem_id=problem_id,
        role="pair",
        grader_used="llm_fallback",
        grader_version="llm-v1",
        version_details={"grader": "llm-v1"},
        node_ledger=[],
        edge_ledger=[],
        score_details={"composite": 0.5},
    )
    db_session.add(row)
    await db_session.flush()
    assert row.grader_payload == {}
    assert row.abstained is False
    assert row.abstention_details is None
    assert row.grading_latency_ms is None
