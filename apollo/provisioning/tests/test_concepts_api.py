"""Teacher concept-authoring API tests (WU-TCA).

Follows the test_authored_api pattern: endpoint functions are called directly
with the real-Postgres ``db_session`` harness; auth is either stubbed
(``_fake_require_user`` + real ``require_course_teacher`` with a seeded
membership) or fully faked where the test isn't about auth.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from auth import AuthContext


class _FakeRequest:
    pass


_STUDENT_USER = "11111111-1111-1111-1111-111111111111"
_TEACHER_USER = "22222222-2222-2222-2222-222222222222"


def _as_user(monkeypatch, capi, user_id: str) -> None:
    async def _require_user(_request):
        return AuthContext(user_id=user_id, access_token="tok")

    monkeypatch.setattr(capi, "require_user", _require_user)


async def _seed_space(db, *, slug: str) -> int:
    from database.models import Course

    space = Course(name=f"Course {slug}", slug=slug, subject_name="MIS")
    db.add(space)
    await db.flush()
    return int(space.id)


async def _seed_membership(db, *, user_id: str, search_space_id: int, role: str) -> None:
    from database.models import CourseMembership

    db.add(CourseMembership(user_id=user_id, course_id=search_space_id, role=role))
    await db.flush()


async def _seed_teacher(db, monkeypatch, capi, *, slug: str) -> int:
    space_id = await _seed_space(db, slug=slug)
    await _seed_membership(db, user_id=_TEACHER_USER, search_space_id=space_id, role="teacher")
    _as_user(monkeypatch, capi, _TEACHER_USER)
    return space_id


def test_mint_slug_shapes():
    from apollo.provisioning.concepts_api import mint_slug

    assert mint_slug("Integration by Parts") == "integration_by_parts"
    assert mint_slug("  Ohm's Law! ") == "ohm_s_law"
    assert mint_slug("!!!") == ""
    assert len(mint_slug("x" * 300)) <= 80


@pytest.mark.asyncio
async def test_create_concept_creates_subject_and_row(db_session, monkeypatch):
    import apollo.provisioning.concepts_api as capi
    from apollo.persistence.models import Concept, Subject

    space_id = await _seed_teacher(db_session, monkeypatch, capi, slug="tca-create")

    resp = await capi.create_teacher_concept(
        body=capi.ConceptCreateBody(
            search_space_id=space_id,
            display_name="Entity Relationship Diagrams",
            description="Modeling data as entities, attributes, and relationships.",
        ),
        request=_FakeRequest(),
        db=db_session,
    )

    assert resp["slug"] == "entity_relationship_diagrams"
    assert resp["display_name"] == "Entity Relationship Diagrams"
    assert resp["has_teachable_problems"] is False

    row = (await db_session.execute(select(Concept).where(Concept.id == resp["id"]))).scalar_one()
    assert row.description.startswith("Modeling data")
    subject = (
        await db_session.execute(select(Subject).where(Subject.id == row.subject_id))
    ).scalar_one()
    assert subject.search_space_id == space_id


@pytest.mark.asyncio
async def test_create_duplicate_slug_409(db_session, monkeypatch):
    import apollo.provisioning.concepts_api as capi

    space_id = await _seed_teacher(db_session, monkeypatch, capi, slug="tca-dupe")

    await capi.create_teacher_concept(
        body=capi.ConceptCreateBody(search_space_id=space_id, display_name="Ohm's Law"),
        request=_FakeRequest(),
        db=db_session,
    )
    with pytest.raises(capi.HTTPException) as exc:
        await capi.create_teacher_concept(
            body=capi.ConceptCreateBody(search_space_id=space_id, display_name="ohm s law"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_unsluggable_name_400(db_session, monkeypatch):
    import apollo.provisioning.concepts_api as capi

    space_id = await _seed_teacher(db_session, monkeypatch, capi, slug="tca-bad")

    with pytest.raises(capi.HTTPException) as exc:
        await capi.create_teacher_concept(
            body=capi.ConceptCreateBody(search_space_id=space_id, display_name="!!!"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_student_gets_403(db_session, monkeypatch):
    import apollo.provisioning.concepts_api as capi

    space_id = await _seed_space(db_session, slug="tca-403")
    await _seed_membership(
        db_session, user_id=_STUDENT_USER, search_space_id=space_id, role="student"
    )
    _as_user(monkeypatch, capi, _STUDENT_USER)

    with pytest.raises(capi.HTTPException) as exc:
        await capi.create_teacher_concept(
            body=capi.ConceptCreateBody(search_space_id=space_id, display_name="Firewalls"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_excludes_provisional_and_counts_problems(db_session, monkeypatch):
    import apollo.provisioning.concepts_api as capi
    from apollo.persistence.models import Concept, ConceptProblem, Subject
    from apollo.provisioning.scrape import PROVISIONAL_CONCEPT_SLUG

    space_id = await _seed_teacher(db_session, monkeypatch, capi, slug="tca-list")

    created = await capi.create_teacher_concept(
        body=capi.ConceptCreateBody(search_space_id=space_id, display_name="Databases"),
        request=_FakeRequest(),
        db=db_session,
    )
    # a provisional-inventory row (scrape seam) must stay invisible
    subject_id = (
        await db_session.execute(select(Subject.id).where(Subject.search_space_id == space_id))
    ).scalar_one()
    db_session.add(
        Concept(
            subject_id=subject_id,
            slug=PROVISIONAL_CONCEPT_SLUG,
            display_name="Provisional",
        )
    )
    # one live teachable problem + one quarantined one
    db_session.add(
        ConceptProblem(
            concept_id=created["id"],
            problem_code="P1",
            difficulty="core",
            payload={},
            tier=2,
            search_space_id=space_id,
        )
    )
    db_session.add(
        ConceptProblem(
            concept_id=created["id"],
            problem_code="P2",
            difficulty="core",
            payload={},
            tier=1,
            search_space_id=space_id,
        )
    )
    await db_session.flush()

    resp = await capi.list_teacher_concepts(
        search_space_id=space_id, request=_FakeRequest(), db=db_session
    )
    assert [c["slug"] for c in resp["concepts"]] == ["databases"]
    (concept,) = resp["concepts"]
    assert concept["problem_count"] == 2
    assert concept["has_teachable_problems"] is True


@pytest.mark.asyncio
async def test_update_edits_fields_keeps_slug(db_session, monkeypatch):
    import apollo.provisioning.concepts_api as capi

    space_id = await _seed_teacher(db_session, monkeypatch, capi, slug="tca-edit")
    created = await capi.create_teacher_concept(
        body=capi.ConceptCreateBody(search_space_id=space_id, display_name="Cloud Computing"),
        request=_FakeRequest(),
        db=db_session,
    )

    resp = await capi.update_teacher_concept(
        concept_id=created["id"],
        body=capi.ConceptUpdateBody(
            display_name="Cloud & Edge Computing", description="IaaS/PaaS/SaaS."
        ),
        request=_FakeRequest(),
        db=db_session,
    )
    assert resp["display_name"] == "Cloud & Edge Computing"
    assert resp["description"] == "IaaS/PaaS/SaaS."
    assert resp["slug"] == "cloud_computing"  # slug is stable across renames


@pytest.mark.asyncio
async def test_update_missing_404(db_session, monkeypatch):
    import apollo.provisioning.concepts_api as capi

    await _seed_teacher(db_session, monkeypatch, capi, slug="tca-404")
    with pytest.raises(capi.HTTPException) as exc:
        await capi.update_teacher_concept(
            concept_id=999_999,
            body=capi.ConceptUpdateBody(description="x"),
            request=_FakeRequest(),
            db=db_session,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_bare_concept_ok(db_session, monkeypatch):
    import apollo.provisioning.concepts_api as capi
    from apollo.persistence.models import Concept

    space_id = await _seed_teacher(db_session, monkeypatch, capi, slug="tca-del")
    created = await capi.create_teacher_concept(
        body=capi.ConceptCreateBody(search_space_id=space_id, display_name="Networking"),
        request=_FakeRequest(),
        db=db_session,
    )

    resp = await capi.delete_teacher_concept(
        concept_id=created["id"], request=_FakeRequest(), db=db_session
    )
    assert resp == {"deleted": True, "id": created["id"]}
    gone = (
        await db_session.execute(select(Concept).where(Concept.id == created["id"]))
    ).scalar_one_or_none()
    assert gone is None


@pytest.mark.asyncio
async def test_delete_with_problems_409(db_session, monkeypatch):
    import apollo.provisioning.concepts_api as capi
    from apollo.persistence.models import ConceptProblem

    space_id = await _seed_teacher(db_session, monkeypatch, capi, slug="tca-del409")
    created = await capi.create_teacher_concept(
        body=capi.ConceptCreateBody(search_space_id=space_id, display_name="Security"),
        request=_FakeRequest(),
        db=db_session,
    )
    db_session.add(
        ConceptProblem(
            concept_id=created["id"],
            problem_code="P1",
            difficulty="core",
            payload={},
            tier=2,
            search_space_id=space_id,
        )
    )
    await db_session.flush()

    with pytest.raises(capi.HTTPException) as exc:
        await capi.delete_teacher_concept(
            concept_id=created["id"], request=_FakeRequest(), db=db_session
        )
    assert exc.value.status_code == 409
