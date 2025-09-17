"""Core callable used by both CLI and HTTP server.

This module centralizes the "answer a question" path so we can reuse it from
the terminal CLI and the FastAPI server. It intentionally stays dependency‑light
and uses the existing retriever pipeline.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence, Union, List
import base64
import mimetypes
import json

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


def _file_to_data_url(path: str) -> str:
    """Read a file and return a data URL string (best-effort mime guess)."""
    try:
        with open(path, "rb") as fh:
            b = fh.read()
    except Exception:
        return ""
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        # Reasonable default for screenshots
        mime = "image/png"
    enc = base64.b64encode(b).decode("ascii")
    return f"data:{mime};base64,{enc}"


def _vision_transcribe(image_paths: Sequence[str]) -> str:
    """Use a vision model to transcribe text/equations from images into plain text.

    Falls back to pytesseract if available. Returns a single whitespace‑collapsed
    string, or an empty string if no text could be extracted.
    """
    if not image_paths:
        return ""

    # Try OpenAI Vision first if API key present
    try:
        from openai import OpenAI  # type: ignore

        if os.getenv("OPENAI_API_KEY"):
            client = OpenAI()
            model = os.getenv("VISION_MODEL", "gpt-4o-mini")
            # Build multi-part user content with images
            content: List[dict] = [{"type": "text", "text": (
                "Transcribe all readable text, symbols, and equations in these images. "
                "Return ONLY plain text suitable for search. No commentary."
            )}]
            for p in image_paths:
                url = _file_to_data_url(p)
                if not url:
                    continue
                content.append({"type": "image_url", "image_url": {"url": url}})
            if len(content) > 1:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You transcribe images into clean text."},
                        {"role": "user", "content": content},
                    ],
                    temperature=0,
                )
                text = resp.choices[0].message.content or ""
                text = " ".join((text or "").split())
                if text:
                    return text
    except Exception:
        # Ignore and fall back
        pass

    # Fallback: pytesseract OCR if available
    try:  # optional deps
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return ""

    parts: List[str] = []
    for p in image_paths:
        try:
            img = Image.open(p)
            txt = pytesseract.image_to_string(img) or ""
            clean = " ".join(txt.split())
            if clean:
                parts.append(clean)
        except Exception:
            continue
    return "\n".join(parts).strip()


def _vision_direct_answer(image_paths: Sequence[str], question_hint: str = "") -> str:
    """Directly answer from images using a vision model, when no text was extracted.

    This provides a graceful image-only fallback without retrieval context. """
    try:
        from openai import OpenAI  # type: ignore
        if not os.getenv("OPENAI_API_KEY"):
            return ""
        client = OpenAI()
        model = os.getenv("VISION_ANSWER_MODEL", os.getenv("MAIN_MODEL", "gpt-4o"))
        # Build content
        content: List[dict] = []
        opener = (
            "Solve the problem shown in the images. "
            "Show steps, state assumptions, and give the final answer clearly."
        )
        if question_hint and question_hint.strip():
            opener += f"\nHint: {question_hint.strip()}"
        content.append({"type": "text", "text": opener})
        for p in image_paths:
            url = _file_to_data_url(p)
            if url:
                content.append({"type": "image_url", "image_url": {"url": url}})

        if len(content) <= 1:
            return ""
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful STEM tutor."},
                {"role": "user", "content": content},
            ],
            temperature=0,
        )
        return resp.choices[0].message.content or ""
    except Exception:
        return ""


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
    # Build an effective query possibly augmented by image transcription
    q = (question or "").strip()
    image_paths = list(image_paths or [])

    image_text = ""
    if image_paths:
        try:
            # Vision-first; OCR fallback
            if os.getenv("VISION_EXTRACT", "on").lower() not in {"0", "off", "false", "no"}:
                image_text = _vision_transcribe(image_paths)
        except Exception:
            image_text = ""

    # Compose final question used for prompting and retrieval
    if q and image_text:
        combined_q = q.rstrip() + "\n\n[Image text]\n" + image_text
    elif q:
        combined_q = q
    elif image_text:
        combined_q = image_text
    else:
        # As a last resort, attempt a direct vision answer
        if image_paths:
            direct = _vision_direct_answer(image_paths)
            if direct:
                return direct
        return ""

    _ensure_assets(doc_sets)

    query = normalize_query(combined_q)
    hits, _ = search(query)
    ctx = pack_context(hits)
    ans = retriever_answer(combined_q, ctx)
    cites = render_citations(ans)

    def _gen() -> Iterator[str]:
        yield ans.text
        if cites:
            yield "\n" + cites

    return _gen()
