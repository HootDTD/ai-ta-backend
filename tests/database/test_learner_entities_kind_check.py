"""Real-Postgres regression for DB-13 Finding 1: ``app.learner_entities`` no
longer accepts ``kind='misconception'`` (``learner_entities__kind__check``
dropped it), and the auto-provisioning / seed writers must never attempt one.

Why this needs its OWN harness instead of the shared ``db_session`` fixture in
``tests/conftest.py``: that fixture builds its schema from the SQLAlchemy ORM
metadata (``Base.metadata.create_all``), and this repo's convention is that the
ORM declares NO SQL CHECK constraints (see ``apollo/persistence/models.py``
around ``ATTEMPT_RESULTS`` and the ``LearnerEntity.kind`` column comment) — so
``learner_entities__kind__check`` simply would not exist on that schema and
this bug (a Postgres CHECK-constraint failure, invisible to the aiosqlite unit
suite) would stay invisible there too. This module instead applies the REAL
migration files (legacy snapshot + DB-13's ``create_app_schema_v1.sql``) to a
throwaway database on the same session pgvector container, mirroring
``tests/database/test_app_schema_v1.py``'s harness pattern, so the actual CHECK
constraint is live.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from apollo.persistence.learner_model_seed import (
    misconceptions_to_entities,
    reference_solution_to_entities,
)
from apollo.persistence.models import LearnerEntity
from apollo.provisioning.tag_mint_persist import upsert_entity

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[2]
_MIGRATIONS = _REPO / "supabase" / "migrations"
_SNAPSHOT = _MIGRATIONS / "20260717032246_legacy_public_snapshot.sql"
_MIGRATION = _MIGRATIONS / "20260717035041_create_app_schema_v1.sql"
_DB_NAME = "learner_entities_kind_check"

_CONNECT_ARGS = {"server_settings": {"search_path": "public,extensions"}}

# Same minimal Supabase Auth role/function shim as test_app_schema_v1.py — the
# app-schema migration's RLS policies reference auth.uid().
_AUTH_BOOTSTRAP = """
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    CREATE ROLE anon NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN NOBYPASSRLS;
  END IF;
END
$roles$;
CREATE SCHEMA auth;
CREATE TABLE auth.users (id uuid PRIMARY KEY);
CREATE FUNCTION auth.uid() RETURNS uuid
LANGUAGE sql STABLE SET search_path = '' AS $$
  SELECT nullif(current_setting('request.jwt.claims', true)::json->>'sub', '')::uuid
$$;
GRANT USAGE ON SCHEMA auth TO anon, authenticated;
GRANT EXECUTE ON FUNCTION auth.uid() TO anon, authenticated;
"""

# A tiny misconception-bearing reference graph — one entity per surviving kind
# family (equation/condition/definition) plus one misconception opposing the
# equation, shaped exactly like the real converters' inputs
# (``apollo/persistence/learner_model_seed.py``).
_PROBLEM = {
    "id": "kind_check_problem",
    "reference_solution": [
        {
            "id": "eq1",
            "entry_type": "equation",
            "content": {"label": "Bernoulli equation", "symbolic": "P + 0.5*rho*v**2 = const"},
        },
        {
            "id": "cond1",
            "entry_type": "condition",
            "content": {"label": "Steady incompressible flow"},
        },
        {
            "id": "def1",
            "entry_type": "definition",
            "content": {"label": "Static pressure"},
        },
    ],
}

_MISCONCEPTIONS = {
    "misconceptions": [
        {
            "key": "misc.pressure_velocity_confusion",
            "display_name": "Pressure/velocity confusion",
            "description": "Believes pressure and velocity rise together.",
            "opposes": "eq.eq1",
            "trigger_phrases": ["pressure and speed both go up"],
        },
    ],
}


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="module")
def kind_check_dsn(_pg_url: str):
    """Fresh throwaway DB with the REAL app-schema DDL (incl. CHECK
    constraints) applied on the session pgvector container, plus one
    ``app.courses`` / ``app.concepts`` row every test upserts entities under.
    Module-scoped (built once); each test isolates via a rolled-back
    SAVEPOINT in ``kind_check_session``."""
    base_database = make_url(_pg_url).database
    assert base_database
    admin_dsn = _dsn(_pg_url, base_database)
    test_dsn = _dsn(_pg_url, _DB_NAME)

    async def setup() -> tuple[int, int]:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
            await admin.execute(f'CREATE DATABASE "{_DB_NAME}"')
        finally:
            await admin.close()

        conn = await asyncpg.connect(test_dsn)
        try:
            await conn.execute(_AUTH_BOOTSTRAP)
            await conn.execute(_SNAPSHOT.read_text(encoding="utf-8"))
            await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
            course_id = await conn.fetchval(
                "INSERT INTO app.courses (name, slug, subject_name) "
                "VALUES ('Kind Check Course', 'kind-check-course', 'Physics') RETURNING id"
            )
            concept_id = await conn.fetchval(
                "INSERT INTO app.concepts "
                "(course_id, subject_slug, subject_display_name, slug, display_name) "
                "VALUES ($1, 'physics', 'Physics', 'kind-check-concept', 'Kind Check Concept') "
                "RETURNING id",
                course_id,
            )
            return course_id, concept_id
        finally:
            await conn.close()

    async def teardown() -> None:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}" WITH (FORCE)')
        finally:
            await admin.close()

    course_id, concept_id = asyncio.run(setup())
    sa_dsn = (
        make_url(test_dsn)
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )
    try:
        yield sa_dsn, course_id, concept_id
    finally:
        asyncio.run(teardown())


@pytest_asyncio.fixture
async def kind_check_session(kind_check_dsn: tuple[str, int, int]):
    """Function-scoped AsyncSession on the shared kind-check DB, isolated via a
    rolled-back SAVEPOINT — same convention as ``db_session`` in
    ``tests/conftest.py``."""
    sa_dsn, course_id, concept_id = kind_check_dsn
    engine = create_async_engine(sa_dsn, poolclass=NullPool, connect_args=_CONNECT_ARGS)
    conn = await engine.connect()
    trans = await conn.begin()
    session = AsyncSession(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session, course_id, concept_id
    finally:
        await session.close()
        if trans.is_active:
            await trans.rollback()
        await conn.close()
        await engine.dispose()


async def test_fixed_tag_mint_path_persists_only_surviving_kinds_no_check_violation(
    kind_check_session: tuple[AsyncSession, int, int],
):
    """DB-13 Finding 1 regression: mirrors ``tag_mint.py``'s step-4 mint after
    the fix (only ``ref_specs`` are upserted; ``misc_specs`` are excluded from
    ``all_specs`` entirely). Proves (a) no CHECK violation persisting the
    surviving reference entities on REAL Postgres, and (c) the surviving kinds
    land correctly."""
    session, course_id, concept_id = kind_check_session
    ref_specs = reference_solution_to_entities(_PROBLEM)
    misc_specs = misconceptions_to_entities(_MISCONCEPTIONS)
    assert misc_specs  # sanity: this fixture really is misconception-bearing

    for spec in ref_specs:  # the FIXED all_specs = ref_specs (misc excluded)
        await upsert_entity(
            session, course_id=course_id, concept_id=concept_id, spec=spec, scope_summary=None
        )
    await session.commit()

    rows = (
        (await session.execute(select(LearnerEntity).where(LearnerEntity.concept_id == concept_id)))
        .scalars()
        .all()
    )
    assert sorted(r.kind for r in rows) == sorted(spec.kind for spec in ref_specs)
    assert {"equation", "condition", "definition"}.issubset({r.kind for r in rows})


async def test_no_misconception_row_is_ever_written(
    kind_check_session: tuple[AsyncSession, int, int],
):
    """(b) After running the fixed mint path, no ``kind='misconception'`` row
    exists in ``app.learner_entities`` for this concept."""
    session, course_id, concept_id = kind_check_session
    ref_specs = reference_solution_to_entities(_PROBLEM)
    for spec in ref_specs:
        await upsert_entity(
            session, course_id=course_id, concept_id=concept_id, spec=spec, scope_summary=None
        )
    await session.commit()

    rows = (
        (
            await session.execute(
                select(LearnerEntity)
                .where(LearnerEntity.concept_id == concept_id)
                .where(LearnerEntity.kind == "misconception")
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_misconception_kind_violates_real_postgres_check(
    kind_check_session: tuple[AsyncSession, int, int],
):
    """Negative control: proves ``learner_entities__kind__check`` is REAL and
    would have rejected the PRE-fix code path (misconception ``EntitySpec``s
    folded into ``tag_and_mint``'s ``all_specs``) — the exact CHECK-constraint
    failure Finding 1 identified, masked by the aiosqlite unit suite."""
    session, course_id, concept_id = kind_check_session
    misc_specs = misconceptions_to_entities(_MISCONCEPTIONS)

    with pytest.raises(IntegrityError):
        await upsert_entity(
            session,
            course_id=course_id,
            concept_id=concept_id,
            spec=misc_specs[0],
            scope_summary=None,
        )
