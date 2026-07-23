import pytest

from apollo.persistence.models import ProvisioningRun
from database.models import Course, Document


@pytest.mark.asyncio
async def test_authored_set_roundtrip(db_session):
    # app.provisioning_runs.course_id is an FK to app.courses, so the
    # parent course must exist before the pairing row is inserted.
    space = Course(name="AAS Roundtrip", slug="aas-roundtrip", subject_name="AAE")
    db_session.add(space)
    await db_session.flush()
    problem_doc = Document(
        id=101,
        course_id=space.id,
        title="Problems",
        content="problems",
        content_hash="aas-problems",
    )
    solution_doc = Document(
        id=102,
        course_id=space.id,
        title="Solutions",
        content="solutions",
        content_hash="aas-solutions",
    )
    db_session.add_all([problem_doc, solution_doc])
    await db_session.flush()

    row = ProvisioningRun.authored_set(
        search_space_id=space.id,
        set_index=1,
        problem_document_id=101,
        solution_document_id=102,
        status="pending",
        result_summary={},
    )
    db_session.add(row)
    await db_session.flush()
    assert row.id is not None

    fetched = await db_session.get(ProvisioningRun, row.id)
    assert fetched.search_space_id == space.id
    assert fetched.kind == "authored_set"
    assert fetched.set_index == 1
    assert fetched.solution_document_id == 102
    assert fetched.status == "pending"
    assert fetched.result_summary == {}


def test_provisioning_factories_enforce_kind_contracts():
    authored = ProvisioningRun.authored_set(search_space_id=1, set_index=0)
    generated = ProvisioningRun.generation(search_space_id=1, concept_id=2)

    assert authored.kind == "authored_set"
    assert authored.concept_id is None
    assert generated.kind == "generation"
    assert generated.set_index is None
    with pytest.raises(ValueError, match="non-negative"):
        ProvisioningRun.authored_set(search_space_id=1, set_index=-1)
    with pytest.raises(ValueError, match="generation status"):
        ProvisioningRun.generation(search_space_id=1, concept_id=2, status="done")
