from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_USER_ID, TEST_USER_ID_2
from apollo.handlers.progress import handle_get_progress
from apollo.persistence.models import StudentProgress
from database.models import Base

COURSE_ID = 101


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(sc, tables=[StudentProgress.__table__])
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_progress_unknown_student_returns_defaults(db):
    payload = await handle_get_progress(
        db=db, user_id=TEST_USER_ID, search_space_id=COURSE_ID
    )
    assert payload == {
        "user_id": TEST_USER_ID,
        "search_space_id": COURSE_ID,
        "xp_total": 0,
        "level": 1,
        "title": "Apollo Apprentice",
        "next_tier_threshold": 300,
    }


@pytest.mark.asyncio
async def test_get_progress_known_student_returns_stored(db):
    db.add(StudentProgress(user_id=TEST_USER_ID, course_id=COURSE_ID, xp_total=1800, level=4))
    await db.commit()

    payload = await handle_get_progress(
        db=db, user_id=TEST_USER_ID, search_space_id=COURSE_ID
    )
    assert payload == {
        "user_id": TEST_USER_ID,
        "search_space_id": COURSE_ID,
        "xp_total": 1800,
        "level": 4,
        "title": "Apollo Sage",
        "next_tier_threshold": 3000,
    }


@pytest.mark.asyncio
async def test_get_progress_max_level_student_has_null_next_threshold(db):
    db.add(StudentProgress(user_id=TEST_USER_ID_2, course_id=COURSE_ID, xp_total=3500, level=5))
    await db.commit()

    payload = await handle_get_progress(
        db=db, user_id=TEST_USER_ID_2, search_space_id=COURSE_ID
    )
    assert payload["level"] == 5
    assert payload["next_tier_threshold"] is None
