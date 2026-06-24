"""Create the LOCAL dev schema (pgvector extension + every ORM table) against
the local Supabase Postgres.

This mirrors exactly what tests/conftest.py does for the throwaway test
container: `CREATE EXTENSION vector` + `Base.metadata.create_all`. Because the
whole ORM surface lives in just two modules (database/models.py and
apollo/persistence/models.py), importing both registers every table on the one
shared Base.metadata before create_all runs.

For a LOCAL FUNCTIONAL run this is the right tool (it builds the current
schema the code expects). It deliberately does NOT replay the numbered SQL
migrations 001..030 -- that sequence (and the 023 collision) is a separate
concern for rehearsing against the TEST Supabase project.

Refuses to run unless SUPABASE_DB_URL points at localhost/127.0.0.1, so it can
never create_all against a cloud project by accident. Idempotent / re-runnable.
"""
from __future__ import annotations

import asyncio
import os
import sys

# Make the repo root importable when run as `python scripts/bootstrap_local_db.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _main() -> None:
    url = (os.getenv("SUPABASE_DB_URL") or "").strip()
    if not url:
        sys.exit("SUPABASE_DB_URL is not set. Point it at the LOCAL Supabase stack.")
    if "127.0.0.1" not in url and "localhost" not in url:
        sys.exit(
            f"Refusing to run: SUPABASE_DB_URL does not look local: {url!r}\n"
            "This script only targets a local Postgres (127.0.0.1 / localhost)."
        )

    # Register ALL ORM tables on the single shared Base.metadata.
    from database.models import Base
    import apollo.persistence.models  # noqa: F401 - import registers apollo tables

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def _setup() -> None:
        # No vector codec on the setup engine: the first connection runs
        # CREATE EXTENSION, so the `vector` type doesn't exist at connect time.
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_setup())
    table_count = len(Base.metadata.tables)
    print(f"OK: ensured {table_count} tables + pgvector on {url}")


if __name__ == "__main__":
    _main()
