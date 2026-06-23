# WU-3B2 split proposal (build-ready, FINAL/revised) — §8B materials→Apollo AUTO-PROVISIONING

**Date:** 2026-06-19
**Author:** scoping pass (autonomous stacked-PR build) — DESIGNER → REVISER over three critic lenses (feasibility / spec-fidelity / ops-cost-safety). Every disputed code claim re-verified file:line by the reviser on 2026-06-19.
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §8B (lines 1225-1410, AUTHORITATIVE), §8 minting (976-1075), §8A wiring (1076-1224), §6.7 calibration (820-838), §1.4 course isolation, §2 Postgres-as-record, §1.8 line 153 (auto-provisioning brought into v1).
**Status:** PROPOSAL ONLY — no code/tests/source touched. Decomposes the XL final unit WU-3B2 into a STACKED sequence of independently-buildable + verifiable sub-units; the per-sub-unit TDD plan is a separate later step. This is the REVISED final version: three critics reviewed the prior draft; EIGHT of their findings were valid and are folded in below, one new gap (`scope_summary` does not exist) was found during re-verification, and each resolution is recorded in §9.
**Templates (mirrored):** `…wu4a/4b/4c/5a/5b/5b3/6a-split-proposal.md`.
**Stacks on:** `feat/apollo-kg-wu6a3-selection-wiring` (#49, tip `a869dc9`) — the FIRST sub-unit's diff-cover compare branch; the rest stack intra-WU (each compare-branch = the previous sub-unit's branch).
**Backbone note:** WU-3B2 is the LAST unit of the whole Apollo KG learner-model build — when it lands the spec is built. It is an INGESTION subsystem: on every teacher-material upload it scrapes questions, finds-or-generates a reference solution, validates the pairing, mints reference + misconception entities, dedups course-locally, and promotes problems Tier-1→Tier-2 through an 8-gate PURE lint — fully automatic, NO human in the loop. It ships behind `APOLLO_AUTOPROVISION_ENABLED` (default OFF) AND behind the §6.7 shadow gate (auto-provisioned grades never move Layer-3 beliefs — `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is OFF everywhere). It does NOT relax §1.4 (course isolation) or §2 (Postgres is the system of record); the ONLY Neo4j artifact is the rebuildable `:Canon` projection.

---

## VERDICT (TL;DR)

**Split WU-3B2 into EIGHT stacked sub-units. Build the migration + the PURE 8-gate validator + the PURE dedup-math FIRST (3 LLM-free units that unblock everything and are the safety core), then the LLM pipeline stages, then the claim/lease queue-drain, then the trigger/worker orchestration, then the anomaly-quarantine backstop.** The seam set is dictated by the **LLM-free vs LLM-required testability fault line** and the **idempotency/cost-boundary** the ai-pipeline planner must declare per stage. The prior draft proposed SEVEN units; the feasibility critic correctly showed 3B2f fused TWO distinct templates (the janitor poll-shell + a net-new SKIP-LOCKED claim/lease drain) and three OPS defects (no token source for the cost budget, idempotency that doesn't survive a re-index, an under-specified quarantine join). The revised split **promotes the claim/lease DB-queue drain to its own sub-unit (3B2f-queue)** so the orchestrator (3B2g) stays an L pass, and pins the cost-metering, content-key idempotency, and quarantine-join contracts explicitly.

- **WU-3B2a** — MIGRATION 030 (tier/solution_source/provenance/quarantine cols + denormalized `search_space_id` + **a content-key `chunk_content_hash` + a `scope_summary` text column** + 4 observability tables + the provisioning job-queue table) + ORM + the **Tier-2 selection gate**. Real-PG. **The migration owner.**
- **WU-3B2b** — the PURE 8-gate promotion lint (the safety core; fixtures, NO LLM). Gate 4 is the ONLY foreign-symbol guard (gate 6 does NOT reject foreign symbols — sympy auto-creates). One positive fixture + one adversarial fixture per gate.
- **WU-3B2c** — the COURSE-LOCAL dedup ladder (slug → embedding-similarity → LLM-judge), course-scoped (NEVER global), deterministic MOCK-embedding cross-course non-merge proof. Writes `apollo_dedup_decisions`. Defines the embedding source (the new `scope_summary` text, embedded on the fly).
- **WU-3B2d** — LLM stage 1 (SCRAPE) + stage 4 (TAG/MINT). Reads existing `AITAChunk` rows; scrape → Tier-1 rows keyed on `chunk_content_hash`; **populates `apollo_concepts.canonical_symbols`/`normalization_map` for new concepts (so gate 4 isn't vacuous)**; reuses the §8 seed's pure `*_to_entities` minting. Resolves the `entry_type` vocabulary mismatch.
- **WU-3B2e** — LLM stage 2 (FIND-OR-GENERATE) + stage 3 (PAIRING/CORRECTNESS GATE). One "solution lifecycle"; RAG-grounded; **fail-CLOSED**.
- **WU-3B2f** — the **claim/lease SKIP-LOCKED queue-drain** (templated on `run_upload_worker_loop`/`_claim_upload_job_async`, NOT the janitor) + the per-call **metered LLM client** that captures `response.usage` into the `ingest_run` aggregate (the cost-budget data source). Real-PG concurrency tests.
- **WU-3B2g** — stage-0 TRIGGER/enqueue (content-key idempotent) + the provisioning WORKER **shell** (mirrors `learner_janitor_worker.py`: loop/flag/SIGTERM) + the 6-stage ORCHESTRATOR (calls 3B2b's lint + the existing `project_canon`) + observability writes + lease-reaper/terminal-status reconciliation.
- **WU-3B2h** — the per-problem ANOMALY QUARANTINE backstop (PURE statistic + the real-PG findings→runs→attempts aggregation join) + the shadow-gate VERIFICATION (SHADOW ON computes findings, LAYER3 OFF → zero belief movement).

**Build order (forced by the dependency DAG + LLM-free-first):** `3B2a → 3B2b → 3B2c → 3B2d → 3B2e → 3B2f → 3B2g → 3B2h`. The migration and the pure validator land before ANY LLM stage so promotion can never run unguarded and the safety core is reviewable in isolation. See §9 for how each critic finding was resolved and §OPEN-DECISIONS for the genuine judgment calls left to the orchestrator.

---

## 0. What WU-3B2 is (and the boundaries it must not cross)

WU-3B2 is the §8B six-stage auto-provisioning PIPELINE plus the 8-gate lint, the 3 backstops, and the observability tables. On every material upload it writes the **SAME course-scoped Postgres rows the §8 seed script writes** (`apollo_concept_problems`, `apollo_kg_entities`, `apollo_entity_prereqs`, misconception entities), only automatically and eagerly at upload time. The 6 stages (§8B.2, each with an explicit Pydantic in→out type, question-first per §1.5):

1. **SCRAPE** — LLM over the document's already-embedded chunks → candidate questions with provenance `(document_id, page, chunk_id)` + LLM difficulty; written as **Tier 1** (inventory, NOT teachable), `search_space_id`-scoped. Keyed on a stable `chunk_content_hash` (NOT the volatile `aita_chunks.id`) so a re-index is a no-op (§9 OPS-2).
2. **FIND-OR-GENERATE** reference solution — retrieve a printed solution (`solution_source=extracted`) else LLM-generate RAG-grounded (`solution_source=generated`).
3. **MODEL APPROVAL** pairing & correctness gate — one LLM validator per `(question, solution)` pair (the automated stand-in for §8 teacher approval).
4. **CONCEPT/ENTITY TAGGING + MINTING** — tag concepts/entities, **author the concept's `canonical_symbols`/`normalization_map`**, prereq edges LLM-drafted, reference nodes minted into Layer-1 entities + misconception competitors attached; reuses the §8 seed's pure converters.
5. **COURSE-LOCAL DEDUP** — resolve candidates against THIS course's inventory: slug → embedding similarity → LLM-judge tiebreaker; NEVER global.
6. **AUTOMATED PROMOTION LINT** → Tier 2 + `:Canon` rebuild — run the 8-gate validator; pass → flips Tier 2 + `:Canon` rebuilds; fail → `rejected_problems`, never a student.

### What WU-3B2 does NOT own (frozen upstream — DO NOT modify)
- The §6 graph-sim grading core (`apollo/graph_compare/**`, WU-4A/4B) and `apollo/handlers/done.py`/`done_grading.py`/`done_inputs.py` (WU-4C). 3B2 produces Tier-2 problems that flow through the ALREADY-gated shadow path with ZERO `done.py` change (§5 integration map).
- `apollo/persistence/learner_model_seed.py` (frozen WU-3B) — the PURE, DB-free, LLM-free converter core. 3B2 **IMPORTS** `EntitySpec`, `reference_solution_to_entities`, `misconceptions_to_entities`, `concept_dag_to_prereqs`, `annotate_reference_solution`, `validate_reference_graph`, `_ENTRY_TYPE_TO_KIND_PREFIX`. **EXCEPTION:** if OPEN-DECISION #5 resolves to "extend the map," 3B2d edits `_ENTRY_TYPE_TO_KIND_PREFIX` (a frozen-module edit, flagged).
- `apollo/knowledge_graph/canon_projection.py::project_canon` (WU-3C1) — stage-6 CALLS it.
- The indexing/upload pipeline (`indexing/**`, `knowledge/teacher_weekly.py`) — 3B2 hooks a THIN enqueue at the completion seam and READS existing chunks; it does NOT re-index.
- The §8 seed DB writer `scripts/seed_apollo_learner_model.py` — its upsert/idempotency KEYS are the template 3B2's writer PARALLELS; the script itself hardcodes the bernoulli concept + reads from disk, so it is WRAPPED, not imported.
- `apollo/agent/_llm.py::cheap_chat`/`main_chat` — REUSED for the LLM stages, but they return only `str` and discard usage. The metered wrapper that captures `response.usage` is **NEW in 3B2f** (it does NOT mutate `_llm.py` — §9 OPS-1).

---

## 1. Ground truth discovered (load-bearing facts, file:line — RE-VERIFIED 2026-06-19 by the reviser)

Every anchor re-checked against the real code. Facts that SHAPE the split are flagged ⚠.

- **`apollo_concept_problems` lacks tier/solution_source/provenance** (`018_apollo_concept_registry.sql:77-90`; ORM `apollo/persistence/models.py:158-169`): cols are `id, concept_id (BIGINT FK), problem_code (Text), difficulty (Text), payload (JSONB), created_at`; `UNIQUE(concept_id, problem_code)`; index `(concept_id, difficulty)`. **→ MIGRATION 030 needed.** CONFIRMED.
- **Next-free migration = 030.** On-disk top is `029_chat_turn_keywords.sql` (verified `ls database/migrations/*.sql`: …026, 027, 028, 029). CONFIRMED. (The `023` collision — `023_apollo_auth_scoping.sql` + `023_chunks_halfvec_hnsw.sql` — is older and irrelevant to 030.)
- ⚠ **`apollo_concept_problems` has NO direct `search_space_id` — it is course-scoped TRANSITIVELY** via `concept_id → apollo_concepts.subject_id → apollo_subjects.search_space_id`. **DECISION (§OPEN-DECISIONS #3): the 4 observability tables + the job-queue carry `search_space_id` DIRECTLY** (mirroring 026's runs/findings). Whether to ALSO denormalize onto `apollo_concept_problems` is the open sub-decision (recommended: yes, for the Tier-2 selection gate's course-scoped index).
- ⚠⚠ **NEW GAP — `scope_summary` does NOT exist.** The §8B dedup ladder (stage 5) keys "embedding similarity" on a per-entity `scope_summary`, but NO such column exists on `apollo_kg_entities` (`models.py:362-389`: only `canonical_key`, `kind`, `display_name`, `payload`, `aliases`) NOR on `apollo_concepts` (`models.py:136-155`: `slug`, `display_name`, `canonical_symbols`, `normalization_map`, `parser_prompt_template`, `solver_hints`, `forbidden_named_laws`, `concept_dag` — no embedding, no `scope_summary`). **3B2c MUST define the embedding source.** Recommended (§OPEN-DECISIONS #11): add a nullable `scope_summary TEXT` to `apollo_kg_entities` in migration 030 (authored at mint time from `display_name`+canonical symbols), embedded ON THE FLY by the dedup ladder (no persisted vector column → no pgvector-on-entities migration). The reviser flags this because the prior draft assumed `scope_summary` existed.
- **The §8 seed reuse surface is real and PURE** (`apollo/persistence/learner_model_seed.py`, DB-free + LLM-free by docstring `:22-24`): `EntitySpec` frozen dc, `reference_solution_to_entities(problem)→list[EntitySpec]` (`:202`), `misconceptions_to_entities(misc)→list[EntitySpec]` with `opposes_entity_key` payload (`:240`), `concept_dag_to_prereqs(dag)→list[(from,to)]` (`:133`), `annotate_reference_solution(problem, key_for_node)→dict` (`:272`), `validate_reference_graph(problem)→ReferenceGraphValidation` (`:307`), `_ENTRY_TYPE_TO_KIND_PREFIX` (`:80-86`), `_entity_key_for_step` (`:196-199`). CONFIRMED.
- ⚠⚠ **`validate_reference_graph` is NOT the 8-gate lint — it is GATE 2 ONLY.** It checks §6.1 closure (every step has a non-empty `entity_key`; `declared_paths` present + cover all nodes; paths reference real node ids) (`:307-364`). The §8B.4 eight gates are NET-NEW but reuse `EDGE_ALLOWED_PAIRS`/`Problem`/`to_kg_graph`/`parse_zero_form`. **3B2b WRAPS it as gate 2; it does NOT replace the validator.**
- ⚠ **`_ENTRY_TYPE_TO_KIND_PREFIX` lacks `variable_mapping` and has NO fallback.** `_entity_key_for_step` (`:196-199`) does `_ENTRY_TYPE_TO_KIND_PREFIX[entry_type]` — a bare dict index. The map (`:80-86`) keys `equation|condition|simplification|procedure_step|definition`; `Problem.EntryType` (`schemas/problem.py:33-36`) ALSO includes `variable_mapping`. **A `variable_mapping` reference step KeyErrors at MINT time, and gate-1 schema validation ACCEPTS it (a gate gap — the failure is mid-mint, not at lint).** RESOLUTION (§9 SPEC-3, §OPEN-DECISIONS #5): prefer option (b) — extend the frozen map to cover `variable_mapping` so seeded and auto-provisioned problems share the full vocabulary; if the frozen edit is disallowed, ADD a gate-1 sub-check that rejects any `entry_type` not in the mint map so the mismatch fails CLOSED at the lint. Do NOT silently drop.
- ⚠ **`concept_id` namespace clash.** `Problem.concept_id` is a string slug (`schemas/problem.py:50`); the DB FK `apollo_concept_problems.concept_id` is BIGINT (`018:79`, `models.py:164`). Scrape mints a string slug; persistence resolves it to the BIGINT row. Gate-8's dup key + dedup first-writer-wins MUST key on the BIGINT, never the slug (§OPEN-DECISIONS #6).
- ⚠ **`ProblemAttempt.problem_id` is TEXT, == `ConceptProblem.problem_code`** (`models.py:270`), NOT the BIGINT `ConceptProblem.id`. This is the SAME TEXT-vs-BIGINT namespace as `concept_id`, and it governs the QUARANTINE join (§9 OPS-3, 3B2h). The quarantine write targets `apollo_concept_problems.quarantined_at` (BIGINT id), so the join is `findings.run_id → runs.attempt_id → attempts.problem_id (TEXT) → (concept_id, problem_code) → concept_problems`.
- **The indexing-completion hook is exact** (`knowledge/teacher_weekly.py`): `_index_existing_upload_async` flips READY at `:1134` and commits the session at `:1174`; the job flips `JOB_STATE_COMPLETED` at `:1163-1169` in the SAME session. The caller seam `_process_claimed_upload_job` runs `_index_existing_upload_async` at `:599-607`. **The transactional enqueue point is inside that `:1131-1174` session.** Scope: `upload.search_space_id`, `document_id`.
- ⚠⚠ **The `(document_id, chunk_id)` idempotency premise does NOT survive a re-index.** `embed_and_persist_chunks` (`checkpoint_indexer.py:90-128`, docstring `:95`) "deletes and reinserts that page's chunks," and `aita_chunks.id` is an autoincrement surrogate — so a re-upload of UNCHANGED content gets NEW `chunk_id`s and re-burns the full LLM pipeline, deduped only at gate-8's runtime hash AFTER scrape+generate+pairing spend. RESOLUTION (§9 OPS-2): the Tier-1 idempotency key is a stable **`chunk_content_hash = sha256(normalize(chunk content))`** (NOT `chunk_id`), AND a document-level `content_hash` on `apollo_ingest_runs` short-circuits enqueue when an unchanged document already has a succeeded run (compare against `AITADocument.content_hash` already computed at `teacher_weekly.py:1041`). Gate-8 is a LAST-resort net, not the primary re-upload guard.
- ⚠ **OWNER-DOC BOUNDARY at the trigger.** The enqueue edit lands in `knowledge/teacher_weekly.py` (owned by **domain-data.md**) but the provisioning logic lands in `apollo/**` (owned by **apollo.md**). 3B2g touches BOTH → reconcile same-commit; keep the `teacher_weekly.py` edit to a THIN enqueue call.
- ⚠⚠ **The worker is TWO templates, not one.** `apollo/learner_janitor_worker.py` is a PURE poll SHELL — its docstring (`:7-16`) explicitly says "owns ONLY the process shell … adds NO drain logic, NO claim/lease/backoff/dead-letter, NO SQL." It owns the loop (`_loop`/`_run_one_iteration`), the per-iteration flag read, SIGTERM/SIGINT, the single `asyncio.run(main())`, and the Procfile line. The **claim/lease/SKIP-LOCKED drain is a DIFFERENT module:** `TeacherWeeklyStorage.run_upload_worker_loop` (`teacher_weekly.py:348`) → `process_next_upload_job` (`:340`) → `_claim_upload_job_async` (`:881`) which does `.with_for_update(skip_locked=True)` (`:902`) + sets `lease_owner`/`lease_expires_at`/`attempt_count` (`:913-915`). **3B2's worker SHELL mirrors the janitor; the queue DRAIN is net-new templated on `run_upload_worker_loop`.** The drain is the part with real concurrency-correctness risk (lease expiry, attempt_count, dead-letter on cost-abort) → it is its own sub-unit (3B2f, §9 FEAS-1).
- **The shadow plug-in needs ZERO `done.py` change.** TWO DISTINCT flags (verified `done.py:67/74/84`): `APOLLO_GRAPH_SIM_SHADOW_ENABLED` (`:67`) gates COMPUTE (the chain runs + persists `apollo_graph_comparison_runs`/`findings`); `APOLLO_GRAPH_SIM_LIVE_ENABLED` (`:74`) gates student-facing rubric DISPLAY; `APOLLO_GRAPH_SIM_LAYER3_ENABLED` (`:84`, "default OFF EVERYWHERE incl. prod + staging") gates BELIEF PERSIST. **For an auto-provisioned problem to ACCUMULATE the calibration/quarantine findings, SHADOW must be ON while LAYER3 stays OFF** (§9 SPEC-4). `handle_done → _find_problem → loads from apollo_concept_problems payload` (the SAME table 3B2 writes). As long as the auto-provisioned payload satisfies `Problem` + `validate_reference_graph`, a Tier-2 problem plugs in transparently. CONFIRMED.
- ⚠ **`opposes_map` is structurally empty** (`done_grading.py:126-135` hard-codes `"opposes": None`) → `misconception_code` is RARELY set in v1. Confirms auto-minted-misconception entities won't drive runtime persona in v1 (a reason the storage-table choice is low-runtime-risk — §OPEN-DECISIONS #2). **Path is `apollo/handlers/done_grading.py`** (verified via glob; the prior draft's §5 row read `apollo/agent/done_grading.py` — CORRECTED, §9 FEAS-4).
- ⚠ **The Tier-2 selection gate does NOT exist yet.** `apollo/overseer/problem_selector.py::list_problems_for_concept` (`:34-48`) loads EVERY `apollo_concept_problems` row for a concept with NO tier filter (`select(ConceptProblem.payload).where(ConceptProblem.concept_id == concept_id)`); `select_problem` and the just-shipped `select_problem_personalized` (WU-6A3) both build on it. **A `tier == 2 AND quarantined_at IS NULL` predicate MUST be added** or Tier-1/quarantined problems leak to students. Fold into 3B2a (column + reader land together), with 3B2h adding the `quarantined_at IS NULL` clause.
- **SymPy gate 6 does NOT reject foreign symbols.** `parse_zero_form` (`sympy_exec.py:47`) builds `local_dict` ONLY from a hardcoded bernoulli list `_CANONICAL_SYMBOLS` (`:35-38`) + `Rational`, then calls `parse_expr` (`:64`) — which AUTO-CREATES unknown symbols rather than raising. **So for an arbitrary-document problem, gate 6 catches only MALFORMED syntax; gate 4 (symbol consistency) is the ONLY foreign-symbol guard** (§9 FEAS-2). Gate 4 reads `apollo_concepts.canonical_symbols`/`normalization_map` (confirmed JSONB `models.py:148-149`) — which are EMPTY (`default=dict`) for an auto-provisioned concept unless 3B2d's stage-4 populates them. **3B2d MUST author canonical_symbols/normalization_map per minted concept, else gate 4 is vacuous.**
- **The 8-gate primitives all exist** (`apollo/ontology/edges.py:39-52` `EDGE_ALLOWED_PAIRS` + `Edge._check_pair` `:71-86` for gate-1 edge-type + gate-3 self-loop; `apollo/ontology/graph.py` `KGGraph.topological_order` raises on cycle (gate 3), `precedes_chain` (gate 5); `Problem.model_validate` (gate 1), `Problem.to_kg_graph` (gates 3/5), `Problem.problem_text`/`given_values: Dict[str,float]`/`target_unknown` (gate-8 dup-hash inputs)). **~60% of the lint is pre-built; 3B2b COMPOSES, gates 4/7/8 are net-new pure logic.**
- ⚠ **The LLM seam `apollo/agent/_llm.py` returns ONLY `str`.** `cheap_chat`/`main_chat` (`:51-95`) build kwargs, call the client, pass `resp` to `_log_call` (`:35-48`) which reads `usage.prompt_tokens`/`completion_tokens` ONLY to emit a log line, then return `resp.choices[0].message.content`. **The usage object is NOT returned — there is NO programmatic token signal.** The prior draft's claim that `ingest_runs` aggregates "feed from this log for free" is FALSE (§9 OPS-1). RESOLUTION: 3B2f adds a provisioning-local METERED wrapper that re-invokes the client (or wraps `_llm`) and captures `response.usage` into the `ingest_run` aggregate; the per-document cost ceiling compares a running aggregate after each call (mirroring `apollo_llm.py`'s pre-call `_estimate_tokens` guard). `_llm.py` is NOT mutated.
- **`:Canon` rebuild is `project_canon(db, neo, *, search_space_id=None, concept_id=None)`** (`canon_projection.py:195`) — read-only on Postgres, idempotent MERGE, course+concept scoped. Stage-6 CALLS it. NOT net-new. CONFIRMED.
- **The observability convention to mirror is migration 026's runs/findings** (`026:184-235`): `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`, `search_space_id INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE`, JSONB defaults, a §7 RLS stopgap (`ENABLE ROW LEVEL SECURITY`, NO policies), a manual rollback comment block. **030 mirrors all of this.**
- ⚠ **The migration test harness is PRE-BUILT but must be ADAPTED, not copied 1:1** (`tests/database/test_apollo_learner_model_migration.py`): it uses a CONTENT-SCOPED chain selector `_TOUCHES_TARGETS` (regex on `apollo_(subjects|concepts|problem_attempts)`, `:54-57`) + an `_EXCLUDE_FROM_CHAIN` set (`:65-68`, drops 026 and 028 which depend on later columns) + a `_STUB_DDL` for `auth.users`/`aita_search_spaces` (`:47-51`). 030 ALTERs `apollo_concept_problems` and FKs the observability tables to `aita_search_spaces` + `apollo_concepts` + `apollo_kg_entities` → the regex + exclude-set must be RE-DERIVED for 030 (e.g. 026 must now be IN the chain since `apollo_kg_entities`/`apollo_concepts` are FK targets) (§9 FEAS-5). The `Procfile` (`web`/`worker`/`apollo-janitor` — exactly 3 lines, verified) gets a 4th line.

---

## 2. Recommendation (the split — EIGHT stacked sub-units, build LLM-free-first)

| Sub-unit | Title | Size | Class | Planner | Depends on (compare branch) |
|---|---|---|---|---|---|
| **3B2a** | Migration 030 + ORM + Tier-2 selection gate | M | DB-write, real-PG | **feller-planner-db** | #49 `feat/apollo-kg-wu6a3-selection-wiring` |
| **3B2b** | The PURE 8-gate promotion lint | L | PURE (no LLM, no DB) | **feller-planner-backend** | 3B2a |
| **3B2c** | Course-local dedup ladder | M | PURE math + thin LLM tiebreaker + real-PG scope | **feller-planner-backend** | 3B2a |
| **3B2d** | LLM stage 1 SCRAPE + stage 4 TAG/MINT (+ author canonical_symbols) | L | LLM + pure mint + real-PG | **feller-planner-ai-pipeline** | 3B2b, 3B2c |
| **3B2e** | LLM stage 2 FIND-OR-GENERATE + stage 3 PAIRING GATE | L | LLM + retrieval | **feller-planner-ai-pipeline** | 3B2d |
| **3B2f** | Claim/lease SKIP-LOCKED queue-drain + metered LLM client (cost source) | M | DB concurrency + cost metering, real-PG | **feller-planner-ai-pipeline** | 3B2a, 3B2e |
| **3B2g** | Stage-0 TRIGGER + worker shell + 6-stage ORCHESTRATOR + observability + lease-reaper | L | DB-write + orchestration | **feller-planner-ai-pipeline** | 3B2b, 3B2e, 3B2f |
| **3B2h** | Per-problem ANOMALY QUARANTINE + shadow VERIFICATION | M | PURE stat + real-PG join | **feller-planner-backend** | 3B2g |

### Grouping rationale (adversarial pass, revised)

- **3B2a alone (not fused with 3B2b).** Migration 030 is the only real-PG/Testcontainers unit; its DB-coverage rules differ from line-coverage. It unblocks 3B2b (gate 4 reads `apollo_concepts.canonical_symbols`/`normalization_map`; gate 8 reads dup rows), 3B2c (the `scope_summary` column + the dedup-decisions table), and the whole writer. It OWNS the Tier-2 selection gate. **feller-planner-db.**
- **3B2b is the keystone PR.** The ENTIRE safety layer, 100% LLM-free, fully fixture-testable, reuses the largest existing surface. MUST land before any LLM stage. **Gate 4 is now explicitly the sole foreign-symbol guard** (gate 6 cannot reject foreign symbols — §9 FEAS-2). **feller-planner-backend.**
- **3B2c (dedup) split from 3B2b** — different risk profile: the deterministic MOCK-embedding cross-course non-merge test is the load-bearing assertion (§8B.7), and the LLM-judge tiebreaker introduces a cost boundary. It DEFINES the embedding source (the new `scope_summary` text, embedded on the fly — §9 NEW-GAP). **feller-planner-backend.**
- **3B2d FUSES scrape (stage 1) + tag/mint (stage 4)** — both LLM-over-chunks → typed records/EntitySpecs, sharing the LLM surface; stage-4 is mostly REUSE of the seed converters. **Now ALSO owns: authoring `canonical_symbols`/`normalization_map` per minted concept (so gate 4 isn't vacuous — §9 FEAS-2) + the `entry_type` vocabulary reconciliation (§9 SPEC-3).** Idempotency on `chunk_content_hash` (§9 OPS-2). **feller-planner-ai-pipeline.**
- **3B2e FUSES find-or-generate (stage 2) + pairing gate (stage 3)** — one "solution lifecycle," shared retrieval surface, tight cost coupling, fail-CLOSED co-located. **feller-planner-ai-pipeline.**
- **3B2f is NEW (promoted out of the old 3B2f) — the claim/lease queue-drain + the metered LLM client.** The feasibility critic correctly showed the old 3B2f understated its size by claiming the worker "mirrors the janitor 1:1" when the SKIP-LOCKED claim/lease drain is a SEPARATE template with real concurrency risk (§9 FEAS-1). Splitting it gives the drain its own real-PG concurrency tests (two workers, SKIP-LOCKED non-overlap, lease-expiry re-claim, cost-abort dead-letter) and keeps the 3B2g orchestrator an L pass. It ALSO owns the metered LLM client — the cost-budget DATA SOURCE the old plan lacked (§9 OPS-1). **feller-planner-ai-pipeline.**
- **3B2g is the orchestration spine** — the trigger at `teacher_weekly.py:1174` (content-key idempotent transactional enqueue), the worker SHELL (mirrors the janitor: loop/flag/SIGTERM), the 6-stage ORCHESTRATOR (drives stages via 3B2f's drained jobs, calls 3B2b's validator + `project_canon`), all observability writes, AND the lease-reaper/terminal-status reconciliation (§9 OPS-5). **feller-planner-ai-pipeline.**
- **3B2h last** — the per-problem quarantine statistic (PURE, fixture-tested) + the real-PG findings→runs→attempts aggregation join (§9 OPS-3) + the shadow-wiring VERIFICATION (SHADOW ON, LAYER3 OFF — §9 SPEC-4). **feller-planner-backend.**

### Alternatives considered + rejected
- **Keep the claim/lease drain inside the orchestrator (the old 7-unit plan):** REJECTED — it fuses two templates (janitor shell + net-new SKIP-LOCKED drain) and pushes 3B2g toward XXL; the drain's concurrency-correctness deserves isolated real-PG tests (§9 FEAS-1).
- **Fuse 3B2d+3B2e into one 4-stage "LLM pipeline" PR:** REJECTED — 4 LLM stages in one PR blows the 95%-patch-coverage diff size and muddies the per-stage idempotency/cost contracts.
- **Fuse 3B2a+3B2b:** REJECTED — mixes real-PG (DB-coverage rules) with pure-Python (line-coverage); the pure validator must be reviewable in isolation as the safety core.
- **Quarantine inside 3B2g:** REJECTED — quarantine needs GRADED findings (a different data dependency) and a 3-table aggregation join that deserves isolated tests.
- **A separate unit just for the job-queue table:** REJECTED — the queue table is a migration-030 artifact (3B2a owns the DDL); the claim/lease DRAIN over it is 3B2f.

---

## 3. Sub-units

### WU-3B2a — Migration 030 + ORM + Tier-2 selection gate (real-PG)

**One-line:** Add `tier`/`solution_source`/`provenance`/`quarantined_at`/`search_space_id` to `apollo_concept_problems`, a `scope_summary TEXT` to `apollo_kg_entities` (the dedup embedding source), a `content_hash` to `apollo_ingest_runs`, the 4 observability tables + the provisioning job-queue table (all course-scoped, RLS-stopgap'd), the ORM, the backfill that flips existing seeded rows to `tier=2`, and the one-predicate Tier-2 filter on `list_problems_for_concept`.

**SEAM:** 3B2a owns every DDL byte + the ORM + the selection-gate reader. It writes NO pipeline logic. Handoff: a migrated local DB + ORM classes + a `tier`-filtered selector.

**Scope — files to create/edit:**
- `database/migrations/030_apollo_autoprovisioning.sql` (NEW) — the EXACT DDL below.
- `apollo/persistence/models.py` (EDIT) — add `tier`/`solution_source`/`provenance`/`quarantined_at`/`search_space_id` to `ConceptProblem` (`:158`); add `scope_summary` to `KGEntity` (`:362`); add 5 NEW model classes (`IngestRun`, `RejectedProblem`, `DedupDecision`, `IngestError`, `ProvisioningJob`) following the exact convention (NO DB CHECK in ORM, `_JSONType`, `BigInteger().with_variant(Integer(),"sqlite")` PK).
- `apollo/overseer/problem_selector.py` (EDIT, ~1 line) — add `ConceptProblem.tier == 2` to the `list_problems_for_concept` WHERE (`:41`). Gates BOTH `select_problem` AND `select_problem_personalized` (no signature change). (3B2h adds `quarantined_at IS NULL`.)
- `tests/database/test_apollo_autoprovisioning_migration.py` (NEW) — ADAPTS `test_apollo_learner_model_migration.py` (re-derive `_TOUCHES_TARGETS`/`_EXCLUDE_FROM_CHAIN` so `018`+`026` precede 030; 030 applied last). Assert columns/tables/indexes/backfill/RLS.
- `apollo/overseer/tests/test_problem_selector.py` (EDIT) — assert Tier-1 EXCLUDED, Tier-2 included.

**Migration 030 — EXACT DDL** (mirrors 026's two-step-safe + RLS-stopgap + rollback-comment; all course-scoped):
```sql
BEGIN;
-- 1. apollo_concept_problems: tier/solution_source/provenance/quarantine + denormalized scope.
ALTER TABLE apollo_concept_problems
  ADD COLUMN IF NOT EXISTS tier            SMALLINT NOT NULL DEFAULT 1
      CHECK (tier IN (1, 2)),                              -- 1=inventory (not teachable), 2=teachable
  ADD COLUMN IF NOT EXISTS solution_source TEXT
      CHECK (solution_source IS NULL OR solution_source IN ('extracted','generated','authored')),
  ADD COLUMN IF NOT EXISTS provenance      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {document_id,page,chunk_content_hash}
  ADD COLUMN IF NOT EXISTS quarantined_at  TIMESTAMPTZ,                          -- §8B.3c anomaly quarantine (NULL = live)
  ADD COLUMN IF NOT EXISTS search_space_id INTEGER;                             -- denormalized course scope (§OPEN-DECISIONS #3)
UPDATE apollo_concept_problems p SET search_space_id = sub.search_space_id
  FROM apollo_concepts c JOIN apollo_subjects sub ON sub.id = c.subject_id
  WHERE p.concept_id = c.id AND p.search_space_id IS NULL;
-- CRITICAL backfill: existing §8-seeded rows ARE teachable -> tier=2, solution_source='authored'.
UPDATE apollo_concept_problems SET tier = 2, solution_source = 'authored' WHERE tier = 1;
CREATE INDEX IF NOT EXISTS apollo_concept_problems_concept_tier_idx
  ON apollo_concept_problems(concept_id, tier);

-- 1b. apollo_kg_entities: scope_summary text (the dedup embedding SOURCE, embedded on the fly; §NEW-GAP).
ALTER TABLE apollo_kg_entities
  ADD COLUMN IF NOT EXISTS scope_summary TEXT;  -- nullable; authored at mint; NO persisted vector (no pgvector migration)

-- 2. apollo_ingest_runs — per-doc counts + LLM call/token/cost aggregates + content_hash + queue state.
CREATE TABLE IF NOT EXISTS apollo_ingest_runs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  document_id       BIGINT  NOT NULL,
  content_hash      TEXT,                 -- document content hash; an unchanged re-upload short-circuits (§OPS-2)
  status            TEXT    NOT NULL DEFAULT 'queued'
      CHECK (status IN ('queued','running','succeeded','failed')),
  n_questions_scraped INTEGER NOT NULL DEFAULT 0,
  n_promoted        INTEGER NOT NULL DEFAULT 0,
  n_rejected        INTEGER NOT NULL DEFAULT 0,
  n_dedup_merged    INTEGER NOT NULL DEFAULT 0,
  llm_calls         INTEGER NOT NULL DEFAULT 0,
  llm_tokens_in     BIGINT  NOT NULL DEFAULT 0,
  llm_tokens_out    BIGINT  NOT NULL DEFAULT 0,
  llm_cost_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_ingest_runs_space_doc_idx ON apollo_ingest_runs(search_space_id, document_id);
-- a succeeded run per (document_id, content_hash) lets enqueue short-circuit an unchanged re-upload:
CREATE INDEX IF NOT EXISTS apollo_ingest_runs_doc_hash_idx ON apollo_ingest_runs(document_id, content_hash);

-- 3. apollo_provisioning_jobs — the SKIP LOCKED work queue (mirrors TeacherUploadJob lease shape).
CREATE TABLE IF NOT EXISTS apollo_provisioning_jobs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  document_id       BIGINT  NOT NULL,
  state             TEXT    NOT NULL DEFAULT 'pending'
      CHECK (state IN ('pending','running','completed','failed')),
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE SET NULL,
  lease_owner       TEXT,
  lease_expires_at  TIMESTAMPTZ,
  attempt_count     INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- idempotent enqueue: at most one OPEN job per document (the brandur/IMTI partial-unique-index idiom).
-- NOTE: this is JOB-level dedup; the (document_id, chunk_content_hash) no-op is a SEPARATE intra-job guarantee (§OPS-2).
CREATE UNIQUE INDEX IF NOT EXISTS apollo_provisioning_jobs_open_uniq
  ON apollo_provisioning_jobs(document_id) WHERE state IN ('pending','running');

-- 4. apollo_rejected_problems — a gate failure + diagnostic + the rejected payload.
CREATE TABLE IF NOT EXISTS apollo_rejected_problems (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  concept_id        BIGINT  REFERENCES apollo_concepts(id) ON DELETE SET NULL,  -- NULL if rejected pre-tag
  failed_gate       SMALLINT,            -- 1..8 (NULL if rejected at the pairing gate, not a lint gate)
  rejected_stage    TEXT    NOT NULL,    -- 'pairing_gate' | 'promotion_lint'
  diagnostic        TEXT    NOT NULL,
  payload           JSONB   NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_rejected_problems_run_idx ON apollo_rejected_problems(ingest_run_id);

-- 5. apollo_dedup_decisions — method + similarity + verdict for every dedup resolution.
CREATE TABLE IF NOT EXISTS apollo_dedup_decisions (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  concept_id        BIGINT  REFERENCES apollo_concepts(id) ON DELETE SET NULL,
  candidate_key     TEXT    NOT NULL,
  method            TEXT    NOT NULL CHECK (method IN ('slug','embedding','llm_judge')),
  similarity        REAL,                -- NULL for the slug exact-match tier
  verdict           TEXT    NOT NULL CHECK (verdict IN ('merged','distinct')),
  matched_entity_id BIGINT  REFERENCES apollo_kg_entities(id) ON DELETE SET NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_dedup_decisions_run_idx ON apollo_dedup_decisions(ingest_run_id);

-- 6. apollo_ingest_errors — stage + class + context for any non-terminal pipeline error.
CREATE TABLE IF NOT EXISTS apollo_ingest_errors (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  stage             TEXT    NOT NULL,    -- scrape|find_or_generate|pairing|tag_mint|dedup|promotion
  error_class       TEXT    NOT NULL,
  context           JSONB   NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_ingest_errors_run_idx ON apollo_ingest_errors(ingest_run_id);

-- 7. RLS stopgap (mirror 026 §7 / migration 022): default-deny to PostgREST on the 5 new public tables.
ALTER TABLE apollo_ingest_runs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_provisioning_jobs  ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_rejected_problems  ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_dedup_decisions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_ingest_errors      ENABLE ROW LEVEL SECURITY;
COMMIT;
-- Rollback (run manually; never auto-applied):
-- BEGIN; DROP TABLE IF EXISTS apollo_ingest_errors, apollo_dedup_decisions, apollo_rejected_problems,
--   apollo_provisioning_jobs, apollo_ingest_runs;
-- ALTER TABLE apollo_kg_entities DROP COLUMN IF EXISTS scope_summary;
-- DROP INDEX IF EXISTS apollo_concept_problems_concept_tier_idx;
-- ALTER TABLE apollo_concept_problems DROP COLUMN IF EXISTS search_space_id, DROP COLUMN IF EXISTS quarantined_at,
--   DROP COLUMN IF EXISTS provenance, DROP COLUMN IF EXISTS solution_source, DROP COLUMN IF EXISTS tier; COMMIT;
```

**Migration?** YES — 030 (this unit OWNS it). **Real-infra?** YES — real-PG Testcontainers (DB-coverage rules: enumerate every column added, every CHECK, every RLS-enable, the two backfills, the partial-unique-index collapse, the `content_hash` short-circuit index, the new `scope_summary` column).
**plannerAgent:** feller-planner-db. **v1-or-defer:** v1.
**Key risks owned:** the `tier=2` backfill of existing seeded rows (else the bernoulli pool vanishes — highest blast radius); the Tier-2 selection-gate predicate (else Tier-1 leaks); the partial-unique-index idempotent-enqueue collapse; the denormalized `search_space_id` backfill; the `scope_summary` column being defined here so 3B2c has an embedding source; the harness being ADAPTED not copied (re-derive the chain selector — §9 FEAS-5); NEVER apply to a remote DB.

---

### WU-3B2b — The PURE 8-gate promotion lint (the safety core)

**One-line:** A pure, LLM-free, DB-free module that runs the §8B.4 eight gates in order, short-circuiting on first failure, over a hand-buildable `MintedReferenceGraph` input → `PromotionResult{ok, failed_gate, diagnostic}`. Exercisable entirely on fixtures with NO LLM stage running.

**SEAM:** 3B2b owns the gate logic + the diagnostic. It does NOT read the DB (gate-4 canonical symbols + gate-8 dup rows are PASSED IN by the caller, so the core stays pure), does NOT promote, does NOT call `project_canon`, does NOT write `rejected_problems`. Handoff: `PromotionResult` (3B2g maps fail → a `rejected_problems` row, pass → promotion).

**Scope — files to create (`apollo/provisioning/`):**
- `apollo/provisioning/__init__.py` (NEW) — package seam (mirrors `apollo/resolution/__init__.py`).
- `apollo/provisioning/promotion_lint.py` (NEW) — `PromotionResult` frozen dc + `run_promotion_lint(graph, *, canonical_symbols, normalization_map, existing_problem_hashes) -> PromotionResult` running the 8 gates IN ORDER. Each gate is a small pure function; the orchestrator short-circuits.
- `apollo/provisioning/problem_hash.py` (NEW) — `problem_dup_hash(problem) -> str` = `sha256(normalize(problem_text) + canonical(given_values) + target_unknown)` (gate 8; the deterministic per-(course,concept) idempotency key reused by 3B2g).

**The 8-gate contract (each gate's check + reuse + its adversarial fixture):**
| # | Gate | Check | Reuse | Adversarial fixture (asserts THIS gate fired) |
|---|---|---|---|---|
| 1 | Schema | `Problem.model_validate` passes; every `Edge` in `EDGE_ALLOWED_PAIRS`; **AND (if OPEN-DECISION #5 = option b's fallback) every `entry_type` is in the mint map** | `Problem` validators + `edges.py:39-86` | a bad edge pair (`equation SCOPES condition`); and a `variable_mapping` step if the gate carries the mint-map check |
| 2 | Reference closure | every `depends_on` + edge endpoint resolves; entity_links present; declared_paths cover all nodes | **`validate_reference_graph` verbatim** (`learner_model_seed.py:307`) — this is gate 2, NOT the whole lint | a dangling `depends_on` / missing entity_key |
| 3 | DAG | acyclic + every node reachable from a root | `KGGraph.topological_order` (raises on cycle) via `Problem.to_kg_graph` | a PRECEDES cycle |
| 4 | Symbol consistency | every symbol in the equations is in `canonical_symbols` or normalizes via `normalization_map` — **the ONLY foreign-symbol guard (gate 6 cannot reject foreign symbols, §9 FEAS-2)** | reads PASSED-IN `apollo_concepts.canonical_symbols`/`normalization_map` (loaded by caller; POPULATED by 3B2d's stage-4) | a foreign symbol (`x` not in the canonical set) |
| 5 | Procedure coherence | the `:ProcedureStep` nodes form ONE `PRECEDES` chain; the terminal step computes `target_unknown` | `Problem.to_kg_graph` PRECEDES build + `precedes_chain` | a broken/forked PRECEDES chain |
| 6 | SymPy parse | `parse_zero_form` succeeds per equation — catches MALFORMED syntax only (auto-creates unknown symbols, does NOT reject foreign ones) | **`sympy_exec.parse_zero_form`** (`:47`) | a malformed equation (`rho*A1*v1 - = P2`) |
| 7 | Equation-system closure (paper-only) | every symbol is given / target / or cancelled — a CLOSURE check, NOT an end-to-end solve (honest v1 limit, §8B.4:1347) | NEW pure logic over `given_values` + the equations' free symbols | an unclosed system (a free symbol neither given nor target) |
| 8 | Duplicate detection | `problem_dup_hash(problem)` not already in `existing_problem_hashes` FOR THIS COURSE'S CONCEPT (caller passes the concept-scoped set) | NEW (`problem_hash.py`); keyed on the BIGINT `concept_id` | a duplicate (same normalized text+givens+target as a seeded problem) |

**Public API for 3B2g:**
```python
@dataclass(frozen=True)
class PromotionResult:
    ok: bool
    failed_gate: int | None        # 1..8, None on pass
    diagnostic: str                # "" on pass
def run_promotion_lint(graph, *, canonical_symbols, normalization_map,
                       existing_problem_hashes) -> PromotionResult: ...
def problem_dup_hash(problem) -> str: ...
```

**Dependencies:** `apollo.schemas.problem.Problem`, `apollo.ontology.edges.EDGE_ALLOWED_PAIRS`, `apollo.ontology.graph.KGGraph`, `apollo.solver.sympy_exec.parse_zero_form`, `apollo.persistence.learner_model_seed.validate_reference_graph`. Stdlib `hashlib`. NO new package.
**Migration?** No. **Real-infra?** None — pure fixture tests (positive bernoulli passes all 8 + one adversarial per gate, §8B.7 Tier-1). **plannerAgent:** feller-planner-backend. **v1-or-defer:** v1.
**Key risks owned:** the gate-ORDER + short-circuit (each adversarial fixture asserts EXACTLY which gate fired); **gate 4 being the SOLE foreign-symbol guard** (a note in the module: gate 6 does NOT reject foreign symbols — §9 FEAS-2); gate 7 being a CLOSURE paper-check NOT a solve; gate 8 keyed on the BIGINT concept; gate 4 reading PASSED-IN symbols (keeps the core pure).

---

### WU-3B2c — Course-local dedup ladder

**One-line:** Resolve a candidate entity/concept against THIS COURSE's existing inventory via the ladder slug-exact → `scope_summary` embedding cosine → `cheap_chat` LLM-judge tiebreaker, emitting a `DedupVerdict{merged|distinct, method, similarity, matched_entity_id}` and writing `apollo_dedup_decisions`. NEVER global.

**SEAM:** 3B2c owns the ladder + the course-scoping invariant + the dedup-decision write + the on-the-fly embedding of `scope_summary`. It takes an injected embedding fn + an injected `cheap_chat` (so Tier-1 tests are deterministic). It does NOT scrape, does NOT mint (3B2d calls it before upsert).

**Scope — files to create (`apollo/provisioning/`):**
- `apollo/provisioning/dedup.py` (NEW) — `DedupVerdict` frozen dc + `async resolve_candidate(db, *, search_space_id, concept_id, candidate, embed_fn, judge_fn) -> DedupVerdict` running the ladder; the COURSE-SCOPE is a WHERE clause on `apollo_kg_entities` (restricted to this course's concepts), applied BEFORE similarity is ever consulted. The candidate's `scope_summary` text + each in-course entity's `scope_summary` (the new 030 column) are embedded on the fly via `embed_fn` (§NEW-GAP — there is no persisted entity vector).
- `apollo/provisioning/dedup_constants.py` (NEW) — `EMBED_MERGE_THRESHOLD`, `EMBED_JUDGE_BAND`. Hand-set v1 constants (§OPEN-DECISIONS #4).

**The ladder (course-scoped, first-writer-wins):**
1. **slug match** — candidate's `canonical_key` already present for this course's concept → `merged` (method=`slug`, similarity=NULL).
2. **embedding similarity** — cosine of the candidate's `scope_summary` embedding vs each in-course inventory entity's; `≥ EMBED_MERGE_THRESHOLD` → `merged` (method=`embedding`); in `EMBED_JUDGE_BAND` → escalate; below → `distinct`.
3. **LLM-judge tiebreaker** — `cheap_chat` "are these the same concept FOR THIS COURSE?" → `merged|distinct` (method=`llm_judge`).
Every resolution writes one `apollo_dedup_decisions` row.

**Public API for 3B2d:**
```python
@dataclass(frozen=True)
class DedupVerdict:
    verdict: str                   # 'merged' | 'distinct'
    method: str                    # 'slug' | 'embedding' | 'llm_judge'
    similarity: float | None
    matched_entity_id: int | None
async def resolve_candidate(db, *, search_space_id, concept_id, candidate, embed_fn, judge_fn) -> DedupVerdict: ...
```

**Dependencies:** `apollo.persistence.models` (`KGEntity`, `DedupDecision`), `indexing.document_embedder.embed_text` (injected), `apollo.agent._llm.cheap_chat` (injected). NO new package.
**Migration?** No (uses 030's `apollo_dedup_decisions` + the new `apollo_kg_entities.scope_summary`). **Real-infra?** real-PG for the course-scoping query + the decision write; the LADDER MATH is unit-testable with deterministic MOCK embeddings. **plannerAgent:** feller-planner-backend. **v1-or-defer:** v1.
**Key risks owned:** the COURSE-SCOPE invariant — the cross-course non-merge test (two courses each with an "incompressible" entity, IDENTICAL mock embeddings, assert the course-scope WHERE keeps them separate BEFORE similarity is consulted, §8B.7); first-writer-wins determinism; the embedding SOURCE being the new `scope_summary` text embedded on the fly (§NEW-GAP); the thin LLM-judge mocked in Tier-1.

---

### WU-3B2d — LLM stage 1 SCRAPE + stage 4 TAG/MINT (+ author canonical_symbols)

**One-line:** Read the document's already-embedded `AITAChunk` rows, scrape candidate questions with provenance → Tier-1 `apollo_concept_problems` rows keyed on `chunk_content_hash`; then tag concepts/entities, **author the concept's `canonical_symbols`/`normalization_map`**, LLM-draft prereq edges, and REUSE the §8 seed's pure `*_to_entities` converters to mint reference + misconception EntitySpecs (resolving the candidate against 3B2c's dedup ladder before upsert).

**SEAM:** owns stage-1 + stage-4. Reads chunks, never re-indexes. Calls 3B2c (`resolve_candidate`) and the seed converters. Idempotent per `chunk_content_hash` (NOT the volatile `chunk_id` — §9 OPS-2). Each LLM boundary has a Pydantic in→out type. Does NOT find/generate the solution (3B2e) and does NOT promote (3B2g).

**Scope — files to create (`apollo/provisioning/`):**
- `apollo/provisioning/scrape.py` (NEW) — `CandidateQuestion` Pydantic type (provenance: `document_id, page, chunk_content_hash` + LLM `difficulty` + `problem_text`/`given_values`/`target_unknown`) + `async scrape_questions(chunks, *, chat_fn) -> list[CandidateQuestion]`. Output `entry_type`s reconciled per §OPEN-DECISIONS #5.
- `apollo/provisioning/tag_mint.py` (NEW) — `ApprovedPair`/`MintPlan` Pydantic types + `async tag_and_mint(db, pair, *, chat_fn, embed_fn) -> MintPlan` that LLM-drafts concept tags + prereq edges, **authors the new concept's `canonical_symbols`/`normalization_map` from the scraped problem's symbol set (so gate 4 isn't vacuous — §9 FEAS-2)**, then calls `reference_solution_to_entities` + `misconceptions_to_entities` + `concept_dag_to_prereqs` + `annotate_reference_solution` (the frozen seed core) and upserts via 3B2c-resolved keys.
- Persistence write helpers PARALLELING `scripts/seed_apollo_learner_model.py`'s `_upsert_entity`/`_link_opposes`/`_insert_prereqs`/`_annotate_problems` (generalized: concept from tags, not `_BERNOULLI_SLUG`).

**Stage I/O types:** `DocumentChunks → list[CandidateQuestion]` (stage 1, written Tier-1); `ApprovedPair → MintPlan` (stage 4). Idempotency `chunk_content_hash`.
**Dependencies:** `database.models.AITAChunk` (read), `apollo.agent._llm` (injected — NOTE: returns only `str`; cost metering is 3B2f's metered wrapper), `indexing.document_embedder` (injected), the frozen seed converters, 3B2c `resolve_candidate`, 3B2a ORM. NO new package.
**Migration?** No. **Real-infra?** Tier-1 = MOCKED LLM (`patch(...cheap_chat, return_value=json.dumps(...))`, the `test_leakage_judge.py:31-36` template) + real-PG for the Tier-1 upsert/idempotency assertions. **plannerAgent:** feller-planner-ai-pipeline (idempotency + per-stage cost budget + failure-path declarations). **v1-or-defer:** v1.
**Key risks owned:** the `entry_type` vocabulary reconciliation (§OPEN-DECISIONS #5; option b extends the frozen map or gate-1 fails closed — §9 SPEC-3); **authoring `canonical_symbols`/`normalization_map` per minted concept (else gate 4 is vacuous — §9 FEAS-2)**; idempotency on `chunk_content_hash` (re-index = no-op — §9 OPS-2); the slug→BIGINT `concept_id` boundary (§OPEN-DECISIONS #6); the auto-minted-misconception STORAGE choice (§OPEN-DECISIONS #2); per-document scrape cost (metered in 3B2f).

---

### WU-3B2e — LLM stage 2 FIND-OR-GENERATE + stage 3 PAIRING/CORRECTNESS GATE

**One-line:** For each scraped question, retrieve over the course corpus for a printed solution (`solution_source=extracted`) else LLM-generate RAG-grounded (`solution_source=generated`); then run a two-phase RAG-grounded validator (Phase A pairing/answer-relevance, Phase B claim-decomposed faithfulness) that **fails CLOSED** — a bad pairing or unfaithful claim → `rejected_problems`, never a student.

**SEAM:** owns stage-2 + stage-3 (one "solution lifecycle"). Consumes 3B2d's Tier-1 `CandidateQuestion`; produces an `ApprovedPair` or a `Rejection`. Does NOT promote/lint (3B2g) and does NOT mint entities (3B2d).

> **Ordering note (resolve in planning):** §8B.2's stage order is scrape(1) → solve(2) → pair(3) → mint(4). 3B2d FUSES 1+4 and 3B2e FUSES 2+3, so the RUNTIME order is 1 → (2,3) → 4. The clean contract: 3B2d-stage-1 writes Tier-1 + emits `CandidateQuestion`; 3B2e takes `CandidateQuestion` → `ApprovedPair | Rejection`; 3B2g's orchestrator feeds an `ApprovedPair` BACK into 3B2d-stage-4's `tag_and_mint`. So `tag_and_mint` is CALLED by the 3B2g orchestrator AFTER 3B2e approves — the module fusion is by planner/cost-boundary, not by call order. This dependency edge is pinned in §6 build order.

**Scope — files to create (`apollo/provisioning/`):**
- `apollo/provisioning/solution.py` (NEW) — `ReferenceSolutionDraft` Pydantic type (`solution_source: extracted|generated`, provenance, the reference steps) + `async find_or_generate(db, question, *, retrieve_fn, chat_fn) -> ReferenceSolutionDraft` (retrieve-first, generate-RAG-grounded fallback).
- `apollo/provisioning/pairing_gate.py` (NEW) — `PairingVerdict` Pydantic type (`paired: bool, faithful: bool, failed_claims: tuple, confidence: float`) + `async validate_pair(question, draft, *, retrieve_fn, judge_fn) -> PairingVerdict`. Span-grounded prompts. **fail-CLOSED on parse failure** (invert `leakage_judge`'s fail-open default).

**Stage I/O types:** `CandidateQuestion → ReferenceSolutionDraft` (stage 2); `(CandidateQuestion, ReferenceSolutionDraft) → PairingVerdict` (stage 3). Idempotency `(chunk_content_hash, solution_hash)`.
**Dependencies:** `retrieval/` (`hybrid_search`/`pipeline`, injected as `retrieve_fn`), `apollo.agent._llm.main_chat`/`cheap_chat` (injected; metered in 3B2f), 3B2d's `CandidateQuestion`. NO new package.
**Migration?** No. **Real-infra?** Tier-1 = MOCKED LLM + MOCKED retrieval; the fail-closed + extracted-vs-generated branches are deterministic. **plannerAgent:** feller-planner-ai-pipeline. **v1-or-defer:** v1.
**Key risks owned:** the **fail-CLOSED** convention (a bad pair must reject); the "judge uses different grounding than generator" failure mode (span-grounded prompts, both phases RAG-grounded); the generated branch carrying real retrieved context so Phase B has something to entail against. **§1.8 caveat (§9 OPS-6): a fabricated coherent-wrong solution that PASSES pairing + all 8 gates IS shown to students as teachable (shadow diagnostic, no belief movement); it is removed only RETROACTIVELY by quarantine. The pairing gate reduces but does not eliminate this — the flag-OFF default + the calibration gate are the real pre-exposure safety.**

---

### WU-3B2f — Claim/lease SKIP-LOCKED queue-drain + metered LLM client (the concurrency + cost spine)

**One-line:** The net-new `apollo_provisioning_jobs` claim/lease drain (templated on `run_upload_worker_loop`/`_claim_upload_job_async`, NOT the janitor), plus the provisioning-local metered LLM client that captures `response.usage` into the `ingest_run` token/cost aggregate (the cost-budget DATA SOURCE the old plan lacked). Real-PG concurrency tests.

**SEAM:** owns the SKIP-LOCKED claim, the lease set/renew/release, the `attempt_count`/dead-letter bookkeeping, and the metered LLM wrapper + the running per-document cost aggregate. It does NOT define the trigger (3B2g), the worker loop shell (3B2g), or any stage logic. Handoff: `claim_provisioning_job()`/`release`/`fail` + a `MeteredChat` that returns `(content, usage)` and accumulates into a passed `ingest_run` row.

**Scope — files to create (`apollo/provisioning/`):**
- `apollo/provisioning/queue.py` (NEW) — `async claim_provisioning_job(session, *, lease_owner, lease_seconds) -> ClaimedJob | None` doing `.with_for_update(skip_locked=True)` over `apollo_provisioning_jobs` WHERE `state='pending' OR (state='running' AND lease_expires_at < now)`, setting `state='running'`, `lease_owner`, `lease_expires_at`, `attempt_count += 1` (mirrors `teacher_weekly.py:881-915`); `complete_job`/`fail_job` (dead-letter when `attempt_count >= MAX`) ; `release_job` (clear lease).
- `apollo/provisioning/metered_chat.py` (NEW) — `MeteredChat` wrapping `cheap_chat`/`main_chat` that captures `response.usage` (prompt/completion tokens) → accumulates `llm_calls`/`llm_tokens_in`/`llm_tokens_out`/`llm_cost_usd` on the `ingest_run` row, with a per-call check against a `PER_DOCUMENT_TOKEN_CEILING` that raises `CostBudgetExceeded` (the abort signal). NOTE: `_llm.py` returns only `str` and is NOT mutated; the metered wrapper invokes the client itself or wraps the call site (§9 OPS-1). Pricing constants `_COST_PER_1K_IN`/`_COST_PER_1K_OUT` per model (config-driven).
- `apollo/provisioning/cost_constants.py` (NEW) — `PER_DOCUMENT_TOKEN_CEILING`, the model→price table (§OPEN-DECISIONS #7).

**Dependencies:** `apollo.persistence.models` (`ProvisioningJob`, `IngestRun`), `apollo.agent._llm` (wrapped, not mutated). NO new package.
**Migration?** No (uses 030's `apollo_provisioning_jobs`/`apollo_ingest_runs`). **Real-infra?** real-PG concurrency: TWO workers claim non-overlapping jobs (SKIP-LOCKED), a lease-expiry re-claim, a `MAX`-attempt dead-letter, a cost-abort path. The metered client is unit-tested with a fake `response.usage`. **plannerAgent:** feller-planner-ai-pipeline (concurrency-correctness + cost-budget are the whole job). **v1-or-defer:** v1.
**Key risks owned:** SKIP-LOCKED non-overlap under contention; lease-expiry re-claim correctness; `attempt_count` + dead-letter on the cost-abort; the metered wrapper being the ONLY token signal (the cost ceiling cannot be implemented without it — §9 OPS-1); scrape→cheap / generate→main / judges→cheap routing (§OPEN-DECISIONS #7); a cost abort writes `apollo_ingest_errors` + marks the run failed.

---

### WU-3B2g — Stage-0 TRIGGER + worker shell + 6-stage ORCHESTRATOR + observability + lease-reaper

**One-line:** Hook a content-key-idempotent transactional enqueue at the indexing-completion seam (`teacher_weekly.py:1174`), a dormant flag-gated worker SHELL that mirrors `learner_janitor_worker.py` (loop/flag/SIGTERM) and drains via 3B2f's `claim_provisioning_job`, the 6-stage ORCHESTRATOR per document, stage-6 promotion (calls 3B2b's lint then `project_canon`), every observability write, and the lease-reaper/terminal-status reconciliation.

**SEAM:** owns the trigger, the worker SHELL, the stage sequencing, stage-6 promotion, every `ingest_runs`/`rejected_problems`/`ingest_errors` write, and the lease-reaper. It IMPORTS 3B2b/c/d/e (stage logic) and 3B2f (claim/lease + metered chat). It does NOT redefine any stage and does NOT change `done.py`.

**Scope — files to create/edit:**
- `knowledge/teacher_weekly.py` (EDIT, THIN ~8 lines) — inside the `:1131-1174` session, after the upload flips READY, **short-circuit if `apollo_ingest_runs` already has a `status='succeeded'` row for `(document_id, content_hash)`** (the unchanged-re-upload no-op, comparing `AITADocument.content_hash` at `:1041` — §9 OPS-2), else INSERT an `apollo_provisioning_jobs` row (the partial-unique-index collapses a duplicate open job). A thin call into `apollo.provisioning.enqueue`.
- `apollo/provisioning/enqueue.py` (NEW) — `async enqueue_provisioning_job(session, *, search_space_id, document_id, content_hash)` (idempotent via the partial unique index + the content-hash short-circuit).
- `apollo/provisioning/orchestrator.py` (NEW) — `async run_provisioning(db, neo, *, job, metered_chat)` running the 6-stage pipeline per document with per-stage checkpointing; the **`(document_id, chunk_content_hash)` no-op is enforced INSIDE the job** by upserting Tier-1 scrape rows ON CONFLICT on `(search_space_id, document_id, chunk_content_hash)` so a re-run skips already-scraped chunks (§9 SPEC-1/OPS-2); aggregates LLM cost via 3B2f's `MeteredChat`; writes `ingest_errors` on a survived stage error AND flips `ingest_runs.status='failed'` + the job to `'failed'` on a terminal error (never leaves it `'running'`, which the partial-unique-index would wedge — §9 OPS-5); a per-document cost-ceiling abort.
- `apollo/provisioning/promote.py` (NEW) — `async promote(db, neo, *, problem, ...)` calling `run_promotion_lint`; pass → store `reference_solution` in payload + flip `tier=2` + `project_canon`; fail → `apollo_rejected_problems` row.
- `apollo/provision_worker.py` (NEW) — the dormant worker SHELL, MIRRORS `learner_janitor_worker.py` (flag `APOLLO_AUTOPROVISION_ENABLED` default OFF, single `asyncio.run(main())` loop, cooperative SIGTERM/SIGINT, `_int_env`, per-iteration flag read, one structured `_LOG.info` per sweep carrying `run_id`/`status`/`n_promoted`/`n_rejected`/`llm_cost`). The DRAIN inside the loop is 3B2f's `claim_provisioning_job` + `run_provisioning` (NOT redefined here — §9 FEAS-1). A lease-reaper pass re-opens an expired `'running'` job and marks its `ingest_run` failed (§9 OPS-5).
- `Procfile` (EDIT) — add a 4th line `apollo-provision: python -m apollo.provision_worker` (the Procfile already has exactly `web`/`worker`/`apollo-janitor`).

**Dependencies:** 3B2b (`run_promotion_lint`), 3B2c (`resolve_candidate`), 3B2d (`scrape_questions`/`tag_and_mint`), 3B2e (`find_or_generate`/`validate_pair`), 3B2f (`claim_provisioning_job`/`MeteredChat`), `apollo.knowledge_graph.canon_projection.project_canon`, 3B2a ORM. NO new package.
**Migration?** No (uses 030's tables). **Real-infra?** real-PG (the enqueue + content-hash short-circuit + observability writes + the lease-reaper + the flag-OFF non-regression — with the flag OFF the trigger enqueues but no worker drains, so uploads behave EXACTLY as today) + a Neo4j touch for `project_canon` (mock or Testcontainers neo4j). **plannerAgent:** feller-planner-ai-pipeline (idempotency + failure-path declarations are the whole job). **v1-or-defer:** v1.
**Key risks owned:** the content-key transactional enqueue (a job is never visible for a doc that didn't finish indexing; an unchanged re-upload is a no-op — §9 OPS-2); the intra-job `(document_id, chunk_content_hash)` no-op (§9 SPEC-1); the owner-doc boundary crossing (reconcile `domain-data.md` + `apollo.md` + `indexing.md` same-commit); the terminal-status reconciliation + lease-reaper (a crashed mid-document run must not wedge the partial-unique-index against re-enqueue — §9 OPS-5); flag-OFF = today's upload behavior byte-identical.

---

### WU-3B2h — Per-problem ANOMALY QUARANTINE + shadow VERIFICATION

**One-line:** A PURE per-problem statistic over a coverage matrix that auto-pulls a problem from the selectable pool (sets `quarantined_at`) when its class-wide coverage is abnormally concentrated-missing on ONE reference node (the signature of a wrong/mispaired solution), fed by a real-PG aggregation over `apollo_graph_comparison_findings`, plus the verification that auto-provisioned problems do NOT move Layer-3 beliefs.

**SEAM:** owns the PURE statistic + the real-PG aggregation join + the `quarantined_at` write + the shadow-verification tests. It READS the findings the §6 SHADOW run already populated; it does NOT change `done.py` or the grading core.

**Scope — files to create/edit (`apollo/provisioning/`):**
- `apollo/provisioning/quarantine.py` (NEW) — `quarantine_decision(coverage_matrix, *, n_min, theta_miss, margin) -> QuarantineVerdict` (PURE; the statistic below — fixture-tested) + `async sweep_quarantine(db)` doing the real-PG AGGREGATION and the `quarantined_at` write. **The join (pinned — §9 OPS-3):** `apollo_graph_comparison_findings` (WHERE `finding_kind='missing_node'`) JOIN `apollo_graph_comparison_runs` ON `findings.run_id = runs.id` JOIN `apollo_problem_attempts` ON `runs.attempt_id = attempts.id`, grouped by `attempts.problem_id` (TEXT == `problem_code`) and the per-node key (`findings.entity_id`/`canonical_key`), filtered `N >= N_MIN`, course-scoped by `runs.search_space_id`; resolve the fired problem to `apollo_concept_problems` via `(concept_id, problem_code == problem_id)` to write `quarantined_at` on the BIGINT row. The sweep RE-CLEARS `quarantined_at` when the concentration no longer fires as N grows (reversible) and writes an observability row on every fire so a human can audit false-positives.
- `apollo/overseer/problem_selector.py` (EDIT, ~1 line) — extend the Tier-2 filter (3B2a) with `quarantined_at IS NULL`.
- `apollo/provisioning/quarantine_constants.py` (NEW) — `N_MIN=8`, `THETA_MISS=0.80`, `CONCENTRATION_MARGIN=0.40` (hand-set v1, config-driven, calibration-tunable — §OPEN-DECISIONS #1).

**The statistic (PURE, fixture-testable):** for problem *P*, reference node *n*, over the *N* graded attempts of *P* in a course: per-node miss rate `m(n) = count(missing_node for n)/N`. Quarantine *P* when `N ≥ N_MIN` AND any single node has `m(n) ≥ THETA_MISS` AND `m(n) − mean_k m(k) ≥ CONCENTRATION_MARGIN`. Per-problem so one bad reference can't hide in an average. **KNOWN LIMIT (§9 OPS-4): the simple rule WILL false-quarantine a genuinely-hard prerequisite node (legitimately missed by most students) — same signature as a mispaired solution. v1 mitigations: (a) reversible (re-clear as N grows); (b) an observability row per fire for human audit; (c) the calibration step that must precede flipping `APOLLO_AUTOPROVISION_ENABLED` treats v1 quarantine as ADVISORY. The point-biserial discrimination conjunction (a hard node has POSITIVE point-biserial; a mispaired reference near-zero/negative) is the v1.1 refinement (§OPEN-DECISIONS #1) — confirm whether numpy (present) suffices to build it now.**

**Dependencies:** `apollo.persistence.models` (`GraphComparisonFinding`, `GraphComparisonRun`, `ProblemAttempt`, `ConceptProblem`). Stdlib `statistics` (or numpy, present) only. NO new package (scipy is a v1.1 open decision, §OPEN-DECISIONS #8).
**Migration?** No (uses 030's `quarantined_at`). **Real-infra?** the statistic is pure fixture-tested (a hand-built coverage matrix → the §8B.7 Tier-3 "mispaired fixture" becomes a deterministic Tier-1 unit test); the `sweep_quarantine` aggregation join + the shadow-verification are real-PG. **plannerAgent:** feller-planner-backend. **v1-or-defer:** v1.
**Key risks owned:** the three hand-set constants (§OPEN-DECISIONS #1); the false-quarantine of hard nodes (mitigated by reversibility + audit + advisory-during-calibration — §9 OPS-4); the 3-table TEXT-vs-BIGINT aggregation join (§9 OPS-3); **the shadow VERIFICATION — assert the FULL invariant: with `APOLLO_GRAPH_SIM_SHADOW_ENABLED` ON + `APOLLO_GRAPH_SIM_LAYER3_ENABLED` OFF, an auto-provisioned Tier-2 problem (a) is teachable AND produces `apollo_graph_comparison_findings` (so quarantine has data) AND (b) moves ZERO Layer-3 beliefs (`apollo_mastery_events`/`apollo_learner_state` unchanged) — §9 SPEC-4**; the quarantine gate threading into the Tier-2 selector.

---

## 4. The Tier-1/Tier-2 problem lifecycle (pinned)

```
SCRAPE (3B2d)        -> apollo_concept_problems row, tier=1 (inventory, NOT selectable), provenance.chunk_content_hash set
                        (UPSERT ON CONFLICT (search_space_id, document_id, chunk_content_hash) -> re-run skips it)
FIND-OR-GENERATE     -> ReferenceSolutionDraft (solution_source=extracted|generated)   (3B2e)
PAIRING GATE         -> PairingVerdict; FAIL -> apollo_rejected_problems (stage=pairing_gate), STOP  (3B2e)
TAG/MINT             -> EntitySpecs upserted; concept canonical_symbols/normalization_map authored; prereqs; misconception entities  (3B2d, via 3B2g)
DEDUP                -> DedupVerdict; merged -> reuse existing entity id; distinct -> new        (3B2c)
PROMOTION LINT       -> run_promotion_lint; FAIL gate g -> apollo_rejected_problems(failed_gate=g), STOP  (3B2b via 3B2g)
                     -> PASS: reference_solution stored in payload; tier flips 1->2 (SELECTABLE); project_canon rebuild  (3B2g)
[runtime, shadow]    -> a Tier-2 problem is teachable; SHADOW_ENABLED ON computes apollo_graph_comparison_findings;
                        LAYER3_ENABLED OFF -> Layer-3 beliefs DO NOT move. A diagnostic shows; calibration gate stays closed.  (existing)
QUARANTINE           -> if class-wide coverage concentrates missing on one node -> quarantined_at set ->
                        excluded from the selectable pool (reversible; re-cleared as N grows).                                (3B2h)
```
The `tier` column + `quarantined_at` column are the two selectability switches; `list_problems_for_concept` filters `tier == 2 AND quarantined_at IS NULL`. The shadow gate is orthogonal: `SHADOW_ENABLED` gates COMPUTE (so findings exist for quarantine), `LAYER3_ENABLED` gates BELIEF MOVEMENT — a Tier-2 auto-provisioned problem is fully teachable in shadow without moving any belief.

---

## 5. Integration map (verified surfaces, per stage — re-verified 2026-06-19)

| Concern | Module : line | Verdict |
|---|---|---|
| §8 seed reuse (stage-4 mint) | `apollo/persistence/learner_model_seed.py:202,240,133,272,307,80` | REUSE the pure converters; WRAP the disk-bound seed writer's KEYS; gate-2 = `validate_reference_graph` ONLY |
| `entry_type` mint map | `apollo/persistence/learner_model_seed.py:80-86,196-199` | LACKS `variable_mapping`, NO fallback → KeyErrors at mint; §OPEN-DECISIONS #5 (extend map OR gate-1 fail-closed) |
| Trigger seam (stage-0) | `knowledge/teacher_weekly.py:1131-1174` (READY flip + commit) / `:599-607` (caller) / `:1041` (content_hash) | THIN content-key transactional enqueue inside that session; owner-doc boundary (domain-data ↔ apollo) |
| Worker SHELL | `apollo/learner_janitor_worker.py` (whole file; docstring `:7-16` = "shell only, NO claim/lease/SQL") | MIRROR the shell (loop/flag/SIGTERM/single-loop) — the DRAIN is separate (next row) |
| Claim/lease DRAIN | `knowledge/teacher_weekly.py:348,340,881-915` (`run_upload_worker_loop`/`_claim_upload_job_async`/`with_for_update(skip_locked=True)`) | the DRAIN template (net-new in 3B2f) — NOT the janitor |
| LLM seam (returns str only) | `apollo/agent/_llm.py:35-95` (`_log_call` reads usage, DISCARDS it; returns content) | REUSE for calls; the cost/token aggregate needs a METERED wrapper (3B2f) — usage is NOT returned today |
| Shadow plug-in | `apollo/handlers/done.py:67/74/84`, `apollo/handlers/done_grading.py:126-135` | ZERO `done.py` change; SHADOW (compute) vs LAYER3 (persist) DISTINCT flags; `opposes_map` empty |
| `:Canon` rebuild (stage-6) | `apollo/knowledge_graph/canon_projection.py:195` `project_canon(db,neo,*,search_space_id,concept_id)` | CALL after promotion; NOT net-new |
| Tier-2 selection gate | `apollo/overseer/problem_selector.py:34-48` (NO tier filter today) | ADD `tier==2 AND quarantined_at IS NULL` (3B2a + 3B2h) — load-bearing for safety |
| Gate primitives (3B2b) | `apollo/ontology/edges.py:39-86`, `apollo/ontology/graph.py` (topo-order), `apollo/schemas/problem.py:33-54`, `apollo/solver/sympy_exec.py:35-64` | ~60% pre-built; gate 6 does NOT reject foreign symbols (auto-creates) → gate 4 carries it; gates 4/7/8 net-new |
| Quarantine join (3B2h) | `apollo/persistence/models.py:540-561` (findings: run_id+entity_id), `:492-537` (runs: attempt_id+search_space_id), `:265-271` (attempts: problem_id TEXT==problem_code), `:158-169` (concept_problems: BIGINT id) | findings→runs→attempts(problem_code TEXT)→(concept_id,problem_code)→concept_problems(BIGINT) — a 3-table join, TEXT-vs-BIGINT |
| `scope_summary` (dedup source) | absent from `apollo_kg_entities` (`:362-389`) AND `apollo_concepts` (`:136-155`) | NEW column added in 030; embedded ON THE FLY by 3B2c (§NEW-GAP) |
| Embeddings / retrieval | `indexing/document_embedder.py` `embed_text`, `retrieval/` hybrid pipeline | REUSE; inject for deterministic Tier-1 |
| Migration baseline + harness | `database/migrations/018:77-90`, `026:184-300`, `tests/database/test_apollo_learner_model_migration.py:40-79` (`_TOUCHES_TARGETS`/`_EXCLUDE_FROM_CHAIN`/`_STUB_DDL`) | 030 next-free; mirror 026 conventions; ADAPT (not copy) the chain selector |

---

## 6. Build order (stacked branches)

1. **3B2a** branches off `feat/apollo-kg-wu6a3-selection-wiring` (#49, tip `a869dc9`) — the diff-cover compare branch. Migration 030 + ORM + the Tier-2 selection gate + the `scope_summary` column. Real-PG, LLM-free. Unblocks everything.
2. **3B2b** branches off 3B2a. The pure 8-gate lint. Pure fixtures, NO container. The safety core.
3. **3B2c** branches off 3B2a (after 3B2b so the stack is linear). The dedup ladder. Mock-embedding cross-course non-merge proof.
4. **3B2d** branches off 3B2c (depends on 3B2b + 3B2c). Scrape + tag/mint + author canonical_symbols. Mocked LLM Tier-1.
5. **3B2e** branches off 3B2d (depends on 3B2d's `CandidateQuestion` type). Find-or-generate + pairing gate. Fail-closed.
6. **3B2f** branches off 3B2e (depends on 3B2a's queue table + the metered-chat target). Claim/lease drain + metered LLM client. Real-PG concurrency.
7. **3B2g** branches off 3B2f (depends on 3B2b + 3B2e + 3B2f + 3B2d's stage-4). Trigger + worker shell + orchestrator + promotion + observability + lease-reaper. The orchestration spine.
8. **3B2h** branches off 3B2g. Quarantine + shadow verification. The backstop; needs the full pipeline to test end-to-end.

Each sub-unit is one TDD-executor pass: 3B2a ≈ 1 migration + ORM edits + 1 ADAPTED real-PG harness; 3B2b ≈ 2 pure modules + ~10 fixture test files (positive + 8 adversarial + the variable_mapping gate-1 case); 3B2c ≈ 2 modules + mock-embedding + real-PG scope tests; 3B2d/3B2e ≈ 2-3 modules each + mocked-LLM + real-PG; 3B2f ≈ 3 modules + real-PG concurrency tests; 3B2g ≈ 5 modules + 1 thin trigger edit + 1 Procfile line + real-PG/neo tests; 3B2h ≈ 3 modules + pure-stat + real-PG aggregation + shadow-verification. **None is XL** — the claim/lease split keeps 3B2g an L pass. **The first unit's compare-branch is `feat/apollo-kg-wu6a3-selection-wiring`.**

---

## 7. Doc / coverage obligations (per project contracts)

- **Patch coverage ≥95%** on changed lines (`diff-cover coverage.xml --compare-branch=origin/<parent> --fail-under=95`; intra-stack compare branch = the previous sub-unit, first = `feat/apollo-kg-wu6a3-selection-wiring`). 3B2b pure core + 3B2h pure stat are exhaustible without containers; 3B2a/c/d/e/f/g trip the `tests/database` real-PG gate (green-not-skipped). The **DB-coverage contract** applies to 030 (enumerate every column/CHECK/RLS-enable/backfill/policy×role, test ≥95% on LOCAL Docker Postgres — NEVER a remote project).
- **NEVER apply migrations to any remote DB.** 030 runs on LOCAL Testcontainers (pgvector:pg16) only; rehearsal-on-test then prod is a human/CI step.
- **Drift contract:** reconcile the owner docs in the SAME commit each sub-unit lands — `docs/architecture/apollo.md` (register `apollo/provisioning/**` + `apollo/provision_worker.py` + the `tier`-filtered selector + `APOLLO_AUTOPROVISION_ENABLED` + the metered-chat note + the shadow plug-in note), `docs/architecture/domain-data.md` (register migration 030 + the 5 new ORM classes + the `scope_summary` column + the `knowledge/teacher_weekly.py` enqueue edit), `docs/architecture/indexing.md` (note the read-existing-chunks-not-reindex + content-hash idempotency contract at the completion seam) — and bump `last_verified` to today.
- **No new package** (sympy/pydantic/openai/numpy/embedder all present — confirmed `requirements.txt`). Any candidate (scipy for point-biserial, instructor for structured-output, a pytest-LLM-mock helper) is an OPEN DECISION, NOT adopted — §OPEN-DECISIONS #8.

---

## 8. Out-of-scope (held firmly)
- **Human-in-the-loop review.** §8B is fully automatic. No approval UI.
- **The §8 seed script generalization beyond what's reused.** `scripts/seed_apollo_learner_model.py` stays the bernoulli BOOTSTRAP; 3B2 PARALLELS its keys.
- **`done.py` / grading-core changes.** ZERO. Auto-provisioned Tier-2 problems flow through the existing shadow path.
- **Flipping `APOLLO_GRAPH_SIM_LAYER3_ENABLED` or `APOLLO_AUTOPROVISION_ENABLED` in prod.** Both stay OFF; activation is a human calibration/deploy step.
- **Tier-2 nightly / Tier-3 release real-LLM runs.** Scaffolded in 3B2g/3B2h, run on the nightly/release schedule, not per-PR.
- **Teacher-UI surfacing of provisioning status/observability.** Backend-only; a dashboard is a separate teacher-ui follow-up.
- **The point-biserial / scipy quarantine refinement.** v1.1 (§OPEN-DECISIONS #1/#8).
- **A persisted entity embedding column / pgvector on `apollo_kg_entities`.** 3B2c embeds `scope_summary` on the fly; a persisted vector index is a perf follow-up.

---

## 9. Critic findings — resolution log (every valid finding folded in; rejections justified)

**FEASIBILITY:**
- **FEAS-1 (HIGH) — worker-template mismatch.** ACCEPTED. Re-verified: `learner_janitor_worker.py:7-16` is a pure poll shell ("adds NO claim/lease/backoff/dead-letter, NO SQL"); the SKIP-LOCKED claim/lease is a SEPARATE template (`teacher_weekly.py:348/881-915`). RESOLUTION: split the claim/lease drain into its OWN sub-unit (3B2f) with real-PG concurrency tests (two workers, SKIP-LOCKED non-overlap, lease-expiry re-claim, cost-abort dead-letter); 3B2g's worker mirrors only the SHELL; this keeps 3B2g an L pass (the split that prevents the hidden XXL).
- **FEAS-2 (MEDIUM) — gate-4/gate-6 generalization gap.** ACCEPTED. Re-verified: `parse_zero_form` (`sympy_exec.py:35-64`) builds `local_dict` from a hardcoded bernoulli `_CANONICAL_SYMBOLS` then calls `parse_expr` which AUTO-CREATES unknown symbols — gate 6 does NOT reject foreign symbols; gate 4 is the sole guard, and `apollo_concepts.canonical_symbols`/`normalization_map` are `default=dict` (empty) for auto-provisioned concepts. RESOLUTION: 3B2d's stage-4 AUTHORS canonical_symbols/normalization_map per minted concept; 3B2b documents that gate 6 doesn't reject foreign symbols. Open sub-decision (§OPEN-DECISIONS #5b): scrape authors the set vs gate 4 derives it from the concept's already-promoted problems.
- **FEAS-3 (MEDIUM) — quarantine join under-specified.** ACCEPTED (also raised by OPS-3). RESOLUTION: 3B2h splits the PURE statistic (over a hand-built coverage matrix) from the real-PG AGGREGATION (the pinned findings→runs→attempts join), each with its own test.
- **FEAS-4 (LOW) — path error.** ACCEPTED. Re-verified via glob: `done_grading.py` is `apollo/handlers/done_grading.py`, NOT `apollo/agent/`. §5 corrected.
- **FEAS-5 (LOW) — migration-harness "copy 1:1" optimistic.** ACCEPTED. Re-verified: the harness uses a content-scoped `_TOUCHES_TARGETS` regex + `_EXCLUDE_FROM_CHAIN` (`:54-68`). RESOLUTION: 3B2a scope now says the harness is ADAPTED — re-derive the chain selector so 018+026 precede 030 (026 now IN-chain since `apollo_kg_entities`/`apollo_concepts` are FK targets), 030 applied last; enumerate the FK targets.

**SPEC-FIDELITY:**
- **SPEC-1 (MEDIUM) — idempotency-granularity mismatch.** ACCEPTED. RESOLUTION: the queue's partial-unique-index is JOB-level dedup; the `(document_id, chunk_content_hash)` no-op is a SEPARATE intra-job guarantee enforced by an UPSERT ON CONFLICT on the Tier-1 scrape write (3B2g orchestrator); acceptance assertion: re-run a completed job → zero new Tier-1 rows. (Note: uses `chunk_content_hash`, not the volatile `chunk_id` — see OPS-2.)
- **SPEC-2 (MEDIUM) — misconception storage deviates from literal §8B.2.** ACCEPTED. RESOLUTION: reframed §OPEN-DECISIONS #2 as a DELIBERATE spec-text deviation — §8B.2:1291 literally names `apollo_misconceptions` (re-verified: migration-019 is a DISTINCT runtime misconception-inference channel with embedding/probe/RT-step schema), but we recommend the kg_entities `kind=misconception` form because it matches the §8 seed minting path + the §6.5 opposes-link requirement (the substantive requirement). Orchestrator must sign off explicitly or request BOTH.
- **SPEC-3 (LOW) — entry_type reconciliation may narrow expressiveness + is a gate gap.** ACCEPTED. Re-verified: `_entity_key_for_step:196-199` bare-indexes the map; `variable_mapping` is a first-class `Problem.EntryType` but absent from the map → KeyErrors at MINT, and gate-1 ACCEPTS it (gate gap). RESOLUTION: prefer option (b) — extend the frozen `_ENTRY_TYPE_TO_KIND_PREFIX` to cover `variable_mapping`; if the frozen edit is disallowed, ADD a gate-1 sub-check rejecting any `entry_type` not in the mint map so it fails CLOSED at the lint. Hard contract before 3B2d builds (§OPEN-DECISIONS #5).
- **SPEC-4 (LOW) — shadow vs belief-freeze flag conflation.** ACCEPTED. Re-verified: `done.py:67` SHADOW (compute) and `:84` LAYER3 (persist) are DISTINCT. RESOLUTION: 3B2h's shadow-verification asserts the FULL invariant — SHADOW ON + LAYER3 OFF → auto-provisioned Tier-2 problem IS teachable AND produces findings (so quarantine has data) AND moves zero beliefs. §4 lifecycle clarified.

**OPS / COST / SAFETY:**
- **OPS-1 (HIGH) — no cost/token data source.** ACCEPTED. Re-verified: `_llm.py:35-95` — `_log_call` reads usage only to log, then DISCARDS it; `cheap_chat`/`main_chat` return only the content string. The old "feeds from the log for free" claim is FALSE. RESOLUTION: 3B2f adds a provisioning-local `MeteredChat` that captures `response.usage` into the `ingest_run` aggregate (the cost-ceiling data source); `_llm.py` is NOT mutated; the per-document ceiling compares a running aggregate after each call.
- **OPS-2 (HIGH) — idempotency does not survive a re-index.** ACCEPTED. Re-verified: `checkpoint_indexer.py:90-128` "deletes and reinserts that page's chunks" → `aita_chunks.id` is a fresh surrogate on re-upload. RESOLUTION: the Tier-1 idempotency key is `chunk_content_hash = sha256(normalize(chunk content))` (NOT `chunk_id`), AND a document-level `content_hash` on `apollo_ingest_runs` short-circuits enqueue for an unchanged re-upload (compare `AITADocument.content_hash` at `teacher_weekly.py:1041`). Gate-8 is documented as a last-resort net, not the primary re-upload guard. Provenance now stores `chunk_content_hash`.
- **OPS-3 (MEDIUM) — quarantine problem-identity join unspecified (TEXT vs BIGINT).** ACCEPTED. Re-verified: `ProblemAttempt.problem_id` is TEXT == `problem_code` (`models.py:270`); runs have `attempt_id`+`search_space_id` but no problem_id; the quarantine write targets the BIGINT `id`. RESOLUTION: 3B2h pins the full findings→runs→attempts(problem_code TEXT)→(concept_id,problem_code)→concept_problems(BIGINT) join, course-scoped by `runs.search_space_id`, per-node key = `findings.entity_id`/`canonical_key`. §OPEN-DECISIONS #6 namespace contract extended to cover problem identity.
- **OPS-4 (MEDIUM) — quarantine false-positive guard thin.** ACCEPTED. RESOLUTION: 3B2h makes quarantine reversible (re-clear as N grows), emits an observability row per fire for human audit, and states the simple rule WILL false-quarantine hard prereq nodes until the point-biserial conjunction lands — so the calibration step treats v1 quarantine as ADVISORY. Confirm whether numpy (present) suffices for point-biserial now (§OPEN-DECISIONS #1).
- **OPS-5 (MEDIUM) — no per-run terminal-status reconciliation / lease-reaper.** ACCEPTED. RESOLUTION: 3B2g adds a lease-reaper (re-open an expired `'running'` job, mark its `ingest_run` failed), wraps each stage so a survived error writes `apollo_ingest_errors` AND a terminal failure flips `ingest_runs.status='failed'` + the job to `'failed'` (never left `'running'`, which the partial-unique-index would wedge against re-enqueue), and emits a one-line-per-run `_LOG.info` health signal (run_id/status/n_promoted/n_rejected/llm_cost).
- **OPS-6 (LOW) — §1.8 fabricated-solution narrative imprecise.** ACCEPTED. RESOLUTION: 3B2e + §OPEN-DECISIONS #12 state precisely that a coherent-wrong solution passing pairing + all 8 gates IS shown to students as teachable (shadow diagnostic, no belief movement) and removed only RETROACTIVELY by quarantine — NOT prevented pre-exposure. The headline safety is the `APOLLO_AUTOPROVISION_ENABLED` default-OFF + the calibration gate; the calibration criteria are now an explicit open decision (§OPEN-DECISIONS #12).

**NEW (reviser-discovered):**
- **NEW-GAP — `scope_summary` does not exist.** The dedup ladder's embedding-similarity input (`scope_summary`) is absent from both `apollo_kg_entities` and `apollo_concepts`. RESOLUTION: migration 030 adds a nullable `scope_summary TEXT` to `apollo_kg_entities` (authored at mint), embedded ON THE FLY by 3B2c (no persisted vector → no pgvector-on-entities migration). §OPEN-DECISIONS #11.

**No findings rejected** — all eight critic findings were independently re-verified against the code and are valid.

---

## OPEN DECISIONS FOR THE ORCHESTRATOR (genuine judgment calls — do NOT let an executor guess)

1. **[QUARANTINE — statistic + constants] (3B2h)** Recommended v1: `N_MIN=8`, `THETA_MISS=0.80`, `CONCENTRATION_MARGIN=0.40`, the concentration rule. The prior-art memo recommends ALSO conjoining a **point-biserial discrimination** test + a MAD-relative threshold (the classical miskey-detection result) to avoid false-quarantining a genuinely-hard node. **Confirm: ship the simple miss-concentration rule for v1, or build the point-biserial conjunction now (numpy is present)?** (Recommended: simple rule v1 + reversibility + audit row + advisory-during-calibration; point-biserial v1.1.)
2. **[STORAGE — auto-minted misconceptions] (3B2d)** §8B.2:1291 literally says "writes `apollo_misconceptions`," but migration-019's `apollo_misconceptions` is a DISTINCT runtime misconception-inference channel (embedding/probe/RT-step). Recommended: the kg_entities `kind=misconception` form (matches the seed, drives the §6 opposes-competition, low runtime risk since `opposes_map` is empty in v1). **Confirm the kg_entities form satisfies §8B.2 — or write BOTH?** (A deliberate spec-text deviation requiring explicit sign-off.)
3. **[MIGRATION — column types + scope denormalization] (3B2a)** Confirm: `tier SMALLINT CHECK IN (1,2)`; `solution_source TEXT CHECK IN ('extracted','generated','authored')`; `provenance JSONB`; `quarantined_at TIMESTAMPTZ`; `llm_cost_usd NUMERIC(12,6)`; `content_hash TEXT` on `apollo_ingest_runs`. AND confirm `search_space_id` is denormalized onto `apollo_concept_problems` + the 4 observability tables + the queue (recommended; leave `apollo_concept_problems.search_space_id` NULLABLE in v1, tighten in a later two-step migration). Confirm the NOT NULL timing.
4. **[DEDUP — thresholds] (3B2c)** `EMBED_MERGE_THRESHOLD` + `EMBED_JUDGE_BAND` are hand-set v1 constants with no spec value. Recommended: merge ≥0.92, judge 0.82–0.92, distinct <0.82 (calibration-tunable, config-driven). **Confirm the bands.**
5. **[VOCABULARY — entry_type reconciliation] (3B2d/3B2b)** `Problem.EntryType` has `variable_mapping`; the frozen `_ENTRY_TYPE_TO_KIND_PREFIX` does NOT (KeyErrors at mint; gate-1 accepts it). **Confirm: (a) constrain scrape output to the 5 mapped types; (b) EXTEND the frozen seed map to cover `variable_mapping` (edits a WU-3B module) — RECOMMENDED, since gate-1 otherwise accepts a type that mint can't handle and (b) preserves full expressiveness. Plus (5b): does stage-4 AUTHOR each concept's `canonical_symbols` set, or does gate 4 DERIVE it from the concept's already-promoted problems (first-writer-wins)?** Hard contract the executor must NOT guess.
6. **[NAMESPACE — TEXT vs BIGINT identities] (3B2a/d/h boundary)** Two namespaces coexist: `concept_id` (slug at scrape, BIGINT `apollo_concepts.id` at persist) AND `problem_id` (TEXT `problem_code` on attempts/findings, BIGINT `apollo_concept_problems.id` for the quarantine write). Confirm the boundary contract: scrape mints slugs; persistence resolves to BIGINT; gate-8 dup key + dedup first-writer-wins key on the BIGINT concept; the quarantine join resolves `problem_code` TEXT → the BIGINT row via `(concept_id, problem_code)`.
7. **[COST — per-document budget + routing] (3B2f)** The pipeline runs ≥5 LLM stages per document over many chunks; without the metered client (3B2f) there is NO token signal. Confirm: the `PER_DOCUMENT_TOKEN_CEILING` value; the model→price table; and the routing (scrape→`cheap_chat`, generate→`main_chat`, judges→`cheap_chat` — recommended). A cost abort writes `apollo_ingest_errors` + marks the run failed.
8. **[PACKAGES — none for v1] CONFIRM no new package** (sympy/pydantic/openai/numpy/embedder present). Candidates flagged but NOT adopted: `scipy.stats` (point-biserial/binomial for quarantine v1.1 — numpy may suffice; do NOT add for three formulas); `instructor` (replaceable by `response_format` + manual re-prompt); a pytest-LLM-mock helper (one `patch` per stage suffices). **Confirm: NO new package; each candidate a deferred decision.**
9. **[TIER-2 SELECTOR — one predicate covers both callers?] (3B2a/h)** The Tier-2 + quarantine filter is two predicates on `list_problems_for_concept`, which gates BOTH `select_problem` AND `select_problem_personalized`. **Confirm no separate selector edit is needed beyond the predicate** (verify against the #49 tip).
10. **[TESTING TIERS — what ships per-PR] CONFIRM** WU-3B2 ships Tier-1 (mocked LLMs: per-stage unit tests + the 8-gate suite + the dedup cross-course non-merge proof + the deterministic mispaired-fixture quarantine test) + the HARNESS for Tier-2 (nightly, synthetic mini-textbook) / Tier-3 (release, real textbook). Real-LLM tiers are nightly/release, NOT per-PR. **Confirm the Tier-2/3 harness is scaffolded (not run) in 3B2g/3B2h.**
11. **[DEDUP EMBEDDING SOURCE — `scope_summary`] (3B2a/c)** `scope_summary` does NOT exist on any table. Recommended: add a nullable `scope_summary TEXT` to `apollo_kg_entities` in 030 (authored at mint), embedded ON THE FLY by the dedup ladder (no persisted vector → no pgvector-on-entities migration). **Confirm the column + the on-the-fly-embedding approach** vs persisting an entity embedding column.
12. **[CALIBRATION GATE CRITERIA — the real safety] (cross-unit)** Because a coherent-wrong solution passing pairing + all 8 gates IS shown to students (shadow only, no belief movement) until quarantine fires retroactively, the headline safety is the `APOLLO_AUTOPROVISION_ENABLED` default-OFF + the calibration gate. **Confirm the calibration criteria — what evidence is required before flipping the flag (e.g. a human-reviewed sample of N auto-provisioned problems, a quarantine false-positive rate ceiling)?** Currently implied, not specified.

---

## ORCHESTRATOR ADJUDICATION (2026-06-19) — all 12 decisions ruled, build authorized

The meta-orchestrator independently re-verified every load-bearing claim against the code before ruling. The 5 make-or-break anchors were re-grepped on 2026-06-19: (#5) the frozen `_ENTRY_TYPE_TO_KIND_PREFIX` (`learner_model_seed.py:80-86`) has exactly the 5 keys `equation|condition|simplification|procedure_step|definition` and **lacks `variable_mapping`** — confirmed; (#9) BOTH `select_problem` AND `select_problem_personalized` funnel through `list_problems_for_concept` (`problem_selector.py:34`) — confirmed one predicate covers both; (#11) `scope_summary` is **absent** from `apollo_kg_entities`/`apollo_concepts` (only in spec+proposal) — confirmed a genuine new column; (#12) `APOLLO_AUTOPROVISION_ENABLED` does **not** exist yet — confirmed it is net-new; (#2) `apollo_misconceptions` (migration-019) is a DISTINCT runtime channel with NOT-NULL `code/description/probe_question`+embedding+rt_steps. `ENTITY_KINDS` (`models.py:53-61`) was read to ground #2/#5: it contains both **`variable`** and **`misconception`** as first-class kinds.

| # | Decision | RULING |
|---|---|---|
| 1 | Quarantine statistic | **Ship the SIMPLE miss-concentration rule for v1** (`N_MIN=8`, `THETA_MISS=0.80`, `CONCENTRATION_MARGIN=0.40`, stdlib `statistics` only). Reversible (re-clear as N grows) + one observability row per fire + treated ADVISORY during calibration. Point-biserial conjunction is **v1.1 (NOT built now)** — avoids scipy and over-building; the flag-OFF default + calibration gate are the real safety. |
| 2 | Auto-minted misconception storage | **Use the `apollo_kg_entities` `kind=misconception` form** (seed-consistent: `misconception` ∈ ENTITY_KINDS; reuses the frozen `misconceptions_to_entities` converter; drives the §6.5 opposes-competition — the SUBSTANTIVE requirement). **Deliberate, documented deviation from the literal §8B.2:1291 `apollo_misconceptions`** — orchestrator SIGNS OFF. Do NOT write both (migration-019's table needs NOT-NULL Socratic `probe_question`/`rt_steps` that auto-provisioning v1 cannot responsibly author; `opposes_map` is structurally empty in v1 so the runtime channel is dormant). Record the deviation in `apollo.md` + the 3B2d plan. |
| 3 | Migration column types + scope denorm | **Accept the §3 DDL verbatim.** `tier SMALLINT CHECK IN (1,2)`; `solution_source TEXT CHECK IN ('extracted','generated','authored')`; `provenance JSONB`; `quarantined_at TIMESTAMPTZ`; `llm_cost_usd NUMERIC(12,6)`; `content_hash TEXT`. Denormalize `search_space_id`: **NOT NULL** on the 4 observability tables + the queue (fresh tables, no backfill risk, mirrors 026); **NULLABLE** on `apollo_concept_problems` (backfilled in-migration, tighten in a later two-step). |
| 4 | Dedup thresholds | **merge ≥0.92 / judge 0.82–0.92 / distinct <0.82**, config-driven + calibration-tunable. |
| 5 | entry_type reconciliation | **Option (b): EXTEND the frozen `_ENTRY_TYPE_TO_KIND_PREFIX`** with `"variable_mapping": ("variable", "varmap")` — maps to the EXISTING `variable` ENTITY_KIND with a fresh prefix (purely additive; no seeded problem uses `variable_mapping`, so the FROZEN WU-6A2 `reference_entity_keys` consumer is unaffected). **AND** 3B2b adds a gate-1 mint-map-membership sub-check (defense-in-depth: any future unmapped `entry_type` fails CLOSED at the lint, not KeyError at mint). **(5b) AUTHOR** canonical_symbols at mint (first-writer-wins, later problems UNION) — NOT derive-from-promoted (circular: gate 4 runs before promotion). |
| 6 | TEXT vs BIGINT namespace | **Accept the pinned contract.** Scrape mints slugs → persistence resolves slug→BIGINT `apollo_concepts.id`; gate-8 dup key + dedup first-writer-wins key on the **BIGINT** concept; quarantine join resolves `problem_code` TEXT → BIGINT `apollo_concept_problems.id` via `(concept_id, problem_code)` (§9 OPS-3 join). |
| 7 | Cost budget + routing | **Routing:** scrape→`cheap_chat`, generate→`main_chat`, judges→`cheap_chat`. `PER_DOCUMENT_TOKEN_CEILING = 2_000_000` (a runaway circuit-breaker, NOT a tight budget — generous for a large chapter; the real cost control is flag-OFF). Model→price table as config constants (gpt-4o ≈ $2.50/$10 per 1M in/out; gpt-4o-mini ≈ $0.15/$0.60). A cost abort writes `apollo_ingest_errors` + marks the run `failed`. |
| 8 | Packages | **NO new package** (sympy/pydantic/openai/numpy/embedder/rapidfuzz all present). scipy.stats DEFERRED (point-biserial is v1.1; numpy suffices if ever needed); instructor DEFERRED (`response_format`+manual reprompt); no pytest-LLM-mock helper (one `patch` per stage). This RULING satisfies the standing "never install new packages without confirming" constraint by choosing the none path. Every sub-unit's testGuidance: **if a plan proposes a package, BLOCK + escalate to the orchestrator.** |
| 9 | Tier-2 selector predicate | **CONFIRMED against the #49 tip:** both selectors call `list_problems_for_concept` (`:34`); adding `tier==2` (3B2a) + `quarantined_at IS NULL` (3B2h) to that single WHERE gates BOTH with NO signature change and NO separate selector edit. |
| 10 | Testing tiers per-PR | **Tier-1 ONLY per-PR** (mocked LLMs + the 8-gate suite + the cross-course non-merge proof + the deterministic mispaired-fixture quarantine test + real-PG for DB stages). Tier-2 (nightly synthetic) / Tier-3 (release real textbook) harnesses **SCAFFOLDED, not run** in 3B2g/3B2h. No real-LLM tokens in CI. |
| 11 | scope_summary source | **Add nullable `scope_summary TEXT` to `apollo_kg_entities` in 030** (authored at mint from display_name+canonical symbols), embedded ON THE FLY by 3B2c via the existing `indexing.document_embedder.embed_text`. NO persisted entity vector (no pgvector-on-entities migration). |
| 12 | Calibration gate criteria | **Documented (not code-enforced — the flag flip is a human deploy step).** Before flipping `APOLLO_AUTOPROVISION_ENABLED` in any env: (a) human-reviewed sample ≥30 auto-provisioned problems/course pass; (b) quarantine false-positive rate ≤10% on the shadow corpus (human audit of per-fire rows); (c) zero Layer-3 belief movement confirmed (LAYER3 OFF) over the shadow window; (d) per-document cost within the budgeted ceiling. v1 ships the flag OFF + the observability that FEEDS this checklist (ingest_runs aggregates, rejected_problems, dedup_decisions, quarantine audit rows). Record the checklist in `apollo.md`. |

**No new package; no remote-DB touch** — so per the standing autonomous mandate the build proceeds WITHOUT a user stop. **Build order (stacked, LLM-free-first):** `3B2a → 3B2b → 3B2c → 3B2d → 3B2e → 3B2f → 3B2g → 3B2h`. The first sub-unit `3B2a` (migration 030 + ORM + Tier-2 gate, `feller-planner-db`, real-PG Testcontainers-ONLY) branches `feat/apollo-kg-wu3b2a-migration-030` off `a869dc9` with diff-cover compare-branch `feat/apollo-kg-wu6a3-selection-wiring` (#49). **This is the LAST unit — when 3B2h lands, the whole Apollo KG learner-model spec is built.**

### Post-build notes

- **WU-3B2b gate-3 narrowing (orchestrator-confirmed SOUND, no code change).** The pinned gate-3 contract-table wording reads "DAG: acyclic + every node reachable from a root (no orphan islands)"; the shipped `_gate_3` checks **acyclicity only**. Adjudicated sound on two independent grounds: (1) the literal "every node reachable from a root" clause is **vacuous for a DAG** — every node of a finite DAG is reachable from some source by walking incoming edges backward, so acyclicity already implies it; (2) the "no orphan islands" intent is **subsumed by gate 2** — `validate_reference_graph` clause (d) requires "every reference-node id appears on ≥1 declared path" (`learner_model_seed.py:350-356`), so a node disconnected from the declared reasoning fails gate 2 before gate 3. Adding a connected-component check to gate 3 would duplicate gate 2's coverage guarantee. Recorded here so a future reviewer does not re-flag. All 8 gates were orchestrator-mutation-proven discriminating (each gate neutered → exactly its adversarial fixture REDs).

- **WU-3B2d idempotency mechanism (orchestrator-ACCEPTED deviation).** The plan pinned `insert().on_conflict_do_nothing(['concept_id','problem_code'])` against migration-018's `UNIQUE(concept_id, problem_code)`; the executor shipped an app-level **SELECT-then-skip** guard in `write_tier1_problems` because the `create_all`-built test ORM does not declare that constraint. ACCEPTED as a documented deviation: (1) the idempotency PROPERTY holds and is mutation-discriminating (re-run → 0 inserts; neutering the guard REDs both `test_scrape_rerun_*` — orchestrator-verified); (2) the key `(provisional_concept_id, "scrape.{chunk_content_hash}")` is content-stable across a re-index (the provisional concept is resolved idempotently, the hash is content-derived); (3) the only weakness — a concurrent same-document scrape raising `IntegrityError` instead of a clean no-op — is architecturally unreachable: 3B2f's SKIP-LOCKED lease + the partial-unique-index on `apollo_provisioning_jobs(document_id) WHERE state IN open` guarantee one worker per document; and the real-PG `UNIQUE(concept_id, problem_code)` is a hard backstop against any duplicate row regardless. Concurrency-hardening (declare the `UniqueConstraint` on the `ConceptProblem` ORM so a portable `ON CONFLICT` is testable, or switch when the queue invariant is relaxed) is a recorded downstream follow-up. The deviation is honestly documented in the `write_tier1_problems` docstring.
- **WU-3B2d frozen-map extension (orchestrator-mutation-proven additive).** `_ENTRY_TYPE_TO_KIND_PREFIX` gained `"variable_mapping": ("variable", "varmap")`. Verified additive: removing the key keeps the WU-6A2 `test_personalization_select.py` golden vectors GREEN (22 passed — the `reference_entity_keys` reconstruction parity is unaffected) yet REDs the new variable_mapping mint tests. The `tier=1`-explicit safety trap is mutation-proven (flipping to `tier=2` REDs `test_tier1_row_excluded_by_selector` — un-linted scraped rows would leak to students).
