---
doc: campaign/cast-subjects-materials
description: Campaign subject registry plus the substantive PDF-material fixture generator (OWNED, not a pytest fixture).
owns:
  - campaign/cast/subjects.py
  - campaign/cast/materials/generate_fixtures.py
related:
  - campaign/cast-teacher
  - campaign/cast-personas
last_verified: 2026-07-25
stub: false
---

# campaign/cast-subjects-materials — registry + PDF fixtures

Subject registry + PDF material fixtures. The registry drives which
personas/materials each judge run covers.

## Interface

- `subjects.py`: `SeededSubject` and `AuthoredSubject` descriptors,
  `materials_dir() -> Path`, `all_subject_keys() -> list[str]` (seeded first),
  `is_held_out(subject_key) -> bool` (held-out subjects reserved for
  generalization eval). Registries: `SEEDED_SUBJECTS` (fluid_mechanics,
  macroeconomics), `AUTHORED_SUBJECTS` (linear_motion + held-out placeholder).
- `generate_fixtures.py`: `build_fixture_pdf(text, path) -> Path` (PyMuPDF
  single-page builder) and `generate_linear_motion_fixtures(out_dir) ->
  (problem_pdf, solution_pdf)` — synthesize the linear-motion problem/solution
  PDFs (`campaign/cast/materials/linear_motion_*.pdf`) the authored-set path
  uploads.

## Invariants & gotchas

- **`generate_fixtures.py` is a real PDF-material generator that is OWNED here**
  — NOT a pytest fixture; it must NOT be caught by any `*_fixtures.py` exclusion
  (the exclude list is narrowed to `**/tests/**/*_fixtures.py`).
- Data owned conceptually but not as code: the persona JSON corpus dirs
  `campaign/cast/personas/<subject>/` and the two materials PDFs.

## Related

- [cast-teacher](cast-teacher.md) (consumes `SEEDED_SUBJECTS` +
  `AuthoredSubject` PDFs), [cast-personas](cast-personas.md).
