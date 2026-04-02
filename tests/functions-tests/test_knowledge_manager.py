from __future__ import annotations

import uuid
from pathlib import Path

from knowledge.manager import KnowledgeManager


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with path.open(mode) as handle:
        handle.write(content)


def _seed_knowledge(subject: str, slug: str, stores: list[dict]) -> None:
    """Seed knowledge_subjects and knowledge_stores via the patched Supabase client."""
    import vendors.supabase_client as sb

    subject_id = str(uuid.uuid4())
    sb.insert("knowledge_subjects", {
        "id": subject_id,
        "subject": subject,
        "slug": slug,
        "created_at": "2025-01-01T00:00:00Z",
    })
    for st in stores:
        sb.insert("knowledge_stores", {
            "id": str(uuid.uuid4()),
            "subject_id": subject_id,
            **st,
        })


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

    # Seed Supabase mock with two stores — one valid, one missing
    _seed_knowledge(subject, slug, [
        {
            "kind": "textbook",
            "title": "Valid",
            "index_path": str(valid_dir),
            "priority": 100,
            "created_at": "2024-01-01T00:00:00Z",
        },
        {
            "kind": "notes",
            "title": "Missing",
            "index_path": str(missing_dir),
            "priority": 80,
            "created_at": "2024-01-02T00:00:00Z",
        },
    ])

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
