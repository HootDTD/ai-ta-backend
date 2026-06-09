from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


openai_stub = types.ModuleType("openai")


class _DummyCompletions:
    def create(self, *args, **kwargs):
        content = '{"markdown":"# Stub report","jsonld":{"@type":"Report"}}'
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
        )


class _DummyEmbeddings:
    def create(self, *args, **kwargs):
        return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=[0.0])])


class _DummyOpenAI:
    def __init__(self, *args, **kwargs):
        self.chat = types.SimpleNamespace(completions=_DummyCompletions())
        self.embeddings = _DummyEmbeddings()


openai_stub.OpenAI = _DummyOpenAI
sys.modules["openai"] = openai_stub


_sb_store: dict[str, list[dict]] = {}


def _sb_reset() -> None:
    _sb_store.clear()


def _sb_select(table: str, params: dict | None = None):
    params = params or {}
    rows = list(_sb_store.get(table, []))
    for key, val in params.items():
        if key in ("select", "order", "limit", "on_conflict"):
            continue
        if isinstance(val, str) and val.startswith("eq."):
            target = val[3:]
            rows = [r for r in rows if str(r.get(key, "")) == target]
    order = params.get("order", "")
    if order:
        field = order.split(".")[0]
        desc = "desc" in order
        rows.sort(key=lambda r: r.get(field, ""), reverse=desc)
    limit = params.get("limit")
    if limit:
        rows = rows[: int(limit)]
    return rows


def _sb_select_one(table: str, params: dict | None = None):
    rows = _sb_select(table, params)
    return rows[0] if rows else None


def _sb_insert(table: str, data):
    if isinstance(data, dict):
        data = [data]
    _sb_store.setdefault(table, [])
    for row in data:
        _sb_store[table].append(dict(row))
    return list(data)


def _sb_upsert(table: str, data, on_conflict: str = "id"):
    if isinstance(data, dict):
        data = [data]
    rows = _sb_store.setdefault(table, [])
    for row in data:
        found = None
        for idx, existing in enumerate(rows):
            if existing.get(on_conflict) == row.get(on_conflict):
                found = idx
                break
        if found is None:
            rows.append(dict(row))
        else:
            rows[found].update(row)
    return list(data)


def _sb_update(table: str, match_params: dict, data: dict):
    rows = _sb_store.get(table, [])
    out = []
    for row in rows:
        matched = True
        for key, val in match_params.items():
            if isinstance(val, str) and val.startswith("eq."):
                if str(row.get(key, "")) != val[3:]:
                    matched = False
                    break
        if matched:
            row.update(data)
            out.append(row)
    return out


def _sb_delete(table: str, match_params: dict):
    rows = _sb_store.get(table, [])
    keep = []
    for row in rows:
        matched = True
        for key, val in match_params.items():
            if isinstance(val, str) and val.startswith("eq."):
                if str(row.get(key, "")) != val[3:]:
                    matched = False
                    break
        if not matched:
            keep.append(row)
    _sb_store[table] = keep


def _sb_rpc(function_name: str, params: dict, *, timeout: int = 30):
    return []


@pytest.fixture(autouse=True)
def _mock_supabase(monkeypatch):
    monkeypatch.setenv("TEST_FAKE_OPENAI", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_API_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_DB_URL", "sqlite+aiosqlite:///:memory:")
    _sb_reset()
    import vendors.supabase_client as sb_mod

    monkeypatch.setattr(sb_mod, "select", _sb_select)
    monkeypatch.setattr(sb_mod, "select_one", _sb_select_one)
    monkeypatch.setattr(sb_mod, "insert", _sb_insert)
    monkeypatch.setattr(sb_mod, "upsert", _sb_upsert)
    monkeypatch.setattr(sb_mod, "update", _sb_update)
    monkeypatch.setattr(sb_mod, "delete", _sb_delete)
    monkeypatch.setattr(sb_mod, "rpc", _sb_rpc)
    yield
    _sb_reset()


@pytest.fixture
def db_session():
    """Async SQLAlchemy session bound to a REAL Postgres + pgvector instance.

    Phase 1 of the testing plan (docs/TESTING-CI-PLAN.md) wires this to an
    ephemeral Testcontainers `pgvector/pgvector` engine with function-scoped
    transactional rollback. Until then, `@pytest.mark.integration` tests that
    require a live database skip cleanly instead of erroring on a missing
    fixture. pgvector / HNSW cannot run on the in-memory SQLite used by the
    other fixtures, so these tests genuinely need a real Postgres.
    """
    import os

    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip(
            "integration: requires TEST_DATABASE_URL (Postgres + pgvector). "
            "Real fixture lands in Phase 1 (Testcontainers)."
        )
    raise RuntimeError(
        "db_session real implementation is scheduled for Phase 1 of the testing plan"
    )
