"""Concept.description column (migration 038) round-trips through the ORM.

The closed-list concept matcher (reversed provisioning) prompts with
"slug — display_name: description" per registered concept; this pins the
column existing, defaulting to '' for legacy rows, and round-tripping.
"""

import pytest

from apollo.persistence.models import Concept
from database.models import Course


async def _course_id(db_session, slug: str) -> int:
    space = Course(name=f"CD {slug}", slug=f"cd-{slug}", subject_name="Calc")
    db_session.add(space)
    await db_session.flush()
    return int(space.id)


@pytest.mark.asyncio
async def test_concept_description_roundtrip(db_session):
    course_id = await _course_id(db_session, "calc2_desc_rt")
    concept = Concept(
        course_id=course_id,
        subject_slug="calc2",
        subject_display_name="Calc 2",
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
    course_id = await _course_id(db_session, "calc2_desc_default")
    concept = Concept(
        course_id=course_id,
        subject_slug="calc2",
        subject_display_name="Calc 2",
        slug="x_concept",
        display_name="X",
    )
    db_session.add(concept)
    await db_session.flush()
    assert concept.description == ""
