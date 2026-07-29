"""ORM shape + SQLite round-trip for AIUsageReport (app.ai_usage_reports).

Mirrors apollo/persistence/tests/test_question_opportunity_model.py: a fast,
no-Docker check of the table/column/index shape plus a basic round trip.
CHECK constraints are SQLite-invisible (the ORM declares none, per repo
convention) -- those are verified against real Postgres in
tests/database/test_app_schema_v1.py and
tests/database/test_ai_use_reports_repository_postgres.py.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from database.models import Base, LearningActivity
from reports.ai_use.models import AIUsageReport


def test_ai_usage_report_target_schema_contract():
    table = AIUsageReport.__table__
    assert table.schema == "app"
    assert table.name == "ai_usage_reports"
    assert {
        "id",
        "user_id",
        "course_id",
        "chat_id",
        "style",
        "length",
        "markdown",
        "jsonld",
        "model_fingerprint",
        "tool_calls",
        "prompt_hashes",
        "created_at",
    } == set(table.columns.keys())
    assert {
        "ai_usage_reports__user_course__idx",
        "ai_usage_reports__chat_id__idx",
    }.issubset(index.name for index in table.indexes)
    assert list(table.primary_key.columns.keys()) == ["id"]


async def test_ai_usage_report_sqlite_round_trip_defaults_prompt_hashes_empty():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[LearningActivity.__table__, AIUsageReport.__table__],
            )
        )

    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            db.add(
                AIUsageReport(
                    id="4b4e6b0a-0000-4000-8000-0000000000ab",
                    user_id="4b4e6b0a-0000-4000-8000-0000000000cd",
                    course_id=7,
                    chat_id="chat-shape-test",
                    style="APA",
                    length="brief",
                    jsonld={"@type": "Report"},
                    tool_calls=[{"name": "retriever"}],
                )
            )
            await db.commit()

            row = (
                await db.execute(
                    select(AIUsageReport).where(AIUsageReport.chat_id == "chat-shape-test")
                )
            ).scalar_one()
            assert row.course_id == 7
            assert row.style == "APA"
            assert row.jsonld == {"@type": "Report"}
            assert row.tool_calls == [{"name": "retriever"}]
            # prompt_hashes was never set -> ORM-side default(list) applies.
            assert row.prompt_hashes == []
    finally:
        await engine.dispose()
