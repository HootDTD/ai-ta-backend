"""P3.1 — Negotiation audit-log SQLAlchemy model + migration sanity.

Tests the Postgres side of the Negotiable OLM:
  - `KGNegotiation` ORM model insert / read / FK cascade.
  - The composite (attempt_id, entry_id) index is registered.
  - Migration 021 file exists and contains the contract bits the runtime
    depends on (table name, CHECK constraints on actor + move).

Tests run against in-memory SQLite (project convention). Postgres-only
features (CHECK constraints) are validated by reading the migration text
rather than by relying on SQLite to enforce them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.persistence.models import (
    KGNegotiation,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringSession,
)
from database.models import Base

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "database"
    / "migrations"
    / "021_apollo_kg_negotiations.sql"
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    tables = [
        TutoringSession.__table__,
        ProblemAttempt.__table__,
        KGNegotiation.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def attempt(db: AsyncSession):
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        concept_id=1,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
    )
    db.add(sess)
    await db.flush()
    a = ProblemAttempt(
        session_id=sess.id,
        problem_id=1,
        difficulty="intro",
        user_id=sess.user_id,
        course_id=sess.course_id,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


# ---------------------------------------------------------------------------
# Model contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_and_read_back_challenge_row(db: AsyncSession, attempt):
    row = KGNegotiation(
        attempt_id=attempt.id,
        entry_id="eq1",
        actor="student",
        move="challenge",
        payload={"reason": "you misheard, this is wrong"},
    )
    db.add(row)
    await db.commit()

    rows = (
        (await db.execute(select(KGNegotiation).where(KGNegotiation.attempt_id == attempt.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].entry_id == "eq1"
    assert rows[0].actor == "student"
    assert rows[0].move == "challenge"
    assert rows[0].payload == {"reason": "you misheard, this is wrong"}


@pytest.mark.asyncio
async def test_three_moves_for_same_entry_each_get_their_own_row(
    db: AsyncSession,
    attempt,
):
    """Audit-log semantics: every move is appended; nothing is updated in
    place. The Done-gate's "has been touched" check is `count(*) > 0`,
    not a status overwrite — and the diagnostic narration counts moves."""
    for actor, move, payload in [
        ("student", "challenge", {"reason": "x"}),
        ("student", "paraphrase", {"surface_form": "y"}),
        ("student", "skip", {}),
    ]:
        db.add(
            KGNegotiation(
                attempt_id=attempt.id,
                entry_id="eq1",
                actor=actor,
                move=move,
                payload=payload,
            )
        )
    await db.commit()

    rows = (
        (await db.execute(select(KGNegotiation).where(KGNegotiation.entry_id == "eq1")))
        .scalars()
        .all()
    )
    assert {r.move for r in rows} == {"challenge", "paraphrase", "skip"}


@pytest.mark.asyncio
async def test_payload_defaults_to_empty_dict(db: AsyncSession, attempt):
    """Skip payload is `{}` — the model default lets handlers omit it."""
    row = KGNegotiation(
        attempt_id=attempt.id,
        entry_id="eq1",
        actor="student",
        move="skip",
    )
    db.add(row)
    await db.commit()
    fetched = (
        await db.execute(select(KGNegotiation).where(KGNegotiation.id == row.id))
    ).scalar_one()
    assert fetched.payload == {}


def test_attempt_fk_declared_with_cascade():
    """SQLAlchemy declares the FK + ON DELETE CASCADE — Postgres enforces
    it (SQLite-in-tests does not, by default). Inspect the model rather
    than relying on runtime enforcement at the test layer."""
    col = KGNegotiation.__table__.columns["attempt_id"]
    assert col.nullable is False
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.column.table.name == "apollo_problem_attempts"
    assert fk.ondelete == "CASCADE"


def test_actor_and_move_columns_are_text_not_enum():
    """Convention from the rest of Apollo: enumish strings live in TEXT
    columns with CHECK constraints in SQL (validated by the migration
    test). Python-side they are plain str."""
    actor_col = KGNegotiation.__table__.columns["actor"]
    move_col = KGNegotiation.__table__.columns["move"]
    assert actor_col.type.python_type is str
    assert move_col.type.python_type is str


# ---------------------------------------------------------------------------
# Migration file sanity
# ---------------------------------------------------------------------------


def test_migration_021_file_exists():
    assert _MIGRATION_PATH.exists(), f"missing migration file: {_MIGRATION_PATH}"


def test_migration_creates_apollo_kg_negotiations_table():
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS apollo_kg_negotiations" in body


def test_migration_constrains_actor_to_three_values():
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "actor IN ('student','parser','system')" in body


def test_migration_constrains_move_to_three_moves():
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "move IN ('challenge','paraphrase','skip')" in body


def test_migration_indexes_attempt_entry_pair():
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "apollo_kg_negotiations_attempt_entry_idx" in body


def test_migration_cascades_on_attempt_delete():
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE" in body
