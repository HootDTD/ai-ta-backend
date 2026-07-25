---
doc: database/transforms
description: Pure column-value coercion helpers used at the ORM/service boundary.
owns:
  - database/transforms.py
related:
  - database/models
  - chats/service
  - knowledge/teacher-weekly
last_verified: 2026-07-25
stub: false
---

# database/transforms — column coercion

Pure boundary coercion helpers, kept out of [models](models.md) to keep it a
shape reference.

## Interface

- `chat_keywords_to_text_array(value) -> list[str]` and
  `chat_keywords_to_json_array(value) -> str` — the `chat_messages.keywords`
  ≤8-term list (migration 029, write-only).
- `merge_course_settings(search_weights, teacher_weights, weight_bounds,
  current_week) -> dict` — merges/normalizes `app.courses` retrieval settings
  (teacher weights win; week clamped to 1..16).
- `document_status_columns(value) -> (state, failure_reason|None)` — splits a
  legacy status envelope into DB-07's typed columns (`pending` → `queued`;
  unknown → `ready`).
- `normalize_upload_status(value)` / `normalize_upload_job_state(value)` —
  validate against allowed frozensets via `_checked_state`.
- `_bounded_float(value, *, default)` — clamps a numeric to `[0, 1]`.

## Invariants & gotchas

- **Unknown upload status/job-state strings RAISE** (`_checked_state`) — they are
  never silently coerced (fail fast). `document_status_columns`, by contrast,
  coerces an unknown state to `"ready"`.
- `DOCUMENT_STATUSES` / `UPLOAD_STATUSES` / `UPLOAD_JOB_STATES` are the frozensets.

## Related

- [models](models.md) (column defaults), [chats/service](../chats/service.md),
  [knowledge/teacher-weekly](../knowledge/teacher-weekly.md) (consumers).
