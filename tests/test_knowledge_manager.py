import json
from pathlib import Path

from backend.knowledge import KnowledgeManager


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with path.open(mode) as handle:
        handle.write(content)


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

    manifest = {
        "subject": subject,
        "slug": slug,
        "materials": [
            {
                "id": "m1",
                "title": "Valid",
                "doc_id": "doc-1",
                "index_dir": "km_valid",
                "created_at": "2024-01-01T00:00:00Z",
                "model": "model",
                "dimensions": 3072,
            },
            {
                "id": "m2",
                "title": "Missing",
                "doc_id": "doc-2",
                "index_dir": "km_missing",
                "created_at": "2024-01-02T00:00:00Z",
            },
        ],
    }
    _touch(subject_dir / "manifest.json", json.dumps(manifest))

    doc_sets = manager.resolve_doc_sets(subject)
    assert doc_sets == [valid_dir.resolve()]


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
