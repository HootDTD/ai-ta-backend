---
doc: apollo/provisioning/concepts-api
description: Teacher-facing concept-authoring HTTP router — write a course's concept list directly.
owns:
  - apollo/provisioning/concepts_api.py
related:
  - apollo/conversation/routing/auth-deps
  - apollo/provisioning/tag-mint
  - apollo/provisioning/concept-match
  - apollo/provisioning/scrape
last_verified: 2026-07-25
stub: false
---

# provisioning/concepts-api

Teacher-facing concept-authoring HTTP router (WU-TCA): teachers write their course's concept
list directly (name + description) as bare `app.concepts` rows. `router` is mounted in
`apollo/api.py` (see `provisioning/_index`).

## Interface

- `router` — the FastAPI router (mounted apollo-wide).
- Endpoints: `list_teacher_concepts`, `create_teacher_concept`, `update_teacher_concept`, `delete_teacher_concept`.
- `mint_slug(display_name) -> str`, `ConceptCreateBody`, `ConceptUpdateBody`.

## Data flow

An authored concept is a first-class `app.concepts` row: the reversed-provisioning matcher
targets every non-provisional concept, and the student browse list only surfaces a concept once
a teachable problem attaches. Creation reuses `tag_mint_persist.resolve_or_create_concept` (key
on the BIGINT id) and then fills the `description` column the matcher prompt renders; the slug
is stable across renames (provisioned problems, KG entities, and match decisions key on it).

## Invariants & gotchas

- **Every route is teacher-gated (`require_course_teacher`) and course-scoped.** Creation 400s
  on a reserved/empty slug and 409s when a normalized slug already exists (retyping a name edits,
  never silently merges).
- **Deletion is conservative** — a concept with ANY problems or KG entities 409s; teardown
  belongs to the authored-set delete flow, not this editor.
- The `provisional.inventory` row is a scrape seam, not curriculum — it is invisible here (404,
  not 403, to avoid leaking its existence).

## Related

- `apollo/conversation/routing/auth-deps` — `require_user` / `require_course_teacher`.
- `provisioning/tag-mint` — `resolve_or_create_concept` (reused from `tag_mint_persist`).
- `provisioning/concept-match` — `norm_slug` (shared slug key).
- `provisioning/scrape` — `PROVISIONAL_CONCEPT_SLUG`.
