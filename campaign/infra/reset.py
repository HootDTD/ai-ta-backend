"""Reset the campaign's local Postgres + Neo4j back to an empty, migrated state.

Used between campaign runs (tune-phase iterations, or before a fresh gate
run) so each run starts from nothing. Only ever points at the local stack —
callers are responsible for passing local DSN/URIs; this module has no
knowledge of remote Supabase or Neo4j Aura credentials.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from campaign.infra.apply_migrations import (
    AsyncpgConnLike,
    ConnectFn,
    _default_connect,
    apply_all,
    bootstrap_baseline,
)

_LOG = logging.getLogger(__name__)

_DROP_AND_RECREATE_SCHEMA_SQL = "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

Neo4jRunFn = Callable[[str, str, tuple[str, str], str], Awaitable[None]]


async def reset_postgres(
    dsn: str,
    *,
    migrations_dir: Path | str = "database/migrations",
    connect: ConnectFn = _default_connect,
) -> list[str]:
    """Drop and recreate the ``public`` schema, re-bootstrap the ORM baseline,
    then re-apply every migration.

    Returns the list of migration filenames applied (all of them, since the
    tracking table is wiped along with the schema drop).
    """
    conn: AsyncpgConnLike = await connect(dsn)
    try:
        await conn.execute(_DROP_AND_RECREATE_SCHEMA_SQL)
        _LOG.info("dropped and recreated public schema on %s", dsn)
    finally:
        close = getattr(conn, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    await bootstrap_baseline(dsn)
    return await apply_all(dsn, migrations_dir, connect=connect)


async def _default_neo4j_wipe(
    uri: str, database: str, auth: tuple[str, str]
) -> None:  # pragma: no cover - thin driver passthrough
    from neo4j import AsyncGraphDatabase

    driver = AsyncGraphDatabase.driver(uri, auth=auth)
    try:
        async with driver.session(database=database) as session:
            await session.run("MATCH (n) DETACH DELETE n")
    finally:
        await driver.close()


async def reset_neo4j(
    uri: str,
    auth: tuple[str, str],
    *,
    database: str = "neo4j",
    wipe: Callable[[str, str, tuple[str, str]], Awaitable[None]] = _default_neo4j_wipe,
) -> None:
    """Delete every node/relationship in the campaign's Neo4j database."""
    await wipe(uri, database, auth)
    _LOG.info("wiped neo4j database %r at %s", database, uri)


async def reset_all(
    *,
    pg_dsn: str,
    neo4j_uri: str,
    neo4j_auth: tuple[str, str],
    migrations_dir: Path | str = "database/migrations",
    pg_connect: ConnectFn = _default_connect,
    neo4j_wipe: Callable[[str, str, tuple[str, str]], Awaitable[None]] = _default_neo4j_wipe,
) -> list[str]:
    """Reset both stores. Returns the list of Postgres migrations (re-)applied."""
    applied = await reset_postgres(pg_dsn, migrations_dir=migrations_dir, connect=pg_connect)
    await reset_neo4j(neo4j_uri, neo4j_auth, wipe=neo4j_wipe)
    return applied


def _main() -> int:  # pragma: no cover - CLI shim
    import argparse
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("CAMPAIGN_DSN"))
    parser.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI"))
    parser.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USERNAME", "neo4j"))
    parser.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    parser.add_argument("--dir", default="database/migrations")
    args = parser.parse_args()
    if not args.dsn or not args.neo4j_uri:
        parser.error("--dsn and --neo4j-uri (or $CAMPAIGN_DSN / $NEO4J_URI) are required")

    applied = asyncio.run(
        reset_all(
            pg_dsn=args.dsn,
            neo4j_uri=args.neo4j_uri,
            neo4j_auth=(args.neo4j_user, args.neo4j_password),
            migrations_dir=args.dir,
        )
    )
    print(f"reset complete: {len(applied)} migration(s) re-applied")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
