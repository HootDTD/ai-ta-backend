# Emergent Misconception Map — Design

- **Date:** 2026-07-10
- **Status:** Approved (design); implementation plan pending
- **Owner doc (drift target):** `docs/architecture/apollo.md`
- **Depends on / builds on:**
  - Existing emergent ledger increment 1 — `apollo/emergent/` + migration `037` (`docs/_archive/specs/2026-07-05-emergent-misconception-store-design.md`, Candidate C)
  - F-struct structural co-key — branch `feat/apollo-misc-struct-cokey`, PR #120 (`docs/_archive/specs/2026-07-09-apollo-misconception-struct-cokey-design.md`)
  - Misconception detector + judge-authoritative gate — PRs #118/#119/#120 stack
  - `entity_key` derivation (this doc, §5.1) — the reference-node canonical key

## 1. Problem

Apollo's misconception detector can only ever dock a misconception that was **already authored into a bank** for the course. On a real (auto-provisioned) course no misconceptions are authored — the professor supplies problems + solutions + a canonical topics list, never a misconception list — so the bank is empty and the detector is a silent no-op. The `apollo/emergent/` ledger that was meant to fix this is a **closed loop**: it only ingests misconceptions that were *already keyed to a bank entry*, so it re-surfaces variants of known misconceptions but can never **discover** a new one. The one signal that would let it bootstrap from nothing — the judge is confidently *wrong* at a reference node but can name no misconception (gate rows `row7_unkeyed_clarify` / `row8_unkeyed_drop`) — is **discarded today**: it never docks, never reaches the ledger, evaporates.

**Goal:** open the loop. Capture the confident-wrong-but-unnamed signal, anchor it to a node, let it accrete across students into a trust-graded **map of misconceptions**, and let sufficiently-trusted entries feed back into live grading and future detection — all without any human authoring a misconception (principle P1 of the 2026-07-05 memo: *emergent, never seeded*).

## 2. Non-goals (explicitly out of scope)

- **Semantic clustering of unnamed errors** (memo OQ1). This scope is node-anchored only (§5.2); finer intra-node resolution is the documented forward path (§5.2, "→ C"), not built now.
- **Teacher curation UI / `muted` kill-switch.** Deliberately excluded (owner decision). The K=3-distinct-students promotion gate is the *only* false-positive safety in this scope. If a bad emergent misconception ever needs suppression before the memo's increment-2 curation surface exists, that is handled operationally (flag off), not by a per-entity switch.
- **The materialized rollup bank / worker** (memo increment 2). Trust stays derived-on-read.
- **Changing authored-misconception behavior.** Hand-authored banks keep working unchanged; emergent is additive.
- **Getting the detector stack live on staging.** That is a prerequisite (§8), tracked by PRs #118/#119/#120, not part of this design's code.

## 3. Decisions (locked)

| # | Fork | Decision | Rationale |
|---|------|----------|-----------|
| D1 | Birth trigger | **Detector births, clarification/repeat upgrades** | Answer-blind detector signal births a *low-trust* observation immediately; a clarification-`refuted` confirmation or a repeat from another student raises trust. Captures everything, weights by evidence quality. |
| D2 | Candidate identity | **Node-anchored** — `signature = emergent.<entity_key>` | Deterministic, needs zero embedding infra, yields "one emergent misconception per reference node" as an honest first map. Forward path to semantic sub-clustering ("→ C") documented, not built. |
| D3 | Grade authority | **Asserts on grades once promoted, same bar as authored** (τ_assert = 0.5, K = 3) | Reuses the existing wired feedback path; a confusion ≥3 distinct students hit at high confidence grades like a bank entry. |
| D4 | Store | **kg-entity** | Ledger stays the append-only event log + trust source; a promoted misconception materializes as a first-class `apollo_kg_entities kind='misconception'` node with an `opposes` link, projected to `:Canon`. The map literally becomes a graph. |
| D5 | Safety | **K=3 gate only; no `muted`** | One hallucinated verdict never promotes — it needs independent corroboration across distinct students. Flag-off is the operational kill-switch. |
| D6 | `entity_key` | **Derive when absent** | Reference-node `entity_key` is deterministic from `(entry_type, id)`; derive it at graph-build time so every problem (hand-authored or provisioned) carries it. Byte-identical for hand-authored problems (already match). |

## 4. The convergence (why this is small)

A node-anchored emergent misconception **is a structural co-key (F-struct) entry by construction** — its `opposes` value *is* the node's `entity_key`. So once promoted:

- it stores as a `:Canon` misconception entity whose `opposes_entity_key` = the reference node's key, and
- it docks future students **through the F-struct path already built in PR #120** — no embeddings, no trigger phrases, no description text (which is exactly what the kg-entity store lacks, and why kg-entity is feasible *here* where full store-unify was not).

The loop closes end to end on the rail we already laid:

> judge trips at a node it can't name → observation → accretes across distinct students → promotes → becomes a `:Canon` opposes-entity → F-struct auto-docks the next student who trips there.

Decisions D2, D4, D6 and the existing F-struct all land on the same mechanism.

## 5. Architecture

### 5.1 `entity_key` derivation (D6)

`_entity_key_for_step(step)` (`apollo/persistence/learner_model_seed.py:203`) already computes the canonical key deterministically: `f"{prefix}.{id}"` where `prefix` comes from `_ENTRY_TYPE_TO_KIND_PREFIX` (`equation→eq`, `definition→def`, `simplification→simp`, `procedure_step→proc`, `condition→cond`, `variable_mapping→varmap`). Hand-authored `entity_key`s (`eq.gdp_deflator`, `def.real_basis`, …) are exactly this function's output.

**Change:** in `Problem.to_kg_graph` (`apollo/schemas/problem.py:117`), when `step.entity_key is None`, derive it via `_entity_key_for_step(step)` before `build_node(entity_key=…)`. Guard the unknown-`entry_type` case (skip/None, never raise). Result: every reference-graph node carries an `entity_key` — byte-identical for hand-authored problems, newly populated for provisioned ones.

This hands the emergent map its **signature vocabulary for free** and replaces today's dead-end `unkeyed:<concept_id>` bucket (which can never promote) with a stable, promotable `emergent.<entity_key>`.

### 5.2 Signature (D2)

For a captured finding at reference node `N`:

```
signature = f"emergent.{N.entity_key}"        # node-anchored
opposes   = N.entity_key
```

A finding at a node **without** an `entity_key` (e.g. unknown entry type) is **not captured** (no stable signature ⇒ nothing to accrete). This is the intended scope boundary.

> **Forward path (→ C, not built):** when one node accumulates genuinely distinct confusions, sub-cluster within the node by embedding the `evidence_span` through the existing dedup ladder (`resolve_candidate`: scope-pool → cosine band → LLM-judge tiebreak), yielding `emergent.<entity_key>.<cluster>`. Deferred until the map demonstrates the need.

### 5.3 Capture hooks (D1)

Two flag-gated write seams, each its own failure domain — a failure logs + rolls back its own write and **never affects the returned grade**:

1. **Detector-unkeyed (the birth signal).** In the gate's `best_judge.bank_code is None` branch (`apollo/overseer/misconception_detector/gate.py:182-204`), when the judge verdict is a confident `wrong`/`misconception` clearing routed tau, at a node that has an `entity_key`, with no bank_code and no struct-opposes match (i.e. the `row7`/`row8` unkeyed outcomes) → emit an observation with `source='detector_unkeyed'`, `confidence` from the judge, `signature`/`opposes` per §5.2, `evidence_span` from the student utterance. (This is the exact signal F-struct *docks* when the graph opposes it; here we *capture* it when the graph does not yet.)
2. **Clarification-refuted (the upgrade signal).** In `resolve_pending_clarifications` (`apollo/clarification/resolve_turn.py`), on `RescoreOutcome == "refuted"`, write an observation with `source='clarification_refuted'` (the value already reserved in migration `037`'s `source` comment but never wired). The `refuted` row already carries `candidate_key`→signature, `concept_id`, `search_space_id`, `user_id`, `attempt_id`, `clarification_text`→`evidence_span`; the rescorer must additionally surface a `confidence` and the `opposes` key (small schema add to the rescorer output).

Both seams gate on the same emergent flag (§5.6).

### 5.4 Ledger + trust (D3)

Reuse `apollo/emergent/` unchanged in shape:

- **Store:** append to `apollo_misconception_observations` (migration `037`, model `MisconceptionObservation`). Extend the `source` enum/domain to include `'detector_unkeyed'` and `'clarification_refuted'` alongside `'grading_artifact'`. Idempotency stays `UNIQUE(attempt_id, signature)`.
- **Trust (derived-on-read, unchanged):** `trust = min(1, distinct_students / K) · mean_confidence · recency_half_life`, with `K = 3`, `τ_project = 0.2`, `τ_assert = 0.5` (`apollo/emergent/config.py`). Because `min(1, students/K)` caps at `1/3 ≈ 0.33` for a single student, **a first observer can never breach τ_assert** — a novel confusion cannot penalize the student who first exhibited it; it needs ≥2–3 distinct students. This is an intended fairness property.

Thresholds are **pre-calibration** (memo OQ2); calibrate against the campaign corpus before enabling on real traffic (§7).

### 5.5 Store + promotion (D4) — the map materializes

- **≥ τ_project (0.2):** materialize/upsert the misconception as a first-class `apollo_kg_entities` row — `kind='misconception'`, `canonical_key='emergent.<entity_key>'`, `payload.opposes_entity_key=<entity_key>` — and link `opposes` via the existing `link_opposes` (`apollo/provisioning/tag_mint_persist.py:173-206`), then project to `:Canon` (`apollo/knowledge_graph/canon_projection.py`). **This is the map** — emergent misconception nodes linked by `opposes` to the reference entities they contradict. Visible; not yet grading.
- **≥ τ_assert (0.5):** the promoted misconception becomes a live detector candidate. Extend `candidate_assembly` (`apollo/clarification/candidate_assembly.py:88-97`, already reads promoted emergent misconceptions) so an emergent `misc.*`/`emergent.*` candidate is matched **structurally** via F-struct's `build_opposes_index` (`opposes = entity_key`) — no embedding/trigger-phrase lookup needed. A future student who errs at that node is docked exactly as by an authored misconception (D3).

> **Note — candidate_assembly opposes bug:** `candidate_assembly.py:58` currently hardcodes `"opposes": None`, discarding the `apollo_misconceptions.opposes` populated by migration `038`. Fix this as part of wiring so emergent (and authored) opposes actually reach F-struct.

### 5.6 Flag & rollback (D5)

Gate every write and the grade-feedback read on the existing `APOLLO_EMERGENT_MISCONCEPTIONS` flag (or a dedicated sub-flag if we want to decouple capture from feedback; decide in planning). Default **OFF** ⇒ byte-identical: no capture, no materialization, no candidate, grade unchanged. Rollback = flag off: capture stops immediately and promoted entities stop being consulted (they remain in `:Canon` as inert history until re-enabled). No data migration to undo.

## 6. Data flow (end to end)

```
student teaches → compute_coverage builds reference_graph (entity_key on every node, §5.1)
      → detector judge runs per reference node
          → confident wrong + named  → docks (existing bank / F-struct)     [unchanged]
          → confident wrong + UNNAMED → §5.3 capture → observation (ledger)  [NEW]
      → clarification 'refuted'       → §5.3 capture → observation (ledger)  [NEW]
ledger accrues across students (§5.4)
      → trust ≥ τ_project → materialize :Canon opposes-entity (§5.5) — on the MAP
      → trust ≥ τ_assert  → live detector candidate → F-struct docks future students (§5.5)
```

## 7. Testing & calibration

- **Patch-coverage contract:** ≥95% on changed lines (repo contract). Unit-test each new unit in isolation: `entity_key` derivation (hand-authored byte-identity + provisioned population + unknown-type guard), each capture seam (fires on the right verdict, does *not* fire on `clear`/controls, own-failure-domain never breaks the grade), signature construction, source-enum extension, materialization upsert, structural candidate match.
- **DB changes:** migration extending the `source` domain — enumerate every source×promotable branch on a LOCAL Docker Postgres (Testcontainers), never a remote Supabase.
- **Calibration gate (before real traffic):** replay the campaign corpus with capture ON; confirm (a) controls surface no promotable emergent misconception, (b) known-misconception clusters accrete to the right node, (c) τ_assert/K produce acceptable precision. Node-anchored means the campaign's labeled clusters map directly to expected `emergent.<entity_key>` signatures.

## 8. The path through staging (deployment sequence)

The emergent map is **not standalone** — it rides on the detector being live on staging. Verified staging reality (2026-07-10): the reference-graph coverage grader (`compute_coverage` → `compute_rubric`) is the live per-node grade on every Done click; the Neo4j graph-*simulation* grader ("S_graph") is off/never-served; the detector is off (flag unset); `APOLLO_GRADING_ARTIFACT_ENABLED` is on (canonical artifacts are being captured). Sequence:

1. **Land the detector stack on staging** — PRs #118 → #119 → #120 (detector + F-struct + blind-judge fix + `entity_key` derivation from §5.1). PR #118 is currently *conflicting* against staging; resolve + merge first. Flag stays OFF (byte-identical).
2. **Enable the detector on staging** and confirm it produces real per-node confident-wrong signals. No signal to capture until this runs.
3. **Build + enable the emergent capture hook** (this design) on top. This is the only net-new code; everything under it already runs on staging.

## 9. Open questions (resolve in planning)

- **Q1 — flag split:** one `APOLLO_EMERGENT_MISCONCEPTIONS` for both capture and grade-feedback, or split so we can capture-and-observe on staging *before* granting grade authority? (Leaning split — safer live-test story.)
- **Q2 — clarification `confidence`/`opposes`:** exact rescorer-schema addition to carry them on the `refuted` branch (§5.3).
- **Q3 — materialization trigger:** upsert the `:Canon` entity lazily at read/rollup time, or eagerly when an observation crosses τ_project (§5.5)? (Memo OQ7: inline vs worker.)
- **Q4 — threshold calibration values:** the campaign-derived K/τ (§7) before any real-traffic enable.
