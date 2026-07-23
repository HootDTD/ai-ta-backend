"""Vision transcription and direct-answer utilities.

Uses OpenAI Vision API to extract text from images, with pytesseract fallback.
"""
from __future__ import annotations

import base64
import mimetypes
import os
from typing import List, Sequence

from config import models


def _file_to_data_url(path: str) -> str:
    """Read a file and return a data URL string (best-effort mime guess)."""
    try:
        with open(path, "rb") as fh:
            b = fh.read()
    except Exception:
        return ""
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        mime = "image/png"
    enc = base64.b64encode(b).decode("ascii")
    return f"data:{mime};base64,{enc}"


def vision_transcribe(image_paths: Sequence[str]) -> str:
    """Use a vision model to transcribe text/equations from images.

    Falls back to pytesseract if available. Returns a single
    whitespace-collapsed string, or empty string if nothing extracted.
    """
    if not image_paths:
        return ""

    try:
        from openai import OpenAI

        if os.getenv("OPENAI_API_KEY"):
            client = OpenAI()
            model = os.getenv("VISION_MODEL", "gpt-4o-mini")
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
        pass

    try:
        import pytesseract
        from PIL import Image
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


def vision_direct_answer(image_paths: Sequence[str], question_hint: str = "") -> str:
    """Directly answer from images using a vision model."""
    try:
        from openai import OpenAI
        if not os.getenv("OPENAI_API_KEY"):
            return ""
        client = OpenAI()
        model = os.getenv("VISION_ANSWER_MODEL") or models.MAIN_MODEL
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
