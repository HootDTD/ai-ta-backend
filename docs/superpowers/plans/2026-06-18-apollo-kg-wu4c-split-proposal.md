# WU-4C split proposal — wiring the graph-simulation grading chain into the live Done endpoint

**Date:** 2026-06-18
**Author:** scoping pass (autonomous stacked-PR build)
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §6.4 (Done pipeline), §6.7 (calibration / shadow gate), §6.8 (constrained diagnostics), §6.3/§6 (rubric mapping), §6.6/§6.1 (named errors → HTTP handlers), §8A (runtime curriculum resolution)
**Status:** PROPOSAL ONLY — no code/tests/source touched. Divides the unit WU-4C into sequential sub-units with clean seams; per-sub-unit TDD plans are a later step.
**Templates:** `docs/superpowers/plans/2026-06-17-apollo-kg-wu4a-split-proposal.md`, `docs/superpowers/plans/2026-06-17-apollo-kg-wu4b-split-proposal.md`

---

## 0. What WU-4C is (and the sibling boundaries it must not cross)

WU-4C is the **orchestration unit that wires the already-built graph-simulation
grading chain into the LIVE `apollo/handlers/done.py`**. Everything it consumes
is DONE: the pure score core (`apollo/retired graph comparator/`, WU-4A), the resolver
(`apollo/resolution/`, WU-3C2), and the downstream grading layer
(`apollo/grading/`, WU-4B — audit, abstention, §6.5 events, runs/findings
persistence). WU-4C writes **no new grading algorithm**; it re-orchestrates the
Done route to call them, sources their inputs from the live request context,
maps their findings into the existing rubric, registers their named errors as
HTTP handlers, and adds the §6.7 shadow harness + §6.8 constrained diagnostics.

It owns §6.4 **wiring steps 1–9, 13, 18** plus step 15 *invocation* (it calls
WU-4B3's `persist_comparison_run`; it does not re-implement persistence). This
is a LIVE endpoint serving real student grades — **not breaking the
student-facing grade is the top risk** (§4.1).

Phase-4 / Phase-5 boundary (from the roadmap, restated and HELD):

- **WU-4A (DONE)** = pure score-math core in `apollo/retired graph comparator/`:
  `validate_student_graph` / `validate_reference` / `build_problem_candidates`
  / `build_student_canonical` / `build_reference_canonical` /
  `grade_attempt(student_canonical, reference_graph) -> GradeResult`. NO IO.
- **WU-4B (DONE)** = `apollo/grading/`: `build_audited_grade` (§6.4 step 12
  audit + step 14 abstention), `convert_findings_to_events` (§6.5, step 16),
  `persist_comparison_run` (step 15), `compute_normalization_confidence`,
  `reference_graph_hash`, `build_opposes_map`. Produces in-memory events;
  persists runs+findings ONLY.
- **WU-4C (this unit)** = `done.py` re-orchestration (steps 1–9, 13, 18), the
  resolver call + RESOLVES_TO write (steps 5/7, via WU-3C2's
  `resolution_store`), `turn_order` sourcing from `apollo_messages.turn_index`,
  rubric mapping (findings/sub-scores → `compute_rubric` input), §6.7
  shadow-mode harness, §6.8 constrained diagnostics, and HTTP-registration of
  the five named errors. Calls WU-4B3's persist; does NOT write
  `apollo_mastery_events` / `apollo_learner_state`.
- **WU-5A (later)** = §6.4 **step 17** only — the 3-state Bayesian filter
  update, writing `apollo_learner_state` + `apollo_mastery_events` atomically
  from the `LearnerEvent` tuple WU-4C produces (via `convert_findings_to_events`)
  and the `turn_order` WU-4C sources.

### Where the lines fall (decided, with spec citations)

1. **Step 16 (`convert_findings_to_events`) is INVOKED by WU-4C, step 17
   (belief update + event persistence) is WU-5A — CONFIRMED.** The prompt's
   step list assigns 4C "steps 1–9, 13, 18". Step 16 produces in-memory events
   from the audited grade; whether WU-4C *calls* `convert_findings_to_events`
   itself or leaves that to WU-5A is a real seam decision. **Recommendation: in
   v1 SHADOW mode WU-4C does NOT call step 16/17 on the live path at all** — it
   computes + persists the comparison run/findings (steps 1–15) and generates
   the diagnostic (step 18), but does not produce or persist events (no Layer-3
   write). Step 16's `convert_findings_to_events` is wired by WU-5A *together*
   with step 17, because the two are bound by the §3 all-or-nothing transaction
   ("events + belief update commit in one all-or-nothing Postgres transaction")
   and `convert_findings_to_events` needs the same `turn_order` the belief
   update consumes. WU-4C *sources* `turn_order` and proves it is queryable;
   WU-5A consumes it. This keeps the §6.7 calibration gate honest: until it
   opens, "events are emitted diagnostic-only/low-confidence" and Layer-3 is
   never written, so deferring the event call to WU-5A costs nothing in v1.
   *(If a later decision wants the events persisted for shadow analysis before
   WU-5A, that is an additive WU-5A concern — WU-4C still must not write
   `apollo_mastery_events`, §4.4.)*

2. **§6.7 shadow gate is WU-4C — CONFIRMED (it was deferred here from WU-4B).**
   WU-4B's split memo §0.1 explicitly hands §6.7 to WU-4C: "the shadow-comparison
   wiring itself is WU-4C." §6.7 says `compute_coverage` (the OLD LLM path) "is
   not deleted… it runs in **shadow** alongside the simulator." This is Done-route
   orchestration: which path produces the student-facing grade, which runs
   alongside for calibration, what gets persisted for the calibration metrics.
   WU-4C owns it.

3. **§6.8 constrained diagnostics is WU-4C — CONFIRMED.** Step 18 (generate
   diagnostic from structured findings + quoted spans only, with a post-check)
   is the last Done step and was listed in WU-4C's scope by both prior memos.
   The repo already has `apollo/overseer/diagnostic.py::generate_diagnostic`
   consuming the OLD `coverage` map — WU-4C must either adapt it to consume
   `GradeResult.findings` + spans (input = findings only, never free transcript)
   or add a new findings-driven diagnostic with the §6.8 post-check.

4. **Rubric mapping is WU-4C — CONFIRMED.** §6.4 ("Rubric mapping") names this
   an "explicit task, not discovered mid-implementation": `compute_rubric`
   today consumes the OLD coverage verdict map; WU-4C must define the
   findings/sub-scores → `compute_rubric` input mapping (or adapt the rubric to
   consume sub-scores directly). In SHADOW v1 (§4.1) the student-facing rubric
   still comes from the OLD path, so this mapping is computed for the *shadow*
   run and persisted/compared, not yet shown to the student — but it must exist
   so the calibration gate can compare.

5. **Pipeline ownership (§6.4):** WU-4C owns steps **1–9, 13, 18** + the
   step-15 *call*. Step 10/11/13-compute = WU-4A2 (`grade_attempt`); step 12 =
   WU-4B1 (`build_audited_grade`); step 14 = WU-4B1; step 15 = WU-4B3
   (`persist_comparison_run`); step 16 = WU-4B2 (`convert_findings_to_events`,
   invoked by WU-5A in v1, §0.1); step 17 = WU-5A.

---

## 1. Ground truth discovered (load-bearing facts that shape the split)

Verified against the real code/migrations/ORM, not assumed (file:line):

- **The live Done handler today (`apollo/handlers/done.py`).** `handle_done`
  (line 188) currently: loads the session + problem (`_find_problem` → the
  parsed `Problem`, lines 181/197), reads the pre-freeze graph
  (`store.read_graph(attempt_id=...)`, line 215), runs the P3.6 Done-gate
  (line 216, flag-gated), `store.freeze` (221), derives the reference graph via
  `problem.to_kg_graph(attempt_id=...)` (224), commits SOLVING phase (226),
  then grades through the **OLD** path: `compute_coverage(student_graph,
  reference_graph)` (line 229, `apollo/overseer/coverage.py`, LLM semantic
  diff) → `compute_rubric(coverage, reference_graph.nodes, misconception_scores)`
  (line 239, `apollo/overseer/rubric.py`) → `generate_diagnostic(coverage=…,
  reference_steps=…, problem_text=…, rubric=…)` (line 245,
  `apollo/overseer/diagnostic.py`) → XP (`compute_xp_earned`, 262) → commits
  `attempt.result="graded"` + `diagnostic_report` + REPORT phase (268–276) →
  `apply_xp` (278) → `store.stamp_graded_at` (296, the post-commit retention
  write). The student-facing return (298–322) is `{rubric, diagnostic_narrative,
  coverage, progress, xp_*}`. **This is the LIVE contract WU-4C must not break.**

- **The OLD grading brain stays (§6.7).** `compute_coverage`
  (`apollo/overseer/coverage.py`) and `compute_rubric`
  (`apollo/overseer/rubric.py`) are NOT deleted — WU-4C keeps them as the
  v1 student-facing path and runs the new chain in shadow. `done.py` raises
  `CoverageGradingError` (already HTTP-registered, 503) on transient coverage
  failure — that contract is unchanged.

- **The NEW chain's entry points (all DONE, signatures verified).** WU-4C must
  call, in order:
  1. `build_problem_candidates(problem: dict, misconceptions: dict, *,
     canon_key_by_canonical_key: dict[str,int]) -> ProblemInputs`
     (`apollo/retired graph comparator/problem_inputs.py:40`).
  2. `resolve_attempt(student_graph, candidates, *, llm_adjudicator=None,
     fuzzy_threshold=0.9, symbolic_mappings=None) -> ResolutionResult`
     (`apollo/resolution/resolver.py:132`). Note `llm_adjudicator` defaults
     None (CI-safe); the live path passes
     `apollo.resolution.adjudication.main_chat_adjudicator`.
  3. `validate_student_graph(...)` / `build_student_canonical(student_graph,
     resolution) -> CanonicalGraph` (`apollo/retired graph comparator/canonical.py:167`);
     `build_reference_canonical(problem: dict) -> ReferenceGraph`
     (`canonical.py:100`, which calls `validate_reference` → raises
     `ReferenceGraphInvalidError`).
  4. `grade_attempt(student_canonical, reference_graph) -> GradeResult`
     (`apollo/retired graph comparator/core.py:78`).
  5. `build_audited_grade(grade, *, transcript, resolution, student_nodes,
     candidates, reference_invalid, audit_fn) -> AuditedGrade`
     (`apollo/grading/audited_grade.py:165`).
  6. `compute_normalization_confidence(audited_grade, resolution) -> float`
     (`apollo/grading/normalization_confidence.py:38`) and
     `reference_graph_hash(reference_graph) -> str`
     (`apollo/grading/reference_hash.py:38`).
  7. `persist_comparison_run(db, *, attempt_id, user_id, search_space_id, grade,
     audited, normalization_confidence, reference_graph_hash) -> int`
     (`apollo/grading/persistence.py:197`) — does NOT commit (caller owns the
     txn boundary, the docstring is explicit).
  8. RESOLVES_TO write (step 7): `apollo/knowledge_graph/resolution_store.py`
     (`write_resolution`-style seam; idempotent MERGE; raises
     `ResolutionUnavailableError` on infra failure).
  9. (WU-5A, NOT 4C v1) `convert_findings_to_events(audited_grade, *,
     opposes_map, turn_order)` (`apollo/grading/events.py:68`) +
     `build_opposes_map(candidates)` (`apollo/grading/opposes.py:16`).

- **CRITICAL — the validated `Problem` cannot feed the new chain; WU-4C must
  pass the RAW payload dict.** `build_reference_canonical(problem: dict)` reads
  `problem["declared_paths"]` and each step's `step["entity_key"]`
  (`canonical.py:114,154`); `build_problem_candidates` reads
  `problem["symbolic_mappings"]` (`problem_inputs.py:59`). But the runtime
  `Problem` pydantic schema (`apollo/schemas/problem.py:48`) declares ONLY
  `id/concept_id/difficulty/problem_text/given_values/target_unknown/
  reference_solution`, and `ReferenceStep` (line 40) declares only
  `step/entry_type/id/content/depends_on` — **no `entity_key`, no
  `declared_paths`, no `symbolic_mappings`.** `Problem.model_validate(payload)`
  silently drops those keys, so `Problem.model_dump()` does NOT round-trip them.
  The on-disk `problem_01.json` DOES carry `declared_paths` (line 14),
  `symbolic_mappings` (line 26), and per-step `entity_key` (lines 45+), and so
  does the DB `apollo_concept_problems.payload`. **WU-4C must source the raw
  `ConceptProblem.payload` dict** (not the parsed `Problem`) for the new chain,
  while still using `Problem` for the OLD path (`to_kg_graph`,
  `reference_solution` for the diagnostic). `problem_selector.list_problems_for_concept`
  (`apollo/overseer/problem_selector.py:25`) currently returns parsed `Problem`s
  — WU-4C needs a parallel raw-payload read (a new `select(ConceptProblem.payload)`
  filtered to the chosen `problem_code`).

- **`canon_key_by_canonical_key` source (the candidate-set arg).**
  `build_problem_candidates` needs `canon_key_by_canonical_key: dict[str,int]`
  (canonical_key → `apollo_kg_entities.id`). That map is the entity-link layer
  populated by `apollo/knowledge_graph/canon_projection.load_entity_specs(db, *,
  search_space_id=…, concept_id=…)` (`canon_projection.py:117`), which is
  course-scoped (forbids a course-blind read, line 138). WU-4C builds the map
  from those rows by `canonical_key → id` for the session's
  `search_space_id`/`concept_id`. (When an entity link is absent, the candidate's
  `canon_key` is `-1` → no Layer-1 surrogate — tolerated; persistence stores
  `entity_id=None`, `persistence.py:130`.)

- **`turn_order` sourcing (deferred to WU-4C by WU-4B2).** `events.py:68` takes
  `turn_order: Mapping[str, int]` (node_id → turn position), INJECTED by WU-4C
  (module docstring lines 10–12: "supplied by WU-4C from
  `apollo_messages.turn_index`/Neo4j `created_at`"). The `Message` ORM
  (`apollo/persistence/models.py:242`) carries `attempt_id` (247), `role` (253),
  `turn_index` (255, NOT NULL), and `message_metadata` (259). The in-memory
  `Node` carries no `created_at`. `done.py` already reads messages by
  `attempt_id` ordered by `turn_index` (`_attempt_misconception_scores`,
  lines 154–159). **The join key gap:** `turn_order` is keyed by KG **node_id**,
  but `apollo_messages` rows are keyed by `turn_index`, not node_id — the bridge
  is which turn a node was extracted from. Resolved in §4.3.

- **The five named errors exist but are NOT HTTP-registered.** `apollo/errors.py`
  declares `ResolutionUnavailableError` (line 169),
  `TranscriptAuditUnavailableError` (189, docstring: "named-but-not-HTTP-registered
  here; WU-4C registers the handler"), `ResolutionInvalidOutputError` (211),
  plus `apollo/retired graph comparator/validator.py` declares `StudentGraphInvalidError` /
  `ReferenceGraphInvalidError`. `apollo/api.py::register_exception_handlers`
  (line 402) registers 12 handlers via `app.add_exception_handler`; the pattern
  is a per-error async handler returning `JSONResponse(status_code=…,
  content=_err_payload(code, msg, **extra))` (lines 264–417). WU-4C adds five
  handlers + five registrations following that exact pattern. `RetentionError`
  (line 153) is the prior-art for "named, NO-FALLBACK, does not void the grade."

- **Tables + ORM exist (migration 026); next free number is 028.** Migrations
  top out at `027_apollo_drop_concept_cluster_id.sql` (verified `ls`), so 028 is
  the next free number — but WU-4C needs **NO migration** (runs/findings tables,
  `learner_update_pending`, and the learner tables all shipped in 026; the
  `Problem`-schema gap in §4.6 is data/code, not DDL). `learner_update_pending`
  (`models.py:280`) is the §6.4 cross-store retry flag WU-4C sets on any
  step-5-onward failure.

- **The Done transaction is staged (no cross-store atomicity).** §6.4 binds:
  (1) the grade (rubric/XP) commits FIRST in Postgres; (2) Neo4j writes
  (RESOLVES_TO) are idempotent MERGEs; (3) the comparison run+findings commit in
  one Postgres txn (supersede); (4) events+learner update in one all-or-nothing
  txn (WU-5A). Any failure from step 5 onward sets `learner_update_pending=true`
  and the retry re-runs FROM resolution idempotently. `persist_comparison_run`
  does not commit (it `flush()`es) — WU-4C owns the commit boundary for step 15.

---

## 2. Recommendation

**Split WU-4C into TWO sub-units (WU-4C1, WU-4C2). No third.**

Rationale: there is one clean seam — **(a) get the new chain RUNNING in shadow
behind a flag, persisting runs/findings and proving the inputs are sourceable,
without ever touching the live student grade; then (b) layer the
calibration-comparison harness + the findings-driven constrained diagnostic on
top.** Each side is independently green at ≥95% patch coverage, and the two have
different test-infra regimes (4C1 needs real-PG + real-Neo4j integration for
the `done.py` wiring; 4C2 is mostly pure logic + LLM-mock for the diagnostic),
so isolating them keeps each on one dominant regime and isolates the single
highest-risk surface — the live-Done rewire — in its own review cycle.

- **WU-4C1 — Done re-orchestration in SHADOW** (steps 1–9, 13, 15 + RESOLVES_TO
  + turn_order sourcing + error registration). Wires the full
  resolve→canonicalize→grade→audit→persist chain into `handle_done`, **behind a
  shadow flag**, computing and persisting the comparison run/findings ALONGSIDE
  the unchanged OLD `compute_coverage`/`compute_rubric` student-facing grade.
  Registers the five named errors as HTTP handlers. Sources `turn_order` and
  proves it queryable (for WU-5A). The student-facing response is byte-identical
  when the chain succeeds; chain failures set `learner_update_pending` and never
  void the grade.
- **WU-4C2 — calibration harness + constrained diagnostics** (§6.7 + §6.8 +
  rubric mapping). Computes the §6.7 per-run calibration metrics (shadow
  simulator vs old LLM diff), defines the findings/sub-scores → `compute_rubric`
  mapping (computed for the shadow run), and replaces/adapts the diagnostic to
  be findings-driven with the §6.8 post-check. The flag that promotes the new
  grade from shadow to live lives here but stays OFF in v1.

Why this seam and not "wire everything live in one unit": going live in one shot
on a LIVE student-grade endpoint is exactly the §4.1 risk. The shadow boundary
is the natural safety seam the spec itself draws (§6.7). 4C1 proves the chain
runs and persists without changing a single student-visible byte; 4C2 builds the
evidence (calibration metrics) that would *justify* flipping to live — which is
a human/calibration decision, not a code change inside this build.

A three-way split (wire / shadow-metrics / diagnostics) was considered and
rejected: the §6.7 metrics are computed from the same run 4C1 persists and the
§6.8 diagnostic reads the same `GradeResult.findings`, so metrics+diagnostics
share the entire substrate and a `done.py` re-entry; splitting them would force
two sub-units to re-establish the Done-route context for thin payloads. Keeping
"new live behavior" (4C1) vs "calibration + feedback surfacing" (4C2) is the
only seam where each side is a complete, independently testable artifact.

---

## 3. Sub-units

### WU-4C1 — Done re-orchestration in SHADOW

**One-line:** Wire the full graph-simulation chain
(resolve → RESOLVES_TO → canonicalize → grade → audit/abstain → persist run+findings)
into `handle_done` behind a shadow flag, sourcing every input from the live
request context, registering the five named errors, and proving `turn_order`
is queryable — all without changing the student-facing grade.

**SEAM (owns vs WU-4C2):** WU-4C1 owns everything that *runs the chain and
persists its run/findings* on the Done route. It produces the persisted
`run_id` + the `AuditedGrade` + the `LearnerEvent`-ready inputs (`opposes_map`,
`turn_order`). It does NOT compute calibration metrics, does NOT define the
rubric mapping, and does NOT change the diagnostic (those are 4C2). It does NOT
write `apollo_mastery_events`/`apollo_learner_state` (WU-5A). Handoff artifacts:
the persisted comparison run + a structured "shadow result" object (the
`GradeResult`, `AuditedGrade`, `normalization_confidence`, `reference_graph_hash`,
`turn_order`, `opposes_map`) that 4C2's metrics and WU-5A's belief update read.

**Scope — files to touch/create:**

- `apollo/handlers/done.py` (EDIT) — insert the new chain after `store.freeze`
  + the grade/XP commit. Sequence: load raw `ConceptProblem.payload` dict (§1
  critical finding); load `canon_key_by_canonical_key` via
  `canon_projection.load_entity_specs(db, search_space_id=…, concept_id=…)`;
  `build_problem_candidates`; `resolve_attempt(... , symbolic_mappings=inputs.
  symbolic_mappings, llm_adjudicator=main_chat_adjudicator)`; write RESOLVES_TO
  (resolution_store) — idempotent; `validate_student_graph`;
  `build_student_canonical`; `build_reference_canonical` (raw dict);
  `grade_attempt`; read the transcript + per-turn parser confidences; read the
  student turns for `transcript`; `build_audited_grade`;
  `compute_normalization_confidence`; `reference_graph_hash`;
  `persist_comparison_run`; commit the run txn. **All of it behind
  `[retired A7 shadow switch]` (default OFF in prod, ON in test);** when
  off, `done.py` is byte-identical to today. The OLD `compute_coverage`/
  `compute_rubric`/`generate_diagnostic`/XP path is UNCHANGED and still produces
  the student-facing return.
- `apollo/handlers/done_grading.py` (NEW, small) — the new-chain orchestration
  extracted out of `done.py` so the handler stays under the 800-line ceiling and
  the chain is unit-testable in isolation:
  `retired graph simulation(db, neo, *, attempt, sess, student_graph, problem_payload)
  -> retired shadow result | None`. Returns None / sets `learner_update_pending` on
  any step-5+ infra failure (NO-FALLBACK, never voids the grade). This is where
  the §6.4 staged-transaction discipline lives.
- `apollo/handlers/done_turn_order.py` (NEW, small) — `build_turn_order(db, *,
  attempt_id, student_graph) -> dict[str,int]` (§4.3): join evidence node-ids to
  `apollo_messages.turn_index`. Pure-ish DB read; the only WU-4C-owned new query.
- `apollo/api.py` (EDIT) — add five async handlers + five `add_exception_handler`
  registrations for `ResolutionUnavailableError` (503, NO-FALLBACK, mirrors
  `coverage_grading_handler`), `TranscriptAuditUnavailableError` (503),
  `ResolutionInvalidOutputError` (500/502 — hallucination, internal),
  `StudentGraphInvalidError` (422), `ReferenceGraphInvalidError` (409/422 —
  blocks grading, §6.6). Follow the `_err_payload` pattern (lines 258–262).
- `apollo/errors.py` (EDIT, tiny) — none required structurally; the five classes
  exist. (Confirm `StudentGraphInvalidError`/`ReferenceGraphInvalidError` are
  importable into `api.py` from `apollo.retired graph comparator.validator`.)

**Public API exposed for WU-4C2 / WU-5A:**

```python
# apollo/handlers/done_grading.py
@dataclass(frozen=True)
class retired shadow result:
    run_id: int
    grade: GradeResult
    audited: AuditedGrade
    normalization_confidence: float
    reference_graph_hash: str
    opposes_map: Mapping[str, str]
    turn_order: Mapping[str, int]
    # the inputs WU-5A's belief update + 4C2's metrics both consume
retired graph simulation(db, neo, *, attempt, sess, student_graph, problem_payload) -> retired shadow result | None
# apollo/handlers/done_turn_order.py
build_turn_order(db, *, attempt_id, student_graph) -> dict[str, int]
```

**Dependencies:** ALL of WU-4A (`apollo.retired graph comparator`), WU-4B
(`apollo.grading`), WU-3C2 (`apollo.resolution` + `resolution_store`), WU-3C1
(`canon_projection.load_entity_specs`), the `KGStore.read_graph` + `Message` /
`ConceptProblem` ORM. No new external deps.

**Migration?** **No.** 026 shipped runs/findings + `learner_update_pending`;
027 dropped `concept_cluster_id`. Next free is 028 but unused.

**Real-infra tests?** **YES — required, the heaviest in WU-4C.** `done.py`
wiring needs: real-PG (Testcontainers `tests/database` gate) for
`persist_comparison_run` round-trip inside the Done txn + supersede on retry +
`learner_update_pending` set on failure + the `apollo_messages` turn_order
query; real-Neo4j (`apollo/knowledge_graph` gate) for `read_graph` +
RESOLVES_TO MERGE idempotency. The resolver's LLM adjudicator and the
transcript audit are MOCKED (inject `llm_adjudicator=None`/stub `audit_fn`) so
no live LLM. **This sub-unit trips BOTH the `tests/database` AND the Neo4j
gates** — the only WU-4C sub-unit that does.

**Key behavioral tests:**
- Shadow flag OFF → `done.py` return is byte-identical to today (regression
  guard; the live grade is untouched).
- Shadow flag ON, happy path → comparison run + findings persisted, OLD grade
  STILL the student-facing one, no `apollo_mastery_events` written (assert the
  table is empty — the 4C/5A boundary).
- `ReferenceGraphInvalidError` from `build_reference_canonical` → 409/422, grade
  NOT voided (the OLD grade still committed first), `learner_update_pending`
  semantics correct.
- `ResolutionUnavailableError` (resolver/RESOLVES_TO infra fail) → 503,
  `learner_update_pending=true`, grade committed (NO-FALLBACK, mirrors
  `RetentionError`).
- `TranscriptAuditUnavailableError` is caught INSIDE `build_audited_grade`
  (never reaches the handler on the normal path) → suppress-all-missing
  abstention; the HTTP handler exists only for a defensive re-raise.
- Retry after a mid-pipeline crash → `persist_comparison_run` supersedes (no
  UNIQUE crash), RESOLVES_TO MERGE idempotent.
- `build_turn_order` maps node-ids to `turn_index` correctly; a node with no
  message linkage → absent key (→ `_SENTINEL_TURN` in events).
- Raw payload dict (not parsed `Problem`) is passed to `build_reference_canonical`
  / `build_problem_candidates` — regression guard that `declared_paths` /
  `symbolic_mappings` / `entity_key` survive (the §1 critical finding).

---

### WU-4C2 — Calibration harness + constrained diagnostics + rubric mapping

**One-line:** Compute the §6.7 per-run calibration metrics (shadow simulator vs
old LLM diff), define the findings/sub-scores → `compute_rubric` input mapping
for the shadow run, and make the diagnostic findings-driven with the §6.8
post-check — all while the student-facing grade stays the OLD path (shadow).

**SEAM (owns vs WU-5A):** WU-4C2 owns everything that *surfaces or measures* the
shadow grade without enabling it for Layer 3. It computes calibration metrics
from the persisted run + findings + the OLD coverage map; it maps findings to a
rubric shape; it generates the constrained diagnostic. It does NOT update Layer 3
and does NOT flip the grade live (the promote-to-live flag lives here but stays
OFF). Handoff: the calibration-metrics record + the findings-driven diagnostic.

**Scope — files to touch/create:**

- `apollo/grading/calibration.py` (NEW) — the §6.7 per-run metrics:
  `compute_calibration_metrics(*, shadow: retired shadow result, old_coverage,
  old_rubric) -> CalibrationMetrics` (false-missing rate vs transcript audit,
  resolution tier distribution from `ResolutionResult.tier_counts`, unresolved
  rate, explicit-vs-inferred edge counts, simulator-vs-old-LLM agreement).
  Pure over the persisted run + the OLD coverage map. Where these metrics are
  STORED is a small decision (a JSONB column on the run is already there via
  `abstention_reasons`; recommend logging + a structured log line / optional
  `message` finding rather than a new column — NO migration, §4.5).
- `apollo/grading/rubric_mapping.py` (NEW) — the §6.4 explicit task:
  `findings_to_rubric_input(grade: GradeResult, audited: AuditedGrade) -> …`
  mapping covered/missing/contradiction findings + the 7 sub-scores to the shape
  `compute_rubric` expects (or a thin adapter that lets `compute_rubric` consume
  sub-scores directly). Computed for the SHADOW run in v1 (not shown to the
  student); persisted/logged for calibration comparison.
- `apollo/overseer/diagnostic.py` (EDIT) or `apollo/grading/diagnostic.py` (NEW)
  — §6.8: input = `GradeResult.findings` + the quoted `evidence_spans` only
  (never the free transcript); the post-check ("must not claim covered what
  findings mark missing; must not introduce misconceptions not in findings; must
  reference spans"), regenerate-once-then-template fallback (NO-FALLBACK,
  logged). Recommend a NEW `apollo/grading/diagnostic.py` so the OLD coverage-fed
  `generate_diagnostic` stays intact for the shadow window and the new one is
  cleanly testable; `done.py`/`done_grading.py` selects which based on the
  promote-to-live flag.
- `apollo/handlers/done.py` / `done_grading.py` (EDIT) — call the calibration
  metrics + (shadow) rubric mapping after `persist_comparison_run`; the
  `APOLLO_GRAPH_SIM_LIVE_ENABLED` promote flag (default OFF) selects whether the
  findings-driven rubric/diagnostic become student-facing. OFF in v1 → OLD path
  remains the student grade; the new artifacts are computed + persisted/logged
  only.

**Public API exposed:**

```python
from apollo.grading.calibration import compute_calibration_metrics, CalibrationMetrics
from apollo.grading.rubric_mapping import findings_to_rubric_input
from apollo.grading.diagnostic import generate_findings_diagnostic  # §6.8 constrained
APOLLO_GRAPH_SIM_LIVE_ENABLED  # promote-to-live flag, default OFF
```

**Dependencies:** WU-4C1 (`retired shadow result`), the OLD `compute_coverage` /
`compute_rubric` outputs (for agreement metrics), `apollo.agent._llm.main_chat`
(diagnostic; injected/mocked).

**Migration?** **No** (calibration metrics are logged / reuse existing JSONB; no
new column — §4.5).

**Real-infra tests?** **None required.** Calibration metrics are pure over the
in-memory shadow result + old coverage map; the diagnostic is pure logic + an
LLM-mock (inject the chat fn). The persistence those metrics describe is already
covered by WU-4C1's real-PG tests. Clean separation from the container regime.

**Key behavioral tests:**
- Calibration: a shadow `missing` that the OLD coverage credited → counted as a
  disagreement; tier distribution from `tier_counts` reported; unresolved rate
  computed.
- Rubric mapping: a `GradeResult` with covered×2 + missing maps to the rubric
  input shape `compute_rubric` accepts (round-trip against the real
  `compute_rubric`).
- §6.8 diagnostic: a diagnostic that claims a `missing` entity covered FAILS the
  post-check → regenerate → on second failure, template fallback (logged).
- Promote flag OFF → student-facing rubric/diagnostic are the OLD ones (shadow);
  flag ON (test-only) → findings-driven rubric/diagnostic become the return.
- §6.11 fixtures (Bernoulli §6.9 walk-through) drive the diagnostic + calibration
  end-to-end against the shadow run from WU-4C1's fixtures.

---

## 4. Risks, uncertainties, and spec ambiguities to resolve

1. **HIGHEST — shadow-vs-live: the new grade is SHADOW in v1, not live.**
   §6.7 is unambiguous: `compute_coverage` "runs in **shadow** alongside the
   simulator… Layer-3 updates from simulation events are enabled only after
   calibration review"; "Until the gate opens… events are emitted
   diagnostic-only/low-confidence." §6.4's transaction story commits the OLD
   grade FIRST. Therefore **WU-4C v1 keeps the OLD `compute_coverage` /
   `compute_rubric` as the student-facing grade and runs the new chain in
   shadow** — computed + persisted (run/findings) + measured (calibration), but
   NOT shown to the student and NOT written to Layer 3. Two flags govern it:
   `[retired A7 shadow switch]` (run the chain at all; default OFF in prod,
   ON in test) and `APOLLO_GRAPH_SIM_LIVE_ENABLED` (promote the new grade to
   student-facing; default OFF, flipped only after human calibration review —
   never inside this build). **This is the single most important decision: it is
   what makes WU-4C unable to break the live student grade.** It mirrors the
   existing `APOLLO_DONE_GATE_FLAG` pattern (`done.py:53`).

2. **HIGH — live-Done breakage is the top operational risk.** `done.py` is a
   LIVE endpoint. Mitigations (all mandatory): the shadow flag (#1) means the
   default code path is byte-identical to today; the new chain is extracted into
   `done_grading.py` and only invoked when the flag is on; every step-5-onward
   failure is caught and converted to `learner_update_pending=true` + a named
   error (NO-FALLBACK) that NEVER voids the already-committed grade (the
   `RetentionError`/`ResolutionUnavailableError` pattern); a byte-identical-return
   regression test guards the flag-off path. The grade/XP commit MUST stay BEFORE
   the new chain (§6.4 staged transaction).

3. **HIGH — `turn_order` sourcing (deferred to WU-4C by WU-4B2).**
   `convert_findings_to_events` needs `turn_order: Mapping[node_id,int]`, but
   `apollo_messages` is keyed by `turn_index`, not by KG node_id, and `Node`
   carries no `created_at`. **Decision: source `turn_order` from
   `apollo_messages.turn_index`, joined to KG node-ids via the turn a node was
   extracted from.** The bridge already exists in the codebase pattern: KG nodes
   are written per-turn and the negotiations table uses `evidence_node_ids` JSONB
   to bridge Neo4j node-ids to Postgres rows (the spec's §2 "Neo4j bridge… same
   pattern as `apollo_kg_negotiations`"). Concretely, `build_turn_order` reads
   each `:_KGNode`'s extraction turn (the `Message.turn_index` of the turn that
   created it — available either as a Neo4j node property `created_at`/turn or by
   the per-turn write order) and maps node_id → turn position. **Fallback (if no
   per-node turn property exists yet):** use Neo4j node `created_at` ordering
   (§6.5 explicitly allows "turn order from node `created_at`") to assign a
   monotonic position. Either way `turn_order` is built by WU-4C1 and proven
   queryable; `events.py` already tolerates absent keys (`_SENTINEL_TURN`). **Verify
   during 4C1 planning which turn signal the Layer-2 nodes actually carry** (the
   §2 "Layer-2 nodes gain `created_at`" property) — if `read_graph` does not yet
   surface it, that is a tiny `store.py` read addition, NOT a schema change.

4. **HIGH — transaction boundary / event-persistence (4C/5A) — HOLD THE LINE.**
   `persist_comparison_run` is ONE txn (run+findings, supersede, no commit —
   WU-4C commits it). The future WU-5A belief update is a SEPARATE all-or-nothing
   txn (events + `apollo_learner_state`, §3). **WU-4C produces the
   `LearnerEvent`-ready inputs (`opposes_map`, `turn_order`) and MAY produce the
   events via `convert_findings_to_events` for shadow logging, but it MUST NOT
   write `apollo_mastery_events` or `apollo_learner_state`** — that is WU-5A's
   atomic txn (§0.1). Recommendation for v1 cleanliness: WU-4C does NOT even call
   `convert_findings_to_events` on the live path (defer to WU-5A, which needs the
   same turn_order) — this keeps the 4C persist txn (step 15) cleanly separate
   from the 5A event txn (steps 16–17) and avoids any temptation to persist
   events in 4C. Assert in tests that `apollo_mastery_events` is empty after a
   WU-4C Done.

5. **MEDIUM — where calibration metrics are stored.** §6.7 lists per-run metrics
   (false-missing rate, tier distribution, unresolved rate, edge reliability,
   agreement). The run table has no dedicated metrics column. **Decision: do NOT
   add a column (no migration).** Persist metrics via structured logging +
   (optionally) as `unsupported_extra`/`message`-bearing finding rows or in the
   existing `abstention_reasons` JSONB pattern; a dedicated `apollo_calibration`
   table is a separate, later, explicitly-DDL'd concern if the pilot needs
   queryable aggregates. v1 wants the numbers visible in logs for the human
   calibration review, not a new schema surface.

6. **MEDIUM — the `Problem`-schema gap (raw payload vs parsed `Problem`).** The
   §1 critical finding: `build_reference_canonical`/`build_problem_candidates`
   need `declared_paths`/`symbolic_mappings`/`entity_key`, which the `Problem`
   pydantic schema drops. **Decision: WU-4C sources the raw
   `ConceptProblem.payload` dict for the new chain** (a new
   `select(ConceptProblem.payload)` filtered to the chosen problem_code), and
   keeps the parsed `Problem` for the OLD path. Extending the `Problem` schema to
   model those fields is an ALTERNATIVE (cleaner long-term) but risks
   regressions in every `Problem` consumer and the schema's strict validators —
   **prefer the raw-dict read in v1; flag schema extension as a follow-up.**
   No migration either way.

7. **MEDIUM — `canon_key_by_canonical_key` may be empty pre-promotion.** If a
   course's `apollo_kg_entities` rows are not yet minted (entity links absent),
   `build_problem_candidates` gets an empty/partial map → candidates carry
   `canon_key=-1` → `persist_comparison_run` stores `entity_id=None` (tolerated,
   `persistence.py:130`). Grading still works (resolution is reference-anchored,
   not entity-anchored); only the Layer-1 surrogate id is absent. Acceptable for
   v1 shadow; note it so it is not mistaken for a bug.

8. **LOW — `main_chat_adjudicator` / `audit_fn` live wiring.** The resolver's
   LLM adjudication (one call/attempt) and the transcript audit (one call/attempt)
   are live LLM calls on the Done path. They are injectable (resolver
   `llm_adjudicator=None` default; `build_audited_grade` `audit_fn` default = live).
   WU-4C1 wires the live impls but every test injects a stub. Their failure modes
   already raise the named errors (`ResolutionUnavailableError`,
   `TranscriptAuditUnavailableError`) handled per #2/#4. Cost/latency: two extra
   Done-time LLM calls — acceptable (the spike measured ~$0.004/turn; these are
   per-attempt, not per-turn).

---

## 5. Build order (stacked branches)

1. **WU-4C1** branches off `feat/apollo-kg-wu4b3-runs-findings-persistence` (the
   WU-4B tip — the last grading-chain branch). Lands the `done.py` rewire
   (shadow-flagged) + `done_grading.py` + `done_turn_order.py` + the five error
   handlers in `api.py`. Independently green at ≥95% patch coverage — **this
   sub-unit trips BOTH the `tests/database` real-PG gate AND the Neo4j gate**
   (Done-route round-trip + RESOLVES_TO idempotency), which must run
   green-not-skipped. LLM calls mocked.
2. **WU-4C2** branches off WU-4C1. Lands `apollo/grading/calibration.py`,
   `apollo/grading/rubric_mapping.py`, `apollo/grading/diagnostic.py` + the
   `done.py`/`done_grading.py` calibration+diagnostic calls and the
   promote-to-live flag (OFF). Independently green at ≥95% (pure + LLM-mock; NO
   containers).
3. WU-5A branches off WU-4C2 (§6.4 step 16 invocation + step 17 — the
   all-or-nothing Bayesian update + event persistence, consuming WU-4C's
   `opposes_map`/`turn_order`/`AuditedGrade`).

Each sub-unit is one TDD-executor pass (an "L"): WU-4C1 ≈ 2 new modules + a
focused `done.py`/`api.py` edit + ~7 test files (incl. real-PG + real-Neo4j
integration); WU-4C2 ≈ 3 new modules + a `done.py` edit + ~7 test files (pure +
LLM-mock). Neither is an XL.

---

## 6. Doc/coverage obligations (per the project contracts)

- Patch coverage ≥95% on changed lines (`diff-cover` vs `origin/staging`),
  enforced per sub-unit. 4C1 needs Docker up for the `tests/database` gate AND a
  Neo4j test instance (green-not-skipped); 4C2 is pure + LLM-mock.
- Reconcile `docs/architecture/apollo.md` in the same commit each sub-unit lands:
  the `owns:` globs already cover `apollo/handlers/**`, `apollo/api.py`,
  `apollo/grading/**`; update the Done-pipeline data-flow narrative (the
  shadow-mode split, the new chain order, the two flags) and the error-handler
  inventory; bump `last_verified`. New modules (`done_grading.py`,
  `done_turn_order.py`, `grading/calibration.py`, `grading/rubric_mapping.py`,
  `grading/diagnostic.py`) register in the owner doc.
- No migration in WU-4C (026 shipped runs/findings + `learner_update_pending`;
  027 dropped `concept_cluster_id`). Next free number stays 028. If risk #5
  (calibration metrics table) is ever chosen, that is a separate, later,
  explicitly-DDL'd change — do not absorb it here.
- The two flags (`[retired A7 shadow switch]`,
  `APOLLO_GRAPH_SIM_LIVE_ENABLED`) and the §6.7/§6.8 thresholds are calibration
  knobs: declare them as named module constants / documented env flags (like
  `APOLLO_DONE_GATE_FLAG`) so the promote decision is config, not code surgery.

---

## 7. Summary table

| Sub-unit | Seam (input → output) | Files | Migration | Real-infra | Key risk owned |
|---|---|---|---|---|---|
| **WU-4C1** | live Done request → persisted comparison run+findings (SHADOW) + `retired shadow result` (turn_order, opposes_map) + 5 error handlers | `done.py` (edit), `done_grading.py`, `done_turn_order.py`, `api.py` (edit) | No | **Yes — real-PG AND Neo4j** | shadow flag + live-Done non-breakage + turn_order sourcing |
| **WU-4C2** | `retired shadow result` + old coverage → §6.7 metrics + §6.4 rubric mapping + §6.8 constrained diagnostic | `grading/calibration.py`, `grading/rubric_mapping.py`, `grading/diagnostic.py`, `done.py` (edit) | No | No (pure + LLM-mock) | promote-to-live flag (OFF), diagnostic post-check |
