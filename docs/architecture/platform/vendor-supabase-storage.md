---
doc: ai-ta-backend/platform/vendor-supabase-storage
description: vendors/supabase_storage.py — a thin REST client for Supabase Storage (ensure/upload/download objects) used by the teacher-upload path
owns:
  - vendors/supabase_storage.py
related:
  - ai-ta-backend/knowledge/teacher-weekly
last_verified: 2026-07-25
stub: false
---

# platform/vendor-supabase-storage — Storage REST client

## Interface

- `SupabaseStorageClient(*, base_url=None, api_key=None)` — resolves the base
  URL from `SUPABASE_URL` and the key preferring `SUPABASE_SERVICE_ROLE_KEY`,
  falling back to `SUPABASE_API_KEY`/`SUPABASE_ANON_KEY`; raises if either is
  missing.
- `ensure_bucket(*, bucket, public=False, timeout=30)` — `POST /storage/v1/bucket`
  (private by default).
- `upload_bytes(*, bucket, object_key, data, content_type, upsert=False)` and
  `download_bytes(*, bucket, object_key)`.
- Internal `_headers()`, `_object_url(...)`.

## Data flow

The teacher-upload/indexing path (`knowledge`) uses this to persist uploaded
PDFs and artifacts to Storage.

## Invariants & gotchas

- **`ensure_bucket` tolerates already-exists** (409, or a 400 whose body says
  "already exists"/"duplicate") — buckets are app-owned env constants, so
  auto-create removes the manual per-environment setup step (the one that left a
  fresh staging project with zero buckets) without masking user typos.

## Env flags

`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (preferred), `SUPABASE_API_KEY`,
`SUPABASE_ANON_KEY`.

## Related

`knowledge/teacher-weekly` (the upload-storage caller).
