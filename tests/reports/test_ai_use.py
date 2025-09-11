from __future__ import annotations

import json
from datetime import datetime, timezone

from backend.reports.ai_use.models import Base, AIUseReportORM
from backend.reports.ai_use.service import (
    build_evidence_pack,
    generate_report,
    redact,
    excerpt,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _fake_chat_loader(chat_id: str):
    now = datetime.now(timezone.utc).isoformat()
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
                "content": "See [§ aero.pdf; p.12] for displacement thickness.",
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
    assert any("[§" in r for r in pack["file_references"]) 


def test_persistence_roundtrip(tmp_path):
    # setup in-memory or temp sqlite
    db_url = f"sqlite:///{tmp_path}/test.db"
    engine = create_engine(db_url, future=True)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(engine)

    pack = build_evidence_pack("chat-2", style="formal", length="long", chat_loader=_fake_chat_loader)
    payload = generate_report(pack, style="formal", length="long")

    with TestingSession() as db:
        obj = AIUseReportORM(
            chat_id="chat-2",
            style="formal",
            length="long",
            markdown=payload["markdown"],
            jsonld=payload["jsonld"],
            model_fingerprint=payload["model_fingerprint"],
            tool_calls=payload["tool_calls"],
            prompt_hashes=payload["prompt_hashes"],
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)

        fetched = db.get(AIUseReportORM, obj.id)
        assert fetched is not None
        assert fetched.chat_id == "chat-2"
        assert fetched.style == "formal"
        assert isinstance(fetched.markdown, str) and len(fetched.markdown) > 0
        # jsonld stored and retrievable
        assert fetched.jsonld is not None

