from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.handlers.progress import handle_get_progress
from apollo.persistence.models import StudentProgress
from database.models import Base


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(
            sc, tables=[StudentProgress.__table__]
        ))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_progress_unknown_student_returns_defaults(db):
    payload = await handle_get_progress(db=db, student_id="new-student")
    assert payload == {
        "student_id": "new-student",
        "xp_total": 0,
        "level": 1,
        "title": "Apollo Apprentice",
        "next_tier_threshold": 300,
    }


@pytest.mark.asyncio
async def test_get_progress_known_student_returns_stored(db):
    db.add(StudentProgress(student_id="veteran", xp_total=1800, level=4))
    await db.commit()

    payload = await handle_get_progress(db=db, student_id="veteran")
    assert payload == {
        "student_id": "veteran",
        "xp_total": 1800,
        "level": 4,
        "title": "Apollo Sage",
        "next_tier_threshold": 3000,
    }


@pytest.mark.asyncio
async def test_get_progress_max_level_student_has_null_next_threshold(db):
    db.add(StudentProgress(student_id="max", xp_total=3500, level=5))
    await db.commit()

    payload = await handle_get_progress(db=db, student_id="max")
    assert payload["level"] == 5
    assert payload["next_tier_threshold"] is None
