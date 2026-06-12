from __future__ import annotations

from types import SimpleNamespace

import pytest

from indexing import document_embedder as de


class _FakeEmbeddings:
    def __init__(self, reverse: bool = False):
        self.calls = []
        self._reverse = reverse

    def create(self, *, model, input, dimensions):
        self.calls.append(list(input))
        items = [
            SimpleNamespace(embedding=[float(i)] * dimensions, index=i)
            for i in range(len(input))
        ]
        # Simulate the API returning items out of arrival order; embed_texts must
        # re-sort by `index` so output still tracks input order.
        if self._reverse:
            items = list(reversed(items))
        return SimpleNamespace(data=items)


class _FakeClient:
    def __init__(self, reverse: bool = False):
        self.embeddings = _FakeEmbeddings(reverse=reverse)


def test_embed_texts_returns_one_vector_per_input(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(de, "_get_client", lambda: client)

    out = de.embed_texts(["alpha", "beta", "gamma"], dim=4)

    assert len(out) == 3
    assert out[0] == [0.0, 0.0, 0.0, 0.0]   # 'alpha' -> within-batch index 0
    assert out[1] == [1.0, 1.0, 1.0, 1.0]   # 'beta'  -> index 1
    assert out[2] == [2.0, 2.0, 2.0, 2.0]   # 'gamma' -> index 2
    assert client.embeddings.calls == [["alpha", "beta", "gamma"]]


def test_embed_texts_preserves_order_when_response_out_of_order(monkeypatch):
    client = _FakeClient(reverse=True)
    monkeypatch.setattr(de, "_get_client", lambda: client)

    out = de.embed_texts(["alpha", "beta", "gamma"], dim=2)

    assert out[0] == [0.0, 0.0]
    assert out[1] == [1.0, 1.0]
    assert out[2] == [2.0, 2.0]


def test_embed_texts_splits_oversized_requests(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(de, "_get_client", lambda: client)
    monkeypatch.setattr(de, "_MAX_INPUTS_PER_REQUEST", 2)

    out = de.embed_texts(["a", "b", "c"], dim=3)

    assert len(out) == 3
    # Two batches; within-batch index restarts at 0 each request.
    assert out[0] == [0.0, 0.0, 0.0]   # 'a' -> batch 0, index 0
    assert out[1] == [1.0, 1.0, 1.0]   # 'b' -> batch 0, index 1
    assert out[2] == [0.0, 0.0, 0.0]   # 'c' -> batch 1, index 0
    assert client.embeddings.calls == [["a", "b"], ["c"]]


def test_embed_texts_truncates_long_inputs(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(de, "_get_client", lambda: client)

    de.embed_texts(["x" * 10_000], dim=2)

    assert client.embeddings.calls[0] == ["x" * 8000]


def test_embed_texts_empty_returns_empty(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(de, "_get_client", lambda: client)

    assert de.embed_texts([], dim=3) == []
    assert client.embeddings.calls == []
