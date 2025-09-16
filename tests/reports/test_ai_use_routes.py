from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


def _write_chat(path, chat_id: str, turns: list[dict]):
    data = {
        "chat_id": chat_id,
        "meta": {"course_id": "CSE101", "assignment_id": "HW1", "due_date": "2025-09-20"},
        "turns": turns,
    }
    with open(os.path.join(path, f"{chat_id}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)


def _mk_turn(turn_id: str, role: str, content: str, *, model: str | None = None, tool: str | None = None):
    return {
        "turn_id": turn_id,
        "role": role,
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "tool_name": tool,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Use sqlite in temp dir
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/api.db")
    # Store chats in temp
    chat_dir = tmp_path / "chats"
    chat_dir.mkdir()
    monkeypatch.setenv("CHAT_STORE_DIR", str(chat_dir))
    # Use fake OpenAI client
    monkeypatch.setenv("TEST_FAKE_OPENAI", "1")

    # Import app after env is set
    from backend.reports.ai_use.models import Base, engine
    Base.metadata.create_all(engine)

    from backend.server import app
    return TestClient(app)


def test_create_and_get_report_happy_path(client, tmp_path):
    chat_id = "demo"
    chat_dir = os.environ["CHAT_STORE_DIR"]
    # Include a secret to verify redaction in evidence (service-level test covers this)
    turns = [
        _mk_turn("t1", "user", "my key sk-THISISASECRETKEYANDSHOULDBEREDACTED question"),
        _mk_turn("t2", "assistant", "Answer referencing [Textbook, p. 12]", model="gpt-4o-mini"),
    ]
    _write_chat(chat_dir, chat_id, turns)

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


def test_truncation_oversized_chat_returns_413(client, tmp_path, monkeypatch):
    # Set a tiny token budget to force dropping all turns
    monkeypatch.setenv("EVIDENCE_TOKEN_BUDGET", "1")
    chat_id = "big"
    chat_dir = os.environ["CHAT_STORE_DIR"]
    big_text = "x" * 4000
    turns = [
        _mk_turn("t1", "user", big_text),
        _mk_turn("t2", "assistant", big_text, model="gpt-4o-mini"),
    ]
    _write_chat(chat_dir, chat_id, turns)

    res = client.post(f"/reports/ai-use/{chat_id}", json={"style": "none", "length": "brief"})
    assert res.status_code == 413

