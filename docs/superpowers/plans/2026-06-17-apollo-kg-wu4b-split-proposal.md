# WU-4B split proposal — Apollo grading-core downstream layer

**Date:** 2026-06-17
**Author:** scoping pass (autonomous stacked-PR build)
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §6.4–§6.11 (Done pipeline / decision table / abstention / fixtures), §2 (runs/findings tables, event shape), §3 (event types + confidence caps/damper), §5 (resolver method-caps)
**Status:** PROPOSAL ONLY — no code/tests/source touched. Divides the XL unit WU-4B into sequential sub-units with clean seams; per-sub-unit TDD plans are a later step.
**Template:** `docs/superpowers/plans/2026-06-17-apollo-kg-wu4a-split-proposal.md`

---

## 0. What WU-4B is (and the sibling boundaries it must not cross)

WU-4B is the **downstream layer of the grading core**: it takes the pure
in-memory `GradeResult` (from WU-4A2's `grade_attempt`) plus the raw transcript
and resolution result, and turns it into **persisted, abstention-gated,
learner-model-ready evidence**. It owns §6.4 steps **12 (transcript audit),
14 (abstention gates), 15 (persist runs+findings), 16 (finding→event conversion)**,
plus the §6.11 executable fixture corpus.

It is NOT the score-math (WU-4A, DONE — `apollo/graph_compare/`), it does NOT
re-orchestrate `done.py` (WU-4C), and it does NOT run the Bayesian belief update
or write `apollo_mastery_events` (WU-5A). It produces **in-memory events** and
**persisted runs/findings**; WU-5A consumes the events to update Layer 3.

Phase-4 / Phase-5 boundary (from the roadmap, restated and HELD):

- **WU-4A (DONE, PRs #31/#32)** = the pure score-math core in
  `apollo/graph_compare/`: `grade_attempt(student_canonical, reference_graph) ->
  GradeResult` with in-memory `Finding`s (`FindingKind` == the
  `apollo_graph_comparison_findings.finding_kind` set). NO events, NO audit, NO
  abstention, NO persistence, NO IO.
- **WU-4B (this unit)** = transcript auditor (§6.4 step 12), abstention gates
  (§6.6), §6.5 finding→event conversion, runs+findings Postgres persistence
  (§6.4 step 15), §6.11 executable fixtures.
- **WU-4C (later)** = `done.py` re-orchestration (§6.4 wiring steps 1–9, 13, 18),
  rubric mapping (findings/sub-scores → `compute_rubric`), constrained
  diagnostics (§6.8), **§6.7 calibration/shadow-gate wiring** (`compute_coverage`
  stays in shadow — do NOT delete).
- **WU-5A (later)** = §6.4 step 17 — the 3-state Bayesian filter update in the
  Done transaction, writing `apollo_learner_state` + `apollo_mastery_events`.

### Where the lines fall (decided, with spec citations)

1. **§6.7 calibration/shadow gate → WU-4C, NOT WU-4B (CONFIRMED).** §6.7 is
   about *wiring* `compute_coverage` to run in shadow alongside the simulator
   and enabling Layer-3 updates only after calibration review. That is Done-route
   orchestration (which path runs, what gets compared, what gets displayed) —
   the WU-4C concern. WU-4B emits events tagged with their confidence (the §3
   caps) but does not decide whether Layer 3 *trusts* them; it never touches
   `done.py`, `coverage.py`, or `rubric.py`. The §6.7 *metrics* (false-missing
   rate, tier distribution, unresolved rate) are computable from the run +
   findings WU-4B persists — WU-4B should persist enough to make them queryable
   (it already does: `abstention_reasons`, per-method via findings), but the
   shadow-comparison wiring itself is WU-4C. **The roadmap's "calibration-gate
   wiring → WU-4C" is correct.**

2. **§6.8 diagnostics → WU-4C, NOT WU-4B (CONFIRMED).** The constrained
   diagnostic LLM (input = findings only, post-check) is step 18, downstream of
   persistence, and is explicitly listed in WU-4C's scope. WU-4B persists the
   findings the diagnostic will read; it does not generate the diagnostic.

3. **Event PERSISTENCE → WU-5A, NOT WU-4B (CONFIRMED — boundary correction).**
   The `MasteryEvent` ORM (`apollo_mastery_events`) and `LearnerState`
   (`apollo_learner_state`) already exist (migration 026), but writing them is
   §6.4 step 17 — the all-or-nothing Bayesian-update transaction (§3). The
   roadmap binds that to **WU-5A** (`compare_branch: WU-4C`). Therefore **WU-4B
   produces events as an in-memory value object and persists ONLY the runs +
   findings tables** (`apollo_graph_comparison_runs` / `_findings`, §6.4 step 15
   — "always, even on abstained runs"). The events it emits are an in-memory
   `LearnerEvent` tuple consumed by WU-5A. This keeps the §3 "events + belief
   update commit all-or-nothing" invariant inside the unit that owns the belief
   update. WU-4B's job for events ends at "compute the correct event list per the
   §6.5 table + the abstention suppressions"; WU-5A's job begins at "apply them
   to belief and write the event log."

4. **Pipeline ownership (§6.4):** WU-4B owns steps **12, 14, 15, 16**. It
   consumes step 13's output (`GradeResult`, WU-4A2) and the resolution result
   (WU-3C2, for `normalization_confidence`) and the raw transcript (from
   `apollo_messages`). Steps 1–9, 13 (compute), 18 are WU-4C; step 17 is WU-5A.

---

## 1. Ground truth discovered (load-bearing facts that shape the split)

Verified against the real code/migrations/ORM, not assumed:

- **Tables + ORM already exist (migration 026).**
  `apollo/persistence/models.py`: `GraphComparisonRun` (line 479),
  `GraphComparisonFinding` (line 527). Columns confirmed:
  `runs` carries the 3 top-line + 7 sub-scores (`*_score`),
  `normalization_confidence` **(NOT NULL)**, `abstained` (NOT NULL, default
  false), `abstention_reasons` **(JSONB, NOT NULL, default `[]`)**,
  `comparison_version` (NOT NULL), `reference_graph_hash` (NOT NULL),
  `UNIQUE (attempt_id, comparison_version)` for supersede semantics.
  `findings` carries `entity_id` (FK SET NULL), `finding_kind` (NOT NULL),
  `score`/`confidence` (nullable), four `*_node_ids`/`*_edge_ids` JSONB,
  `evidence_spans` JSONB, `message`. **WU-4B needs NO migration** (next free
  = 028, reserved only if a new need surfaces — see §4).

- **No comparison-run STORE exists yet.** `grep` for `GraphComparisonRun` /
  `graph_comparison_runs` in non-test, non-models code finds only
  `graph_compare/core.py` (the `comparison_version` constant). There is no
  `apollo/.../graph_comparison_store.py`. WU-4B creates the persistence seam.
  The model to imitate is `apollo/knowledge_graph/resolution_store.py`
  (pure pre-DB spec dataclasses + thin async write seam) and
  `scripts/seed_apollo_learner_model.py` (the SQLAlchemy write layer is kept
  out of the pure conversion module). The real-PG round-trip test pattern lives
  in `tests/database/` (e.g. `test_resolution_resolves_to_postgres.py`,
  `test_graph_compare_symbolic_mappings_roundtrip.py`).

- **`GradeResult` (WU-4A2) is the exact input.** `apollo/graph_compare/core.py`:
  `GradeResult` carries the 10 `*_score` fields **named 1:1 to the runs columns**,
  `comparison_confidence` (==1.0 v1, carried but NOT a persisted column),
  `findings: tuple[Finding, ...]`, `comparison_version` (`"graph-compare-v1"`).
  `Finding` (`apollo/graph_compare/findings.py`) carries `kind` (FindingKind),
  `canonical_key`, `student_node_ids`, `reference_node_ids`, `evidence_spans`,
  `score`, `confidence`, `message`. **Finding fields map 1:1 onto the findings
  columns** (the module docstring states this); edge ids are carried in
  `message` text and default to `[]` on persist. `FindingKind` value-set ==
  `models.FINDING_KINDS` (asserted by a test).

- **`normalization_confidence` is WU-4B's to supply at persist time.** It is NOT
  on `GradeResult`. Source: the resolution method-caps — `METHOD_CONFIDENCE_CAP`
  (`apollo/resolution/candidates.py`: exact 1.00 · symbolic 0.98 · alias 0.92 ·
  fuzzy 0.80 · LLM 0.75 · transcript-audit 0.75 · unresolved 0.00) and the
  per-node capped `confidence` already on each `ResolvedNode`
  (`apollo/resolution/result.py`). The spec (§3) defines `grader_confidence =
  normalization_confidence × comparison_confidence`. **The exact aggregation of
  per-node caps → one run-level `normalization_confidence` scalar is NOT
  specified** — a real ambiguity WU-4B must resolve (see §4 risk #2).

- **Transcript source for the audit.** The raw student transcript lives in
  `apollo_messages` (the `Message` ORM: `role`, `turn_index`, `attempt_id`,
  `message_metadata`). `done.py` already reads it (`_attempt_misconception_scores`
  selects `Message.role == "apollo"` ordered by `turn_index`). The auditor needs
  the **student** turns (the things the student said). So the audit's transcript
  input is a DB read (`select(Message)... where role in {student/user}, order by
  turn_index`) — a thin async query, mockable in tests.

- **The audit IS an LLM call (mockable).** §6.4/§6.3 `transcript_audit.py`
  bind it as "one batched Done-time LLM call (never per-node)... constrained
  verification, not re-grading." The client is `apollo.agent._llm.main_chat`
  (`purpose=`, `messages=`, optional `response_format`; wraps `OpenAI()`).
  `main_chat` is **synchronous** (returns a string) — the audit wrapper is sync,
  injected/mocked in tests by patching `main_chat` or passing an adjudicator-style
  callable (mirrors how the resolver injects `llm_adjudicator`). **Recommend the
  injectable-callable pattern** (`audit_fn: Callable[...] | None = None`,
  default = the real `main_chat`-backed impl) so the audit is testable with zero
  network and the abstention "audit fails → suppress missing" path is a
  deterministic raise.

- **§6.5 turn-order signal (`corrected` vs `misconception`) has NO in-memory
  home.** The in-memory `Node` (Pydantic `_NodeBase`, `apollo/ontology/nodes.py`)
  carries `node_id`, `attempt_id`, `source`, `parser_confidence`, `status`,
  `student_belief` — but **NO `created_at`**. §6.5 says "turn order from node
  `created_at`" and §2 says Layer-2 Neo4j nodes "gain `created_at`". So the
  turn-order signal for the conflict rows is either (a) the Neo4j node
  `created_at` property, or (b) `apollo_messages.turn_index` joined via the
  evidence node-ids. The `GradeResult.Finding` carries `student_node_ids` but no
  timestamps. **This is a real seam (§4 risk #1):** WU-4B's event conversion
  needs a per-node ordering key, which is not currently threaded into the pure
  findings. Options in §4.

- **Misconception/opposes structure is algorithmic.** `Candidate`
  (`apollo/resolution/candidates.py`) carries `is_misconception: bool` and
  `opposes_key: str | None`. A contradiction finding's `canonical_key` is a
  `misc.*` key (the §6.5 `misconception` row); the `opposes_key` is what lets the
  event layer see that a `contradiction` on `misc.X` and a `covered_node` on the
  entity X concern the **same concept** (the `corrected` / last-position-wins
  rows). WU-4B needs the opposes-link map at event-conversion time — sourced from
  the candidate set / `apollo_kg_entities.payload.opposes_entity_id` (or the
  `misconceptions.json` `opposes` field already parsed by
  `candidates_from_misconceptions`).

- **Abstention gate inputs.** §6.6 gates need: `unresolved_rate` (computable
  from `GradeResult.findings` — count `unresolved` / total resolved nodes, or
  from the `ResolutionResult.tier_counts`); `parser_confidence` **min over the
  attempt's turns** (from `Node.parser_confidence`, available on the student
  graph WU-4C loads — must be threaded in); misconception resolution confidence
  (the finding's `confidence`); reference-validation failure (already raised as
  `ReferenceGraphInvalidError` by `build_reference_canonical`, WU-4A1);
  transcript-audit failure (the audit wrapper raises). `abstention_reasons` is
  the JSONB list persisted on the run.

- **§6.11 fixtures are partly pre-staged by WU-4A2.** The WU-4A2 split already
  shipped pure-score fixtures for: valid alternative path, correct-thin,
  wrong-answer-correct-concepts (contradiction), reference-omits-assumption
  (`unsupported_extra`), misconception-not-in-bank (`unsupported_extra`),
  polar near-miss (resolver-level). WU-4B owns the fixtures that need
  audit/abstention/events: parser-misses-sentence→audit-finds-span,
  conflicting-first/last→corrected/misconception (turn order), and
  high-unresolved→abstention. **Every §6.11 fixture must ship with expected
  EVENTS in WU-4B** (the spec: "each fixture ships with expected findings AND
  expected learner events") — WU-4A asserted findings; WU-4B asserts events.

---

## 2. Recommendation

**Split WU-4B into THREE sub-units (WU-4B1, WU-4B2, WU-4B3).**

Rationale: there are three genuinely separable concerns with clean data seams,
and a two-way split would force either (a) the LLM-audit + abstention logic to
share a branch with the real-PG persistence (two different test-infra regimes —
LLM-mock vs Testcontainers-PG — in one CI cycle, and ~an XL again), or (b) the
finding→event decision table to ride along with persistence (mixing the §6.5
pure-logic table with DB round-trips). The three-way seam keeps each sub-unit
~an "L", each independently testable at ≥95% patch coverage, and each on a
single test-infra regime:

- **WU-4B1 — transcript audit + abstention gates** (pure logic + ONE
  LLM-mock seam; no DB). Turns `GradeResult` + transcript + resolution into an
  **audited, gate-decorated grade** (`AuditedGrade`): upgraded `missing`
  findings, computed abstention reasons, the `abstained` flag. Test regime:
  pure unit + LLM-mock.
- **WU-4B2 — finding→event conversion (§6.5 decision table)** (pure logic; no
  DB, no LLM). Turns the `AuditedGrade` + opposes-map + turn-order into the
  in-memory `LearnerEvent` tuple per the binding §6.5 table, honoring the
  abstention suppressions. Test regime: pure unit (table-driven).
- **WU-4B3 — runs+findings Postgres persistence + §6.11 corpus** (real-PG
  round-trip; the executable fixture corpus asserting findings AND events
  end-to-end). Persists the run + findings (supersede semantics), supplies
  `normalization_confidence`, and lands the §6.11 fixtures that exercise the
  whole 4A2→4B1→4B2→4B3 chain. Test regime: Testcontainers-PG + the e2e corpus.

Why this seam and not "audit+events vs persistence" (two-way): the §6.5
decision table is the single most spec-dense, ambiguity-prone piece of WU-4B
(the binding table with turn-order rules), and the transcript audit is the
single most infra-coupled pure-logic piece (the LLM-mock seam + the load-bearing
"audit fails → suppress missing" abstention path). Giving each its own branch +
review cycle isolates the two hardest correctness surfaces. Persistence + the
e2e corpus is then a clean, mechanical real-PG unit that proves the whole chain.

A four-way split (audit / abstention / events / persistence) was rejected:
abstention is tightly coupled to the audit (the audit-failure gate IS an
abstention reason, and the parser-confidence gate suppresses the same `missing`
events the audit upgrades) — splitting them would create a sub-unit too thin to
justify a branch and force B-abstention to re-establish B-audit context.

---

## 3. Sub-units

### WU-4B1 — Transcript audit + abstention gates

**One-line:** Audit simulator-flagged `missing` findings against the raw
transcript (one batched LLM call), then apply the §6.6 abstention gates,
producing an `AuditedGrade` (upgraded findings + abstention reasons + abstained
flag).

**SEAM (owns vs WU-4B2):** WU-4B1 owns everything that turns *(GradeResult, raw
transcript, resolution result, per-turn parser confidences)* into an
`AuditedGrade`. It mutates NO scores (the score-math is frozen — WU-4A2); it
**re-labels findings** (a `missing_node` whose span the audit finds becomes a
`partial`/`covered`-eligible finding at `method=transcript_audit`,
`confidence ≤ 0.75`) and **records abstention reasons**. It does NOT consult the
§6.5 event table and emits NO events. Handoff artifact: a frozen `AuditedGrade`
(the `GradeResult` + audit-upgraded findings + `abstention_reasons: tuple[str,...]`
+ `abstained: bool` + the new alias candidates from found spans).

**Scope — files to create (recommend a NEW package `apollo/grading/`):**

> **Package-home recommendation:** put WU-4B in a new `apollo/grading/` package,
> NOT inside `apollo/graph_compare/`. `graph_compare/` is the **pure, IO-free**
> score core (its `__init__` docstring asserts "persists NOTHING, runs NO Neo4j /
> Postgres / LLM"). WU-4B introduces an LLM call, a DB read, and DB writes —
> mixing those into `graph_compare/` would break that invariant and the clean
> "pure core" story. `apollo/grading/` becomes the downstream orchestration
> layer that *imports* `apollo.graph_compare` (and WU-4C's `done.py` rewiring
> imports `apollo.grading`). Mirrors the `resolution/` (pure) vs
> `knowledge_graph/resolution_store.py` (IO) separation already in the repo.

- `apollo/grading/__init__.py` — package seam; re-exports the public API as it
  grows across 4B1/4B2/4B3.
- `apollo/grading/transcript_audit.py` — the §6.3 `transcript_audit.py` row:
  `audit_missing(missing_findings, transcript, *, audit_fn=None) ->
  AuditResult`. Builds ONE batched prompt (missing reference entities' display
  names + aliases + the transcript, chunked per §6.3 context-budget rule), calls
  the injected `audit_fn` (default = a `main_chat`-backed impl with a strict
  `response_format`), parses per-entity span-or-null. **Binding failure mode:**
  any audit infrastructure failure (timeout/error/parse) raises a named
  `TranscriptAuditUnavailableError` — NEVER "skip audit, emit missing". Span
  found → the finding is marked audit-upgraded (carries the quoted span + an
  alias candidate at the 0.75 cap). The transcript-read helper (`Message` query)
  lives here or is injected by the caller (WU-4C); 4B1 takes the transcript text
  as input so the pure logic is DB-free and testable.
- `apollo/grading/abstention.py` — the §6.6 gates (hand-set v1 thresholds as
  module constants): `unresolved_rate > 0.35` → no-update/diagnostic-only;
  `min parser_confidence < 0.6` → suppress `missing`; reference-validation
  failure → block (already raised upstream as `ReferenceGraphInvalidError`);
  misconception resolution confidence < 0.8 → no `misconception` event;
  audit failure → suppress all `missing`. `apply_abstention(grade, audit_result,
  *, unresolved_rate, min_parser_confidence) -> Abstention` returns the
  `abstention_reasons` list + the `abstained` flag + the per-kind suppression set
  (which event kinds WU-4B2 must withhold). **`parser_confidence` is min over
  turns, never mean** (§6.6 binding).
- `apollo/grading/audited_grade.py` (small) — the `AuditedGrade` /
  `AuditResult` / `Abstention` / `AliasCandidate` frozen dataclasses (the
  handoff vocabulary).

**Public API exposed for WU-4B2 / 4B3 / 4C:**

```python
from apollo.grading import (
    audit_missing, AuditResult, TranscriptAuditUnavailableError,
    apply_abstention, Abstention, ABSTENTION_THRESHOLDS,
    AuditedGrade, AliasCandidate,
    build_audited_grade,   # GradeResult + transcript + resolution -> AuditedGrade
)
```

**Dependencies:** WU-4A2 (`GradeResult`/`Finding`), WU-3C2
(`ResolutionResult`/`METHOD_CONFIDENCE_CAP`), `apollo.agent._llm.main_chat`
(injected), the `Node.parser_confidence` field. No DB writes; the transcript is
passed in as text (DB read is the caller's/WU-4C's concern, kept out of the pure
logic).

**Migration?** **No.**

**Real-infra tests?** **None.** Pure logic + LLM-mock (inject `audit_fn`). The
audit-failure abstention path is a deterministic raise — no network.

**§6.11 fixtures that land in 4B1 (unit-level, audit/abstention behavior):**
- "parser misses a key sentence → transcript audit finds the span → partial/
  covered at ≤0.75; NO false missing" (the core audit-upgrade case).
- "high-unresolved-rate (>0.35) → abstention: abstained=true, reason recorded"
  (the abstention gate, at the `AuditedGrade` level — the *persisted + no-event*
  end-to-end assertion is 4B3).
- audit-infra-failure → all `missing` suppressed, `abstention_reasons` carries
  the audit-failure reason, named error surfaced not swallowed (the binding
  biased-evidence guard).
- `parser_confidence` min-over-turns trips the gate where the mean would hide it.

---

### WU-4B2 — Finding→event conversion (§6.5 decision table)

**One-line:** Convert an `AuditedGrade`'s findings into the in-memory
`LearnerEvent` tuple per the binding §6.5 decision table, honoring turn order
(corrected vs misconception) and the abstention suppressions.

**SEAM (owns vs WU-4B3):** WU-4B2 owns the pure §6.5 mapping: findings (per
entity) + opposes-map + per-node turn-order → `tuple[LearnerEvent, ...]`. It
emits the event value objects (`covered|partial|missing|misconception|corrected`
with `score`, `confidence`, `misconception_code`, `entity_id`,
`evidence_node_ids`, `reference_step_id`) — the §2/§3 `MasteryEvent`-shaped
payload — but **does NOT persist them** (that is WU-5A's belief-update txn) and
does NOT persist findings/runs (that is WU-4B3). Handoff artifact: a frozen
`LearnerEvent` tuple. WU-5A consumes it; WU-4B3 persists the run/findings
alongside it.

**Scope — files to create (`apollo/grading/`):**

- `apollo/grading/events.py` — the §6.3 `findings.py`/`events.py` event half +
  the §6.5 table: `convert_findings_to_events(audited_grade, *, opposes_map,
  turn_order) -> tuple[LearnerEvent, ...]`. Implements every binding row:
  `covered_node + required edges` → `covered` (s≈1.0 × resolution confidence);
  `covered_node + edge missing` → `covered` (v1; the `partial` variant is
  **calibration-gated OFF** — a module constant, never enabled here);
  `missing_node AND audit-negative` → `missing` (s=0.0); `missing_node BUT audit
  span` → `partial`/`covered` at `method=transcript_audit`; `contradiction
  (misc.* ≥ gate confidence)` → `misconception` (s=0.0, `misconception_code`);
  the **conflict rows** (`contradiction` earlier + `covered` later on the opposed
  entity → `corrected`; `covered` earlier + `contradiction` later →
  `misconception`, last-position-wins; ambiguous order → `partial` low-confidence
  + `mixed-understanding` flag) using `opposes_map` + `turn_order`;
  `unsupported_extra` → **no event**; `unresolved` → **no event** (counts toward
  abstention, already consumed in 4B1). Applies the 4B1 suppression set (e.g.
  abstained run → empty events; `missing` suppressed → drop those).
- `apollo/grading/event_model.py` (small) — the `LearnerEvent` frozen
  dataclass + `LearnerEventKind` StrEnum (value-set == `models.MASTERY_EVENT_KINDS`:
  `covered|missing|partial|misconception|corrected`; assert equality so they
  can't drift, mirroring the WU-4A2 `FindingKind`↔`FINDING_KINDS` test).
- `apollo/grading/opposes.py` (small) — build the `opposes_map`
  (`misc.* key → opposed entity key`) from the candidate set /
  `apollo_kg_entities.payload` so the conflict rows are detectable. The
  turn-order key (§4 risk #1) is resolved here or in 4B1 — **recommend it is an
  explicit input to `convert_findings_to_events`** (a `Mapping[node_id, int]`
  from `apollo_messages.turn_index` or Neo4j `created_at`), supplied by WU-4C at
  wiring time and by fixtures in tests; 4B2 stays pure over it.

**Public API exposed for WU-4B3 / WU-5A / 4C:**

```python
from apollo.grading import (
    convert_findings_to_events, LearnerEvent, LearnerEventKind,
    build_opposes_map, EVENT_CONVERSION_VERSION,
)
# LearnerEvent fields map onto apollo_mastery_events columns (entity_id,
# event_kind, score, misconception_code, evidence_node_ids, reference_step_id);
# parser_confidence/grader_confidence/belief fields are WU-5A's to fill at update.
```

**Dependencies:** WU-4B1 (`AuditedGrade`), WU-3C2 (`Candidate.opposes_key`),
the `opposes` data. No DB, no LLM. Turn-order is an injected mapping.

**Migration?** **No.**

**Real-infra tests?** **None.** The §6.5 table is pure — table-driven unit tests
(one per binding row) at 100% branch coverage. This is where the decision-table
correctness is proven in isolation, off the DB.

**§6.11 fixtures that land in 4B2 (event-mapping behavior):**
- "conflicting statements, misconception first then correct → `corrected`"
  (turn order).
- "conflicting statements, correct first then misconception → `misconception`
  (last position wins)".
- "wrong final answer, mostly correct concepts → covered events + one
  `misconception` (or `missing`) on the final relation" (the event view of the
  4A2 finding fixture).
- "correct thin explanation → low-coverage covered events, no misconception".
- ambiguous-order → `partial` + mixed-understanding flag.

---

### WU-4B3 — Runs+findings Postgres persistence + §6.11 corpus

**One-line:** Persist the comparison run + findings to Postgres (supersede
semantics, `normalization_confidence` supplied), and land the §6.11 executable
fixture corpus that asserts findings AND events end-to-end through
4A2→4B1→4B2→4B3.

**SEAM (owns vs WU-5A):** WU-4B3 owns the `apollo_graph_comparison_runs` +
`apollo_graph_comparison_findings` writes (§6.4 step 15 — "always, even on
abstained runs"), including computing `normalization_confidence` from resolution
method-caps, the `reference_graph_hash`, and the supersede (delete-then-reinsert
on the `(attempt_id, comparison_version)` UNIQUE). It does NOT write
`apollo_mastery_events` / `apollo_learner_state` (WU-5A, step 17). Handoff: the
persisted `run_id` (+ findings) that WU-5A's belief-update transaction joins.

**Scope — files to create (`apollo/grading/`):**

- `apollo/grading/persistence.py` — `persist_comparison_run(db, *, attempt_id,
  user_id, search_space_id, grade, audited, normalization_confidence,
  reference_graph_hash) -> int` (returns `run_id`). Maps `GradeResult.*_score`
  → `runs` columns 1:1, writes `abstained` + `abstention_reasons` from the
  `AuditedGrade`, then inserts one `findings` row per `Finding` (kind, score,
  confidence, the JSONB id/span lists, message). **Supersede on re-run**: inside
  one Postgres transaction, delete the prior run at the same
  `(attempt_id, comparison_version)` (CASCADE drops its findings) then insert —
  never crash a legitimate retry (§2 binding). Pure pre-DB spec dataclasses
  (`RunRowSpec` / `FindingRowSpec`) split out so the mapping is testable without
  a container (mirrors `resolution_store`'s `*_to_row` seams).
- `apollo/grading/normalization_confidence.py` (small) — the §4-risk-#2
  decision: aggregate the per-node `METHOD_CONFIDENCE_CAP` confidences (from the
  `ResolutionResult`) into the one run-level `normalization_confidence` scalar.
  **Recommend: the min (or evidence-weighted mean) over the resolved nodes that
  backed a covered/contradiction finding** — document the choice; it feeds the
  §3 damper, so a conservative min is the safe v1 (see §4).
- `apollo/grading/reference_hash.py` (small) — `reference_graph_hash` (a stable
  hash of the reference graph AS GRADED, §2: "teacher edits after grading must
  not make old runs unexplainable"). A deterministic hash of the
  `ReferenceGraph` (nodes+edges+paths).
- `apollo/grading/fixtures/` + `apollo/grading/tests/test_fixtures_corpus.py` —
  the §6.11 corpus: each fixture = a student transcript + problem + expected
  findings (re-asserting 4A2) AND **expected events** (the spec's executable
  contract). The persistence ones (high-unresolved → run persisted + no events;
  parser-miss → audit-upgraded finding persisted) run against real PG.

**Public API exposed for WU-4C / WU-5A:**

```python
from apollo.grading import (
    persist_comparison_run, RunRowSpec, FindingRowSpec,
    compute_normalization_confidence, reference_graph_hash,
)
```

**Dependencies:** WU-4B1 + WU-4B2 (consumes `AuditedGrade` + events for the
corpus), the `GraphComparisonRun`/`GraphComparisonFinding` ORM (migration 026),
the async `AsyncSession`, `METHOD_CONFIDENCE_CAP`.

**Migration?** **No** (026 shipped the tables).

**Real-infra tests?** **YES — required.** Real-PG (Testcontainers, per the DB
gate) round-trip for: run + findings insert + read-back (all columns incl.
`abstention_reasons` JSONB, sub-scores nullable), supersede (second run at same
version deletes the first + its findings, no UNIQUE crash), abstained-run still
persists (run + findings written, `abstained=true`). The §6.11 corpus's
persistence-touching fixtures run here. **This is the only sub-unit that
triggers the `tests/database` gate** — clean isolation of the container regime.

**§6.11 fixtures that land in 4B3 (end-to-end, persisted):**
- "high-unresolved-rate transcript → abstention: findings persisted, diagnostic
  produced (stub at this layer), NO Layer-3 update" — the *persisted-run +
  empty-events* assertion (the diagnostic itself is WU-4C; 4B3 asserts the run
  is written + events empty).
- "parser misses a key sentence → audit span → partial/covered finding persisted
  at ≤0.75; NO false missing row".
- A full Bernoulli §6.9 walk-through persisted (covered ×2, partial, missing) as
  the integration capstone (findings rows + event list match §6.9).

---

## 4. Risks, uncertainties, and spec ambiguities to resolve

1. **HIGHEST: turn-order signal for the §6.5 conflict rows has no in-memory
   home.** §6.5's `corrected` (contradiction-then-covered) vs `misconception`
   (covered-then-contradiction, last-wins) rows require a per-node ordering key,
   but the pure `Node`/`Finding`/`GradeResult` carry **no `created_at`/turn
   index** (verified: `_NodeBase` has none; §2 puts `created_at` only on the
   Neo4j Layer-2 node). **Resolve in WU-4B2.** Preferred: `convert_findings_to_
   events` takes an explicit `turn_order: Mapping[node_id, int]` input, supplied
   by WU-4C from `apollo_messages.turn_index` (joined via evidence node-ids) or
   the Neo4j node `created_at` property — keeping 4B2 pure over it and fixtures
   trivial. Fallback (worse): thread `created_at` through `CanonicalNode` →
   `Finding`, which would re-touch the frozen WU-4A1/4A2 dataclasses (avoid —
   they're done and reviewed). **Decision needed before WU-4B2 planning.**

2. **`normalization_confidence` aggregation is unspecified.** §3 names
   `grader_confidence = normalization_confidence × comparison_confidence` and
   the per-method caps, but **not** how the per-node caps collapse into one
   run-level scalar (the persisted NOT-NULL column). **Resolve in WU-4B3.**
   Recommend: min over (or evidence-weighted mean of) the resolved nodes that
   backed a scored finding — the damper wants the *weakest-link* confidence to
   be honest, so min is the conservative v1. Document the choice in the owner
   doc; it is a calibration knob (revisitable, like `CONTRADICTION_UNIT_PENALTY`).

3. **Event persistence boundary (4B/5A) — HOLD THE LINE.** It is tempting to
   write `apollo_mastery_events` in WU-4B3 since the ORM exists. **Do not.** §6.4
   step 17 + §3 bind events to the all-or-nothing belief-update transaction
   (WU-5A). WU-4B emits events as in-memory `LearnerEvent`s; WU-5A persists them
   atomically with the belief update. If WU-4B3 wrote events separately, a retry
   could double-count or a partial write could violate §3. **Boundary
   correction vs a naive reading of the roadmap's "finding→event persistence":
   WU-4B persists runs+findings ONLY; events are produced, not persisted.**

4. **`partial` (edge-gap) variant is calibration-gated OFF (§6.5 row 2).**
   WU-4B2 must implement the `covered` (v1) behavior and keep the `partial`
   variant behind a constant flag (default OFF) — enabling it before edge recall
   is proven would let an edge gap halve a score (the §6.2 bias the demotion rule
   forbids). Mirror WU-4A2's "edges are diagnostic-only" stance. The flag's
   *wiring* to the calibration gate is WU-4C.

5. **Alias-candidate provenance (anti-laundering, §6.3).** Audit-derived spans
   become alias candidates that enter the §8 teacher-approval queue; until
   approved a learned alias resolves at the 0.75 transcript-audit cap, never the
   0.92 alias tier. WU-4B1 produces `AliasCandidate`s (span + entity); **persisting
   them to the approval queue is likely §8/WU-3B2's table, NOT WU-4B** — WU-4B1
   should emit them as a value object and flag the persistence target as a
   downstream follow-up (confirm the queue table exists or defer). Do not invent
   a queue table here.

6. **Misconception "≥ gate confidence" threshold (§6.5 / §6.6).** The
   `misconception` event requires the contradiction resolved to `misc.*` at
   **≥ 0.8** resolution confidence (§6.6 gate); below that, the finding persists
   (4B3) but no event fires (4B2). WU-4B2 reads the finding's `confidence`
   (already method-capped) against the constant. Straightforward, but pin it as
   a fixture (a 0.80-fuzzy misconception fires; a hypothetical sub-0.8 does not).

7. **`reference_graph_hash` stability.** The hash must be deterministic over the
   `ReferenceGraph` and stable across runs (so replays match) but change when the
   teacher edits the reference. A sorted-canonical serialization of nodes+edges+
   paths, hashed. Low risk; flagged so it isn't discovered mid-build.

---

## 5. Build order (stacked branches)

1. **WU-4B1** branches off `feat/apollo-kg-wu4a2-simulation-scores` (the
   `compare_branch` in state.json). Lands `apollo/grading/{__init__,
   transcript_audit,abstention,audited_grade}.py`. Independently green at ≥95%
   patch coverage (pure unit + LLM-mock; NO containers).
2. **WU-4B2** branches off WU-4B1. Lands `apollo/grading/{events,event_model,
   opposes}.py`. Independently green at ≥95% (pure table-driven unit; NO
   containers, NO LLM). Resolves the turn-order seam (risk #1).
3. **WU-4B3** branches off WU-4B2. Lands `apollo/grading/{persistence,
   normalization_confidence,reference_hash}.py` + the `fixtures/` corpus.
   Independently green at ≥95% — **this sub-unit triggers the `tests/database`
   real-PG gate** (round-trip + supersede + abstained-run), which must run
   green-not-skipped.
4. WU-4C branches off WU-4B3 (`done.py` re-orchestration, rubric mapping, §6.7
   shadow wiring, §6.8 diagnostics).
5. WU-5A branches off WU-4C (§6.4 step 17 — Bayesian update + event persistence).

Each sub-unit is one TDD-executor pass (an "L"): 4B1 ≈ 4 modules + ~5 test files
(incl. LLM-mock); 4B2 ≈ 3 modules + ~6 test files (one per §6.5 row); 4B3 ≈ 4
modules + the corpus + ~4 real-PG test files. None is another XL.

---

## 6. Doc/coverage obligations (per the project contracts)

- Patch coverage ≥95% on changed lines (`diff-cover` vs `origin/staging`),
  enforced per sub-unit. 4B1/4B2 are pure (LLM-mock only) so the gate is
  achievable without containers; 4B3 needs Docker up for the `tests/database`
  gate (green-not-skipped, per the workflow hardening).
- Reconcile `docs/architecture/apollo.md` in the same commit each sub-unit lands
  (register `apollo/grading/**` in the `owns:` globs; bump `last_verified`).
  `graph_compare/**` stays owned as before; the new package is additive.
- No migration in WU-4B (026 shipped the runs/findings tables + ORM). Next free
  number stays 028. If risk #5 (alias-candidate queue) forces a table, that is a
  separate §8/WU-3B2 concern — do not absorb it here.
- The §3/§6 confidence-cap and gate-threshold constants are calibration knobs:
  declare them as named module constants (like `CONTRADICTION_UNIT_PENALTY` in
  graph_compare) so WU-4C/calibration can retune without code surgery.

---

## 7. Summary table

| Sub-unit | Seam (input → output) | Files (`apollo/grading/`) | Migration | Real-infra | §6.11 fixtures |
|---|---|---|---|---|---|
| **WU-4B1** | `GradeResult` + transcript + resolution → `AuditedGrade` (upgraded findings + abstention) | `transcript_audit`, `abstention`, `audited_grade`, `__init__` | No | No (LLM-mock) | parser-miss→audit-span; high-unresolved→abstain; audit-fail→suppress-missing |
| **WU-4B2** | `AuditedGrade` + opposes-map + turn-order → `tuple[LearnerEvent]` (§6.5) | `events`, `event_model`, `opposes` | No | No (pure) | conflict first/last→corrected/misconception; thin→no-misc; ambiguous→partial |
| **WU-4B3** | `GradeResult`+`AuditedGrade` → persisted run+findings (+`normalization_confidence`) | `persistence`, `normalization_confidence`, `reference_hash`, `fixtures/` | No | **Yes (real-PG)** | high-unresolved→persisted+no-events; §6.9 Bernoulli e2e capstone |
