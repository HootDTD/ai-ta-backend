# AAE 333 promotion-gate fixtures

The forward oracle for the subject-agnostic promotion-gate change
(`docs/superpowers/specs/2026-06-26-subject-agnostic-promotion-gates-design.md`).

- **`aae333_0{1..6}.json`** — the 6 Purdue AAE 333 (aerodynamics) homework problems
  from the live staging E2E (`search_space_id=4`, `document_id=6`, `ingest_run_id=2`)
  that promoted **0/6**.
- **`aae333_expected.json`** — per fixture: the documented current reject gate
  (`failed_gate`) plus the `canonical_symbols` / `normalization_map` table to run it
  under. Snapshot: **5 reject at gate 5** (prose target, covering table) + **1 at
  gate 4** (`aae333_06`, fresh concept, empty table).

## Provenance / honesty

The **problem statements, prose `target_unknown`s, and `given_values` are REAL** —
pulled read-only from staging. The **`reference_solution`s are RECONSTRUCTED**: the
live solutions the lint actually evaluated were never persisted (rejected rows carry
`payload={}`; the Tier-1 inventory rows hold only statement stubs). Each reconstruction
is a well-formed symbolic system with a **single graph-derived answer** (the lone free
symbol left after givens + intermediates + cancellations), matching the live failure
shape (prose target ≠ a symbol the terminal equation computes).

Used by `test_promotion_lint.py`:
- Phase 1 `test_aae333_currently_rejects_5x_gate5_1x_gate4` — pins the current reject.
- Phase 2 (Step 2.6) inverts it: after the subject-agnostic change all six **promote**
  — the 5 gate-5 cases via the graph-derived symbolic answer, `aae333_06` via internal
  symbol grounding (table-less gate 4).
