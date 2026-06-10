from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_USER_ID, TEST_USER_ID_2
from apollo.persistence.models import StudentProgress
from apollo.persistence.progress_repo import apply_xp, load_progress
from database.models import Base

# Stable UUID-string ids for SQLite in-memory tests.
_UID_NEW = "a1000000-0000-4000-8000-000000000001"
_UID_1 = "a1000000-0000-4000-8000-000000000002"
_UID_2 = "a1000000-0000-4000-8000-000000000003"
_UID_3 = "a1000000-0000-4000-8000-000000000004"
_UID_4 = "a1000000-0000-4000-8000-000000000005"
_UID_5 = "a1000000-0000-4000-8000-000000000006"
_UID_6 = "a1000000-0000-4000-8000-000000000007"


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
async def test_load_progress_creates_default_row_for_new_student(db):
    sp = await load_progress(db=db, user_id=_UID_NEW)
    assert sp.user_id == _UID_NEW
    assert sp.xp_total == 0
    assert sp.level == 1

    # Row was persisted.
    row = (await db.execute(
        select(StudentProgress).where(StudentProgress.user_id == _UID_NEW)
    )).scalar_one()
    assert row.xp_total == 0


@pytest.mark.asyncio
async def test_load_progress_returns_existing_row(db):
    db.add(StudentProgress(user_id=_UID_1, xp_total=420, level=2))
    await db.commit()

    sp = await load_progress(db=db, user_id=_UID_1)
    assert sp.xp_total == 420
    assert sp.level == 2


@pytest.mark.asyncio
async def test_apply_xp_zero_delta_is_idempotent(db):
    result = await apply_xp(db=db, user_id=_UID_2, xp_delta=0)
    assert result == {
        "xp_before": 0,
        "xp_after": 0,
        "level_before": 1,
        "level_after": 1,
        "level_up": False,
    }

    # A second zero-delta call still leaves the row at (0, 1).
    result2 = await apply_xp(db=db, user_id=_UID_2, xp_delta=0)
    assert result2["xp_after"] == 0
    assert result2["level_up"] is False


@pytest.mark.asyncio
async def test_apply_xp_increments_and_returns_before_after(db):
    result = await apply_xp(db=db, user_id=_UID_3, xp_delta=90)
    assert result["xp_before"] == 0
    assert result["xp_after"] == 90
    assert result["level_before"] == 1
    assert result["level_after"] == 1
    assert result["level_up"] is False


@pytest.mark.asyncio
async def test_apply_xp_flags_level_up_when_threshold_crossed(db):
    # Seed at xp=250 (level 1). Adding 100 crosses the 300 boundary → level 2.
    db.add(StudentProgress(user_id=_UID_4, xp_total=250, level=1))
    await db.commit()

    result = await apply_xp(db=db, user_id=_UID_4, xp_delta=100)
    assert result["xp_before"] == 250
    assert result["xp_after"] == 350
    assert result["level_before"] == 1
    assert result["level_after"] == 2
    assert result["level_up"] is True

    # Row mutated in place.
    row = (await db.execute(
        select(StudentProgress).where(StudentProgress.user_id == _UID_4)
    )).scalar_one()
    assert row.xp_total == 350
    assert row.level == 2
    assert row.last_level_up_at is not None


@pytest.mark.asyncio
async def test_apply_xp_does_not_set_last_level_up_at_when_no_level_change(db):
    db.add(StudentProgress(user_id=_UID_5, xp_total=10, level=1))
    await db.commit()

    await apply_xp(db=db, user_id=_UID_5, xp_delta=50)

    row = (await db.execute(
        select(StudentProgress).where(StudentProgress.user_id == _UID_5)
    )).scalar_one()
    assert row.last_level_up_at is None


@pytest.mark.asyncio
async def test_apply_xp_rejects_negative_delta(db):
    with pytest.raises(ValueError):
        await apply_xp(db=db, user_id=_UID_6, xp_delta=-5)
