---
doc: knowledge/_index
description: Router for teacher course-material ingestion — the pipeline that produces the documents retrieval reads.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# knowledge — teacher course-material ingestion

Teachers upload weekly notes/slides (week-gated) and course-wide textbooks; a
durable leased job queue drives async ingestion into `app.documents` /
`internal.document_chunks` via the checkpointed indexer; week gating controls
student visibility. Carved out of the retired domain-data.

## Leaves
| Doc | One-liner · owns |
|---|---|
| [teacher-weekly](teacher-weekly.md) | TeacherWeeklyStorage: uploads, week/weight controls, leased job queue, worker loop · `knowledge/teacher_weekly.py`, `knowledge/__init__.py` |
| [teacher-pdf-ingestion](teacher-pdf-ingestion.md) | TeacherPDFIngestor: PyMuPDF extraction + Mathpix OCR fallback + dedupe · `knowledge/teacher_pdf_ingestion.py` |

## Cross-cutting invariants
- Week gating for student visibility is owned by
  `rag-pipeline/document-visibility`; this domain only sets `Course.current_week`
  and the `Upload`/`Document` status the predicate reads.
- **Cross-domain**: `TeacherPDFIngestor` / `NormalizedPage` are also imported by
  the apollo authored-sets provisioning pipeline (`apollo/provisioning/_index`).

## Related domains
`indexing/_index` (checkpoint indexer + embedder), `rag-pipeline/document-visibility`
(week gating), `apollo/provisioning/_index`, `platform/vendor-supabase-storage`.
