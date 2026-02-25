"""Tests for the remote search backend (Supabase pgvector)."""
from __future__ import annotations

import uuid

import backend.supabase_client as sb
from backend.remote_search import RemoteSearchBackend


def _seed_store(subject: str = "Physics 101") -> str:
    """Seed a knowledge_subject + knowledge_store + knowledge_store_meta.
    Returns the store_id.
    """
    subject_id = str(uuid.uuid4())
    sb.insert("knowledge_subjects", {
        "id": subject_id,
        "subject": subject,
        "slug": "physics-101",
        "created_at": "2025-01-01T00:00:00Z",
    })
    store_rows = sb.insert("knowledge_stores", {
        "subject_id": subject_id,
        "kind": "textbook",
        "title": "Test Textbook",
        "index_path": "/tmp/test-index",
        "priority": 100,
    })
    store_id = store_rows[0]["id"]
    sb.insert("knowledge_store_meta", {
        "store_id": store_id,
        "model": "text-embedding-3-large",
        "dimensions": 3072,
        "num_items": 2,
        "doc_titles": {"doc-1": "Test Textbook"},
        "aliases": {"doc-1": "Test Textbook"},
    })
    return store_id


def _seed_items(store_id: str) -> list[str]:
    """Seed knowledge_items for the given store. Returns item IDs."""
    items = [
        {
            "id": "item-1",
            "store_id": store_id,
            "doc_id": "doc-1",
            "page": 1,
            "type": "body",
            "section_path": "Chapter 1 > Intro",
            "text": "The boundary layer is a thin region near a solid surface.",
            "doc_title": "Test Textbook",
            "doc_short": "Test Textbook",
            "embedding": "[" + ",".join(["0.1"] * 3072) + "]",
        },
        {
            "id": "item-2",
            "store_id": store_id,
            "doc_id": "doc-1",
            "page": 2,
            "type": "heading",
            "section_path": "Chapter 1 > Displacement Thickness",
            "text": "Displacement thickness definition and formula",
            "doc_title": "Test Textbook",
            "doc_short": "Test Textbook",
            "embedding": "[" + ",".join(["0.2"] * 3072) + "]",
        },
    ]
    sb.insert("knowledge_items", items)
    return [it["id"] for it in items]


def test_hybrid_search_returns_results():
    store_id = _seed_store()
    item_ids = _seed_items(store_id)

    backend = RemoteSearchBackend([store_id], {})
    results = backend.hybrid_search([0.1] * 3072, "boundary layer", k=10)

    assert len(results) == 2
    assert results[0]["item_id"] == "item-1"
    assert results[0]["score_sem"] > 0


def test_fts_count():
    store_id = _seed_store()
    _seed_items(store_id)

    backend = RemoteSearchBackend([store_id], {})
    count = backend.fts_count("boundary")
    assert count == 1  # Only item-1 contains "boundary"

    count_zero = backend.fts_count("nonexistent")
    assert count_zero == 0


def test_fetch_items():
    store_id = _seed_store()
    item_ids = _seed_items(store_id)

    backend = RemoteSearchBackend([store_id], {})
    items = backend.fetch_items(["item-1", "item-2"])

    assert "item-1" in items
    assert "item-2" in items
    assert items["item-1"]["text"] == "The boundary layer is a thin region near a solid surface."

    # Cached on second call
    items2 = backend.fetch_items(["item-1"])
    assert items2["item-1"]["text"] == items["item-1"]["text"]


def test_fetch_items_missing_id():
    store_id = _seed_store()
    _seed_items(store_id)

    backend = RemoteSearchBackend([store_id], {})
    items = backend.fetch_items(["item-1", "nonexistent"])

    assert "item-1" in items
    assert "nonexistent" not in items


def test_load_items_df():
    store_id = _seed_store()
    _seed_items(store_id)

    backend = RemoteSearchBackend([store_id], {})
    df = backend.load_items_df()

    assert len(df) == 2
    assert "item-1" in df.index
    assert "item-2" in df.index
    assert "store_key" in df.columns


def test_load_store_meta():
    store_id = _seed_store()

    backend = RemoteSearchBackend([store_id], {})
    meta = backend.load_store_meta()

    assert store_id in meta
    assert meta[store_id]["model"] == "text-embedding-3-large"
    assert meta[store_id]["dimensions"] == 3072


def test_empty_store_returns_empty():
    store_id = _seed_store()
    # Don't seed items

    backend = RemoteSearchBackend([store_id], {})
    results = backend.hybrid_search([0.1] * 3072, "test", k=10)
    assert results == []

    count = backend.fts_count("test")
    assert count == 0

    items = backend.fetch_items(["nonexistent"])
    assert items == {}
