import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from apollo.persistence.models import QuestionOpportunity
from database.models import Base


def test_question_opportunity_target_schema_contract():
    table = QuestionOpportunity.__table__
    assert table.schema == "app"
    assert table.name == "question_opportunities"
    assert {
        "course_id",
        "learning_activity_id",
        "attempt_id",
        "reference_node_id",
        "state",
        "question",
        "asked_turn",
        "answered_turn",
        "evidence",
        "student_declined",
        "times_asked",
        "last_asked_turn",
        "created_at",
        "updated_at",
    }.issubset(table.columns.keys())
    unique_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("attempt_id", "reference_node_id") in unique_sets
    assert {
        "question_opportunities__course_session__idx",
        "question_opportunities__activity_id__idx",
    }.issubset(index.name for index in table.indexes)


@pytest.mark.asyncio
async def test_question_opportunity_sqlalchemy_round_trip_preserves_merged_state():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[QuestionOpportunity.__table__]
            )
        )
    async with AsyncSession(engine) as db:
        db.add(
            QuestionOpportunity(
                course_id=11,
                session_id=13,
                attempt_id=7,
                reference_node_id="node-a",
                state="tentative",
                question="Can you explain node a?",
                asked_turn=3,
                evidence=[{"turn_id": 2, "quote": "not sure"}],
                student_declined=True,
                times_asked=1,
                last_asked_turn=3,
            )
        )
        await db.commit()
        row = (await db.execute(select(QuestionOpportunity))).scalar_one()
        assert row.session_id == 13
        assert row.state == "tentative"
        assert row.evidence == [{"turn_id": 2, "quote": "not sure"}]
        assert row.student_declined is True
        assert row.times_asked == 1
        assert row.last_asked_turn == 3
    await engine.dispose()
