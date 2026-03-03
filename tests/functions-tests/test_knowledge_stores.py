from __future__ import annotations

from pathlib import Path

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

    # Import app after env is set
    from backend.server import app
    return TestClient(app)


def test_register_and_list_stores(client, tmp_path):
    # Create two fake indexes
    idx_textbook = tmp_path / "idx_textbook"
    idx_slides = tmp_path / "idx_slides"
    _make_index(idx_textbook)
    _make_index(idx_slides)

    subject = "Aero 101"

    # Register textbook (should default to highest priority)
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

    # Register slides with lower priority
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

    # List stores and verify ordering (textbook first by priority)
    r3 = client.get(f"/knowledge/stores?subject={subject}")
    assert r3.status_code == 200
    arr = r3.json()
    assert isinstance(arr, list) and len(arr) >= 2
    assert arr[0]["kind"] == "textbook"
    # priorities preserved
    kinds = {a["kind"]: a["priority"] for a in arr}
    assert kinds["textbook"] >= kinds["slides"]
