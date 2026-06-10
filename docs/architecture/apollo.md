---
doc: ai-ta-backend/apollo
description: Apollo "student teaches the tutor" subsystem — GPT-4o parsing of student utterances into a typed Neo4j knowledge graph, a confused-learner LLM persona, and Done-time semantic diff grading (coverage + rubric + XP)
owns:
  - apollo/**
related:
  - ai-ta-backend/domain-data
  - shared/supabase
  - shared/product-context
last_verified: 2026-06-10
stub: false
---

# Apollo — Learning-by-Teaching Subsystem

Apollo inverts tutoring: the student explains a concept, Apollo plays a learner that knows
nothing, and at "I'm done teaching" the explanation is graded by diffing the student's
knowledge graph (KG) against an authored reference graph. State is split across two stores:
Postgres (Supabase) owns sessions/messages/attempts/progress; Neo4j owns the per-attempt KG
subgraph. The current architecture is "diff-at-Done v1" (spec: workspace
`docs/superpowers/specs/2026-06-09-apollo-diff-at-done-design.md`, outside this repo):
the per-turn loop is just **nodify + dumb reply**; the output filter and SymPy solver were
unwired (code still present), and grading is the Done-time LLM semantic diff.

## Module map and file landmarks

| Subpackage | Key files | Role |
|---|---|---|
| `apollo/` (root) | `api.py`, `errors.py`, `conftest.py` | FastAPI router (prefix `/apollo`), per-error exception handlers, process-singleton Neo4j client (`get_neo4j_client`/`close_neo4j_client`). `errors.py`: one named exception per failure mode, NO FALLBACK policy. `conftest.py`: live Neo4j fixture (skips if `NEO4J_URI` unset; test attempt_ids must be NEGATIVE — cleanup deletes `attempt_id < 0`). |
| `apollo/handlers/` | `chat.py`, `done.py`, `intent.py`, `lifecycle.py`, `negotiate.py`, `next.py`, `restart_problem.py`, `progress.py`, `history.py`, `olm_invite.py` | One module per endpoint group. `chat.py` = teaching turn + intent state machine; `done.py` = freeze→grade→XP; `intent.py` = cheap-LLM intent classifier + confirmation gate; `history.py` = bounded history (12-turn raw window + rolling LLM summary); `negotiate.py` = Negotiable-OLM moves (challenge/paraphrase/skip/trace); `olm_invite.py` = low-confidence clarification invite (env-flagged, currently not wired into the v1 chat turn). |
| `apollo/parser/` | `parser_llm.py`, `prompt_builder.py` | `parse_utterance()`: GPT-4o JSON-mode → typed Nodes+Edges. Triviality detection (length floor, ACK list, math-char regex, then cheap-LLM classifier at threshold 0.6). `prompt_builder.py` substitutes `{{concept_name}}` into the concept's `parser_prompt_template.md` (v1: parser captures the student's own form; canonical-symbol slots removed). |
| `apollo/ontology/` | `nodes.py`, `edges.py`, `graph.py` | The KG type system (single source of truth). 6 node types, 4 edge types, `KGGraph` aggregate with traversal helpers (`precedes_chain`, `topological_order`, `neighbors`, `merge`). |
| `apollo/knowledge_graph/` | `store.py` | `KGStore(db, neo)`: Neo4j CRUD (`write_nodes`/`write_edges`/`read_graph`/`walk_chain`/`delete_subgraph`), Postgres-backed freeze/unfreeze, `summarize_for_apollo()` bullet summary, and the three OLM negotiation moves with Postgres audit logging. |
| `apollo/overseer/` | `coverage.py`, `rubric.py`, `diagnostic.py`, `misconception.py`, `misconception_bank.py`, `concept_inference.py`, `problem_selector.py`, `xp.py` | Grading and orchestration brain. `coverage` = LLM semantic diff student-vs-reference; `rubric` = deterministic weighted grade (no LLM); `diagnostic` = LLM narration of the rubric verdict (does NOT re-grade); `concept_inference` = Hoot transcript → cluster_id; `problem_selector` = authored problem bank loader; `xp` = pure-function XP/levels; `misconception*` = embedding-matched misconception bank (pgvector 3072, migration 019; not wired into the v1 turn). |
| `apollo/solver/` | `sympy_exec.py`, `forward_chain.py`, `sufficiency.py`, `narrator.py` | SymPy forward-chaining solver + per-turn sufficiency signal. **Unwired in v1** — `done.py` sets `attempt.solver_trace = None` and never calls it; `sympy_exec.parse_zero_form` is still used by `store.py` for best-effort LaTeX rendering of equation nodes. |
| `apollo/agent/` | `apollo_llm.py`, `_llm.py`, `output_filter.py`, `leakage_judge.py` | `apollo_llm.draft_reply()` = the confused-learner reply (temp 0.7, strict ignorance prompt, 100k-token budget → `ContextOverflowError`). `_llm.py` = shared `cheap_chat`/`main_chat` helpers with per-call audit logging. `output_filter` + `leakage_judge` = two-stage anti-leak filter, **unwired in v1** (replaced by structural isolation; see conventions). |
| `apollo/persistence/` | `models.py`, `neo4j_client.py`, `progress_repo.py`, `attempt_history.py` | SQLAlchemy models for all `apollo_*` Postgres tables; async Neo4j driver wrapper (env-only credentials); XP repo (`load_progress`/`apply_xp`); cross-session re-attempt detection. |
| `apollo/schemas/` | `problem.py`, `dag.py`, `procedure.py`, `variable_map.py` | `Problem` (Pydantic) validates authored problem JSON (depends_on resolution, `uses_equations` must be real equation ids, procedure order 1..N contiguous) and derives the reference graph via `Problem.to_kg_graph(attempt_id)`. |
| `apollo/hoot_bridge/` | `session_init.py` | Hoot→Apollo handoff: transcript → concept inference → problem selection → session + attempt rows. |
| `apollo/subjects/` | `__init__.py`, `fluid_mechanics/concepts/bernoulli_principle/` | Filesystem concept registry: `canonical_symbols.json`, `normalization_map.json`, `parser_prompt_template.md`, `solver_hints.json`, `forbidden_named_laws.json`, `concept_dag.json`, `problems/problem_*.json`. `load_concept(subject_id, concept_id) -> ConceptDefinition`. Only one concept exists today. A DB-backed replacement (`apollo_subjects`/`apollo_concepts`/`apollo_concept_problems`, migration 018) is modeled in `persistence/models.py` but the runtime still reads the filesystem registry via `problem_selector.cluster_to_concept`. |
| `*/tests/` | per-subpackage `tests/` dirs + `apollo/tests/` e2e smokes | Most are **skip-marked**: "Tracked in claude_v3_checklist.md item 1; will be re-enabled in test-rewrite phase" (they predate the V3 KGGraph + Neo4j rewrite). |

## Public interfaces

Mounted in `server.py`: `app.include_router(apollo_router)` + `register_exception_handlers(app)`
(server.py:54, 645-646).

HTTP routes (all defined in `apollo/api.py`):

| Route | Handler | Notes |
|---|---|---|
| `POST /apollo/sessions/from_hoot` | `hoot_bridge.session_init.init_session_from_hoot` | Body `{student_id, hoot_transcript, difficulty="intro"}`. Ends any prior active session. 409 on `NoMatchingConceptError`/`PoolExhaustedError`. |
| `GET /apollo/sessions/{id}` | `handlers.lifecycle.handle_get_session` | Session + problem + full KG + messages. |
| `POST /apollo/sessions/{id}/chat` | `handlers.chat.handle_chat` | Body `{message}`. Returns `{apollo_reply, kg_entries_added, kg}` (+ optional `intent_pending` / `intent_executed`). |
| `POST /apollo/sessions/{id}/done` | `handlers.done.handle_done` | Freeze → grade → XP. 422 `review_required` if Done-gate fires. |
| `POST /apollo/sessions/{id}/retry` | `handlers.lifecycle.handle_retry` | Unfreeze, back to TEACHING. |
| `POST /apollo/sessions/{id}/next` | `handlers.next.handle_next` | New problem at chosen difficulty; advance (REPORT) or abandon (TEACHING/PROBLEM_REVEAL); blocked during SOLVING. |
| `POST /apollo/sessions/{id}/restart_problem` | `handlers.restart_problem` | Wipes attempt KG (Neo4j `DETACH DELETE`) + messages, same problem. |
| `POST /apollo/sessions/{id}/end` | `handlers.lifecycle.handle_end` | Marks ended; best-effort deletes every per-attempt subgraph. |
| `GET /apollo/progress/{student_id}` | `handlers.progress` | XP, level, title, next tier threshold. |
| `POST /apollo/sessions/{id}/kg/{entry_id}/challenge` / `paraphrase` / `skip`, `GET .../trace` | `handlers.negotiate` | Negotiable-OLM moves (P3). 404 `kg_entry_not_found` on stale entry ids. |

Key service entry points:
- `parse_utterance(utterance, *, concept: ConceptDefinition, attempt_id: int, model=None) -> tuple[list[Node], list[Edge]]` — raises `ParserCouldNotExtractError` only when a *non-trivial* utterance yields zero entries.
- `KGStore.write_nodes(*, attempt_id, nodes, source) -> int` / `write_edges(...)` / `read_graph(*, attempt_id) -> KGGraph` / `delete_subgraph(*, attempt_id)` / `summarize_for_apollo(*, attempt_id) -> str` / `freeze(session_id)` / `unfreeze(session_id)` / `mark_node_disputed` / `paraphrase_node` / `skip_node` / `get_node_trace`.
- `compute_coverage(student_graph: KGGraph, reference_graph: KGGraph) -> dict` (async; LLM).
- `compute_rubric(coverage, reference_nodes, *, misconception_scores=None) -> dict` (pure).
- `generate_diagnostic(*, coverage, reference_steps, problem_text, rubric, model=None) -> str` (LLM).
- `draft_reply(history, kg_summary, *, problem_text=None, model=None, history_summary=None) -> str`.
- `Problem.to_kg_graph(attempt_id) -> KGGraph`; `load_problem(path)`; `load_concept(subject_id, concept_id)`.
- `compute_xp_earned(*, overall_score, difficulty, is_reattempt) -> int`; `apply_xp(*, db, student_id, xp_delta)`.

Core types (`apollo/ontology/`):
- **Node types** (Pydantic discriminated union on `node_type`): `equation` (`symbolic`, `label`, `variables`), `condition` (`applies_when`), `simplification` (`applies_when`, `transformation`), `definition` (`concept`, `meaning`), `variable_mapping` (`term`, `symbol`), `procedure_step` (`action`, `purpose`). Common fields: `node_id`, `attempt_id`, `source` (`parser|reference|system`), `parser_confidence` (default 1.0), `status` (`ACCEPTED|DISPUTED|DUAL`), `student_belief` (str|None).
- **Edge types** (`EdgeType` StrEnum) with allowed-pair validation: `PRECEDES` (proc→proc; ordering — there is no `order` int on nodes), `USES` (proc→equation), `DEPENDS_ON` (any→any, no self-loops), `SCOPES` (simplification/condition→equation).
- **Coverage result**: `{per_step: {ref_id: "covered"|"missing"}, procedure_scores: {ref_id: 0..1}, confidences: {ref_id: 0..1}, negotiation_counts: {dual, disputed, paraphrased, skipped}}`.
- **Rubric result**: `{overall: {score, letter}, procedure|justification|simplification|misconception_corrected: {score, letter, present[, detected, resolved]}}`. Letter bands A+..F (`rubric.LETTER_BANDS`).
- **Done response**: `{rubric, diagnostic_narrative, coverage, progress: {xp_earned, xp_before/after, level_before/after, level_up, title_after, level_progress_pct, xp_to_next_level}, ...flat legacy XP fields}`.

## Main data flows

**(a) Student teaching turn — `POST /sessions/{id}/chat` (`handlers/chat.py`)**
1. Load session + latest `ProblemAttempt` for `session.current_problem_id`; resolve concept via `problem_selector.cluster_to_concept(sess.concept_cluster_id)` → `load_concept`.
2. Intent state machine (checklist item #5): if `sess.pending_intent == "done"`, treat the message as a confirmation (`detect_confirmation` regex). Affirmed → dispatch `handle_done` inline and return a chat-shaped envelope with `intent_executed`; rejected/ambiguous → clear pending, fall through. Otherwise classify intent (`classify_intent`, cheap LLM); a non-teaching intent at confidence ≥ 0.7 sets `pending_intent` and returns a confirmation prompt without parsing.
3. Parse: `parse_utterance` (GPT-4o JSON mode, temp 0.0) → typed nodes (`node_id = "stu_" + uuid12`) + edges. USES edges resolved from the LLM's `uses_equation_ordinals` self-references; PRECEDES chains consecutive procedure steps within the utterance.
4. Write nodes then edges via `KGStore` (edges MUST be written after nodes — Neo4j `MATCH...CREATE` silently drops edges whose endpoints don't exist). Writes are rejected with `SessionFrozenError` if phase ∈ {PROBLEM_REVEAL, SOLVING, REPORT}.
5. Reply: `load_windowed_history` (last 12 turns raw + rolling cheap-LLM summary refreshed every 8 older turns, persisted on `apollo_sessions.history_summary`) → `summarize_for_apollo` (bullet list of the KG) → `draft_reply` with ONLY the problem text + KG summary + history. v1: no sufficiency / misconception / OLM-invite signals and no output filter on this path.
6. Persist the (student, apollo) message pair in one commit; return reply + full KG dump.

**(b) "I'm done teaching" — `POST /sessions/{id}/done` (`handlers/done.py`)**
1. Read the per-attempt graph BEFORE freezing. If `APOLLO_DONE_GATE_ENABLED` (default off): flag parser-sourced entries with `status == DISPUTED` or `parser_confidence < 0.6` that have no row in `apollo_kg_negotiations` → raise `ReviewRequiredError` (422, FE renders a review modal).
2. `store.freeze(session_id)` (phase → PROBLEM_REVEAL), then phase → SOLVING.
3. Reference graph: `problem.to_kg_graph(attempt_id)` from the authored `reference_solution` in `problems/problem_*.json` — this is the v1 truth source (no retrieval/RAG).
4. `compute_coverage`: all matcher calls run concurrently (`asyncio.to_thread` + `gather`, no `return_exceptions`). Binary types (equation/condition/simplification) are batched one LLM call per type; procedure steps get one partial-credit call each (score ≥ 0.5 → "covered"). DUAL nodes with `student_belief` are graded against the student's wording. Transient failures retry 3x with backoff then raise `CoverageGradingError` (503 — grading unavailable, never a silent downgrade).
5. `compute_rubric` (pure): procedure 0.57 / justification 0.2375 / simplification 0.1425 / misconception_corrected 0.05; absent axes redistribute weight proportionally (no misconceptions → exact pre-P2.8 60/25/15). Misconception scores are read from `apollo_messages.metadata` — since the v1 chat path no longer writes per-turn metadata, this axis is normally absent.
6. `generate_diagnostic` (LLM) narrates the rubric verdict — hard-ruled not to contradict per-axis scores, formative tone.
7. Re-attempt detection (`attempt.result` already set, or `has_prior_graded_attempt` cross-session) → `compute_xp_earned` (score × difficulty multiplier 1.0/1.5/2.0 × 0.25 if re-attempt) → `apply_xp` updates `apollo_student_progress` and computes level (5 tiers, thresholds 0/300/800/1600/3000). Attempt gets `result="graded"`, `solver_trace=None`, `diagnostic_report={narrative, rubric, coverage}`; phase → REPORT.

**(c) Hoot handoff — `POST /sessions/from_hoot` (`hoot_bridge/session_init.py`)**
transcript → `infer_concept_cluster` (GPT-4o, must return one of `_AVAILABLE_CLUSTERS = ["fluid_mechanics"]` or 409) → `select_problem` (deterministic: sorted by id, unattempted at difficulty, else `PoolExhaustedError`) → end stale active sessions → create `ApolloSession` (phase TEACHING) + `ProblemAttempt` → return `{session_id, attempt_id, problem}`.

## Key dependencies

- **OpenAI** (`openai` sync client, constructed per call): models resolved per call site — parser/coverage/diagnostic/concept-inference use `MAIN_MODEL` (default `gpt-4o`); `draft_reply` uses `APOLLO_MODEL` > `MAIN_MODEL` > `gpt-4o`; cross-checks (intent, triviality, history summarizer, leakage judge) use `APOLLO_CHEAP_MODEL` (default `gpt-4o-mini`) via `agent/_llm.cheap_chat`. Every `_llm` call logs `{event: "llm_call", purpose, model, tokens}` for cost audit.
- **Neo4j** (`neo4j` async driver): `Neo4jClient.from_env()` requires `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` (Aura instance — see project memory). One driver per process, lazily built in `api.py`; `close_neo4j_client()` should be wired to app shutdown.
- **SymPy**: `solver/sympy_exec.py` (zero-form parsing, `solve_system`) — in v1 only the LaTeX display path in `store.py` exercises it at runtime.
- **Supabase Postgres tables** (via the repo-wide async SQLAlchemy `database.session.get_db_session`): `apollo_sessions`, `apollo_messages` (JSONB `metadata`, migration 020), `apollo_problem_attempts`, `apollo_student_progress`, `apollo_kg_negotiations` (migration 021), plus the DB-curriculum tables `apollo_subjects`/`apollo_concepts`/`apollo_concept_problems` (018) and `apollo_misconceptions` (019, vector(3072) embeddings). Partial unique index enforces one active session per student.
- **tiktoken** (best-effort): token estimate for `draft_reply`'s 100k budget; falls back to chars/4.
- **Env flags**: `APOLLO_DONE_GATE_ENABLED` (off), `APOLLO_OLM_INVITES_ENABLED` (off).

## Non-obvious conventions

- **NO FALLBACK policy**: every failure mode is a named exception in `apollo/errors.py` with its own HTTP status + `error_code` registered in `api.py` (`register_exception_handlers`). Never soft-fail a grade or silently truncate context. Exceptions: deliberately soft-failing helpers — triviality classifier (→ trivial), intent classifier (→ "teaching"), history summarizer (→ no summary), LaTeX render (→ None).
- **Temperature**: 0.0 for every classification/extraction/grading call; 0.7 only for `draft_reply` (Apollo's persona).
- **Ignorance prompt invariants** (`APOLLO_SYSTEM_PROMPT` in `agent/apollo_llm.py`): Apollo never names concepts/laws the student hasn't named, never corrects the student, paraphrases using the student's exact vocabulary, stays in role under prompt injection, answers "do you know X?" with "no", keeps replies 1-3 sentences, and is a *stuck student* (asks about the plan, not the subject). v1 enforces anti-leak **structurally** — the reply call sees only problem text + student KG + history — instead of via the (now unwired) `output_filter`/`leakage_judge` two-stage filter; the filter code and `forbidden_named_laws.json` per-concept stoplists (fuzzy match ratio 0.8, min len 5, possessive stripping) remain for reference.
- **Canonicalization stance (v1)**: the parser captures the student's *own form* — the "Extract → Canonicalize" redesign (`2026-06-09-apollo-kg-extract-canonicalize-handoff.md`) was **superseded** by diff-at-Done, which removed the consumers that needed canonical strings (filter + solver). Coverage grades "by meaning, not wording".
- **Neo4j shape**: every node gets its type label (`Equation`, `Condition`, ...) PLUS secondary `:_KGNode` label so one index covers all subgraph reads/cleanup. Cypher can't parameterize labels → one CREATE template per label/edge type. `None` properties are omitted (Neo4j has no NULL). `_record_to_node` defensively coerces unknown `status` to ACCEPTED and defaults missing `parser_confidence` to 1.0 (protects pre-P1/P3 legacy nodes from false-firing gates).
- **Confidence thresholds form a ladder**: cheap-LLM judges act at ≥ 0.6 (triviality, leakage); intent confirmation gate at ≥ 0.7; OLM clarification invite at parse-confidence < 0.7; Done-gate (final brake) at < 0.6; coverage binary matches with confidence < 0.5 are downgraded to "missing" and logged `coverage_uncertain`.
- **Negotiable OLM (P3)**: moves mutate the Neo4j node (`status`, `student_belief`) AND append an immutable audit row to `apollo_kg_negotiations`; the Done-gate clears once each flagged entry has ≥ 1 move. DUAL + `student_belief` redirects coverage to grade the student's wording (for procedure steps it *replaces* `action`).
- **Freeze discipline**: writes/moves check phase via Postgres before touching Neo4j; reads are allowed while frozen. `delete_subgraph` is the idempotent cross-DB cleanup contract (session end is best-effort with logged failures; orphans swept by a future janitor).
- **`summarize_for_apollo` format is contract**: the bullet shapes (`- equation (label): symbolic`, `- variable: term → symbol`, procedure steps ordered via the PRECEDES chain) are Apollo's vocabulary mirror — see `apollo/agent/LEAKAGE_POLICY.md`; don't change casually.
- **Hardcoded migration-window seams**: `hoot_bridge._AVAILABLE_CLUSTERS = ["fluid_mechanics"]` and `problem_selector._CLUSTER_TO_CONCEPT = {"fluid_mechanics": ("fluid_mechanics", "bernoulli_principle")}` are the only cluster→concept mappings; `apollo_sessions.concept_cluster_id` (TEXT) is legacy, `concept_id` (FK) is the target (migration 022 will drop the former). `sympy_exec._CANONICAL_SYMBOLS` is still a fluid-mechanics list.
- **Tests**: nearly all `*/tests/` modules are `pytest.mark.skip`-ed with "Tracked in claude_v3_checklist.md item 1; will be re-enabled in test-rewrite phase". Live Neo4j tests use negative attempt_ids (cleanup contract in `apollo/conftest.py`).

## Product context

Apollo implements **learning-by-teaching** (protégé effect): explaining a concept to a naive
agent forces the student to surface their own understanding; the system grades the *teaching*,
not answers to questions. Pedagogical commitments visible in code: Apollo never corrects the
student mid-session (misconceptions are detected silently and graded at Done); the diagnostic
report is formative ("you didn't walk Apollo through X"), never punitive; the Negotiable OLM
(challenge/paraphrase/skip, after Bull & Pain's Mr Collins and STyLE-OLM) lets the student
contest what the parser claims they said before being graded on it; XP/levels (Apollo
Apprentice → Archon) reward completion with reduced credit for re-attempts. Prefer smaller,
higher-leverage v1 shipments over feature-complete ones (project memory:
`feedback_apollo_pedagogy_choices.md`).

Active redesign working docs (read these before any non-trivial Apollo change):
- `ai-ta-backend/docs/apollo-redesign.md` — gap analysis (Gaps A–G) × academic literature map.
- `ai-ta-backend/docs/claude_v3_checklist.md` — itemized V3 flaw checklist; item 1 gates the test rewrite.
- `C:\Users\ultra\OneDrive\TA-test\docs\superpowers\specs\2026-06-09-apollo-diff-at-done-design.md` — the approved v1 design this code currently implements.
- `C:\Users\ultra\OneDrive\TA-test\docs\superpowers\specs\2026-06-09-apollo-kg-extract-canonicalize-handoff.md` — superseded EDC direction (diagnosis still valid).
