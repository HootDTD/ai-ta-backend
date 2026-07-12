"""Apollo Neo4j degraded mode — apollo/api.py surface.

Covers:
- `get_neo4j_client`: returns None (never raises) on missing env / a
  `from_env()` construction failure; singleton caches a SUCCESSFUL
  construction; NO NEGATIVE CACHING — a failure does not poison the
  singleton, so the next call retries construction fresh.
- `require_neo4j_client`: raises `KGUnavailableError` when the resolved
  client is None; passes a healthy client through unchanged.
- `kg_unavailable` exception handler: 503 payload shape.

NOTE on location: mirrors `test_apollo_error_handlers.py` (same module-file
constraint — `apollo/api.py` has no package to host an `apollo/api/tests/`
directory).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apollo.api as apollo_api
from apollo.api import get_neo4j_client, register_exception_handlers, require_neo4j_client
from apollo.errors import KGUnavailableError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Every test starts with a clean process-wide singleton and every
    NEO4J_* env var absent unless the test sets it itself."""
    apollo_api._neo4j_client_singleton = None
    yield
    apollo_api._neo4j_client_singleton = None


def _clear_neo4j_env(monkeypatch):
    for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE"):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# get_neo4j_client
# ---------------------------------------------------------------------------


def test_get_neo4j_client_returns_none_on_missing_env(monkeypatch):
    """Missing env vars -> Neo4jClient.from_env() raises KeyError internally;
    get_neo4j_client must swallow it and return None, never raise."""
    _clear_neo4j_env(monkeypatch)
    assert get_neo4j_client() is None


def test_get_neo4j_client_returns_none_on_construction_exception(monkeypatch):
    """Any exception from from_env() (bad URI, driver misconfiguration, ...)
    degrades to None rather than propagating."""

    def _boom():
        raise RuntimeError("driver misconfigured")

    monkeypatch.setattr(apollo_api.Neo4jClient, "from_env", staticmethod(_boom))
    assert get_neo4j_client() is None


def test_get_neo4j_client_caches_successful_construction(monkeypatch):
    """A successful construction is cached: from_env() is called exactly
    once across two calls."""
    calls = {"n": 0}
    sentinel = object()

    def _from_env():
        calls["n"] += 1
        return sentinel

    monkeypatch.setattr(apollo_api.Neo4jClient, "from_env", staticmethod(_from_env))

    first = get_neo4j_client()
    second = get_neo4j_client()

    assert first is sentinel
    assert second is sentinel
    assert calls["n"] == 1


def test_get_neo4j_client_retries_after_failure_no_negative_caching(monkeypatch):
    """A construction failure must NOT poison the singleton — the very next
    call retries construction fresh (env may be fixed / Aura may return)."""
    calls = {"n": 0}
    sentinel = object()

    def _from_env():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("aura unreachable")
        return sentinel

    monkeypatch.setattr(apollo_api.Neo4jClient, "from_env", staticmethod(_from_env))

    first = get_neo4j_client()
    assert first is None
    assert calls["n"] == 1

    second = get_neo4j_client()
    assert second is sentinel
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# require_neo4j_client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_neo4j_client_raises_on_none():
    with pytest.raises(KGUnavailableError) as exc_info:
        await require_neo4j_client(None)
    assert exc_info.value.stage == "get_neo4j_client"


@pytest.mark.asyncio
async def test_require_neo4j_client_passes_through_healthy_client():
    sentinel = object()
    out = await require_neo4j_client(sentinel)
    assert out is sentinel


# ---------------------------------------------------------------------------
# kg_unavailable exception handler — 503 payload shape
# ---------------------------------------------------------------------------


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/raise/kg_unavailable")
    def _r():
        raise KGUnavailableError(stage="read_graph", last_error="connection reset")

    return app


def test_kg_unavailable_503():
    r = TestClient(_app(), raise_server_exceptions=False).get("/raise/kg_unavailable")
    assert r.status_code == 503
    body = r.json()
    assert body["error_code"] == "kg_unavailable"
    assert body["stage"] == "read_graph"
    assert body["last_error"] == "connection reset"
    assert "message" in body
