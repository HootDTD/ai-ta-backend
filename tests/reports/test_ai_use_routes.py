from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


def _mk_turn(turn_id: str, role: str, content: str, *, model: str | None = None, tool: str | None = None):
    return {
        "turn_id": turn_id,
        "role": role,
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "tool_name": tool,
    }


def _seed_chat(chat_id: str, turns: list[dict]):
    """Seed a chat into the in-memory Supabase mock store via the patched client."""
    import uuid
    import backend.supabase_client as sb
    session_id = str(uuid.uuid4())
    sb.insert("chat_sessions", {
        "id": session_id,
        "chat_id": chat_id,
        "meta": {"course_id": "CSE101", "assignment_id": "HW1", "due_date": "2025-09-20"},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    for turn in turns:
        sb.insert("chat_turns", {
            "id": str(uuid.uuid4()),
            "chat_session_id": session_id,
            **turn,
        })


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TEST_FAKE_OPENAI", "1")
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_API_KEY", "test-key")

    from backend.server import app
    return TestClient(app)


def test_create_and_get_report_happy_path(client):
    chat_id = "demo"
    turns = [
        _mk_turn("t1", "user", "my key sk-THISISASECRETKEYANDSHOULDBEREDACTED question"),
        _mk_turn("t2", "assistant", "Answer referencing [Textbook, p. 12]", model="gpt-4o-mini"),
    ]
    _seed_chat(chat_id, turns)

    res = client.post(f"/reports/ai-use/{chat_id}", json={"style": "none", "length": "brief"})
    assert res.status_code == 200, res.text
    payload = res.json()
    assert "report_id" in payload and payload["report_id"]
    assert isinstance(payload["markdown"], str)
    assert payload["jsonld"] is not None

    rid = payload["report_id"]
    res2 = client.get(f"/reports/ai-use/{rid}")
    assert res2.status_code == 200
    got = res2.json()
    assert got["id"] == rid
    assert got["chat_id"] == chat_id


def test_truncation_oversized_chat_returns_413(client, monkeypatch):
    monkeypatch.setenv("EVIDENCE_TOKEN_BUDGET", "1")
    chat_id = "big"
    big_text = "x" * 4000
    turns = [
        _mk_turn("t1", "user", big_text),
        _mk_turn("t2", "assistant", big_text, model="gpt-4o-mini"),
    ]
    _seed_chat(chat_id, turns)

    res = client.post(f"/reports/ai-use/{chat_id}", json={"style": "none", "length": "brief"})
    assert res.status_code == 413
