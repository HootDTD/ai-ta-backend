from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _make_index(dirpath: Path) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "items.jsonl").write_text("{}\n", encoding="utf-8")
    (dirpath / "embeddings.npy").write_bytes(b"\x93NUMPY")
    (dirpath / "faiss.index").write_bytes(b"")
    (dirpath / "sqlite.db").write_bytes(b"")
    (dirpath / "meta.json").write_text("{}", encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Configure knowledge base dir in temp
    kb = tmp_path / "knowledge"
    kb.mkdir()
    monkeypatch.setenv("KNOWLEDGE_BASE_DIR", str(kb))
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_API_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_DB_URL", "sqlite+aiosqlite:///:memory:")

    # Import app after env is set
    from server import app
    return TestClient(app)


def test_register_and_list_stores(client, tmp_path):
    # Create two fake indexes
    idx_textbook = tmp_path / "idx_textbook"
    idx_slides = tmp_path / "idx_slides"
    _make_index(idx_textbook)
    _make_index(idx_slides)

    subject = "Aero 101"

    # Mock the KnowledgeManager methods that now use SQLAlchemy
    mock_store_textbook = {
        "id": "1",
        "subject": subject,
        "kind": "textbook",
        "title": "Main Book",
        "index_path": str(idx_textbook),
        "priority": 100,
        "created_at": "2025-01-01T00:00:00Z",
    }
    mock_store_slides = {
        "id": "2",
        "subject": subject,
        "kind": "slides",
        "title": "Lecture Slides",
        "index_path": str(idx_slides),
        "priority": 80,
        "created_at": "2025-01-01T00:00:00Z",
    }

    with patch("knowledge.manager.run_async") as mock_run_async:
        # register_store calls run_async twice: _get_or_create_search_space + _register_store_async
        mock_run_async.side_effect = [
            # First call: _get_or_create_search_space for textbook
            {"id": 1, "name": subject, "slug": "aero-101", "subject_name": subject},
            # Second call: _register_store_async for textbook
            mock_store_textbook,
            # Third call: _get_or_create_search_space for slides
            {"id": 1, "name": subject, "slug": "aero-101", "subject_name": subject},
            # Fourth call: _register_store_async for slides
            mock_store_slides,
            # Fifth call: list_stores -> load_manifest -> run_async
            {
                "subject": subject,
                "slug": "aero-101",
                "materials": [],
                "stores": [mock_store_textbook, mock_store_slides],
            },
        ]

        # Register textbook
        r1 = client.post(
            "/knowledge/stores",
            json={
                "subject": subject,
                "kind": "textbook",
                "title": "Main Book",
                "index_path": str(idx_textbook),
            },
        )
        assert r1.status_code == 200, r1.text
        s1 = r1.json()
        assert s1["kind"] == "textbook" and s1["title"] == "Main Book"
        assert s1["priority"] >= 100

        # Register slides
        r2 = client.post(
            "/knowledge/stores",
            json={
                "subject": subject,
                "kind": "slides",
                "title": "Lecture Slides",
                "index_path": str(idx_slides),
                "priority": 80,
            },
        )
        assert r2.status_code == 200, r2.text

        # List stores
        r3 = client.get(f"/knowledge/stores?subject={subject}")
        assert r3.status_code == 200
        arr = r3.json()
        assert isinstance(arr, list) and len(arr) >= 2
        assert arr[0]["kind"] == "textbook"
        kinds = {a["kind"]: a["priority"] for a in arr}
        assert kinds["textbook"] >= kinds["slides"]
