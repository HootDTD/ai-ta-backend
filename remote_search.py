"""Remote search backend using Supabase pgvector + PostgreSQL FTS.

Activated when ``RETRIEVAL_BACKEND=supabase``.  Replaces local FAISS + SQLite
with PostgREST RPC calls to Supabase.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from . import supabase_client as sb

log = logging.getLogger(__name__)


class RemoteSearchBackend:
    """Encapsulates all Supabase pgvector + FTS interactions."""

    def __init__(
        self,
        store_ids: List[str],
        store_meta: Dict[str, dict],
    ) -> None:
        self.store_ids = store_ids
        self.store_meta = store_meta          # store_id → meta dict
        self._items_cache: Dict[str, dict] = {}  # item_id → row dict

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def hybrid_search(
        self,
        query_embedding: List[float],
        query_text: str,
        k: int = 60,
    ) -> List[dict]:
        """Call ``hybrid_search`` RPC.

        Returns list of dicts with keys:
        ``item_id, store_id, score_sem, score_lex, rank_sem, rank_lex``.
        """
        # Sanitize: websearch_to_tsquery can't handle quoted phrases or NEAR/N
        clean_tokens = re.findall(r'[a-zA-Z0-9]+', query_text)
        sanitized_text = " ".join(clean_tokens) if clean_tokens else query_text

        embedding_str = "[" + ",".join(f"{v:.8f}" for v in query_embedding) + "]"
        rows = sb.rpc("hybrid_search", {
            "query_text": sanitized_text,
            "query_embedding": embedding_str,
            "p_store_ids": self.store_ids,
            "match_count": k,
        }, timeout=30)
        return rows if isinstance(rows, list) else []

    # ------------------------------------------------------------------
    # FTS helpers
    # ------------------------------------------------------------------

    def fts_count(self, query_text: str) -> int:
        """Count FTS matches across loaded stores."""
        if not query_text:
            return 0
        result = sb.rpc("fts_count", {
            "query_text": query_text,
            "p_store_ids": self.store_ids,
        })
        return int(result) if isinstance(result, (int, float)) else 0

    # ------------------------------------------------------------------
    # Item metadata
    # ------------------------------------------------------------------

    def fetch_items(self, item_ids: List[str]) -> Dict[str, dict]:
        """Bulk-fetch item rows by ID.  Returns ``{item_id: row_dict}``.

        Results are cached for the lifetime of this backend instance
        (i.e. one request).
        """
        missing = [iid for iid in item_ids if iid not in self._items_cache]
        if missing:
            rows = sb.rpc("fetch_items", {
                "p_item_ids": missing,
                "p_store_ids": self.store_ids,
            }, timeout=30)
            if isinstance(rows, list):
                for row in rows:
                    rid = row.get("id")
                    if rid:
                        self._items_cache[rid] = row
        return {iid: self._items_cache[iid] for iid in item_ids if iid in self._items_cache}

    def fetch_items_as_df(self, item_ids: List[str]) -> pd.DataFrame:
        """Like :meth:`fetch_items` but return a DataFrame indexed by ``id``."""
        items = self.fetch_items(item_ids)
        if not items:
            return pd.DataFrame()
        df = pd.DataFrame(list(items.values()))
        if "id" in df.columns:
            df.set_index("id", inplace=True)
        return df

    # ------------------------------------------------------------------
    # Bulk loading (for alias mining at load time)
    # ------------------------------------------------------------------

    def load_items_df(self) -> pd.DataFrame:
        """Fetch **all** items for the loaded stores.

        Used during ``load_assets`` to run ``_mine_aliases`` on the full
        corpus, just like the local path reads ``items.jsonl``.

        Uses PostgREST ``select`` with pagination (1 000 rows per page)
        to avoid payload limits.
        """
        all_rows: List[dict] = []
        for sid in self.store_ids:
            offset = 0
            page_size = 1000
            while True:
                params = {
                    "store_id": f"eq.{sid}",
                    "select": "id,doc_id,page,type,section_path,text,raw_text,caption,"
                              "figure_id,neighbors,parents,source_path,doc_title,"
                              "doc_short,store_id,sha256,source_pdf",
                    "order": "id.asc",
                    "offset": str(offset),
                    "limit": str(page_size),
                }
                rows = sb.select("knowledge_items", params)
                if not rows:
                    break
                all_rows.extend(rows)
                if len(rows) < page_size:
                    break
                offset += page_size

        if not all_rows:
            return pd.DataFrame()

        df = pd.DataFrame(all_rows)
        if "id" in df.columns:
            df.set_index("id", inplace=True)
        # Add store_key column expected by retriever (use store_id as key)
        if "store_id" in df.columns:
            df["store_key"] = df["store_id"]
        df["store_kind"] = None
        return df

    def load_store_meta(self) -> Dict[str, dict]:
        """Fetch ``knowledge_store_meta`` rows for all loaded stores."""
        result: Dict[str, dict] = {}
        for sid in self.store_ids:
            row = sb.select_one("knowledge_store_meta", {"store_id": f"eq.{sid}"})
            if row:
                result[sid] = row
        return result
