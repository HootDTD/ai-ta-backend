# WU-5A split proposal (FINAL, build-ready) — the Done-txn 3-state Bayesian learner-model write

**Date:** 2026-06-18 (revised after adversarial critique: feasibility / spec-fidelity / prior-art)
**Author:** scoping pass (autonomous stacked-PR build)
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §3 (the 3-state filter + update math), §6.4 (Done pipeline steps 16–17), §6.5 (finding→event table — built as WU-4B2), §6.6 (abstention — enforced upstream), §2 (Layer-3 schema, NULLS NOT DISTINCT)
**Status:** PROPOSAL — FINAL. No code/tests/source touched. Every critic REVISE/REJECT finding resolved below; the math is now NUMERICALLY PINNED (reproduced, not asserted) so an executor cannot guess.
**Templates:** `docs/superpowers/plans/2026-06-18-apollo-kg-wu4c-split-proposal.md`, `…wu4b-split-proposal.md`, `…wu4a-split-proposal.md`
**Stacks on:** WU-4C2 (compare branch `feat/apollo-kg-wu4c2-calibration-diagnostics`)
**Backbone note:** WU-5A = the filter (decision-table consumption + 3-state belief update + all-or-nothing Done-txn persist). WU-5B = decay-on-stale-read + janitor retry + the RQ5 chat-keyword hedge. Decay/retry/RQ5 do NOT belong in 5A (see §8).

---

## CHANGE LOG vs the pre-critique draft (what the three critics moved)

1. **[HIGH — done_ts provenance corrected; the load-bearing seam was factually wrong.]**
   The old draft (§1, risk #7) claimed `apollo_problem_attempts` carries
   `graded_at`/`frozen_at` to read. **It does NOT.** Verified: `ProblemAttempt`
   ORM (`apollo/persistence/models.py:265-285`) has ONLY `id, session_id,
   problem_id, difficulty, result, solver_trace, diagnostic_report,
   learner_update_pending, created_at` — no graded/frozen column. `store.stamp_graded_at`
   (`apollo/knowledge_graph/store.py:537-564`) generates `ts=_utc_now_iso()`
   INTERNALLY, does `SET n.graded_at=$ts` on **Neo4j nodes only**, returns a
   COUNT, and its own docstring says "Does NOT touch Postgres." `freeze()` is
   Neo4j-only. **Resolution (§4 decision #7, locked):** `done.py` captures ONE
   `done_ts = datetime.now(UTC)` near the stamp site and threads that SAME value
   into both `stamp_graded_at(ts=…)` (signature widened to accept it) and
   `run_learner_update(done_ts=…)`, so the two stores stamp the identical instant.
2. **[HIGH — absorbing-zero math bug; plan-caused, now floored.]** `L_covered(1.0)=(0,0,1)`
   (reproduced) permanently annihilates `p_misc` on the common perfect-answer
   path at `q=1`, violating the spec's OWN §3-line-427 rule. Fixed with a 5A1
   `LIKELIHOOD_FLOOR=0.02` clamp (standard BKT boundary-floor). Flagged as a SPEC
   self-contradiction to the orchestrator (§OPEN-DECISIONS).
3. **[HIGH — "mid scores land on shaky" is only true at s=0.5.]** Reproduced: at
   s=0.25 argmax=**misconception**, not shaky. The pinning test is now a frozen
   GOLDEN-VECTOR table, not a prose "all mid → shaky" assertion.
4. **[MEDIUM — cold-start mastery is 0.50, not 0.40.]** `0.5·0.60+0.20=0.50`
   (reproduced). The spec asserts 0.40 in prose; the FORMULA gives 0.50. Test
   target corrected to 0.50; the 0.40 prose flagged as a SPEC defect to fix.
5. **[MEDIUM — 0.5 misconception-flag threshold is unreachable in one update.]**
   First misconception from cold-start → p_misc=0.4839 (argmax-but-UNFLAGGED);
   second → 0.7475 (flagged). The two-step behavior is now pinned; the draft's
   "sets code at ≥0.5 in one step" claim is removed.
6. **[MEDIUM — retry/supersede recompute base pinned to the event log.]** The
   recompute base is ALWAYS re-derived from `apollo_mastery_events` (delete this
   attempt's rows across ALL kinds first), never from the possibly-self-mutated
   state row. Two acceptance-blocking retry tests added (identical-twice +
   changed-kind).
7. **[LOW→pinned — parser_confidence single-sourced.]** `done.py` computes
   `parser_confidence = min_parser_confidence_of(student_graph.nodes)` reusing the
   SHIPPED §6.6 helper (`apollo/grading/abstention.py:70`) and threads it in. NOT
   "graph node OR message_metadata" executor choice.
8. **[LOW→pinned — grader_confidence vs the covered score s.]** `q = parser_confidence ·
   grader_confidence`; `grader_confidence = shadow.normalization_confidence × 1.0`.
   The COVERED score `s = event.score` is ALREADY resolution-scaled by the converter
   (`events.py:307-329`); resolution confidence must NOT be re-multiplied into q.
9. **[LOW→pinned — constraint tests on the real-PG harness only.]** The default `db`
   fixture is SQLite (`_RealArrayType = ARRAY(REAL).with_variant(JSON(),'sqlite')`,
   `models.py:45`) where the belief-length-3 / 0-1 CHECKs DON'T EXIST. Every
   constraint-dependent assertion runs under `pytest.mark.integration` / `db_session`
   (Testcontainers), never the unit `db` fixture (else it passes vacuously).

---

## 0. What WU-5A is (and the sibling boundaries it must not cross)

WU-5A is **§6.4 step 16 *invocation* + step 17** — the explicit, atomic Layer-3
write. It receives the frozen `retired shadow result` that WU-4C1 builds
(`apollo/handlers/done_grading.py:91-113`), calls the **already-built, frozen**
pure `convert_findings_to_events` (WU-4B2, `apollo/grading/events.py:68`) to turn
`audited.findings` into a `tuple[LearnerEvent, ...]`, runs the **§3 3-state
Bayesian belief update** per entity, and persists the result — `apollo_mastery_events`
appended + `apollo_learner_state` upserted — in **one all-or-nothing Postgres
transaction** at Done time. It owns **no grading algorithm, no findings
production, no run/findings persistence, no decay, no janitor**.

Everything upstream is DONE and frozen (WU-4A retired graph comparator, WU-4B1 audit/abstention,
WU-4B2 events, WU-4B3 run/findings persistence, WU-4C1 shadow chain + `retired shadow result`,
WU-4C2 calibration/diagnostic + `APOLLO_GRAPH_SIM_LIVE_ENABLED`). The detailed
boundary citations are unchanged from the pre-critique draft and verified below in §1.

### Where the lines fall (decided, with citations)

1. **WU-5A INVOKES `convert_findings_to_events` (step 16); it does NOT reimplement
   it.** WU-4C1 deliberately does NOT call it (`done_grading.py`; the tripwire
   `test_no_mastery_events_written`, `apollo/handlers/tests/test_done_grading_unit.py:473-491`,
   patches it with `create=True` and asserts `not_called`). WU-5A is the FIRST
   caller — from `learner_update.py`/`persistence.py`, NEVER from `done_grading.py`.
2. **WU-5A owns the §3 belief math.** The math is §3 lines 405-451: the §6.5
   likelihood table, the §3.1 confidence damper, Bayes+renormalize. The
   `LearnerEvent` carries only `score`/`confidence`/`event_kind`; WU-5A COMPUTES
   the posterior and the persisted `prior_belief`/`posterior_belief` columns.
3. **WU-5A is SHADOW-gated, never auto-promoted** behind a NEW dedicated flag
   `APOLLO_GRAPH_SIM_LAYER3_ENABLED` (default OFF) — §4 decision #1.
4. **Decay, the janitor retry, and the RQ5 hedge are WU-5B, NOT 5A** (§8). 5A
   RECORDS `dt_days_since_last` but applies NO decay multiply in v1.

## 1. Ground truth discovered (load-bearing facts, file:line — RE-VERIFIED post-critique)

- **`retired shadow result`** (`apollo/handlers/done_grading.py:91-113`): `run_id`,
  `grade: GradeResult`, `audited: AuditedGrade`, `normalization_confidence: float`,
  `reference_graph_hash: str`, `opposes_map: Mapping[str,str]`,
  `turn_order: Mapping[str,int]`, `graph_sim_rubric: dict`, `calibration`,
  `diagnostic`. **It does NOT carry `user_id`/`search_space_id`, `parser_confidence`,
  or any Done timestamp.** `done.py` has `sess` in scope
  (`sess.user_id`/`sess.search_space_id`/`sess.concept_id`) AND the raw
  `student_graph` (passed into `retired graph simulation`, `done.py:385-389`) — so
  WU-5A's entry takes `sess`, the `retired shadow result`, an explicitly-captured
  `done_ts`, and a `parser_confidence` scalar computed in `done.py` from
  `student_graph`. **No FK chase up `apollo_problem_attempts`.**
- **`convert_findings_to_events` is PURE and abstention-correct**
  (`apollo/grading/events.py:68-102`): `if audited_grade.abstained: return ()`
  (line 89-90); `_apply_suppression` drops `suppressed_event_kinds`. WU-5A's
  "skip-when-abstained / skip-when-empty" is AUTOMATIC — `()` → write nothing. No
  re-implementing §6.6.
- **The COVERED score already incorporates resolution confidence**
  (`events.py:302-329`): `_covered_event` sets `score=_plain_covered_score(finding)`
  (= finding.score else finding.confidence else 1.0) and `confidence=finding.confidence`
  separately. **So `event.score` is the resolution-scaled coverage; `event.confidence`
  is the resolution confidence — they are DIFFERENT fields.** (Pins finding #8: s
  carries resolution confidence once; q must not re-multiply it.)
- **`AMBIGUOUS_ORDER_SCORE=0.5`, `AMBIGUOUS_ORDER_CONFIDENCE=0.5`,
  `MIXED_UNDERSTANDING_FLAG='mixed-understanding'`** are SHIPPED constants
  (`events.py:52-54`). 5A1 IMPORTS them — never re-hardcodes `0.5`.
- **`LearnerEvent`** (`apollo/grading/event_model.py:41-58`): `canonical_key: str`,
  `event_kind: LearnerEventKind` (covered|missing|partial|misconception|corrected),
  `score: float|None`, `confidence: float|None`, `misconception_code: str|None`,
  `evidence_node_ids: tuple[str,...]`, `reference_step_id: str|None`,
  `diagnostic_flags: tuple[str,...]` ('edge-gap'/'mixed-understanding' — data-only,
  NOT a DB column; 5A IGNORES on write). **No `parser_confidence` field.**
- **`AuditedGrade` is frozen** (`apollo/grading/audited_grade.py:59-74`) and carries
  `grade, findings, abstention_reasons, abstained, suppressed_event_kinds,
  alias_candidates` — **it does NOT carry `min_parser_confidence`.** (Confirms
  parser_confidence is NOT on the handoff and must be sourced explicitly in `done.py`.)
- **`min_parser_confidence_of(nodes) -> float`** is a SHIPPED pure helper
  (`apollo/grading/abstention.py:70-76`): `min(n.parser_confidence …)`, `1.0` for
  empty, "MIN, NEVER mean (§6.6 binding)." **5A reuses this verbatim** to compute
  the parser_confidence scalar from `student_graph.nodes` — same source the §6.6
  gate uses.
- **`apollo_mastery_events` ORM** (`apollo/persistence/models.py:427-476`):
  `user_id UUID NOT NULL`, `search_space_id INT NOT NULL`, `entity_id BIGINT NOT
  NULL FK`, `attempt_id BIGINT NULL FK ON DELETE SET NULL`, `event_kind TEXT NOT
  NULL`, `score`, `misconception_code`, `parser_confidence`, `grader_confidence`,
  `negotiation_move` (NULL v1), `reference_step_id`, `prior_belief REAL[] NOT NULL`,
  `posterior_belief REAL[] NOT NULL`, `mastery_after REAL NOT NULL`,
  `dt_days_since_last REAL NULL`, `evidence_node_ids JSON NOT NULL default list`.
  **UNIQUE (attempt_id, entity_id, event_kind) NULLS NOT DISTINCT** (models.py:468-475).
  The length-3 / 0-1 CHECKs are in migration 026, real-PG ONLY.
- **`apollo_learner_state` ORM** (`apollo/persistence/models.py:397-424`): PK
  `(user_id, search_space_id, entity_id)`; `belief REAL[] NOT NULL`, `mastery
  REAL NOT NULL`, `confidence REAL NOT NULL`, `misconception_code TEXT NULL`,
  `evidence_count INT DEFAULT 0`, `last_evidence_at TIMESTAMPTZ NULL`, `updated_at`.
  Pure snapshot — no `run_id`/`comparison_version`.
- **`_RealArrayType = ARRAY(REAL).with_variant(JSON(), "sqlite")`** (models.py:45).
  **The SQLite `db` fixture has NO array-length / bounds CHECKs** — constraint
  tests MUST run on Testcontainers (`db_session`), never the unit `db` fixture.
- **entity_id = surrogate `apollo_kg_entities.id`, joined from `canonical_key` at
  WRITE time.** `LearnerEvent.canonical_key` is a STRING; both Layer-3 `entity_id`
  cols are NOT NULL BIGINT FKs. `load_entity_specs(db, *, search_space_id=,
  concept_id=)` (`apollo/knowledge_graph/canon_projection.py:117-173`) returns
  `CanonNodeSpec` whose `.key` is the surrogate id (line 65) and `.canonical_key`
  the string (line 68). WU-4C1 already builds the map
  (`done_grading.py:190`: `{spec.canonical_key: spec.key for spec in specs}`). An
  UNMAPPED canonical_key → its event is **SKIPPED** (the FK is NOT NULL; never a
  stub/NULL insert).
- **The tripwires:** `test_done_shadow_route_postgres.py:144-148` (run count==1,
  stays); `:151-160` (`me==0`, `ls==0` — stays GREEN with LAYER3 flag OFF, the
  default, so 5A's flag-off path is byte-identical to today);
  `test_done_grading_unit.py:473-491` (`test_no_mastery_events_written` — stays
  GREEN because 5A never calls the converter from `done_grading.py`). The
  shadow-route student graph node carries `parser_confidence=0.95` (test line 54).
- **No migration.** 026 shipped both tables, all CHECKs, the UNIQUE NULLS NOT
  DISTINCT, `learner_update_pending`, the Q2/Q3 indexes. 027 dropped
  `concept_cluster_id`. Next free is 028, UNUSED by 5A.
- **Cold-start + opposes seed.** `learner_model_seed.py:240-258` mints `misc.*`
  entities; cold-start prior `[0.20,0.60,0.20]` (§3 line 407) is a 5A1 constant.
- **Δt anchor — CORRECTED.** §3 line 413-415 binds Δt to the freeze/Done
  timestamp, never `now()`. There is NO Postgres-reachable Done timestamp today.
  **5A captures `done_ts=datetime.now(UTC)` ONCE in `done.py`, threads it into
  `stamp_graded_at(ts=…)` AND `run_learner_update(done_ts=…)`** so both stores
  stamp the same instant. `apollo_learner_state.last_evidence_at` is the prior
  anchor; `dt_days_since_last = (done_ts − prior.last_evidence_at).days` is
  RECORDED; the decay MULTIPLY is WU-5B.

## 2. Recommendation (the split — UNCHANGED; the seam survived critique)

**Split WU-5A into TWO sub-units (WU-5A1, WU-5A2). No third.** All three critics
confirmed the two-way seam is SOUND: 5A1 (pure 3-state Bayesian filter, no
IO/containers) cleanly isolates the §6 math landmines from 5A2 (all-or-nothing
Layer-3 persist + ~10-line `done.py` wiring), mirroring the shipped WU-4B2/4B3/4C1
pure-core / persist / done-wire pattern.

- **WU-5A1 — the pure 3-state Bayesian filter core** (`apollo/learner_model/`):
  the cold-start prior, the §6.5 likelihood table WITH the boundary floor, the §3.1
  damper, Bayes+renormalize, the mastery/confidence readouts, and the
  `event + prior + confidences → BeliefUpdate + *RowSpec` mapping. **PURE — no DB,
  no LLM, no Neo4j, no containers.** This is where ALL four math defects are PINNED
  with golden-vector tests.
- **WU-5A2 — the all-or-nothing Layer-3 persist + Done wiring**
  (`apollo/learner_model/persistence.py` + `apollo/handlers/learner_update.py` +
  the `done.py` edit): calls the converter (step 16), resolves canonical_key →
  entity_id, reads priors `SELECT … FOR UPDATE`, applies 5A1, supersedes prior
  events, appends + upserts in ONE txn, behind `APOLLO_GRAPH_SIM_LAYER3_ENABLED`.
  Flips the two empty-table tripwires (flag ON). **Real-PG (Testcontainers) gate;
  no Neo4j, no LLM.**

A three-way split (math / persist / done-wire) was considered and rejected: the
persist layer and the `done.py` call share one transaction, one flag, and one
`retired shadow result` substrate; the `done.py` edit is a ~10-line call site that
cannot be independently green without the persist function it calls.

## 3. Sub-units

### WU-5A1 — the pure 3-state Bayesian filter core

**One-line:** Implement the §3 belief filter as pure arithmetic — cold-start prior,
the §6.5 per-event likelihood table WITH a boundary floor, the §3.1 damper, Bayes +
renormalize, the mastery/confidence readouts, and the
`(LearnerEvent, prior_belief, parser_confidence, grader_confidence, done_ts) →
BeliefUpdate + (MasteryEventRowSpec, LearnerStateRowSpec)` mapping — with NO IO.

**SEAM (owns vs WU-5A2):** WU-5A1 owns every NUMBER. It takes in-memory inputs and
returns immutable value objects. It does NOT read/write Postgres, does NOT call
`convert_findings_to_events`, does NOT know `entity_id`, transactions, flags, or
`sess`. It applies NO decay. Handoff: pure functions + frozen `*RowSpec` dataclasses.

**Scope — files to create:**

- `apollo/learner_model/__init__.py` (NEW) — package marker (mirrors `apollo/grading/`).
- `apollo/learner_model/belief.py` (NEW) — the math, NAMED CONSTANTS:
  - `COLD_START_PRIOR: tuple[float,float,float] = (0.20, 0.60, 0.20)` (§3 line 407).
  - `GAMMA: float = 1.5` (§6.5 covered exponent, line 421).
  - `LIKELIHOOD_FLOOR: float = 0.02` (**NEW — the BKT boundary-floor fix for the
    absorbing-zero defect**; every post-floor likelihood component is clamped to
    `≥ LIKELIHOOD_FLOOR` BEFORE the damper, so no component is ever exactly 0 —
    §3 line 427 "never 0").
  - `MISCONCEPTION_FLAG_THRESHOLD: float = 0.5` (§3 line 232 — set iff p_misc is
    argmax AND ≥ this; two-step from cold-start, see tests).
  - `MISSING_LIKELIHOOD = (0.7, 1.0, 0.4)`; `MISCONCEPTION_LIKELIHOOD = (3.0, 1.0,
    0.2)`; `CORRECTED_LIKELIHOOD = (0.5, 1.5, 1.2)` (§6.5 lines 422-424).
  - `NO_OP_LIKELIHOOD = (1.0, 1.0, 1.0)` (blank cells / damper no-op anchor).
  - `likelihood_for_event(event) -> tuple[float,float,float]`:
    - covered → `clamp_each((1−s)^γ, 1−|2s−1|^γ, s^γ)` where `s=event.score` and
      `clamp_each` raises each component to `≥ LIKELIHOOD_FLOOR`.
    - missing → `MISSING_LIKELIHOOD`; misconception → `MISCONCEPTION_LIKELIHOOD`;
      corrected → `CORRECTED_LIKELIHOOD`.
    - **partial → covered at `s = event.score` (the converter already set it to
      `AMBIGUOUS_ORDER_SCORE=0.5`, IMPORTED from `events.py`, never re-literal'd).**
    - The misconception/corrected fixed rows are ALSO floored (the 0.2 is already
      > floor, so a no-op, but the clamp is applied uniformly for invariance).
  - `damp(L, q) -> tuple[float,float,float]` — `L ← q·L + (1−q)·NO_OP_LIKELIHOOD`
    (linear interpolation, §3.1 line 437). q∈[0,1].
  - `bayes_update(prior, L) -> tuple[float,float,float]` — `normalize(prior ⊙ L)`;
    if `sum == 0` return `prior` unchanged (the §3 zero-sum guard — unreachable now
    that L is floored and the prior sums to 1, but kept defensively).
  - `mastery_of(belief) -> float` — `0.5·p_shaky + p_mastered` (§3 line 445).
  - `confidence_of(belief) -> float` — `1 − entropy(belief)/log 3` (§3 line 446);
    guard `0·log0 = 0`.
  - `misconception_code_of(belief, event) -> str | None` — the code iff p_misc is
    argmax AND `≥ MISCONCEPTION_FLAG_THRESHOLD` AND the event carries one (§3 line 232).
- `apollo/learner_model/state_model.py` (NEW) — frozen value objects:
  - `BeliefUpdate` (frozen): `prior_belief`, `posterior_belief`, `mastery_after`,
    `confidence_after`, `misconception_code`, `parser_confidence`, `grader_confidence`,
    `dt_days_since_last`.
  - `MasteryEventRowSpec` (frozen) — 1:1 onto `apollo_mastery_events` non-id columns;
    `entity_id: int | None = None` here, REQUIRED non-None before write (mirrors
    `FindingRowSpec`'s entity_id pattern).
  - `LearnerStateRowSpec` (frozen) — 1:1 onto `apollo_learner_state` non-pk-context
    columns (belief, mastery, confidence, misconception_code, evidence_count, last_evidence_at).
- `apollo/learner_model/update.py` (NEW) — pure orchestration:
  - `apply_event(event, *, prior_belief, prior_last_evidence_at, parser_confidence,
    grader_confidence, done_ts) -> BeliefUpdate`: computes `q = parser_confidence ·
    grader_confidence`, runs `likelihood_for_event → damp → bayes_update`, derives
    mastery/confidence/misconception_code, computes `dt_days_since_last` from
    `prior_last_evidence_at` vs `done_ts` (None prior → None Δt). PURE.
  - `event_to_row_specs(event, update, *, ids…) -> (MasteryEventRowSpec, LearnerStateRowSpec)`.

**Public API exposed for WU-5A2:**

```python
# apollo/learner_model/belief.py
COLD_START_PRIOR: tuple[float, float, float]
LIKELIHOOD_FLOOR: float
MISCONCEPTION_FLAG_THRESHOLD: float
def likelihood_for_event(event: LearnerEvent) -> tuple[float, float, float]: ...
def damp(L, q: float) -> tuple[float, float, float]: ...
def bayes_update(prior, L) -> tuple[float, float, float]: ...
def mastery_of(belief) -> float: ...
def confidence_of(belief) -> float: ...
def misconception_code_of(belief, event: LearnerEvent) -> str | None: ...
# apollo/learner_model/update.py
@dataclass(frozen=True)
class BeliefUpdate: ...
def apply_event(event, *, prior_belief, prior_last_evidence_at,
                parser_confidence, grader_confidence, done_ts) -> BeliefUpdate: ...
# apollo/learner_model/state_model.py
@dataclass(frozen=True)
class MasteryEventRowSpec: ...
@dataclass(frozen=True)
class LearnerStateRowSpec: ...
```

**Dependencies:** `apollo.grading.event_model.LearnerEvent`/`LearnerEventKind`
(frozen WU-4B2) and the IMPORTED `AMBIGUOUS_ORDER_SCORE` from
`apollo.grading.events`. NO other Apollo imports. Stdlib `math` only.

**Migration?** No. **Real-infra tests?** None — fully pure.

**PINNED golden tests (NUMERICALLY REPRODUCED — these are the acceptance gates):**

- **GOLDEN VECTOR table — covered likelihood + argmax (replaces the false "all mid →
  shaky" prose):**

  | s | L (floored) | argmax |
  |---|---|---|
  | 0.00 | (1.0000, 0.0200, 0.0200) | misc |
  | 0.25 | (0.6495, 0.6464, 0.1250) | **misc** (NOT shaky — intended lean below s≈0.4) |
  | 0.50 | (0.3536, 1.0000, 0.3536) | shaky |
  | 0.75 | (0.1250, 0.6464, 0.6495) | mastered |
  | 1.00 | (0.0200, 0.0200, 1.0000) | mastered |

  Assert these EXACT vectors (3-dp) and `argmax==shaky` ONLY at s=0.5. Document
  the s<0.5→misc lean as intended.
- **Absorbing-zero is FIXED:** an exact-tier perfect covered (s=1.0, q=1.0) from
  cold-start leaves `p_misc > 0` (reproduced: posterior ≈ (0.0185, 0.0556, 0.9259)),
  AND a SUBSEQUENT misconception event STILL moves p_misc up (reproduced: → ≈0.1875).
  Without the floor, both are 0.0 forever — assert the floored path.
- **Cold-start readouts:** `mastery_of(COLD_START_PRIOR) == 0.50` (NOT 0.40 — the
  spec prose is wrong, §OPEN-DECISIONS); `confidence_of(COLD_START_PRIOR)` =
  `1 − entropy([.2,.6,.2])/log3` (assert the computed value).
- **Misconception two-step (replaces "≥0.5 in one step"):** first misconception
  from cold-start (q=1) → p_misc=0.4839, argmax=misc, `misconception_code_of`
  returns **None** (argmax but < threshold); second → p_misc=0.7475,
  `misconception_code_of` returns the code.
- **partial maps to covered at s=0.5:** `likelihood_for_event(partial@0.5)` ==
  `likelihood_for_event(covered@0.5)` → argmax=shaky (the named γ-pin the proposal
  promised — confirms, does not break, the constant).
- **Damper:** `damp(L, 0.0) == NO_OP_LIKELIHOOD` (strict no-op); `damp(L, 1.0) == L`;
  monotone in q; output never 0 (since L is floored and NO_OP is 1.0).
- **q sourcing pin (finding #8):** `apply_event` computes `q = parser_confidence ·
  grader_confidence`; assert a covered event's resolution confidence appears in
  `s` (`event.score`) EXACTLY ONCE and is NOT re-multiplied into q (construct a
  covered event with known score and confidences, assert the posterior matches the
  hand-computed `normalize(prior ⊙ damp(L(s), parser·grader))`).
- **bayes invariants:** sums-to-1; zero-sum guard returns the prior.
- **Δt:** `dt_days_since_last` from a fixed `prior_last_evidence_at` vs a fixed
  `done_ts` (anchored, never `now()`); None prior → None.

---

### WU-5A2 — the all-or-nothing Layer-3 persist + Done wiring

**One-line:** Capture `done_ts` once, compute `parser_confidence` from the student
graph, call `convert_findings_to_events` on the `retired shadow result`, resolve each
event's `canonical_key → entity_id`, read the prior `LearnerState` rows
`SELECT … FOR UPDATE`, supersede this attempt's prior mastery events, apply 5A1's
pure update per entity (recomputing from the EVENT-LOG prior, not the state row),
and append `apollo_mastery_events` + upsert `apollo_learner_state` in ONE
all-or-nothing Postgres transaction — all behind `APOLLO_GRAPH_SIM_LAYER3_ENABLED`
(default OFF).

**SEAM (owns vs WU-5A1 / WU-4x / WU-5B):** WU-5A2 owns every IO byte and the
transaction. FIRST caller of `convert_findings_to_events`. Computes NO belief number
(delegates to 5A1). Does NOT touch run/findings persistence (WU-4B3), the shadow
chain (WU-4C1), the student grade, or `done_grading.py`. Applies NO decay / no
janitor (WU-5B).

**Scope — files to touch/create:**

- `apollo/learner_model/persistence.py` (NEW) — the real-PG seam, mirroring
  `apollo/grading/persistence.py`:
  - `persist_learner_update(db, *, sess, shadow, done_ts, parser_confidence,
    canon_key_by_canonical_key) -> LearnerUpdateResult` — one public callable.
    Steps (ALL inside the caller's open txn — flush only):
    1. `events = convert_findings_to_events(shadow.audited, opposes_map=shadow.opposes_map,
       turn_order=shadow.turn_order)`. If `events == ()` → return empty result
       (`abstained=True`), write NOTHING.
    2. `grader_confidence = shadow.normalization_confidence * 1.0` (comparison_confidence
       = 1.0 in v1, §3 line 432-435). One per run.
    3. Resolve each `event.canonical_key → entity_id` via the injected map; collect
       unmapped keys (skipped, logged, NEVER a NULL/stub insert).
    4. **Supersede:** `DELETE FROM apollo_mastery_events WHERE attempt_id = :attempt_id`
       (ALL entity/kind for this attempt — covers misconception→corrected kind
       changes, finding #6), same txn.
    5. `SELECT … FOR UPDATE` the affected `LearnerState` rows for `(user_id,
       search_space_id, entity_id ∈ resolved)`.
    6. **Recompute base = the EVENT LOG, never the state row** (finding #6): for
       each affected `(user, entity)`, `prior_base = posterior_belief` of the
       `apollo_mastery_events` row with `MAX(created_at) WHERE entity_id = :e AND
       attempt_id IS DISTINCT FROM :attempt_id`, else `COLD_START_PRIOR`. Apply
       5A1's `apply_event` from `prior_base` (NOT from the possibly-self-contaminated
       current `LearnerState.belief`).
    7. Build the two row specs per entity; append `MasteryEvent` rows + upsert
       `LearnerState` (belief, mastery, confidence, misconception_code,
       evidence_count+1, last_evidence_at=done_ts).
    8. `flush()` ONLY — the caller owns `commit()` (mirrors `persist_comparison_run`'s
       "does NOT commit", `grading/persistence.py:24/216`).
  - `*_orm_from_spec` helpers (spec → `MasteryEvent`/`LearnerState` ORM), mirroring
    `_run_orm_from_spec`/`_finding_orm_from_spec`. `evidence_node_ids` written as
    `list(...)` (JSON).
- `apollo/handlers/learner_update.py` (NEW, thin) —
  `run_learner_update(db, *, sess, attempt, shadow, done_ts, parser_confidence)
  -> LearnerUpdateResult | None`: re-derives `canon_key_by_canonical_key` via
  `load_entity_specs(db, search_space_id=sess.search_space_id, concept_id=sess.concept_id)`
  (same as `done_grading.py:187-190`), calls `persist_learner_update`, then
  `db.commit()` (the all-or-nothing boundary). On ANY failure inside the txn:
  rollback, set `attempt.learner_update_pending=True`, commit that flag in a
  SEPARATE tiny txn, re-raise (NO-FALLBACK, mirrors `done_grading._set_pending_and_commit`).
  **`convert_findings_to_events` is imported/called HERE/in persistence.py — NEVER
  in `done_grading.py`** (keeps `test_no_mastery_events_written` green).
- `apollo/handlers/done.py` (EDIT, ~12 lines):
  - At the stamp site (`done.py:345`): capture `done_ts = datetime.now(UTC)` ONCE,
    pass it to `stamp_graded_at(attempt_id=attempt.id, ts=done_ts)` (signature
    widened, see below).
  - After the existing shadow block (`done.py:381-401`), add:
    ```
    if shadow is not None and _graph_sim_layer3_enabled():
        parser_confidence = min_parser_confidence_of(student_graph.nodes)
        await run_learner_update(db, sess=sess, attempt=attempt, shadow=shadow,
                                 done_ts=done_ts, parser_confidence=parser_confidence)
    ```
    Gated by the NEW `_GRAPH_SIM_LAYER3_FLAG` constant (mirrors
    `_GRAPH_SIM_SHADOW_FLAG`/`_GRAPH_SIM_LIVE_FLAG`, `done.py:63/70`). Flag OFF
    (default) → the call never fires → byte-identical to today, tripwires stay 0.
    Reached only when `shadow is not None`.
- `apollo/knowledge_graph/store.py` (EDIT, ~3 lines) — **widen `stamp_graded_at`'s
  signature** from `(*, attempt_id)` to `(*, attempt_id, ts: str | datetime | None
  = None)`: when `ts` is provided use it (normalize to ISO), else fall back to
  `_utc_now_iso()` (back-compat for any other caller). This makes Neo4j's
  `n.graded_at` and Postgres's `last_evidence_at` stamp the IDENTICAL instant
  (resolves the done_ts HIGH finding). Reconcile `docs/architecture/apollo.md`
  (store.py owner).

**Public API exposed:**

```python
# apollo/learner_model/persistence.py
@dataclass(frozen=True)
class LearnerUpdateResult:
    events_written: int
    states_upserted: int
    skipped_unmapped: tuple[str, ...]   # canonical_keys with no entity_id
    abstained: bool                     # True when events == ()
async def persist_learner_update(db, *, sess, shadow, done_ts, parser_confidence,
                                 canon_key_by_canonical_key) -> LearnerUpdateResult: ...
# apollo/handlers/learner_update.py
async def run_learner_update(db, *, sess, attempt, shadow, done_ts,
                             parser_confidence) -> LearnerUpdateResult | None: ...
# apollo/handlers/done.py
_GRAPH_SIM_LAYER3_FLAG = "APOLLO_GRAPH_SIM_LAYER3_ENABLED"  # default OFF
```

**Dependencies:** WU-5A1 (`belief`/`update`/`state_model`), WU-4B2
(`convert_findings_to_events`), WU-3C1 (`load_entity_specs`), WU-4C1
(`retired shadow result`, `sess`, `learner_update_pending`),
`min_parser_confidence_of` (`apollo.grading.abstention`), the
`LearnerState`/`MasteryEvent`/`ProblemAttempt` ORM. No new external deps.

**Migration?** No — 026 shipped everything.

**Real-infra tests?** **YES — real-PG (Testcontainers `tests/database` gate,
`pytest.mark.integration` / `db_session`), the heaviest in WU-5A.** ALL
constraint-dependent assertions (belief length 3, score/mastery 0-1, UNIQUE NULLS
NOT DISTINCT, NOT-NULL `entity_id` FK rejection, `SELECT … FOR UPDATE`, all-or-nothing
rollback) live in `tests/database/test_done_layer3_route_postgres.py` under the
SAME harness as `test_done_shadow_route_postgres.py` — **NEVER the unit `db`
(SQLite) fixture** (else they pass vacuously). No Neo4j, no LLM.

**Key behavioral tests (acceptance gates):**
- Flag OFF (default) → `done.py` writes NO Layer-3 rows; `test_done_shadow_route_postgres.py:151-160`
  (`me==0`, `ls==0`) stays GREEN unchanged.
- Flag ON, happy path → one `MasteryEvent` per mapped entity, one `LearnerState`
  upsert per entity; belief sums-to-1; mastery∈[0,1]; first-touch `prior_belief == COLD_START_PRIOR`.
- Abstained `retired shadow result` → converter returns `()` → NO Layer-3 rows,
  `result.abstained=True`; the run/findings (WU-4C1) count unchanged.
- Unmapped `canonical_key` → that event SKIPPED (in `result.skipped_unmapped`), the
  others still write; **no NULL entity_id insert, no FK crash.**
- **[ACCEPTANCE-BLOCKING] Retry-identical:** run `persist_learner_update` twice for
  the SAME attempt with the SAME events → belief IDENTICAL to one run (no
  double-count); the supersede DELETE + event-log recompute guarantees it.
- **[ACCEPTANCE-BLOCKING] Retry-changed-kind:** second run flips an entity's event
  from misconception→corrected → no UNIQUE NULLS NOT DISTINCT crash (the
  attempt-wide DELETE covers all kinds), no double-count, the final belief reflects
  ONLY the second run's events.
- All-or-nothing: inject a failure between the event append and the state upsert →
  rollback leaves BOTH tables untouched; `learner_update_pending=True` set +
  committed in a separate txn.
- `SELECT … FOR UPDATE` issued on the learner rows before read-modify-write (assert
  via emitted SQL / a concurrent-update test).
- Second Done for the same `(user, entity)` at a later `done_ts` → `dt_days_since_last`
  is the real lag from the prior `last_evidence_at`; `evidence_count` increments;
  the new `last_evidence_at == done_ts`.
- `done_ts` identity: assert Neo4j `n.graded_at` (from `stamp_graded_at(ts=done_ts)`)
  and `apollo_learner_state.last_evidence_at` carry the SAME instant (the integration
  test may stub `stamp_graded_at` — if so, assert it was called WITH `ts=done_ts`).

## 4. Cross-cutting decisions (LOCKED)

1. **SHADOW-vs-LIVE belief write — a NEW dedicated flag
   `APOLLO_GRAPH_SIM_LAYER3_ENABLED` (default OFF everywhere incl. the test
   regression path; ON only in the Layer-3 integration tests).** THREE orthogonal
   flags now:
   - `[retired A7 shadow switch]` (WU-4C1) — run the chain (persist run/findings).
   - `APOLLO_GRAPH_SIM_LIVE_ENABLED` (WU-4C2) — promote the graph-sim GRADE to the student.
   - `APOLLO_GRAPH_SIM_LAYER3_ENABLED` (WU-5A2, NEW) — write `apollo_mastery_events`/
     `apollo_learner_state`. The §6.7 calibration gate.
   Independent: Layer-3 can be enabled to validate the filter on real episodes
   WITHOUT promoting the grade, and vice versa. Writing the belief requires LAYER3
   ON regardless of LIVE.
2. **The all-or-nothing transaction boundary lives in `handlers/learner_update.py`,
   not in `persistence.py`.** `persist_learner_update` flushes only; `run_learner_update`
   owns the single `commit()` making steps 16–17 atomic (§3 line 456-461). Events
   AND state upserts in one txn — a crash between rolls back BOTH. SEPARATE from the
   WU-4C1 run/findings txn (committed at `done_grading.py:256`) and the OLD grade
   txn (committed earlier in `done.py:325`); 5A adds the FOURTH stage.
3. **`entity_id=None` vs UNIQUE NULLS NOT DISTINCT vs the NOT-NULL FK — three
   different NULLs:**
   - `apollo_graph_comparison_findings.entity_id` is NULLABLE; v1 leaves it NULL (WU-4B3).
   - `apollo_mastery_events.entity_id` / `apollo_learner_state.entity_id` are **NOT
     NULL** — 5A2 MUST resolve the surrogate first; an unmapped key → the event is
     SKIPPED (recorded in `skipped_unmapped`), NEVER a stub.
   - **UNIQUE NULLS NOT DISTINCT is on `attempt_id`** (the column that CAN be NULL):
     it treats NULL `attempt_id` as equal so a retry can't double-insert. In the
     normal Done path `attempt_id` is concrete → behaves like a plain UNIQUE on
     `(attempt_id, entity_id, event_kind)`.
   - **All-or-nothing applies to the WRITE (the txn), NOT to the mapping-eligibility
     filter:** eligible (mapped) events all commit together or not at all; ineligible
     (unmapped) events are filtered out BEFORE the txn. One unmapped entity must not
     void the whole attempt's update.
4. **The `opposes_map`-empty prerequisite for `corrected` events is ALREADY HANDLED
   upstream and is a non-error in 5A.** `convert_findings_to_events` (frozen WU-4B2),
   when `opposes_map` is empty or a key is absent, emits a STANDALONE `misconception`
   instead of `corrected` (`events.py` conflict path). In v1 the misconception bank
   carries no opposes column so `opposes_map` is structurally empty
   (`done_grading.py:120-140`, `_misconceptions_dict` sets `opposes=None`) → all
   conflicts degrade to standalone misconceptions. **5A does NOT validate opposes_map
   completeness and does NOT error on an empty one** — it consumes whatever events
   the frozen converter returns. (Note in the apollo doc so it isn't mistaken for a
   defect during pilot.)
5. **Retry semantics: supersede-by-delete-within-the-txn, recompute base from the
   EVENT LOG (NOT the state row).** The recompute base is ALWAYS re-derived from
   `apollo_mastery_events`:
   - DELETE all `apollo_mastery_events` for this `attempt_id` (ANY entity/kind)
     inside the txn (covers misconception→corrected kind changes across ALL kinds).
   - For each affected `(user, entity)`: `prior_base = posterior_belief` of the row
     with `MAX(created_at) WHERE entity_id = :e AND attempt_id IS DISTINCT FROM
     :attempt_id`, else `COLD_START_PRIOR`.
   - Recompute the `LearnerState` upsert from `prior_base` via `apply_event`, **never
     from the possibly-self-contaminated current `LearnerState.belief`** (the state
     row may already have been mutated by this attempt's first run in some race
     orderings). This mirrors `persist_comparison_run`'s supersede
     (`grading/persistence.py:229-244`). **Pinned by the two acceptance-blocking
     retry tests in §3.** (Full lazy-decay-on-read recompute is WU-5B.)
6. **Which tripwire tests flip (and which stay):**
   - **FLIP:** a NEW `tests/database/test_done_layer3_route_postgres.py` asserts
     `MasteryEvent`/`LearnerState` are written (flag ON).
   - **STAYS GREEN unchanged:** `test_done_shadow_route_postgres.py:151-160` (`me==0`,
     `ls==0`) — runs with LAYER3 OFF, now the "flag-off regression guard." Do NOT
     edit those assertions.
   - **STAYS GREEN unchanged:** `test_no_mastery_events_written`
     (`test_done_grading_unit.py:473-491`) — 5A never calls the converter from
     `done_grading.py`.
   - **STAYS GREEN unchanged:** `test_learner_model_allowlists.py` — 5A writes
     `event_kind.value` verbatim, no new kind.
7. **Δt anchored to a SINGLE handler-captured `done_ts`, never `now()`; NO decay
   multiply in v1 (CORRECTED — resolves the HIGH finding).** There is NO
   Postgres-reachable Done/freeze timestamp (the old draft's `attempt.graded_at` does
   not exist; `stamp_graded_at` is Neo4j-only and discards its ts). Therefore:
   - `done.py` captures `done_ts = datetime.now(UTC)` ONCE at the stamp site.
   - It threads `done_ts` into `stamp_graded_at(ts=done_ts)` (signature widened) AND
     `run_learner_update(done_ts=done_ts)` → Neo4j and Postgres stamp the IDENTICAL
     instant.
   - v1's "freeze timestamp" IS this handler-captured value (Δt≈0 for the current
     attempt; the value only matters as the prior anchor on the NEXT episode's Δt).
   - `apply_event` records `dt_days_since_last = (done_ts − prior.last_evidence_at).days`
     (None prior → None). The decay MULTIPLY (§3 line 409-410) is WU-5B.
8. **parser_confidence / grader_confidence sourcing (LOCKED — resolves findings #7/#8):**
   - `q = parser_confidence · grader_confidence` (§3 line 430).
   - `parser_confidence = min_parser_confidence_of(student_graph.nodes)` — computed
     in `done.py` reusing the SHIPPED §6.6 helper (`abstention.py:70`), the SAME
     min-over-turns the abstention gate uses; threaded into `run_learner_update`.
   - `grader_confidence = shadow.normalization_confidence × 1.0` (comparison_confidence
     = 1.0 in v1), one per run, off the frozen `retired shadow result.normalization_confidence`.
   - The COVERED score `s = event.score` is ALREADY resolution-scaled by the
     converter (`events.py:307-329`); `event.confidence` (resolution confidence) is
     NOT re-multiplied into q. (Pinned by the q-sourcing test in §3.)

## 5. Per-sub-unit contract tables

### WU-5A1 — pure 3-state Bayesian filter core

| Field | Value |
|-------|-------|
| Kind | Pure library (no IO) — mirrors `apollo/grading/event_model.py` + `events.py` |
| Files (NEW) | `apollo/learner_model/__init__.py`, `belief.py`, `state_model.py`, `update.py` |
| Frozen public API | `COLD_START_PRIOR`; `LIKELIHOOD_FLOOR`; `MISCONCEPTION_FLAG_THRESHOLD`; `likelihood_for_event`; `damp`; `bayes_update`; `mastery_of`; `confidence_of`; `misconception_code_of`; `apply_event(...)->BeliefUpdate`; frozen `BeliefUpdate`/`MasteryEventRowSpec`/`LearnerStateRowSpec` |
| Consumes | `apollo.grading.event_model.LearnerEvent`/`LearnerEventKind`; imported `AMBIGUOUS_ORDER_SCORE` from `apollo.grading.events` (both frozen WU-4B2) |
| depends_on | WU-4B2 |
| real_infra | none (pure; `math` stdlib only) |
| migration | none |
| Side effects | none |
| Key risks owned | absorbing-zero floor; γ=1.5 golden-vector behavior (s=0.25→misc, s=0.5→shaky); damper interpolation form; misconception two-step 0.5 threshold; cold-start mastery 0.50; q-no-double-count; zero-sum guard |

### WU-5A2 — all-or-nothing Layer-3 persist + Done wiring

| Field | Value |
|-------|-------|
| Kind | IO/persistence + thin Done wiring — mirrors `apollo/grading/persistence.py` (WU-4B3) + `done.py` edit (WU-4C1) |
| Files (NEW) | `apollo/learner_model/persistence.py`, `apollo/handlers/learner_update.py`, `tests/database/test_done_layer3_route_postgres.py` |
| Files (EDIT) | `apollo/handlers/done.py` (~12 lines: capture done_ts + new flag + gated call + parser_confidence), `apollo/knowledge_graph/store.py` (~3 lines: widen `stamp_graded_at` to accept `ts`) |
| Frozen public API | `persist_learner_update(db,*,sess,shadow,done_ts,parser_confidence,canon_key_by_canonical_key)->LearnerUpdateResult` (flush-only); `run_learner_update(db,*,sess,attempt,shadow,done_ts,parser_confidence)->LearnerUpdateResult\|None` (owns commit + NO-FALLBACK); frozen `LearnerUpdateResult`; `_GRAPH_SIM_LAYER3_FLAG` |
| Consumes | WU-5A1; WU-4B2 (`convert_findings_to_events`); WU-3C1 (`load_entity_specs`); WU-4C1 (`retired shadow result`, `sess`, `learner_update_pending`); `min_parser_confidence_of`; `LearnerState`/`MasteryEvent`/`ProblemAttempt` ORM |
| depends_on | WU-5A1, WU-4C1 (branch tip), WU-4B2, WU-3C1 |
| real_infra | **real-PG (Testcontainers `tests/database`, `pytest.mark.integration`/`db_session`)** — CHECK constraints, UNIQUE NULLS NOT DISTINCT, NOT-NULL `entity_id` FK, `SELECT … FOR UPDATE`, all-or-nothing rollback. **No Neo4j, no LLM.** |
| migration | none |
| Side effects | DB writes: append `apollo_mastery_events`, upsert `apollo_learner_state`, set `learner_update_pending` on failure, Neo4j `n.graded_at` stamp at the SAME `done_ts`. Gated by `APOLLO_GRAPH_SIM_LAYER3_ENABLED` (default OFF → no writes) |
| Idempotent | yes — same-attempt re-run supersedes (attempt-wide DELETE + event-log recompute) + UNIQUE NULLS NOT DISTINCT guard (§4 decision #5) |
| Key risks owned | transaction atomicity; canonical_key→entity_id join + unmapped-skip; retry/supersede no-double-count (event-log base); done_ts threading; flag-off byte-identity; tripwire flips |

## 6. Risks, uncertainties, spec ambiguities (post-critique)

1. **HIGHEST — transaction atomicity (§3 line 456-461) is HARD BINDING.** A partial
   Layer-3 write is cascading read-modify-write corruption. Mitigations (mandatory):
   flush-only persist; single `commit()` in `run_learner_update`; `SELECT … FOR
   UPDATE`; a forced mid-write-failure rollback test; `learner_update_pending=True`
   in a SEPARATE txn on failure (NO-FALLBACK). Confidence: HIGH (mirrors WU-4B3 +
   WU-4C1, both shipped).
2. **HIGH→RESOLVED — done_ts provenance.** Was factually wrong (non-existent
   column). Now: single `done_ts` captured in `done.py`, threaded into both stores;
   `stamp_graded_at` widened to accept `ts`. Confidence: HIGH (the fix is mechanical
   and verified against store.py:537-564 / models.py:265-285).
3. **HIGH→RESOLVED — absorbing-zero math.** `L_covered(1.0)=(0,0,1)` reproduced;
   `LIKELIHOOD_FLOOR=0.02` floor verified to rescue it while preserving every pin.
   Confidence: HIGH. **The §3-line-427 self-contradiction is a SPEC bug — see
   §OPEN-DECISIONS (do not silently change the constant; the fix is the floor, which
   honors line 427).**
4. **HIGH→RESOLVED — "mid scores land on shaky" partly false.** s=0.25→misc
   reproduced. Replaced with the golden-vector table. Confidence: HIGH.
5. **HIGH→RESOLVED — retry/supersede belief no-double-count.** Recompute base pinned
   to the event log (attempt-wide DELETE + `MAX(created_at) WHERE attempt_id IS
   DISTINCT FROM this`), two acceptance-blocking tests. Confidence: HIGH on the
   mechanism; MEDIUM that the executor implements the event-log read correctly — the
   tests are the guard.
6. **MEDIUM→RESOLVED — cold-start mastery is 0.50, not 0.40.** Reproduced
   `0.5·0.60+0.20=0.50`. Test target = 0.50. **The 0.40 prose is a SPEC defect —
   §OPEN-DECISIONS (correct the prose, or respecify the mastery weighting; do NOT
   let the executor guess).**
7. **MEDIUM→RESOLVED — 0.5 threshold unreachable in one update.** Two-step behavior
   pinned (first unflagged, second flagged). The draft's "≥0.5 in one step" claim is
   removed.
8. **LOW→RESOLVED — parser_confidence sourcing.** `min_parser_confidence_of(student_graph.nodes)`,
   the shipped §6.6 helper. Confidence: HIGH.
9. **LOW→RESOLVED — grader_confidence vs covered score s.** s already resolution-scaled;
   q = parser·grader; no double-apply. Pinned by the q-sourcing test. Confidence: HIGH.
10. **LOW→RESOLVED — partial event has no §6.5 row.** Mapped to covered at
    `s=AMBIGUOUS_ORDER_SCORE=0.5` (IMPORTED from events.py); argmax=shaky. The 0.5
    confidence participates in q like any covered confidence (no special-casing).
    Confidence: HIGH (s=0.5→shaky reproduced).
11. **MEDIUM — `negotiation_move` is NULL in v1 (no bridge).** No Turn→negotiation
    source; write `negotiation_move=NULL`, apply NO negotiation multiplier. Faithful
    to the data (the converter never emits a negotiation signal). Wiring it needs
    `apollo_kg_negotiations` (migration 021) joined to the entity — follow-up.
    Confidence: HIGH this is correct v1.
12. **LOW — constraint tests must run on the real-PG harness, not SQLite.**
    `_RealArrayType` is a JSON variant on SQLite (models.py:45) with NO CHECKs. All
    constraint assertions live under `pytest.mark.integration`/`db_session`. Confidence: HIGH.
13. **LOW — `misconception_code` namespace.** The code is `LearnerEvent.misconception_code`,
    set by the converter to the misconception entity's `canonical_key` (e.g.
    `canon.misc.*`). No separate registry — 5A writes it verbatim. Confidence: HIGH.
14. **LOW — `evidence_node_ids` shape.** `tuple[str,...]` → JSON `list(...)`
    (mirrors `finding_to_row_spec`). Confidence: HIGH.

## 7. Build order (stacked branches)

1. **WU-5A1** branches off `feat/apollo-kg-wu4c2-calibration-diagnostics` (the WU-4C2
   tip, the patch-coverage compare branch). Lands the `apollo/learner_model/` pure
   package (`belief.py`, `state_model.py`, `update.py`). Independently green at ≥95%
   patch coverage with **NO container** (pure golden-vector tests). Highest-uncertainty
   (the §6 math) but lowest-infra — ships FIRST and de-risks the now-PINNED constants
   (the floor, the golden vectors, mastery 0.50, the two-step threshold) before any IO.
2. **WU-5A2** branches off WU-5A1. Lands `apollo/learner_model/persistence.py`,
   `apollo/handlers/learner_update.py`, the `done.py` flag + gated call + done_ts
   capture + parser_confidence, the `store.py` `stamp_graded_at` widening, and
   `tests/database/test_done_layer3_route_postgres.py`. Independently green at ≥95% —
   **trips the `tests/database` real-PG gate (green-not-skipped), no Neo4j.** The
   shadow-route empty-table assertions stay green (flag-off guard); the new test
   asserts the writes flag-ON.
3. **WU-5B** branches off WU-5A2 — decay-on-stale-read (§3 Step 0), the
   `learner_update_pending` janitor retry driver, the RQ5 chat-keyword hedge (§8).
   NOT part of this build.

Each sub-unit is one TDD-executor pass (an "L"): WU-5A1 ≈ 3 pure modules + ~4
golden-vector test files; WU-5A2 ≈ 2 new modules + a ~12-line `done.py` edit + a
~3-line `store.py` edit + the real-PG integration test.

## 8. Out-of-scope vs WU-5B (held firmly)

- **Between-session decay (§3 Step 0, lines 409-415)** — 5A RECORDS
  `dt_days_since_last`, applies no decay (a STALE-READ concern). WU-5B.
- **The `learner_update_pending` janitor RETRY DRIVER** — 5A2 SETS the flag
  (NO-FALLBACK) and `run_learner_update` is independently callable, but the scan/
  re-run/clear loop is WU-5B. (The supersede that makes a retry safe IS in 5A2.)
- **The RQ5 chat-keyword hedge (spec §10)** — reserved `event_kind='chat_question'`
  (migration 026:150); no `apollo_sessions`/`apollo_turns` schema for it. WU-5B/later.
- **Parameter fitting (spec §3 lines 475-481)** — EM emission fit, grid-search decay
  `k`, Elo backfill, per-student graph merging. Offline, never v1.
- **The §6.5 negotiation multiplier + `negotiation_move` sourcing** — no
  Turn→negotiation bridge in v1; column NULL, multiplier not applied. Follow-up.
- **Teacher-dashboard / longitudinal READOUTS (§3 readouts table)** — `AVG(mastery)`,
  "% p_misc ≥ 0.5", the trend series are read-side consumers. 5A only WRITES.

## 9. Doc / coverage obligations (per project contracts)

- **Patch coverage ≥95%** on changed lines (`diff-cover coverage.xml
  --compare-branch=origin/staging --fail-under=95`; intra-stack compare branch
  `feat/apollo-kg-wu4c2-calibration-diagnostics`). WU-5A1 is pure → exhaustible.
  WU-5A2 needs Docker for the `tests/database` gate (green-not-skipped); BOTH the
  flag-on write path and the flag-off regression path covered.
- **NEVER apply migrations to any remote DB.** No migration in 5A. Real-PG tests run
  on LOCAL Testcontainers only.
- **Drift contract:** reconcile `docs/architecture/apollo.md` in the same commit each
  sub-unit lands — **add `apollo/learner_model/**` to its `owns:` globs**, register
  the new modules, document the §6.4 step 16-17 wiring, the THREE-flag model, the
  all-or-nothing txn stage, the done_ts single-capture + `stamp_graded_at` widening,
  the LIKELIHOOD_FLOOR fix, and the opposes-empty→standalone-misconception v1
  behavior; bump `last_verified`.
- **The new flag** `APOLLO_GRAPH_SIM_LAYER3_ENABLED` — declare it as a named module
  constant + documented env flag (like `_SHADOW`/`_LIVE`, `done.py:63/70`). Default
  OFF everywhere incl. prod.

## 10. Summary table

| Sub-unit | Seam (input → output) | Files | Migration | Real-infra | Key risk owned |
|---|---|---|---|---|---|
| **WU-5A1** | `LearnerEvent` + prior belief + confidences + done_ts → posterior belief + `MasteryEventRowSpec`/`LearnerStateRowSpec` (PURE, floored) | `learner_model/__init__.py`, `belief.py`, `state_model.py`, `update.py` (NEW) | No | **No** (pure, `math` only) | absorbing-zero floor; golden-vector γ=1.5; damper form; two-step 0.5 threshold; cold-start mastery 0.50; q-no-double-count |
| **WU-5A2** | `retired shadow result` + `sess` + `done_ts` + `parser_confidence` → `apollo_mastery_events` appended + `apollo_learner_state` upserted in ONE all-or-nothing txn (flag-gated) | `learner_model/persistence.py`, `handlers/learner_update.py`, `tests/database/test_done_layer3_route_postgres.py` (NEW); `handlers/done.py`, `knowledge_graph/store.py` (edit) | No | **Yes — real-PG** (no Neo4j, no LLM) | txn atomicity; key→id join + unmapped-skip; retry/supersede no-double-count (event-log base); done_ts threading; flag-off byte-identity; tripwire flips |

**The one-sentence split:** WU-5A1 is the interpretable filter's arithmetic (pure,
exhaustively golden-vector-tested, NO container, with the BKT boundary-floor that
kills the absorbing-zero) and WU-5A2 is the all-or-nothing Done-transaction write of
that arithmetic into Layer 3 (real-PG, flag-gated, tripwire-flipping, threading a
single handler-captured `done_ts` into both Neo4j and Postgres).

## OPEN DECISIONS FOR THE ORCHESTRATOR (genuine human/spec calls before building)

1. **[SPEC BUG] §3 line 427 vs the covered likelihood.** The spec's own "never 0"
   rule is violated by `L_covered(s)` at s∈{0,1}. The plan adopts the standard BKT
   boundary-floor (`LIKELIHOOD_FLOOR=0.02`) which HONORS line 427. **Confirm the
   floor (vs s-clamping into [δ,1−δ]) and that we are not silently re-parameterizing
   γ.** Recommended: the floor (uniform across all event kinds, minimal blast radius).
2. **[SPEC BUG] cold-start mastery: formula says 0.50, prose says 0.40.** The plan
   codes the FORMULA (0.50). **Confirm: correct the §3 prose to 0.40→0.50, OR if 0.40
   is the intended product target, respecify the mastery WEIGHTING (not the belief).**
   Recommended: correct the prose to 0.50 (the belief vector is the load-bearing
   product object; re-weighting mastery would ripple into the teacher-dashboard
   readouts).
3. **[SPEC CLARITY] misconception flag is two-step from cold-start (0.4839 then
   0.7475), not one-step.** The plan pins the two-step behavior. **Confirm the §6.5
   table / §1 persona-flag wording is updated so "sets misconception_code=c" reads as
   "once p_misc crosses 0.5 (typically the second misconception event)."**
4. **[SCOPE CONFIRM] `stamp_graded_at` signature widening crosses into WU-3C1's
   store.py** (owned by `docs/architecture/apollo.md`). It is a ~3-line back-compat
   change (new optional `ts` param). **Confirm 5A2 may edit `store.py`, or carve the
   widening into a tiny pre-step.** Recommended: allow it in 5A2 (it is the minimal
   change that makes Neo4j + Postgres agree on the instant).

---

## ORCHESTRATOR ADJUDICATION (2026-06-18) — all 4 open decisions RESOLVED; math independently re-verified

The orchestrator independently RE-COMPUTED the load-bearing vectors and CONFIRMS every numerical pin: cold-start `mastery_of(0.20,0.60,0.20)=0.50`; covered-likelihood golden table at γ=1.5 (s=0.5→shaky, s=0.25→**misc** by 0.6495 vs 0.6464); the floored absorbing-zero rescue (perfect covered → (0.0185,0.0556,0.9259), a later misconception → p_misc 0.1875); the misconception two-step (0.4839 unflagged → 0.7475 flagged). All reproduce exactly.

1. **LIKELIHOOD_FLOOR=0.02 — CONFIRMED.** Adopt the uniform BKT boundary-floor (clamp each likelihood component to ≥0.02 BEFORE the damper). It directly implements §3 line 427 ("never 0"), has minimal blast radius (only bites the covered boundary s∈{0,1}; the fixed missing/misc/corrected rows' min component 0.2 is already > floor → no-op), and is the standard battle-tested fix. Do NOT s-clamp; do NOT re-parameterize γ (γ=1.5 STAYS). The executor must NOT invent an alternative.
2. **Cold-start mastery = 0.50 — CONFIRMED (code the FORMULA, not the prose).** The belief vector + the mastery formula are the load-bearing objects; the §3 line 407 prose "0.40" is an arithmetic error. Pin 0.50 in a golden test; add a one-line correction note at §3 line 407 (0.40→0.50) in the WU-5A1 commit so the spec stops misleading downstream units.
3. **Misconception flag is TWO-STEP — CONFIRMED.** Mathematically determined AND pedagogically correct (one expression = a slip, not a held misconception; two = a pattern worth flagging). Pin BOTH steps; update the §6.5/§1 wording to "sets misconception_code=c once p_misc crosses 0.5 (typically the second misconception event)."
4. **WU-5A2 MAY edit store.py — CONFIRMED.** The ~3-line back-compat widening `stamp_graded_at(*, attempt_id, ts=None)` is permitted in 5A2 (store.py is apollo infrastructure owned by docs/architecture/apollo.md, which 5A2 reconciles in the same commit). No separate pre-step. The no-`ts` call stays back-compat.

**Prior-art:** the 3-state filter is HAND-ROLLED (~50 lines of arithmetic) with the BKT-style boundary floor — NO external library port (overkill; golden-vector tests + the floor are the right rigor). CONFIRMED.

**Build order CONFIRMED:** WU-5A1 (pure, off the WU-4C2 tip) FIRST to de-risk the now-pinned constants, then WU-5A2 (real-PG). Proceeding to build WU-5A1.
