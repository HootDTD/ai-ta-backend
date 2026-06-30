"""OpenAI vision OCR provider (WU-AAS).

Transcribes a rendered page image to text/LaTeX with a self-reported confidence,
behind the OCRProvider seam. Chosen for handwritten solutions; reuses
OPENAI_API_KEY. A non-JSON or failed response yields an empty result, so a
degraded page is a per-page no-op.
"""

from __future__ import annotations

import base64
import json
import logging
import os

from ocr.provider import OCRBlock, OCRProvider, OCRResult

_LOG = logging.getLogger(__name__)

_SYSTEM = (
    "You are an OCR engine for math/STEM worksheet and solution pages, including "
    "handwriting. Transcribe ALL visible content faithfully to Markdown with LaTeX for "
    "math ($...$ inline, $$...$$ display). Do NOT solve, summarize, or add anything not "
    "on the page. Respond ONLY as JSON: "
    '{"text": "<transcription>", "confidence": <0..1 how legible/certain you are>}.'
)
_DEFAULT_MODEL = "gpt-4o"


def _data_url(image_bytes: bytes, mime: str | None) -> str:
    enc = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime or 'image/png'};base64,{enc}"


class OpenAIVisionOCRProvider(OCRProvider):
    def __init__(self, *, client=None, model: str | None = None) -> None:
        self._client = client
        self._model = model or _DEFAULT_MODEL

    @classmethod
    def from_env(cls) -> OpenAIVisionOCRProvider:
        model = (os.getenv("APOLLO_OCR_MODEL") or _DEFAULT_MODEL).strip()
        return cls(client=None, model=model)

    def _ensure_client(self):
        if self._client is None:  # pragma: no cover - real OpenAI client construction
            from openai import OpenAI

            self._client = OpenAI()
        return self._client

    def recognize(
        self, image_bytes: bytes, mime: str | None = None, dpi: int | None = None
    ) -> OCRResult:
        try:
            client = self._ensure_client()
            resp = client.chat.completions.create(
                model=self._model,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Transcribe this page."},
                            {
                                "type": "image_url",
                                "image_url": {"url": _data_url(image_bytes, mime)},
                            },
                        ],
                    },
                ],
            )
            raw = resp.choices[0].message.content or ""
            data = json.loads(raw)
            text = str(data.get("text") or "").strip()
            conf = data.get("confidence")
            conf = float(conf) if isinstance(conf, (int, float)) else 0.0
            conf = max(0.0, min(1.0, conf))
        except Exception as exc:
            _LOG.warning(
                "openai_vision_ocr_failed",
                extra={"event": "openai_vision_ocr_failed", "error": str(exc)},
            )
            return OCRResult(blocks=[])
        if not text:
            return OCRResult(blocks=[])
        return OCRResult(blocks=[OCRBlock(kind="latex", text=text, confidence=conf)])
