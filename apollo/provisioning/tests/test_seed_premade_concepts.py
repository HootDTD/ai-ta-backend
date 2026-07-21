"""seed_premade_concepts: idempotent premade-list registration (reversed model)."""

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from apollo.persistence.models import Concept
from apollo.provisioning.concept_match import norm_slug
from database.models import Course
from scripts.seed_premade_concepts import seed_premade_concepts

_CONCEPTS = [
    {
        "slug": "integration-by-parts",
        "name": "Integration by Parts",
        "desc": "u dv = uv - v du",
    },
    {"slug": "u-substitution", "name": "Substitution", "desc": "change of variables"},
]


async def _space(db_session, slug: str) -> int:
    space = Course(name=f"Premade {slug}", slug=f"premade-{slug}", subject_name="Calc")
    db_session.add(space)
    await db_session.flush()
    return int(space.id)


@pytest.mark.asyncio
async def test_seed_creates_subject_and_concepts_with_description(db_session):
    ss = await _space(db_session, "create")
    report = await seed_premade_concepts(
        db_session,
        search_space_id=ss,
        subject_slug="calculus_2",
        subject_display_name="Calculus 2",
        concepts=_CONCEPTS,
    )
    assert report.created == 2 and report.updated == 0
    rows = (
        (
            await db_session.execute(
                select(Concept).where(Concept.course_id == ss)
            )
        )
        .scalars()
        .all()
    )
    assert {r.slug for r in rows} == {"integration-by-parts", "u-substitution"}
    assert all(r.description for r in rows)


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_slug_normalized(db_session):
    ss = await _space(db_session, "idem")
    # Pre-existing row with UNDERSCORE spelling (the registry-seeder spelling).
    subj = SimpleNamespace(slug="calculus_2", display_name="Calculus 2", search_space_id=ss)
    db_session.add(
        Concept(
            course_id=subj.search_space_id, subject_slug=subj.slug, subject_display_name=subj.display_name,
            slug="integration_by_parts",
            display_name="Integration by Parts",
        )
    )
    await db_session.flush()

    report = await seed_premade_concepts(
        db_session,
        search_space_id=ss,
        subject_slug="calculus_2",
        subject_display_name="Calculus 2",
        concepts=_CONCEPTS,
    )
    # hyphen JSON slug matched the underscore row -> update, not duplicate
    assert report.created == 1 and report.updated == 1
    n = (
        await db_session.execute(
            select(func.count()).select_from(Concept).where(
                Concept.course_id == ss, Concept.subject_slug == subj.slug
            )
        )
    ).scalar_one()
    assert n == 2
    row = (
        await db_session.execute(select(Concept).where(Concept.slug == "integration_by_parts"))
    ).scalar_one()
    assert row.description == "u dv = uv - v du"  # desc backfilled, slug spelling kept

    # second run: no changes
    report2 = await seed_premade_concepts(
        db_session,
        search_space_id=ss,
        subject_slug="calculus_2",
        subject_display_name="Calculus 2",
        concepts=_CONCEPTS,
    )
    assert report2.created == 0 and report2.updated == 0


@pytest.mark.asyncio
async def test_seed_copies_vocab_only_into_empty_rows(db_session, tmp_path):
    ss = await _space(db_session, "vocab")
    vocab_dir = tmp_path / "concepts"
    cdir = vocab_dir / "integration_by_parts"
    cdir.mkdir(parents=True)
    (cdir / "canonical_symbols.json").write_text('{"symbols": ["x", "u", "v"]}')
    (cdir / "normalization_map.json").write_text('{"antiderivative": "F"}')

    report = await seed_premade_concepts(
        db_session,
        search_space_id=ss,
        subject_slug="calculus_2",
        subject_display_name="Calculus 2",
        concepts=_CONCEPTS,
        vocab_dir=vocab_dir,
    )
    assert report.vocab_copied == 1
    row = (
        await db_session.execute(select(Concept).where(Concept.slug == "integration-by-parts"))
    ).scalar_one()
    assert row.canonical_symbols == ["x", "u", "v"]
    assert row.symbol_metadata == {"description": {"x": "value"}}
    # u-substitution had no vocab dir -> untouched
    other = (
        await db_session.execute(select(Concept).where(Concept.slug == "u-substitution"))
    ).scalar_one()
    assert list(other.canonical_symbols or []) == []


def test_norm_slug() -> None:
    assert norm_slug("Integration-By-Parts ") == "integration_by_parts"


@pytest.mark.asyncio
async def test_seed_backfills_vocab_and_name_on_existing_row(db_session, tmp_path):
    ss = await _space(db_session, "backfill")
    subj = SimpleNamespace(slug="calculus_2", display_name="Calculus 2", search_space_id=ss)
    db_session.add(Concept(course_id=subj.search_space_id, subject_slug=subj.slug, subject_display_name=subj.display_name, slug="integration_by_parts", display_name=""))
    await db_session.flush()

    vocab_dir = tmp_path / "concepts"
    cdir = vocab_dir / "integration_by_parts"
    cdir.mkdir(parents=True)
    (cdir / "canonical_symbols.json").write_text('{"symbols": ["x", "u"]}')
    (cdir / "normalization_map.json").write_text('{"antiderivative": "F"}')

    report = await seed_premade_concepts(
        db_session,
        search_space_id=ss,
        subject_slug="calculus_2",
        subject_display_name="Calculus 2",
        concepts=_CONCEPTS,
        vocab_dir=vocab_dir,
    )
    assert report.vocab_copied == 1
    row = (
        await db_session.execute(select(Concept).where(Concept.slug == "integration_by_parts"))
    ).scalar_one()
    assert row.display_name == "Integration by Parts"  # empty name backfilled
    assert list(row.canonical_symbols) == ["x", "u"]  # empty vocab filled
