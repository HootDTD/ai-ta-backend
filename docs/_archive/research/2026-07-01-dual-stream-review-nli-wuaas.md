# WU-AAS + NLI Resolver Tier — Honest Status Report

**Branch:** `staging` · **Date:** 2026-07-01 · **Scope:** last ~5 days
**Streams:** (A) NLI resolver tier — SchoolsInBeirut, PR #76 (merge `03a5055`); (B) WU-AAS authored problem/solution sets — ishaanbatra, ~35 commits `e4dae97`…`c6333f0`
**Review basis:** four stream code reviews + one test inventory, spot-verified against code on `staging`. DB-backed tests could NOT be re-executed in the review sandbox (testcontainers absent); their existence/naming was confirmed, their pass status was not independently re-run except the non-DB subset.

---

## 1. What was actually done (verified against code)

### Stream A — NLI resolver tier (SchoolsInBeirut)
- **Per-model thresholds re-tuned, not copied** (`apollo/resolution/nli_config.py`): `_PER_MODEL_DEFAULTS` = large `min_ent 0.90 / max_con 0.05 / veto 0.98`, small `0.70 / 0.10 / 0.96`, resolved at runtime by `load_nli_params()` with `APOLLO_NLI_*` env overrides. Verified.
- **Default-ON flip** (`cc942f0`): `nli_enabled()` returns `True` when `APOLLO_NLI_ENABLED` is unset; `0/false/no/off/''` is the kill switch. **Verified in code** (`nli_config.py:35`).
- **NLI deps moved into main `requirements.txt`** (lines 30–32: `transformers>=4.44,<5`, `torch>=2.2`, `sentencepiece`, behind the CPU wheel index). **Verified.**
- **Recall-only references fallback** (`resolver.py` `_content_match`): fires only after lexical tiers miss; can only convert unresolved→resolved, never downgrade. Misconception veto (`nli_resolution.match_nli_semantic`) runs first. Content-overlap floor + polarity guard gate every certification.
- **Litotes bounded window** (`polarity.py` `_LITOTES_WINDOW=3`).
- **Feeds only the SHADOW graph-sim grade** — both consumer flags (`APOLLO_GRAPH_SIM_SHADOW_ENABLED`, `APOLLO_CLARIFICATION_ENABLED`) are default-OFF in prod, so default-ON NLI cannot alter a live student grade **as prod is currently configured**.

### Stream B — WU-AAS authored sets (ishaanbatra)
- **Migration + model** (`e4dae97`): `database/migrations/032_apollo_authored_sets.sql`, `apollo/persistence/models.py::AuthoredSet`.
- **Label scrape + matcher** (`fbe04bf`/`933bf7a`, fix `7184007`): `label_match.py` sub-label regex now requires the trailing letter be adjacent to the digit (fixes the `1m` absorption bug); ambiguous (≥2 chunks) and zero-match both return `None` → fall through to doc-scoped retrieval.
- **Vision OCR** (`2d7972e`): `ocr/openai_vision.py` degrades to empty `OCRResult` on API error / bad JSON / empty text rather than raising or fabricating.
- **PDF indexer** (`238adba`, fix `0032181`): `indexing.py` `_find_existing_indexed_doc` matches by `unique_identifier_hash` then falls back to `content_hash` (both search-space-scoped) → idempotent re-upload.
- **Paired-solution retrieve_fn** (`22477be`): `paired_retrieval.py` strictly filters both branches on `AITAChunk.document_id == solution_document_id`; does not touch the student-RAG active-document gate.
- **OCR verify** (`373c83b`): `verification.py` generate-and-compare runs only when low-confidence; exact final-answer match short-circuits; unparseable judge fails closed.
- **Provisioning + endpoints** (`20d2cb7`/`1aabbbb`): `authored_sets/api.py` create/list/get/approve/delete + background lifecycle; `orchestrator.py`.
- **Fix wave** (`0032181`..`c6333f0`):
  - **Dedup concept-scope** (`efd7325`): `_in_course_entities` scopes by `search_space_id` AND `concept_id` AND `id NOT IN exclude_entity_ids`, at both slug and embedding tiers; same-mint exclusion threaded via `resolved_ids`.
  - **Acyclicity guard** (`24529f9`/`0490cca`): `tag_mint.py` `_acyclic_prereq_pairs` — resolves keys to ids, drops self-loops and edges whose target already reaches source (incremental DFS), drop-and-log, never raises.
  - **Concept-scope prereq partition** (`99f0bb9`): `partition_prereqs_by_concept_scope` requires both endpoints owned by the mint concept; applied before the acyclicity guard AND re-applied at the `insert_prereqs` writer boundary.
  - **Solution provenance** (`f7cbf7c`): keyword-only `solution_source` threaded through promote (`='extracted'`), idempotent, preserves a pre-stamped source.
  - **KG teardown** (`c6333f0`): `delete_authored_set` tears down only fully-orphaned concepts; `_protected_concepts` spares any concept with a session / learner_state / mastery_event / `apollo_misconceptions` row / inbound cross-concept prereq; `_concepts_with_canon_history` guards `:Canon`; PG delete then post-commit Neo4j `DETACH DELETE`.
  - **Per-candidate error isolation** (`d469748`): orchestrator catches per-candidate `TagMintError` → `rejected` result; `CanonProjectionError` intentionally propagates.
- Owner doc `docs/architecture/apollo.md` updated per commit; audit docs `docs/audits/2026-06-30-authored-set-fix-handoff.md` and `-dedup-false-merge.md` exist.

---

## 2. What has real test / probe evidence

- **Suite run on `staging`** (real Postgres via `db_session`, Neo4j faked/None): `pytest apollo tests -k "authored or nli or mint or dedup or teardown or label_match or ocr"` → **261 passed, 14 skipped** (pre-existing unrelated legacy V2), 0 failed. (Runner's own result; the DB tests did NOT run in the *separate* review sandbox.)
- **NLI, independently re-run in review sandbox:** `test_polarity.py` + `test_nli_config.py` + `test_done_grading_nli.py` → **27 passed**; `apollo/resolution/tests -k 'nli or resolution or semantic or embedding'` → **201 passed, 1 skipped**. Confirms recall-only fallback, misconception veto, litotes window, per-model registry, default-ON/kill-switch context build (all with a **Fake adjudicator**).
- **NLI threshold tuning is evidence-backed on authored data:** `docs/_archive/experiments/2026-07-01-nli-threshold-tuning/RESULTS.md` — large recall flat 0.714 @ min_ent 0.50–0.90, `resolve_attempt` gate PASS (0 false credits) both models. **Synthetic dev data only** (104 ref pairs + 24 veto records).
- **Dedup ladder** (`test_dedup.py`, ~30 tests, real db_session): cross-concept slug/embedding isolation, `exclude_entity_ids` same-mint exclusion, band boundary 0.82, tie-break determinism — covers the audited bugs AND their fixes.
- **Acyclicity** (`test_tag_mint.py`): 6 pure-function cases (self-loop, 2/3-cycle, DAG, diamond, merge-collapsed self-loop) — **re-run in sandbox, all pass**; plus integration tests for cross-concept-drop-before-acyclicity ordering and `dropped_prereq_pairs` reasons.
- **Teardown** (`test_authored_api.py`, ~35 tests incl. 13 delete): orphan teardown + 6 `spares_concept_with_*` guards; Postgres cascade real, Neo4j via hand-written `_FakeNeo`.
- **Indexing re-upload** (`test_authored_indexing.py`, real `prepare_for_indexing` + db_session): `test_reupload_reuses_content_identical_doc_via_real_prepare` — the specific regression a stubbed version had masked.
- **Paired retrieval** (`test_authored_paired_retrieval.py`, real db_session): `test_retrieval_branch_is_solution_document_scoped` proves single-document scoping.
- **Solution provenance** (`test_promote.py`): threaded value persisted, default applied, pre-stamped source preserved.
- **`_make_metered_chat` Decimal seed fix** (`9ba41f9`): real regression test `test_make_metered_chat_seeds_decimal_cost_not_float`.

---

## 3. Untested or only claimed (docs claiming more than code/tests support)

- **aas-kg (reference-graph-gen) is the weakest-tested surface — weight accordingly.** The real KG-write path is under-exercised:
  - **`orchestrator.py` `project_canon` / real Neo4j is NEVER exercised** — `test_authored_set_orchestrator.py` passes `neo=None` in every test. Teardown's Neo4j `DETACH DELETE` is only proven against a hand-written `_FakeNeo` — a Cypher syntax error or driver call-shape mismatch would not be caught by any test.
  - **Partial mid-mint commit on `TagMintError` is untested** — the one isolation test (`test_candidate_tag_mint_error_is_rejected`) mocks `tag_and_mint` to raise *before any DB write*, so the partial-write path never executes. **Verified: no `begin_nested`/savepoint/rollback anywhere under `apollo/provisioning/authored_sets/` or `tag_mint.py`.**
  - **Cross-mint prereq cycle is untested** — `_acyclic_prereq_pairs` starts `adj` empty every call and never loads persisted `apollo_entity_prereqs`; no test seeds pre-existing edges.
- **NLI default-ON path is NOT exercised end-to-end.** Both `tests/conftest.py` and `apollo/conftest.py` autouse-force `APOLLO_NLI_ENABLED=0`, so every quoted full-suite pass count (1911/1915/1927/261) is an **NLI-OFF run**. NLI-ON is covered only by targeted unit tests with a **Fake** adjudicator.
- **The real transformer model is never loaded or run by any test.** `done_grading.py:_build_adjudicator` is `# pragma: no cover`; `TransformersNLIAdjudicator.classify` (real forward pass, lazy HF download) is untested. **A stream review explicitly flagged that the doc-reader summary's claim of `run_in_executor` offload is FALSE — verified: no `to_thread`/`run_in_executor` in `done_grading.py`.**
- **No executed assertion proves `_nli_context()` reaches `resolve_attempt(nli_ctx=...)`** in a live `run_graph_simulation` — `test_done_grading_unit.py` mocks `resolve_attempt` wholesale. Wiring confirmed only by code reading.
- **`requirements-nli.txt` header is stale/contradictory (drift-contract violation).** **Verified:** its header still says the deps are "kept out of the main requirements.txt … while the tier is default-OFF / dormant," but `cc942f0` moved them INTO `requirements.txt` and flipped the tier default-ON.
- **Production middle-band dedup judge is hardwired to always return `'distinct'`** (documented in `docs/audits/2026-06-30-authored-set-dedup-false-merge.md`). `test_dedup.py` proves the merge branch works *when a `judge_fn` says merge*, but nothing enables that branch in deployed code — it is **dead in production** regardless of coverage.
- **Vision-OCR dedicated coverage is thin** (~5 tests across `test_openai_vision_ocr.py` + legacy `test_ocr_mathpix.py`) relative to the size of `2d7972e`; most assurance is indirect via verification/paired-retrieval OCR-confidence tests.
- **All threshold/veto tuning is synthetic** — zero live/production student-attempt validation. The spec's own gating prerequisite (14 live macro/fluids acceptance probe via `scripts/run_macro_probe.py`, checking unresolved_rate delta + no grade-math regression) **does not appear to have been run before the default-ON flip.**
- **WU-AAS handoff's deferred empirical validation is NOT done** — re-provisioning `apollo_authored_sets id=4` after teardown+redeploy and re-running the audit SQL/Cypher (zero cross-concept edges, zero cycles, no false merges, `solution_source='extracted'` on all 7, `:Canon` count match) is explicitly open. Deployment (vs. merge) of the WU-AAS PRs to staging is not confirmed in reviewed code.
- **PR5** (OCR/extraction-quality normalization — unicode minus, forced math-page cross-check) is **not started** — accepted, tracked gap.

---

## 4. Findings, ranked by severity (deduped, evidence-backed only)

### CRITICAL
**C1 — `approve_held_problem` IDOR / cross-tenant authz bypass** · `apollo/provisioning/authored_sets/api.py:527-579`
**Verified in code.** The endpoint checks `require_course_member` against `authored_set.search_space_id` (from the URL `set_id`), then does `row = await db.get(ConceptProblem, problem_id)` **with zero cross-check that `problem_id` belongs to that set or search space** (confirmed: lines 545-548 fetch by bare `problem_id`; the only validation is `authored_review.required`). A member of course A can call `POST /authored-sets/{A-set-id}/problems/{B-problem-id}/approve`, pass the course-A membership check, then `build_approved_pair(..., search_space_id=A)` / `promote(..., search_space_id=A)` — minting/promoting **course B's held problem under course A's scope**. No test constructs a cross-set/cross-space `problem_id`.

### HIGH
**H1 — No teacher-role gate; any enrolled student can create/delete/approve authored sets** · `apollo/provisioning/authored_sets/api.py` (all endpoints), `apollo/auth_deps.py:49`
**Verified.** Module docstring says "Teacher-gated," but every endpoint calls `require_course_member(...)` — which calls `has_membership(...)` **with no `role` argument** (confirmed: `require_course_member` has no `role` parameter at all). The repo's teacher-only precedent (`server.py` passes `role="teacher"` at ~10 sites) is not used. With `AUTO_ENROLL_STUDENT_MEMBERSHIP`, a self-enrolled student can upload arbitrary PDFs as authored pairs, delete any set, or approve/promote held problems. No role-denial test exists.

**H2 — `DELETE` during in-flight provisioning orphans Neo4j `:Canon` nodes** · `apollo/provisioning/authored_sets/api.py` (delete + `_run_set_background`), `promote.py::project_canon`
`_run_set_background` holds one long-lived session/transaction, committing only at the end. `promote()`→`project_canon` writes `:Canon` to Neo4j per-candidate, outside that PG transaction. DELETE is allowed "in ANY status." A concurrent delete removes the `AuthoredSet` + `AITADocument` rows; the background task's next `db.get(AuthoredSet)` returns `None` → silent `return` → session close rolls back uncommitted PG writes, but **Neo4j `:Canon` nodes already written are not rolled back** and become permanently invisible to teardown (which walks `ConceptProblem` rows only). No test exercises delete-during-provisioning. (Mechanism plausible from code; not reproduced live.)

### MEDIUM
**M1 — Partial mid-mint KG writes committed on `TagMintError` (no savepoint)** · `orchestrator.py` `_process_authored_candidate`, `tag_mint.py`
`tag_and_mint` flushes concept/entities/dedup rows before the fail-closed raise at step 5a/5b. The caught `TagMintError`→`rejected` lets the loop continue, and `_run_set_background` commits the whole session → tagged concept + KGEntities + `apollo_dedup_decisions` persist with **no promoted `ConceptProblem`**, unreachable by teardown → permanent KG litter that becomes a live dedup target. **Verified: no `begin_nested`/savepoint anywhere in the module.** Untested (isolation test raises before any write).

**M2 — Cross-mint prereq cycle can be persisted into a shared concept** · `tag_mint.py:254` `_acyclic_prereq_pairs`
Guard starts `adj` empty and never loads existing `apollo_entity_prereqs`. Two problems tagging the same slug → same `concept_id`, share entities via dedup. Mint 1 persists `A→B`; Mint 2 drafts `B→A` — both pass concept-scope + empty-adjacency acyclicity, and `insert_prereqs` only SELECT-skips the exact pair, not the reverse → 2-cycle `A↔B`. The code comment claims dedup handles cross-run cycles, but dedup's job is *merging*, not cycle prevention — the stated mitigation does not close the gap. Untested.

**M3 — Grading-path NLI blocks the event loop; no node cap; lazy in-request model download** · `apollo/handlers/done_grading.py`
`run_graph_simulation`→`resolve_attempt`→`match_nli_semantic`→`TransformersNLIAdjudicator.classify` is fully **synchronous CPU inference**, and `_load()` downloads the HF model on first call — inside the async request handler, with **no** `APOLLO_NLI_CHAT_MAX_NODES`-style cap (that cap exists only on the chat path). **Verified: no `to_thread`/`run_in_executor` in the file** (contradicts the doc-reader's "executor offload" claim). Contingent on `APOLLO_GRAPH_SIM_SHADOW_ENABLED` (off in prod) — bites when shadow-grading is turned on.

**M4 — Missing OCR confidence (`None`) is treated as high-confidence, silently skipping generate-and-compare** · `apollo/provisioning/authored_sets/verification.py:73`
`low_confidence = (min_conf is not None and min_conf < conf_threshold) or problem_low_conf`. If a chunk's page has no `page_debug` entry (digital-extraction page, or page-numbering mismatch), `min_conf` is `None` → OCR text trusted with **no confidence signal at all**. `test_high_confidence_skips_verification` only exercises `min_conf=0.95`, never `None`; no doc acknowledges the choice.

**M5 — Migration number `032` collides** · `database/migrations/032_apollo_authored_sets.sql` vs `032_apollo_clarifications.sql`
**Verified both present on `staging` HEAD.** No automated numbered runner, so not a runtime break, but it breaks the numbering invariant the team relies on (workspace memory already flags a prior "023 collision" as a recurring pattern).

### LOW (evidence-backed; batch-fix)
- **L1 — Teardown can delete an entities-only shared/seed concept** (`api.py` `_protected_concepts`): auto-minted misconceptions are stored as `KGEntity kind='misconception'`, NOT in the `apollo_misconceptions` table the guard checks; a slug-bound concept with entities only, no `ConceptProblem`, no student footprint, no `:Canon` history passes all guards → cascade-deleted. Narrow.
- **L2 — Residual same-concept cross-problem false merge** (`dedup.py`): `exclude_entity_ids` only excludes same-mint entities; two different problems minting the same thin-payload `variable_mapping` entity into one concept can still cosine-merge. Documented as accepted residual.
- **L3 — Content-hash re-upload fallback not scoped by `role`** (`indexing.py:187-212`): a problem and solution PDF with identical extracted content in one search space could cross-match to the wrong role's doc. Low likelihood; untested.
- **L4 — Grading NLI has no graceful degradation on missing `transformers`** (`done_grading.py:90-93`): a hard import inside the step-5 try is caught by the broad `except Exception` → sets `learner_update_pending=True` and re-raises every run → re-arms retry indefinitely. Chat path degrades; grading doesn't. Reachable only with shadow-grading on.
- **L5 — `requirements-nli.txt` header stale/contradictory** — drift-contract violation. **Verified.**
- **L6 — Affirmative-label dependency unguarded** (`candidates.py:110`): an NLI-eligible reference step authored without `content.label` falls back to `display_name = canonical_key` (snake_case), which can't pass the overlap floor → NLI silently does nothing. Safe direction (false negative). Current 10-file corpus is 28/28 labeled; no `problem_validator` enforces it for future WU-AAS/teacher problems.
- **L7 — Antonym-pole polarity guard assumes a shared quantity it never verifies** (`polarity.py:144-150`): student "velocity higher" vs reference "pressure lower" fires `direction_mismatch`. Safe direction (blocks match), real false-negative source.
- **L8 — `create_authored_set` `set_index` race** (`api.py:74-82`): `SELECT MAX` then INSERT with no lock; concurrent double-submit → `IntegrityError` surfaced as unhandled 500. Low likelihood (teacher UI).
- **L9 — No watchdog/retry for stuck runs**: provisioning runs via in-process FastAPI `BackgroundTasks`, not the durable worker; a web-dyno redeploy kills it mid-run. Only shipped remedy is the (destructive, and per H2 unsafe mid-run) DELETE — clearing ≠ recovering.

---

## 5. Prioritized next actions

### MUST fix/test before promotion to `ApolloV3`
1. **Fix C1 (IDOR).** In `approve_held_problem`, verify the fetched `ConceptProblem` belongs to `set_id`/`authored_set.search_space_id` (cross-check against `result_summary['problems'][*].concept_problem_id`, or add set/space provenance to `ConceptProblem`) before minting. **Add a cross-set/cross-space test that expects 404/403.** This is a live cross-tenant bypass regardless of the shadow-grading flags.
2. **Decide H1 explicitly.** Add a teacher-role gate (`has_membership(..., role="teacher")` / a `require_course_teacher` dependency) to all authored-sets endpoints, OR formally document + accept student-accessible authoring. Add a role-denial test. Given the docstring, gating is almost certainly intended.
3. **Fix M1 (partial-commit).** Wrap the per-candidate `tag_and_mint` in `db.begin_nested()` and roll back on `TagMintError` (mirror `enqueue.py` / the clarification-loop PR). Add a test using the **real** `tag_and_mint` raising at step 5a/5b, asserting zero orphan Concept/KGEntity/DedupDecision rows.
4. **Serialize H2 (delete-during-provisioning)** — refuse DELETE while `status in ('indexing','provisioning')` unless forced (and if forced, cancel/await the task first), OR document the orphaned-`:Canon` risk as a known limitation. Add a delete-during-provisioning test.
5. **Run the DB-backed suite under Docker/testcontainers and capture `diff-cover --fail-under=95`.** The dedup-scope, endpoint-scope, DB-backed acyclicity, and all 13 teardown tests did **not execute** in the review sandbox; the 95% patch gate (CLAUDE.md contract) is unverified for this diff outside CI.
6. **Exercise the real KG-write path once (aas-kg is the weak spot).** Add at least one orchestrator/teardown test against a real (or Testcontainers) Neo4j so a Cypher/driver-shape error in `project_canon` / `DETACH DELETE` is caught — currently 100% faked/`None`.
7. **Do NOT rely on default-ON NLI being validated.** Before enabling either consumer flag (`APOLLO_GRAPH_SIM_SHADOW_ENABLED` / `APOLLO_CLARIFICATION_ENABLED`) on `ApolloV3`: fix M3 (offload `classify` to a thread + add a grading node-budget cap + pre-warm the model at startup) and M4 (treat `min_conf=None` deliberately). NLI default-ON with the flags off is safe today, but the flip has **only synthetic-data evidence** and the real model path is 100% untested.
8. **Execute the deferred WU-AAS empirical validation** — re-provision authored set `id=4` on staging and run the audit SQL/Cypher (zero cross-concept edges, zero cycles, no false merges, `solution_source='extracted'` on all 7, `:Canon` count match). This is the only end-to-end signal for the fix wave.
9. **Add the one missing wiring assertion**: a test that runs `run_graph_simulation` with NLI ON (Fake adjudicator) and asserts `_nli_context()` actually reaches `resolve_attempt(nli_ctx=...)`.

### Nice to have (post-promotion / batch)
- **M2 (cross-mint cycle):** seed `adj` in `_acyclic_prereq_pairs` from persisted `apollo_entity_prereqs`, or re-check acyclicity inside `insert_prereqs`; add a persist-`A→B`-then-draft-`B→A` test. (Two-shared-slug prerequisite makes it lower-probability than M1.)
- **M5:** rename one `032_*.sql` to `033_…`; verify no third `032` on pending branches.
- **L1:** add an entities-only shared/seed-concept guard (check `KGEntity kind='misconception'` in `_protected_concepts`).
- **L3:** scope the `content_hash` fallback query by `authored_role`; test varying role at constant content.
- **L4:** wrap the grading-path adjudicator import in graceful degradation (log + skip NLI).
- **L5:** fix the `requirements-nli.txt` header (same-commit drift reconcile).
- **L6:** add a `problem_validator`/authoring lint requiring an affirmative `content.label` on NLI-eligible reference steps.
- **L8:** add a unique-violation retry or advisory lock to `_next_set_index`.
- **L9:** add a watchdog for `AuthoredSet` stuck in `indexing`/`provisioning`; move provisioning to the durable worker instead of in-process `BackgroundTasks`.
- **Deprioritized/accepted:** PR5 (OCR extraction-quality normalization, unicode-minus, forced math-page cross-check); the production middle-band dedup judge is dead-`'distinct'` (documented) — decide whether to wire the real judge or leave it.

---

**Bottom line:** The WU-AAS fix wave and the NLI tuning are substantive and well-tested *at the unit/logic level with real Postgres*. But three things break the "ready" narrative: (1) a **live cross-tenant IDOR + a missing teacher gate** on the authored-sets API that exist independent of any feature flag; (2) the **real KG-write and real-model paths are untested** — every orchestrator/teardown test fakes Neo4j and every full-suite count is NLI-OFF, so aas-kg and NLI default-ON rest on code-reading, not executed assertions; and (3) the DB-backed 95% patch gate was **not re-run** in this review and the handoff's own empirical validation (re-provision id=4, macro/fluids probe) is **still open**. Fix C1/H1/M1/H2 and produce the CI + live-probe evidence before any `staging → ApolloV3` promotion.