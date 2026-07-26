---
doc: rag-pipeline/document-visibility
description: active_document_conditions — the single source of truth for week-gated student-visible documents.
owns:
  - retrieval/document_visibility.py
related:
  - rag-pipeline/hybrid-search
  - chats/bundle-cache
  - platform/workspaces
  - knowledge/teacher-weekly
  - database/models
last_verified: 2026-07-25
stub: false
---

# document-visibility — shared visibility predicate

A cross-cutting SQL-predicate helper deciding which documents are searchable —
imported by three domains, so it is the single source of truth for "what
students can retrieve".

## Interface

- `active_document_conditions(search_space_id) -> list[conditions]` — SQLAlchemy
  filter conditions.
- `build_chunk_metadata(document_metadata, page_number) -> dict` — flattens
  per-page debug/OCR/asset metadata; `_page_debug_entry` is internal.
- `WEEKLY_UPLOAD_KINDS = ("notes", "slides")`.

## Data flow

`active_document_conditions` returns: `Document.course_id == search_space_id`
AND `Document.status == "ready"` AND a week gate — a document is visible when its
`week` is NULL, or its `material_kind` is not weekly, or a latest ready `Upload`
row for it exists with `Upload.week <= Course.current_week` (a correlated
`EXISTS`). Consumers: `hybrid_search` (visible-id array + FTS filter),
`chats/bundle_cache` (visible-docs fingerprint), `workspaces/db` (cross-domain).

## Invariants & gotchas

- **Week gating here is authoritative.** Textbooks store `week=NULL`, so they
  stay visible every week once ready; weekly notes/slides appear only when their
  `Upload.week` has been reached.
- Uses the post-#194 ORM `Course`/`Document`/`Upload` (not `aita_*`/`TeacherUpload`).

## Related

`hybrid-search`, `chats/bundle-cache`, `platform/workspaces`,
`knowledge/teacher-weekly` (sets `current_week` + upload status).
