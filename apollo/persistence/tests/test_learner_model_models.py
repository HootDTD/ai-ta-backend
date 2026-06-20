"""Fast ORM-shape + insert/read-back tests for the WU-3A learner-model models.

Mirrors ``test_negotiation_model.py`` / ``test_models_auth_scoping.py``: shape
assertions via ``__table__`` introspection, plus insert/read-back where SQLite
can honor it. Postgres-only semantics (the array CHECK, NULLS NOT DISTINCT,
real FK cascade) are asserted by model inspection here and exercised for real
in ``tests/database/test_apollo_learner_model_migration.py``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import Boolean, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.persistence.models import (
    GraphComparisonFinding,
    GraphComparisonRun,
    KGEntity,
    LearnerState,
    MasteryEvent,
    ProblemAttempt,
    Subject,
)
from database.models import Base

# ---------------------------------------------------------------------------
# Table-name mapping
# ---------------------------------------------------------------------------


def test_six_models_map_expected_tablenames():
    assert KGEntity.__tablename__ == "apollo_kg_entities"
    assert LearnerState.__tablename__ == "apollo_learner_state"
    assert MasteryEvent.__tablename__ == "apollo_mastery_events"
    assert GraphComparisonRun.__tablename__ == "apollo_graph_comparison_runs"
    assert GraphComparisonFinding.__tablename__ == "apollo_graph_comparison_findings"
    from apollo.persistence.models import EntityPrereq

    assert EntityPrereq.__tablename__ == "apollo_entity_prereqs"


# ---------------------------------------------------------------------------
# KGEntity — per-concept uniqueness + concept FK cascade
# ---------------------------------------------------------------------------


def _unique_constraints(model):
    from sqlalchemy import UniqueConstraint

    return [c for c in model.__table__.constraints if isinstance(c, UniqueConstraint)]


def test_kg_entity_unique_constraint_is_per_concept():
    ucs = _unique_constraints(KGEntity)
    cols = [tuple(c.name for c in uc.columns) for uc in ucs]
    assert ("concept_id", "canonical_key") in cols, (
        f"expected per-concept UNIQUE(concept_id, canonical_key), got {cols}"
    )
    # Guard against a global-unique regression.
    assert ("canonical_key",) not in cols


def test_kg_entity_concept_fk_cascade():
    col = KGEntity.__table__.columns["concept_id"]
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "apollo_concepts"
    assert fk.ondelete == "CASCADE"


# ---------------------------------------------------------------------------
# LearnerState — composite PK + three cascading FKs
# ---------------------------------------------------------------------------


def test_learner_state_composite_pk():
    pk_cols = [c.name for c in LearnerState.__table__.primary_key.columns]
    assert pk_cols == ["user_id", "search_space_id", "entity_id"]


def test_learner_state_fk_targets_and_cascades():
    """search_space_id and entity_id carry ORM FKs (CASCADE). user_id does NOT
    carry an ORM FK — auth.users is Supabase-managed and not in Base.metadata, so
    the FK lives only in the migration SQL (same convention as ApolloSession);
    the real cascade is exercised by the real-Postgres migration test."""
    table = LearnerState.__table__
    targets = {
        "search_space_id": "aita_search_spaces",
        "entity_id": "apollo_kg_entities",
    }
    for col_name, target_table in targets.items():
        fk = next(iter(table.columns[col_name].foreign_keys))
        assert fk.column.table.name == target_table
        assert fk.ondelete == "CASCADE", f"{col_name} should cascade"
    # user_id is a bare UUID column in the ORM (no FK object).
    assert not table.columns["user_id"].foreign_keys


# ---------------------------------------------------------------------------
# MasteryEvent — attempt SET NULL + entity CASCADE + NULLS NOT DISTINCT flag
# ---------------------------------------------------------------------------


def test_mastery_event_attempt_fk_set_null():
    table = MasteryEvent.__table__
    attempt_fk = next(iter(table.columns["attempt_id"].foreign_keys))
    assert attempt_fk.column.table.name == "apollo_problem_attempts"
    assert attempt_fk.ondelete == "SET NULL"
    entity_fk = next(iter(table.columns["entity_id"].foreign_keys))
    assert entity_fk.ondelete == "CASCADE"


def test_mastery_event_unique_nulls_not_distinct_flag():
    ucs = _unique_constraints(MasteryEvent)
    target = next(
        uc
        for uc in ucs
        if tuple(c.name for c in uc.columns) == ("attempt_id", "entity_id", "event_kind")
    )
    pg_opts = target.dialect_options["postgresql"]
    assert pg_opts["nulls_not_distinct"] is True


# ---------------------------------------------------------------------------
# GraphComparisonRun / Finding
# ---------------------------------------------------------------------------


def test_comparison_run_unique_attempt_version():
    ucs = _unique_constraints(GraphComparisonRun)
    cols = [tuple(c.name for c in uc.columns) for uc in ucs]
    assert ("attempt_id", "comparison_version") in cols


def test_comparison_finding_entity_set_null():
    table = GraphComparisonFinding.__table__
    entity_fk = next(iter(table.columns["entity_id"].foreign_keys))
    assert entity_fk.ondelete == "SET NULL"
    run_fk = next(iter(table.columns["run_id"].foreign_keys))
    assert run_fk.column.table.name == "apollo_graph_comparison_runs"
    assert run_fk.ondelete == "CASCADE"


# ---------------------------------------------------------------------------
# Edited existing models
# ---------------------------------------------------------------------------


def test_subject_requires_search_space_id():
    col = Subject.__table__.columns["search_space_id"]
    assert col.nullable is False
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "aita_search_spaces"
    assert fk.ondelete == "CASCADE"


# ---------------------------------------------------------------------------
# Insert / read-back on in-memory SQLite (per test_negotiation_model.py)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Subject.__table__,
        KGEntity.__table__,
        LearnerState.__table__,
        MasteryEvent.__table__,
        ProblemAttempt.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_insert_read_back_learner_state_sqlite(db: AsyncSession):
    row = LearnerState(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        entity_id=1,
        belief=[0.2, 0.3, 0.5],
        mastery=0.5,
        confidence=0.4,
    )
    db.add(row)
    await db.commit()
    fetched = (
        await db.execute(select(LearnerState).where(LearnerState.entity_id == 1))
    ).scalar_one()
    assert fetched.belief == [0.2, 0.3, 0.5]
    assert fetched.mastery == 0.5
    # evidence_count default applies.
    assert fetched.evidence_count == 0


@pytest.mark.asyncio
async def test_insert_read_back_mastery_event_sqlite(db: AsyncSession):
    row = MasteryEvent(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        entity_id=1,
        event_kind="covered",
        prior_belief=[0.3, 0.3, 0.4],
        posterior_belief=[0.2, 0.3, 0.5],
        mastery_after=0.5,
    )
    db.add(row)
    await db.commit()
    fetched = (
        await db.execute(select(MasteryEvent).where(MasteryEvent.entity_id == 1))
    ).scalar_one()
    assert fetched.prior_belief == [0.3, 0.3, 0.4]
    assert fetched.posterior_belief == [0.2, 0.3, 0.5]
    assert fetched.evidence_node_ids == []


@pytest.mark.asyncio
async def test_problem_attempt_has_learner_update_pending_default_false(db: AsyncSession):
    col = ProblemAttempt.__table__.columns["learner_update_pending"]
    assert isinstance(col.type, Boolean)
    assert col.nullable is False
    assert col.server_default is not None

    sess_attempt = ProblemAttempt(session_id=1, problem_id="p1", difficulty="intro")
    db.add(sess_attempt)
    await db.commit()
    fetched = (
        await db.execute(select(ProblemAttempt).where(ProblemAttempt.problem_id == "p1"))
    ).scalar_one()
    assert fetched.learner_update_pending is False
