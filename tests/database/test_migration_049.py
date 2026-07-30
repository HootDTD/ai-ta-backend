"""Real-Postgres contract test for the auth.users identity-grant migration 049.

Proves the three behaviors the design spec calls out:
  * guarded no-op when the ``app_runtime`` role is absent (local reset order);
  * guarded no-op when ``auth.users`` is absent (Testcontainers / local Docker);
  * the full path grants ``app_runtime`` USAGE on schema ``auth`` and
    COLUMN-level SELECT on exactly ``(id, email, raw_user_meta_data)`` — every
    other column (e.g. ``encrypted_password``) stays unreadable — and is
    idempotent on re-run.

Each scenario is built and torn down inside a rolled-back transaction, so the
cluster-global ``app_runtime`` role never leaks between tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2] / "database/migrations/049_auth_users_identity_grant.sql"
)
DB_NAME = "auth_users_identity_grant_migration"


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def identity_grant_dsn(_pg_url: str):
    base = make_url(_pg_url).database
    assert base
    admin_dsn, test_dsn = _dsn(_pg_url, base), _dsn(_pg_url, DB_NAME)

    async def setup():
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}"')
            await admin.execute(f'CREATE DATABASE "{DB_NAME}"')
        finally:
            await admin.close()

    asyncio.run(setup())
    return test_dsn


@pytest_asyncio.fixture
async def identity_grant_conn(identity_grant_dsn):
    conn = await asyncpg.connect(identity_grant_dsn)
    transaction = conn.transaction()
    await transaction.start()
    try:
        yield conn
    finally:
        await transaction.rollback()
        await conn.close()


_SQL = MIGRATION.read_text(encoding="utf-8")


async def _create_app_runtime(conn) -> None:
    await conn.execute("CREATE ROLE app_runtime NOLOGIN NOBYPASSRLS")


async def _create_auth_users(conn) -> None:
    await conn.execute("CREATE SCHEMA auth")
    await conn.execute(
        "CREATE TABLE auth.users ("
        "  id uuid PRIMARY KEY, email text, raw_user_meta_data jsonb,"
        "  encrypted_password text)"
    )


async def test_noop_when_role_absent(identity_grant_conn):
    # No app_runtime role and no auth schema: the guard must skip cleanly (twice).
    await identity_grant_conn.execute(_SQL)
    await identity_grant_conn.execute(_SQL)
    # Nothing to assert beyond "did not raise" — there is no role to grant to.
    assert await identity_grant_conn.fetchval("SELECT to_regclass('auth.users')") is None


async def test_noop_when_auth_users_absent(identity_grant_conn):
    # Role present but auth.users missing (the Testcontainers / local shape).
    await _create_app_runtime(identity_grant_conn)
    await identity_grant_conn.execute(_SQL)
    await identity_grant_conn.execute(_SQL)
    # The guard skipped before any GRANT: no auth schema, role untouched.
    assert await identity_grant_conn.fetchval("SELECT to_regclass('auth.users')") is None
    assert (
        await identity_grant_conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime'")
        == 1
    )


async def test_full_path_grants_only_the_three_columns_and_is_idempotent(identity_grant_conn):
    await _create_app_runtime(identity_grant_conn)
    await _create_auth_users(identity_grant_conn)

    # Run twice — GRANT is idempotent.
    await identity_grant_conn.execute(_SQL)
    await identity_grant_conn.execute(_SQL)

    assert (
        await identity_grant_conn.fetchval(
            "SELECT has_schema_privilege('app_runtime', 'auth', 'USAGE')"
        )
        is True
    )
    for column in ("id", "email", "raw_user_meta_data"):
        assert (
            await identity_grant_conn.fetchval(
                "SELECT has_column_privilege('app_runtime', 'auth.users', $1, 'SELECT')", column
            )
            is True
        ), column
    # The sensitive column was NOT granted (column-level scoping holds).
    assert (
        await identity_grant_conn.fetchval(
            "SELECT has_column_privilege('app_runtime', 'auth.users', 'encrypted_password', 'SELECT')"
        )
        is False
    )
