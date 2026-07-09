# Apollo misconception detector — structural co-key ("F-struct") design

**Date:** 2026-07-09
**Branch:** `feat/apollo-misc-trace` (@ `dbcc81e`)
**Owner doc:** `docs/architecture/apollo.md` (`owns: apollo/overseer/misconception_detector/**`, `apollo/**`)
**Supersedes direction of:** `docs/_archive/specs/2026-07-09-apollo-misconception-trace-and-tau-calibration-design.md` (Phase 2 — tau/co-key calibration was DISPROVEN; see that spec's top note).
**Predecessors:** recall-gap handoff `docs/_archive/handoffs/2026-07-08-apollo-misconception-recall-gap-handoff.md`; corroboration/keying redesign `docs/_archive/specs/2026-07-08-apollo-misconception-corroboration-redesign.md`; Phase-1 trace `…-trace-and-tau-calibration-design.md`.

---

## 1. Diagnosis (proven — not re-litigated here)

Apollo's misconception detector still bands **Strong** on some misconception
attempts (**88 / 95 / 112**, all `nominal_for_real`), while the contrast attempts
**100 / 105** dock correctly. A flag-guarded per-node trace (`APOLLO_MISC_TRACE`,
Phase 1, already merged on this branch) plus a live instrumented run established:

* **Not a tau problem.** Judge confidence is ~1.0 on docks, misses, AND controls
  alike. No threshold separates the three. (This kills the Phase-2
  tau-calibration direction outright.)
* **The judge LOCALIZES reliably.** The judge is invoked per reference-graph node
  (one batched call, one row per node — `judge.py::judge_concepts`). It is
  *reliable at saying WHERE the error is*: controls return `clear@~1.0`, misses
  return `wrong@~1.0` at the `real_basis` node, and all four control attempts
  produce 0 false positives (`clear` never flips to `wrong`).
* **The judge NAMES unreliably.** On the misses it returns `verdict="wrong"` with
  an EMPTY `misconception_code` at the `real_basis` node; on the docks (100/105)
  the same node returns `verdict="misconception"` + `code="nominal_for_real"` →
  the existing row-3 co-key docks it. `wrong` vs `misconception` flips run-to-run
  at `temperature=0.0`. So the gate reduces (handoff §5-T5) to "did the LLM NAME
  a valid bank code this run", which is a coin-flip on these attempts.

**The link that would name the misconception already exists in authoring data,
but is dropped before runtime — at TWO points:**

1. **Node drop.** Problem JSON steps carry `entity_key` (e.g. the `real_basis`
   step carries `"entity_key": "def.real_basis"` —
   `apollo/subjects/macroeconomics/concepts/nominal_vs_real_gdp/problems/problem_02.json`).
   But `apollo/schemas/problem.py::ReferenceStep` has no `entity_key` field, so
   `Problem.model_validate` DROPS it (pydantic ignores the extra key — noted at
   `apollo/learner_model/personalization_select.py:16-23`), and
   `Problem.to_kg_graph` never passes it to `build_node`. The runtime reference
   `Node` (`apollo/ontology/nodes.py::_NodeBase`) has no field to hold it. So at
   detection time the reference node the judge localized to has **no canonical
   entity key**.

2. **Bank drop.** `misconceptions.json` declares `"opposes": "def.real_basis"` on
   the `nominal_for_real` entry
   (`apollo/subjects/macroeconomics/concepts/nominal_vs_real_gdp/misconceptions.json`).
   But `apollo/persistence/misconception_bank_seed.py::misconception_entry_to_bank_spec`
   never copies `opposes`, the `apollo_misconceptions` table (migration 019) has
   no `opposes` column, and the runtime `MisconceptionEntry`
   (`apollo/overseer/misconception_bank.py`) keeps only `concept_id` (whole-concept
   scope). So the bank cannot tell you "this misconception opposes THIS node".

   *Precedent that the link is real:* the OTHER store already carries it. The
   emergent store's `apollo_misconception_observations` has an `opposes` column
   (migration 037), and `learner_model_seed.misconceptions_to_entities` copies
   `entry["opposes"]` → `payload.opposes_entity_key` into `apollo_kg_entities`,
   which `apollo/grading/opposes.py::build_opposes_map` reads for the graph
   grader. Only the **detector's** bank (`apollo_misconceptions`) lacks it.

---

## 2. The fix — F-struct (structural co-key)

**Split the labor: the judge LOCALIZES, the GRAPH NAMES.** When the judge returns
a confident non-`clear`/non-`needs_clarification` verdict (`wrong` OR
`misconception`) at reference node X, and some bank entry declares
`opposes == X.entity_key`, treat that bank entry as the name — emit a keyed
finding with `bank_code = that entry's code` and route it through the SAME
existing row-3 co-key/ceiling machinery. This makes the dock deterministic:
localization (reliable) does the work; naming no longer depends on the LLM's
run-to-run `wrong`↔`misconception` flip.

Five moving parts:

### 2.1 Node plumbing — carry `entity_key` to the runtime reference node
* Add `entity_key: str | None = Field(default=None)` to
  `apollo/ontology/nodes.py::_NodeBase` (all six node variants inherit it;
  default `None` keeps parser/system/legacy nodes byte-identical).
* Add `entity_key: str | None = None` to `build_node(...)` (pass-through, same
  shape as the existing `status` / `student_belief` optional kwargs).
* Add `entity_key: str | None = None` to
  `apollo/schemas/problem.py::ReferenceStep` so `Problem.model_validate` STOPS
  dropping it, and pass `entity_key=step.entity_key` in `Problem.to_kg_graph`'s
  `build_node(...)` call. Reference nodes now carry their canonical key; parser
  and system nodes stay `None`.

### 2.2 Bank plumbing — carry `opposes` to the runtime bank entry
* Migration **038** (next number; on-disk max is 037): `ALTER TABLE
  apollo_misconceptions ADD COLUMN opposes TEXT;` (nullable — most banks have no
  opposes; the column is the per-entry scope link). Mirror migration 037's
  header/RLS/`COMMENT` conventions.
* ORM `Misconception` (`apollo/persistence/models.py`): add
  `opposes = Column(Text, nullable=True)`.
* `MisconceptionEntry` (`apollo/overseer/misconception_bank.py`): add
  `opposes: str | None` field; populate it in `_from_row` and in
  `match_by_embedding`'s inline row-build; add `opposes` to the `upsert_entry`
  INSERT/UPDATE and `SELECT`.
* Seeder (`misconception_bank_seed.py`): add `opposes: str | None` to
  `MisconceptionBankSpec`; copy `entry.get("opposes")` in
  `misconception_entry_to_bank_spec`; thread `spec.opposes` through
  `scripts/seed_apollo_misconceptions.py::_seed_concept → upsert_entry`.

### 2.3 Gate — the new structural co-key path
`apollo/overseer/misconception_detector/gate.py::_gate_one_concept`. Build a
`opposes_by_key` index ONCE in `gate_findings` (from the reference graph's nodes:
`{node.entity_key: node}` — but the gate is a pure function over findings, so the
opposing information must be passed IN). **The clean seam:** pass an
`opposes_index: dict[str, str]` into `gate_findings` — mapping
`node_id → bank_code` for every reference node whose `entity_key` is opposed by
a bank entry's `opposes`. This index is built by the CALLER
(`done.py` / the campaign harness / detector wiring) from the reference graph +
the loaded bank, so the gate stays pure and node-shape-agnostic.

In `_gate_one_concept`, AFTER the existing judge/bank logic, add the structural
branch:

* Fires ONLY when `best_judge` exists, its verdict is `wrong` or `misconception`
  (i.e. non-`clear`/non-`needs_clarification`), it clears its routed tau
  (`_judge_clears_tau`, reused — the existing dual-tau routing), AND
  `best_judge.bank_code is None` (the judge did NOT already name a validated
  code).
* Look up the judge's node's `entity_key` in `opposes_index`. The gate keys
  anchors by `concept_key` (== `node_id`), NOT `entity_key`, so the index must be
  keyed so the gate can resolve it. **Chosen encoding:** the caller builds
  `opposes_index: dict[node_id, bank_code]` — i.e. it pre-resolves
  `entity_key → node_id` while it still has the reference graph, and hands the
  gate a `node_id`-keyed map (matching the gate's own `concept_key` keying). The
  gate never sees `entity_key` directly.
* On a hit: emit a docked representative via `dataclasses.replace(best_judge,
  verdict="misconception", corroborated=True, ceiling_eligible=True,
  bank_code=<code>, signature=f"misc.{code}")`. This reuses the row-3 co-key
  semantics exactly (ceiling-eligible, keyed → `merge._keyed_row` emits the
  artifact `misconceptions[]` row).

**No double-dock:** the structural branch is guarded by `best_judge.bank_code is
None`, so it CANNOT fire on a node the judge already keyed (that node takes the
existing row-3/row-5 path). A node is docked by AT MOST one path.

### 2.4 Trace — record the structural match
`apollo/overseer/misconception_detector/trace.py`: extend `_gate_row_label` with
a `row3s_struct_cokey_dock` label and add `struct_opposes_code` +
`docked_via` (`judge_named` | `struct_opposes` | `none`) fields to each node's
trace row, so the re-validation run shows which path docked each node.

### 2.5 Flag
New sub-flag `APOLLO_MISC_STRUCT_COKEY` (default OFF), read at call time (same
`_TRUTHY` set as `detector_enabled`). The structural path is gated under it. When
OFF, `gate_findings` receives an empty `opposes_index` (or the caller skips
building it), so behavior + output are **byte-identical** to today. The sub-flag
is ALSO subordinate to `APOLLO_MISCONCEPTION_DETECTOR` (the detector must be on
for any of this to run at all).

---

## 3. Control-safety argument (why this cannot regress the 0/4 control FP)

The structural path fires ONLY on a judge verdict of `wrong` or `misconception`.
The proven diagnosis is that **controls return `clear` at ~1.0** (0/4 control
false positives across the labeled set). A `clear` verdict never enters the
structural branch (guarded on non-`clear`/non-`needs_clarification`). Therefore
the structural path is control-safe *by construction*: no control attempt can be
docked by it, because no control produces the triggering verdict. This is a
strictly stronger guarantee than "we tuned a threshold" — it does not depend on
any confidence value, only on the verdict category the judge is RELIABLE at.

Secondary safety: the routed-tau reuse means a hypothetical low-confidence
`wrong` (none observed) still must clear 0.85/0.90 before the structural name is
attached, so a hedged localization cannot silently dock.

---

## 4. Design decisions (resolved by reading the code)

| # | Decision | Chosen default | Rationale |
|---|----------|----------------|-----------|
| D1 | Runtime field name + which node variants carry `entity_key` | `entity_key: str \| None = None` on **`_NodeBase`** (all six variants inherit; only reference nodes populated) | `_NodeBase` already holds `node_id`/`source`/`status`/`student_belief`; one optional field there is the minimal uniform change and keeps `build_node` symmetric. Per-variant fields would triple the surface for zero benefit. |
| D2 | Does the structural path need a min confidence? | **Reuse `_judge_clears_tau`** (existing dual-tau routing, 0.85 token-prob / 0.90 verbalized). No new constant. | Data is ~1.0 so it always passes, but reusing keeps ONE confidence policy and guarantees a future hedged `wrong` can't dock silently. A bespoke floor would be an un-calibrated third knob. |
| D3 | >1 bank entry opposing the same node (multi-opposes) | Single case only (nominal cluster is 1:1). If multiple, pick the **lexicographically-lowest `code`** deterministically; document multi-opposes disambiguation as minimal/future. | The labeled cluster is single-opposes; a deterministic tiebreak avoids nondeterminism without over-engineering a real fan-out policy that has no data yet. |
| D4 | Interaction with judge-named co-key (no double-count) | Structural branch **guarded by `best_judge.bank_code is None`** — fires only when the judge did NOT name a validated code. | A node is docked by at most one path; the existing row-3 co-key owns the judge-named case, the structural path owns the localized-but-unnamed case. Clean mutual exclusion, no merge arithmetic needed. |
| D5 | Gate purity — how does a pure findings-function see `opposes`? | Caller builds a `node_id → bank_code` **`opposes_index`** from `(reference_graph, bank_entries)` and passes it into `gate_findings`; the gate never sees `entity_key` or node shapes. | Keeps `gate.py` pure/IO-free and node-shape-agnostic (its current contract). The `entity_key → node_id` resolution happens where the reference graph is in hand (done.py / harness), same place `centrality` is computed. |
| D6 | Migration number | **038** | On-disk max is `037_apollo_misconception_observations.sql`. |
| D7 | Flag name | **`APOLLO_MISC_STRUCT_COKEY`** (sub-flag, default OFF; also subordinate to `APOLLO_MISCONCEPTION_DETECTOR`) | Mirrors the `APOLLO_MISC_TRACE` sub-flag convention; lets the structural behavior flip independently for the re-validation run. |

---

## 5. Scope

**In scope (this spec + plan):** build the general structural-co-key mechanism
(node key + bank `opposes` + gate path + trace + flag + migration) and validate
it on the `nominal_for_real` cluster (attempts 88/95/112 must dock; control FP
stays 0/4) via `campaign/validate_misconception_detector.py` with
`APOLLO_MISC_TRACE=1`.

**Out of scope (explicit follow-ons):**
* **`opposes` coverage audit across the whole bank.** Only the two macro
  `nominal_vs_real_gdp` entries currently declare `opposes`; the calc-2
  `misconceptions.json` files declare it too but were never validated to point at
  real reference-node `entity_key`s. Auditing that every authored `opposes`
  resolves to a real reference node bank-wide is a SEPARATE task (data-quality,
  not mechanism).
* **Level-2 — penalizing a confident `wrong` that has NO opposing misconception**
  (novel / arithmetic errors, where localization is confident but no bank entry
  opposes the node). This is a distinct policy decision (do we dock an
  un-nameable error? at what severity?) and is explicitly deferred to a future
  design.

---

## 6. Constraints (see the plan's Global Constraints for verbatim gate values)

* Flag-OFF (`APOLLO_MISC_STRUCT_COKEY` unset/falsy) ⇒ byte-identical behavior +
  output. The migration adds a nullable column only (no backfill, no default
  change), so an un-seeded `opposes` is `NULL` and the structural path never
  fires — a deploy that applies 038 but leaves the flag OFF is a no-op.
* Patch coverage ≥95% on changed lines vs `origin/staging`; full `pytest apollo/`
  green.
* Migration 038 is numbered SQL, authored + tested on LOCAL Docker Postgres /
  Testcontainers ONLY. Agents NEVER apply it to any remote Supabase.
* Drift: update `docs/architecture/apollo.md` in the same work; bump
  `last_verified` to 2026-07-09.
