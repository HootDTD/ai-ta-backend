from __future__ import annotations

import types

import numpy as np
import pandas as pd

import backend.retriever as r


class _FakeEmbeddingsAPI:
    def create(self, model, input, dimensions):  # type: ignore[override]
        embedding = [1.0] * dimensions
        return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=embedding)])


class _FakeOpenAIClient:
    def __init__(self):
        self.embeddings = _FakeEmbeddingsAPI()


class _FakeFaissIndex:
    def __init__(self):
        self.ntotal = 2

    def search(self, vec, k):  # type: ignore[override]
        scores = np.array([[0.4, 0.4]], dtype=np.float32)
        idxs = np.array([[0, 1]], dtype=np.int64)
        return scores, idxs


def test_store_bias_prioritizes_textbook(monkeypatch):
    monkeypatch.setattr(r, "_get_client", lambda: _FakeOpenAIClient())
    r._client = None

    df = pd.DataFrame(
        [
            {
                "id": "t:1",
                "text": "lift coefficient relation",
                "type": "body",
                "store_key": "/idx/textbook",
                "store_kind": "textbook",
            },
            {
                "id": "s:1",
                "text": "lift coefficient relation",
                "type": "body",
                "store_key": "/idx/slides",
                "store_kind": "slides",
            },
        ]
    )
    df.set_index("id", inplace=True)

    r._faiss_list = [_FakeFaissIndex()]
    r._sqlite_conns = []
    r._items_dfs = [df]
    r._items_df = df
    r._id_to_row = {idx: df.loc[idx] for idx in df.index}
    r._meta = {"model": "fake", "dimensions": 2}
    r._meta_titles = {}
    r._alias_to_occurrences = {}
    r._term_to_aliases = {}
    r._definition_ids = set()
    r._store_biases = {
        "/idx/textbook": 0.2,
        "/idx/slides": 0.0,
    }
    r._store_meta = {
        "/idx/textbook": {"kind": "textbook", "average_confidence": None},
        "/idx/slides": {"kind": "slides", "average_confidence": None},
    }

    hits = r._run_search("lift coefficient", k_sem=2, k_lex=0)
    assert len(hits) >= 2
    top_ids = [h.id for h in hits]
    assert top_ids[0] == "t:1"
