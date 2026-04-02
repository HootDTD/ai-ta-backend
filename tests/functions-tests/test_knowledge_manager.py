from __future__ import annotations

import types
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from knowledge.manager import KnowledgeManager


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with path.open(mode) as handle:
        handle.write(content)


def _make_mock_space(name="Physics 101", slug="physics-101", space_id=1):
    """Create a mock SearchSpace object."""
    space = MagicMock()
    space.id = space_id
    space.name = name
    space.slug = slug
    space.subject_name = name
    space.weight_overrides = {}
    return space


def _make_mock_doc(doc_id=1, title="Valid", kind="textbook", index_path="", priority=100):
    """Create a mock AITADocument object."""
    doc = MagicMock()
    doc.id = doc_id
    doc.title = title
    doc.material_kind = kind
    doc.document_metadata = {"index_path": index_path, "priority": priority}
    doc.created_at = "2024-01-01T00:00:00Z"
    return doc


def test_resolve_doc_sets_only_returns_existing_indexes(tmp_path):
    base_dir = tmp_path / "knowledge"
    manager = KnowledgeManager(base_dir=base_dir)

    subject = "Physics 101"
    slug = manager._slugify(subject)
    subject_dir = base_dir / slug
    subject_dir.mkdir(parents=True, exist_ok=True)

    valid_dir = subject_dir / "km_valid"
    _touch(valid_dir / "meta.json", "{}")
    _touch(valid_dir / "items.jsonl", "")
    _touch(valid_dir / "embeddings.npy", b"")

    missing_dir = subject_dir / "km_missing"

    mock_space = _make_mock_space()
    mock_docs = [
        _make_mock_doc(doc_id=1, title="Valid", kind="textbook", index_path=str(valid_dir), priority=100),
        _make_mock_doc(doc_id=2, title="Missing", kind="notes", index_path=str(missing_dir), priority=80),
    ]

    # Mock the async load_manifest to return our test data
    manifest = {
        "subject": subject,
        "slug": slug,
        "materials": [],
        "stores": [
            {
                "id": "1",
                "document_id": 1,
                "kind": "textbook",
                "title": "Valid",
                "index_path": str(valid_dir),
                "priority": 100,
                "created_at": "2024-01-01T00:00:00Z",
            },
            {
                "id": "2",
                "document_id": 2,
                "kind": "notes",
                "title": "Missing",
                "index_path": str(missing_dir),
                "priority": 80,
                "created_at": "2024-01-02T00:00:00Z",
            },
        ],
    }

    with patch.object(manager, "load_manifest", return_value=manifest):
        doc_sets = manager.resolve_doc_sets(subject)

    assert doc_sets == [valid_dir]


def test_delete_ocr_pngs(tmp_path):
    manager = KnowledgeManager(base_dir=tmp_path / "knowledge")
    index_dir = manager.base_dir / "chemistry" / "km_test"
    images_dir = index_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    ocr_file = images_dir / "page_0001_ocr.png"
    keep_file = images_dir / "page_0001.png"
    _touch(ocr_file, b"data")
    _touch(keep_file, b"data")

    manager._delete_ocr_pngs(index_dir)

    assert not ocr_file.exists()
    assert keep_file.exists()
    assert images_dir.exists()
