"""Fast unit test for the WU-3A seeder edit: _upsert_subject now attributes a
subject to the bootstrap course (MIN(aita_search_spaces.id)) because
apollo_subjects.search_space_id is NOT NULL (migration 026, isolation invariant
§1.4).

The seeder is a script with no DB harness of its own; this exercises the one
changed function on in-memory SQLite so the changed lines are covered (the real
end-to-end seeding runs against Postgres at deploy time). Covers both the create
path (new subject gets the bootstrap space_id) and the update path (existing row
keeps its id).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, MetaData, Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.persistence.models import Subject
from database.models import Base
from scripts.seed_apollo_concept_registry import _upsert_subject


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    # Minimal aita_search_spaces stub so MIN(id) resolves; Subject.__table__ for
    # the upsert target. SQLite does not enforce the FK, matching repo convention.
    spaces = Table(
        "aita_search_spaces",
        MetaData(),
        Column("id", Integer, primary_key=True),
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: spaces.create(sc))
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=[Subject.__table__]))
        await conn.exec_driver_sql("INSERT INTO aita_search_spaces (id) VALUES (7), (3)")
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_subject_create_uses_bootstrap_min_space(db: AsyncSession):
    subject_id = await _upsert_subject(db, "fluid_mechanics")
    await db.commit()

    row = (await db.execute(select(Subject).where(Subject.id == subject_id))).scalar_one()
    assert row.slug == "fluid_mechanics"
    assert row.display_name == "Fluid Mechanics"
    # MIN(id) over {7, 3} -> 3 (the bootstrap/pilot course).
    assert row.search_space_id == 3


@pytest.mark.asyncio
async def test_upsert_subject_update_path_keeps_id(db: AsyncSession):
    first_id = await _upsert_subject(db, "thermo")
    await db.commit()
    # Re-upsert the same slug hits the update branch and returns the same id.
    again_id = await _upsert_subject(db, "thermo")
    await db.commit()
    assert again_id == first_id
    rows = (await db.execute(select(Subject).where(Subject.slug == "thermo"))).scalars().all()
    assert len(rows) == 1
