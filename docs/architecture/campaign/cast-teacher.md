---
doc: campaign/cast-teacher
description: Teacher-side provisioning driver for both curriculum arms — seeded incumbents and the real WU-AAS authored path.
owns:
  - campaign/cast/teacher.py
related:
  - campaign/cast-subjects-materials
  - apollo/provisioning/authored-sets/api
  - campaign/scripts-diff-eval
last_verified: 2026-07-25
stub: false
---

# campaign/cast-teacher — provisioning driver

Sets up the course/problem bank the student cast then teaches against. Both
verbs are pure request/flow logic over injected seams (subprocess runner, httpx
client, sleep) so they unit-test without Docker or a live backend; only the real
seam implementations are pragma-excluded.

## Interface

- Errors: `SeedProvisioningError`, `AuthoredProvisioningError`,
  `AuthoredProvisioningTimeout`.
- **Seeded arm:** `provision_seeded(subject_key, dsn, *, run_subprocess,
  project_canon) -> SeedProvisionResult` — replays the filesystem-registry
  seeding scripts as subprocess steps (`_run_step` / `_default_run_subprocess`):
  `seed_apollo_concept_registry`, `seed_apollo_learner_model --subject-slug …`,
  and (unless `project_canon=False`) `seed_canon_projection`. Returns
  `SeedStepResult`/`SeedProvisionResult`; raises on the first non-zero exit.
- **Authored arm:** `provision_authored(*, client, base_url, teacher_token,
  search_space_id, problem_pdf, solution_pdf, …) -> AuthoredProvisionResult` —
  the REAL WU-AAS path: multipart POST to `/apollo/authored-sets`,
  `_poll_until_terminal` on `GET .../authored-sets/{set_id}`, then approve every
  held (`review_required`) problem. `_minted_and_held_ids` splits minted vs held.

## Invariants & gotchas

- Terminal statuses are `{done, failed}`; a non-`done` finish raises
  `AuthoredProvisioningError` with the recorded diagnostic; timeout raises
  `AuthoredProvisioningTimeout`.
- `SEEDED_SUBJECTS` is the registry ([cast-subjects-materials](cast-subjects-materials.md)).

## Related

- [cast-subjects-materials](cast-subjects-materials.md) (subject registry),
  [apollo/provisioning/authored-sets/api](../apollo/provisioning/authored-sets/api.md)
  (the real teacher HTTP surface), [scripts-diff-eval](scripts-diff-eval.md).
