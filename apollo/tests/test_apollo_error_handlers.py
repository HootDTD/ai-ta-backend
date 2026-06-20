"""WU-4C1 — the five new Apollo error -> HTTP handlers.

NOTE on location: ``apollo/api.py`` is a MODULE file, not a package, so an
``apollo/api/tests/`` directory cannot exist. ``apollo/tests/`` is the api-test
home (``test_errors.py`` / ``test_api_auth.py`` live here) and is in scope.

Builds a minimal FastAPI app, registers the Apollo exception handlers, mounts one
route per error, and drives it via ``TestClient`` (no DB, no network). Pins the
HTTP status + the ``_err_payload`` shape (``error_code`` + ``message`` + the
listed extras) for each of the five WU-4C1 errors, plus a registration check.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apollo.api import register_exception_handlers
from apollo.errors import (
    ResolutionInvalidOutputError,
    ResolutionUnavailableError,
    TranscriptAuditUnavailableError,
)
from apollo.graph_compare.validator import (
    ReferenceGraphInvalidError,
    StudentGraphInvalidError,
)

pytestmark = pytest.mark.unit


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/raise/resolution_unavailable")
    def _r1():
        raise ResolutionUnavailableError(stage="write_resolves_to", last_error="boom")

    @app.get("/raise/transcript_audit_unavailable")
    def _r2():
        raise TranscriptAuditUnavailableError(last_error="timeout")

    @app.get("/raise/resolution_invalid_output")
    def _r3():
        raise ResolutionInvalidOutputError(
            returned_key="hallucinated", allowed_keys=("a", "b", "c")
        )

    @app.get("/raise/student_graph_invalid")
    def _r4():
        raise StudentGraphInvalidError(reasons=("bad edge", "cycle"))

    @app.get("/raise/reference_graph_invalid")
    def _r5():
        raise ReferenceGraphInvalidError(reasons=("no declared_paths",))

    return app


def test_resolution_unavailable_503():
    r = TestClient(_app(), raise_server_exceptions=False).get("/raise/resolution_unavailable")
    assert r.status_code == 503
    body = r.json()
    assert body["error_code"] == "resolution_unavailable"
    assert body["stage"] == "write_resolves_to"
    assert body["last_error"] == "boom"
    assert "message" in body


def test_transcript_audit_unavailable_503():
    r = TestClient(_app(), raise_server_exceptions=False).get("/raise/transcript_audit_unavailable")
    assert r.status_code == 503
    body = r.json()
    assert body["error_code"] == "transcript_audit_unavailable"
    assert body["stage"] == "transcript_audit"
    assert body["last_error"] == "timeout"


def test_resolution_invalid_output_500():
    r = TestClient(_app(), raise_server_exceptions=False).get("/raise/resolution_invalid_output")
    assert r.status_code == 500
    body = r.json()
    assert body["error_code"] == "resolution_invalid_output"
    assert body["returned_key"] == "hallucinated"
    # bounded payload: the COUNT, not the full key list
    assert body["allowed_key_count"] == 3
    assert "allowed_keys" not in body


def test_student_graph_invalid_422():
    r = TestClient(_app(), raise_server_exceptions=False).get("/raise/student_graph_invalid")
    assert r.status_code == 422
    body = r.json()
    assert body["error_code"] == "student_graph_invalid"
    assert body["reasons"] == ["bad edge", "cycle"]


def test_reference_graph_invalid_409():
    r = TestClient(_app(), raise_server_exceptions=False).get("/raise/reference_graph_invalid")
    assert r.status_code == 409
    body = r.json()
    assert body["error_code"] == "reference_graph_invalid"
    assert body["reasons"] == ["no declared_paths"]


def test_all_five_registered():
    app = _app()
    for exc in (
        ResolutionUnavailableError,
        TranscriptAuditUnavailableError,
        ResolutionInvalidOutputError,
        StudentGraphInvalidError,
        ReferenceGraphInvalidError,
    ):
        assert exc in app.exception_handlers
