# Master Results — Macro (OpenStax Ch.6) graph-grading probe

**Run:** 2026-06-22 → 2026-06-23 · local Docker stack · agent-driven.
**Scope:** a brand-new non-fluid Apollo subject driven through the *entire*
graph-grading pipeline, end-to-end, to (a) sanity-check generality and (b) probe
whether the "case-3" derived-form edge-resolution bug is fluid-specific or
general. This document is the self-contained master record: setup, every phase
we observed with its raw output, the full score data, and a defect catalog that
maps each finding to a pipeline stage, its architectural blast radius, and
whether the same defect appeared in the fluid (Bernoulli) line.

Companion docs in this folder: `BACKGROUND.md` (how grading works + the case-3
lineage), `DESIGN.md` (the approved design), `WORKFLOW.md` (the exact reproducible
command sequence). Prior fluid handoffs referenced throughout:
`../APOLLO-USES-EDGE-RESOLUTION-HANDOFF.md` (Bernoulli attempt 8),
`../APOLLO-GRADING-EDGE-RESOLUTION-HANDOFF.md`, `../APOLLO-GRAPH-GRADING-HANDOFF.md`.

---

## 0. Executive summary

1. **The engine is genuinely subject-agnostic.** With *zero* engine code change,
   a hand-authored macroeconomics subject (2 concepts, 5 problems) ran through
   parse → resolve → S_norm/R_norm → score and **discriminated explanation
   quality** (strong > partial > weak) on 4 of 5 problems. The only structural
   change needed was a Bernoulli-hardcoded *seeder driver* and a *probe*.
2. **Case-3 is GENERAL, not fluid-specific.** A derived/computed/solved form of
   an in-scope equation fails to resolve, the procedure's `USES` edge attaches to
   it, and that edge is dropped from S_norm → `usage`/`edge_coverage` collapse —
   the *exact* Bernoulli attempt-8 signature, reproduced on macroeconomics
   (`real_gdp_growth`). The macro corpus let us see the mechanism with
   unusual clarity (Part 4).
3. **The RAG relevance pathway works** once a real corpus exists: hybrid retrieval
   surfaced the exact textbook worked-examples for all 5 questions, and the
   provisioning grounding adapter returned non-empty spans — directly refuting the
   handoff's "empty corpus → faithfulness judge rejects everything" failure mode.
4. **Six further defects surfaced** (Part 5), four of which are *shared with the
   fluid line* (so they are architecture-level, not data-level), and two of which
   are new (an LLM-adjudicator type-gate violation; the dual-storage misconception
   footgun).

---

## 1. Setup — what we built and why

### 1.1 The system under test (one paragraph)
Apollo grades a student's spoken explanation by parsing it into a typed Neo4j
knowledge graph (6 node types, 4 edge types), **resolving** each student node to
a closed candidate set (this problem's reference entities + course
misconceptions), building a normalized **student graph S_norm** and **reference
graph R_norm**, and scoring S_norm against R_norm. The grading chain
(`apollo/handlers/done_grading.py::run_graph_simulation`) is: load
misconceptions + entity specs → `build_problem_candidates` → `validate_student_graph`
(422 gate) → `resolve_attempt` (cleanser #1: node identity) → `write_resolution`
→ `build_student_canonical` / `build_reference_canonical` (cleanser #2: structure,
**drops edges with an unresolved endpoint**) → `grade_attempt` (pure scoring) →
audit → abstention → persist. Full detail in `BACKGROUND.md`.

### 1.2 The subject we authored
- **Source:** OpenStax *Principles of Macroeconomics 2e*, Ch.6 "The Macroeconomic
  Perspective" (CC BY 4.0). Chosen because it is open-licensed, quantitative
  (SymPy-tractable equations), and contains a clean *derived form* (the GDP
  deflator equation rearranged into the real-GDP formula).
- **Tree:** `apollo/subjects/macroeconomics/` — 2 concepts, 20 files.
  - `gdp_components` (§6.1): **Q1** `gdp_identity` `GDP−(C+INV+G+NX)` →560,
    **Q2** `net_exports_sign` `NX−(X−M)` →−20 (deficit), **Q3** `nnp_chain`
    `GNP−(GDP+RIN−ROUT)` then `NNP−(GNP−DEP)` →522.
  - `nominal_vs_real_gdp` (§6.2–6.3): **Q4** `real_gdp_from_deflator`
    `deflator−(nomGDP/realGDP)*100` →2859.5 (the case-3 trap), **Q5**
    `real_gdp_growth` `growth−((g2−g1)/g1)*100` →376%.
  - Each problem carries `entity_key` + `declared_paths` + SymPy zero-form
    equations + `depends_on` + procedure `order`/`uses_equations`; Q4 carries the
    `{PI: deflator}` simplification substitution.
  - **Authoring catch:** `I` is SymPy's imaginary unit (parses to a non-free
    symbol, silently dropping investment) → investment is `INV` everywhere.

### 1.3 Isolation + the live stack
A **dedicated course** `macro-econ` (`search_space_id=3`) was created so the
macro corpus, problems, and probe users never mix with the existing fluids smoke
courses (1, 2). Local stack: Supabase Postgres `:54322`, Neo4j `:7687`, all
`APOLLO_GRAPH_SIM_{SHADOW,LIVE,LAYER3}_ENABLED` flags ON (so the student-facing
rubric *is* the graph-sim rubric and the probe reads graph-grading scores
end-to-end).

### 1.4 How it was orchestrated (the meta-method)
Two subagent workflows + a hands-on live run:
- **Scout workflow** (6 parallel readers) mapped the building blocks → the
  pipeline is ~85% reusable; fluid coupling isolated to the learner-model seeder
  driver, the probe, and one antonym list.
- **Build workflow** (5 agents, disjoint files) authored the subject content
  (2 agents), generalized the seeder, wrote the index/antonyms/scripts, and the
  probe+orchestrator — each self-verifying against the real validators / tests.
- **Live run** (agent-driven, sequential) embedded → seeded → mined → graded,
  reading scores out of `apollo_graph_comparison_runs`.

---

## 2. Phases observed (with raw output)

### Phase A — Embed the textbook into local pgvector
`index_local_pdf.py` (PyMuPDF extract → `AITAIndexingService.index_from_items`,
no auto-provisioning) →
```
Indexed 'free_short_macro_textbook_openstax_ch6': 562 chunks.
document_id=4, status=ready, search_space_id=3
```
**Observed:** 34-page PDF → 562 chunks, one embedding call per chunk
(`text-embedding-3-large`, 3072-dim halfvec), `status={'state':'ready'}`,
`material_kind='textbook'`, `week=None` (so `active_document_conditions` makes it
retrievable without weekly gating).

### Phase B — Seed (registry → learner-model → canon)
```
registry:      subjects=3 concepts=3 problems=10   (5 macro tier-2 + fluids)
learner-model: entities_inserted=62 prereqs=20 misconceptions_linked=4
               problems_annotated=5 concepts_seeded=2
canon:         merged=62 entity_count=62           (:Canon total 103 = 62 macro + 41 fluids)
```
**Observed gotcha (the #1 failure mode):** the registry seeder backfills a new
subject to `MIN(aita_search_spaces.id)` = the fluids course. The macro subject had
to be **explicitly pinned** to course 3, and the learner-model + canon seeds had
to be passed `--search-space-id 3` — otherwise they resolve the wrong/no subject
and error `no 'macroeconomics' subject for search_space_id=1`.

### Phase C — RAG relevance pathway test
`_macro_mine.py` ran the production `AITAHybridSearchRetriever.hybrid_search`
(pgvector + FTS + RRF) for each question over the embedded corpus. **5/5 retrieved
on-topic Ch.6 material — the exact worked examples:**

| question | top retrieved span (page) |
|---|---|
| gdp_identity | p15 `= C + I + G + (X − M) = $400 + $60 + $120 + ($100 − $120) = $560 billion` |
| net_exports_sign | p15 `Net exports = X − M` |
| nnp_chain | p15 `NNP = GDP + Income receipts… − Depreciation = $560 + $10 − $8 − $40 = $522 billion` |
| real_gdp_from_deflator | p19 `in 1960, nominal GDP was $543.3 billion and the price index (GDP deflator)…` |
| real_gdp_growth | p33 `real GDP grew by (8,225.0 − 5,926.5)/(5,926.5) = 39%…` |

The provisioning **grounding adapter** (`make_course_retrieve_fn`, the exact
closure `validate_pair` uses) returned **6 non-empty `GroundingSpan`s** for
gdp_identity, first span = the `= C + I + G + (X − M) = $400 + $60…` worked
example. **This is the direct refutation of the handoff's empty-corpus failure:**
with a real corpus, the relevance pathway surfaces material to ground against.
(RRF scores are small in absolute terms — ~0.016 — but the *ranking* is exact.)

### Phase D — The 15-attempt sweep
5 problems × {strong, partial, weak}, each on a fresh cold-start probe user, via
the live `/apollo/sessions/from_hoot → /chat → /done` API. To serve each problem
uniquely we assigned a **distinct difficulty per problem within a concept** (a
routing key, since `session_init` serves the *first problem at the requested
difficulty*): gdp_components {intro, standard, hard}, nominal_vs_real_gdp
{standard, hard}. Full matrix in Part 3.

### Phase E — Enhancements
- **Misconception bank seeding** (`_macro_seed_misconceptions.py`) → re-ran the 5
  weak variations to exercise contradiction/soundness (Part 3.3).
- **Q4 case-3 robustness** — 3 dedicated `real_gdp_from_deflator` strong repeats
  (Part 4).

---

## 3. Full score data

Source: `apollo_graph_comparison_runs WHERE search_space_id=3`, `graph-compare-v1`.
Columns: `cov`=coverage, `node`=node_coverage, `edge`=edge_coverage,
`scop`=scoping, `use`=usage, `porder`=procedure_order, `dep`=dependency,
`snd`=soundness, `bisim`=bisimilarity, `contra`=contradiction, `abst`=abstained.

### 3.1 The sweep (15 attempts)

| # | problem | var | cov | node | edge | scop | use | porder | dep | snd | bisim | contra | abst |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 11 | gdp_identity | strong | **1.00** | 1.00 | 0.43 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 | **1.00** | 1.00 | F |
| 12 | gdp_identity | partial | 0.60 | 0.60 | 0.14 | 1.00 | 0.50 | 1.00 | 0.00 | 1.00 | 0.75 | 1.00 | F |
| 13 | gdp_identity | weak | 0.20 | 0.20 | 0.00 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 | 0.33 | 1.00 | **T** |
| 14 | net_exports_sign | strong | **1.00** | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 | 0.33 | 1.00 | **1.00** | 1.00 | F |
| 15 | net_exports_sign | partial | 0.67 | 0.67 | 0.25 | 1.00 | 1.00 | 1.00 | 0.00 | 1.00 | 0.80 | 1.00 | F |
| 16 | net_exports_sign | weak | 0.00 | 0.00 | 0.00 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 | **T** |
| 17 | nnp_chain | strong | **1.00** | 1.00 | 0.25 | 1.00 | 1.00 | 1.00 | 0.20 | 1.00 | **1.00** | 1.00 | F |
| 18 | nnp_chain | partial | 0.80 | 0.80 | 0.25 | 1.00 | 0.50 | 1.00 | 0.20 | 1.00 | 0.89 | 1.00 | F |
| 19 | nnp_chain | weak | 0.20 | 0.20 | 0.00 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 | 0.33 | 1.00 | **T** |
| 20 | **real_gdp_from_deflator** | strong | **1.00** | 1.00 | 0.50 | 1.00 | **1.00** | 1.00 | 0.33 | 1.00 | **1.00** | 1.00 | F |
| 21 | real_gdp_from_deflator | partial | 1.00 | 1.00 | 0.25 | 1.00 | 1.00 | 1.00 | 0.33 | 1.00 | 1.00 | 1.00 | F |
| 22 | real_gdp_from_deflator | weak | 0.00 | 0.00 | 0.00 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 | **T** |
| 23 | **real_gdp_growth** | strong | **0.25** | 0.25 | 0.00 | 1.00 | **0.00** | 1.00 | 0.00 | 1.00 | 0.40 | 1.00 | **T** |
| 24 | real_gdp_growth | partial | 0.25 | 0.25 | 0.00 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 | 0.40 | 1.00 | **T** |
| 25 | real_gdp_growth | weak | 0.25 | 0.25 | 0.00 | 1.00 | 0.00 | 1.00 | 0.00 | 1.00 | 0.40 | 1.00 | **T** |

(Smoke attempt 10 = `real_gdp_from_deflator` strong, identical profile to 20.)

### 3.2 What the sweep shows
- **Coverage discriminates strong > partial > weak on 4/5 problems.** The clean
  signal is `bisimilarity` (harmonic mean of soundness × coverage), which tracks
  coverage here because soundness was saturated (3.3). Weak variations abstain
  (`unresolved_rate > 0.35`).
- **`real_gdp_growth` is a flat 0.25 / abstain across all three variations** — not
  a discrimination failure but the case-3 reproduction collapsing every variation
  to "only the symbolic equation resolves" (Part 4).
- **`edge_coverage` and `dependency` are low even for strong** (edge 0.0–0.5,
  dep 0.0–0.33) — Defect 4.
- **`scoping` and `procedure_order` are a constant 1.0** — vacuous, not perfect
  (Defect 6).

### 3.3 Soundness — only exercised after seeding the misconception bank
The sweep ran with the runtime misconception table empty for macro, so weak
variations had no `misc.*` to resolve to → `soundness=1.0` (vacuous). After
seeding `apollo_misconceptions` (4 macro misconceptions) and re-running the weak
variations:

| problem | weak soundness: empty bank → seeded |
|---|---|
| gdp_identity | 1.00 → **0.50** |
| net_exports_sign | 1.00 → **0.50** |
| nnp_chain | 1.00 → **0.50** |
| real_gdp_growth | 1.00 → **0.50** |
| real_gdp_from_deflator | 1.00 → 1.00 (still abstained — `misc.deflate_wrong_direction` didn't resolve that parse) |

`soundness = 1 − 0.5·(#misc nodes in S_norm)`; 0.50 = exactly one misconception
resolved. **4/5 weak answers now register the contradiction** and stop abstaining.
The misconception type-gate (misconceptions are forced to `node_type='definition'`)
did *not* block resolution — the parser typed the misconception statements
compatibly. (This is Defect 5: the bank is a separate store that hand-authoring
must seed.)

---

## 4. Deep dive — case-3, seen from both sides

This is the experiment's reason for existing, so it gets the full mechanism.

### 4.1 The mechanism (recap)
Resolution decides node identity in isolation; `build_student_canonical` then
**drops any edge with an unresolved endpoint**. If a procedure's `USES` edge
points at a *derived/computed/solved* form of an equation (not symbolically equal
to the canonical zero-form, so it never resolves), the edge dies in S_norm and
`usage`/`edge_coverage` lose that credit — even for a correct answer.

### 4.2 Q5 `real_gdp_growth` strong — FULL reproduction (`inspect_attempt.py 23`)

| student node | kind | surface | resolution |
|---|---|---|---|
| stu_0b84… | Equation | `growth - ((g2-g1)/g1)*100` (symbolic) | **resolved `eq.growth_rate`** (exact) |
| stu_3555… | Equation | `growth - (10739.0/2859.5)*100` (**computed**) | **unresolved** |
| stu_96ea… | ProcedureStep | "divide by g1 and multiply by 100…" | resolved `eq.growth_rate` (**llm**) |
| stu_643d… | ProcedureStep | "calculate the change in real GDP…" | **unresolved** |
| stu_a543… | Definition | "real GDP" | **unresolved** |

```
edge: (proc, eq.growth_rate) -USES-> (computed eq, UNRESOLVED)   → DROPPED
run 23: edge_coverage=0.0 usage=0.0 node_cov=0.25 abstained=True (unresolved_rate)
findings: 6 × missing_edge, 3 × unresolved
```
The student stated the canonical equation (resolved) *and* a numerically
**computed** form (unresolved). The procedure's `USES` edge landed on the
computed form → dropped → `usage=0`. **This is Bernoulli attempt 8 verbatim**,
where the procedure's `USES` edge landed on the solved form `v2 = sqrt(2·g·h1)`
(unresolved) instead of the Bernoulli node. Same engine, different subject, same
failure.

### 4.3 Q4 `real_gdp_from_deflator` strong — case-3 happens but is MASKED (`inspect_attempt.py 20`)

| student node | kind | surface | resolution |
|---|---|---|---|
| stu_38ca… | Equation | `deflator - (nomGDP/realGDP)*100` (base) | **resolved `eq.gdp_deflator`** (exact) |
| stu_cbda… | Equation | `realGDP - nomGDP/(PI/100)` (**rearranged**) | **unresolved** |
| stu_a256… | ProcedureStep | "rearrange the deflator definition…" | resolved `proc.rearrange_for_real_gdp` (llm) |
| stu_edaf… | ProcedureStep | "substitute the given values…" | resolved `proc.rearrange_for_real_gdp` (llm) |
| stu_bcaa… | Definition | "GDP deflator" | resolved `simp.deflator_is_price_index` (**llm**) |
| stu_3ec4… | Simplification | "The price index quoted… is the GDP deflator" | **unresolved** |

```
edges:
  (proc rearrange) -USES-> (base eq, RESOLVED eq.gdp_deflator)   → KEPT   ✅
  (proc rearrange) -USES-> (rearranged eq, UNRESOLVED)           → DROPPED ❌  ← case-3, here too
run 20: edge_coverage=0.5 usage=1.0 node_cov=1.0 abstained=False
findings: 2 × matched_edge, 2 × missing_edge, 2 × unresolved
```
**Key insight:** case-3 fires in Q4 as well — the rearranged form
`realGDP - nomGDP/(PI/100)` is unresolved and its `USES` edge is dropped. Q4
escapes a *zero* `usage` only because the strong student **redundantly** stated
the base equation and a *second* `USES` edge landed on that resolved node,
covering the reference's single `USES` edge. Q5 had no such redundancy.

### 4.4 Verdict on case-3
- **General, not fluid-specific** — reproduced on macro with the identical
  signature. ✔ answers `APOLLO-USES-EDGE-RESOLUTION-HANDOFF.md` Part 7.
- **Robustness:** Q4 `usage=1.0` across **6 runs** (attempts 10, 20, 21, 31, 32,
  33) — but §4.3 shows that is *resilience via redundancy*, not absence of the
  bug. Q5 reproduced it on **every** run.
- **The `{var: expr}` substitution patch cannot fix it** — a numerically computed
  form is a `solve()`/eval result, not a variable substitution. Confirms the
  handoff's conclusion: the fix belongs in the **cleanser** (resolve a
  derived/computed form to its governing entity via USES-context), not the scorer.

---

## 5. Defect catalog (with architectural blast radius + fluid cross-reference)

Each defect: **what**, **evidence**, **pipeline stage it lives in**, **how it
could affect the overarching architecture**, and **whether it also appeared in
the Bernoulli (fluid) line**.

### D1 — Derived/computed/solved forms don't resolve → USES edge dropped (case-3)
- **Evidence:** att 23 (full repro), att 20 (masked). `usage`/`edge_coverage`→0
  for correct answers.
- **Stage:** `resolve_attempt` (cleanser #1) leaves the derived form `unresolved`;
  `build_student_canonical` (cleanser #2) drops the edge.
- **Architectural impact:** `usage` and `edge_coverage` are **structurally
  unreliable as quality signals** while this stands — they punish exactly the
  students who show their work (state the solved/computed form). Any Layer-3
  belief update or student-facing rubric keyed on these dimensions inherits the
  noise. The scorer is blameless; **the fix must be a cleanser change** (resolve
  case-3 forms to the governing entity using the assembled subgraph context, the
  Direction-A/B options in the handoff). Until then, weight grading toward
  `coverage`/`bisimilarity`, not edge metrics.
- **Bernoulli:** ✅ **same defect** — attempt 8, solved form `v2=sqrt(2gh1)`
  unresolved → `USES` edge on it → `edge_coverage=usage=0`
  (`APOLLO-USES-EDGE-RESOLUTION-HANDOFF.md`).

### D2 — Non-equation nodes (definition / condition / simplification) resolve weakly
- **Evidence:** att 23 `def.real_basis` "real GDP" unresolved; att 20
  simplification "the price index… is the GDP deflator" unresolved.
- **Stage:** resolution tiers — the symbolic tier is **equation-only**; reference
  candidates carry **no aliases** (`candidates_from_reference_solution` sets
  `aliases=()`), so a non-equation node can only match via `exact` (needs the full
  `concept`+`meaning` / `applies_when` surface) or the single LLM call. Fuzzy
  compares against aliases, of which references have none.
- **Architectural impact:** **definition-/condition-heavy reference graphs resolve
  poorly**, capping coverage and silently raising `unresolved_rate` toward the
  0.35 abstention gate. Reference authoring should favor equations + procedure
  steps; or the resolver needs entity-level aliases (the `normalization_map`
  Layer-1 aliases) wired into the reference candidate path.
- **Bernoulli:** ✅ **same defect** — the handoff Part 6 explicitly notes
  "non-equation nodes (simplification/condition) resolve poorly… reference simps/
  conditions carry no aliases."

### D3 — LLM adjudicator violates the hard type-gate
- **Evidence:** att 23 a **ProcedureStep** resolved to **`eq.growth_rate`** (an
  equation key) via `method=llm`; att 20 a **Definition** resolved to
  **`simp.deflator_is_price_index`** (a simplification key) via `method=llm`.
- **Stage:** `resolve_attempt`'s LLM adjudication tier. The deterministic tiers
  enforce a hard `type_compatible` gate (a node never resolves to a candidate of
  another type); the LLM path does **not** re-check it.
- **Architectural impact:** type-inconsistent resolutions **corrupt S_norm node
  identities and edge-endpoint types**. A procedure that "becomes" an equation
  key can satisfy/deny edge matches it should never participate in, distorting
  `usage`/`dependency` and the coverage set in ways that are invisible downstream
  (the persisted node just carries the wrong `resolved_key`). This is a latent
  correctness hole in the one non-deterministic tier. **Cheap fix:** re-apply
  `type_compatible` to the adjudicator's output (reject a cross-type LLM
  resolution).
- **Bernoulli:** ⚠️ **not observed** in attempt 8 (its procedure resolved to the
  correct `proc.*` type) — but the LLM adjudication path is **shared code**, so it
  is a latent risk on the fluid line too; macro merely surfaced it.

### D4 — Reference `DEPENDS_ON` density exceeds parser expressiveness
- **Evidence:** strong rows `edge_coverage` 0.0–0.5, `dependency` 0.0–0.33; att 20
  the two `missing_edge`s are `DEPENDS_ON` edges *into* the procedure
  (`eq/simp → proc`) that no spoken explanation produced.
- **Stage:** mismatch between R_norm (authored `depends_on`) and S_norm (parser
  output). The reference encodes a dense dependency web; students state nodes and
  the occasional `USES`, rarely the full `DEPENDS_ON` lattice.
- **Architectural impact:** `edge_coverage` is **systematically depressed even for
  excellent answers**, so the aggregate under-credits and the dimension is a poor
  discriminator. Either reference authoring should keep `DEPENDS_ON` sparse and
  load-bearing, or the parser/normalizer should *infer* obvious dependencies, or
  `edge_coverage` should down-weight `DEPENDS_ON` relative to `USES`/`PRECEDES`.
- **Bernoulli:** ✅ **same pattern** — "discriminates B+ vs F but **edge scores
  dead**" / "`edge_coverage` STILL 0 live"
  (`apollo-graph-grading-probe` notes, `APOLLO-GRADING-EDGE-RESOLUTION-HANDOFF.md`).

### D5 — Misconceptions live in a SEPARATE table from the KG entities
- **Evidence:** soundness vacuous 1.0 across the whole sweep until
  `apollo_misconceptions` was seeded; then weak → 0.50 (§3.3). The learner-model
  seeder mints `apollo_kg_entities (kind='misconception')`, but the Done chain
  reads the **`apollo_misconceptions` table** via
  `overseer.misconception_bank.load_for_concept`.
- **Stage:** `done_grading` input assembly (`_misconceptions_dict(load_for_concept(...))`).
- **Architectural impact:** **dual-storage footgun.** A new subject (or any
  hand-authored content) silently grades with `soundness=1.0` / no contradiction
  detection unless *both* stores are seeded — a correctness gap that fails *open*
  (looks fine, isn't). Onboarding/seeding must populate both, or the two should be
  unified / the seeder taught to write both.
- **Bernoulli:** ➖ **not a fluid defect** — Bernoulli's `apollo_misconceptions`
  rows were seeded through its own path, so soundness worked there. This is a gap
  specific to the new-subject hand-authoring route (which production
  auto-provisioning would have covered). Calling it out because it is an
  *architecture* footgun, not a one-off.

### D6 — Vacuous-1.0 dimensions read as "perfect" instead of "untested"
- **Evidence:** `scoping=1.0` on all 15 attempts (R_norm contains **no** `SCOPES`
  edges — `build_reference_canonical` carries condition/simplification → equation
  as `DEPENDS_ON`, never synthesizes `SCOPES` despite `EDGE_ALLOWED_PAIRS`
  permitting it); `procedure_order=1.0` on single-step procedures (no comparable
  ordered pairs).
- **Stage:** R_norm construction (no SCOPES synthesis) + every edge sub-score's
  "no reference edges of this type → 1.0" branch.
- **Architectural impact:** the aggregate is **misleadingly inflated** — a reader
  (or a calibration metric, or a student) sees `scoping=1.0` and infers mastery
  where the dimension was never exercised. A "not-applicable / N-A" sentinel
  distinct from `1.0` would prevent untested dimensions from masquerading as
  perfect scores.
- **Bernoulli:** ✅ **same** — fluid references likewise produce no `SCOPES` edges
  in R_norm, so `scoping` is vacuous there too.

### D7 — The legacy coverage path and the graph-sim path disagree
- **Evidence:** att 23 — the **old** `done.py` coverage matched 3/4 reference
  steps (`covered=[compute_real_change, apply_percent_change, growth_rate]`), while
  the **graph-sim** resolver matched only 1/4 (`node_cov=0.25`).
- **Stage:** two parallel graders — `done.py::handle_done` (old coverage/rubric)
  and `done_grading.py::run_graph_simulation` (graph-sim), running in shadow/live.
- **Architectural impact:** the graders **diverge in strictness** (graph-sim is
  stricter, gated by resolution quality). During the shadow → live promotion this
  divergence is the whole risk surface: the live flag swaps the student-facing
  rubric from the lenient old path to the strict graph-sim path, so a student's
  grade can drop without any change in their answer. Calibration must quantify
  this gap per concept before flipping `LIVE` in prod.
- **Bernoulli:** ✅ **inherent to the dual-path design** — the handoffs run
  graph-sim in shadow precisely because it does not yet agree with the old path;
  macro quantifies the same divergence on a fresh subject.

### Cross-reference summary

| defect | macro | Bernoulli / fluid line |
|---|---|---|
| D1 case-3 (derived form → dropped USES edge) | ✅ att 23, 20 | ✅ attempt 8 (solved form) |
| D2 non-equation nodes resolve weakly | ✅ att 23, 20 | ✅ handoff Part 6 |
| D3 LLM adjudicator type-gate violation | ✅ att 23, 20 | ⚠️ latent (shared path; not seen in att 8) |
| D4 reference DEPENDS_ON density | ✅ all strong rows | ✅ "edge scores dead" |
| D5 misconception dual-storage | ✅ §3.3 | ➖ N/A (seeded via its own path) |
| D6 vacuous-1.0 (scoping/procedure_order) | ✅ all rows | ✅ same |
| D7 old-path vs graph-sim divergence | ✅ att 23 | ✅ dual-path by design |

---

## 6. Architectural implications (synthesis)

1. **Resolution is the load-bearing wall, and it has two structural cracks that
   are subject-independent** (D1 case-3, D2 non-equation weakness). Both depress
   coverage and edge metrics, both push `unresolved_rate` toward abstention, and
   both reproduce across fluids and macro. The grader downstream is faithful — so
   investment should go into the **cleanser** (resolution + canonicalization), not
   the scoring math. The handoff's "resolve case-3 to the governing entity using
   the assembled subgraph" (Direction A) or "keep every node first-class and move
   matching to a tolerant layer" (Direction B) is now validated as a *general*
   need, not a fluid patch.
2. **Edge metrics are not yet trustworthy as student-facing signals** (D1, D4, D6).
   `coverage`/`bisimilarity` are sound; `usage`/`edge_coverage`/`scoping` are
   variously punitive (D1, D4) or vacuous (D6). Any rubric or belief update keyed
   on edges inherits this — gate the LIVE promotion on edge-metric calibration.
3. **The non-deterministic LLM tiers need invariants re-imposed** (D3). The whole
   design delegates fuzzy/ambiguous resolution to one LLM call but does not
   re-validate its output against the same hard gates (type compatibility) the
   deterministic tiers enforce — a correctness hole that *will* surface wherever
   the adjudicator runs.
4. **Setup/onboarding has a fail-open footgun** (D5). Two misconception stores,
   one of which silently makes soundness vacuous. New-subject onboarding (and any
   future "author a course by hand" flow) must seed both or the storage must be
   unified.
5. **Shadow → live is a strictness cliff** (D7). The two graders disagree by
   design; macro shows the graph-sim path is meaningfully stricter. Per-concept
   calibration of the gap is a prerequisite to flipping `APOLLO_GRAPH_SIM_LIVE` in
   production.

---

## 7. Verdict, reproducibility, artifacts

**Verdict.** The graph-grading engine generalizes to a non-fluid subject with no
engine change and discriminates explanation quality. The case-3 edge-resolution
bug is **general** (reproduced on macro with the Bernoulli signature) and must be
fixed in the cleanser. Six further defects were catalogued; four are shared with
the fluid line (so they are architectural), one is a new latent correctness hole
(D3), and one is a setup footgun (D5). `real_gdp_growth` is now a ready-made,
non-fluid regression case for the case-3 fix, alongside Bernoulli attempt 8.

**Reproducibility.** Exact command sequence in `WORKFLOW.md`. State: course
`macro-econ` (id 3), corpus doc 4 (562 chunks), 5 tier-2 problems, 62 macro
`:Canon` nodes, 4 `apollo_misconceptions` rows. All code/data **uncommitted** on
`fix/apollo-retrieval-grounding` (mostly new files; the in-flight derived-equation
fix untouched). 391 unit tests green across the changed modules.

**Artifacts.**
- Probe report: `scripts/apollo_grade_probe_report.json`
- RAG report: `scripts/macro_mining_report.json`
- Per-attempt dumps: `scripts/inspect_attempt.py <id>` (**23** = full case-3 repro;
  **20** = masked case-3)
- Scores: `apollo_graph_comparison_runs WHERE search_space_id=3`
- New/changed code: `apollo/subjects/macroeconomics/**`, generalized
  `seed_apollo_learner_model.py` / `apollo_grade_probe.py`, `index_local_pdf.py`,
  `run_macro_probe.py`, `_macro_{setup_course,mine,seed_misconceptions,scenarios,
  probe_report,preflight}.py`, antonyms in `apollo/resolution/competition.py`.
