import pytest

from apollo.persistence.models import AuthoredSet
from database.models import Course


@pytest.mark.asyncio
async def test_authored_set_roundtrip(db_session):
    # apollo_authored_sets.search_space_id is an FK to app.courses, so the
    # parent course must exist before the pairing row is inserted.
    space = Course(name="AAS Roundtrip", slug="aas-roundtrip", subject_name="AAE")
    db_session.add(space)
    await db_session.flush()

    row = AuthoredSet(
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

    fetched = await db_session.get(AuthoredSet, row.id)
    assert fetched.search_space_id == space.id
    assert fetched.set_index == 1
    assert fetched.solution_document_id == 102
    assert fetched.status == "pending"
    assert fetched.result_summary == {}
