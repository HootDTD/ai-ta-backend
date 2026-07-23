"""Registry seeder coverage for the subjects-into-concepts fold."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.persistence.models import Concept
from database.models import Base
from scripts.seed_apollo_concept_registry import _upsert_concept


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    courses = Table("courses", MetaData(), Column("id", Integer, primary_key=True))
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: courses.create(sc))
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=[Concept.__table__]))
        await conn.exec_driver_sql("INSERT INTO courses (id) VALUES (3)")
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


def _payloads() -> dict:
    return {
        "canonical_symbols": {"symbols": ["x"], "description": {"x": "value"}},
        "normalization_map": {"X": "x"},
        "parser_prompt_template": "Parse",
        "solver_hints": {"constants": {}},
        "forbidden_named_laws": {"named_laws": ["shortcut"]},
        "concept_dag": {"ignored": ["normalized elsewhere"]},
    }


@pytest.mark.asyncio
async def test_upsert_concept_folds_subject_and_normalizes_fields(db: AsyncSession):
    concept_id = await _upsert_concept(
        db,
        course_id=3,
        subject_slug="fluid_mechanics",
        slug="bernoulli",
        payloads=_payloads(),
    )
    row = await db.get(Concept, concept_id)
    assert row is not None
    assert row.course_id == 3
    assert row.subject_slug == "fluid_mechanics"
    assert row.subject_display_name == "Fluid Mechanics"
    assert row.canonical_symbols == ["x"]
    assert row.symbol_metadata == {"description": {"x": "value"}}
    assert row.solver_config == {"constants": {}}
    assert row.forbidden_named_laws == ["shortcut"]
    assert "concept_dag" not in row.__table__.columns


@pytest.mark.asyncio
async def test_upsert_concept_is_idempotent(db: AsyncSession):
    first = await _upsert_concept(
        db, course_id=3, subject_slug="thermo", slug="energy", payloads=_payloads()
    )
    second = await _upsert_concept(
        db, course_id=3, subject_slug="thermo", slug="energy", payloads=_payloads()
    )
    assert second == first
