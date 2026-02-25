#!/usr/bin/env python3
"""Quick smoke test for Supabase pgvector hybrid search."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from backend import supabase_client as sb

# Auto-detect store_id from the database
sb._reset()
_stores = sb.select("knowledge_stores", {"select": "id,title", "limit": "5"})
if not _stores:
    print("ERROR: No stores found in knowledge_stores table!")
    sys.exit(1)
print("=== available stores ===")
for s in _stores:
    print(f"  {s['id']}  {s.get('title', '?')}")
store_id = _stores[0]["id"]
print(f"  Using: {store_id}\n")

# Debug: check what credentials the sb module will actually use
import os
url = os.environ.get("SUPABASE_URL", "(not set)")
key = os.environ.get("SUPABASE_API_KEY", "(not set)")
print(f"=== credentials ===")
print(f"  URL: {url}")
print(f"  KEY: {key[:20]}...{key[-10:]}" if len(key) > 30 else f"  KEY: {key}")

# Verify sb module uses the same key
sb_headers = sb._headers()
sb_key = sb_headers.get("apikey", "")
print(f"  sb._headers apikey: {sb_key[:20]}...{sb_key[-10:]}" if len(sb_key) > 30 else f"  sb._headers apikey: {sb_key}")
print()

print("=== sb.select ===")
check = sb.select("knowledge_items", {
    "store_id": f"eq.{store_id}",
    "select": "id,text",
    "limit": "2",
})
print(f"  SELECT returned {len(check)} rows")
for row in check:
    print(f"    {row['id'][:20]}  {(row.get('text') or '')[:60]}")
print()

import requests as _req

def _rpc_debug(name, params):
    """Call RPC and print full error body on failure."""
    print(f"=== {name} ===")
    try:
        result = sb.rpc(name, params)
        return result
    except Exception as exc:
        resp = getattr(exc, "response", None)
        if resp is not None:
            print(f"  HTTP {resp.status_code}")
            print(f"  Body: {resp.text[:500]}")
        else:
            print(f"  Error: {exc}")
        return None

# 1. Test hybrid_search RPC
dummy_embedding_str = "[" + ",".join(["0.1"] * 3072) + "]"
results = _rpc_debug("hybrid_search", {
    "query_text": "boundary layer thickness",
    "query_embedding": dummy_embedding_str,
    "p_store_ids": [store_id],
    "match_count": 5,
})
if results is not None:
    for r in results:
        print(f"  sem={r['score_sem']:.4f}  lex={r['score_lex']:.4f}  item={r['item_id'][:30]}")
    print(f"  -> {len(results)} results")
print()

# 2. Test fts_count RPC
count = _rpc_debug("fts_count", {
    "query_text": "boundary layer",
    "p_store_ids": [store_id],
})
if count is not None:
    print(f"  FTS matches for 'boundary layer': {count}")
print()

# 3. Test fetch_items RPC (use a known item id from the select above)
if check:
    item_ids = [check[0]["id"]]
    items = _rpc_debug("fetch_items", {
        "p_item_ids": item_ids,
        "p_store_ids": [store_id],
    })
    if items is not None:
        for it in items:
            text_preview = (it.get("text") or "")[:80]
            print(f"  [{it['id'][:15]}] p.{it.get('page')} {it.get('type'):8s} {text_preview}")
        print(f"  -> {len(items)} items fetched")
print()

print("Done!")
