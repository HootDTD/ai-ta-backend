# Apollo V3 — Flaws Checklist & Redesign Targets

> Working doc for the ApolloV3 branch. Each item captures a concrete flaw in the current Apollo implementation, why it's wrong, where it lives, and what "fixed" looks like. Work through items one at a time — do not batch.

**Context for future chats:** Apollo is a "student teaches the tutor" subsystem in the AI-TA project. The student explains a concept to Apollo, Apollo plays a confused learner, and when the student clicks "I'm done teaching," a SymPy solver + LLM rubric grade the student's explanation. Backend lives at `ai-ta-backend/apollo/`, frontend at `ai-ta-student-ui/app/apollo/` and `ai-ta-student-ui/components/apollo/`. Current implementation is "Slice 0" / v2 — single problem, fluid-mechanics only. V3 is the generalization pass.

**Branch:** `ApolloV3` (note: at time of writing, local working copy was still on `master` — confirm checkout before editing).

---

## 1. The "Knowledge Graph" is not a graph

**Where:** `ai-ta-backend/apollo/knowledge_graph/store.py`, `ai-ta-backend/apollo/persistence/models.py:64-84` (`apollo_kg_entries` table).

**Current state:**
- Single Postgres table `apollo_kg_entries` with `(id, session_id, attempt_id, type, content JSONB, source)`.
- No edges table. No relations. No traversal. "Graph" is a misnomer — it's a typed bag of JSON facts scoped to `(session_id, attempt_id)`.
- 6 entry types: `equation`, `definition`, `condition`, `simplification`, `variable_mapping`, `procedure_step`.
- `procedure_step.order` is a plain int the parser assigns; duplicates and gaps are not detected.
- `uses_equations` in procedure_step is a list of equation **labels** (free-form strings) — no FK to actual equation entries. A typo silently breaks grading for everyone.

**What V3 needs:**
- [ ] Real graph model: entries become **nodes**; introduce an **edges** table (`apollo_kg_edges`) with typed relations: `uses`, `depends_on`, `simplifies`, `applies_to`, `defines`, `maps_to`.
- [ ] Procedure steps become a linked chain (edge type = `precedes`) instead of a bare `order` int.
- [ ] `uses_equations` becomes real FK edges to equation nodes — rename fails loudly, not silently.
- [ ] Add a KG traversal API (`walk_chain(from_node, edge_types)`) the solver + coverage can use instead of the current "group by type" blob read.
- [ ] Decide: keep Postgres (cheap, in-stack, good enough for per-attempt graphs) or introduce a graph DB. **Default recommendation: stay on Postgres with a proper edge table** — a graph DB is overkill for per-attempt scopes that never exceed ~100 nodes.
- [ ] Migration path: rewrite `store.py` `write_entries` / `read_kg` / `summarize_for_apollo` to operate over nodes+edges.

**Risk if not fixed:** V3 blocks cross-concept reasoning. You cannot express "this simplification applies only when this condition is true" without edges. Every new concept type will keep inflating the `content` JSONB blob.

---

## 2. Apollo LLM is called every turn with unbounded context

**Where:** `ai-ta-backend/apollo/handlers/chat.py:41-93`, `ai-ta-backend/apollo/agent/apollo_llm.py:48-66`.

**Current state:**
- Every student turn triggers **two** GPT-4o calls: parser (JSON-mode, temp 0) + Apollo reply (temp 0.7).
- Apollo call payload = `[system prompt, KG summary bullet list, FULL message history, new user message]`.
- History is loaded from DB every turn, never truncated, never summarized.
- KG summary is rebuilt from DB every turn (no cache).
- After ~20 turns on GPT-4o this starts burning money and approaches context limits.

**What V3 needs:**
- [ ] History management: rolling window + periodic summarization ("compact prior N turns into a system note") like main Hoot does.
- [ ] KG summary caching: recompute only when a KG write happens, not on every read.
- [ ] Move Apollo to a cheaper model where possible (Haiku 4.5 or Sonnet 4.6) — its job is intentionally dumb, Opus-level reasoning is wasted.
- [ ] Consider streaming for Apollo replies (UX win).
- [ ] Token budget enforcement with explicit error if exceeded, not silent truncation.

**Risk if not fixed:** Cost per session grows quadratically with session length; long sessions will 400 on context-length overflow.

---

## 3. Output filter stopword list is a toy

**Where:** `ai-ta-backend/apollo/agent/output_filter.py:16-27`.

**Current state:**
- Hardcoded `frozenset` of ~30 fluid-mechanics terms (`bernoulli`, `viscosity`, `laminar`, `pascal`, etc.).
- Word-level exact match. Paraphrases leak freely ("the dynamicist's equation about velocity and pressure" passes).
- Allowlist = any word the student has said OR any word in the KG. So if a student says "I'm not sure about viscosity," the term becomes legal and Apollo can lecture about it.
- No concept-scoping: words legal for one problem are legal for all problems in the session.
- Adding a new subject (circuits, thermo, biology, any non-STEM) means hand-curating a new list. Does not scale.

**What V3 needs:**
- [ ] Kill the hardcoded list. Replace with a **concept-scoped allowlist derived from the current problem's reference solution and the student's utterances**.
- [ ] Add an **LLM-as-judge leakage check** on Apollo's draft: "does this reply introduce any concept, equation name, or principle that the student has NOT mentioned in the conversation so far? Yes/no + offending phrase." Judge runs on a cheap model.
- [ ] Keep a very small deterministic pre-filter only for canonical giveaways (exact named-law strings) as a fast path.
- [ ] Log every rejection with draft + reason so we can tune thresholds.
- [ ] Design a 50+ adversarial probe suite (already promised in the v2 design doc, never written) and wire it into CI.

**Risk if not fixed:** Apollo V3 cannot support any subject beyond fluid mechanics without manual stopword curation. The entire "structural ignorance" claim collapses on paraphrase.

---

## 4. Parser heuristic silently drops natural prose

**Where:** `ai-ta-backend/apollo/parser/parser_llm.py:56-76` (`_is_non_trivial`).

**Current state:**
- Decides whether to raise `ParserCouldNotExtractError` when the LLM returns zero entries.
- Triggers: ≥10 chars, not in a 10-item ack set, contains math chars OR one of 14 fluid-mechanics keywords OR one of 11 plan markers.
- **False negatives:** "the stuff in the tube only goes one way and what comes in must equal what goes out" — plainly teaching continuity, hits zero triggers, silently dropped.
- **False positives:** "first, hi there!" trips `first` → if LLM extracts nothing → error shown to student.
- Keyword list is fluid-mechanics only. Any other subject gets no domain-keyword signal.

**What V3 needs:**
- [ ] Replace the hand-curated keyword heuristic with an **LLM classifier call** (or reuse the parser's own "did you try to teach me something?" signal) — cheap model, binary output + confidence.
- [ ] Separate "trivial ack" detection (cheap + deterministic is fine) from "teaching attempt" detection (needs semantics).
- [ ] Make the trigger set **concept-aware**: pull keywords from the current problem's reference solution, not a global list.
- [ ] On ambiguous cases (LLM says "maybe teaching" with low confidence), prompt the student gently instead of erroring: "I didn't quite catch what you were teaching me — could you rephrase?"

**Risk if not fixed:** Students who explain in plain prose (the majority of students outside STEM) get silently dropped. V3 is unusable outside math-heavy subjects.

---

## 5. `done` transition is button-only, no intent detection

**Where:** `ai-ta-backend/apollo/handlers/done.py:43`, `ai-ta-backend/apollo/handlers/chat.py` (no intent routing), `ai-ta-student-ui/components/apollo/ApolloChat.tsx` (button).

**Current state:**
- `handle_done` only runs when frontend explicitly POSTs `/sessions/{id}/done` — wired to a literal "I'm done teaching" button.
- `handle_chat` has no intent classification. If student types "ok that's everything, your turn" in chat, session never freezes, Apollo keeps asking confused questions, student is stuck.
- Same problem exists for implicit retry, next-problem, return-to-hoot intents — all button-only.

**What V3 needs:**
- [ ] Add an **intent classifier step** at the top of `handle_chat`: detects `{teaching, done, restart, next, return_to_hoot, help, off_topic}` from the student's utterance.
- [ ] Route explicit done intents to `handle_done` automatically, after a confirmation prompt ("It sounds like you're done teaching — shall I grade what you've shown me?").
- [ ] Keep the manual button as a secondary path (accessibility, users who want deterministic control).
- [ ] Log classification confidence; low-confidence utterances fall through to the normal teaching path.

**Risk if not fixed:** Chat-native UX is broken. Students will feel Apollo is "not listening" when they signal completion conversationally.

---

## 6. Hardcoded fluid-mechanics assumptions leak across the codebase

**Where:** multiple — enumerated below.

**Current state:**
- `parser_llm.py:20-54` — system prompt starts with "You extract structured KG entries from a student's explanation of a fluid-mechanics concept." Canonical symbols `P, rho, v, A, h, g, Q` are fluid-mechanics only. Subscript convention `P1/v1/A1/h1` is Bernoulli-specific.
- `done.py:68-75` — hardcoded `augmented_givens.setdefault("g", 9.81)` and a special case for `h1 == h2` simplification. Physics-domain leakage in the generic handler.
- `parser_llm.py:68-76` — fluid-mechanics keyword list.
- `output_filter.py:16-27` — fluid-mechanics stopword list (see item 3).
- `apollo/problems/AUTHORING.md` and `apollo/concepts/README.md` — only describe Bernoulli problems.
- `variable_normalization/fluid_mechanics.json` — the only normalization map.

**What V3 needs:**
- [ ] Introduce a **Subject / Concept Registry**: `subjects/<subject_id>/concepts/<concept_id>/...` with `canonical_symbols.json`, `normalization_map.json`, `stopwords.json` (if still needed), `parser_prompt_template.md`, `solver_hints.json`.
- [ ] Parser prompt becomes a template parameterized by concept.
- [ ] `done.py` augmentations move into the problem/concept definition (per-problem `augmented_givens`, per-concept `constants`).
- [ ] `problem_selector` already takes `cluster_id` — extend to `(subject_id, concept_id, difficulty)`.
- [ ] Add one non-fluid-mechanics concept as a proof-of-generalization during V3 (suggestion: basic kinematics, or a non-physics example like stoichiometry balancing — forces the abstractions).

**Risk if not fixed:** Every domain expansion requires grep-and-replace across 6+ files. V3 will accumulate fluid-mechanics scar tissue forever.

---

## 7. Procedure-step authoring is unvalidated & fragile

**Where:** `ai-ta-backend/apollo/schemas/problem.py`, `ai-ta-backend/apollo/problems/*.json`, `ai-ta-backend/apollo/overseer/coverage.py`.

**Current state:**
- `procedure_step.uses_equations` is a list of free-form strings referencing equation labels elsewhere in the reference solution.
- No validation that referenced labels exist. `uses_equations=["continuty"]` (typo) silently breaks the coverage matcher for every student on that problem.
- `order` field is a bare int — no validation that steps are 1..N with no gaps or duplicates.
- Pydantic schema only shape-checks; no cross-reference validation.

**What V3 needs:**
- [ ] Add a validator (Pydantic `model_validator` or separate CI check) that:
  - All `uses_equations` labels resolve to real equation entries in the same reference_solution.
  - Procedure step `order` forms a 1..N contiguous sequence.
  - `depends_on` references exist.
- [ ] Run this validator in CI against every file in `apollo/problems/`.
- [ ] Once KG becomes graph-based (item 1), these become real FK constraints in the DB for student-authored entries too.

**Risk if not fixed:** Silent grading bugs that are invisible until a student gets a wrong grade and complains.

---

## 8. Cross-module coupling on KG entry shape

**Where:** flows through `parser_llm.py` → `knowledge_graph/store.py` → `overseer/coverage.py` → `overseer/rubric.py` → `solver/narrator.py` → frontend components.

**Current state:**
- The `{type: str, content: dict}` shape is an informal contract passed through 5+ modules.
- Adding `procedure_step` required coordinated edits across parser prompt, store schema, coverage matcher, rubric weights, narrator templates, KG panel UI.
- No single source of truth for "what is a valid KG entry."
- No schema validation at the store boundary beyond `type in _KG_TYPES` — arbitrary content JSON can be written.

**What V3 needs:**
- [ ] Promote KG entry shapes to **Pydantic models** (one class per type, discriminated union).
- [ ] `store.write_entries` validates input against the union; invalid entries raise, not skip.
- [ ] Export TypeScript types from the Pydantic models for the frontend (use `datamodel-code-generator` or write them by hand and test for drift).
- [ ] Coverage/rubric/narrator consume the Pydantic objects, not raw dicts.
- [ ] Once graph (item 1) lands, entry types become node-type taxonomy with explicit parent class.

**Risk if not fixed:** Every new entry type keeps spreading across 5+ files with no compiler help. Drift between backend shape and frontend rendering is inevitable.

---

## 9. XP formula duplication between backend and frontend

**Where:** `ai-ta-backend/apollo/overseer/xp.py`, `ai-ta-backend/apollo/handlers/done.py:121-125`, `ai-ta-student-ui/components/apollo/ApolloReportPanel.tsx` (XP rendering).

**Current state:**
- Backend computes XP, returns `xp_earned` + `xp_before` + `xp_after` + `level_before` + `level_after`.
- Frontend **also** renders level-up animations and XP bars — implicitly assumes a level formula.
- If backend changes XP or level thresholds mid-session, frontend animations desync from returned values.
- No API contract versioning.

**What V3 needs:**
- [ ] Backend is the sole source of truth: return `level_progress_pct`, `xp_to_next_level`, `did_level_up` directly, not just raw numbers.
- [ ] Frontend only renders what the backend computed — no formula duplication.
- [ ] Version the Apollo API response envelopes (`{ version: "v3", data: ... }`) so future changes don't silently break old clients.

**Risk if not fixed:** Visual bugs on gamification that are hard to repro and erode student trust.

---

## 10. Coverage matching is N LLM calls with no retry or confidence gating

**Where:** `ai-ta-backend/apollo/overseer/coverage.py` (file contents not re-verified this session — confirm before editing).

**Current state (from prior exploration):**
- Each reference_solution entry gets its own LLM call to match against student KG.
- Soft-fails to "missing" on exception — a transient API hiccup silently downgrades the grade.
- No retry, no caching, no confidence threshold.

**What V3 needs:**
- [ ] Retry with exponential backoff on transient errors. Do not silently downgrade on network failure — raise a named error and let the UI say "grading unavailable, try again."
- [ ] Add confidence scores to matches; below-threshold matches flagged for review instead of binary covered/missing.
- [ ] Consider batching: one LLM call that takes all reference entries + full KG and returns per-entry verdicts, instead of N calls.
- [ ] Cache per `(attempt_id, reference_entry_hash, kg_hash)` — same inputs should not re-call.

**Risk if not fixed:** Grades are noisy and network-dependent. Students get different grades for the same work.

---

## 11. "Graph" summary for Apollo leaks student's raw phrasing back

**Where:** `ai-ta-backend/apollo/knowledge_graph/store.py:78-96` (`summarize_for_apollo`).

**Current state:**
- Bullet list fed to Apollo as a system message.
- Uses `eq.get('label')` — which is whatever free-text label the parser extracted from the student (e.g. "my pressure rule"). Fine.
- BUT: `variable_mapping` bullets expose `term → symbol` pairs. If Apollo later mentions a symbol, the filter lets any word from `term` through as allowed vocabulary. Student says "the squishiness thing → k" → Apollo can now freely say "squishiness."
- This is probably fine (the point is to mirror the student's words), but worth auditing against the structural-ignorance goal.

**What V3 needs:**
- [ ] Audit the summary format vs. the leakage policy. Decide explicitly: does Apollo know the student's variable mapping, or only the symbols?
- [ ] Document the policy in `apollo/concepts/README.md`.

**Risk if not fixed:** Subtle, but if the leakage policy is unclear, item 3's LLM-judge replacement can't be written correctly.

---

## Suggested execution order

1. **Item 6** (de-hardcode fluid-mechanics) — unblocks everything else; forces the abstractions that later items depend on.
2. **Item 8** (Pydantic KG entry models) — prerequisite for a clean item 1.
3. **Item 1** (real graph model with edges) — the big architectural lift.
4. **Item 7** (problem-file validation in CI) — cheap, catches authoring bugs immediately.
5. **Item 3** (LLM-judge output filter) — unblocks any second subject.
6. **Item 4** (LLM-based parser triviality) — completes the de-hardcoding story.
7. **Item 5** (intent classifier in chat) — UX unlock, independent of the above.
8. **Item 2** (history management & model downgrade) — cost control; do after behavior is stable.
9. **Item 10** (coverage retry & batching) — reliability polish.
10. **Item 9** (XP source-of-truth) — small, safe cleanup.
11. **Item 11** (summary leakage audit) — documentation + minor tweak.

---

## Quick reference — file map

| Concern | File |
|---|---|
| KG schema | `ai-ta-backend/apollo/persistence/models.py` |
| KG store | `ai-ta-backend/apollo/knowledge_graph/store.py` |
| Parser (LLM + heuristic) | `ai-ta-backend/apollo/parser/parser_llm.py` |
| Apollo reply LLM | `ai-ta-backend/apollo/agent/apollo_llm.py` |
| Output filter | `ai-ta-backend/apollo/agent/output_filter.py` |
| Chat handler | `ai-ta-backend/apollo/handlers/chat.py` |
| Done handler | `ai-ta-backend/apollo/handlers/done.py` |
| Coverage / rubric / diagnostic | `ai-ta-backend/apollo/overseer/` |
| Solver (SymPy) | `ai-ta-backend/apollo/solver/` |
| Problem schemas | `ai-ta-backend/apollo/schemas/` |
| Problem bank | `ai-ta-backend/apollo/problems/` |
| Hoot integration | `ai-ta-backend/apollo/hoot_bridge/session_init.py` |
| Frontend components | `ai-ta-student-ui/components/apollo/` |
| Frontend pages | `ai-ta-student-ui/app/apollo/` |
| Frontend API proxies | `ai-ta-student-ui/app/api/apollo/` |
