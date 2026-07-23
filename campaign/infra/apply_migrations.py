"""Apply every ``database/migrations/*.sql`` file, in order, to a local DB.

Used by the campaign's local Supabase stack (Task C1). This is deliberately
NOT wired to any remote-Supabase codepath — ``apply_all`` takes a plain
asyncpg DSN and is only ever pointed at the local `supabase start` Postgres
(or the docker-compose Postgres fallback) by ``campaign/infra`` callers.

The pure ordering/parsing logic (``migration_files``) has no I/O and is unit
tested without Docker; ``apply_all``/``_apply_one`` do the real asyncpg work
and are exercised manually against the local stack (Task C1 Step 2) plus a
mocked-connection unit test.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_LOG = logging.getLogger(__name__)

# Migration filenames are `<number>_<slug>.sql`. Numbers are meant to be
# unique but 023 was double-minted historically
# (`023_apollo_auth_scoping.sql` + `023_chunks_halfvec_hnsw.sql`, both already
# applied independently in prod history) — that specific pair is an allowed,
# known duplicate. Any OTHER duplicate number is a real authoring error and
# must fail loudly rather than silently apply one and skip the other.
KNOWN_DUP_NUMBERS: frozenset[int] = frozenset({23})

_NAME_RE = re.compile(r"^(\d+)_.+\.sql$")

_TRACKING_TABLE = "_campaign_migrations"

_ASYNC_SCHEME_PREFIX = "postgresql+asyncpg://"
_PLAIN_SCHEME_PREFIX = "postgresql://"


def to_asyncpg_dsn(dsn: str) -> str:
    """Strip the SQLAlchemy ``+asyncpg`` driver marker for raw ``asyncpg.connect``.

    Campaign config (``env.campaign.example``) uses the SQLAlchemy-style DSN
    (``postgresql+asyncpg://...``) everywhere for consistency with the
    backend's own ``SUPABASE_DB_URL``/``DATABASE_URL`` convention. The raw
    ``asyncpg`` driver used by this module's tracking-table logic only
    understands the plain ``postgresql://`` scheme.
    """
    if dsn.startswith(_ASYNC_SCHEME_PREFIX):
        return _PLAIN_SCHEME_PREFIX + dsn[len(_ASYNC_SCHEME_PREFIX) :]
    return dsn


def to_sqlalchemy_dsn(dsn: str) -> str:
    """Add the ``+asyncpg`` driver marker for SQLAlchemy's async engine."""
    if dsn.startswith(_PLAIN_SCHEME_PREFIX) and not dsn.startswith(_ASYNC_SCHEME_PREFIX):
        return _ASYNC_SCHEME_PREFIX + dsn[len(_PLAIN_SCHEME_PREFIX) :]
    return dsn


_CREATE_TRACKING_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TRACKING_TABLE} (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationOrderError(ValueError):
    """Raised when migration filenames can't be safely ordered/applied."""


@dataclass(frozen=True)
class ParsedMigration:
    number: int
    name: str
    path: Path


def _parse(path: Path) -> ParsedMigration:
    match = _NAME_RE.match(path.name)
    if not match:
        raise MigrationOrderError(
            f"migration filename does not match '<number>_<slug>.sql': {path.name}"
        )
    return ParsedMigration(number=int(match.group(1)), name=path.name, path=path)


def migration_files(directory: Path | str) -> list[Path]:
    """Return every ``*.sql`` migration in ``directory``, sorted for apply order.

    Sort key is ``(number, name)`` — numeric primary, filename secondary for
    stability when two files share a number. Duplicate numbers are rejected
    UNLESS the number is in ``KNOWN_DUP_NUMBERS`` (currently just 23).
    """
    directory = Path(directory)
    sql_paths = sorted(p for p in directory.glob("*.sql") if p.is_file())
    parsed = [_parse(p) for p in sql_paths]

    by_number: dict[int, list[ParsedMigration]] = {}
    for item in parsed:
        by_number.setdefault(item.number, []).append(item)

    for number, items in by_number.items():
        if len(items) > 1 and number not in KNOWN_DUP_NUMBERS:
            names = ", ".join(sorted(i.name for i in items))
            raise MigrationOrderError(
                f"duplicate migration number {number} not in KNOWN_DUP_NUMBERS: {names}"
            )

    ordered = sorted(parsed, key=lambda item: (item.number, item.name))
    return [item.path for item in ordered]


# ---------------------------------------------------------------------------
# asyncpg application. Connection creation is injected so unit tests can pass
# a fake connection factory without a real DB.
# ---------------------------------------------------------------------------


class AsyncpgConnLike(Protocol):
    """Structural subset of ``asyncpg.Connection`` this module relies on.

    Kept narrow (execute/fetch/transaction) so unit tests can pass a plain
    mock/fake without inheriting from asyncpg's real connection class.
    """

    async def execute(self, query: str, *args: Any) -> str: ...
    async def fetch(self, query: str, *args: Any) -> Sequence[Any]: ...
    def transaction(self) -> Any: ...


ConnectFn = Callable[[str], Awaitable[AsyncpgConnLike]]


async def _default_connect(
    dsn: str,
) -> AsyncpgConnLike:  # pragma: no cover - thin asyncpg passthrough
    import asyncpg

    return await asyncpg.connect(to_asyncpg_dsn(dsn))


async def _fetch_applied(conn: AsyncpgConnLike) -> set[str]:
    rows = await conn.fetch(f"SELECT name FROM {_TRACKING_TABLE}")
    return {row["name"] for row in rows}


async def _apply_one(conn: AsyncpgConnLike, migration: Path) -> None:
    sql = migration.read_text(encoding="utf-8")
    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute(
            f"INSERT INTO {_TRACKING_TABLE} (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
            migration.name,
        )
    _LOG.info("applied migration %s", migration.name)


async def bootstrap_baseline(dsn: str) -> None:
    """Create the pgvector extension + the full current SQLAlchemy schema.

    ``database/migrations/`` only covers 004-onward: the base tables
    (``app.courses``, ``app.documents``, ...) have never had a numbered
    migration — production/CI bootstrap them via ``Base.metadata.create_all``
    (see ``tests/conftest.py``'s ``_pg_url`` fixture) and every migration from
    004 on assumes that baseline already exists. Every migration file uses
    guarded DDL (``IF NOT EXISTS`` / ``DROP ... IF EXISTS``), so replaying them
    on top of the CURRENT (not historical) ORM schema is safe: columns/tables
    the ORM already has are no-ops, and DROP-COLUMN migrations for columns the
    current ORM never created are no-ops too.

    Uses a SQLAlchemy asyncpg engine (not the raw ``asyncpg`` connections the
    rest of this module uses) because ``Base.metadata.create_all`` needs the
    ORM's engine machinery. Callers pass a plain ``postgresql://`` DSN; this
    function adapts it to the asyncpg SQLAlchemy URL internally.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from database.models import Base

    engine = create_async_engine(to_sqlalchemy_dsn(dsn), poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()
    _LOG.info("bootstrapped baseline SQLAlchemy schema on %s", dsn)


async def apply_all(
    dsn: str,
    directory: Path | str = "database/migrations",
    *,
    connect: ConnectFn = _default_connect,
) -> list[str]:
    """Apply every not-yet-applied migration in ``directory`` to ``dsn``.

    Idempotent: migrations already recorded in ``_campaign_migrations`` are
    skipped. Returns the list of migration filenames actually applied this
    run (empty on a fully-up-to-date DB).
    """
    files = migration_files(directory)
    conn = await connect(dsn)
    try:
        await conn.execute(_CREATE_TRACKING_SQL)
        applied = await _fetch_applied(conn)
        newly_applied: list[str] = []
        for path in files:
            if path.name in applied:
                continue
            await _apply_one(conn, path)
            newly_applied.append(path.name)
        return newly_applied
    finally:
        close = getattr(conn, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result


def _main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI shim
    import argparse
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("CAMPAIGN_DSN") or os.environ.get("SUPABASE_DB_URL"),
        help="asyncpg-compatible DSN (default: $CAMPAIGN_DSN or $SUPABASE_DB_URL)",
    )
    parser.add_argument("--dir", default="database/migrations")
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="skip the Base.metadata.create_all baseline step (DB already bootstrapped)",
    )
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("--dsn (or $CAMPAIGN_DSN / $SUPABASE_DB_URL) is required")

    async def _run() -> list[str]:
        if not args.skip_baseline:
            await bootstrap_baseline(args.dsn)
        return await apply_all(args.dsn, args.dir)

    applied = asyncio.run(_run())
    if applied:
        print(f"applied {len(applied)} migration(s):")
        for name in applied:
            print(f"  {name}")
    else:
        print("no new migrations to apply")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
