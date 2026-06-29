import pytest

from apollo.persistence.models import AuthoredSet


@pytest.mark.asyncio
async def test_authored_set_roundtrip(db_session):
    row = AuthoredSet(
        search_space_id=4,
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
    assert fetched.search_space_id == 4
    assert fetched.set_index == 1
    assert fetched.solution_document_id == 102
    assert fetched.status == "pending"
    assert fetched.result_summary == {}
