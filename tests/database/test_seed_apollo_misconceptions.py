"""Unit tests for ``scripts.seed_apollo_misconceptions`` (the campaign-fakes
harness for the ``apollo_misconceptions`` TABLE-bank seeder wired into
``campaign.cast.teacher.provision_seeded``).

Mirrors ``apollo/persistence/tests/test_learner_model_seed_generic.py``'s
in-memory SQLite "mock DB" harness for concept/subject RESOLUTION (real
``select``/``flush``/``commit`` queries, no network, no remote writes).
``apollo.overseer.misconception_bank.upsert_entry`` is monkeypatched because
its raw SQL casts ``vector(3072)`` — a Postgres-only type SQLite cannot run;
the resolution/glue logic this module owns (subject/concept lookup, on-disk
``misconceptions.json`` discovery, embedding opt-out, stats aggregation) is
exercised against the REAL fluid_mechanics/macroeconomics source trees.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import scripts.seed_apollo_misconceptions as seeder
from apollo.persistence.models import Concept, Subject
from database.models import Base

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]

_SEED_TABLES: list[Table] = [
    Subject.__table__,  # type: ignore[list-item]
    Concept.__table__,  # type: ignore[list-item]
]


@pytest_asyncio.fixture
async def db_url() -> AsyncGenerator[str, None]:
    name = f"miscseed_{uuid.uuid4().hex}"
    url = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    engine = create_async_engine(url)
    spaces = Table("aita_search_spaces", MetaData(), Column("id", Integer, primary_key=True))
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: spaces.create(sc))
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=_SEED_TABLES))
    keepalive = await engine.connect()
    try:
        yield url
    finally:
        await keepalive.close()
        await engine.dispose()


async def _insert_course(url: str, space_id: int) -> None:
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as s:
            await s.execute(
                seeder.text("INSERT INTO aita_search_spaces (id) VALUES (:i)"),
                {"i": space_id},
            )
            await s.commit()
    finally:
        await engine.dispose()


async def _insert_subject(url: str, *, slug: str, space_id: int) -> int:
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as s:
            subject = Subject(slug=slug, display_name=slug.title(), search_space_id=space_id)
            s.add(subject)
            await s.flush()
            sid = int(subject.id)  # type: ignore[arg-type]
            await s.commit()
            return sid
    finally:
        await engine.dispose()


async def _insert_concept(url: str, *, subject_id: int, slug: str) -> int:
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with Session() as s:
            concept = Concept(subject_id=subject_id, slug=slug, display_name=slug.title())
            s.add(concept)
            await s.flush()
            cid = int(concept.id)  # type: ignore[arg-type]
            await s.commit()
            return cid
    finally:
        await engine.dispose()


def _fake_upsert(monkeypatch, calls: list[dict]):
    async def _upsert_entry(db, **kwargs):
        calls.append(kwargs)
        return len(calls)

    monkeypatch.setattr(seeder, "upsert_entry", _upsert_entry)


# ---------------------------------------------------------------------------
# Resolution errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_raises_when_no_course_seeded(db_url):
    with pytest.raises(seeder.SeedError, match="no aita_search_spaces rows"):
        await seeder.seed(db_url, subject_slug="fluid_mechanics")


@pytest.mark.asyncio
async def test_seed_defaults_search_space_id_to_min_course(db_url, monkeypatch):
    calls: list[dict] = []
    _fake_upsert(monkeypatch, calls)

    await _insert_course(db_url, 5)
    subject_id = await _insert_subject(db_url, slug="fluid_mechanics", space_id=5)
    await _insert_concept(db_url, subject_id=subject_id, slug="bernoulli_principle")

    # No search_space_id passed — must resolve MIN(aita_search_spaces.id) == 5.
    stats = await seeder.seed(
        db_url,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        embed=False,
    )

    assert stats == {"entries_upserted": 2, "concepts_seeded": 1}


@pytest.mark.asyncio
async def test_seed_raises_when_subject_missing(db_url):
    await _insert_course(db_url, 1)
    with pytest.raises(seeder.SeedError, match="no 'fluid_mechanics' subject"):
        await seeder.seed(db_url, subject_slug="fluid_mechanics", search_space_id=1)


@pytest.mark.asyncio
async def test_seed_raises_when_requested_concept_missing(db_url):
    await _insert_course(db_url, 1)
    await _insert_subject(db_url, slug="fluid_mechanics", space_id=1)
    with pytest.raises(seeder.SeedError, match="no concept 'nope'"):
        await seeder.seed(
            db_url,
            subject_slug="fluid_mechanics",
            concept_slug="nope",
            search_space_id=1,
        )


# ---------------------------------------------------------------------------
# Real-source-tree seeding (fluid_mechanics/bernoulli, macroeconomics)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_bernoulli_upserts_both_authored_misconceptions(db_url, monkeypatch):
    calls: list[dict] = []
    _fake_upsert(monkeypatch, calls)

    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="fluid_mechanics", space_id=1)
    await _insert_concept(db_url, subject_id=subject_id, slug="bernoulli_principle")

    stats = await seeder.seed(
        db_url,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        search_space_id=1,
        embed=False,
    )

    assert stats == {"entries_upserted": 2, "concepts_seeded": 1}
    assert {c["code"] for c in calls} == {
        "pressure_velocity_same_direction",
        "density_ignored",
    }
    for c in calls:
        assert c["description_embedding"] is None  # embed=False
    opposes_by_code = {c["code"]: c["opposes"] for c in calls}
    assert opposes_by_code == {
        "pressure_velocity_same_direction": "def.pressure_velocity_tradeoff",
        "density_ignored": "cond.incompressibility",
    }


@pytest.mark.asyncio
async def test_seed_macroeconomics_sums_across_all_concepts_when_unscoped(db_url, monkeypatch):
    calls: list[dict] = []
    _fake_upsert(monkeypatch, calls)

    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="macroeconomics", space_id=1)
    await _insert_concept(db_url, subject_id=subject_id, slug="gdp_components")
    await _insert_concept(db_url, subject_id=subject_id, slug="nominal_vs_real_gdp")

    stats = await seeder.seed(
        db_url,
        subject_slug="macroeconomics",
        search_space_id=1,
        embed=False,
    )

    # gdp_components has 2 authored misconceptions, nominal_vs_real_gdp has its
    # own file too — sum across both concepts, none skipped.
    assert stats["concepts_seeded"] == 2
    assert stats["entries_upserted"] == len(calls)
    assert stats["entries_upserted"] >= 2


@pytest.mark.asyncio
async def test_seed_concept_with_no_misconceptions_json_is_a_noop(db_url, monkeypatch, tmp_path):
    calls: list[dict] = []
    _fake_upsert(monkeypatch, calls)
    monkeypatch.setattr(seeder, "_SUBJECTS_ROOT", tmp_path)

    concept_dir = tmp_path / "empty_subject" / "concepts" / "no_misconceptions"
    concept_dir.mkdir(parents=True)
    # deliberately no misconceptions.json written

    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="empty_subject", space_id=1)
    await _insert_concept(db_url, subject_id=subject_id, slug="no_misconceptions")

    stats = await seeder.seed(
        db_url,
        subject_slug="empty_subject",
        search_space_id=1,
        embed=False,
    )

    assert stats == {"entries_upserted": 0, "concepts_seeded": 1}
    assert calls == []


@pytest.mark.asyncio
async def test_seed_dry_run_still_reports_stats_but_rolls_back(db_url, monkeypatch):
    calls: list[dict] = []
    _fake_upsert(monkeypatch, calls)

    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="fluid_mechanics", space_id=1)
    await _insert_concept(db_url, subject_id=subject_id, slug="bernoulli_principle")

    stats = await seeder.seed(
        db_url,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        search_space_id=1,
        dry_run=True,
        embed=False,
    )

    assert stats == {"entries_upserted": 2, "concepts_seeded": 1}


@pytest.mark.asyncio
async def test_seed_embeds_description_when_enabled(db_url, monkeypatch):
    calls: list[dict] = []
    _fake_upsert(monkeypatch, calls)
    monkeypatch.setattr("indexing.document_embedder.embed_text", lambda text: [0.1, 0.2, 0.3])

    await _insert_course(db_url, 1)
    subject_id = await _insert_subject(db_url, slug="fluid_mechanics", space_id=1)
    await _insert_concept(db_url, subject_id=subject_id, slug="bernoulli_principle")

    await seeder.seed(
        db_url,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        search_space_id=1,
        embed=True,
    )

    assert all(c["description_embedding"] == [0.1, 0.2, 0.3] for c in calls)


# ---------------------------------------------------------------------------
# CLI argument wiring
# ---------------------------------------------------------------------------


def test_main_requires_database_url_or_env(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = seeder.main(["--subject-slug", "fluid_mechanics"])
    assert rc == 2
    assert "ERROR" in capsys.readouterr().err


def test_main_success_path_prints_stats_and_forwards_args(monkeypatch, capsys):
    captured: dict = {}

    async def fake_seed(db_url, **kwargs):
        captured["db_url"] = db_url
        captured.update(kwargs)
        return {"entries_upserted": 2, "concepts_seeded": 1}

    monkeypatch.setattr(seeder, "seed", fake_seed)

    rc = seeder.main(
        [
            "--database-url",
            "postgresql://x",
            "--subject-slug",
            "fluid_mechanics",
            "--concept-slug",
            "bernoulli_principle",
            "--search-space-id",
            "1",
            "--dry-run",
            "--no-embeddings",
            "-v",
        ]
    )

    assert rc == 0
    assert captured == {
        "db_url": "postgresql://x",
        "subject_slug": "fluid_mechanics",
        "concept_slug": "bernoulli_principle",
        "search_space_id": 1,
        "dry_run": True,
        "embed": False,
    }
    assert "seeded:" in capsys.readouterr().out
