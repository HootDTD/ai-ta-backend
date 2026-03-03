#!/usr/bin/env python3
"""Migrate local FAISS/SQLite index directories to Supabase pgvector.

Reads each index directory referenced in ``knowledge_stores``, uploads the items
and embeddings to ``knowledge_items``, and the metadata to ``knowledge_store_meta``.

Usage:
    python scripts/migrate_indexes_to_supabase.py [--dry-run] [--batch-size 100]

Requires SUPABASE_URL and SUPABASE_API_KEY in environment (reads from .env).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path so ``backend`` is importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import numpy as np

from backend import supabase_client as sb


def _embedding_str(vec: np.ndarray) -> str:
    """Serialize a numpy vector as a pgvector-compatible string."""
    return "[" + ",".join(f"{v:.8f}" for v in vec.flat) + "]"


def migrate_store(store: dict, *, dry_run: bool = False, batch_size: int = 100) -> dict:
    """Migrate one index directory to Supabase.

    Returns a summary dict with counts.
    """
    store_id = store["id"]
    index_path = store.get("index_path", "")
    idx_dir = Path(index_path)

    # Try relative to project root if absolute path doesn't exist
    if not idx_dir.exists():
        idx_dir = ROOT / index_path
    if not idx_dir.exists():
        return {"store_id": store_id, "path": index_path, "status": "skipped", "reason": "dir not found"}

    items_path = idx_dir / "items.jsonl"
    embeddings_path = idx_dir / "embeddings.npy"
    meta_path = idx_dir / "meta.json"

    for required in [items_path, embeddings_path, meta_path]:
        if not required.exists():
            return {"store_id": store_id, "path": index_path, "status": "skipped", "reason": f"missing {required.name}"}

    # Load local data
    items = []
    with items_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))

    embeddings = np.load(str(embeddings_path))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    if embeddings.shape[0] != len(items):
        return {"store_id": store_id, "path": index_path, "status": "error",
                "reason": f"item/embedding count mismatch: {len(items)} vs {embeddings.shape[0]}"}

    if dry_run:
        return {"store_id": store_id, "path": index_path, "status": "dry-run",
                "items": len(items), "dims": embeddings.shape[1]}

    # Check if already fully migrated
    existing_rows = sb.select("knowledge_items", {
        "store_id": f"eq.{store_id}",
        "select": "id",
        "limit": "1",
    })
    if existing_rows and len(items) > 0:
        # Count how many are already uploaded (rough check)
        count_check = sb.select("knowledge_items", {
            "store_id": f"eq.{store_id}",
            "select": "id",
            "limit": str(len(items) + 1),
        })
        if len(count_check) >= len(items):
            return {"store_id": store_id, "path": index_path, "status": "skipped",
                    "reason": f"already has {len(count_check)} items (expected {len(items)})"}
        # Partial upload — delete and re-upload
        print(f"  [{store_id[:8]}] partial upload detected ({len(count_check)}/{len(items)}), clearing and re-uploading...", flush=True)
        try:
            sb.delete("knowledge_items", {"store_id": f"eq.{store_id}"})
        except Exception as exc:
            resp = getattr(exc, "response", None)
            body = resp.text if resp is not None else str(exc)
            print(f"  [{store_id[:8]}] WARNING: could not delete existing items: {body[:300]}", flush=True)
            print(f"  [{store_id[:8]}] Add DELETE RLS policy: CREATE POLICY \"ki_delete\" ON knowledge_items FOR DELETE TO anon USING (true);", flush=True)
            return {"store_id": store_id, "path": index_path, "status": "error",
                    "reason": "cannot delete partial upload — add DELETE RLS policy"}

    # Upload store metadata (delete first to handle re-runs)
    try:
        sb.delete("knowledge_store_meta", {"store_id": f"eq.{store_id}"})
    except Exception:
        pass  # May not exist yet, or no DELETE policy — that's fine
    sb.insert("knowledge_store_meta", {
        "store_id": store_id,
        "source_pdf": meta.get("source_pdf"),
        "source_pdf_sha256": meta.get("source_pdf_sha256"),
        "model": meta.get("model", "text-embedding-3-large"),
        "dimensions": int(meta.get("dimensions", 3072)),
        "num_items": len(items),
        "counts_by_type": meta.get("counts_by_type"),
        "page_count": meta.get("page_count"),
        "has_ocr": meta.get("has_ocr", False),
        "caption_model": meta.get("caption_model"),
        "tokenizer": meta.get("tokenizer"),
        "token_limit": meta.get("token_limit"),
        "overlap_tokens": meta.get("overlap_tokens"),
        "min_figure_area_ratio": meta.get("min_figure_area_ratio"),
        "doc_titles": meta.get("doc_titles", {}),
        "aliases": meta.get("aliases", {}),
        "store_kind": meta.get("store_kind"),
        "week": str(meta.get("week")) if meta.get("week") is not None else None,
    })

    # Upload items in batches
    def _clean(val):
        """Strip null bytes that PostgreSQL TEXT columns reject."""
        if isinstance(val, str):
            return val.replace("\x00", "")
        return val

    uploaded = 0
    for batch_start in range(0, len(items), batch_size):
        batch_items = items[batch_start:batch_start + batch_size]
        batch_embs = embeddings[batch_start:batch_start + batch_size]
        rows = []
        for item, emb_vec in zip(batch_items, batch_embs):
            sec_path = item.get("section_path")
            if isinstance(sec_path, list):
                sec_path = " > ".join(sec_path)
            rows.append({
                "id": item.get("id", ""),
                "store_id": store_id,
                "doc_id": _clean(item.get("doc_id")),
                "page": item.get("page", 0),
                "type": item.get("type", "body"),
                "section_path": _clean(sec_path),
                "text": _clean(item.get("text", "")),
                "raw_text": _clean(item.get("raw_text")),
                "caption": _clean(item.get("caption")),
                "figure_id": _clean(item.get("figure_id")),
                "neighbors": item.get("neighbors"),
                "parents": item.get("parents"),
                "sha256": item.get("sha256"),
                "source_pdf": _clean(item.get("source_pdf")),
                "source_path": _clean(item.get("source_path")),
                "doc_title": _clean(item.get("doc_title")),
                "doc_short": _clean(item.get("doc_short")),
                "embedding": _embedding_str(emb_vec),
            })
        try:
            sb.insert("knowledge_items", rows)
        except Exception as exc:
            # Print the response body for debugging
            resp = getattr(exc, "response", None)
            body = resp.text if resp is not None else str(exc)
            print(f"  [{store_id[:8]}] ERROR at batch {batch_start}-{batch_start+len(rows)}: {body[:500]}", flush=True)
            raise
        uploaded += len(rows)
        print(f"  [{store_id[:8]}] uploaded {uploaded}/{len(items)} items", flush=True)

    print(f"  [{store_id[:8]}] migration complete: {uploaded} items uploaded", flush=True)

    return {"store_id": store_id, "path": index_path, "status": "ok", "items": uploaded,
            "dims": int(embeddings.shape[1])}


def main():
    parser = argparse.ArgumentParser(description="Migrate local indexes to Supabase pgvector")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated without uploading")
    parser.add_argument("--batch-size", type=int, default=100, help="Rows per insert batch (default: 100)")
    args = parser.parse_args()

    # Fetch all knowledge stores
    stores = sb.select("knowledge_stores", {"order": "priority.desc"})
    if not stores:
        print("No knowledge_stores found in Supabase.")
        return

    print(f"Found {len(stores)} knowledge stores to migrate.\n")

    results = []
    for store in stores:
        title = store.get("title", "")
        print(f"Migrating: {title} (store_id={store['id'][:8]}...)")
        result = migrate_store(store, dry_run=args.dry_run, batch_size=args.batch_size)
        results.append(result)
        print(f"  → {result['status']}: {result.get('reason', result.get('items', ''))}\n")

    # Summary
    print("\n=== Migration Summary ===")
    for r in results:
        status = r["status"]
        items = r.get("items", "-")
        print(f"  {r['store_id'][:8]}  {status:12s}  items={items}  path={r.get('path', '')}")


if __name__ == "__main__":
    main()
