"""WU-3C1 — CLI smoke tests for scripts/seed_canon_projection.py.

No live infra: `run` / `project_canon` / `Neo4jClient.from_env` are mocked so
the tests assert ONLY arg parsing + that the scoped args reach the seeder.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from apollo.knowledge_graph.canon_projection import CanonProjectionResult
from scripts.seed_canon_projection import main, run


def test_main_parses_scope_args_and_calls_run(monkeypatch):
    fake_result = CanonProjectionResult(merged=3, entity_count=3)
    run_mock = AsyncMock(return_value=fake_result)
    with patch("scripts.seed_canon_projection.run", run_mock):
        code = main(["--database-url", "postgresql+asyncpg://x/y", "--concept-id", "7"])
    assert code == 0
    run_mock.assert_awaited_once()
    _, kwargs = run_mock.call_args
    assert kwargs["concept_id"] == 7
    assert kwargs["search_space_id"] is None


def test_main_parses_search_space_id(monkeypatch):
    run_mock = AsyncMock(return_value=CanonProjectionResult(merged=1, entity_count=1))
    with patch("scripts.seed_canon_projection.run", run_mock):
        code = main(["--database-url", "postgresql+asyncpg://x/y", "--search-space-id", "5"])
    assert code == 0
    _, kwargs = run_mock.call_args
    assert kwargs["search_space_id"] == 5
    assert kwargs["concept_id"] is None


def test_main_missing_db_url_returns_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code = main([])
    assert code == 2  # documented non-zero (mirrors WU-3B CLI)


@pytest.mark.asyncio
async def test_run_builds_neo4j_from_env_and_projects():
    fake_result = CanonProjectionResult(merged=2, entity_count=2)
    project_mock = AsyncMock(return_value=fake_result)

    fake_neo = AsyncMock()
    fake_engine = AsyncMock()

    class _FakeSessionCtx:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *a):
            return False

    def _fake_sessionmaker(*a, **kw):
        return lambda: _FakeSessionCtx()

    with (
        patch("scripts.seed_canon_projection.create_async_engine", return_value=fake_engine),
        patch("scripts.seed_canon_projection.async_sessionmaker", _fake_sessionmaker),
        patch("scripts.seed_canon_projection.Neo4jClient.from_env", return_value=fake_neo),
        patch("scripts.seed_canon_projection.project_canon", project_mock),
    ):
        result = await run(
            "postgresql+asyncpg://x/y",
            search_space_id=None,
            concept_id=7,
        )

    assert result is fake_result
    project_mock.assert_awaited_once()
    _, kwargs = project_mock.call_args
    assert kwargs["concept_id"] == 7
    fake_neo.close.assert_awaited()
    fake_engine.dispose.assert_awaited()


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DefaultScopeSession:
    """Fake AsyncSession whose execute() returns the MIN(search_space) scalar
    so run()'s no-scope default-resolution branch is exercised offline."""

    def __init__(self, min_id):
        self._min_id = min_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **kw):
        return _ScalarResult(self._min_id)


def _patch_run_infra(session, project_mock):
    fake_neo = AsyncMock()
    fake_engine = AsyncMock()
    return (
        patch("scripts.seed_canon_projection.create_async_engine", return_value=fake_engine),
        patch("scripts.seed_canon_projection.async_sessionmaker", lambda *a, **kw: (lambda: session)),
        patch("scripts.seed_canon_projection.Neo4jClient.from_env", return_value=fake_neo),
        patch("scripts.seed_canon_projection.project_canon", project_mock),
    )


@pytest.mark.asyncio
async def test_run_default_scope_resolves_min_search_space():
    """No scope args -> resolve search_space_id = MIN(aita_search_spaces.id) and
    pass it EXPLICITLY into project_canon (the unscoped-refusal stays intact)."""
    project_mock = AsyncMock(return_value=CanonProjectionResult(merged=4, entity_count=4))
    session = _DefaultScopeSession(min_id=11)
    p1, p2, p3, p4 = _patch_run_infra(session, project_mock)
    with p1, p2, p3, p4:
        result = await run("postgresql+asyncpg://x/y", search_space_id=None, concept_id=None)
    assert result.merged == 4
    _, kwargs = project_mock.call_args
    assert kwargs["search_space_id"] == 11
    assert kwargs["concept_id"] is None


@pytest.mark.asyncio
async def test_run_default_scope_no_search_spaces_raises():
    """No scope args AND no aita_search_spaces rows -> RuntimeError (refuse to
    project against an empty/un-seeded course set)."""
    project_mock = AsyncMock()
    session = _DefaultScopeSession(min_id=None)
    p1, p2, p3, p4 = _patch_run_infra(session, project_mock)
    with p1, p2, p3, p4:
        with pytest.raises(RuntimeError, match="no aita_search_spaces"):
            await run("postgresql+asyncpg://x/y", search_space_id=None, concept_id=None)
    project_mock.assert_not_awaited()
