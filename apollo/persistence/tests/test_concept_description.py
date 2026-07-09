"""Concept.description column (migration 038) round-trips through the ORM.

The closed-list concept matcher (reversed provisioning) prompts with
"slug — display_name: description" per registered concept; this pins the
column existing, defaulting to '' for legacy rows, and round-tripping.
"""

import pytest

from apollo.persistence.models import Concept, Subject
from database.models import SearchSpace


async def _subject(db_session, slug: str) -> Subject:
    space = SearchSpace(name=f"CD {slug}", slug=f"cd-{slug}", subject_name="Calc")
    db_session.add(space)
    await db_session.flush()
    subject = Subject(slug=slug, display_name="Calc 2", search_space_id=space.id)
    db_session.add(subject)
    await db_session.flush()
    return subject


@pytest.mark.asyncio
async def test_concept_description_roundtrip(db_session):
    subject = await _subject(db_session, "calc2_desc_rt")
    concept = Concept(
        subject_id=subject.id,
        slug="integration-by-parts",
        display_name="Integration by Parts",
        description="Product-form integrals via u dv = uv - v du",
    )
    db_session.add(concept)
    await db_session.flush()

    got = await db_session.get(Concept, concept.id)
    assert got is not None
    assert got.description == "Product-form integrals via u dv = uv - v du"


@pytest.mark.asyncio
async def test_concept_description_defaults_empty(db_session):
    subject = await _subject(db_session, "calc2_desc_default")
    concept = Concept(subject_id=subject.id, slug="x_concept", display_name="X")
    db_session.add(concept)
    await db_session.flush()
    assert concept.description == ""
