from __future__ import annotations

import os
from typing import Optional

from .provider import OCRProvider
from .mathpix import MathpixOCRProvider, config_from_env


def get_ocr_provider_from_env() -> Optional[OCRProvider]:
    """Return an OCRProvider instance based on environment flags.

    - OCR_PROVIDER=mathpix selects Mathpix if credentials are present.
    - OCR_DPI may be used by providers that support it.
    - Returns None when no valid provider is configured.
    """
    provider = (os.environ.get("OCR_PROVIDER") or "").strip().lower()
    if provider == "mathpix":
        cfg = config_from_env()
        if cfg is not None:
            return MathpixOCRProvider(cfg)
        return None
    if provider == "openai":
        from .openai_vision import OpenAIVisionOCRProvider

        return OpenAIVisionOCRProvider.from_env()
    # Unknown or empty provider -> disabled
    return None
