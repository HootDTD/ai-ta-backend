from __future__ import annotations

"""Layout-aware chunker for AI-TA's educational PDFs.

Unlike SurfSense's RecursiveChunker (which operates on raw markdown),
this module operates on Item objects from layout_multimodal_embedder.py.
Each Item is already a semantically coherent unit (a paragraph, heading,
or figure caption extracted with bounding-box awareness), so we use a
1:1 mapping: one Item → one Chunk. This preserves exact page numbers
needed for citation markers like "[Textbook, p. 42]".
"""
from typing import TYPE_CHECKING

from .text_sanitization import sanitize_jsonable, strip_nul

if TYPE_CHECKING:
    # Avoid a hard import of the heavy embedder module at import time
    pass


def items_to_chunk_texts(items: list) -> list[tuple[str, dict]]:
    """Convert layout_multimodal_embedder Item objects to (text, metadata) pairs.

    Args:
        items: List of Item dataclass instances from layout_multimodal_embedder.py

    Returns:
        List of (chunk_text, chunk_metadata) tuples. Empty-text items are skipped.
        NUL bytes are stripped — Postgres rejects \\x00 in TEXT and JSONB.
    """
    result = []
    for item in items:
        text = strip_nul(getattr(item, "text", None) or "").strip()
        if not text:
            # Fall back to raw_text for OCR-scanned items
            text = strip_nul(getattr(item, "raw_text", None) or "").strip()
        if not text:
            continue

        metadata = sanitize_jsonable({
            "page_number": getattr(item, "page", None),
            "section_path": " > ".join(getattr(item, "section_path", None) or []),
            "chunk_type": getattr(item, "type", "body"),
            "figure_id": getattr(item, "figure_id", None),
            "source_pdf": getattr(item, "source_pdf", None),
            "item_id": getattr(item, "id", None),
        })
        result.append((text, metadata))
    return result
