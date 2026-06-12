"""Session-scoped retrieval bundle cache for the retrieval-mode orchestrator.

Persists the snippets (and their citation-scoring results) from the most
recent retrieval into ``chat_session_snippets`` so follow-up turns routed
NONE/AUGMENT can skip pgvector retrieval and the per-snippet scoring wave.

Staleness contract: the cache records a fingerprint of the document IDs that
were visible when it was saved. If the visible set changes (new upload, week
advanced), callers must treat the cache as absent and route FRESH.

Everything here fails toward "no cache" — a corrupt payload or contract drift
degrades to a FRESH retrieval, never an error.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.contracts import BundleSnippet
from database.models import AITADocument, ChatSession, ChatSessionSnippet
from retrieval.document_visibility import active_document_conditions

log = logging.getLogger(__name__)

BUNDLE_CACHE_MAX_CHUNKS = int(os.getenv("BUNDLE_CACHE_MAX_CHUNKS", "40"))

_META_KEY = "bundle_cache"


@dataclass(frozen=True)
class CachedBundle:
    """Rehydrated cache contents for one chat session."""

    snippets: List[BundleSnippet]
    # snippet.id -> citation-scoring row from bundle.provenance["citation_rankings"]
    scoring: Dict[str, Dict[str, Any]]
    visible_docs_hash: str
    saved_turn: int

    @property
    def titles(self) -> List[str]:
        """Distinct doc titles + sections, for the router's cached-context evidence."""
        seen: set[str] = set()
        titles: List[str] = []
        for sn in self.snippets:
            label = sn.doc_title or sn.doc_short or ""
            if sn.section_path:
                label = f"{label} — {sn.section_path}" if label else sn.section_path
            if label and label not in seen:
                seen.add(label)
                titles.append(label)
        return titles


def visible_docs_fingerprint(doc_ids: Iterable[int]) -> str:
    joined = ",".join(str(i) for i in sorted(int(d) for d in doc_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


async def compute_visible_docs_hash(
    db_session: AsyncSession, search_space_id: int
) -> str:
    """Fingerprint the currently searchable document set (same visibility rules
    as hybrid retrieval: ready status + week gating)."""
    result = await db_session.execute(
        select(AITADocument.id).where(*active_document_conditions(search_space_id))
    )
    return visible_docs_fingerprint(row[0] for row in result)


async def load_bundle_cache(
    db_session: AsyncSession, *, chat_session: ChatSession
) -> Optional[CachedBundle]:
    """Return the cached bundle for a session, or None when absent/corrupt."""
    rows = (
        (
            await db_session.execute(
                select(ChatSessionSnippet)
                .where(ChatSessionSnippet.chat_session_id == chat_session.id)
                .order_by(ChatSessionSnippet.original_score.desc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None

    snippets: List[BundleSnippet] = []
    scoring: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        payload = row.snippet_payload or {}
        snippet_dict = payload.get("snippet")
        if not isinstance(snippet_dict, dict):
            continue
        try:
            snippet = BundleSnippet(**snippet_dict)
            snippet.validate()
        except (TypeError, ValueError):
            # Contract drift since the payload was written — drop the row.
            log.warning(
                "bundle_cache: unreadable snippet payload for session=%s chunk=%s",
                chat_session.id, row.chunk_id,
            )
            continue
        snippets.append(snippet)
        score_row = payload.get("scoring")
        if isinstance(score_row, dict):
            scoring[snippet.id] = score_row

    if not snippets:
        return None

    meta = (chat_session.meta or {}).get(_META_KEY) or {}
    return CachedBundle(
        snippets=snippets,
        scoring=scoring,
        visible_docs_hash=str(meta.get("visible_docs_hash", "")),
        saved_turn=int(meta.get("turn_index", 0) or 0),
    )


async def save_bundle_cache(
    db_session: AsyncSession,
    *,
    chat_session: ChatSession,
    turn_index: int,
    snippets: List[BundleSnippet],
    scoring: Dict[str, Dict[str, Any]],
    visible_docs_hash: str,
    replace: bool,
) -> None:
    """Persist the bundle used to answer this turn.

    ``replace=True`` (FRESH) drops the previous cache; ``replace=False``
    (NONE/AUGMENT) merges, refreshing ``last_used_turn`` on reused chunks.
    Evicts least-recently-used rows beyond BUNDLE_CACHE_MAX_CHUNKS.
    """
    if replace:
        await db_session.execute(
            delete(ChatSessionSnippet).where(
                ChatSessionSnippet.chat_session_id == chat_session.id
            )
        )
        existing: Dict[int, ChatSessionSnippet] = {}
    else:
        rows = (
            (
                await db_session.execute(
                    select(ChatSessionSnippet).where(
                        ChatSessionSnippet.chat_session_id == chat_session.id
                    )
                )
            )
            .scalars()
            .all()
        )
        existing = {row.chunk_id: row for row in rows}

    for snippet in snippets:
        try:
            chunk_id = int(snippet.id)
        except (TypeError, ValueError):
            continue
        score_row = scoring.get(snippet.id)
        score = 0.0
        if isinstance(score_row, dict):
            try:
                score = float(score_row.get("score", 0.0) or 0.0)
            except (TypeError, ValueError):
                score = 0.0
        payload = {"snippet": asdict(snippet), "scoring": score_row}

        row = existing.get(chunk_id)
        if row is not None:
            row.last_used_turn = turn_index
            row.original_score = score
            row.snippet_payload = payload
        else:
            new_row = ChatSessionSnippet(
                chat_session_id=chat_session.id,
                chunk_id=chunk_id,
                original_score=score,
                first_seen_turn=turn_index,
                last_used_turn=turn_index,
                snippet_payload=payload,
                created_at=datetime.now(UTC),
            )
            db_session.add(new_row)
            existing[chunk_id] = new_row

    # LRU eviction: keep the most recently used, highest-scored rows.
    # Flush first so newly added rows are persisted and deletable.
    if len(existing) > BUNDLE_CACHE_MAX_CHUNKS:
        await db_session.flush()
        ranked = sorted(
            existing.values(),
            key=lambda r: (r.last_used_turn, r.original_score),
            reverse=True,
        )
        for row in ranked[BUNDLE_CACHE_MAX_CHUNKS:]:
            await db_session.delete(row)

    # JSONB column: assign a new dict so SQLAlchemy detects the change.
    chat_session.meta = {
        **(chat_session.meta or {}),
        _META_KEY: {
            "visible_docs_hash": visible_docs_hash,
            "turn_index": turn_index,
        },
    }
    chat_session.updated_at = datetime.now(UTC)
