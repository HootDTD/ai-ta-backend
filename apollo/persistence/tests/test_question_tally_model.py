import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from apollo.persistence.models import QuestionTally
from database.models import Base


def test_question_tally_schema_contract():
    columns = QuestionTally.__table__.columns
    assert {
        "attempt_id",
        "reference_node_id",
        "status",
        "evidence",
        "student_declined",
        "times_asked",
        "last_asked_turn",
        "updated_at",
    }.issubset(columns.keys())
    unique_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in QuestionTally.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("attempt_id", "reference_node_id") in unique_sets
    assert "ix_apollo_question_tally_attempt" in {
        index.name for index in QuestionTally.__table__.indexes
    }


@pytest.mark.asyncio
async def test_question_tally_sqlalchemy_round_trip():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[QuestionTally.__table__])
        )
    async with AsyncSession(engine) as db:
        db.add(
            QuestionTally(
                attempt_id=7,
                reference_node_id="node-a",
                status="tentative",
                evidence=[{"turn_id": 2, "quote": "not sure"}],
                student_declined=True,
                times_asked=1,
                last_asked_turn=3,
            )
        )
        await db.commit()
        row = (await db.execute(select(QuestionTally))).scalar_one()
        assert row.evidence == [{"turn_id": 2, "quote": "not sure"}]
        assert row.student_declined is True
        assert row.times_asked == 1
        assert row.last_asked_turn == 3
    await engine.dispose()
