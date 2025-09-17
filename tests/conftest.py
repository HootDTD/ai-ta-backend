from __future__ import annotations

import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

openai_stub = types.ModuleType("openai")


class _DummyOpenAI:
    def __init__(self, *args, **kwargs):
        pass


if "openai" not in sys.modules:
    openai_stub.OpenAI = _DummyOpenAI
    sys.modules["openai"] = openai_stub

faiss_stub = types.ModuleType("faiss")


class _DummyIndex:
    def __init__(self, *args, **kwargs):
        pass


def _dummy_read_index(*args, **kwargs):
    return _DummyIndex()


if "faiss" not in sys.modules:
    faiss_stub.Index = _DummyIndex
    faiss_stub.IndexFlatIP = _DummyIndex
    faiss_stub.read_index = _dummy_read_index
    sys.modules["faiss"] = faiss_stub

if "numpy" not in sys.modules:
    numpy_stub = types.ModuleType("numpy")

    def _identity(value=None, *args, **kwargs):
        return value

    numpy_stub.array = _identity
    numpy_stub.sqrt = lambda x: x ** 0.5
    numpy_stub.pi = 3.141592653589793
    sys.modules["numpy"] = numpy_stub

if "pandas" not in sys.modules:
    pandas_stub = types.ModuleType("pandas")

    class _DummyDataFrame:
        def __init__(self, data=None, **kwargs):
            self._data = data or []

        def iterrows(self):
            return iter(())

        def itertuples(self, *args, **kwargs):
            return iter(())

        def set_index(self, *args, **kwargs):
            return self

        def to_dict(self, *args, **kwargs):
            return {}

        def get(self, key, default=None):
            if isinstance(self._data, dict):
                return self._data.get(key, default)
            return default

        def __len__(self):
            return len(self._data) if hasattr(self._data, "__len__") else 0

    class _DummySeries(dict):
        pass

    pandas_stub.DataFrame = _DummyDataFrame
    pandas_stub.Series = _DummySeries
    sys.modules["pandas"] = pandas_stub

if "tiktoken" not in sys.modules:
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
for mod in ("fastapi", "sqlalchemy", "pydantic"):
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
