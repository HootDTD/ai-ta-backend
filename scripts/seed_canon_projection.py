"""WU-3C1 — :Canon projection rebuild CLI.

Operational rebuild primitive: ":Canon is rebuildable from Postgres at any
time" (§2). Reads Layer-1 ``apollo_kg_entities`` (seeded by WU-3B) and
idempotently ``MERGE``s ``:Canon`` Neo4j nodes via
:func:`apollo.knowledge_graph.canon_projection.project_canon`. SAFE to re-run —
the MERGE is idempotent (a second run yields the same node count, no
duplicates). Mirrors ``scripts/seed_apollo_learner_model.py``.

The projection is ALWAYS scoped (the isolation invariant forbids a course-blind
seed). When neither ``--concept-id`` nor ``--search-space-id`` is passed, the
CLI resolves a default ``search_space_id = MIN(aita_search_spaces.id)`` (the
WU-3B bootstrap convenience) and passes it EXPLICITLY into the seeder, so the
unscoped-refusal in ``load_entity_specs`` stays intact.

Neo4j credentials are read from env only (``Neo4jClient.from_env()`` —
NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD / NEO4J_DATABASE); never hardcoded.

Usage::

    python -m scripts.seed_canon_projection [--database-url URL]
        [--search-space-id N] [--concept-id N] [-v]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Make the package importable when run as `python -m scripts....`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apollo.knowledge_graph.canon_projection import (  # noqa: E402
    CanonProjectionResult,
    project_canon,
)
from apollo.persistence.neo4j_client import Neo4jClient  # noqa: E402

_LOG = logging.getLogger(__name__)


async def run(
    database_url: str,
    *,
    search_space_id: int | None = None,
    concept_id: int | None = None,
) -> CanonProjectionResult:
    """Build an async engine + a Neo4jClient, project :Canon for one scope,
    and close both. When neither scope arg is given, resolve a default
    ``search_space_id = MIN(aita_search_spaces.id)`` and pass it explicitly."""
    engine = create_async_engine(database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    neo = Neo4jClient.from_env()
    try:
        async with Session() as session:
            if concept_id is None and search_space_id is None:
                search_space_id = (
                    await session.execute(text("SELECT MIN(id) FROM aita_search_spaces"))
                ).scalar_one_or_none()
                if search_space_id is None:
                    raise RuntimeError(
                        "no aita_search_spaces rows — seed a course before projecting :Canon"
                    )
            result = await project_canon(
                session,
                neo,
                search_space_id=search_space_id,
                concept_id=concept_id,
            )
        _LOG.info(
            "canon_projection_done merged=%s entity_count=%s",
            result.merged,
            result.entity_count,
        )
        return result
    finally:
        await neo.close()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=None,
        help="async PostgreSQL URL (defaults to env DATABASE_URL)",
    )
    parser.add_argument(
        "--search-space-id",
        type=int,
        default=None,
        help="course id (defaults to MIN(aita_search_spaces.id) when no scope given)",
    )
    parser.add_argument(
        "--concept-id",
        type=int,
        default=None,
        help="restrict the projection to a single concept",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db_url = args.database_url or os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: pass --database-url or set DATABASE_URL", file=sys.stderr)
        return 2

    result = asyncio.run(
        run(
            db_url,
            search_space_id=args.search_space_id,
            concept_id=args.concept_id,
        )
    )
    print(f"projected :Canon: merged={result.merged} entity_count={result.entity_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
