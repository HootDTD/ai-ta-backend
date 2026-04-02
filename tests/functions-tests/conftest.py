from __future__ import annotations

import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

openai_stub = types.ModuleType("openai")


class _DummyCompletions:
    def create(self, *args, **kwargs):
        # Return a JSON blob with keys that common callers expect
        import json as _json
        content = _json.dumps({
            "markdown": "# Stub report",
            "jsonld": {"@context": "https://schema.org", "@type": "Report"},
        })
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content=content)
            )]
        )


class _DummyEmbeddings:
    def create(self, *args, **kwargs):
        return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=[0.0])])


class _DummyOpenAI:
    def __init__(self, *args, **kwargs):
        self.chat = types.SimpleNamespace(completions=_DummyCompletions())
        self.embeddings = _DummyEmbeddings()


if "openai" not in sys.modules:
    openai_stub.OpenAI = _DummyOpenAI
    sys.modules["openai"] = openai_stub

if "numpy" not in sys.modules:
    try:
        import numpy  # use real numpy when available
    except ImportError:
        numpy_stub = types.ModuleType("numpy")

        def _identity(value=None, *args, **kwargs):
            return value

        numpy_stub.array = _identity
        numpy_stub.zeros = lambda shape, **kw: [[0.0] * (shape[1] if len(shape) > 1 else 1) for _ in range(shape[0] if isinstance(shape, tuple) else shape)]
        numpy_stub.sqrt = lambda x: x ** 0.5
        numpy_stub.pi = 3.141592653589793
        numpy_stub.isscalar = lambda x: isinstance(x, (int, float, complex, str, bytes))
        numpy_stub.float32 = "float32"
        numpy_stub.int64 = "int64"
        numpy_stub.save = lambda path, arr: None
        sys.modules["numpy"] = numpy_stub

if "tiktoken" not in sys.modules:
    try:
        import tiktoken  # use real tiktoken when available
    except ImportError:
        tiktoken_stub = types.ModuleType("tiktoken")

        class _DummyEncoding:
            def encode(self, text):
                if text is None:
                    return []
                return list(str(text).encode("utf-8"))

        def _get_encoding(name):
            return _DummyEncoding()

        tiktoken_stub.get_encoding = _get_encoding
        sys.modules["tiktoken"] = tiktoken_stub

MISSING_REPORT_DEPS: list[str] = []
for mod in ("fastapi", "pydantic"):
    try:
        __import__(mod)
    except ModuleNotFoundError:
        MISSING_REPORT_DEPS.append(mod)


def pytest_ignore_collect(collection_path, path=None, config=None):
    if not MISSING_REPORT_DEPS:
        return False
    path_obj = Path(str(collection_path))
    if "reports" in path_obj.parts:
        return True
    return False


# ---- Supabase mock store for tests ----
import pytest  # noqa: E402

_sb_store: dict[str, list[dict]] = {}


def _sb_reset():
    """Reset the in-memory Supabase mock store."""
    _sb_store.clear()


def _sb_select(table, params=None):
    params = params or {}
    rows = list(_sb_store.get(table, []))
    # Apply simple eq./lte. filters from PostgREST params
    for key, val in params.items():
        if key in ("select", "order", "limit", "on_conflict"):
            continue
        if isinstance(val, str) and val.startswith("eq."):
            target = val[3:]
            rows = [r for r in rows if str(r.get(key, "")) == target]
        elif isinstance(val, str) and val.startswith("lte."):
            target = val[4:]
            rows = [r for r in rows if r.get(key) is not None and r.get(key) <= int(target)]
    # Apply order
    order = params.get("order", "")
    if order:
        field = order.split(".")[0]
        desc = "desc" in order
        rows.sort(key=lambda r: r.get(field, ""), reverse=desc)
    # Apply limit
    limit = params.get("limit")
    if limit:
        rows = rows[:int(limit)]
    return rows


def _sb_select_one(table, params=None):
    rows = _sb_select(table, params)
    return rows[0] if rows else None


def _sb_insert(table, data):
    import uuid as _uuid
    if isinstance(data, dict):
        data = [data]
    for row in data:
        row.setdefault("id", str(_uuid.uuid4()))
        row.setdefault("created_at", "2025-01-01T00:00:00Z")
        _sb_store.setdefault(table, []).append(row)
    return list(data)


def _sb_upsert(table, data, on_conflict="id"):
    if isinstance(data, dict):
        data = [data]
    for row in data:
        rows = _sb_store.setdefault(table, [])
        existing = None
        for i, r in enumerate(rows):
            if r.get(on_conflict) == row.get(on_conflict):
                existing = i
                break
        if existing is not None:
            rows[existing].update(row)
        else:
            import uuid as _uuid
            row.setdefault("id", str(_uuid.uuid4()))
            rows.append(row)
    return list(data)


def _sb_update(table, match_params, data):
    rows = _sb_store.get(table, [])
    updated = []
    for r in rows:
        match = True
        for key, val in match_params.items():
            if isinstance(val, str) and val.startswith("eq."):
                if str(r.get(key, "")) != val[3:]:
                    match = False
                    break
        if match:
            r.update(data)
            updated.append(r)
    return updated


def _sb_delete(table, match_params):
    rows = _sb_store.get(table, [])
    new_rows = []
    for r in rows:
        match = True
        for key, val in match_params.items():
            if isinstance(val, str) and val.startswith("eq."):
                if str(r.get(key, "")) != val[3:]:
                    match = False
                    break
        if not match:
            new_rows.append(r)
    _sb_store[table] = new_rows


def _sb_rpc(function_name, params, *, timeout=30):
    """Mock RPC dispatcher for Supabase functions."""
    return []


@pytest.fixture(autouse=True)
def _mock_supabase(monkeypatch):
    """Automatically mock supabase_client for all tests."""
    _sb_reset()
    import vendors.supabase_client as sb_mod
    monkeypatch.setattr(sb_mod, "select", _sb_select)
    monkeypatch.setattr(sb_mod, "select_one", _sb_select_one)
    monkeypatch.setattr(sb_mod, "insert", _sb_insert)
    monkeypatch.setattr(sb_mod, "upsert", _sb_upsert)
    monkeypatch.setattr(sb_mod, "update", _sb_update)
    monkeypatch.setattr(sb_mod, "delete", _sb_delete)
    monkeypatch.setattr(sb_mod, "rpc", _sb_rpc)
    monkeypatch.setattr(sb_mod, "_reset", _sb_reset)
    yield
    _sb_reset()
