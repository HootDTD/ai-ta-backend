---
doc: indexing/ocr-core
description: The OCR provider contract, env-gated provider selection, and package re-exports.
owns:
  - ocr/provider.py
  - ocr/factory.py
  - ocr/__init__.py
  - ocr/README.md
related:
  - indexing/ocr-providers
  - knowledge/teacher-pdf-ingestion
  - apollo/provisioning/authored-sets/indexing
last_verified: 2026-07-25
stub: false
---

# ocr-core — provider contract + factory

The abstract OCR seam + env-gated selection.

## Interface

- `OCRBlock(kind, text, confidence)` — `kind` is `'text'`/`'latex'`/vendor.
- `OCRResult(blocks)` with `.fused_text` (blocks joined by `"\n\n"`) and
  `.average_confidence` (mean of present block confidences, or `None`).
- `OCRProvider` ABC — `recognize(image_bytes, mime, dpi) -> OCRResult`.
- `get_ocr_provider_from_env() -> OCRProvider | None` — returns
  `MathpixOCRProvider` when `OCR_PROVIDER=mathpix` and creds exist,
  `OpenAIVisionOCRProvider` when `OCR_PROVIDER=openai`, else `None`.
- `__init__.py` re-exports `get_ocr_provider_from_env`, `OCRProvider`,
  `OCRResult`, `OCRBlock`.

## Invariants & gotchas

- **The factory is intentionally NOT wired into the live weekly upload runtime**
  (README): [knowledge/teacher-pdf-ingestion](../knowledge/teacher-pdf-ingestion.md)
  constructs Mathpix directly via its own `build_teacher_mathpix_provider`. The
  factory serves authored-set indexing + tests.
- `teacher_pdf_ingestion` accepts ANY `OCRProvider` at its `mathpix_provider`
  param and only ever calls `.recognize()` — the seam is honored.

## Env flags

- `OCR_PROVIDER` (`mathpix`/`openai`; unknown/empty → disabled).

## Related

- [ocr-providers](ocr-providers.md) (the concrete implementations),
  [knowledge/teacher-pdf-ingestion](../knowledge/teacher-pdf-ingestion.md),
  [apollo/provisioning/authored-sets/indexing](../apollo/provisioning/authored-sets/indexing.md).
