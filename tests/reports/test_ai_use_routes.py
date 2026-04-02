from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from auth import AuthContext


def _fake_chat_loader(chat_id: str):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "chat_id": chat_id,
        "meta": {"course_id": "CSE101", "assignment_id": "HW1", "due_date": "2025-09-20"},
        "turns": [
            {
                "turn_id": "t1",
                "role": "user",
                "content": "my key is sk-THISISASECRETKEYANDSHOULDBEREDACTED",
                "created_at": now,
            },
            {
                "turn_id": "t2",
                "role": "assistant",
                "content": "Answer referencing [Textbook, p. 12]",
                "created_at": now,
                "model": "gpt-4o-mini",
            },
        ],
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_API_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_DB_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("TEST_FAKE_OPENAI", "1")

    import reports.ai_use.routes as report_routes

    monkeypatch.setattr(
        report_routes,
        "resolve_auth_context",
        lambda _request: AuthContext(user_id="user-1", access_token="token"),
    )
    monkeypatch.setattr(report_routes, "_load_chat_for_user", lambda chat_id, _uid: _fake_chat_loader(chat_id))
    monkeypatch.setattr(report_routes, "_user_owns_chat", lambda _uid, _chat_id: True)

    from server import app

    with TestClient(app) as tc:
        yield tc


def test_create_and_get_report_happy_path(client: TestClient):
    chat_id = "demo-chat"
    create_res = client.post(f"/reports/ai-use/{chat_id}", json={"style": "none", "length": "brief"})
    assert create_res.status_code == 200, create_res.text
    payload = create_res.json()
    assert payload.get("report_id")
    assert isinstance(payload.get("markdown"), str)
    assert payload.get("jsonld") is not None

    rid = payload["report_id"]
    get_res = client.get(f"/reports/ai-use/{rid}")
    assert get_res.status_code == 200, get_res.text
    fetched = get_res.json()
    assert fetched["id"] == rid
    assert fetched["chat_id"] == chat_id


def test_create_report_forbidden_without_chat_ownership(client: TestClient, monkeypatch):
    import reports.ai_use.routes as report_routes

    monkeypatch.setattr(report_routes, "_user_owns_chat", lambda _uid, _chat_id: False)
    res = client.post("/reports/ai-use/not-owned", json={"style": "none", "length": "brief"})
    assert res.status_code == 403
