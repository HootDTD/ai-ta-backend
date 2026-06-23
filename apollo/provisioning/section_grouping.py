"""Phase 2 — reconstruct document sections from retrieval micro-chunks.

The layout-aware indexer splits a document into line/phrase-level ``aita_chunks``
(median ~17 chars) tuned for RETRIEVAL, plus ``chunk_type='heading'`` markers and a
``section_path`` label per chunk. The per-chunk scrape (stage 1) re-sent the scrape
system prompt once PER micro-chunk — ~92% wasted tokens, and no call ever saw a
whole problem. This module regroups those micro-chunks into the document's real
sections so stage 1 can scrape a whole section at once.

PURE: no DB, no LLM. Consumes duck-typed chunk rows (``id``, ``content``,
``document_id``, ``page_number``, ``section_path``, ``chunk_type``) ordered by
``id`` ascending (the orchestrator's ``_load_chunks`` order) and returns ordered
``Section`` records.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["Section", "group_into_sections", "section_content_hash"]

_HEADING = "heading"


def _normalize(text: str) -> str:
    """Collapse whitespace, strip, lowercase — the content-stable hash input
    (mirrors ``scrape._normalize``; a local helper to keep this module pure)."""
    return re.sub(r"\s+", " ", text).strip().lower()


def section_content_hash(text: str) -> str:
    """sha256 hex of the normalized section text (64 lowercase hex chars). Keys on
    CONTENT not chunk id, so it survives a re-index that re-mints ids."""
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Section:
    """One reconstructed section. ``text`` is the concatenated member-chunk body
    (heading lines excluded — they live in ``title``)."""

    title: str
    document_id: int
    page_start: int | None
    page_end: int | None
    text: str
    source_content_hash: str
    member_chunk_ids: tuple[int, ...]


def group_into_sections(chunk_rows: Sequence) -> list[Section]:
    """Group micro-chunks into sections. A ``chunk_type='heading'`` chunk OR a
    change in non-empty ``section_path`` opens a new section; body/equation chunks
    accumulate. No headings and no ``section_path`` → a single whole-document
    section. The section ``title`` is the heading text (else the first non-empty
    ``section_path``, else ``"section"``); ``text`` excludes heading lines."""
    sections: list[Section] = []
    cur_title: str | None = None
    cur_path: str | None = None
    cur_body: list[str] = []
    cur_ids: list[int] = []
    cur_pages: list[int] = []
    cur_doc: int | None = None

    def _flush() -> None:
        nonlocal cur_title, cur_path, cur_body, cur_ids, cur_pages, cur_doc
        if not cur_ids:
            return
        text = "\n".join(cur_body)
        title = (cur_title or cur_path or "section").strip() or "section"
        sections.append(
            Section(
                title=title,
                document_id=int(cur_doc) if cur_doc is not None else -1,
                page_start=min(cur_pages) if cur_pages else None,
                page_end=max(cur_pages) if cur_pages else None,
                text=text,
                source_content_hash=section_content_hash(text or title),
                member_chunk_ids=tuple(cur_ids),
            )
        )
        cur_title = None
        cur_body = []
        cur_ids = []
        cur_pages = []
        cur_doc = None

    for row in chunk_rows:
        content = str(getattr(row, "content", "") or "")
        is_heading = getattr(row, "chunk_type", None) == _HEADING
        spath = (getattr(row, "section_path", None) or "").strip()
        path_changed = bool(spath) and spath != cur_path

        if is_heading or (path_changed and cur_ids):
            _flush()
        if spath:
            cur_path = spath
        if is_heading:
            cur_title = content.strip() or spath or cur_path
        elif cur_title is None and spath:
            cur_title = spath

        cur_ids.append(int(getattr(row, "id")))
        cur_doc = getattr(row, "document_id", cur_doc)
        page = getattr(row, "page_number", None)
        if page is not None:
            cur_pages.append(int(page))
        if not is_heading and content.strip():
            cur_body.append(content)

    _flush()
    return sections
