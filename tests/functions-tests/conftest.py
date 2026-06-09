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
        import numpy  # noqa: F401  # imported for its sys.modules side effect
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
        import tiktoken  # noqa: F401  # imported for its sys.modules side effect
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


@pytest.fixture(autouse=True)
def _mock_supabase(monkeypatch):
    """Automatically mock supabase_client for all function-tests.

    Uses the shared SupabaseMock (tests/support/supabase_mock.py) with
    ``auto_id=True`` to preserve this suite's historical behaviour of filling
    in ``id`` / ``created_at`` on insert.
    """
    from tests.support.supabase_mock import SupabaseMock

    mock = SupabaseMock(auto_id=True)
    mock.install(monkeypatch)
    yield
    mock.reset()
