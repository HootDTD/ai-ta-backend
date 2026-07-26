---
doc: knowledge/teacher-pdf-ingestion
description: TeacherPDFIngestor — native-first PDF extraction with selective Mathpix OCR fallback and fuzzy dedupe.
owns:
  - knowledge/teacher_pdf_ingestion.py
related:
  - knowledge/teacher-weekly
  - indexing/ocr-providers
  - indexing/ocr-core
  - apollo/provisioning/_index
last_verified: 2026-07-25
stub: false
---

# knowledge/teacher-pdf-ingestion — PDF → page/chunk items

~700 lines. PyMuPDF (`fitz`) native extraction with a heuristic Mathpix OCR
fallback.

## Interface

- `class TeacherPDFIngestor(config=None, *, mathpix_provider=None)` with
  `ingest(pdf_path, *, doc_id, upload_page_asset=None) -> TeacherPDFIngestionResult`
  (`items`, `source_markdown`, `pages`, `page_count`, `ocr_summary`,
  `artifact_manifest`, `warnings`).
- Config `TeacherPDFIngestionConfig` (`from_env`, `render_dpi`).
- Dataclasses `NativeBlock` / `PageHeuristic` / `NormalizedRegion` /
  `NormalizedPage` / `TeacherPDFIngestionResult`.
- Module functions `build_teacher_mathpix_provider(render_dpi)`,
  `choose_mathpix_strategy(...)`, `merge_page_models(...)`.

## Data flow

PyMuPDF native text per page → per-page heuristics (low text / image-dominant /
equation-like via `_math_symbol_ratio`) trigger Mathpix OCR
(`indexing/ocr-providers`) → `merge_page_models` reconciles native + OCR with
fuzzy trigram dedupe (`_char_trigrams` / `_fuzzy_similar` / `_dedupe_key`,
threshold 0.75) → page → chunk items.

## Invariants & gotchas

- **Native heading detection is conservative**: short ≥14 pt lines, short bold
  body-size lines, and `_NUMBERED_OUTLINE_RE` numbered headers become headings —
  but numbered questions (long / question-terminated / answer-followed via
  `_ANSWER_LINE_RE` / inside a `_SAMPLE_QUESTIONS_RE` list) stay BODY chunks so
  authored study guides do not gain false sections.
- **Cross-domain consumer**: `TeacherPDFIngestor` + `NormalizedPage` are imported
  by the apollo authored-sets provisioning pipeline (`apollo/provisioning/_index`),
  not only by the weekly worker.

## Env flags

`TEACHER_UPLOAD_RENDER_DPI`, `MATHPIX_ENDPOINT` (+ Mathpix creds via
`indexing/ocr-providers`).

## Related

`knowledge/teacher-weekly` (worker caller), `indexing/ocr-providers`,
`indexing/ocr-core`, `apollo/provisioning/_index`.
