from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from auth import AuthContext
from config.contracts import ParsedTask


@pytest.fixture
def client_with_server(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_API_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_DB_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("TEST_FAKE_OPENAI", "1")

    import server as server

    with TestClient(server.app) as client:
        yield client, server


def test_teacher_weeks_forbidden_without_teacher_role(client_with_server, monkeypatch):
    client, server = client_with_server

    def _deny_teacher(_request, *, search_space_id: int, role=None):
        assert search_space_id == 1
        assert role == "teacher"
        raise HTTPException(status_code=403, detail="Forbidden for this course")

    monkeypatch.setattr(server, "_require_course_membership", _deny_teacher)
    resp = client.get("/teacher/weeks", params={"search_space_id": 1})
    assert resp.status_code == 403


def test_teacher_upload_uses_search_space_and_uploaded_by(client_with_server, monkeypatch):
    client, server = client_with_server
    if not getattr(server, "_HAS_MULTIPART", False):
        pytest.skip("python-multipart not installed in this environment")

    calls: list[dict] = []

    @dataclass
    class _UploadRecord:
        data: dict

        def to_dict(self):
            return dict(self.data)

    class _TeacherStorage:
        def enqueue_upload_by_search_space(self, search_space_id, *, week, kind, pdf_path, title, uploaded_by):
            calls.append(
                {
                    "search_space_id": search_space_id,
                    "week": week,
                    "kind": kind,
                    "title": title,
                    "uploaded_by": uploaded_by,
                    "pdf_exists": pdf_path.exists(),
                }
            )
            return _UploadRecord(
                {
                    "id": "upload-1",
                    "week": week,
                    "kind": kind,
                    "title": title,
                    "status": "queued",
                    "uploaded_at": "2026-03-05T00:00:00Z",
                    "source_name": "week3-notes.pdf",
                    "page_count": 12,
                    "doc_id": "doc-123",
                    "material_id": None,
                }
            )

    monkeypatch.setattr(
        server,
        "_require_course_membership",
        lambda _request, *, search_space_id, role=None: AuthContext(user_id="teacher-1", access_token="tok"),
    )
    monkeypatch.setattr(server, "_get_teacher_storage", lambda: _TeacherStorage())

    resp = client.post(
        "/teacher/upload",
        data={
            "search_space_id": "7",
            "week": "3",
            "kind": "notes",
            "title": "Week 3 Notes",
        },
        files={
            "file": ("week3-notes.pdf", b"%PDF-1.4 test", "application/pdf"),
        },
    )

    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert payload["week"] == 3
    assert payload["kind"] == "notes"
    assert payload["status"] == "queued"
    assert payload["doc_id"] == "doc-123"
    assert calls
    assert calls[0]["search_space_id"] == 7
    assert calls[0]["uploaded_by"] == "teacher-1"
    assert calls[0]["pdf_exists"] is True


def test_teacher_retry_requeues_failed_upload(client_with_server, monkeypatch):
    client, server = client_with_server
    calls: list[tuple[str, int]] = []

    @dataclass
    class _UploadRecord:
        data: dict

        def to_dict(self):
            return dict(self.data)

    class _TeacherStorage:
        def get_upload_search_space_id(self, upload_id):
            calls.append(("lookup", upload_id))
            return 7

        def retry_upload(self, upload_id):
            calls.append(("retry", upload_id))
            return _UploadRecord(
                {
                    "id": str(upload_id),
                    "week": 3,
                    "kind": "slides",
                    "title": "Week 3 Slides",
                    "status": "queued",
                    "uploaded_at": "2026-03-05T00:00:00Z",
                    "source_name": "week3-slides.pdf",
                    "page_count": 18,
                    "doc_id": "doc-987",
                    "error_message": None,
                    "warning_count": 0,
                    "started_at": None,
                    "completed_at": None,
                    "ocr_provider": None,
                    "ocr_summary": {},
                    "material_id": None,
                }
            )

    monkeypatch.setattr(
        server,
        "_require_course_membership",
        lambda _request, *, search_space_id, role=None: AuthContext(user_id="teacher-1", access_token="tok"),
    )
    monkeypatch.setattr(server, "_get_teacher_storage", lambda: _TeacherStorage())

    resp = client.post("/teacher/uploads/55/retry")

    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert payload["id"] == "55"
    assert payload["status"] == "queued"
    assert payload["kind"] == "slides"
    assert calls == [("lookup", 55), ("retry", 55)]


def test_ask_includes_memory_context_and_persists_assistant_turn(client_with_server, monkeypatch):
    client, server = client_with_server

    parse_inputs: list[str] = []
    assistant_turns: list[dict] = []

    fake_workspace = SimpleNamespace(
        class_name="AAE 33300",
        subject_name="Fluid Mechanics",
        weight_overrides={},
        materials=[],
        metadata={"search_space_id": 1},
    )

    class _WorkspaceManager:
        def get(self, identifier: str):
            assert identifier == "1"
            return fake_workspace

    class _TeacherStorage:
        def get_retrieval_weights_by_search_space(self, search_space_id: int):
            assert search_space_id == 1
            return {}

    monkeypatch.setattr(
        server,
        "_require_course_membership",
        lambda _request, *, search_space_id, role=None: AuthContext(user_id="student-1", access_token="tok"),
    )
    monkeypatch.setattr(server, "_get_workspace_manager", lambda: _WorkspaceManager())
    monkeypatch.setattr(server, "_get_teacher_storage", lambda: _TeacherStorage())
    monkeypatch.setattr(server, "_save_attachments", lambda _atts: [])
    monkeypatch.setattr(
        server,
        "_load_memory_and_append_user_turn",
        lambda **kwargs: "Conversation summary:\nPrior work on Bernoulli equation.",
    )
    monkeypatch.setattr(server, "_ask_pgvector", lambda **kwargs: SimpleNamespace(snippets=[]))
    monkeypatch.setattr(
        server,
        "parse_question",
        lambda text, **kwargs: parse_inputs.append(text)
        or ParsedTask(problem_type="qa", asked_outputs=["answer"], asked_output_keys=["answer"]),
    )
    monkeypatch.setattr(server, "solve_with_bundle", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        server,
        "format_answer",
        lambda *args, **kwargs: SimpleNamespace(text="Memory-aware answer", citations=[]),
    )
    monkeypatch.setattr(
        server,
        "_append_assistant_turn_and_refresh",
        lambda **kwargs: assistant_turns.append(kwargs),
    )

    resp = client.post(
        "/ask",
        json={
            "question": "How do we compute dynamic pressure?",
            "chat_id": "chat-1",
            "search_space_id": 1,
            "attachments": [],
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "Memory-aware answer"
    assert parse_inputs, "parse_question should receive the effective prompt"
    effective_prompt = parse_inputs[0]
    assert "Conversation summary:" in effective_prompt
    assert "Current question:" in effective_prompt
    assert "How do we compute dynamic pressure?" in effective_prompt
    assert assistant_turns, "assistant turn should be persisted after response generation"
    assert assistant_turns[0]["chat_id"] == "chat-1"
    assert assistant_turns[0]["search_space_id"] == 1


def test_retrieval_weight_merge_keeps_teacher_overrides_last(client_with_server, monkeypatch):
    client, server = client_with_server

    captured_weights: list[dict] = []
    fake_workspace = SimpleNamespace(
        class_name="AAE 33300",
        subject_name="Fluid Mechanics",
        weight_overrides={"slides": 0.2, "textbook": 0.33},
        materials=[],
        metadata={"search_space_id": 1},
    )

    class _WorkspaceManager:
        def get(self, identifier: str):
            assert identifier == "1"
            return fake_workspace

    class _TeacherStorage:
        def get_retrieval_weights_by_search_space(self, search_space_id: int):
            assert search_space_id == 1
            return {"slides": 0.8}

    monkeypatch.setattr(
        server,
        "_require_course_membership",
        lambda _request, *, search_space_id, role=None: AuthContext(user_id="student-1", access_token="tok"),
    )
    monkeypatch.setattr(server, "_get_workspace_manager", lambda: _WorkspaceManager())
    monkeypatch.setattr(server, "_get_teacher_storage", lambda: _TeacherStorage())
    monkeypatch.setattr(server, "_save_attachments", lambda _atts: [])
    monkeypatch.setattr(server, "_load_memory_and_append_user_turn", lambda **kwargs: "")
    monkeypatch.setattr(
        server,
        "_ask_pgvector",
        lambda **kwargs: captured_weights.append(dict(kwargs["weight_overrides"])) or SimpleNamespace(snippets=[]),
    )
    monkeypatch.setattr(
        server,
        "parse_question",
        lambda *args, **kwargs: ParsedTask(problem_type="qa", asked_outputs=["answer"], asked_output_keys=["answer"]),
    )
    monkeypatch.setattr(server, "solve_with_bundle", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        server,
        "format_answer",
        lambda *args, **kwargs: SimpleNamespace(text="ok", citations=[]),
    )
    monkeypatch.setattr(server, "_append_assistant_turn_and_refresh", lambda **kwargs: None)

    resp = client.post(
        "/ask",
        json={
            "question": "What sources should be weighted highest?",
            "chat_id": "chat-weights",
            "search_space_id": 1,
            "attachments": [],
        },
    )

    assert resp.status_code == 200, resp.text
    assert captured_weights
    assert captured_weights[0]["slides"] == 0.8
    assert captured_weights[0]["textbook"] == 0.33
    assert captured_weights[0]["other"] == 0.03
