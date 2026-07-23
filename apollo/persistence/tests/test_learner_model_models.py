"""Fast ORM-shape + insert/read-back tests for the WU-3A learner-model models,
retargeted onto the DB-13 app-schema DDL
(``supabase/migrations/20260717035041_create_app_schema_v1.sql``).

Mirrors ``test_models_auth_scoping.py``: shape assertions via ``__table__``
introspection, plus insert/read-back where SQLite can honor it. Postgres-only
semantics (the array CHECK, NULLS NOT DISTINCT, real FK cascade) are asserted
by model inspection here and exercised for real against the new DDL in
``tests/database/test_app_schema_v1.py``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import Boolean, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.persistence.models import (
    Concept,
    EntityPrereq,
    LearnerEntity,
    LearnerState,
    MasteryEvent,
    ProblemAttempt,
    TutoringSession,
)
from database.models import Base

# DB-14/A7 note: GraphComparisonRun/GraphComparisonFinding (the dead
# graph-comparison leg -- internal.grading_findings was dropped from the
# target, T-J already removed the shadow chain that wrote them) were DELETED
# from apollo.persistence.models, not renamed -- there is nothing to retarget
# their shape-assertion tests onto. test_six_models_map_expected_tablenames
# dropped its two comparison-model assertions; the two dedicated
# test_comparison_run_unique_attempt_version /
# test_comparison_finding_entity_set_null tests below were removed outright.

# ---------------------------------------------------------------------------
# Table-name mapping
# ---------------------------------------------------------------------------


def test_six_models_map_expected_tablenames():
    assert LearnerEntity.__tablename__ == "learner_entities"
    assert LearnerEntity.__table__.schema == "app"
    assert LearnerState.__tablename__ == "learner_state"
    assert LearnerState.__table__.schema == "app"
    assert MasteryEvent.__tablename__ == "mastery_events"
    assert MasteryEvent.__table__.schema == "app"
    assert EntityPrereq.__tablename__ == "entity_prerequisites"
    assert EntityPrereq.__table__.schema == "internal"


# ---------------------------------------------------------------------------
# LearnerEntity — per-concept uniqueness + concept/course FK cascade
# ---------------------------------------------------------------------------


def _unique_constraints(model):
    from sqlalchemy import UniqueConstraint

    return [c for c in model.__table__.constraints if isinstance(c, UniqueConstraint)]


def test_learner_entity_unique_constraint_is_per_concept():
    ucs = _unique_constraints(LearnerEntity)
    cols = [tuple(c.name for c in uc.columns) for uc in ucs]
    assert ("concept_id", "canonical_key") in cols, (
        f"expected per-concept UNIQUE(concept_id, canonical_key), got {cols}"
    )
    # Guard against a global-unique regression.
    assert ("canonical_key",) not in cols


def test_learner_entity_concept_fk_cascade():
    col = LearnerEntity.__table__.columns["concept_id"]
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "concepts"
    assert fk.column.table.schema == "app"
    assert fk.ondelete == "CASCADE"


def test_learner_entity_requires_course_id():
    """DB-13: course_id is a new denormalized column (not in the pre-cleanup
    LearnerEntity model) — added for initplan-safe RLS, matching the rest of the
    app schema."""
    col = LearnerEntity.__table__.columns["course_id"]
    assert col.nullable is False
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "courses"
    assert fk.column.table.schema == "app"
    assert fk.ondelete == "CASCADE"


def test_learner_entity_aliases_is_array_not_json():
    """DB-13: aliases becomes TEXT[] (was JSONB pre-cleanup)."""
    from sqlalchemy.dialects.postgresql import ARRAY

    col = LearnerEntity.__table__.columns["aliases"]
    assert isinstance(col.type, ARRAY)


def test_entity_kinds_excludes_misconception():
    """DB-13/A6: the target kind CHECK no longer admits 'misconception' —
    misconceptions live on MasteryEvent.event_kind, not as an entity kind."""
    from apollo.persistence.models import ENTITY_KINDS

    assert "misconception" not in ENTITY_KINDS


# ---------------------------------------------------------------------------
# EntityPrereq — course_id + FK targets retargeted onto learner_entities
# ---------------------------------------------------------------------------


def test_entity_prereq_course_id_and_fk_targets():
    table = EntityPrereq.__table__
    course_fk = next(iter(table.columns["course_id"].foreign_keys))
    assert course_fk.column.table.name == "courses"
    assert course_fk.ondelete == "CASCADE"
    for col_name in ("from_entity_id", "to_entity_id"):
        fk = next(iter(table.columns[col_name].foreign_keys))
        assert fk.column.table.name == "learner_entities"
        assert fk.column.table.schema == "app"
        assert fk.ondelete == "CASCADE"


# ---------------------------------------------------------------------------
# LearnerState — composite PK + three cascading FKs
# ---------------------------------------------------------------------------


def test_learner_state_composite_pk():
    """Physical PK columns — `search_space_id` is the Python attribute name
    (kept to minimize caller churn); the physical column is `course_id`."""
    pk_cols = [c.name for c in LearnerState.__table__.primary_key.columns]
    assert pk_cols == ["user_id", "course_id", "entity_id"]


def test_learner_state_fk_targets_and_cascades():
    """course_id (Python: search_space_id) and entity_id carry ORM FKs
    (CASCADE). user_id does NOT carry an ORM FK — auth.users is
    Supabase-managed and not in Base.metadata, so the FK lives only in the
    migration SQL (same convention as TutoringSession); the real cascade is
    exercised by the real-Postgres migration test."""
    table = LearnerState.__table__
    targets = {
        "course_id": "courses",
        "entity_id": "learner_entities",
    }
    for col_name, target_table in targets.items():
        fk = next(iter(table.columns[col_name].foreign_keys))
        assert fk.column.table.name == target_table
        assert fk.ondelete == "CASCADE", f"{col_name} should cascade"
    # user_id is a bare UUID column in the ORM (no FK object).
    assert not table.columns["user_id"].foreign_keys


def test_learner_state_has_no_misconception_code():
    """DB-13: misconception_code is GONE from the target DDL."""
    assert "misconception_code" not in LearnerState.__table__.columns


# ---------------------------------------------------------------------------
# MasteryEvent — attempt SET NULL + entity CASCADE + NULLS NOT DISTINCT flag
# ---------------------------------------------------------------------------


def test_mastery_event_attempt_fk_set_null():
    table = MasteryEvent.__table__
    attempt_fk = next(iter(table.columns["attempt_id"].foreign_keys))
    assert attempt_fk.column.table.name == "problem_attempts"
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


def test_mastery_event_has_no_misconception_code():
    """DB-13: misconception_code is GONE from the target DDL."""
    assert "misconception_code" not in MasteryEvent.__table__.columns


def test_mastery_event_evidence_node_ids_is_array_not_json():
    """DB-13: evidence_node_ids becomes TEXT[] (was JSONB pre-cleanup)."""
    from sqlalchemy.dialects.postgresql import ARRAY

    col = MasteryEvent.__table__.columns["evidence_node_ids"]
    assert isinstance(col.type, ARRAY)


# ---------------------------------------------------------------------------
# GraphComparisonRun / Finding -- DELETED (dead graph-comparison leg, A7 /
# DB-14 artifacts-only merge onto internal.grading_runs; T-J already removed
# the shadow chain that wrote to them). No replacement model exists to
# retarget these shape assertions onto -- see the module docstring note.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Edited existing models
# ---------------------------------------------------------------------------


def test_concept_requires_course_id():
    col = Concept.__table__.columns["course_id"]
    assert col.nullable is False
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "courses"
    assert fk.column.table.schema == "app"
    assert fk.ondelete == "CASCADE"


# ---------------------------------------------------------------------------
# Insert / read-back on in-memory SQLite (per test_negotiation_model.py)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    tables = [
        Concept.__table__,
        LearnerEntity.__table__,
        LearnerState.__table__,
        MasteryEvent.__table__,
        TutoringSession.__table__,
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

    session = TutoringSession(user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID)
    db.add(session)
    await db.flush()
    sess_attempt = ProblemAttempt(
        session_id=session.id,
        problem_id=1,
        difficulty="intro",
        user_id=session.user_id,
        course_id=session.course_id,
    )
    db.add(sess_attempt)
    await db.commit()
    fetched = (
        await db.execute(select(ProblemAttempt).where(ProblemAttempt.problem_id == 1))
    ).scalar_one()
    assert fetched.learner_update_pending is False
