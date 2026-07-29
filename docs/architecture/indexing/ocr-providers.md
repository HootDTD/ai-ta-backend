---
doc: indexing/ocr-providers
description: The two concrete OCRProvider implementations — Mathpix and OpenAI vision.
owns:
  - ocr/mathpix.py
  - ocr/openai_vision.py
related:
  - indexing/ocr-core
  - knowledge/teacher-pdf-ingestion
last_verified: 2026-07-25
stub: false
---

# ocr-providers — Mathpix + OpenAI vision

Two concrete `OCRProvider`s behind the [ocr-core](ocr-core.md) contract.

## Interface

- `MathpixOCRProvider(config)` — POSTs a base64 data-URL JSON body to
  `https://api.mathpix.com/v3/text` via **stdlib `urllib`** (no `requests`),
  requesting `formats=['text','latex_styled']`, and parses plain-text + LaTeX
  into separate `OCRBlock`s. `MathpixConfig` BaseModel + `config_from_env()`
  reads `MATHPIX_APP_ID`, `MATHPIX_APP_KEY`, `MATHPIX_ENDPOINT`, `OCR_DPI`.
  `_extract_confidence` normalizes Mathpix's confidence shapes.
- `OpenAIVisionOCRProvider(*, client=None, model=None)` — Chat Completions vision
  OCR for rendered page images: sends the page as a base64 data URL, requests
  JSON `{text, confidence}`, returns one LaTeX-flavored `OCRBlock`. `from_env()`
  reads `APOLLO_OCR_MODEL` (default `gpt-4o`) and reuses `OPENAI_API_KEY`.

## Invariants & gotchas

- **`OpenAIVisionOCRProvider` fails soft to an empty `OCRResult`** on malformed
  or provider errors (and on empty transcription), so a degraded page is a
  per-page no-op — never a hard failure.
- Downstream (applied by the teacher caller, not here): an OCR
  `average_confidence < 0.4` (`TEACHER_MIN_OCR_CONFIDENCE`) discards the text.

## Env flags

- `MATHPIX_APP_ID`, `MATHPIX_APP_KEY`, `MATHPIX_ENDPOINT`, `OCR_DPI`,
  `APOLLO_OCR_MODEL`.

## Related

- [ocr-core](ocr-core.md), [knowledge/teacher-pdf-ingestion](../knowledge/teacher-pdf-ingestion.md).
