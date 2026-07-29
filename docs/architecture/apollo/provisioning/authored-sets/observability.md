---
doc: apollo/provisioning/authored-sets/observability
description: Ingest-audit writes for the authored-set path — content-ingest run, per-page OCR evidence, terminal counts, stage errors
owns:
  - apollo/provisioning/authored_sets/observability.py
related:
  - apollo/provisioning/authored-sets/_index
  - apollo/provisioning/authored-sets/api
  - apollo/provisioning/metered-chat
  - apollo/persistence/models
last_verified: 2026-07-25
stub: false
---

## Interface

- `start_ingest_run(db, *, search_space_id, document_id, content_hash=None)` →
  `IngestRun` opened `status='running'` (flushed so `run.id` exists).
- `persist_page_evidence(db, *, ingest_run, search_space_id, document_id, role,
  pages, conf_threshold=DEFAULT_CONF_THRESHOLD)` → count of page rows written.
- `finalize_ingest_run(db, *, ingest_run, status, n_pages=None,
  n_questions_scraped=None, n_promoted=None, n_rejected=None)`.
- `record_ingest_error(db, *, search_space_id, ingest_run, stage, exc, context=None)`.
- `page_ocr_text`, `DEFAULT_CONF_THRESHOLD` (0.6). All consumed by `api.py` and
  `problem_generation/api.py`.

## Data flow

The authored-set ingest path historically wrote nothing to the audit tables, so the
S2 audit ran on absent inputs. This module opens one `internal.content_ingest_runs`
row (MeteredChat accrues token/cost on the SAME row for free), writes one
`internal.ingest_page_evidence` row per source page (recognized OCR text +
self-reported confidence + extraction mode + the `verify_path_fired` low-confidence
flag the S2 contract reads), stamps terminal status/timing + scraped/promoted/rejected
counts on finalize, and records one `internal.content_ingest_errors` row per stage
failure.

## Invariants & gotchas

- **Every write flushes but does NOT commit** — the caller (`api._run_set_background`
  / the generation background task) owns the transaction boundary.
- `document_id` is nullable (problem-generation and pre-index failures have no source
  document); `finalize_ingest_run` writes only the non-None counts a run knows.
- `verify_path_fired` is set when a page's confidence is present and ≤ the threshold.
- `page_ocr_text` duck-types `NormalizedPage` (`plain_text`/`latex_text`); an object
  exposing neither yields `""`. Error context is a bounded, PII-free summary — never
  raw OCR/LLM bodies.

## Related

Persistence ORM `IngestRun`/`IngestError`/`IngestPageEvidence`; `MeteredChat` accrues
usage on the same run row (metered-chat).
