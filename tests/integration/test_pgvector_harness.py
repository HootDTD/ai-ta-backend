"""Phase 1 exit criterion #1: pgvector distance ordering on known vectors.

Proves the real-Postgres harness works end to end: a `Vector(3072)` column
round-trips through asyncpg, and pgvector's distance operators order results
the way the retrieval pipeline relies on. Runs only when Docker is available
(skips cleanly otherwise via the `db_session` -> `_pg_url` fixture chain).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from database.models import AITADocument
from tests.factories import AITADocumentFactory, SearchSpaceFactory, persist
from tests.fakes import one_hot_embedding

pytestmark = pytest.mark.integration


async def _seed_docs(db_session, axes: list[int]):
    space = await persist(db_session, SearchSpaceFactory.build())
    docs = []
    for axis in axes:
        doc = await persist(
            db_session,
            AITADocumentFactory.build(
                search_space_id=space.id,
                title=f"axis-{axis}",
                content_hash=f"axis-{axis}",
                embedding=one_hot_embedding(axis),
            ),
        )
        docs.append(doc)
    await db_session.flush()
    return space, docs


async def test_cosine_distance_orders_nearest_first(db_session):
    # Three docs on orthogonal axes 0, 1, 2.
    await _seed_docs(db_session, axes=[0, 1, 2])

    query = one_hot_embedding(1)  # identical to the axis-1 doc
    rows = (
        (
            await db_session.execute(
                select(AITADocument.title).order_by(AITADocument.embedding.cosine_distance(query))
            )
        )
        .scalars()
        .all()
    )

    # axis-1 is identical to the query (distance 0); the others are orthogonal.
    assert rows[0] == "axis-1"
    assert set(rows) == {"axis-0", "axis-1", "axis-2"}


async def test_cosine_distance_values_are_correct(db_session):
    await _seed_docs(db_session, axes=[0, 1])
    query = one_hot_embedding(0)

    result = await db_session.execute(
        select(
            AITADocument.title,
            AITADocument.embedding.cosine_distance(query).label("dist"),
        ).order_by("dist")
    )
    by_title = {title: dist for title, dist in result.all()}

    # Identical vector -> 0; orthogonal one-hot -> 1.
    assert by_title["axis-0"] == pytest.approx(0.0, abs=1e-6)
    assert by_title["axis-1"] == pytest.approx(1.0, abs=1e-6)


async def test_l2_distance_orders_nearest_first(db_session):
    await _seed_docs(db_session, axes=[0, 1, 2])
    query = one_hot_embedding(2)

    nearest = (
        await db_session.execute(
            select(AITADocument.title).order_by(AITADocument.embedding.l2_distance(query)).limit(1)
        )
    ).scalar_one()

    assert nearest == "axis-2"
