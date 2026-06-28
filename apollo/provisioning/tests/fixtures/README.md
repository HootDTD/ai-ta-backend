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

The **problem statements, prose `target_unknown`s, and the REAL `given_values` are
from staging** — pulled read-only. The **`reference_solution`s are RECONSTRUCTED**:
the live solutions the lint actually evaluated were never persisted (rejected rows
carry `payload={}`; the Tier-1 inventory rows hold only statement stubs). Each
reconstruction is a well-formed symbolic system with a **single graph-derived
answer** — the lone free symbol left after givens + intermediates + cancellations
(`_derive_symbolic_answer`) — keeping the live failure shape (prose target ≠ a symbol
the terminal equation computes).

The prose `target_unknown` is preserved verbatim from staging even where the
reconstruction computes the single *terminal* quantity it resolves to:
- `aae333_01` (target "boundary layer thickness and wall shear stress") reconstructs
  to the terminal wall-shear-stress `tau`, with `x` (trailing edge `x=L`) and `rho`
  (water density) supplied as the real givens they are. Single graph-derived answer
  `{tau}`; covering symbol table → currently rejects at gate 5.
- `aae333_06` (target "u velocity at y=δ/2") reconstructs to the self-similar Blasius
  result `u = U·f_half` (`f_half ≈ 0.793`, the universal profile value at that
  height — a real given). Single graph-derived answer `{u}`; empty symbol table →
  currently rejects at gate 4.
  *(`aae333_01`/`06` were calibrated to the single-answer shape in Phase 2 after the
  Option-2 gate-7 under-determination check landed; the four others — `02`–`05` —
  were already single-answer.)*

Used by `test_promotion_lint.py`:
- `aae333_expected.json` `failed_gate` documents the gate the OLD subject-specific
  lint rejected each fixture at (5×gate5, 1×gate4 — spec §1's live snapshot).
- Phase 2 `test_aae333_now_promotes_under_content_derived_gates`: after the
  subject-agnostic change all six **promote** — the 5 covering-table cases via the
  graph-derived symbolic answer (gate 5 symbolic half + gate 7), `aae333_06` via
  internal symbol grounding (table-less gate 4).
