# WU-6A split proposal (build-ready) — the SESSION PERSONALIZATION WEDGE

**Date:** 2026-06-19
**Author:** scoping pass (autonomous stacked-PR build)
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §1 lines 99-113 (the three levers + guardrails), §1.4 (per-classroom isolation invariant), §6 Readouts line 470 (the Q1 personalization read), §8 lines 1065-1066 (selection-as-a-join — FUTURE-state, see §1), §12 line 1538.
**Status:** PROPOSAL — REVISED 2026-06-19 after three-critic review (FEASIBILITY / SPEC-FIDELITY / OPS-UX, all three verdict ACCEPT). Every critic finding re-verified against the actual code in this pass and folded in; the resolutions are recorded inline and summarized in §11 (Critic-finding ledger). No code/tests/source touched. Every load-bearing anchor RE-VERIFIED against the actual code (file:line). The make-or-break N+1 question is EMPIRICALLY SOLVED (round-trip reproduced, not asserted). The cold-start / flag-OFF non-regression proof is pinned to today's exact `candidates[0]` branch.
**Templates:** `docs/superpowers/plans/2026-06-18-apollo-kg-wu5a-split-proposal.md`, `…wu5b-split-proposal.md`, `…wu4c-split-proposal.md`.
**Stacks on:** `feat/apollo-kg-wu5b5-chat-keyword-wireup` (#46, tip `3b0ec52`) — the first sub-unit's diff-cover compare branch; the rest stack.
**Backbone note:** WU-6A is the v1 product wedge the whole Apollo KG learner-model build exists to serve. It is a READ + SELECTION + PERSONA change. It needs NO migration (CONFIRMED, §1). It ships behind `APOLLO_SESSION_PERSONALIZATION_ENABLED` (default OFF) AND is a no-op when the learner-state table is empty even with the flag ON (the prod path — §1 GATING TRUTH).

---

## VERDICT (TL;DR)

**Split WU-6A into FOUR distinct seams — but SHIP THREE for v1 and DEFER the fourth (persona).** The orchestrator's strong-candidate seam was THREE (6A1 read / 6A2 pure-selection / 6A3 wiring), matching 4A/4B/4C/5A/5B. Adversarial evaluation (all three critics concur) says the proposed **6A3 "live wiring" must split in two**: selection-wiring and persona-wiring are independent on every axis (different handlers, different functions, different trigger frequency, different test substrate, different risk surface — persona carries the LEAKAGE_POLICY contract AND has NO runtime leakage gate). The 6A1↔6A2 IO-read / pure-core seam SURVIVES (do not fuse: different test substrates). Because the persona sub-unit fires ~never in v1 (empty `opposes_map` → `misconception_code` ~never set), the v1 RELEASE is **6A1 → 6A2 → 6A3** with **6A4 DEFERRED** to a follow-up WU. The full four-seam split:

- **WU-6A1** — the Q1 READ path: `read_learner_profile(...)` over `apollo_learner_state` + `apollo_entity_prereqs`, course-scoped, empty-profile cold-start. Real-PG Testcontainers. NO migration.
- **WU-6A2** — the pure SELECTION + COVERAGE + DIFFICULTY algorithm. Pure golden-vector. Cold-start → today's `candidates[0]` as a pure branch.
- **WU-6A3** — SELECTION wiring: thread 6A1→6A2 into the TWO `select_problem` call-sites (`session_init.py:58` AND `next.py:79`) behind the flag. Real-PG route test + flag-OFF byte-identical regression.
- **WU-6A4** — PERSONA wiring: optional `misconception_code` conditioning into `draft_reply`, behind the flag, null-tolerant, LEAKAGE_POLICY-bounded (more-confused-only). Persona test + leakage-corpus test + flag-OFF byte-identical regression. **STRONGLY RECOMMENDED TO DEFER** to a follow-up WU (§OPEN-DECISIONS #8): it is a near-total no-op in v1 (the `opposes_map` is structurally empty, so `misconception_code` is ~never set — `done_grading.py:135` hard-codes `"opposes": None`), AND — corrected this revision — the live per-turn chat path has **NO runtime leakage gate** (chat.py:289 calls `draft_reply` directly; `test_chat_no_signals.py:31,42` actively forbids re-introducing `validate_or_raise`/`infer_misconception`), so persona conditioning would be the first v1 code to add non-student-derived content to the prompt with only an OFFLINE corpus test as a backstop. Shipping 6A1/6A2/6A3 (the wedge that actually fires) as v1 is the smaller-but-higher-leverage release.

The whole wedge is NOT small enough to be one unit (a new PG read module + a pure scorer + two selection call-sites + a persona change behind a flag with a leakage contract — exactly the multi-substrate shape the established seam decomposes). Build order: **6A1 → 6A2 → 6A3** as the v1 wedge, with **6A4 DEFERRED** (or, if the orchestrator approves it for v1, built fourth and parallelizable off 6A1 since it only needs 6A1's `misconception_code` field — never fused into 6A3).

---

## 0. What WU-6A is (and the boundaries it must not cross)

WU-6A reads Layer 3 at Apollo SESSION START and uses it to (a) SELECT the problem whose reference graph best covers the student's WEAK entities, (b) CONDITION the confused-AI persona on an active misconception flag, (c) TUNE difficulty; low confidence → re-probe (spec §1 lines 99-103, §6 line 470). The "Q1 personalization read" = weakest entities + misconception flags + confidence for `(student, concept)` at session init, from `apollo_learner_state`. Problem selection prefers entities in the TEACHABLE-EDGE band (mastery 0.3–0.7) whose PREREQUISITES are mastered (`apollo_entity_prereqs`, in-memory).

**Guardrails (spec §1 lines 111-113, HARD BINDING):** interpretable BKT-family-or-simpler, hand-set params, **NO neural KT, NO new infrastructure, one developer.** Every scoring rule in WU-6A2 is stdlib `set`/`dict`/`sorted` arithmetic. **NO new package** (see §OPEN-DECISIONS #7).

**Per-classroom isolation INVARIANT (spec §1.4):** mastery/entities/problems NEVER cross courses — every read scoped by `search_space_id`. WU-6A1 enforces this with the `search_space_id` predicate on `apollo_learner_state` AND the `concept_id → apollo_concepts → search_space_id` chain on `apollo_kg_entities`.

### What WU-6A does NOT own (frozen upstream — DO NOT modify)
- `apollo/learner_model/*` belief/update/state_model/decay/negotiation/persistence (WU-5A/5B) — the WRITE path. WU-6A is the FIRST READ path; it reuses the FROZEN stored columns, not the belief recompute.
- `apollo/handlers/learner_update.py` + `done.py` + `done_grading.py` — the write path.
- migration 026 schema (`apollo_learner_state` / `apollo_entity_prereqs` / `apollo_kg_entities`).
- `apollo/persistence/learner_model_seed.py` — frozen WU-3B. WU-6A2 IMPORTS the pure, DB-free `_ENTRY_TYPE_TO_KIND_PREFIX` map (it imports, never mutates — see §1).
- `apollo/overseer/problem_selector.py::select_problem` SIGNATURE — it has other callers (read-back). WU-6A3 ADDS a personalized path; it leaves `select_problem` as the cold-start / flag-OFF fallback (see §4 decision #2).

---

## 1. Ground truth discovered (load-bearing facts, file:line — RE-VERIFIED)

Every orchestrator anchor re-checked; three load-bearing facts that SHAPE the split are flagged ⚠.

- **SELECTION SEAM — `select_problem` returns `candidates[0]`** (`apollo/overseer/problem_selector.py:54-59`):
  `pool = await list_problems_for_concept(db, concept_id=concept_id)` (`:54`); `candidates = [p for p in pool if p.difficulty == difficulty and p.id not in attempted]` (`:56`); `if not candidates: raise PoolExhaustedError(...)` (`:57-58`); `return candidates[0]` (`:59`). `list_problems_for_concept` (`:25-39`) is ONE query (`SELECT payload FROM apollo_concept_problems WHERE concept_id=:cid`) then `sorted(problems, key=lambda p: p.id)` (`:39`). **`candidates[0]` is exactly what WU-6A personalizes — replace "first not-attempted at difficulty" with "best covers the student's weak entities", same deterministic `Problem.id` sort as the tie-break.**

- ⚠ **TWO selection call-sites, not one** (the pre-recon named only `handle_next`):
  1. `apollo/hoot_bridge/session_init.py:58` — `init_session_from_hoot` picks the FIRST problem with `attempted_ids=[]`. **This is the literal "session start" the spec §1 means** — there is no separate INIT handler; sessions are born in TEACHING via the Hoot bridge. Has `user_id`, `search_space_id`, `concept_id` in scope (`session_init.py:39-46` params + the `infer_concept_id` result at `:53`).
  2. `apollo/handlers/next.py:79` — `handle_next` picks subsequent problems. Has `sess` loaded (`:37-41`, with `.with_for_update()`) → `sess.user_id`/`sess.search_space_id`/`sess.concept_id` + `attempted_ids` (`:69-77`).
  **BOTH must be personalized or the wedge does NOT fire at session start.** WU-6A3 wires both.

- **READ TARGET — `apollo_learner_state` stores the readouts as COLUMNS** (migration `026:120-133`; ORM `apollo/persistence/models.py:410-437`): PK `(user_id UUID, search_space_id INT, entity_id BIGINT)`; `mastery REAL CHECK 0..1` (`026:126`), `confidence REAL CHECK 0..1` (`026:127`), `misconception_code TEXT NULL` (`026:128`). **The Q1 read is a plain SELECT, NOT a belief recompute.** Confirmed: the ONLY existing read of this table in the whole `apollo/` tree is the write-path `_lock_prior_state` (`persistence.py:197`, `with_for_update()`). **WU-6A1 builds the genuinely first read path.**

- **belief-helper PARITY — read the columns directly, do NOT re-import `belief.py`** (`apollo/learner_model/belief.py:117-127`): `mastery_of(b) = 0.5·b[1] + b[2]`, `confidence_of(b) = 1 − entropy/log3`. These are the formulas the WRITE path persisted INTO the `mastery`/`confidence` columns. To recompute them the read would need the raw `belief REAL[3]` array — pointless. **WU-6A1 reads `LearnerState.mastery/confidence/misconception_code` columns verbatim.**

- **PREREQ GATE** (`apollo_entity_prereqs`, `026:107-110`; ORM `EntityPrereq` `models.py:392-407`): composite PK `(from_entity_id, to_entity_id)` BIGINT, "from depends on to". WU-6A1 reads the edges for the concept's entities (one scoped query) into an in-memory `{entity → [prereq entities]}` map; the prereq-mastered flag is computed in-memory against the same mastery map.

- ⚠ **MAKE-OR-BREAK — the per-problem reference-entity set IS available in-memory; NO N+1. EMPIRICALLY PROVEN.**
  - The parsed `Problem` schema **DROPS** the seeded entity links. `ReferenceStep` (`apollo/schemas/problem.py:40-45`) has fields `step, entry_type, id, content, depends_on` — **no `entity_key`, no `declared_paths`**; `Problem`/`ReferenceStep` declare NO `model_config`, so pydantic v2 defaults to `extra="ignore"`. (`done_inputs.py:43-49` documents this exact wall for the grading chain.)
  - **VERIFIED by round-trip** (`.venv/Scripts/python.exe`, 2026-06-19): `hasattr(parsed_step, "entity_key") == False`; `Problem.model_config.get("extra") is None`.
  - **BUT** each problem's canonical-key set is a PURE deterministic function of `(entry_type, id)` via the FROZEN `_ENTRY_TYPE_TO_KIND_PREFIX` map (`learner_model_seed.py:80-86`: `equation→eq`, `condition→cond`, `simplification→simp`, `procedure_step→proc`, `definition→def`) and `_entity_key_for_step` (`:196-199`: `f"{prefix}.{step['id']}"`) — the SAME rule the seeder minted into `apollo_kg_entities.canonical_key` via `reference_solution_to_entities` (`:202-232`).
  - **PROVEN identical** (round-trip on `problem_01`): reconstructed `{f"{prefix}.{s.id}" for s in parsed.reference_solution}` == seeded `{spec.canonical_key for spec in reference_solution_to_entities(raw)}` → `MATCH: True`. The set was `['cond.incompressibility','eq.bernoulli','eq.continuity','proc.plan_apply_continuity','proc.plan_apply_horizontal_simplification','proc.plan_solve_bernoulli_for_p2','simp.horizontal_simplification']`.
  - **Consequence:** WU-6A2 re-derives each candidate's reference-entity-key set IN-MEMORY from the already-loaded parsed `Problem.reference_solution` (the pool `list_problems_for_concept` already loaded in ONE query) via the frozen pure prefix map. **Zero per-problem DB or Neo4j round-trip. The N+1 hazard does not exist.** (`_ENTRY_TYPE_TO_KIND_PREFIX` is in a WU-3B module that is pure/DB-free/LLM-free — IMPORTED, never modified, so the frozen-upstream rule holds.)

- **PERSONA SEAM** (`apollo/agent/apollo_llm.py`): `APOLLO_SYSTEM_PROMPT` const (`:66-90`) is the confused-classmate prompt; `draft_reply` (`:93-160`) builds `messages = [{system: APOLLO_SYSTEM_PROMPT}, …problem…, …kg_summary…, …history_summary…]` then `messages.extend(history)` (`:118-138`). Call site is `apollo/handlers/chat.py:289` (`handle_chat`, per-turn) — **NOT `handle_next`.** `handle_next` never calls `draft_reply`. (This is the load-bearing reason persona-wiring is its own sub-unit — §2.)

- ⚠⚠ **THE LIVE CHAT PATH HAS NO RUNTIME LEAKAGE GATE — and the codebase actively FORBIDS re-introducing one** (verified `chat.py:280-294` + `test_chat_no_signals.py`, 2026-06-19 — folds in OPS HIGH #1, the most important correction this revision). `handle_chat` calls `draft_reply(...)` DIRECTLY (`chat.py:289`); the comment at `chat.py:280-283` states v1 is "nodify + dumb reply. No sufficiency, misconception, OLM-invite, or output filter… Apollo is fed only the student's own KG + the problem, so it cannot leak an un-taught concept (**structural** anti-leak replaces the deleted filter)." `apollo/handlers/tests/test_chat_no_signals.py` is a REGRESSION GUARD: `:31` asserts `infer_misconception` is absent from the chat module and `:42` asserts `"validate_or_raise(" not in src`. **There is NO `leakage_judge`/`validate_or_raise` in the runtime chat path, and adding one back would break a guard test.** Therefore the earlier draft's claim that a "leakage-judge regression" protects WU-6A4 at runtime is FALSE and is corrected throughout: WU-6A4's only safety net is (a) the STRUCTURAL more-confused-only polarity (it strictly reduces competence, cannot add knowledge) and (b) an OFFLINE leakage-CORPUS test (no live judge). Persona conditioning is the FIRST v1 change to add prompt content that is NOT derived from the student's own utterances — that is precisely why it carries elevated review weight and why §OPEN-DECISIONS #8 STRONGLY recommends deferring it. If 6A4 IS built, re-wiring `leakage_judge` into the conditioned path is an explicit contract change that MUST also update `test_chat_no_signals.py` — flag as part of #6/#8, never let an executor silently re-add it.

- ⚠ **PERSONA LEAKAGE CONTRACT** (`apollo/agent/LEAKAGE_POLICY.md` §"What Apollo MUST NOT do"): rule 1 forbids "Name a concept the student has not named"; rule 3 forbids "Paraphrase a concept by its canonical-form description." The `misconception_code` slug (e.g. `misc.pressure_velocity_same_direction`, `misconceptions.json`) maps to a misconception entity whose seeded `display_name`/`description` (`learner_model_seed.py:240-258`) describe the inverted/opposed concept. **Injecting the literal description = a rule-3 paraphrase leak.** Making Apollo MORE confused is directionally safe (it strictly REDUCES competence, never leaks knowledge), BUT the conditioning string must steer CONFUSION BEHAVIOR ("be especially unsure when reasoning about how two quantities relate"), never embed the misconception's description or a canonical concept name. This is a genuine, reviewer-heavy contract decision (§OPEN-DECISIONS #6) — strong evidence persona-wiring is its own unit.

- **FLAG IDIOM** (`apollo/overseer/misconception.py:499-505`): `is_enabled()` → `os.getenv("APOLLO_MISCONCEPTION_ENABLED","").lower() in {"1","true","yes","on"}`. **Mirror exactly** for a NEW `APOLLO_SESSION_PERSONALIZATION_ENABLED` (default OFF), in its own `is_enabled()` so call-sites don't duplicate the literal.

- **NO SELECTION OBSERVABILITY TODAY → 6A3 must add one structured log** (verified `problem_selector.py` has zero logging; `apollo_llm.py:144-152` is the structured-log template — folds in OPS MEDIUM #2). `select_problem`/`list_problems_for_concept` emit NO log of which `Problem` was chosen or why. In prod (cold-start) every selection silently degrades to `candidates[0]`; once LAYER3 is flipped, operators would have no signal that the wedge fired vs degraded — a silent-no-op that makes a flag-gated wedge un-auditable. `apollo_llm.py:144` shows the house pattern: `_LOG.info("apollo_draft_reply", extra={"event": "llm_call", "purpose": …, …})`. **WU-6A3 owns ONE such `_LOG.info` per personalized selection** (see §3 WU-6A3, observability item) carrying `{event, personalization_enabled, profile_is_empty, n_weak_entities, chosen problem_id, top coverage_score, fallback_fired}`. This is ~3 lines and is the only observable signal that the wedge engaged; it is an explicit acceptance item, not an afterthought.

- ⚠ **THE GATING TRUTH (the cold-start invariant — every design must handle it).** `apollo_learner_state` is populated ONLY when `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is ON (`done.py:99 _graph_sim_layer3_enabled()`, gating `done.py:415`), which is OFF in prod and flipped only after a human calibration review (NEVER in this build). **So IN PROD THE LEARNER-STATE TABLE IS EMPTY → cold-start is the PROD path, not an edge case.** Even with LAYER3 on, the runtime `opposes_map` is structurally empty (carried heads-up from WU-5A2/WU-4C1: §6.5 conflict/corrected detection is inert) → `misconception_code` is RARELY set. **WU-6A persona conditioning MUST tolerate `misconception_code IS NULL` (the common case); WU-6A selection MUST cold-start to today's `candidates[0]` exactly.**

- **SCOPING KEYS — all on the session row, no extra query.** `ApolloSession` carries `user_id` (`models.py:205`), `search_space_id` (`:206`), `concept_id` (`:217`), `current_problem_id` (`:225`). Both `handle_next` (`sess` loaded, `next.py:81` passes `sess.concept_id`) and `init_session_from_hoot` (params + inferred `concept_id` at `:53`, `:58`) already have the three scoping keys. WU-6A1 keys everything off the session's **integer** `concept_id` (never `Problem.concept_id`, which is the **slug** — `Problem.concept_id` is `str`, `schemas/problem.py:50`). The bridge between profile (`entity_id`/`canonical_key`) and candidate entity sets (`canonical_key`) is the `canonical_key` STRING.

- ⚠ **THE POOL IS FILED BY DIRECTORY, NOT BY THE JSON's `concept_id` SLUG** (verified by reading all 5 seed files + the seeder, 2026-06-19 — folds in SPEC-FIDELITY MEDIUM #1). `list_problems_for_concept` filters by `ConceptProblem.concept_id` (the INTEGER, `problem_selector.py:32`). The seeder (`scripts/seed_apollo_concept_registry.py:164-185, 198-216`) assigns that integer from `concept_dir.name` (the DIRECTORY, e.g. `bernoulli_principle`) to EVERY `problem_*.json` in that directory via `_upsert_concept` → `_upsert_problem(concept_id=<int>)`, IGNORING each JSON's own `concept_id` field. **Verified divergence:** `problem_03.json` carries `concept_id="continuity_equation"` and `problem_04.json` carries `concept_id="volumetric_flow_rate"` in their payloads, yet both sit in the `bernoulli_principle/problems/` directory → both load when `select_problem(concept_id=<bernoulli int>)` runs, and `Problem.model_validate(payload)` then reads `Problem.concept_id` back as the divergent SLUG. **Consequence for 6A2 golden vectors:** the candidate pool is the set of `ConceptProblem` rows sharing the bernoulli INTEGER `concept_id` (P1–P5), so P3/P4 ARE in-pool — but a test that filters or asserts candidates by `Problem.concept_id` (the slug) would wrongly exclude P3/P4 and silently build a pool the runtime never produces. **6A2 fixtures MUST construct/seed via the integer `concept_id` and assert ONLY on `Problem.id`, never on `Problem.concept_id`.** The `reference_entity_keys` round-trip test must likewise use the seeded-payload path, not the JSON's `concept_id`, for P3/P4/P5.

- ⚠ **SEED DIFFICULTY DISTRIBUTION constrains the discriminating selection tests** (verified all 5 seed files, 2026-06-19 — folds in FEASIBILITY LOW #3). Of the 5 bernoulli-directory problems: **FOUR are `intro` (P1–P4), exactly ONE is `standard` (P5 = `bernoulli_full_find_p2`), ZERO are `hard`.** So any test that asserts personalization "differs from `candidates[0]`" can ONLY be exercised at `difficulty="intro"` (4 candidates). At `standard` there is a single candidate → personalization can never differ from `candidates[0]` (it degenerates to the one problem). At `hard` there is no candidate → `select_problem`/`personalize_selection` always raise `PoolExhaustedError`. **Every discriminating selection test (6A2 Examples A/B + the 6A3 seeded-weak route test) MUST pin `difficulty="intro"`;** a "differs at standard" assertion would be silently vacuous. (Coverage of the `standard`-single-candidate and `hard`-raises branches still needs one test each, but those assert degeneracy/raise, not difference.)

- **NO MIGRATION** — CONFIRMED. All three reads hit existing migration-026 tables. `next_free_migration = 030` stays UNUSED. The only migration question is the optional persona cache column on `ApolloSession` — recommended DEFERRED in favor of a per-turn read (§4 decision #5, §OPEN-DECISIONS #5).

- **OWNER DOC** `docs/architecture/apollo.md` already declares `owns: [apollo/**, apollo/learner_model/**]`, `last_verified: 2026-06-19`. WU-6A reconciles it same-commit per sub-unit and bumps `last_verified`.

- **TEST HARNESSES (exist, reuse):** `apollo/overseer/tests/test_problem_selector.py`, `apollo/handlers/tests/test_next.py` + `test_next_db.py` (real-PG), `apollo/agent/tests/test_apollo_llm.py` (the `@patch("apollo.agent.apollo_llm.OpenAI")` capture harness), `apollo/agent/tests/test_leakage_judge.py` + `test_leakage_corpus.py`, `apollo/handlers/tests/test_chat.py`.

---

## 2. Recommendation (the split — FOUR distinct seams; v1 ships THREE, the fourth DEFERRED)

**The seam is four-way; the v1 release is three.** Selection-wiring and persona-wiring are genuinely independent sub-units (table below), so the persona work is NEVER fused into selection-wiring. But the persona sub-unit (6A4) is DEFERRED out of the v1 wedge: it fires essentially never (empty `opposes_map`) and has no runtime leakage gate, so it carries the heaviest review weight for ~zero v1 payoff. v1 = 6A1 → 6A2 → 6A3.

### Why NOT one unit
The wedge touches: a NEW PG read module, a PURE scoring algorithm (golden-vector-testable), selection wiring at TWO call-sites in two files, AND persona wiring in `chat.py` + `apollo_llm.py` — four files across two handlers plus a new module, behind a flag, with a leakage contract. Too much for a single 95%-patch-covered unit with a clean diff-cover boundary. The 4A/4B/4C/5A/5B precedent (pure-core / IO-read / live-wiring) applies.

### Why split 6A1 (read) from 6A2 (pure) — do NOT fuse
Tempting (both small), but they have **different test substrates**. 6A1 (the Q1 read) needs **Testcontainers real-PG** to prove the column SELECT, the course-scoping (`search_space_id`), the prereq-edge emission, AND the cold-start empty-table return. 6A2 is **pure golden-vector** (no DB) — band filter, `prereqs_mastered` derivation, coverage scoring, deterministic tie-break, empty-profile→`candidates[0]` branch. Fusing forces the pure scoring tests to drag a container and muddies the diff-cover boundary. This is exactly the repo convention (belief.py pure vs persistence.py real-PG, WU-5A1/5A2). **Keep split.** The clean contract is a frozen DTO (`LearnerProfile`, raw edges/maps) that 6A1 produces and 6A2 consumes purely (one-way arrow; 6A1 imports nothing from 6A2).

### Why the selection-wiring (6A3) and persona-wiring (6A4) seams are distinct — and why 6A4 is DEFERRED
They are independent on every axis:

| Axis | Selection wiring (6A3) | Persona wiring (6A4) |
|---|---|---|
| Handler | `init_session_from_hoot` (`session_init.py:58`) + `handle_next` (`next.py:79`) | `handle_chat` (`chat.py:289`, per-turn) |
| Function patched | personalized selection over `select_problem` | `draft_reply` (`apollo_llm.py:93`) |
| Trigger frequency | once per problem advance | every chat turn |
| Reads from 6A1 | raw mastery + prereq_edges + id↔key maps (6A2 derives coverage) | `misconception_code` only |
| Risk surface | N+1 / raw-payload / two call-sites / tie-break stability / observability | **LEAKAGE_POLICY contract** (more-confused-only, no description leak) + **NO runtime gate** |
| Cold-start | `candidates[0]` | verbatim `APOLLO_SYSTEM_PROMPT` |
| Test shape | real-PG route test + flag-OFF byte-identical + observability log | persona-string + OFFLINE leakage-corpus (no live judge) + flag-OFF byte-identical |

Bundling them would mix a PG-route change with an LLM-prompt-contract change in one diff, hurting both review and the 95% patch gate. The persona change is leakage-sensitive (a reviewer-heavy, semantically delicate change touching the LLM prompt with only an OFFLINE corpus and NO runtime gate). **Splitting selection-wiring from persona-wiring is the higher-leverage cut than the 6A1/6A2 split the orchestrator already proposed** — and once split, persona-wiring is the clean thing to DEFER (it fires ~never in v1), shipping the selection wedge alone. All three critics (FEASIBILITY/SPEC-FIDELITY/OPS) independently reached both the distinct-seam and the defer-persona conclusions.

### Recommended final split (THREE shipped sub-units + a DEFERRED fourth)
1. **WU-6A1** — `read_learner_profile(...)` (new module under `apollo/learner_model/`). Real-PG. Empty-profile cold-start. NO migration. Compare branch: `feat/apollo-kg-wu5b5-chat-keyword-wireup`. Emits RAW per-entity mastery + prereq-edge data in `LearnerProfile` (it does NOT compute the prereq-mastered SEMANTIC — that number lives in 6A2; see §4 decision #4, revised).
2. **WU-6A2** — pure `personalize_selection(profile, pool, *, concept_id, difficulty, attempted_ids)` + the D1–D6 predicates (including `prereqs_mastered` derivation). Golden-vector. Cold-start → `candidates[0]` pure branch. **`concept_id` is in the signature** so the exhaustion path raises the byte-identical `PoolExhaustedError(concept_cluster_id=str(concept_id), …)` (corrected this revision — see §4 decision #9). Stacks on 6A1.
3. **WU-6A3** — selection wiring at BOTH call-sites behind the flag, plus one structured selection log (observability — corrected this revision). Real-PG route test + flag-OFF byte-identical. Stacks on 6A2. **This is the last unit of the v1 wedge.**
4. **WU-6A4 (DEFERRED — default)** — persona wiring behind the flag, null-tolerant, LEAKAGE_POLICY-bounded. Persona + leakage-CORPUS test (NOT a live judge — the chat path has none) + flag-OFF byte-identical. If approved for v1, stacks on 6A3 (or parallel off 6A1). Default recommendation: defer (§OPEN-DECISIONS #8).

---

## 3. Sub-units

### WU-6A1 — the Q1 read path (real-PG)

**One-line:** A NEW course-scoped read module that loads, per entity for `(user, course, concept)`, `{mastery, confidence, misconception_code, prereqs_mastered}` from `apollo_learner_state` + `apollo_entity_prereqs`, returning an EMPTY profile cleanly when the table has no rows (cold-start = the prod path).

**SEAM (owns vs 6A2):** WU-6A1 owns every IO byte of the read and the course-scoping. It does NOT score, rank, select, or condition any persona. It does NOT touch `select_problem`, `draft_reply`, or any flag. Handoff: a frozen `LearnerProfile` DTO that 6A2 consumes purely.

**Scope — files to create:**
- `apollo/learner_model/personalization_read.py` (NEW):
  - `@dataclass(frozen=True) class EntityProfile`: `entity_id: int`, `canonical_key: str`, `mastery: float`, `confidence: float`, `misconception_code: str | None`. **NOTE (revised — FEASIBILITY MEDIUM #2):** `EntityProfile` carries ONLY the RAW stored columns; it does **NOT** carry a `prereqs_mastered` bool. The prereq-mastered SEMANTIC (which uses `MASTERED_THRESHOLD` + the unseen-prereq rule) is a SCORING NUMBER and is computed PURELY in 6A2, not here — so "6A2 owns every number" stays true and the rule is golden-vector-testable instead of real-PG-only.
  - `@dataclass(frozen=True) class LearnerProfile`:
    - `by_canonical_key: Mapping[str, EntityProfile]` — the present-row entities (absent entities are simply absent → 6A2 treats absence as cold = mastery 0.50).
    - `prereq_edges: frozenset[tuple[int, int]]` — the raw `(from_entity_id, to_entity_id)` edges for this concept's entities (the `apollo_entity_prereqs` rows), emitted RAW for 6A2 to evaluate.
    - `entity_id_by_key: Mapping[str, int]` and `key_by_entity_id: Mapping[int, str]` — the `concept_id`→entity id↔canonical_key maps (from `apollo_kg_entities`), so 6A2 can resolve a prereq edge's endpoints to keys/mastery purely.
    - `is_empty: bool` (True when no `apollo_learner_state` rows for this scope — the cold-start / prod path).
  - `async def read_learner_profile(db, *, user_id: str, search_space_id: int, concept_id: int) -> LearnerProfile`:
    1. Load this concept's entities: `SELECT id, canonical_key FROM apollo_kg_entities WHERE concept_id = :concept_id`. (Course isolation via the `concept_id → apollo_concepts → search_space_id` chain; `concept_id` is the session's INTEGER id.) Build `entity_id_by_key` / `key_by_entity_id`.
    2. Load this learner's state rows, scoped: `SELECT entity_id, mastery, confidence, misconception_code FROM apollo_learner_state WHERE user_id = :user_id AND search_space_id = :search_space_id AND entity_id IN (:concept_entity_ids)`. (Belt-and-suspenders course scoping: BOTH `search_space_id` predicate AND the entity-id restriction to this concept.) **Empty result → `LearnerProfile(by_canonical_key={}, prereq_edges=…, …, is_empty=True)`** (the maps/edges may still be populated; `is_empty` reflects the learner_state row count only).
    3. Load prereq edges for the concept's entities: `SELECT from_entity_id, to_entity_id FROM apollo_entity_prereqs WHERE from_entity_id IN (:ids) OR to_entity_id IN (:ids)`. Store RAW as `prereq_edges`. **6A1 does NOT compute prereqs_mastered** (that is 6A2's pure derivation — §4 decision #4, revised).
  - `read_learner_profile` performs **at most 3 scoped queries, NONE per-problem and NONE per-loop.** Read ONCE per session at the selection call-site (and, for v1, once per turn at the persona call-site if 6A4 is built — §4 decision #5; see also the per-turn trip-wire in §OPEN-DECISIONS #5).

**Public API for 6A2/6A3/6A4:**
```python
@dataclass(frozen=True)
class EntityProfile:               # raw stored columns only — NO derived prereqs_mastered
    entity_id: int
    canonical_key: str
    mastery: float
    confidence: float
    misconception_code: str | None
@dataclass(frozen=True)
class LearnerProfile:
    by_canonical_key: Mapping[str, EntityProfile]
    prereq_edges: frozenset[tuple[int, int]]
    entity_id_by_key: Mapping[str, int]
    key_by_entity_id: Mapping[int, str]
    is_empty: bool
async def read_learner_profile(db, *, user_id, search_space_id, concept_id) -> LearnerProfile: ...
```
No constant is imported FROM 6A2 INTO 6A1 — the dependency arrow stays one-directional (6A1 → 6A2). 6A1 ships no scoring threshold.

**Dependencies:** `LearnerState`/`KGEntity`/`EntityPrereq` ORM (`apollo/persistence/models.py`). No belief recompute. No new external deps.
**Migration?** No (pure read over 026 tables).
**Real-infra tests?** **YES — real-PG (Testcontainers `tests/database`, `pytest.mark.integration`/`db_session`).** The SQLite unit `db` fixture lacks the 026 CHECKs and the FK chain — the course-scoping and cold-start assertions must run on real PG.

**Key tests (acceptance gates):**
- Cold-start: seed `apollo_kg_entities` + `apollo_entity_prereqs` for a concept, ZERO `apollo_learner_state` rows → `LearnerProfile.is_empty == True`, `by_canonical_key == {}` (but `prereq_edges` / the id↔key maps ARE populated from the seeded entities/edges).
- Course isolation: seed two courses (search_spaces) with learner_state rows for BOTH; read scoped to course A → NO course-B entity/mastery appears (the §1.4 invariant — assert on real PG).
- Column parity: seed a learner_state row with known `mastery=0.45, confidence=0.30, misconception_code='misc.x'` → the EntityProfile carries those exact column values (NOT a recomputed belief).
- Prereq-edge emission: seed prereq edges E→P; assert `(E_id, P_id) ∈ LearnerProfile.prereq_edges` and the id↔key maps resolve both endpoints. (6A1 emits the RAW edges; the prereqs_mastered DERIVATION is tested as a pure 6A2 golden vector, not here.)
- Mixed: a concept where some entities have rows and some do not → present entities carry their columns, absent entities are absent from `by_canonical_key` (6A2 treats absence as cold).

---

### WU-6A2 — the pure SELECTION + COVERAGE + DIFFICULTY algorithm

**One-line:** Given a `LearnerProfile`, the candidate `Problem` pool, the chosen `difficulty`, and `attempted_ids`, score each in-difficulty candidate by COVERAGE of the student's WEAK-and-teachable entities; deterministic tie-break to lowest `Problem.id`; cold-start / empty-weak-set → today's `candidates[0]`. PURE — no DB, no LLM, no Neo4j, no containers.

**SEAM (owns vs 6A1 / 6A3):** WU-6A2 owns every NUMBER (the weak-entity predicate, the prereq gate threshold, the coverage formula, the tie-break, the difficulty rule, the re-probe rule) and the cold-start branch. It takes in-memory inputs (`LearnerProfile` + `list[Problem]`) and returns a `Problem`. It does NOT read PG, does NOT know `sess`/flags/transactions, does NOT call `draft_reply`. It RE-DERIVES each candidate's entity-key set in-memory via the FROZEN `_ENTRY_TYPE_TO_KIND_PREFIX` (imported from `learner_model_seed`).

**Scope — files to create:**
- `apollo/learner_model/personalization_select.py` (NEW), NAMED CONSTANTS (6A2 owns EVERY number; nothing imports these back into 6A1):
  - `TEACHABLE_BAND_LO = 0.3`, `TEACHABLE_BAND_HI = 0.7` (spec §6 line 470 "mastery 0.3–0.7"; inclusive — §OPEN-DECISIONS #2).
  - `MASTERED_THRESHOLD = 0.7` (the prereq-mastered cut — reuse the band top; one fewer magic number — §OPEN-DECISIONS #3).
  - `UNSEEN_MASTERY = 0.50` (the cold-start readout for an entity with no `apollo_learner_state` row — matches `belief.mastery_of` cold-start, `belief.py:118-120`; an unseen prereq at 0.50 < 0.70 ⇒ NOT mastered ⇒ blocks — §OPEN-DECISIONS #3).
  - `REPROBE_CONFIDENCE = 0.4` (low-confidence re-probe threshold — §OPEN-DECISIONS #5b; in-v1-or-defer #5).
  - `reference_entity_keys(problem: Problem) -> frozenset[str]` — `{f"{_ENTRY_TYPE_TO_KIND_PREFIX[s.entry_type][1]}.{s.id}" for s in problem.reference_solution}`. (The PROVEN round-trip; imports the frozen map.)
  - `_mastery_of_key(profile, key) -> float` — `profile.by_canonical_key[key].mastery` if present else `UNSEEN_MASTERY`. (PURE; the single place the unseen-=-0.50 rule lives.)
  - `prereqs_mastered(profile, key) -> bool` — resolve `entity_id = profile.entity_id_by_key[key]`, walk `profile.prereq_edges` for edges `(entity_id, prereq_id)`, map each `prereq_id → prereq_key` via `profile.key_by_entity_id`, and return True iff EVERY prereq's `_mastery_of_key(...) ≥ MASTERED_THRESHOLD`. An entity with NO prereq edges is trivially `True`. **This is the SEMANTIC moved out of 6A1 (FEASIBILITY MEDIUM #2) — it is now a pure, golden-vector-testable function over 6A1's RAW edge/map data.**
  - `weak_teachable(profile) -> dict[str, float]` — `{canonical_key: deficit}` for entities with `TEACHABLE_BAND_LO ≤ mastery ≤ TEACHABLE_BAND_HI` AND `prereqs_mastered(profile, key)`, where `deficit = 1 − mastery`. (Only entities PRESENT in `by_canonical_key` can be weak — an absent entity's mastery is 0.50, in-band, but it has no signal, so it is excluded from the weak set; the cold-start profile has an empty `by_canonical_key` ⇒ empty weak set ⇒ `candidates[0]`.)
  - `coverage_score(problem, weak_deficits) -> float` — `sum(weak_deficits[k] for k in reference_entity_keys(problem) if k in weak_deficits)` (deficit-weighted greedy coverage — §OPEN-DECISIONS #4).
  - `personalize_selection(profile, pool, *, concept_id: int, difficulty, attempted_ids) -> Problem`:
    **`concept_id` is in the signature** (revised — FEASIBILITY MEDIUM #1) SOLELY so the exhaustion path reconstructs the byte-identical error; it is NOT used in scoring.
    1. `candidates = [p for p in pool if p.difficulty == difficulty and p.id not in set(attempted_ids)]` — **the EXACT today filter** (`problem_selector.py:56`), preserving the `sorted-by-Problem.id` order the pool arrives in.
    2. `if not candidates: raise PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)` — **byte-identical to today** (`problem_selector.py:58`, verified `errors.py:64` takes `concept_cluster_id` and embeds it in the message). The golden vector pins the exact `concept_cluster_id` string.
    3. `weak = weak_teachable(profile)`; **if `profile.is_empty` OR `not weak`: return `candidates[0]`** — the cold-start / no-weak-entity branch == today's exact behavior (the NON-REGRESSION proof).
    4. (re-probe, §4 decision #6 / §OPEN-DECISIONS #5) low-confidence weak entities are KEPT in `weak` (never hard-negatived).
    5. `scored = [(coverage_score(p, weak), p) for p in candidates]`; pick `max` by `(score, −problem_id_rank)` → equivalently: among the max-score candidates, the one with the LOWEST `Problem.id`. (Deterministic, matches today's stability.)

**WORKED NUMERIC EXAMPLES (the bernoulli seed `intro` set — REAL data, reproduced 2026-06-19).** All four examples are pinned to `difficulty="intro"` (the ONLY difficulty with >1 candidate — §1 seed-distribution fact). **Why P3/P4 are in the bernoulli pool even though their JSON `concept_id` is `continuity_equation`/`volumetric_flow_rate`:** the pool is filed by DIRECTORY, not by the JSON slug (§1 "POOL IS FILED BY DIRECTORY" — the seeder assigns the bernoulli INTEGER `concept_id` to every `problem_*.json` under `bernoulli_principle/problems/`). So `select_problem(concept_id=<bernoulli int>)` returns all 5; `Problem.concept_id` is read back as the divergent SLUG and must NEVER be used to filter the candidate set. The four `intro` candidates and their reconstructed entity-key sets:
- `P1 = bernoulli_horizontal_pipe_find_p2` → {cond.incompressibility, eq.bernoulli, eq.continuity, proc.plan_apply_continuity, proc.plan_apply_horizontal_simplification, proc.plan_solve_bernoulli_for_p2, simp.horizontal_simplification}
- `P2 = bernoulli_height_change_find_v2` → {eq.bernoulli, proc.plan_apply_equal_pressure_simplification, proc.plan_set_v1_zero_and_solve_bernoulli, simp.equal_pressure_simplification}
- `P3 = continuity_area_change_find_v2` → {cond.incompressibility, eq.continuity, proc.plan_invoke_incompressibility, proc.plan_solve_continuity_for_v2}
- `P4 = volumetric_flow_rate_find_Q` → {eq.flow_rate_definition, proc.plan_apply_flow_rate_definition}
(`Problem.id` sort order P-by-id; the pool arrives sorted.)

  **Example A — deficit-weighted coverage picks the right problem.**
  Profile weak-and-teachable: `eq.continuity` mastery 0.35 (deficit 0.65), `cond.incompressibility` mastery 0.40 (deficit 0.60), `eq.bernoulli` mastery 0.68 (deficit 0.32). Scores:
  - P1 covers all three → 0.65 + 0.60 + 0.32 = **1.57**
  - P3 covers {eq.continuity, cond.incompressibility} → 0.65 + 0.60 = **1.25**
  - P2 covers {eq.bernoulli} → **0.32**
  - P4 covers ∅ → **0.00**
  → **P1 selected.** Note deficit-weighting beats a plain count tie: P1 (count 3) still wins, but if P1 only covered `eq.bernoulli` (0.32) and P3 covered the two high-deficit entities (1.25), deficit-weighting prefers P3 (the higher-leverage weak coverage) even though both have low counts.

  **Example B — a TIE, broken deterministically by lowest `Problem.id`.**
  Profile weak-and-teachable: `eq.continuity` mastery 0.50 (deficit 0.50) ONLY (`cond.incompressibility` and everything else are either >0.7 or prereq-blocked). Scores:
  - P1 covers {eq.continuity} → **0.50**
  - P3 covers {eq.continuity} → **0.50**
  - P2, P4 cover ∅ → 0.00
  → P1 and P3 TIE at 0.50. **Tie-break: lowest `Problem.id`** → whichever of `bernoulli_horizontal_pipe_find_p2` / `continuity_area_change_find_v2` sorts first by `Problem.id` (the existing `sorted` key). This is the SAME stability rule as today, so when the weak set is empty the tie-break degenerates to `candidates[0]` (the non-regression anchor).

  **Example C — cold-start fallback (the prod path).** `profile.is_empty == True` (no learner_state rows) → `weak == {}` → `return candidates[0]` == `bernoulli_horizontal_pipe_find_p2` (or whatever `Problem.id`-sorted-first not-attempted `intro` problem) — **byte-identical to today's `select_problem`.**

  **Example D — partially-warm profile: unseen prereq BLOCKS, mastered prereq ADMITS (the unseen-prereq semantic, §OPEN-DECISIONS #3).** Suppose `eq.continuity` is in-band (mastery 0.40, deficit 0.60) and has one prereq edge `eq.continuity → cond.incompressibility`. Two sub-cases pinned as a single golden pair:
  - (D-blocked) `cond.incompressibility` has NO `apollo_learner_state` row → `_mastery_of_key = UNSEEN_MASTERY = 0.50 < 0.70` → `prereqs_mastered("eq.continuity") == False` → `eq.continuity` is EXCLUDED from `weak`. If it was the only in-band entity → `weak == {}` → `candidates[0]` (fallback).
  - (D-admitted) same profile but `cond.incompressibility` mastery = 0.80 ≥ 0.70 → `prereqs_mastered == True` → `eq.continuity` enters `weak` with deficit 0.60 → coverage scoring runs (P1/P3 cover it, P3 wins or ties per id). This pins the conservative "unseen blocks" choice visibly so the orchestrator can confirm/flip it (§OPEN-DECISIONS #3) without re-reading the code.

**DIFFICULTY RULE (v1 = CLAMP-WITHIN-CHOICE; §OPEN-DECISIONS #1).** Difficulty is a HARD STUDENT INPUT today (`body.difficulty` → `init_session_from_hoot`/`handle_next`, `Literal["intro","standard","hard"]`, `schemas/problem.py:37`). v1 does NOT override the student's choice: the coverage scoring runs ONLY over the in-difficulty candidate set (`p.difficulty == difficulty`, unchanged filter); only the ORDERING within that set changes. This keeps the flag-OFF regression trivially byte-identical and respects explicit UI input. (Auto-tune — map band position → difficulty — is a deferred follow-up.)

**LOW-CONFIDENCE RE-PROBE (v1 = soft hold; §OPEN-DECISIONS #5).** Per spec line 470 "Low confidence → re-probe" and §1's "keywords/signals are CLASS-LEVEL first, NEVER a hard per-student negative": a low-confidence weak entity (`confidence < REPROBE_CONFIDENCE`) is KEPT in the weak set (so the next problem re-exercises it) — it is never dropped or hard-negatived. v1 implements re-probe as "don't drop a low-confidence weak entity"; the stronger "prefer same-entity coverage" / "re-select the same problem" variants are an open decision.

**Public API for 6A3:**
```python
TEACHABLE_BAND_LO: float; TEACHABLE_BAND_HI: float; MASTERED_THRESHOLD: float
UNSEEN_MASTERY: float; REPROBE_CONFIDENCE: float
def reference_entity_keys(problem: Problem) -> frozenset[str]: ...
def prereqs_mastered(profile: LearnerProfile, canonical_key: str) -> bool: ...
def personalize_selection(profile: LearnerProfile, pool: list[Problem], *,
                          concept_id: int, difficulty: str, attempted_ids: Sequence[str]) -> Problem: ...
```

**Dependencies:** `apollo.schemas.problem.Problem`, `apollo.errors.PoolExhaustedError`, the IMPORTED frozen `_ENTRY_TYPE_TO_KIND_PREFIX` from `apollo.persistence.learner_model_seed`, 6A1's `LearnerProfile`. Stdlib only.
**Migration?** No. **Real-infra?** None — fully pure golden-vector.

**Key golden tests (acceptance gates):**
- Examples A/B/C/D above pinned as exact golden vectors (assert the selected `Problem.id`); ALL pinned to `difficulty="intro"` (the only multi-candidate difficulty — §1 seed-distribution).
- `reference_entity_keys` round-trip: assert it == `{spec.canonical_key for spec in reference_solution_to_entities(raw)}` for all 5 seed problems (the PROVEN identity, frozen as a test). **Construct each `Problem` from the SEEDED payload (integer-`concept_id` filing), never by filtering on `Problem.concept_id`** (the slug — §1 directory-filing fact), so P3/P4/P5 are exercised correctly.
- Empty-profile → `candidates[0]` IDENTICAL to today's `select_problem` output (run both with the SAME `concept_id`, assert same `Problem.id`) — the non-regression anchor.
- No-weak-entity (all present entities >0.7 or all prereq-blocked) → `candidates[0]`.
- `PoolExhaustedError` raised identically when no in-difficulty unattempted candidate remains — pin the EXACT `concept_cluster_id=str(concept_id)` string and `difficulty` in the assertion (FEASIBILITY MEDIUM #1; e.g. `difficulty="hard"` has zero candidates in the seed set, so it always raises).
- `prereqs_mastered` pure unit: Example-D pair (unseen prereq → False/blocked; prereq ≥0.7 → True/admitted); plus an entity with NO prereq edges → True.
- Prereq-blocked weak entity is EXCLUDED from `weak` (an in-band entity with an unmastered/unseen prereq does not drive selection).
- Tie-break determinism: the Example-B tie resolves to the same `Problem.id` across runs.
- Single-candidate degeneracy: at `difficulty="standard"` (one seed candidate, P5) a non-empty weak profile still returns P5 — personalization cannot "differ" with one candidate (guards against a vacuous "differs at standard" assertion — FEASIBILITY LOW #3).

---

### WU-6A3 — selection wiring (live, real-PG)

**One-line:** Behind `APOLLO_SESSION_PERSONALIZATION_ENABLED` (default OFF), read the `LearnerProfile` once (6A1) and route problem selection through 6A2's `personalize_selection` at BOTH session-start call-sites (`session_init.py:58` AND `next.py:79`); flag-OFF or empty-profile falls back to today's `select_problem`, byte-identical.

**SEAM (owns vs 6A2 / 6A4):** WU-6A3 owns the flag gate, the once-per-session profile read at the two SELECTION call-sites, and the fallback. It does NOT compute scores (delegates to 6A2) and does NOT touch the persona (6A4). It leaves `select_problem`'s signature UNCHANGED (other callers depend on it) — it adds a NEW personalized path alongside.

**Scope — files to touch/create:**
- `apollo/overseer/personalization_flag.py` (NEW, tiny) — `def is_enabled() -> bool: return os.getenv("APOLLO_SESSION_PERSONALIZATION_ENABLED","").lower() in {"1","true","yes","on"}` (mirrors `misconception.py:499-505`). Own the literal once.
- `apollo/overseer/problem_selector.py` (EDIT, ~20 lines incl. the log) — ADD `async def select_problem_personalized(db, *, user_id, search_space_id, concept_id, difficulty, attempted_ids) -> Problem`:
  - `if not personalization_flag.is_enabled(): return await select_problem(db, concept_id=concept_id, difficulty=difficulty, attempted_ids=attempted_ids)` — flag-OFF = the EXACT old path (no log, no read, byte-identical).
  - else: `pool = await list_problems_for_concept(db, concept_id=concept_id)`; `profile = await read_learner_profile(db, user_id=user_id, search_space_id=search_space_id, concept_id=concept_id)`; `chosen = personalize_selection(profile, pool, concept_id=concept_id, difficulty=difficulty, attempted_ids=attempted_ids)`. (Empty profile → 6A2 returns `candidates[0]` — also byte-identical.)
  - **Observability (OPS MEDIUM #2 — explicit acceptance item):** emit ONE structured `_LOG.info` mirroring `apollo_llm.py:144-152` — `_LOG.info("apollo_select_problem_personalized", extra={"event": "personalized_selection", "personalization_enabled": True, "profile_is_empty": profile.is_empty, "n_weak_entities": len(weak_teachable(profile)), "chosen_problem_id": chosen.id, "fallback_fired": <profile.is_empty or no-weak>})`. (`top_coverage_score` optional.) Then `return chosen`.
  - `select_problem` itself is UNTOUCHED (the fallback + read-back callers keep it).
- `apollo/handlers/next.py` (EDIT, ~6 lines) — replace the `select_problem(...)` call at `:79` with `select_problem_personalized(db, user_id=sess.user_id, search_space_id=sess.search_space_id, concept_id=sess.concept_id, difficulty=difficulty, attempted_ids=attempted_ids)`. The `sess` row is already loaded; no extra query.
- `apollo/hoot_bridge/session_init.py` (EDIT, ~6 lines) — replace the `select_problem(...)` call at `:58` with `select_problem_personalized(db, user_id=user_id, search_space_id=search_space_id, concept_id=concept_id, difficulty=difficulty, attempted_ids=[])`. `user_id`/`search_space_id` are params; `concept_id` is the inferred integer id.
- `tests/database/test_personalization_select_route_postgres.py` (NEW) — real-PG route tests.

**The return payloads are UNCHANGED.** `handle_next` returns the same dict (`next.py:98-109`) and `init_session_from_hoot` the same (`session_init.py:96-107`). Only WHICH `Problem` populates them changes. **No API-shape change → no student-UI change required** (note: the CONTENT the UI renders becomes personalized — additive, flag-gated; a future "why this problem" field would be a separate student-ui follow-up — §OPEN-DECISIONS #8/out-of-scope).

**Dependencies:** WU-6A1, WU-6A2, the new flag module. `ApolloSession` ORM. No new external deps.
**Migration?** No. **Real-infra?** **YES — real-PG (Testcontainers `tests/database`).**

**Key tests (acceptance gates):**
- **Flag-OFF byte-identical (BOTH call-sites):** with `APOLLO_SESSION_PERSONALIZATION_ENABLED` unset, `handle_next` and `init_session_from_hoot` return byte-identical payloads to today (reuse `test_next_db.py` real-PG seed harness + `test_problem_selector.py`). The selected `Problem.id` is the same as today's `select_problem`.
- **Flag-ON, empty learner_state (the prod path):** identical to flag-OFF (cold-start fallback fires inside 6A2) — assert same `Problem.id`.
- **Flag-ON, seeded weak profile (`difficulty="intro"` — the only multi-candidate difficulty, §1):** seed `apollo_learner_state` rows (real-PG) so `eq.continuity`/`cond.incompressibility` are weak-and-teachable (and their prereqs mastered) → `handle_next` selects the high-coverage problem (Example A → P1), NOT `candidates[0]` if they differ. Both call-sites covered. **Do NOT attempt this at `standard` (one candidate ⇒ vacuous) or `hard` (raises).**
- **Observability:** assert the personalized branch emits exactly one `event=personalized_selection` log with `chosen_problem_id` + `profile_is_empty` + `fallback_fired` (capture via `caplog`); flag-OFF emits none.
- **Course isolation through the wiring:** a learner with weak entities in course A and a session in course B selects from course B's profile only (the §1.4 invariant end-to-end).
- **PoolExhausted** still raised identically flag-ON when no in-difficulty candidate remains (e.g. `difficulty="hard"`).

---

### WU-6A4 — persona wiring (LEAKAGE_POLICY-bounded) — DEFER BY DEFAULT (§OPEN-DECISIONS #8)

**STATUS — DEFER RECOMMENDED.** Two independently-verified facts make this a near-total no-op AND a non-trivial safety decision in v1, so the default recommendation is to NOT build it as part of the WU-6A wedge: (1) `misconception_code` is ~never set — the `opposes_map` is structurally empty (`done_grading.py:135` hard-codes `"opposes": None`), so even with LAYER3 on the persona block fires essentially never; (2) the live chat path has NO runtime leakage gate (`chat.py:289` calls `draft_reply` directly; `test_chat_no_signals.py:31,42` forbids re-adding `validate_or_raise`/`infer_misconception`), so persona conditioning is the first v1 change to add non-student-derived prompt content with only an OFFLINE corpus test behind it. The contents below are the COMPLETE spec for it IF the orchestrator approves it for v1 anyway.

**One-line:** Behind `APOLLO_SESSION_PERSONALIZATION_ENABLED`, when a non-null `misconception_code` is active for `(student, concept)`, append ONE behavioral system block to `draft_reply` that makes Apollo MORE confused about that misconception — never naming a concept or paraphrasing its description (LEAKAGE_POLICY rules 1 & 3); flag-OFF or null code → the verbatim `APOLLO_SYSTEM_PROMPT`, byte-identical.

**SEAM (owns vs 6A3):** WU-6A4 owns the persona conditioning string contract and the per-turn misconception read. It does NOT touch problem selection. It reuses 6A1's read (only the `misconception_code` field).

**WHICH `misconception_code` drives the block (multi-flag tie-break — SPEC-FIDELITY LOW #2):** `read_learner_profile` returns a per-ENTITY map for the session's concept, and MORE THAN ONE entity could (rarely) carry a non-null `misconception_code`. The spec persona is one "be extra confused about {code}" block per turn, so 6A4 MUST pick ONE deterministically: **the non-null-code entity with the LOWEST `mastery` (most-confused), tie-broken by lowest `entity_id`.** The read is ALREADY restricted to the session's concept entities (via `read_learner_profile`'s `concept_id` filter), so no cross-concept code can leak in. This rule must be pinned (a unit test) so the leakage-corpus assertion is stable even though the path is near-inert in v1. (Part of §OPEN-DECISIONS #6.)

**Scope — files to touch/create:**
- `apollo/agent/apollo_llm.py` (EDIT, ~8 lines) — add an optional kwarg to `draft_reply`: `misconception_code: str | None = None`. When `personalization_flag.is_enabled()` AND `misconception_code` is non-null, append ONE system block AFTER `APOLLO_SYSTEM_PROMPT`, BEFORE history:
  - The block is BEHAVIORAL and code-as-internal-flag only — e.g. *"You find one particular way of reasoning especially hard to follow and you keep second-guessing it; when it comes up, express that uncertainty and ask the student to re-explain, but never name a principle, never state it as a fact, and never correct anyone."* It MUST NOT embed the misconception entity's `display_name`/`description` or any named law. **VERIFIED HAZARD:** `misconceptions.json` descriptions literally name "Bernoulli" and paraphrase the canonical relation ("faster flow means lower static pressure") — injecting `display_name`/`description` would be a direct rule-3 leak. The slug (`misc.pressure_velocity_same_direction`) is used ONLY as an internal flag to select the static behavioral string; it is never rendered into the prompt. The exact string is §OPEN-DECISIONS #6 (the central decision of this sub-unit).
  - Flag-OFF OR `misconception_code is None` → no extra block → byte-identical `messages` list.
- `apollo/handlers/chat.py` (EDIT, ~6 lines) — before the `draft_reply(...)` call at `:289`, read the misconception code: `profile = await read_learner_profile(db, user_id=sess.user_id, search_space_id=sess.search_space_id, concept_id=sess.concept_id)` (per-turn read, cheap indexed query, EMPTY in prod), select the active code via the tie-break above, pass it to `draft_reply`. (See §4 decision #5 for the read-once-per-session-vs-per-turn decision; v1 = per-turn read, no migration. The per-turn read TRIP-WIRE is in §OPEN-DECISIONS #5.)
- Tests in `apollo/agent/tests/test_apollo_llm.py` (reuse the `@patch("apollo.agent.apollo_llm.OpenAI")` capture harness) + `apollo/agent/tests/test_leakage_corpus.py` (the OFFLINE corpus; there is NO live judge on this path).

**Dependencies:** WU-6A1 (the read), the flag module, `LEAKAGE_POLICY.md` (the WRITTEN contract) + the OFFLINE `test_leakage_corpus.py`. **There is NO runtime `leakage_judge` in the chat path and 6A4 must NOT add one** (it would break `test_chat_no_signals.py`); if a live gate is ever wanted that is an explicit contract change updating that guard test (§OPEN-DECISIONS #6/#8). No new external deps.
**Migration?** No (per-turn read; the persona-cache column is the only migration candidate and is DEFERRED — §4 decision #5).
**Real-infra?** Persona unit test (no DB, capture harness) + the OFFLINE leakage corpus (the `test_leakage_corpus.py` set — no live judge). The per-turn read path is covered by a flag-OFF chat route regression (real-PG `test_chat`).

**Key tests (acceptance gates):**
- **Flag-OFF / null-code byte-identical:** with the flag unset OR `misconception_code=None`, `draft_reply` produces the EXACT same `messages` list (assert no extra system block — reuse `test_apollo_llm.py`'s capture client). Null is the prod-common case (empty `opposes_map`).
- **Flag-ON, code present:** the extra block is appended AFTER `APOLLO_SYSTEM_PROMPT`, BEFORE history; assert the block is behavioral-only (does NOT contain the misconception's `display_name`/`description` substring nor any forbidden named law).
- **Multi-flag tie-break determinism:** with two non-null-code entities for the concept, the lowest-mastery (then lowest-`entity_id`) code is selected; pinned as a unit test.
- **Leakage CORPUS regression (offline):** the conditioned block + a representative draft pass the OFFLINE `test_leakage_corpus.py` checks (no named concept, no canonical-relation paraphrase). This is NOT a live judge — it is the only safety net besides the structural more-confused polarity. Cover BOTH null (common) and a synthetically-set code.
- **More-confused polarity:** the block strictly increases uncertainty (a behavioral assertion / corpus check) — it never adds a corrective or a fact.

---

## 4. Cross-cutting decisions (LOCKED)

1. **ONE new flag `APOLLO_SESSION_PERSONALIZATION_ENABLED` (default OFF everywhere incl. prod), shared by 6A3 + 6A4.** Mirrors `misconception.py:499-505`. Owned in `apollo/overseer/personalization_flag.py::is_enabled()`; call-sites never duplicate the literal. Orthogonal to `APOLLO_GRAPH_SIM_LAYER3_ENABLED` (which gates the WRITE / table population). Personalization can be flag-ON in prod and STILL be a total no-op because the learner-state table is empty (LAYER3 OFF) — the wedge is double-gated by design.
2. **`select_problem`'s signature is UNCHANGED; 6A3 adds `select_problem_personalized` alongside.** `select_problem` has read-back/other callers; the personalized function delegates to it on flag-OFF and is the fallback on empty-profile. The cold-start / flag-OFF path is therefore the EXACT existing code (`problem_selector.py:54-59`) — the byte-identical non-regression proof.
3. **The cold-start / flag-OFF fallback is `candidates[0]` (today) for selection and verbatim `APOLLO_SYSTEM_PROMPT` for persona.** This is the PROD path (LAYER3 OFF → empty table) AND the flag-OFF path. Pinned as a pure branch in 6A2 and a real-PG flag-OFF byte-identical regression in 6A3, and a capture-harness flag-OFF byte-identical regression in 6A4.
4. **Where the band/threshold constants live + WHO computes `prereqs_mastered` (REVISED — FEASIBILITY MEDIUM #2).** ALL scoring constants — `TEACHABLE_BAND_LO/HI`, `MASTERED_THRESHOLD`, `UNSEEN_MASTERY`, `REPROBE_CONFIDENCE` — and the `prereqs_mastered(...)` SEMANTIC live in `personalization_select.py` (6A2), the pure unit, so the golden vectors pin every number. **6A1 imports NOTHING from 6A2** (the dependency arrow is strictly 6A1 → 6A2; importing a constant back would create a 6A1↔6A2 import cycle). 6A1 therefore does NOT compute `prereqs_mastered`; it emits the RAW `prereq_edges` + id↔key maps + raw per-entity mastery, and 6A2 derives `prereqs_mastered` purely (golden-vector-testable, not real-PG-only). **Unseen prereq (no learner_state row) = treated as `UNSEEN_MASTERY = 0.50` = NOT mastered = blocks teachability** (conservative; on a fully-cold profile NO entity is teachable → fall through to `candidates[0]`, consistent with the cold-start fallback). §OPEN-DECISIONS #3.
5. **Persona read: per-turn (no migration) in v1; cache-on-session is DEFERRED.** Apollo is stateless per turn (`draft_reply` is a free function; there is NO session-scoped agent object). To honor "read once at session start," the natural store is a `ApolloSession.misconception_code` column — but that needs migration 030. v1 instead RE-READS `read_learner_profile` per turn in `handle_chat`: it is one cheap indexed query (PK-prefix on `apollo_learner_state`) and is EMPTY in prod, so the cost is negligible. The cache column is a future optimization. §OPEN-DECISIONS #5.
6. **Difficulty = CLAMP-WITHIN-CHOICE; re-probe = SOFT HOLD.** v1 does not override the student's `body.difficulty` (a hard UI input). Coverage re-ranks only within the chosen-difficulty candidate set. Low-confidence weak entities are held in the weak set, never hard-negatived (spec §1 "class-level first, never a hard per-student negative"). §OPEN-DECISIONS #1, #5.
7. **The reference-entity set is reconstructed PURELY in 6A2, NOT read from the DB/Neo4j.** The parsed `Problem` drops `entity_key`, but the canonical-key set is the PROVEN deterministic function of `(entry_type, id)` via the frozen `_ENTRY_TYPE_TO_KIND_PREFIX`. The pool is already loaded in ONE query by `list_problems_for_concept`. **No N+1.** (Alternative — read raw `ConceptProblem.payload` via the `done_inputs._find_problem_payload` pattern — is available but NOT needed; the pure reconstruction avoids the extra query entirely. Adding `extra="allow"` to `Problem`/`ReferenceStep` is REJECTED: it touches a schema used by frozen grading code.)
8. **NO migration in 6A1/6A2/6A3.** The only migration candidate (persona-cache column) is DEFERRED (decision #5). `next_free_migration=030` stays unused.
9. **`personalize_selection` carries `concept_id` (REVISED — FEASIBILITY MEDIUM #1).** Today's exhaustion raise is `PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=difficulty)` (`problem_selector.py:58`; constructor `errors.py:64` embeds `concept_cluster_id` in the message, and a 409 handler depends on this back-compat string). The pure scorer therefore takes `concept_id: int` purely to reconstruct that byte-identical error; it is NOT used in scoring. (Rejected alternative: have 6A2 raise a neutral sentinel and let 6A3 translate — adds a control-flow seam for no gain; threading `concept_id` is cleaner and keeps the raise co-located with the candidate check.)
10. **6A3 emits ONE structured selection log (REVISED — OPS MEDIUM #2).** `select_problem_personalized` logs `event=personalized_selection` with `{personalization_enabled, profile_is_empty, n_weak_entities, chosen_problem_id, fallback_fired}`, mirroring `apollo_llm.py:144-152`. Flag-OFF emits nothing (byte-identical, no behavioral change). This is the only runtime signal that the wedge fired vs degraded; required before anyone flips the flag in prod.

---

## 5. Per-sub-unit contract tables

### WU-6A1 — Q1 read path
| Field | Value |
|---|---|
| Kind | Real-PG read module — mirrors the IO seam of `apollo/grading/persistence.py` / `learner_model/persistence.py` |
| Files (NEW) | `apollo/learner_model/personalization_read.py`; `tests/database/test_personalization_read_postgres.py` |
| Frozen public API | `EntityProfile` (raw columns only) / `LearnerProfile{by_canonical_key, prereq_edges, entity_id_by_key, key_by_entity_id, is_empty}` (frozen); `async read_learner_profile(db,*,user_id,search_space_id,concept_id)->LearnerProfile` |
| Consumes | `LearnerState`/`KGEntity`/`EntityPrereq` ORM |
| depends_on | compare branch `feat/apollo-kg-wu5b5-chat-keyword-wireup` (#46) |
| real_infra | **real-PG (Testcontainers `tests/database`, `pytest.mark.integration`/`db_session`)** — course-scoping + cold-start need the FK chain + empty-table return |
| migration | none |
| Key risks owned | course-isolation scoping (§1.4); empty-profile cold-start return; RAW prereq-edge + id↔key emission (NOT the prereqs_mastered semantic — that is 6A2's); column-read parity (no belief recompute); imports NOTHING from 6A2 (one-way arrow) |

### WU-6A2 — pure selection + coverage + difficulty
| Field | Value |
|---|---|
| Kind | Pure library (no IO) — mirrors `apollo/learner_model/belief.py` (WU-5A1) |
| Files (NEW) | `apollo/learner_model/personalization_select.py` |
| Frozen public API | `TEACHABLE_BAND_LO/HI`; `MASTERED_THRESHOLD`; `UNSEEN_MASTERY`; `REPROBE_CONFIDENCE`; `reference_entity_keys(problem)`; `prereqs_mastered(profile,key)`; `personalize_selection(profile,pool,*,concept_id,difficulty,attempted_ids)->Problem` |
| Consumes | `Problem`; `PoolExhaustedError`; IMPORTED frozen `_ENTRY_TYPE_TO_KIND_PREFIX`; 6A1's `LearnerProfile` (raw edges + maps) |
| depends_on | WU-6A1 |
| real_infra | none (pure golden-vector) |
| migration | none |
| Key risks owned | the weak-entity predicate (band + prereq gate); the `prereqs_mastered` derivation (moved here from 6A1); the deficit-weighted coverage formula; the `Problem.id` tie-break; the byte-identical `PoolExhaustedError` (needs `concept_id`); the entity-key reconstruction (no N+1); the empty-profile → `candidates[0]` non-regression branch; difficulty clamp-within-choice; re-probe soft-hold |

### WU-6A3 — selection wiring
| Field | Value |
|---|---|
| Kind | Live wiring at TWO call-sites + flag — mirrors a `done.py`/route edit |
| Files (NEW) | `apollo/overseer/personalization_flag.py`; `tests/database/test_personalization_select_route_postgres.py` |
| Files (EDIT) | `apollo/overseer/problem_selector.py` (+`select_problem_personalized`, ~15 lines, `select_problem` untouched); `apollo/handlers/next.py` (~6 lines); `apollo/hoot_bridge/session_init.py` (~6 lines) |
| Frozen public API | `personalization_flag.is_enabled()`; `select_problem_personalized(db,*,user_id,search_space_id,concept_id,difficulty,attempted_ids)->Problem` |
| Consumes | WU-6A1, WU-6A2; `ApolloSession` ORM |
| depends_on | WU-6A2 |
| real_infra | **real-PG (Testcontainers `tests/database`)** — both call-sites; flag-OFF byte-identical regression; seeded-profile selection; course isolation |
| migration | none |
| Key risks owned | wiring BOTH call-sites (else no fire at session start); flag-OFF byte-identity; once-per-session read; unchanged return payload (no UI break); ONE structured selection log (observability, OPS MEDIUM #2) |

### WU-6A4 — persona wiring
| Field | Value |
|---|---|
| Kind | LLM-prompt-contract edit + per-turn read — mirrors a `chat.py`/`apollo_llm.py` edit. **DEFER BY DEFAULT** (near-no-op + no live leakage gate) |
| Files (EDIT) | `apollo/agent/apollo_llm.py` (~8 lines, optional `misconception_code` kwarg + behavioral block); `apollo/handlers/chat.py` (~6 lines, per-turn read + tie-break + pass-through) |
| Files (test) | `apollo/agent/tests/test_apollo_llm.py`; `apollo/agent/tests/test_leakage_corpus.py` (OFFLINE corpus — NO live judge) |
| Frozen public API | `draft_reply(..., misconception_code: str \| None = None)` (additive, back-compat) |
| Consumes | WU-6A1 (`misconception_code` field); flag module; `LEAKAGE_POLICY.md` (written contract) + the OFFLINE `test_leakage_corpus.py` |
| depends_on | WU-6A3 (or parallel off WU-6A1) |
| real_infra | persona capture-harness (no DB) + OFFLINE leakage corpus (NO live judge in path) + a flag-OFF chat-route regression (real-PG `test_chat`) |
| migration | none (per-turn read; cache column deferred — per-turn trip-wire in §OPEN-DECISIONS #5) |
| Key risks owned | **LEAKAGE_POLICY rules 1 & 3** (behavioral-only, no description/named-law leak — `misconceptions.json` descriptions literally name Bernoulli); **NO runtime leakage gate exists** (must NOT re-add one — breaks `test_chat_no_signals.py`); multi-flag tie-break determinism; null-tolerance (the common case); more-confused polarity; flag-OFF byte-identity |

---

## 6. Risks, uncertainties, spec ambiguities

1. **HIGHEST — wedge must fire at SESSION START → BOTH selection call-sites must be wired (§1).** Wiring only `handle_next` (the pre-recon's single named site) would miss the literal session start (`session_init.py:58`). Mitigation: WU-6A3 wires both; a real-PG test asserts the init pick is personalized. Confidence: HIGH (both sites verified, both have the scoping keys in scope).
2. **HIGH — LEAKAGE_POLICY contract for persona conditioning, WITH NO RUNTIME GATE (§1, WU-6A4 — corrected this revision per OPS HIGH #1).** Injecting the misconception's description/name is a rule-3/rule-1 leak (VERIFIED: `misconceptions.json` descriptions name Bernoulli + paraphrase the canonical relation). The earlier draft claimed a "leakage-judge regression" protects this path — that is FALSE: `chat.py:289` calls `draft_reply` directly with NO `validate_or_raise`/`leakage_judge`, and `test_chat_no_signals.py:31,42` actively forbids re-adding one. The ONLY safety nets are (a) the structural more-confused-only polarity (cannot add knowledge) and (b) an OFFLINE leakage CORPUS test. Because persona conditioning is the FIRST v1 change to add non-student-derived prompt content AND fires essentially never (empty `opposes_map`), the mitigation is to **DEFER WU-6A4** (§OPEN-DECISIONS #8). If built: behavioral-only block, null-tolerant, deterministic multi-flag tie-break, offline-corpus-checked, separate reviewable sub-unit; re-adding a live judge is an explicit contract change that must update the guard test. Confidence: HIGH on the bound and on the no-live-gate fact; MEDIUM on the exact wording (§OPEN-DECISIONS #6).
3. **HIGH→RESOLVED — N+1 reference-entity hazard.** EMPIRICALLY PROVEN the entity-key set reconstructs purely from the parsed `Problem` via the frozen prefix map; the pool is one query; zero per-problem round-trip. Confidence: HIGH (round-trip reproduced).
4. **HIGH→RESOLVED — cold-start / flag-OFF non-regression.** The fallback IS the existing `select_problem` (untouched) + verbatim `APOLLO_SYSTEM_PROMPT`; pinned by byte-identical tests at all three layers. Confidence: HIGH.
5. **MEDIUM — `misconception_code` is RARELY set in v1 (empty `opposes_map`).** Persona conditioning is a near-total no-op in real sessions. This makes WU-6A4 low-impact in v1 (an argument to DEFER it — §OPEN-DECISIONS #8) but the null-tolerant design is correct regardless. Confidence: HIGH (carried heads-up from WU-5A2/4C1).
6. **MEDIUM — the six scoring semantics are UNSPECIFIED by the spec.** Band inclusivity, prereq threshold, coverage formula, tie-break, difficulty rule, re-probe mechanic are all hand-set here (interpretable, guardrail-compliant) but each is a genuine calibration call — §OPEN-DECISIONS #1-5. The golden vectors pin whatever the orchestrator confirms. Confidence: HIGH the recommended values are defensible (corroborated by prior art: Rimac ZPD [0.4,0.6], InfoTutor [0.5,0.8], ZPDES prereq-activation, greedy max-coverage).
7. **MEDIUM — difficulty is a hard student input, not a free lever (§spec "tune difficulty" vs `body.difficulty`).** v1 clamps within the student's choice (does not override). Confidence: HIGH this is the safest v1; the override/advisory variants are §OPEN-DECISIONS #1.
8. **LOW — per-turn persona read vs cache-on-session.** v1 re-reads per turn (cheap, empty in prod, no migration); the cache column is a deferred optimization. Confidence: HIGH.
9. **LOW — slug-vs-int `concept_id` AND directory-filing.** All reads/joins key off the session's INTEGER `concept_id`, never `Problem.concept_id` (slug). The pool is filed by DIRECTORY (the seeder overrides each JSON's `concept_id` with the directory's integer id), so P3/P4 (JSON slug `continuity_equation`/`volumetric_flow_rate`) ARE in the bernoulli pool — 6A2 fixtures MUST seed via the integer id and assert on `Problem.id`. Confidence: HIGH (verified all 5 seed files + `seed_apollo_concept_registry.py:164-185`).
10. **LOW→RESOLVED — constant ownership / import cycle.** All scoring constants + the `prereqs_mastered` semantic live in 6A2; 6A1 emits RAW edges/maps and imports nothing from 6A2 (one-way arrow, no cycle). Confidence: HIGH (FEASIBILITY MEDIUM #2 resolved in §4 decision #4).
11. **LOW — seed difficulty distribution (4 intro / 1 standard / 0 hard).** Discriminating selection tests can only run at `intro`; `standard` degenerates to one candidate and `hard` always raises. All such tests pinned to `intro` (§1). Confidence: HIGH (verified all 5 seed files).
12. **LOW — `PoolExhaustedError` byte-identity needs `concept_id`.** `personalize_selection` takes `concept_id` solely to reconstruct `concept_cluster_id=str(concept_id)`; the golden vector pins the exact string. Confidence: HIGH (verified `problem_selector.py:58` + `errors.py:64`).
13. **LOW — per-turn persona read becomes a hot-path tax IF LAYER3 is flipped AND 6A4 is live.** v1 is safe (table empty in prod). The trip-wire — move to the deferred `ApolloSession.misconception_code` cache column at the same gate that flips LAYER3 — is documented in §OPEN-DECISIONS #5. Confidence: HIGH.
14. **LOW — silent downstream UX change.** Behind the flag, the same student gets a personalized problem order with no UI affordance. Student-UI is correctly out of scope; the "why this problem" surfacing is recorded as a known downstream follow-up (§OPEN-DECISIONS #10), not discovered in the pilot. Confidence: HIGH.

---

## 7. Build order (stacked branches)

1. **WU-6A1** branches off `feat/apollo-kg-wu5b5-chat-keyword-wireup` (#46, tip `3b0ec52`) — the diff-cover compare branch. Lands `apollo/learner_model/personalization_read.py` + the real-PG read test. Independently green at ≥95% patch coverage; **trips the `tests/database` real-PG gate (green-not-skipped), no Neo4j, no LLM.** Ships first: it is the substrate 6A2/6A3/6A4 all consume, and the course-isolation + cold-start invariants are the riskiest IO behavior to lock early.
2. **WU-6A2** branches off WU-6A1. Lands `apollo/learner_model/personalization_select.py` (pure). Independently green at ≥95% with **NO container** (golden-vector tests, including the proven entity-key round-trip and the worked Examples A/B/C). De-risks every scoring number before any wiring.
3. **WU-6A3** branches off WU-6A2. Lands the flag module + the `select_problem_personalized` path + the two call-site edits + the real-PG route test. Independently green at ≥95%; **trips the `tests/database` real-PG gate.** The flag-OFF byte-identical regression at BOTH call-sites is the acceptance anchor.
4. **WU-6A4 — DEFERRED BY DEFAULT** to a follow-up WU (§OPEN-DECISIONS #8), gated on `opposes_map` being populated enough that `misconception_code` actually fires. Rationale: near-total no-op in v1 (empty `opposes_map`) AND the live chat path has NO runtime leakage gate (only a structural polarity + an offline corpus), so it carries the heaviest review weight for the least v1 payoff. If the orchestrator approves it for v1, it branches off WU-6A3 (or parallel off WU-6A1 — it only needs the read) and lands the `draft_reply` kwarg + the `chat.py` per-turn read + tie-break + the persona/offline-corpus tests. It must NEVER be fused into 6A3.

The v1 wedge is **6A1 → 6A2 → 6A3** (the part that actually fires in real sessions). Each sub-unit is one TDD-executor pass: 6A1 ≈ 1 module + 1 real-PG test file; 6A2 ≈ 1 pure module + ~3 golden-vector test files; 6A3 ≈ 1 tiny flag module + 3 thin edits + 1 real-PG test (incl. the observability-log assertion); 6A4 (if built) ≈ 2 thin edits + 2 persona/offline-corpus test files.

---

## 8. Out-of-scope (held firmly)

- **The `question→entity tags` table (spec §8 line 1066).** §8 frames selection as a join over a per-problem entity-tag table — that table is DEFERRED future work and does NOT exist. v1's substrate is the per-problem `reference_solution[].entity_key`, reconstructed in-memory (§1, decision #7). Building the tag table is a separate WU, not 6A.
- **Difficulty auto-tune (override/map band→difficulty).** v1 clamps within the student's choice; auto-tune is a follow-up (§OPEN-DECISIONS #1).
- **The persona-cache column on `ApolloSession` (migration 030).** Deferred; v1 reads per turn (decision #5).
- **Student-UI changes.** Backend-only. The `/next` and chat response shapes are UNCHANGED (only the content is personalized). A future "why this problem" surfacing would be an additive, flag-gated student-ui follow-up — note only.
- **Parameter fitting / calibration of the band + thresholds.** Hand-set in v1 per the guardrails; offline calibration is post-pilot.
- **WU-6A4 if deferred** — persona conditioning ships as a follow-up WU when `opposes_map` is non-empty enough to make `misconception_code` actually fire.

---

## 9. Doc / coverage obligations (per project contracts)

- **Patch coverage ≥95%** on changed lines (`diff-cover coverage.xml --compare-branch=origin/<parent> --fail-under=95`; intra-stack compare branch = the previous sub-unit, first = `feat/apollo-kg-wu5b5-chat-keyword-wireup`). 6A2 is pure → exhaustible. 6A1/6A3 need Docker for the `tests/database` gate (green-not-skipped); BOTH the flag-on and flag-off paths covered. 6A4's per-turn read path + the null/code branches covered.
- **NEVER apply migrations to any remote DB.** No migration in 6A. Real-PG tests run on LOCAL Testcontainers (pgvector:pg16) only.
- **Drift contract:** reconcile `docs/architecture/apollo.md` in the SAME commit each sub-unit lands — register `personalization_read.py` / `personalization_select.py` / `personalization_flag.py` and the `select_problem_personalized` (+ its observability log) + (if 6A4 built) `draft_reply` kwarg + the two call-site wirings; document the NEW `APOLLO_SESSION_PERSONALIZATION_ENABLED` flag (default OFF), the double-gating (flag AND LAYER3-populated table), the cold-start/flag-OFF non-regression contract, the directory-filed pool + slug-vs-int `concept_id`, the entity-key reconstruction (no N+1), the silent personalized-order UX change behind the flag (the downstream "why this problem" follow-up), and — if 6A4 ships — the LEAKAGE_POLICY-bounded persona contract WITH the explicit note that NO runtime leakage gate exists on the chat path (offline corpus + structural polarity only); bump `last_verified`. `apollo.md` already owns `apollo/**` + `apollo/learner_model/**` — no `owns:` change needed.
- **The new flag** `APOLLO_SESSION_PERSONALIZATION_ENABLED` — declare it as a named module function (`personalization_flag.is_enabled()`, mirroring `misconception.is_enabled()`) + a documented env flag. Default OFF everywhere incl. prod.

---

## 10. Summary table

| Sub-unit | Seam (input → output) | Files | Migration | Real-infra | Key risk owned |
|---|---|---|---|---|---|
| **WU-6A1** | `(user_id, search_space_id, concept_id)` → `LearnerProfile{by_canonical_key (raw mastery/confidence/misconception_code), prereq_edges, id↔key maps, is_empty}`, course-scoped, EMPTY on empty table | `learner_model/personalization_read.py`, `tests/database/test_personalization_read_postgres.py` (NEW) | No | **Yes — real-PG** (no Neo4j, no LLM) | course isolation (§1.4); cold-start empty return; RAW edge/map emission (NOT prereqs_mastered); column-read parity |
| **WU-6A2** | `LearnerProfile` + `Problem` pool + `concept_id` + difficulty + attempted → best-coverage `Problem` (cold-start → `candidates[0]`) | `learner_model/personalization_select.py` (NEW) | No | **No** (pure golden-vector) | weak predicate + `prereqs_mastered` derivation; deficit-weighted coverage; `Problem.id` tie-break; byte-identical `PoolExhaustedError` (needs `concept_id`); entity-key reconstruction (no N+1); non-regression branch; difficulty clamp; re-probe |
| **WU-6A3** | flag-gated `LearnerProfile` read once → `personalize_selection(…,concept_id,…)` at BOTH `session_init.py:58` + `next.py:79` (flag-OFF → today's `select_problem`) + ONE structured selection log | `overseer/personalization_flag.py`, `tests/database/test_personalization_select_route_postgres.py` (NEW); `overseer/problem_selector.py`, `handlers/next.py`, `hoot_bridge/session_init.py` (EDIT) | No | **Yes — real-PG** | wire BOTH call-sites; flag-OFF byte-identity; unchanged return payload; observability log (OPS MEDIUM #2) |
| **WU-6A4** (**DEFER by default**) | flag-gated `misconception_code` (lowest-mastery tie-break) → one behavioral, LEAKAGE-safe system block in `draft_reply` (flag-OFF/null → verbatim prompt) | `agent/apollo_llm.py`, `handlers/chat.py` (EDIT); `agent/tests/test_apollo_llm.py`, `test_leakage_corpus.py` | No | persona capture + OFFLINE leakage corpus (NO live judge) + flag-OFF chat regression | LEAKAGE_POLICY rules 1&3; NO runtime gate (must not re-add — breaks `test_chat_no_signals.py`); multi-flag tie-break; null-tolerance; more-confused polarity; flag-OFF byte-identity |

**The one-sentence split:** WU-6A1 is the first read path over the (prod-empty) learner-state table (real-PG, course-scoped, cold-start-clean, RAW edges/maps); WU-6A2 is the pure interpretable selection arithmetic (greedy deficit-weighted coverage of the teachable-edge weak set, `prereqs_mastered` derived purely, `Problem.id` tie-break, byte-identical `PoolExhaustedError`, cold-start → today's `candidates[0]`, entity sets reconstructed in-memory with NO N+1); WU-6A3 threads that selection through BOTH session-start call-sites behind one default-OFF flag with a byte-identical flag-OFF regression and one observability log; and WU-6A4 (DEFERRED by default) would condition the confused-classmate persona on an active misconception flag inside the LEAKAGE_POLICY contract (behavioral, more-confused-only, null-tolerant, no runtime gate).

---

## 11. Critic-finding ledger (every finding re-verified + resolved this revision)

All three lenses returned ACCEPT. Each finding below was re-verified by reading the cited code in this pass; the resolution and the section it landed in are recorded. No finding was rejected.

| # | Lens / sev | Finding (re-verified) | Resolution |
|---|---|---|---|
| 1 | FEAS / MED | `PoolExhaustedError` byte-identity needs `concept_id`; today's raise is `PoolExhaustedError(concept_cluster_id=str(concept_id), difficulty=…)` (`problem_selector.py:58`, `errors.py:64`), but the proposed `personalize_selection` had no `concept_id`. | ACCEPTED. `concept_id: int` added to `personalize_selection`'s signature (§3 6A2, §4 decision #9); golden vector pins the exact `concept_cluster_id` string. |
| 2 | FEAS / MED | `MASTERED_THRESHOLD` in 6A2 + "6A1 imports it back" creates a 6A1↔6A2 import CYCLE; and putting `prereqs_mastered` (a scoring semantic) in 6A1 contradicts "6A2 owns every number" + forces real-PG testing of a pure rule. | ACCEPTED. 6A1 now emits RAW `prereq_edges` + id↔key maps + raw mastery and imports NOTHING from 6A2 (one-way arrow); `prereqs_mastered` derivation + ALL constants live in 6A2 (pure, golden-tested). §3 6A1/6A2, §4 decision #4. |
| 3 | FEAS / LOW | Seed difficulty is 4 intro / 1 standard / 0 hard — discriminating selection tests only work at `intro`; a "differs at standard" assertion is vacuous. | ACCEPTED. §1 seed-distribution fact added; all discriminating tests pinned to `intro`; a `standard` single-candidate degeneracy test + `hard` raises test added (§3 6A2/6A3). |
| 4 | FEAS / LOW + OPS (defer) | 6A4 is marginal: empty `opposes_map` (`done_grading.py:135`) ⇒ `misconception_code` ~never set; building it 4th spends the leakage-review budget on a path that won't fire. | ACCEPTED. 6A4 is now DEFER-BY-DEFAULT (VERDICT, §3, §7, §OPEN-DECISIONS #8). If built, parallel off 6A1. |
| 5 | SPEC / MED | Worked Examples A/B narrate P3/P4 as bernoulli candidates, but their JSON `concept_id` is `continuity_equation`/`volumetric_flow_rate`; correct ONLY because the seeder files by DIRECTORY (`seed_apollo_concept_registry.py:164-185`). A fixture keyed off `Problem.concept_id` would wrongly exclude them. | ACCEPTED. §1 "POOL IS FILED BY DIRECTORY" fact added; Examples + 6A2 key-tests note it; fixtures pinned to the INTEGER `concept_id`, asserts on `Problem.id` only. |
| 6 | SPEC / LOW | 6A4 doesn't pin WHICH entity's `misconception_code` drives the single persona block when >1 is set, nor confirm concept-scoping. | ACCEPTED. Multi-flag tie-break (lowest mastery, then lowest `entity_id`; read already concept-scoped) specified in §3 6A4 + §OPEN-DECISIONS #6. |
| 7 | SPEC / LOW | Unseen-prereq = 0.50 = blocks is a hand-set calibration choice, not a spec mandate (`belief.py:118-120` defines 0.50 as the cold-start "unknown", not "weak"). | ACCEPTED. Called out as a calibration choice in §OPEN-DECISIONS #3; Example D (partially-warm: unseen blocks vs prereq≥0.7 admits) added as a 6A2 golden pair. |
| 8 | OPS / HIGH | 6A4's safety story was MISREPRESENTED: the live chat path has NO leakage gate (`chat.py:289` direct `draft_reply`; `test_chat_no_signals.py:31,42` forbids `validate_or_raise`/`infer_misconception`). The "leakage-judge regression" net does not exist at runtime. | ACCEPTED (most important correction). Corrected throughout (§1 new ⚠⚠ bullet, §3 6A4, §6 risk #2, §OPEN-DECISIONS #6/#8); 6A4's net is now stated as structural polarity + OFFLINE corpus only; re-adding a live gate is an explicit contract change updating the guard test. Reinforces the DEFER recommendation. |
| 9 | OPS / MED | No observability: `select_problem`/`list_problems_for_concept` emit no log; nothing distinguishes a personalized from a fallback selection (un-auditable wedge). `apollo_llm.py:144` is the house template. | ACCEPTED. 6A3 now emits ONE structured `event=personalized_selection` log (§1 new bullet, §3 6A3, §4 decision #10) as an explicit acceptance item + a `caplog` test. |
| 10 | OPS / LOW | Per-turn persona read is "negligible" only because the table is empty; once LAYER3 is flipped AND 6A4 live it is a per-turn tax on the hottest path for a near-null field. | ACCEPTED. Trip-wire documented (§OPEN-DECISIONS #5): move to the cache column at the same gate that flips LAYER3. v1 unchanged. |
| 11 | OPS / LOW | Silent UX change: behind the flag the same student gets a personalized order with no UI affordance — record as a known downstream item, not a pilot discovery. | ACCEPTED. Recorded in §OPEN-DECISIONS #10 + the drift-note obligation; student-UI stays out of scope. |

---

## OPEN DECISIONS FOR THE ORCHESTRATOR (genuine human/spec calls before building)

1. **[SCOPE — difficulty tuning] Override vs CLAMP-WITHIN-CHOICE vs advisory.** Difficulty is a HARD student input today (`body.difficulty`, `Literal["intro","standard","hard"]`). The spec says "tune difficulty" but the UI sends an explicit choice. **Recommended: CLAMP-WITHIN-CHOICE** (coverage re-ranks only within the chosen difficulty; never overrides) — safest, keeps flag-OFF byte-identical, respects UI input. Confirm vs override/advisory.
2. **[SEMANTICS — weak band] Inclusivity + weakest-first promotion.** Recommended: `0.3 ≤ mastery ≤ 0.7` INCLUSIVE (literal spec line 470). Open sub-decision: should `mastery < 0.3` entities WITH mastered prereqs be promoted into the weak set (weakest-first), or strictly excluded? **Recommended: strictly the band** (flag weakest-first as a calibration tweak).
3. **[SEMANTICS — prereq gate] Threshold + unseen-prereq rule.** Recommended: prereq "mastered" iff `mastery ≥ 0.7` (reuse the band top — one fewer constant); **unseen prereq (no row) = mastery 0.50 = NOT mastered = BLOCKS** (conservative; on a fully-cold profile no entity is teachable → `candidates[0]` fallback). Alternative: unseen = satisfied (don't block). Confirm.
4. **[SEMANTICS — coverage formula] Deficit-weighted vs plain count; tie-break.** Recommended: `score = Σ_{covered weak e} (1 − mastery_e)` (deficit-weighted greedy coverage — prefers higher-leverage weak coverage, see Example A). **Tie-break = lowest `Problem.id` (LOCKED — matches today's `sorted` stability and the non-regression proof).** Confirm deficit-weighted vs plain count.
5. **[SCOPE — re-probe + caching] Re-probe mechanic + read-once + the per-turn TRIP-WIRE.** (a) Re-probe in v1 = SOFT HOLD (keep low-confidence weak entities in the weak set; `REPROBE_CONFIDENCE=0.4`), never hard-negative — or the stronger "prefer same-entity coverage" / "re-select same problem"? Recommended: soft-hold for v1. (b) Persona read (only relevant if 6A4 is built): PER-TURN (no migration, cheap, empty in prod) vs cache on `ApolloSession` (migration 030)? **Recommended: per-turn read, defer the cache column.** **TRIP-WIRE (OPS LOW #3 — bind to the LAYER3 gate):** the per-turn read is only "negligible" because the table is EMPTY in prod; if/when `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is flipped AND 6A4 is live, the per-turn read becomes 1–3 scoped queries on EVERY chat turn (the hottest path) to fetch a near-always-null field — at that point it MUST move to the deferred `ApolloSession.misconception_code` cache column (read-once-at-session-start, written at problem advance). Tie this to the same calibration-review gate that flips LAYER3 so it is not forgotten. Confirm (a), (b), and the trip-wire.
6. **[CONTRACT — persona string + tie-break + NO live gate] WU-6A4's central decision.** (a) The exact "be extra confused about {code}" wording must make Apollo MORE confused WITHOUT naming a concept (rule 1) or paraphrasing the misconception's canonical description (rule 3) — behavioral-only, code-as-internal-flag. **VERIFIED hazard:** `misconceptions.json` descriptions literally name Bernoulli + paraphrase the relation, so the `display_name`/`description` MUST NOT be rendered. (b) **Multi-flag tie-break (SPEC-FIDELITY LOW #2):** when >1 of the concept's entities carries a non-null `misconception_code`, the single persona block is driven by the LOWEST-mastery entity (tie → lowest `entity_id`); confirm this rule. (c) **NO runtime leakage gate exists on the chat path (OPS HIGH #1):** `chat.py:289` calls `draft_reply` directly and `test_chat_no_signals.py` forbids re-adding `validate_or_raise`. The only safety nets are the structural more-confused polarity + an OFFLINE corpus test. If a live gate is wanted, it is an explicit contract change updating that guard test. Provide/approve the string AND decide whether to ship 6A4 at all given (c) and the empty `opposes_map`.
7. **[GUARDRAIL — packages] No new package.** Greedy max-coverage / ZPD band / prereq gate / cold-start are all stdlib `set`/`dict`/`sorted` over 6A1's RAW edges/maps. `networkx` could model the prereq DAG but is FORBIDDEN (new infra). No Neo4j read is needed (entity sets reconstructed in-memory — §1). **Recommended: NO new package, NO Neo4j read.** Confirm.
8. **[INTERNAL SPLIT — the call to make] 3-unit v1 (defer persona) vs 4-unit.** **Recommended (STRENGTHENED this revision): the 3-unit v1 — ship 6A1/6A2/6A3 (the selection wedge that actually fires) and DEFER 6A4 persona conditioning to a follow-up WU.** Justification, both verified: persona is a near-total no-op in v1 (empty `opposes_map` → `misconception_code` ~never set) AND the live chat path has NO runtime leakage gate, so 6A4 spends the heaviest review budget on a path that won't execute. The 4-unit split (build 6A4 fourth, parallelizable off 6A1) remains valid IF the orchestrator wants persona in v1. **Under NO split is persona FUSED into selection-wiring.** Confirm: defer-persona (3-unit, recommended) vs build-6A4 (4-unit).
9. **[MIGRATION — confirm none] CONFIRMED no migration** for 6A1/6A2/6A3 (pure reads over 026 tables). The only candidate is the deferred persona-cache column (#5). `next_free_migration=030` unused. Confirm.
10. **[UI — note only, out of scope] No API-shape change, but a silent UX change behind the flag.** `/next` + chat response shapes are unchanged (`next.py:98-109`, `session_init.py:96-107`); only the content/order is personalized. Behind the flag the same student gets a personalized problem ORDER with no UI affordance explaining why — a known downstream item (a future "why this problem" surfacing) to capture in the WU-6A3 PR + `apollo.md` drift note, NOT to discover in the pilot. Student-UI stays out of 6A scope; flag only.
