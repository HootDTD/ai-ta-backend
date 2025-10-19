from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from pydantic import BaseModel, Field


class OCRBlock(BaseModel):
    kind: str = Field(..., description="Block type: 'text' | 'latex' | vendor-specific")
    text: str = Field(..., description="Recognized textual content")
    confidence: Optional[float] = Field(
        default=None, description="Block-level confidence in [0,1] if available"
    )


class OCRResult(BaseModel):
    blocks: List[OCRBlock] = Field(default_factory=list)

    @property
    def fused_text(self) -> str:
        """Concatenate blocks in order, separated by two newlines."""
        return "\n\n".join(b.text for b in self.blocks if b.text)

    @property
    def average_confidence(self) -> Optional[float]:
        vals = [b.confidence for b in self.blocks if b.confidence is not None]
        if not vals:
            return None
        return float(sum(vals) / len(vals))


class OCRProvider(ABC):
    """Abstract provider for OCR/vision transcription.

    Implementations should be stateless and safe to construct on demand.
    """

    @abstractmethod
    def recognize(self, image_bytes: bytes, mime: str | None = None, dpi: int | None = None) -> OCRResult:
        """Return OCRResult for the given image.

        - image_bytes: raw bytes of the image (PNG/JPEG)
        - mime: optional hint (e.g., 'image/png')
        - dpi: dots-per-inch hint for providers that accept it
        """
        raise NotImplementedError

