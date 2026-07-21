from __future__ import annotations

import types
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from auth import AuthContext


def _fake_chat_loader(chat_id: str):
    now = datetime.now(UTC).isoformat()
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


class _FakeReportStore:
    """In-memory stand-in for reports.ai_use.models' repository functions.

    Mirrors the real repository's contract exactly (owner-scoped get, no
    server round-trip needed) so routes.py's call sites can be exercised
    without a real DB session -- the real repository is covered separately
    against Postgres (tests/database/test_ai_use_reports_repository_postgres.py,
    tests/database/test_app_schema_v1.py).
    """

    def __init__(self) -> None:
        self.rows: dict[str, types.SimpleNamespace] = {}
        self.created_calls: list[dict] = []

    async def create(self, _db, **kwargs):
        self.created_calls.append(dict(kwargs))
        row = types.SimpleNamespace(
            id=f"report-{len(self.rows) + 1}",
            created_at=datetime.now(UTC),
            user_id=kwargs["user_id"],
            course_id=kwargs["course_id"],
            chat_id=kwargs["chat_id"],
            style=kwargs.get("style"),
            length=kwargs.get("length"),
            markdown=kwargs.get("markdown"),
            jsonld=kwargs.get("jsonld"),
            model_fingerprint=kwargs.get("model_fingerprint"),
            tool_calls=kwargs.get("tool_calls"),
            prompt_hashes=list(kwargs.get("prompt_hashes") or []),
        )
        self.rows[row.id] = row
        return row

    async def get(self, _db, *, report_id, user_id):
        row = self.rows.get(report_id)
        if row is None or row.user_id != user_id:
            return None
        return row


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_API_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_DB_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("TEST_FAKE_OPENAI", "1")

    import reports.ai_use.routes as report_routes

    store = _FakeReportStore()
    owned_session = types.SimpleNamespace(course_id=42)

    monkeypatch.setattr(
        report_routes,
        "resolve_auth_context",
        lambda _request: AuthContext(user_id="user-1", access_token="token"),
    )
    monkeypatch.setattr(report_routes, "_load_chat_for_user", lambda chat_id, _uid: _fake_chat_loader(chat_id))
    # The owning chat session is the ONLY source of course_id -- never client input.
    monkeypatch.setattr(report_routes, "_load_owned_chat_session", lambda _chat_id, _uid: owned_session)
    monkeypatch.setattr(report_routes, "create_report", store.create)
    monkeypatch.setattr(report_routes, "get_report_for_user", store.get)

    from server import app

    with TestClient(app) as tc:
        tc.store = store
        tc.owned_session = owned_session
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


def test_create_report_derives_user_and_course_from_session(client: TestClient):
    """user_id comes from the authenticated context and course_id from the
    owning chat session -- create_ai_use_report never reads either off the
    request body (CreateReportBody only has style/length fields at all)."""
    chat_id = "demo-chat-2"
    res = client.post(f"/reports/ai-use/{chat_id}", json={"style": "none", "length": "brief"})
    assert res.status_code == 200, res.text

    [call] = [c for c in client.store.created_calls if c["chat_id"] == chat_id]
    assert call["user_id"] == "user-1"
    assert call["course_id"] == client.owned_session.course_id


def test_create_report_forbidden_without_chat_ownership(client: TestClient, monkeypatch):
    import reports.ai_use.routes as report_routes

    monkeypatch.setattr(report_routes, "_load_owned_chat_session", lambda _chat_id, _uid: None)
    res = client.post("/reports/ai-use/not-owned", json={"style": "none", "length": "brief"})
    assert res.status_code == 403


def test_get_report_cross_user_read_refused(client: TestClient, monkeypatch):
    chat_id = "demo-chat-3"
    create_res = client.post(f"/reports/ai-use/{chat_id}", json={"style": "none", "length": "brief"})
    rid = create_res.json()["report_id"]

    import reports.ai_use.routes as report_routes

    monkeypatch.setattr(
        report_routes,
        "resolve_auth_context",
        lambda _request: AuthContext(user_id="someone-else", access_token="token"),
    )
    res = client.get(f"/reports/ai-use/{rid}")
    assert res.status_code == 404
