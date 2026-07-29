"""Real-PG ORM round-trip tests for the AIUsageReport repository.

Runs on the shared ``db_session`` fixture (real pgvector container,
``Base.metadata.create_all`` picks up ``app.ai_usage_reports``, savepoint
rollback per test). Proves the typed repository (``reports.ai_use.models.
create_report``/``get_report_for_user``) persists correctly against real
Postgres FKs (``app.courses``, ``app.learning_activities.external_id``), and
that a cross-user read is refused. CHECK-constraint and RLS-policy behavior
(not exercised by ``Base.metadata.create_all``, which declares no CHECK
constraints) are covered separately in
tests/database/test_app_schema_v1.py.
"""

from __future__ import annotations

import pytest

from database.models import ChatSession, Course
from reports.ai_use.models import create_report, get_report_for_user

pytestmark = pytest.mark.integration

_STUDENT = "10000000-0000-4000-8000-0000000000aa"
_OTHER_STUDENT = "20000000-0000-4000-8000-0000000000bb"


async def _seed_chat_session(db_session) -> ChatSession:
    course = Course(
        name="ai-use repo course",
        slug=f"ai-use-repo-{id(db_session)}",
        subject_name="Physics",
    )
    db_session.add(course)
    await db_session.flush()

    session = ChatSession(
        external_id=f"ai-use-repo-chat-{id(db_session)}",
        user_id=_STUDENT,
        course_id=course.id,
        metadata_={},
        memory_summary="",
    )
    db_session.add(session)
    await db_session.flush()
    return session


async def test_create_report_persists_session_derived_user_and_course(db_session):
    session = await _seed_chat_session(db_session)

    row = await create_report(
        db_session,
        user_id=session.user_id,
        course_id=session.course_id,
        chat_id=session.external_id,
        style="APA",
        length="brief",
        markdown="# Report",
        jsonld={"@type": "Report"},
        model_fingerprint="gpt-4o-mini",
        tool_calls=[{"name": "retriever", "inputs_summary": "{}"}],
        prompt_hashes=["deadbeef"],
    )

    assert row.id
    assert row.user_id == session.user_id
    assert row.course_id == session.course_id
    assert row.chat_id == session.external_id
    assert row.prompt_hashes == ["deadbeef"]

    fetched = await get_report_for_user(db_session, report_id=row.id, user_id=session.user_id)
    assert fetched is not None
    assert fetched.chat_id == session.external_id
    assert fetched.jsonld == {"@type": "Report"}
    assert fetched.tool_calls == [{"name": "retriever", "inputs_summary": "{}"}]


async def test_get_report_for_user_refuses_cross_user_read(db_session):
    session = await _seed_chat_session(db_session)
    row = await create_report(
        db_session,
        user_id=session.user_id,
        course_id=session.course_id,
        chat_id=session.external_id,
    )

    same_user = await get_report_for_user(db_session, report_id=row.id, user_id=session.user_id)
    assert same_user is not None

    other_user = await get_report_for_user(db_session, report_id=row.id, user_id=_OTHER_STUDENT)
    assert other_user is None


async def test_get_report_for_user_returns_none_for_unknown_id(db_session):
    session = await _seed_chat_session(db_session)
    missing = await get_report_for_user(
        db_session, report_id="00000000-0000-4000-8000-000000000000", user_id=session.user_id
    )
    assert missing is None


async def test_create_report_prompt_hashes_default_to_empty_list(db_session):
    session = await _seed_chat_session(db_session)
    row = await create_report(
        db_session,
        user_id=session.user_id,
        course_id=session.course_id,
        chat_id=session.external_id,
    )
    assert row.prompt_hashes == []
    assert row.jsonld is None
    assert row.tool_calls is None
