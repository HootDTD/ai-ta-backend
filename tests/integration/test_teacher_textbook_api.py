"""Server-level tests for course-wide textbook on the teacher API.

Uses the same fake-storage / mocked-membership pattern as test_server_part8.py
(no real DB): asserts the textbook section round-trips through /teacher/weeks and
that kind="textbook" uploads pass through /teacher/upload.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from auth import AuthContext

pytestmark = pytest.mark.integration


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


def _allow_teacher(_request, *, search_space_id: int, role=None):
    assert role == "teacher"
    return AuthContext(user_id="teacher-1", access_token="tok")


def _textbook_entry():
    return {
        "id": "upload-9",
        "week": 0,
        "kind": "textbook",
        "title": "Fluids Textbook",
        "status": "ready",
        "uploaded_at": "2026-06-11T00:00:00Z",
        "source_name": "white-fluids.pdf",
        "page_count": 873,
        "doc_id": "doc-1",
        "material_id": "mat-1",
        "error_message": None,
        "warning_count": 0,
        "started_at": None,
        "completed_at": None,
        "ocr_provider": None,
        "ocr_summary": {},
    }


def _course_payload(*, with_textbook: bool):
    payload = {
        "search_space_id": 2,
        "course": "E2E Fluids",
        "slug": "e2e-fluids",
        "current_week": 1,
        "weeks": [
            {
                "week": 1,
                "notes": {"latest": None, "history": []},
                "slides": {"latest": None, "history": []},
            }
        ],
    }
    if with_textbook:
        payload["textbook"] = {"latest": _textbook_entry(), "history": [_textbook_entry()]}
    return payload


def test_weeks_response_includes_textbook_section(client_with_server, monkeypatch):
    client, server = client_with_server

    class _Storage:
        def list_course_by_search_space(self, search_space_id):
            return _course_payload(with_textbook=True)

    monkeypatch.setattr(server, "_require_course_membership", _allow_teacher)
    monkeypatch.setattr(server, "_get_teacher_storage", lambda: _Storage())

    resp = client.get("/teacher/weeks", params={"search_space_id": 2})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["textbook"]["latest"]["title"] == "Fluids Textbook"
    assert data["textbook"]["latest"]["kind"] == "textbook"
    assert data["textbook"]["latest"]["page_count"] == 873
    assert len(data["textbook"]["history"]) == 1


def test_weeks_response_textbook_defaults_empty_when_absent(client_with_server, monkeypatch):
    # Back-compat: a storage payload without a "textbook" key must not 500.
    client, server = client_with_server

    class _Storage:
        def list_course_by_search_space(self, search_space_id):
            return _course_payload(with_textbook=False)

    monkeypatch.setattr(server, "_require_course_membership", _allow_teacher)
    monkeypatch.setattr(server, "_get_teacher_storage", lambda: _Storage())

    resp = client.get("/teacher/weeks", params={"search_space_id": 2})
    assert resp.status_code == 200, resp.text
    assert resp.json()["textbook"] == {"latest": None, "history": []}


def test_upload_accepts_textbook_kind(client_with_server, monkeypatch):
    client, server = client_with_server
    if not getattr(server, "_HAS_MULTIPART", False):
        pytest.skip("python-multipart not installed in this environment")

    calls: list[dict] = []

    @dataclass
    class _UploadRecord:
        data: dict

        def to_dict(self):
            return dict(self.data)

    class _Storage:
        def enqueue_upload_by_search_space(
            self, search_space_id, *, week, kind, pdf_path, title, uploaded_by
        ):
            calls.append(
                {"search_space_id": search_space_id, "week": week, "kind": kind, "title": title}
            )
            return _UploadRecord(
                {
                    "id": "upload-9",
                    "week": 0,
                    "kind": kind,
                    "title": title or "Textbook",
                    "status": "queued",
                    "uploaded_at": "2026-06-11T00:00:00Z",
                    "source_name": "white-fluids.pdf",
                    "page_count": None,
                    "doc_id": None,
                    "material_id": None,
                }
            )

    monkeypatch.setattr(server, "_require_course_membership", _allow_teacher)
    monkeypatch.setattr(server, "_get_teacher_storage", lambda: _Storage())

    resp = client.post(
        "/teacher/upload",
        data={"search_space_id": "2", "week": "0", "kind": "textbook", "title": ""},
        files={"file": ("white-fluids.pdf", b"%PDF-1.4 test", "application/pdf")},
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["kind"] == "textbook"
    assert body["status"] == "queued"
    assert calls and calls[0]["kind"] == "textbook"
    assert calls[0]["search_space_id"] == 2
