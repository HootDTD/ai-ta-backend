---
doc: indexing/connector-document
description: AITAConnectorDocument input DTO plus its NUL-sanitization helper at the pipeline boundary.
owns:
  - indexing/connector_document.py
  - indexing/text_sanitization.py
related:
  - indexing/chunking-embedding
  - indexing/indexing-service
  - database/models
last_verified: 2026-07-25
stub: false
---

# connector-document — input DTO + NUL sanitization

`AITAConnectorDocument` is the Pydantic DTO every material enters the indexing
pipeline as, paired with the NUL-stripping helper it validates through.

## Interface

- `AITAConnectorDocument` fields: `title`, `source_markdown`, `unique_id`,
  `document_type` (default `"EDUCATIONAL_FILE"`), `search_space_id`
  (`gt=0` — the course; still the external contract key even though the ORM is
  `Course`/`app.courses`), `material_kind`, `should_summarize`, `page_count`,
  `week` (`None` = permanent material), `metadata`.
- `VALID_MATERIAL_KINDS = {textbook, slides, homework, exams, notes, other}`.
- `text_sanitization.strip_nul(text) -> str` and
  `sanitize_jsonable(value) -> value` (recurses dicts/lists, never mutates input).

## Data flow

Produced by the upload pipeline; consumed by `prepare_for_indexing`
(hashing keys) and the chunker. `material_kind` drives downstream retrieval
store-bias.

## Invariants & gotchas

- **NUL stripped, never rejected.** `title`/`source_markdown`/`unique_id`
  validators call `strip_nul`; `metadata` runs through `sanitize_jsonable`.
  Postgres TEXT/JSONB reject `\x00`, and PyMuPDF/Mathpix emit it on scanned PDFs
  (a 873-page scanned textbook historically killed the INSERT). `text_sanitization`
  is applied at TWO chokepoints — here and in `document_chunker`.
- **Invalid `material_kind` silently coerced to `"other"`**, never rejected.
- `title`/`source_markdown`/`unique_id` must be non-empty after stripping.

## Related

- [chunking-embedding](chunking-embedding.md) (the other `text_sanitization`
  chokepoint), [indexing-service](indexing-service.md) (consumer),
  [database/models](../database/models.md) (`material_kind` → `Document`).
