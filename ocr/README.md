Pluggable OCR Layer

Environment flags (no runtime wiring yet):

- OCR_PROVIDER: set to `mathpix` to enable the Mathpix adapter.
- MATHPIX_APP_ID: your Mathpix App ID.
- MATHPIX_APP_KEY: your Mathpix App Key.
- OCR_DPI: optional integer DPI hint (e.g., `300`).

Usage in code (example):

```
from ocr import get_ocr_provider_from_env

prov = get_ocr_provider_from_env()
if prov:
    result = prov.recognize(image_bytes, mime="image/png")
    print(result.fused_text, result.average_confidence)
```

This package is intentionally not wired into existing flows yet, per constraints.

