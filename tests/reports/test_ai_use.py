from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from database.models import Base, ChatSession, LearningActivity
from reports.ai_use.models import AIUsageReport, create_report, get_report_for_user
from reports.ai_use.service import (
    build_evidence_pack,
    excerpt,
    generate_report,
    redact,
)


def _fake_chat_loader(chat_id: str):
    now = datetime.now(UTC).isoformat()
    return {
        "chat_id": chat_id,
        "meta": {"course_id": "CSE101", "assignment_id": "HW1", "due_date": "2025-09-20"},
        "turns": [
            {
                "turn_id": "t1",
                "role": "user",
                "content": "my key is sk-THISISASECRETKEYANDSHOULDBEREDACTED and question about boundary layer",
                "created_at": now,
                "model": None,
            },
            {
                "turn_id": "t2",
                "role": "tool",
                "content": "searching…",
                "created_at": now,
                "tool_name": "retriever",
                "tool_inputs": {"k": "v"},
            },
            {
                "turn_id": "t3",
                "role": "assistant",
                "content": "See [Textbook, p. 12] for displacement thickness.",
                "created_at": now,
                "model": "gpt-4o-mini",
            },
        ],
    }


def test_redaction_and_truncation():
    s = "prefix sk-ABCDEFG0123456789012345 suffix"
    r = redact(s)
    assert "<redacted>" in r and "sk-ABCDEFG" not in r
    long = "x" * 1500
    assert len(excerpt(long)) == 1000


def test_evidence_assembly():
    pack = build_evidence_pack("chat-1", style="concise", length="short", chat_loader=_fake_chat_loader)
    assert pack["chat_id"] == "chat-1"
    assert pack["course_meta"]["course_id"] == "CSE101"
    assert len(pack["turns"]) == 3
    # user prompt hashes present
    assert len(pack["prompt_hashes"]) == 1
    # tool calls aggregated
    assert pack["tool_calls"][0]["name"] == "retriever"
    # file references extracted from assistant answer
    assert any("[Textbook, p." in r for r in pack["file_references"])


async def test_persistence_roundtrip():
    """create_report/get_report_for_user against a real (in-memory SQLite)
    SQLAlchemy session -- the legacy Supabase PostgREST mock is gone, this is
    the typed repository end to end."""
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
    # No app.courses row: SQLite doesn't enforce FK existence, and this test
    # only needs a plausible course_id, not full course-table integrity (that
    # FK/CHECK integrity is covered by the real-Postgres tests in
    # tests/database/test_ai_use_reports_repository_postgres.py and
    # tests/database/test_app_schema_v1.py).

    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            # A hex-letter suffix ("...aaaa") keeps this a non-numeric string --
            # an all-decimal-digit UUID gets SQLite NUMERIC-affinity-coerced to
            # a float on round trip through the Uuid column type (a real
            # SQLite gotcha, not specific to this table).
            chat_session = ChatSession(
                external_id="chat-2",
                user_id="11111111-1111-1111-1111-1111111111aa",
                course_id=101,
                metadata_={},
                memory_summary="",
            )
            db.add(chat_session)
            await db.flush()

            pack = build_evidence_pack(
                "chat-2", style="formal", length="long", chat_loader=_fake_chat_loader
            )
            payload = generate_report(pack, style="formal", length="long")

            row = await create_report(
                db,
                user_id=chat_session.user_id,
                course_id=chat_session.course_id,
                chat_id=chat_session.external_id,
                style="formal",
                length="long",
                markdown=payload["markdown"],
                jsonld=payload["jsonld"],
                model_fingerprint=payload["model_fingerprint"],
                tool_calls=payload["tool_calls"],
                prompt_hashes=payload["prompt_hashes"],
            )

            assert row.chat_id == "chat-2"
            assert row.style == "formal"

            fetched = await get_report_for_user(
                db, report_id=row.id, user_id=chat_session.user_id
            )
            assert fetched is not None
            assert fetched.chat_id == "chat-2"
            assert fetched.style == "formal"
            assert isinstance(fetched.markdown, str) and len(fetched.markdown) > 0
            assert fetched.jsonld is not None

            # cross-user read is refused: same report_id, different user_id.
            other_user = await get_report_for_user(
                db, report_id=row.id, user_id="22222222-2222-2222-2222-2222222222bb"
            )
            assert other_user is None
    finally:
        await engine.dispose()
