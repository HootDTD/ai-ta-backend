"""Core callable used by both CLI and HTTP server.

This module centralizes the "answer a question" path so we can reuse it from
the terminal CLI and the FastAPI server. It intentionally stays dependency‑light
and uses the existing retriever pipeline.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence, Union

from .retriever import (
    load_assets,
    load_assets_all,
    search,
    pack_context,
    answer as retriever_answer,
    render_citations,
)
from .main_ai import normalize_query


def _ensure_assets(doc_sets: Optional[Sequence[str]]) -> None:
    """Load retrieval assets based on doc_sets or env/DEFAULT path.

    - If `doc_sets` is provided, attempt to load those indexes (supports multi).
    - Else, use env INDEX_DIR or the repository default path.
    """
    if doc_sets:
        paths = [Path(p) for p in doc_sets]
        if len(paths) > 1:
            load_assets_all(paths)
        else:
            load_assets(paths[0])
        return

    # Fallback to single default index dir (mirrors CLI behavior)
    default_index = Path(
        os.getenv(
            "INDEX_DIR",
            Path(__file__).resolve().parent / "text-embeder/my_book_index_aero",
        )
    )
    load_assets(default_index)


def answer_question(
    question: str,
    image_paths: Optional[Sequence[str]] = None,
    course_id: Optional[str] = None,
    doc_sets: Optional[Sequence[str]] = None,
) -> Union[str, Iterable[str], Iterator[str]]:
    """Answer a question using the existing retriever pipeline.

    Parameters currently accepted for future extensibility:
    - image_paths: not used yet (attachments are ignored by retriever).
    - course_id/doc_sets: optional filtering or multi-index selection.

    Returns a generator yielding the final text and a second line of citations,
    matching the current CLI output format. If a single string is preferred,
    callers may join the chunks.
    """
    if not question or not question.strip():
        return ""

    _ensure_assets(doc_sets)

    query = normalize_query(question)
    hits, _ = search(query)
    ctx = pack_context(hits)
    ans = retriever_answer(question, ctx)
    cites = render_citations(ans)

    def _gen() -> Iterator[str]:
        yield ans.text
        if cites:
            yield "\n" + cites

    return _gen()

