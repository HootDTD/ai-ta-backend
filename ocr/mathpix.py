from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .provider import OCRBlock, OCRResult, OCRProvider


class MathpixConfig(BaseModel):
    app_id: str = Field(..., description="Mathpix APP ID")
    app_key: str = Field(..., description="Mathpix APP KEY")
    endpoint: str = Field(
        default="https://api.mathpix.com/v3/text",
        description="Mathpix OCR endpoint",
    )
    dpi: Optional[int] = Field(default=None, description="Requested DPI override")


def _b64_data_url(image_bytes: bytes, mime: Optional[str]) -> str:
    enc = base64.b64encode(image_bytes).decode("ascii")
    mt = mime or "image/png"
    return f"data:{mt};base64,{enc}"


class MathpixOCRProvider(OCRProvider):
    """Mathpix-backed OCR implementation.

    Converts images to data URLs and posts JSON to the Mathpix API. Parses
    a minimal subset of fields into OCRResult, creating 'text' and 'latex' blocks
    when present. Confidence is averaged from available fields.
    """

    def __init__(self, config: MathpixConfig):
        self._cfg = config

    def recognize(self, image_bytes: bytes, mime: str | None = None, dpi: int | None = None) -> OCRResult:
        data_url = _b64_data_url(image_bytes, mime)
        payload: Dict[str, Any] = {
            "src": data_url,
            # request both plain text and LaTeX
            "formats": ["text", "latex_styled"],
        }
        eff_dpi = dpi if dpi is not None else self._cfg.dpi
        if eff_dpi:
            payload["ocr_dpi"] = int(eff_dpi)

        req = urllib.request.Request(
            self._cfg.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "app_id": self._cfg.app_id,
                "app_key": self._cfg.app_key,
            },
            method="POST",
        )

        with urllib.request.urlopen(req) as resp:  # type: ignore[call-arg]
            raw = resp.read()
        try:
            doc = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # pragma: no cover - defensive
            raise RuntimeError("Mathpix returned non-JSON response") from exc

        blocks: List[OCRBlock] = []
        # Prefer plain text as one block when provided
        if isinstance(doc.get("text"), str) and doc["text"].strip():
            blocks.append(
                OCRBlock(kind="text", text=str(doc["text"]).strip(), confidence=_extract_confidence(doc))
            )
        # Add LaTeX-styled output as a separate block if present
        latex_field = doc.get("latex_styled") or doc.get("latex")
        if isinstance(latex_field, str) and latex_field.strip():
            blocks.append(
                OCRBlock(kind="latex", text=str(latex_field).strip(), confidence=_extract_confidence(doc, prefer="latex"))
            )

        return OCRResult(blocks=blocks)


def _extract_confidence(doc: Dict[str, Any], prefer: str | None = None) -> Optional[float]:
    """Heuristic extraction of confidence from Mathpix JSON.

    If a top-level 'confidence' exists and is numeric, use it. Otherwise, try
    latex-specific fields. Returns None if unavailable.
    """
    cand = doc.get("confidence")
    if isinstance(cand, (int, float)):
        val = float(cand)
        if 0.0 <= val <= 1.0:
            return val
    if prefer == "latex":
        for key in ("latex_confidence", "confidence_latex"):
            v = doc.get(key)
            if isinstance(v, (int, float)):
                val = float(v)
                if 0.0 <= val <= 1.0:
                    return val
    return None


def config_from_env() -> Optional[MathpixConfig]:
    app_id = os.environ.get("MATHPIX_APP_ID")
    app_key = os.environ.get("MATHPIX_APP_KEY")
    if not app_id or not app_key:
        return None
    endpoint = os.environ.get("MATHPIX_ENDPOINT", "https://api.mathpix.com/v3/text")
    dpi_raw = os.environ.get("OCR_DPI")
    dpi: Optional[int] = None
    try:
        dpi = int(dpi_raw) if dpi_raw else None
    except (TypeError, ValueError):
        dpi = None
    return MathpixConfig(app_id=app_id, app_key=app_key, endpoint=endpoint, dpi=dpi)

