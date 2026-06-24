# Subject-Fluid Apollo — Authored Problems → Teachable Graphs → Student KG

- **Date:** 2026-06-23
- **Branch:** `ApolloRun` (feature branch; never merge to `main` without explicit go-ahead)
- **Status:** Design approved; ready for `/feller:do`
- **Supersedes/relates:** the textbook-scrape provisioning path (`apollo/provisioning/`),
  the Phase-2 structured-scrape design (`2026-06-23-apollo-structured-scrape-design.md`).
  Textbook scraping is **removed from scope** by this design (see Context).

---

## 1. Context & Problem

Apollo is a learning-by-teaching (Feynman) tutor. A student teaches a problem back; Apollo
grades their reconstructed knowledge graph against a **reference solution graph** and updates
the student's learner model. The set of teachable items is **Tier-2** (`:Canon` + the
per-problem reference graph stored in `apollo_concept_problems.payload`).

The current auto-provisioner is **fluid-mechanics-specialized by construction**:

- The promotion lint's **gate 5** (`promotion_lint.py:200`) requires the terminal procedure
  step to use an equation whose SymPy free-symbols include `problem.target_unknown`. But
  `target_unknown` is a **prose phrase** ("average value", "net signed area"), so gate 5 is
  unsatisfiable for non-symbolic subjects. Calculus scrapes fine but promotes **0**.
- The teachable-unit model (solve-for-a-symbol via equations, SymPy-validated) fits Bernoulli,
  poorly fits calculus, and not at all political science.

A live investigation (2026-06-23) also established two facts that **reframe the input**:

1. The single completed calculus provisioning run scraped a handful of questions and promoted
   0; the "~90 questions" was a projection — earlier large scrapes were **inserted then rolled
   back** (the orchestrator commits once at run end), so nothing persisted.
2. The calculus index has **lossy math extraction** — numeric values and inline math symbols
   were dropped at PDF ingestion (verified against raw `aita_chunks`), so every scraped math
   problem is an unsolvable shell. This is an upstream indexing problem, not Apollo's.

**Decision (product owner):** textbook question-generation is **out of scope**. Apollo will
ingest **professor-authored problem sets** (clean, complete, every value explicit), in three
solution-completeness flavors, and build teachable graphs from them. The value is (1) building
the **student's** knowledge graph via teach-back, and later (2) generating targeted practice
(PrairieLearn-style) — Goal 2, a **separate later spec**.

## 2. Goal & Scope

Make Apollo's teachable-unit pipeline **subject-fluid**, chosen dynamically per subject, working
for **quantitative-symbolic** (fluid mechanics) and **qualitative-argumentative** (political
science) courses, fed by **professor-authored problem sets**.

**In scope (A + B + C, one spec):**
- **A. Ingest** a professor-authored problem set into discrete problems (statement + optional
  solution + optional worked procedure), classified by completeness.
- **B. Build & validate** a subject-fluid teachable **reference graph** per problem, behind a
  pluggable **subject profile** (node vocabulary, gate set, target contract, validator).
- **C. Teach-back → student KG:** prove the existing `graph_compare` grader + learner model run
  end-to-end on the new graphs for **both** a fluid problem and a polisci problem.

**Out of scope:**
- Textbook scraping / question generation from textbooks (deleted path).
- PrairieLearn-style targeted problem generation (Goal 2 — later spec).
- Argument-native node types (`claim`/`evidence`/`warrant`/…) — deferred; v1 **reuses the
  existing general node subset** (see §4).
- Fixing math-extraction fidelity in indexing (separate upstream track).

## 3. Non-Negotiable Constraints

- **Feynman teach-back is the product.** Every artifact serves a student teaching a concept back
  to build their knowledge graph. No "study bank" / read-only paths.
- **No fluid-mechanics regression.** The `quantitative_symbolic` profile must reproduce today's
  behavior; the **41 seeded ss=2 `:Canon`** must still promote, and the existing provisioning
  tests must stay green.
- **CLAUDE.md rules:** citations non-negotiable; never bypass the semantic filter; **no new
  packages** without confirming; **feature branch only**, never push to `main`; new features need
  tests with the Supabase mock fixtures (`conftest.py`).
- **Staging vs prod:** any DB write/verification targets **staging** (`hjevtxdt…`), never prod
  (`uduxdnii…`). Confirm the active `SUPABASE_DB_URL` before any DB run.
- **Single-flight:** before any live provisioning/drain run, `pgrep -f apollo.provision_worker`
  and check `apollo_provisioning_jobs` for `state='running'`; bound scope so a run finishes well
  under the 30-min lease.
- **Drift contract:** update `docs/architecture/apollo.md` in the same commit as code changes and
  bump `last_verified`.

## 4. Architecture

### 4.1 The subject profile (the spine)

A **subject profile** is a per-subject strategy that declares everything subject-specific. It is
**auto-detected once and persisted** on the `apollo_subjects` row; the gates and validators then
run **deterministically** against the persisted profile (no LLM in the per-promotion control path).

A profile declares:
- `node_vocab`: the allowed `entry_type` subset.
- `active_gates`: which promotion-lint gates run.
- `target_contract`: `symbol` | `prose` | `none`.
- `validator`: which reference-solution validator to apply.

**Two built-in profiles for v1:**

| Profile | node_vocab | active_gates | target_contract | validator | Subjects |
|---|---|---|---|---|---|
| `quantitative_symbolic` *(default)* | all 6 types | **1–8** | `symbol` | symbolic (SymPy) | fluid mech, calculus |
| `qualitative_argumentative` | `procedure_step`, `definition`, `condition` | **1, 2, 3, 8 + faithfulness** (4,5 OFF; 6,7 vacuous) | `prose` | faithfulness / rubric | political science |

Grounding facts (verified):
- All 6 entry types are already in the gate-1 mint map (`learner_model_seed.py:86`), so an
  argument graph built from `procedure_step`/`definition`/`condition` passes gate 1 **with no
  mint-map change**.
- Gates **1, 2, 3, 8** are structural/dedup → subject-agnostic. Gates **6, 7** pass vacuously on
  an equation-less graph. Gates **4, 5** are the *only* ones that actively break for arguments →
  the profile must be able to switch them off.
- `graph_compare.grade_attempt` is structural (coverage/soundness/bisimilarity over typed
  nodes/paths) and already supports `procedure_step`/`definition`/`condition`. The dormant
  **rubric** (`compute_rubric`) and **opposes/contradiction** machinery are available for
  counterargument/misconception grading.
- `solution_source` already allows `authored` (`apollo_concept_problems`).

**Profile assignment / detection.** A **profile-neutral probe** runs once over the ingested
problem set (does it carry equations / numeric answers / symbolic targets, vs prose arguments)
and writes `profile_kind` + `profile_confidence` + `profile_evidence` to `apollo_subjects`.
**Fail-open to `quantitative_symbolic`** (the strictest, back-compat-preserving profile) on low
confidence or probe error, and log it for review. Re-detection is explicit (corpus change / flag).

### 4.2 A — Ingest authored problem sets

Parse a problem set into discrete `AuthoredProblem` records, each = `statement` +
`solution?` + `worked_procedure?`, classified into three completeness cases:

1. **Worked** (statement + solution + procedure) → *structure* the professor's procedure.
2. **Answer-only** (statement + final answer, no procedure) → *reconstruct* the procedure,
   **anchored to the known answer**.
3. **None** (statement only) → *generate* the solution; mark lower confidence for review.

**v1 input assumption (stated, not asked):** authored sets arrive as a **structured input**
(one problem per record, explicit fields) — never re-introduce extraction-loss. A loader-format
adapter is a small, isolated unit.

### 4.3 B — Subject-fluid reference-graph construction & validation

For each `AuthoredProblem`, build the reference graph in the **profile's** `node_vocab`:
- **Worked** → extract/structure the authored procedure into typed steps.
- **Answer-only** → constrained generation to the known answer.
- **None** → RAG/generation, flagged.

Faithfulness is judged **against the professor's authored solution** (authoritative ground
truth) rather than RAG'd chunks. Promotion runs the **profile-gated** lint: universal gates
(1, 2, 3, 8) + faithfulness always; symbolic gates (4–7) only for `quantitative_symbolic`.
On pass → flip Tier-2, store the annotated graph, project `:Canon`.

### 4.4 C — Teach-back → student knowledge graph

The existing teach-back loop (`graph_compare` grade → learner-model update) must run on the new
reference graphs **subject-fluidly**. Verify end-to-end for one fluid problem (back-compat) and
one polisci problem (new), including a student turn that writes `apollo_learner_state` /
`apollo_mastery_events`.

## 5. File-Level Touch Points (verify each before editing; load owner doc per drift contract)

- `apollo/persistence/models.py` — `Subject`: add `profile_kind`, `profile_confidence`,
  `profile_evidence` columns. **+ new migration** (`database/migrations/…` / Supabase migration);
  default existing subjects to `quantitative_symbolic`.
- `apollo/provisioning/subject_profile.py` *(new)* — profile dataclass/registry + the two
  built-ins + the profile-neutral **detection probe** + persistence read/write.
- `apollo/provisioning/promotion_lint.py` — make the gate list **profile-driven**
  (`run_promotion_lint(..., active_gates=...)` or a profile arg); gates 4 & 5 become conditional.
  Keep the pure / DB-free / LLM-free contract.
- `apollo/provisioning/promote.py` — resolve the subject's profile; pass `active_gates` +
  `validator` selection into the lint; keep `:Canon` projection unchanged.
- `apollo/provisioning/ingest.py` *(new — replaces scrape as the Stage-1 entry)* — load a
  structured authored problem set → `AuthoredProblem` records + completeness classification +
  Tier-1 write. **Commit Tier-1 inventory independently** of downstream stages (fix the
  write-then-rollback hazard).
- `apollo/provisioning/solution.py` — `find_or_generate`: add the three-completeness branching
  (worked / answer-only / none); ground faithfulness on the **authored** solution; honor the
  profile's `target_contract` (don't force a symbol target).
- `apollo/schemas/problem.py` — `target_unknown` and `given_values` become **optional per
  profile** (prose/empty allowed for `qualitative_argumentative`); keep them required-shaped for
  `quantitative_symbolic`.
- `apollo/provisioning/pairing_gate.py` — faithfulness grounded against the authored solution.
- `apollo/provisioning/orchestrator.py` — wire: detect/resolve profile → ingest → profile-gated
  per-candidate pipeline → promote. Replace `_SCRAPE_SYSTEM_PROMPT`/`_TRIAGE_SYSTEM_PROMPT` usage.
- `apollo/graph_compare/` + `apollo/grading/` — **verify** (mostly reuse) the grader + rubric +
  opposes path runs on argument graphs; wire `opposes_map` if needed for counterarguments.
- `apollo/provisioning/tests/`, `apollo/tests/`, `apollo/graph_compare/tests/` — new fixtures:
  a polisci authored set + a fluid authored set; back-compat regression test; profile-detection
  test; profile-gated-lint test. Use the Supabase mock fixtures (`conftest.py`).
- `docs/architecture/apollo.md` — update owner doc + `last_verified` in the same commit.

## 6. Acceptance Criteria

1. **Back-compat (hard):** with the `quantitative_symbolic` profile, the existing provisioning +
   lint test suites pass unchanged, and a fluid authored problem with a symbolic target promotes
   exactly as today; the 41 ss=2 `:Canon` still promote (re-projection is a no-op).
2. **Subject-fluid promotion:** a **political-science** authored problem (statement + worked
   argument, prose target, no equations) **promotes to Tier-2** under `qualitative_argumentative`
   — i.e., it passes gates 1/2/3/8 + faithfulness with gates 4/5 correctly skipped.
3. **Completeness handling:** all three cases (worked / answer-only / none) produce a valid
   reference graph or a clean per-candidate rejection (never a run abort).
4. **Profile detection:** the probe assigns `quantitative_symbolic` to a fluid set and
   `qualitative_argumentative` to a polisci set, persists kind+confidence+evidence, and
   **fails open to `quantitative_symbolic`** on low confidence (with a log).
5. **Durable Tier-1:** ingested inventory is committed independently of downstream stages (an
   interrupted run no longer loses scraped/ingested problems).
6. **Teach-back (C):** a student teaches back one fluid and one polisci promoted problem; the
   grader returns a `GradeResult` and the learner model (`apollo_learner_state` /
   `apollo_mastery_events`) updates for both.
7. **Gates stay pure:** `promotion_lint` remains DB-free / LLM-free; the profile/active-gates are
   passed in by the caller.
8. **Tests:** all new behavior covered with Supabase mock fixtures; `pytest` green.

## 7. Testing Strategy

- Unit: profile registry + detection probe (fail-open), profile-gated `run_promotion_lint`
  (4/5 off path), three-completeness construction, schema optionality per profile.
- Integration (mock LLM, Supabase mock): ingest → build → profile-gated promote for a fluid set
  and a polisci set; back-compat regression on the fluid path.
- E2E verification (staging, **opt-in, bounded, single-flight**): a capped live run on a small
  authored polisci set with a monkeypatched rejection-printer to confirm the gate path; real
  gpt-4o tokens; reset any stranded `running` job first.

## 8. Migration & Back-Compat

- New `apollo_subjects` columns are additive; backfill existing rows to `quantitative_symbolic`.
- The migration-tracking table in staging is known out-of-sync — **trust schema inspection**, not
  `list_migrations`.
- The provisioning queue is "FROZEN"; the independent-Tier-1-commit change must be scoped and
  tested, not an ad-hoc edit (it intersects the commit-once hazard).

## 9. Suggested Implementation Order (for the planner)

1. Subject-profile model + migration + detection probe (fail-open) + persistence.
2. Profile-gated `run_promotion_lint` + `promote` wiring (back-compat proven first).
3. Schema optionality (`target_unknown`/`given_values` per profile).
4. Authored-problem ingest (A) + independent Tier-1 commit.
5. Three-completeness reference-graph construction (B) + authored-solution faithfulness.
6. Teach-back verification on both subjects (C).
7. Tests + docs (`apollo.md`) throughout; bounded staging E2E last.

## 10. Open Assumptions

- Authored sets arrive **structured** (one problem per record). A different input format (PDF)
  would need a parsing front-end and is explicitly deferred.
- A political-science authored set + a fluid authored set must be available in **staging** to run
  the E2E verification; unit/integration coverage does not depend on them.
- `qualitative_argumentative` v1 drops gate-5's chain-coverage sub-check along with the symbolic
  terminal check; acceptable given simpler/sparser argument graphs. Revisit if argument graphs
  grow procedural.
