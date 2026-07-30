"""GET /materials/file-url — signed source-PDF links for citation chips.

Mirrors the ``client_with_server`` harness from test_server_part8.py. The
endpoint's job: resolve upload_id (citations' teacher_upload_id) or doc_id
(review pointers' document id) to an app.uploads row, enforce course
membership, and mint a short-lived signed URL for the private-bucket
storage_key. New-tab opens can't carry a Bearer header, so membership is
checked here and expiry rides on the signed URL itself.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from auth import AuthContext


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


def _upload_row(**overrides):
    row = {
        "id": 17,
        "course_id": 7,
        "document_id": 91,
        "storage_key": "course-7/week-1/notes.pdf",
    }
    row.update(overrides)
    return SimpleNamespace(**row)


def _stub_lookup(monkeypatch, server, row):
    """Replace the endpoint's DB read: run_async(_load()) -> row."""

    def _run(coro):
        coro.close()
        return row

    monkeypatch.setattr(server, "run_async", _run)


def _allow_membership(monkeypatch, server, seen):
    def _allow(_request, *, search_space_id: int, role=None):
        seen.append((search_space_id, role))
        return AuthContext(user_id="student-1", access_token="t")

    monkeypatch.setattr(server, "_require_course_membership", _allow)


class _FakeStorage:
    minted: list[dict] = []

    def __init__(self, *a, **k):
        pass

    def create_signed_url(self, *, bucket, object_key, expires_in):
        _FakeStorage.minted.append(
            {"bucket": bucket, "object_key": object_key, "expires_in": expires_in}
        )
        return "http://localhost:54321/storage/v1/object/sign/x?token=abc"


def test_requires_upload_id_or_doc_id(client_with_server):
    client, _server = client_with_server
    resp = client.get("/materials/file-url")
    assert resp.status_code == 400


def test_unknown_material_404s(client_with_server, monkeypatch):
    client, server = client_with_server
    _stub_lookup(monkeypatch, server, None)
    resp = client.get("/materials/file-url", params={"upload_id": 999})
    assert resp.status_code == 404


def test_membership_denial_propagates(client_with_server, monkeypatch):
    client, server = client_with_server
    _stub_lookup(monkeypatch, server, _upload_row())

    def _deny(_request, *, search_space_id: int, role=None):
        assert search_space_id == 7
        raise HTTPException(status_code=403, detail="Forbidden for this course")

    monkeypatch.setattr(server, "_require_course_membership", _deny)
    resp = client.get("/materials/file-url", params={"upload_id": 17})
    assert resp.status_code == 403


def test_missing_storage_key_404s(client_with_server, monkeypatch):
    client, server = client_with_server
    _stub_lookup(monkeypatch, server, _upload_row(storage_key=None))
    _allow_membership(monkeypatch, server, [])
    resp = client.get("/materials/file-url", params={"upload_id": 17})
    assert resp.status_code == 404


def test_happy_path_mints_signed_url_after_membership_check(client_with_server, monkeypatch):
    client, server = client_with_server
    _stub_lookup(monkeypatch, server, _upload_row())
    seen: list = []
    _allow_membership(monkeypatch, server, seen)
    _FakeStorage.minted = []
    monkeypatch.setattr("vendors.supabase_storage.SupabaseStorageClient", _FakeStorage)

    resp = client.get("/materials/file-url", params={"doc_id": 91})

    assert resp.status_code == 200
    assert resp.json() == {
        "url": "http://localhost:54321/storage/v1/object/sign/x?token=abc",
        "expires_in": 300,
    }
    assert seen == [(7, None)]
    assert _FakeStorage.minted == [
        {
            "bucket": "teacher-weekly-uploads",
            "object_key": "course-7/week-1/notes.pdf",
            "expires_in": 300,
        }
    ]


def test_signing_failure_is_a_502_not_a_500(client_with_server, monkeypatch):
    client, server = client_with_server
    _stub_lookup(monkeypatch, server, _upload_row())
    _allow_membership(monkeypatch, server, [])

    class _Broken:
        def __init__(self, *a, **k):
            pass

        def create_signed_url(self, **kwargs):
            raise RuntimeError("sign endpoint down")

    monkeypatch.setattr("vendors.supabase_storage.SupabaseStorageClient", _Broken)
    resp = client.get("/materials/file-url", params={"upload_id": 17})
    assert resp.status_code == 502


# --------------------------------------------------------------------------- #
# Real-PG coverage of the resolver the endpoint's run_async wraps
# --------------------------------------------------------------------------- #
async def test_resolve_material_upload_branches_on_real_pg(db_session):
    """upload_id wins; doc_id falls back to Upload.document_id with
    newest-row-wins across re-uploads; misses return None."""
    from database.models import Course, Document, Upload
    from server import _resolve_material_upload

    course = Course(name="MGMT 101", slug="mgmt-101-file-url", subject_name="Management")
    db_session.add(course)
    await db_session.flush()

    doc = Document(
        title="Week 1 Notes",
        content="notes",
        content_hash="file-url-test-hash-1",
        course_id=course.id,
    )
    db_session.add(doc)
    await db_session.flush()

    older = Upload(
        course_id=course.id,
        week=1,
        kind="notes",
        title="Week 1 Notes (v1)",
        document_id=doc.id,
        storage_key="c/w1-v1.pdf",
    )
    db_session.add(older)
    await db_session.flush()
    newer = Upload(
        course_id=course.id,
        week=1,
        kind="notes",
        title="Week 1 Notes (v2)",
        document_id=doc.id,
        storage_key="c/w1-v2.pdf",
    )
    db_session.add(newer)
    await db_session.flush()

    by_upload = await _resolve_material_upload(db_session, upload_id=older.id, doc_id=None)
    assert by_upload is not None and by_upload.id == older.id

    # upload_id wins even when a doc_id is also supplied.
    both = await _resolve_material_upload(db_session, upload_id=older.id, doc_id=doc.id)
    assert both is not None and both.id == older.id

    by_doc = await _resolve_material_upload(db_session, upload_id=None, doc_id=doc.id)
    assert by_doc is not None and by_doc.id == newer.id
    assert by_doc.storage_key == "c/w1-v2.pdf"

    assert await _resolve_material_upload(db_session, upload_id=None, doc_id=doc.id + 999) is None


def test_load_wrapper_runs_through_real_run_async(client_with_server, monkeypatch):
    """No run_async stub here: the endpoint's _load coroutine executes for
    real against a faked session factory, pinning the run_async + session
    plumbing the other endpoint tests bypass."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    client, server = client_with_server

    row = _upload_row()

    class _Result:
        def scalars(self):
            return SimpleNamespace(first=lambda: row)

    fake_session = SimpleNamespace(execute=AsyncMock(return_value=_Result()))

    @asynccontextmanager
    async def _fake_factory():
        yield fake_session

    monkeypatch.setattr(server, "get_async_session", _fake_factory)
    _allow_membership(monkeypatch, server, [])
    monkeypatch.setattr("vendors.supabase_storage.SupabaseStorageClient", _FakeStorage)

    resp = client.get("/materials/file-url", params={"upload_id": 17})

    assert resp.status_code == 200
    fake_session.execute.assert_awaited_once()
