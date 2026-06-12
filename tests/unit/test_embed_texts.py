from __future__ import annotations

from types import SimpleNamespace

import pytest

from indexing import document_embedder as de


class _FakeEmbeddings:
    def __init__(self):
        self.calls = []

    def create(self, *, model, input, dimensions):
        self.calls.append(list(input))
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[float(i)] * dimensions) for i in range(len(input))]
        )


class _FakeClient:
    def __init__(self):
        self.embeddings = _FakeEmbeddings()


def test_embed_texts_returns_one_vector_per_input(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(de, "_get_client", lambda: client)

    out = de.embed_texts(["alpha", "beta", "gamma"], dim=4)

    assert len(out) == 3
    assert all(len(v) == 4 for v in out)
    assert client.embeddings.calls == [["alpha", "beta", "gamma"]]


def test_embed_texts_splits_oversized_requests(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(de, "_get_client", lambda: client)
    monkeypatch.setattr(de, "_MAX_INPUTS_PER_REQUEST", 2)

    out = de.embed_texts(["a", "b", "c"], dim=3)

    assert len(out) == 3
    assert client.embeddings.calls == [["a", "b"], ["c"]]


def test_embed_texts_empty_returns_empty(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(de, "_get_client", lambda: client)

    assert de.embed_texts([], dim=3) == []
    assert client.embeddings.calls == []
