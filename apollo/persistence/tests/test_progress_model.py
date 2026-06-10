from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_USER_ID, TEST_USER_ID_2
from apollo.persistence.models import StudentProgress
from database.models import Base


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=[StudentProgress.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_student_progress_defaults(session):
    sp = StudentProgress(user_id=TEST_USER_ID)
    session.add(sp)
    await session.commit()
    await session.refresh(sp)

    # Defaults match the spec.
    assert sp.xp_total == 0
    assert sp.level == 1
    assert sp.last_level_up_at is None


@pytest.mark.asyncio
async def test_student_progress_round_trip(session):
    session.add(StudentProgress(user_id=TEST_USER_ID_2, xp_total=1700, level=4))
    await session.commit()

    loaded = (await session.execute(
        select(StudentProgress).where(StudentProgress.user_id == TEST_USER_ID_2)
    )).scalar_one()
    assert loaded.xp_total == 1700
    assert loaded.level == 4


@pytest.mark.asyncio
async def test_student_progress_student_id_is_primary_key(session):
    session.add(StudentProgress(user_id=TEST_USER_ID))
    await session.commit()

    # Inserting a second row with the same user_id should fail.
    session.add(StudentProgress(user_id=TEST_USER_ID))
    with pytest.raises(Exception):
        await session.commit()
