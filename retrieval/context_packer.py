from __future__ import annotations

"""Simple token-budget context packer for the pgvector retrieval path.

Replaces retriever.py's pack_context() quota system (6 body / 4 figure / 2 heading).
Iterates ranked chunks in order, accumulates until the token budget is hit,
and converts each chunk dict to a BundleSnippet with a citation marker.

Also exposes _summarize_snippets() moved here from retriever.py (no FAISS dependency).
"""

import re
from typing import Any, Optional

from ..citations.formatter import DOC_TYPE_LABELS
from ..config.contracts import BundleSnippet
from ..config.settings import get_citation_label

_TOKEN_BUDGET_FRACTION = 0.85  # Use 85% of budget to leave room for prompt overhead


def _count_tokens(text: str) -> int:
    """Fast approximate token count (4 chars ≈ 1 token)."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def pack_context(
    ranked_chunks: list[dict[str, Any]],
    token_budget: int = 6000,
    citation_label: Optional[str] = None,
) -> list[BundleSnippet]:
    """Convert ranked chunk dicts to BundleSnippet list respecting token budget.

    Args:
        ranked_chunks: Chunks sorted by final_score (descending) from store_bias.py.
        token_budget: Max total tokens to include in context.
        citation_label: Override for citation label (default: from CITATION_LABEL env).

    Returns:
        List of BundleSnippet objects ready for solve_with_bundle().
    """
    label = citation_label or get_citation_label()
    limit = int(token_budget * _TOKEN_BUDGET_FRACTION)
    total_tokens = 0
    seen_chunk_ids: set[int] = set()
    snippets: list[BundleSnippet] = []

    for chunk in ranked_chunks:
        chunk_id = chunk.get("chunk_id")
        if chunk_id is not None and chunk_id in seen_chunk_ids:
            continue

        text = (chunk.get("content") or "").strip()
        if not text:
            continue

        toks = _count_tokens(text)
        if total_tokens + toks > limit:
            break

        material_kind = str(chunk.get("material_kind") or "").strip().lower()
        marker_label = _citation_label_for_kind(material_kind, label)
        week = chunk.get("week")

        # Build citation marker: "[Label, p. N]" or "[Label]" for chunks without page
        page = chunk.get("page_number")
        if page:
            if week and material_kind in {"notes", "slides"}:
                marker = f"[{marker_label}, Week {week}, p. {page}]"
            else:
                marker = f"[{marker_label}, p. {page}]"
        else:
            doc_title = chunk.get("doc_title") or marker_label
            short = doc_title[:30] if len(doc_title) > 30 else doc_title
            marker = f"[{short}]"

        doc_title = chunk.get("doc_title") or ""
        doc_short = doc_title[:40] if doc_title else marker_label

        snippets.append(BundleSnippet(
            id=str(chunk_id) if chunk_id is not None else "",
            type=chunk.get("chunk_type") or "body",
            page=page or 0,
            section_path=chunk.get("section_path") or "",
            text=text,
            figure_id=chunk.get("figure_id"),
            why="hit",
            source_path=chunk.get("source_path") or "",
            doc_title=doc_title or None,
            doc_short=doc_short,
            citation_marker=marker,
            final_score={"final": chunk.get("final_score", 0.0)},
            metadata=dict(chunk.get("metadata") or {}),
        ))

        if chunk_id is not None:
            seen_chunk_ids.add(chunk_id)
        total_tokens += toks

    return snippets


def _citation_label_for_kind(kind: str, fallback: str) -> str:
    key = (kind or "").strip().lower()
    if key == "textbook" or not key:
        return fallback
    return DOC_TYPE_LABELS.get(key, fallback)


# ---------------------------------------------------------------------------
# Snippet summarizer (moved from retriever.py — no FAISS dependency)
# ---------------------------------------------------------------------------

_EQ_PATTERN = re.compile(
    r"(?:equation|eq\.?|formula|law)[:\s]+(.+?)(?:\.|$)",
    re.IGNORECASE,
)
_GLOSSARY_PATTERN = re.compile(
    r"(?:defined?\s+as|is\s+called|refers?\s+to|known\s+as)[:\s]+(.+?)(?:\.|$)",
    re.IGNORECASE,
)
_ASSUME_PATTERN = re.compile(
    r"(?:assum(?:ing|e|ption)|approximat(?:e|ing|ion))[:\s]+(.+?)(?:\.|$)",
    re.IGNORECASE,
)


def _summarize_snippets(
    snippets: list[BundleSnippet],
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Extract equations, glossary entries, assumptions, and boundary conditions
    from BundleSnippet text via regex.

    Returns (equations, glossary, assumptions, boundary_conditions).
    Ported from retriever.py — standalone, no FAISS dependency.
    """
    equations: list[dict] = []
    glossary: list[dict] = []
    assumptions: list[dict] = []
    boundary_conditions: list[dict] = []

    seen_eq: set[str] = set()
    seen_gl: set[str] = set()
    seen_as: set[str] = set()

    for sn in snippets:
        text = sn.text or ""

        for m in _EQ_PATTERN.finditer(text):
            val = m.group(1).strip()[:200]
            if val and val not in seen_eq:
                seen_eq.add(val)
                equations.append({"expression": val, "source": sn.citation_marker})

        for m in _GLOSSARY_PATTERN.finditer(text):
            val = m.group(1).strip()[:200]
            if val and val not in seen_gl:
                seen_gl.add(val)
                glossary.append({"definition": val, "source": sn.citation_marker})

        for m in _ASSUME_PATTERN.finditer(text):
            val = m.group(1).strip()[:200]
            if val and val not in seen_as:
                seen_as.add(val)
                assumptions.append({"assumption": val, "source": sn.citation_marker})

    return equations, glossary, assumptions, boundary_conditions
