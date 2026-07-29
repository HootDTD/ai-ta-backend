---
doc: rag-pipeline/citations-formatter
description: Source-labeling and structured-citation builder for the answer response (+ DOC_TYPE_LABELS).
owns:
  - citations/formatter.py
  - citations/__init__.py
related:
  - rag-pipeline/context-packer
  - rag-pipeline/main-ai
  - platform/http-server
last_verified: 2026-07-26
stub: false
---

# citations-formatter — response-side structured citations

Ownership moved here from the old `_overview.md` (`citations/**` is no longer
`_overview`-owned). `citations/__init__.py` re-exports `build_citation_info` and
`format_citations` (not `DOC_TYPE_LABELS`).

## Interface

- `build_citation_info(snippet, row, store_meta) -> dict` — `doc_type, file, page,
  bbox, week, verified, label, store_key, ocr_*, page_asset, raw_latex`.
- `format_citations(entries, id_to_row, store_meta) -> (labels, structured)` —
  used by server.py to build response citation objects (dedupes on
  `(doc_type, file, page)`).
- Module constant `DOC_TYPE_LABELS` (store-kind → display label) — imported by
  `retrieval/context_packer` for retrieval-time markers.

## Data flow

`doc_type` from `meta_entry.kind` or `store_kind` via `_doc_type_label`. A present
`snippet.doc_title` (first 30 characters) replaces the type in label shapes,
including `[Title]` without a page; untitled labels remain `[Notes, Week N, p. X]`
for notes/slides with `week>0`, else `[Type, p. X]`. `_normalize_page` and
`_normalize_bbox` coerce, with NaN guards on `store_key`/`store_kind`.

## Invariants & gotchas

- **`verified=True` ONLY for Textbook sources.**
- **Two distinct citation surfaces:** retrieval-time markers (`context-packer`,
  via `DOC_TYPE_LABELS`) vs answer-response structured citations
  (`format_citations`, at server.py).

## Related

`context-packer`, `main-ai` (`format_answer` marker enforcement),
`platform/http-server` (response assembly).
