# Plan: WU-5A1 — pure 3-state Bayesian filter core

**Goal:** Implement the §3 / §6.5 interpretable 3-state belief filter as pure arithmetic in a NEW `apollo/learner_model/` package — cold-start prior, the floored §6.5 likelihood table, the §3.1 damper, Bayes+renormalize, mastery/confidence/misconception readouts, and the `event → BeliefUpdate + RowSpec` mapping — with NO IO, golden-vector-pinned.
**Architecture:** New pure library package `apollo/learner_model/` (mirrors `apollo/grading/event_model.py` + `events.py`); 3 source modules + 1 `__init__.py` + a `tests/` dir. No DB, no LLM, no Neo4j, no containers, no migration. Consumed by WU-5A2 (persistence + Done wiring), which is OUT OF SCOPE here.
**Tech stack:** Python 3.11+, stdlib `math` only, `@dataclass(frozen=True)` value objects, pytest (`.venv/Scripts/python.exe -m pytest`), `pytest.approx`/`math.isclose` (tol 1e-3/1e-4), diff-cover ≥95% vs `feat/apollo-kg-wu4c2-calibration-diagnostics`.

---
provides:
  - apollo/learner_model package (NEW)
  - belief.py constants: COLD_START_PRIOR, GAMMA, LIKELIHOOD_FLOOR, MISCONCEPTION_FLAG_THRESHOLD, MISSING/MISCONCEPTION/CORRECTED/NO_OP_LIKELIHOOD
  - belief.py fns: likelihood_for_event, damp, bayes_update, mastery_of, confidence_of, misconception_code_of
  - update.py: apply_event(...) -> BeliefUpdate, event_to_row_specs(...)
  - state_model.py: frozen BeliefUpdate, MasteryEventRowSpec, LearnerStateRowSpec
consumes:
  - apollo.grading.event_model.{LearnerEvent, LearnerEventKind} (frozen WU-4B2)
  - apollo.grading.events.AMBIGUOUS_ORDER_SCORE (imported, never re-literal'd)
  - stdlib math
depends_on:
  - WU-4B2 (event_model + events, already shipped/frozen)
  - branch base feat/apollo-kg-wu4c2-calibration-diagnostics (patch-coverage compare branch)
---

## Overview

WU-5A1 is the FIRST sub-unit of WU-5A (the Done-txn Layer-3 learner-model write). It owns
**every number** in the §3 belief filter and NOTHING else. It is a pure library: it takes
in-memory inputs (a frozen `LearnerEvent`, a prior belief tuple, two confidence scalars, a
prior anchor timestamp, a `done_ts`) and returns immutable value objects. It does NOT read or
write Postgres/Neo4j, does NOT call `convert_findings_to_events`, does NOT know `entity_id`,
transactions, flags, `sess`, or decay. The handoff to WU-5A2 is: 6 pure functions + named
constants + 3 frozen dataclasses.

All four math defects the WU-5A split proposal surfaced (and the orchestrator independently
re-verified, §ORCHESTRATOR ADJUDICATION) are PINNED here with golden-vector tests:

1. **Absorbing-zero floor** — `LIKELIHOOD_FLOOR=0.02` clamps each likelihood component to
   `>= 0.02` BEFORE the damper (the standard BKT boundary-floor that implements §3 line 427
   "never 0"). Without it `L_covered(1.0)=(0,0,1)` permanently annihilates `p_misc`.
2. **γ=1.5 golden vectors** — covered likelihood + argmax at s ∈ {0, 0.25, 0.5, 0.75, 1.0};
   `argmax==shaky` ONLY at s=0.5; s=0.25 leans **misc** (NOT shaky — intended below s≈0.4).
3. **Cold-start mastery = 0.50** (the FORMULA), NOT 0.40 (the §3 line 407 prose, which is an
   arithmetic error corrected in this commit).
4. **Misconception two-step** — first misconception from cold-start → p_misc=0.4839
   (argmax-but-UNFLAGGED, `misconception_code_of` returns None); second → p_misc=0.7475
   (FLAGGED, returns the code).

The constants and golden vectors below are **LOCKED** — pin them, do NOT re-derive, re-improve,
or re-parameterize. γ stays 1.5. The floor stays 0.02 (clamp-each, NOT s-clamp). Every expected
value in this plan was hand-computed and independently reproduced with the project interpreter
(see §Golden-vector / numeric pins).

## Prior art (sibling modules)

The package deliberately mirrors the shipped `apollo/grading/` pure-core pattern:

- **`apollo/grading/event_model.py:41-58`** — frozen `LearnerEvent`/`LearnerEventKind` shape +
  the `from __future__ import annotations` + `@dataclass(frozen=True)` convention this unit
  imports and mirrors. `event_model.py:30-38`: `LearnerEventKind` StrEnum values
  (covered/missing/partial/misconception/corrected) — `likelihood_for_event` dispatches on these.
- **`apollo/grading/events.py:1-65`** — the PURE module-header convention ("This unit is PURE:
  NO DB, NO LLM, NO Neo4j, NO containers"), `import math`, named module constants
  (`AMBIGUOUS_ORDER_SCORE=0.5` at line 53 — IMPORTED by this unit, never re-literal'd). The
  covered-score provenance: `events.py:302-329` (`_covered_event`/`_plain_covered_score`) shows
  `event.score` is ALREADY resolution-scaled by the converter and `event.confidence` is a
  SEPARATE field — the load-bearing fact behind the q-no-double-count pin.
- **`apollo/grading/persistence.py:50-91`** — frozen `RunRowSpec`/`FindingRowSpec` value objects
  (1:1 onto DB columns, NO id/created_at, immutable "builds NEW spec objects, never mutates
  inputs"). `FindingRowSpec.entity_id: int | None` (line 83) is the EXACT pattern
  `MasteryEventRowSpec.entity_id: int | None = None` mirrors.
- **`apollo/grading/__init__.py:83-130`** — the `__all__` re-export convention this unit's
  `apollo/learner_model/__init__.py` mirrors.
- **`apollo/grading/tests/test_event_model.py`** — the pure-test convention: "Pure imports — no
  DB, no LLM, no Neo4j, no network"; `dataclasses.FrozenInstanceError` immutability assertion
  (lines 28-38); a parity test against `models.MASTERY_EVENT_KINDS`. This unit's tests follow
  the same header + assertion style.
- **`apollo/grading/tests/test_persistence_specs.py:56-90`** — distinct-value spec-mapping tests
  ("Make every score distinct so a swapped mapping is caught") — the convention behind this
  unit's `event_to_row_specs` mapping test.

## Layered tasks (TDD-ORDERED)

The layered order is adapted for a PURE library (no HTTP/DB layers). Order: package marker →
value objects → math core (test-first) → orchestration (test-first) → package wiring → spec doc
fix. Every code module is written AFTER its RED test exists. No skip/xfail, no
asserting-nothing.

### 1. DB migration
- [ ] **None — justified.** WU-5A1 is pure arithmetic over in-memory value objects. The Layer-3
  tables (`apollo_mastery_events`, `apollo_learner_state`) were shipped by migration 026 and are
  written by WU-5A2, not this unit. Next free migration number is 028, UNUSED here. No SQL, no
  remote DB touch.
- Verify: `git status` shows NO file under `database/migrations/`.

### 2. Repository / data-access
- [ ] **None — justified.** No data-access layer: the unit reads/writes nothing. WU-5A2 owns the
  `SELECT … FOR UPDATE` + upsert + supersede. The frozen `*RowSpec` dataclasses (task 3) are the
  pre-DB mapping objects WU-5A2's repository half will consume; they carry NO session/engine.
- Verify: no SQLAlchemy import anywhere in `apollo/learner_model/` (`rg -n "sqlalchemy" apollo/learner_model` returns nothing).

### 3. Value objects — `apollo/learner_model/state_model.py` (NEW)
- [ ] **RED first:** `apollo/learner_model/tests/test_state_model.py` asserting each dataclass is
  frozen (`dataclasses.FrozenInstanceError` on mutate), the `MasteryEventRowSpec.entity_id`
  default is `None`, and the field sets match the spec column lists below. Run → fails (module
  absent).
- [ ] **GREEN:** create `state_model.py` with three `@dataclass(frozen=True)` value objects.
  - `BeliefUpdate` fields: `prior_belief: tuple[float, float, float]`,
    `posterior_belief: tuple[float, float, float]`, `mastery_after: float`,
    `confidence_after: float`, `misconception_code: str | None`, `parser_confidence: float`,
    `grader_confidence: float`, `dt_days_since_last: float | None`.
  - `MasteryEventRowSpec` (1:1 onto `apollo_mastery_events` non-id columns,
    `models.py:427-476`): `user_id: str | None = None`, `search_space_id: int | None = None`,
    `entity_id: int | None = None` (REQUIRED non-None before write — mirrors `FindingRowSpec`),
    `attempt_id: int | None = None`, `event_kind: str = ""`, `score: float | None = None`,
    `misconception_code: str | None = None`, `parser_confidence: float | None = None`,
    `grader_confidence: float | None = None`, `negotiation_move: str | None = None` (NULL v1),
    `reference_step_id: str | None = None`, `prior_belief: tuple[float, float, float] = (...)` ,
    `posterior_belief: tuple[float, float, float] = (...)`, `mastery_after: float = 0.0`,
    `dt_days_since_last: float | None = None`, `evidence_node_ids: tuple[str, ...] = ()`.
    NOTE: the identity columns default `None` because WU-5A1 does not resolve `entity_id`/ids;
    WU-5A2 fills them. The belief/mastery/event-payload columns ARE filled by WU-5A1.
  - `LearnerStateRowSpec` (1:1 onto `apollo_learner_state` non-pk-context columns,
    `models.py:397-424`): `belief: tuple[float, float, float]`, `mastery: float`,
    `confidence: float`, `misconception_code: str | None`, `evidence_count: int = 1`,
    `last_evidence_at` is NOT carried here (WU-5A2 sets it to `done_ts`) — document this in the
    docstring so the executor does not invent a field. (Alternatively carry it as `Any | None`;
    LOCKED choice: OMIT — `last_evidence_at` is a persist-time concern, WU-5A2 sets it.)
- Verify: `.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_state_model.py -q`
  green; `.venv/Scripts/python.exe -c "import apollo.learner_model.state_model"` imports clean.

### 4. Math core — `apollo/learner_model/belief.py` (NEW) — TEST FIRST
- [ ] **RED first:** `apollo/learner_model/tests/test_belief.py` with the FULL golden-vector
  table (covered L by s + argmax), the cold-start readouts, the damper invariants, the
  bayes invariants, the absorbing-zero rescue, the misconception two-step, and partial==covered.
  Run → fails (module absent). Every expected value is the HAND-COMPUTED constant from §Golden
  pins — never asserted against the function's own output.
- [ ] **GREEN:** create `belief.py`. Module header mirrors `events.py:1-34` (PURE, `import math`).
  - Named constants (see §Public signatures): `COLD_START_PRIOR=(0.20, 0.60, 0.20)`,
    `GAMMA=1.5`, `LIKELIHOOD_FLOOR=0.02`, `MISCONCEPTION_FLAG_THRESHOLD=0.5`,
    `MISSING_LIKELIHOOD=(0.7, 1.0, 0.4)`, `MISCONCEPTION_LIKELIHOOD=(3.0, 1.0, 0.2)`,
    `CORRECTED_LIKELIHOOD=(0.5, 1.5, 1.2)`, `NO_OP_LIKELIHOOD=(1.0, 1.0, 1.0)`.
  - `_clamp_each(L) -> tuple` private helper: `max(x, LIKELIHOOD_FLOOR)` per component.
  - `likelihood_for_event(event)`: dispatch on `event.event_kind`:
    - COVERED → `_clamp_each(((1 - s) ** GAMMA, 1 - abs(2 * s - 1) ** GAMMA, s ** GAMMA))`,
      `s = event.score` (event.score is the resolution-scaled coverage — never re-scale).
    - PARTIAL → covered at `s = AMBIGUOUS_ORDER_SCORE` (IMPORT from `apollo.grading.events`).
      Implementation: call the covered branch with `s=AMBIGUOUS_ORDER_SCORE` (NOT `event.score`,
      since the converter already set partial's score to 0.5 — but use the imported constant for
      provenance/no-double-literal; assert in test that `partial@0.5 == covered@0.5`).
    - MISSING → `_clamp_each(MISSING_LIKELIHOOD)`; MISCONCEPTION → `_clamp_each(MISCONCEPTION_LIKELIHOOD)`;
      CORRECTED → `_clamp_each(CORRECTED_LIKELIHOOD)` (the clamp is a uniform no-op on the fixed
      rows — min component 0.2 > floor — applied for invariance).
  - `damp(L, q) -> tuple`: `tuple(q * l + (1 - q) * one for l, one in zip(L, NO_OP_LIKELIHOOD))`
    (linear interpolation; q=0 → NO_OP no-op; q=1 → identity).
  - `bayes_update(prior, L) -> tuple`: `products = (p*l ...)`; `z = sum(products)`; if `z == 0`
    return `prior` unchanged (zero-sum guard); else `tuple(x / z for x in products)`.
  - `mastery_of(belief) -> float`: `0.5 * belief[1] + belief[2]`.
  - `confidence_of(belief) -> float`: `1 - (-sum(p*log(p) for p in belief if p > 0)) / log(3)`
    (guard `0*log0 == 0` via the `if p > 0` filter).
  - `misconception_code_of(belief, event) -> str | None`: return `event.misconception_code` IFF
    `belief[0]` is the argmax AND `belief[0] >= MISCONCEPTION_FLAG_THRESHOLD` AND
    `event.misconception_code is not None`; else `None`. Argmax tie-break: p_misc must be strictly
    the max OR `>=` all others — LOCKED: use `belief[0] == max(belief)` (so a tie at the max still
    counts p_misc as argmax; harmless given the threshold gate).
- Verify: `.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_belief.py -q` green.

### 5. Orchestration — `apollo/learner_model/update.py` (NEW) — TEST FIRST
- [ ] **RED first:** `apollo/learner_model/tests/test_update.py` with: the q-no-double-count
  posterior pin, the Δt-days computation (fixed anchors, None-prior → None), cold-start path
  (None prior_belief → COLD_START_PRIOR), the full `BeliefUpdate` field population, and
  `event_to_row_specs` distinct-field mapping. Run → fails (module absent).
- [ ] **GREEN:** create `update.py`. Imports `belief` functions/constants, `state_model` dataclasses,
  `LearnerEvent` (type only). Stdlib `math` for nothing here (Δt uses `datetime.timedelta.days`).
  - `apply_event(event, *, prior_belief, prior_last_evidence_at, parser_confidence, grader_confidence, done_ts) -> BeliefUpdate`:
    1. `prior = prior_belief if prior_belief is not None else COLD_START_PRIOR`.
    2. `q = parser_confidence * grader_confidence` (the ONLY place q is formed — do NOT
       re-multiply `event.confidence`).
    3. `L = likelihood_for_event(event)`; `Ld = damp(L, q)`; `posterior = bayes_update(prior, Ld)`.
    4. `mastery_after = mastery_of(posterior)`; `confidence_after = confidence_of(posterior)`.
    5. `misconception_code = misconception_code_of(posterior, event)`.
    6. `dt = (done_ts - prior_last_evidence_at).days if prior_last_evidence_at is not None else None`.
    7. return a NEW `BeliefUpdate(...)` (immutable; never mutate inputs).
  - `event_to_row_specs(event, update, *, user_id=None, search_space_id=None, entity_id=None, attempt_id=None) -> tuple[MasteryEventRowSpec, LearnerStateRowSpec]`:
    pure mapping of `event` + `update` onto the two row specs (belief/mastery/confidence/misc_code
    from `update`; event_kind/score/misconception_code/reference_step_id/evidence_node_ids from
    `event`; parser/grader from `update`; identity kwargs passed through, default None for 5A1).
    NOTE: this is the WU-5A2 hand-off seam; 5A1 ships it pure + tested but WU-5A2 supplies the
    resolved identity ids.
- Verify: `.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_update.py -q` green.

### 6. Package wiring — `apollo/learner_model/__init__.py` + `tests/__init__.py` (NEW)
- [ ] `apollo/learner_model/__init__.py` — package marker with an `__all__` re-export of the
  frozen public API (constants + 6 belief fns + `apply_event`/`event_to_row_specs` +
  `BeliefUpdate`/`MasteryEventRowSpec`/`LearnerStateRowSpec`), mirroring `grading/__init__.py`.
- [ ] `apollo/learner_model/tests/__init__.py` — empty package marker (mirrors
  `apollo/grading/tests/__init__.py`).
- [ ] **Package-seam test** `apollo/learner_model/tests/test_package_seam.py`: import the FROZEN
  WU-5A2 surface FROM the package root (`from apollo.learner_model import COLD_START_PRIOR,
  LIKELIHOOD_FLOOR, MISCONCEPTION_FLAG_THRESHOLD, likelihood_for_event, damp, bayes_update,
  mastery_of, confidence_of, misconception_code_of, apply_event, event_to_row_specs,
  BeliefUpdate, MasteryEventRowSpec, LearnerStateRowSpec`) and assert each is callable/defined —
  this is the contract WU-5A2 depends on (mirrors `grading/tests/test_package_seam.py`).
- Verify: `.venv/Scripts/python.exe -m pytest apollo/learner_model -q` all green.

### 7. Spec-doc one-line correction — `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md`
- [ ] §3 line 407: change the prose `(mastery 0.40 — ...)` to `(mastery 0.50 — ...)` and add an
  inline note: `the mastery FORMULA 0.5·p_shaky + p_mastered is authoritative (= 0.50 here); the
  prior 0.40 prose was an arithmetic error (orchestrator-confirmed 2026-06-18)`. Touch ONLY that
  line's prose — do NOT alter the belief vector `[0.20, 0.60, 0.20]` or any formula.
- Verify: `git diff` on the spec shows a single-line prose change.

### Final acceptance gate (run from repo root `C:/Users/ultra/OneDrive/TA-test/ai-ta-backend`)
- [ ] `.venv/Scripts/python.exe -m pytest apollo -q --tb=short` — all apollo tests green. (The
  `apollo/tests/test_api_auth.py` "Windows fatal exception: access violation" faulthandler dumps
  are PRE-EXISTING non-fatal aiosqlite/TestClient noise — process survives to 100%; NOT a failure.)
- [ ] `.venv/Scripts/python.exe -m pytest --cov=apollo --cov-report=xml -q` — writes coverage.xml.
- [ ] `diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu4c2-calibration-diagnostics --fail-under=95`
  — patch coverage ≥ 95% on changed lines. (NEVER bare `python` — that is anaconda base; a
  ModuleNotFoundError there is an interpreter-selection error, switch to `.venv`, NOT a blocker.)
- Do NOT install packages. No migration. The REAL-INFRA gate must NOT trigger (no changed files
  under `tests/database/`, `database/migrations/`, `apollo/knowledge_graph/`, `neo4j_client.py`).

## Public signatures (FROZEN API for WU-5A2)

```python
# apollo/learner_model/belief.py
COLD_START_PRIOR: tuple[float, float, float] = (0.20, 0.60, 0.20)   # (p_misc, p_shaky, p_mastered)
GAMMA: float = 1.5
LIKELIHOOD_FLOOR: float = 0.02
MISCONCEPTION_FLAG_THRESHOLD: float = 0.5
MISSING_LIKELIHOOD: tuple[float, float, float] = (0.7, 1.0, 0.4)
MISCONCEPTION_LIKELIHOOD: tuple[float, float, float] = (3.0, 1.0, 0.2)
CORRECTED_LIKELIHOOD: tuple[float, float, float] = (0.5, 1.5, 1.2)
NO_OP_LIKELIHOOD: tuple[float, float, float] = (1.0, 1.0, 1.0)

def likelihood_for_event(event: LearnerEvent) -> tuple[float, float, float]: ...
def damp(L: tuple[float, float, float], q: float) -> tuple[float, float, float]: ...
def bayes_update(prior: tuple[float, float, float], L: tuple[float, float, float]) -> tuple[float, float, float]: ...
def mastery_of(belief: tuple[float, float, float]) -> float: ...
def confidence_of(belief: tuple[float, float, float]) -> float: ...
def misconception_code_of(belief: tuple[float, float, float], event: LearnerEvent) -> str | None: ...

# apollo/learner_model/state_model.py
@dataclass(frozen=True)
class BeliefUpdate: ...           # prior/posterior belief, mastery_after, confidence_after,
                                  # misconception_code, parser_confidence, grader_confidence, dt_days_since_last
@dataclass(frozen=True)
class MasteryEventRowSpec: ...    # 1:1 apollo_mastery_events non-id cols; entity_id: int | None = None
@dataclass(frozen=True)
class LearnerStateRowSpec: ...    # belief, mastery, confidence, misconception_code, evidence_count

# apollo/learner_model/update.py
def apply_event(event: LearnerEvent, *, prior_belief: tuple[float, float, float] | None,
                prior_last_evidence_at: datetime | None, parser_confidence: float,
                grader_confidence: float, done_ts: datetime) -> BeliefUpdate: ...
def event_to_row_specs(event: LearnerEvent, update: BeliefUpdate, *, user_id: str | None = None,
                       search_space_id: int | None = None, entity_id: int | None = None,
                       attempt_id: int | None = None) -> tuple[MasteryEventRowSpec, LearnerStateRowSpec]: ...
```

Backward-compat: this is a NEW package — nothing imports it yet, so there is no existing
signature to preserve. The FROZEN guarantee is FORWARD: WU-5A2 will import exactly these names
from `apollo.learner_model`; the package-seam test (task 6) locks them.

## Golden-vector / numeric pins (LOCKED — hand-computed)

All values below were reproduced with `.venv/Scripts/python.exe` against the exact formulas in
§4 (verified 2026-06-18, identical to the orchestrator's independent re-computation). Assert with
`math.isclose(a, b, abs_tol=1e-3)` or `pytest.approx(b, abs=1e-3)` unless a tighter tol is noted.
NO tautologies — every expected number here is the independent target, never the function's own
output.

**Covered likelihood table (floored), argmax (s, L_misc, L_shaky, L_mastered):**

| s    | L (floored, 3-dp)              | argmax    |
|------|--------------------------------|-----------|
| 0.00 | (1.0000, 0.0200, 0.0200)       | misc      |
| 0.25 | (0.6495, 0.6464, 0.1250)       | **misc**  (NOT shaky; intended below s≈0.4) |
| 0.50 | (0.3536, 1.0000, 0.3536)       | shaky     |
| 0.75 | (0.1250, 0.6464, 0.6495)       | mastered  |
| 1.00 | (0.0200, 0.0200, 1.0000)       | mastered  |

- `argmax == shaky` ONLY at s=0.5. At s=0.25, L_misc=0.6495 > L_shaky=0.6464 (assert the
  inequality explicitly so a re-parameterized γ is caught).

**Cold-start readouts:**
- `mastery_of((0.20, 0.60, 0.20)) == 0.50` (exact; assert `== 0.5`, tol 1e-9 ok).
- `confidence_of((0.20, 0.60, 0.20)) ≈ 0.135026` (assert abs_tol 1e-4 against 0.135026).

**Absorbing-zero rescue (the FLOOR is load-bearing):**
- Perfect covered (s=1.0, q=1.0) from `COLD_START_PRIOR`:
  `posterior ≈ (0.0185, 0.0556, 0.9259)` — assert `posterior[0] > 0` AND the 3-dp vector.
- A SUBSEQUENT misconception event (q=1.0) from that posterior:
  `posterior[0] ≈ 0.1875` — assert it MOVED UP from 0.0185 (without the floor both p_misc paths
  would be 0.0 forever; this proves the floored path).

**Misconception two-step (replaces "≥0.5 in one step"):**
- First misconception from `COLD_START_PRIOR` (q=1.0): `p_misc ≈ 0.4839`, argmax==misc,
  `misconception_code_of(posterior, event_with_code) is None` (argmax but < 0.5 threshold).
- Second misconception (q=1.0) from that posterior: `p_misc ≈ 0.7475`,
  `misconception_code_of(posterior, event_with_code) == "<the code>"`.

**q-no-double-count (the LOAD-BEARING resolution-confidence pin):**
- Construct a COVERED event: `score=0.8`, `confidence=0.5` (resolution confidence — a red
  herring that must NOT enter q). Call `apply_event(event, prior_belief=COLD_START_PRIOR,
  prior_last_evidence_at=None, parser_confidence=0.9, grader_confidence=0.95, done_ts=<fixed>)`.
- `q = 0.9 * 0.95 = 0.855` (event.confidence=0.5 is NOT multiplied in).
- Expected `posterior ≈ (0.079491, 0.648885, 0.271624)` — assert against the hand-computed
  `normalize(COLD_START_PRIOR ⊙ damp(likelihood_for_event(covered@0.8), 0.855))` (abs_tol 1e-4).
  The test must hand-compute this independently (recompute the formula inline), NOT call
  `apply_event` twice.

**Damper invariants:**
- `damp(L, 0.0) == NO_OP_LIKELIHOOD == (1.0, 1.0, 1.0)` for any L (strict no-op).
- `damp(L, 1.0) == L` (identity) — assert per-component equal.
- Monotone in q on a non-trivial component (e.g. `damp(L, 0.3)[0]` between `1.0` and `L[0]`).
- Output never 0 (since L is floored and NO_OP is 1.0).

**Bayes invariants:**
- `sum(bayes_update(prior, L)) ≈ 1.0` (abs_tol 1e-9) for a representative prior+L.
- Zero-sum guard: `bayes_update(prior, (0.0, 0.0, 0.0)) == prior` (returns the prior unchanged).

**partial == covered at s=0.5:**
- `likelihood_for_event(partial_event) == likelihood_for_event(covered_event_at_0.5)` →
  `(0.3536, 1.0000, 0.3536)`, argmax==shaky. (`partial_event` built with
  `event_kind=PARTIAL, score=AMBIGUOUS_ORDER_SCORE`.)

**Δt-days:**
- Fixed `prior_last_evidence_at = datetime(2026, 6, 1, tzinfo=UTC)`,
  `done_ts = datetime(2026, 6, 8, tzinfo=UTC)` → `dt_days_since_last == 7`.
- `prior_last_evidence_at = None` → `dt_days_since_last is None`.
- No decay multiply is applied (assert the posterior is unaffected by dt — decay is WU-5B).

## Full test list (each test = name + assertion + how deps are mocked)

No external deps to mock — the entire unit is pure (`LearnerEvent` is a frozen in-memory
dataclass; no LLM, no DB, no network, no Neo4j). "Mocking" is N/A; every test constructs
`LearnerEvent` directly and asserts hand-computed numbers. Use `from datetime import UTC,
datetime` for the fixed timestamps. Import `LearnerEvent`/`LearnerEventKind` from
`apollo.grading.event_model` and `AMBIGUOUS_ORDER_SCORE` from `apollo.grading.events`.

**`tests/test_state_model.py`**
- `test_belief_update_is_frozen` — construct a `BeliefUpdate`, assert mutation raises
  `dataclasses.FrozenInstanceError`; assert all 8 fields readable.
- `test_mastery_event_row_spec_entity_id_defaults_none` — `MasteryEventRowSpec()` (or minimal)
  has `entity_id is None` (mirrors `FindingRowSpec` pattern); assert frozen.
- `test_learner_state_row_spec_fields` — assert the field set is exactly {belief, mastery,
  confidence, misconception_code, evidence_count}; assert `evidence_count` default == 1; frozen.

**`tests/test_belief.py`** (the golden-vector heart)
- `test_covered_likelihood_golden_vectors` — for each s in {0, 0.25, 0.5, 0.75, 1.0} build a
  COVERED event, assert `likelihood_for_event` == the 3-dp vector from §pins (abs_tol 1e-3).
- `test_covered_argmax_shaky_only_at_half` — assert argmax index == 1 (shaky) ONLY at s=0.5; at
  0.25 argmax==0 (misc) and explicitly `L[0] > L[1]`; at 0.75/1.0 argmax==2; at 0.0 argmax==0.
- `test_likelihood_floor_applied_at_boundaries` — at s=0.0 assert `L[1]==L[2]==LIKELIHOOD_FLOOR`;
  at s=1.0 assert `L[0]==L[1]==LIKELIHOOD_FLOOR`; assert no component < LIKELIHOOD_FLOOR.
- `test_missing_misconception_corrected_likelihoods` — assert the three fixed vectors equal the
  named constants after the (no-op) clamp.
- `test_partial_maps_to_covered_at_half` — `likelihood_for_event(partial@AMBIGUOUS_ORDER_SCORE)
  == likelihood_for_event(covered@0.5) == (0.3536, 1.0, 0.3536)`, argmax==shaky.
- `test_damp_zero_is_noop` — `damp(L, 0.0) == NO_OP_LIKELIHOOD` for a non-trivial L.
- `test_damp_one_is_identity` — `damp(L, 1.0) == L` component-wise.
- `test_damp_monotone_in_q` — for a component < 1.0, `damp(L, 0.3)[i]` lies strictly between
  `NO_OP` (1.0) and `L[i]`; output never 0.
- `test_bayes_update_sums_to_one` — `math.isclose(sum(bayes_update(prior, L)), 1.0, abs_tol=1e-9)`.
- `test_bayes_update_zero_sum_guard` — `bayes_update(prior, (0,0,0)) == prior`.
- `test_mastery_of_cold_start_is_half` — `mastery_of(COLD_START_PRIOR) == 0.5` (exact).
- `test_confidence_of_cold_start` — `confidence_of(COLD_START_PRIOR) ≈ 0.135026` (abs_tol 1e-4);
  also `confidence_of((1.0, 0.0, 0.0)) == 1.0` (zero entropy, the 0*log0 guard) and
  `confidence_of((1/3, 1/3, 1/3)) ≈ 0.0` (max entropy).
- `test_misconception_code_below_threshold_returns_none` — belief with `p_misc=0.4839` (argmax
  but < 0.5) + event-with-code → `misconception_code_of` returns None.
- `test_misconception_code_at_threshold_returns_code` — belief with `p_misc=0.7475` + event with
  `misconception_code="misc.density_ignored"` → returns that code; event WITHOUT a code → None.
- `test_misconception_code_requires_argmax` — belief `(0.5, 0.5, 0.0)`-ish where p_misc is NOT
  strictly max → returns None even if ≥ threshold (LOCKED tie rule: `p_misc == max(belief)`
  counts; pick a belief where p_misc < some other to assert None).

**`tests/test_update.py`** (orchestration + the rescue/two-step end-to-end)
- `test_apply_event_q_no_double_count` — covered event score=0.8, confidence=0.5,
  parser=0.9, grader=0.95 → posterior ≈ (0.079491, 0.648885, 0.271624) (abs_tol 1e-4),
  hand-computed inline as `normalize(prior ⊙ damp(L(0.8), 0.855))`; assert `q` did NOT use 0.5.
- `test_apply_event_cold_start_when_prior_none` — `prior_belief=None` → uses COLD_START_PRIOR;
  assert `update.prior_belief == COLD_START_PRIOR`.
- `test_apply_event_populates_belief_update` — assert all `BeliefUpdate` fields are set:
  `posterior_belief` sums to 1, `mastery_after == mastery_of(posterior)`,
  `confidence_after == confidence_of(posterior)`, `parser_confidence`/`grader_confidence`
  echoed, `dt_days_since_last` computed.
- `test_absorbing_zero_rescue_perfect_then_misconception` — apply_event(perfect covered s=1.0,
  parser=1.0, grader=1.0) from cold-start → posterior ≈ (0.0185, 0.0556, 0.9259), p_misc > 0;
  then apply_event(misconception, q=1.0) from that posterior → p_misc ≈ 0.1875 (MOVED UP).
- `test_misconception_two_step` — apply_event(misconception, q=1.0) from cold-start → p_misc ≈
  0.4839, `update.misconception_code is None`; second apply from that posterior → p_misc ≈
  0.7475, `update.misconception_code == "<code>"`.
- `test_dt_days_since_last_computed` — fixed anchors 2026-06-01 → 2026-06-08 → `dt == 7`.
- `test_dt_days_none_when_no_prior_anchor` — `prior_last_evidence_at=None` → `dt is None`.
- `test_no_decay_multiply_in_v1` — two apply_event calls identical except `done_ts` 30 days
  apart (same prior_last_evidence_at=None) → IDENTICAL posterior (decay is WU-5B, not applied).
- `test_event_to_row_specs_maps_distinct_fields` — build an event + update with DISTINCT field
  values, assert each lands on the correct `MasteryEventRowSpec`/`LearnerStateRowSpec` column
  (mirrors `test_persistence_specs.py:56-90` "swapped mapping is caught"); identity kwargs pass
  through; defaults None when omitted; both specs frozen.
- `test_apply_event_does_not_mutate_inputs` — pass a prior tuple, assert it is unchanged after
  the call (immutability contract).

**`tests/test_package_seam.py`**
- `test_public_api_importable_from_package_root` — import all 19 frozen names from
  `apollo.learner_model`, assert each is not None / callable as appropriate.
- `test_no_io_imports` — assert `apollo.learner_model.belief`/`update`/`state_model` modules do
  NOT import sqlalchemy/asyncpg/openai/neo4j (introspect `sys.modules` or read `__file__` source
  via `inspect.getsource` and assert those tokens absent) — pins the PURE contract.

**Coverage:** every branch (each event_kind in `likelihood_for_event`, the zero-sum guard, the
None-prior cold-start, the None-anchor Δt, each `misconception_code_of` gate, the confidence
0*log0 guard) is exercised above → exhaustible ≥95% patch coverage with no container.

## DI discovery findings

No dependency injection — this is a pure library, not a NestJS/service-graph module. There is no
provider to register, no injection token, no scope. The only imports are:
- `apollo.grading.event_model.{LearnerEvent, LearnerEventKind}` — frozen WU-4B2 value
  object/enum (`event_model.py:30-58`), imported for type dispatch only.
- `apollo.grading.events.AMBIGUOUS_ORDER_SCORE` — shipped constant `= 0.5` (`events.py:53`),
  imported so the partial→covered@0.5 mapping never re-literals `0.5`.
- stdlib `math` (entropy/log) and `datetime` (Δt).

NO other apollo imports. NO DB/LLM/Neo4j/SQLAlchemy. This is enforced by
`test_package_seam.test_no_io_imports`.

## Transaction scope decisions

**N/A — no writes.** WU-5A1 touches no table, opens no transaction. The all-or-nothing Done-txn
boundary (`SELECT … FOR UPDATE`, append events + upsert state in one commit, supersede-by-delete,
event-log recompute base) is entirely WU-5A2's concern (split proposal §4 decision #2/#5). WU-5A1
returns immutable value objects; WU-5A2 decides when/how they hit Postgres. No external service
call → no compensation strategy needed. No half-applied state is even representable here.

## Error contract decisions

**No new exception classes, no HTTP surface.** WU-5A1 is a pure math library reached only from
WU-5A2's persistence path (which itself runs post-grade-commit, never on a request thread that
returns the math error to a client). The unit raises NOTHING of its own:
- Degenerate inputs are handled by guards, not exceptions: the zero-sum Bayes guard returns the
  prior unchanged; the `confidence_of` 0*log0 case is filtered (`if p > 0`); a None prior_belief
  falls back to `COLD_START_PRIOR`; a None anchor yields a None Δt.
- The likelihood floor makes `bayes_update` numerically safe (no all-zero product on normal
  inputs), so the zero-sum guard is defensive only.
- This matches the apollo NO-FALLBACK convention boundary: the genuine failure modes
  (Neo4j/Postgres/LLM unavailability, `learner_update_pending` retry) all live in WU-5A2 /
  WU-4C1 named errors (`docs/architecture/apollo.md` §Non-obvious conventions). WU-5A1 adds NO
  error to that registry.

## Owner-doc updates

Owner doc: **`docs/architecture/apollo.md`** (owns `apollo/**`). Reconcile IN THE SAME COMMIT:

1. **Frontmatter `owns:`** — add `- apollo/learner_model/**` to the globs list (after
   `apollo/grading/**`).
2. **Frontmatter `last_verified:`** — set to `2026-06-16` (per the binding drift-contract
   instruction for this work unit).
3. **Module map table (§Module map and file landmarks)** — add a new row for
   `apollo/learner_model/` with key files `belief.py`, `state_model.py`, `update.py`,
   `__init__.py` and the role text: "WU-5A1 — the PURE 3-state Bayesian filter core (§3 / §6.5
   math), no IO. `belief.py`: cold-start prior `(0.20,0.60,0.20)`, the §6.5 likelihood table with
   the BKT boundary floor `LIKELIHOOD_FLOOR=0.02` (implements §3 line 427 'never 0' — kills the
   absorbing-zero on perfect-covered s∈{0,1}), the §3.1 linear damper `q·L+(1-q)·(1,1,1)`,
   Bayes+renormalize with a zero-sum guard, `mastery_of=0.5·p_shaky+p_mastered`,
   `confidence_of=1-entropy/log3`, and the two-step `misconception_code_of` (flags only once
   p_misc is argmax AND ≥0.5 — typically the 2nd misconception). γ=1.5 (s=0.5→shaky, s=0.25→misc).
   `state_model.py`: frozen `BeliefUpdate`/`MasteryEventRowSpec`/`LearnerStateRowSpec` (the
   `entity_id: int|None` is filled by WU-5A2, mirrors `FindingRowSpec`). `update.py`: pure
   `apply_event` (q=parser·grader; event.confidence is NOT re-multiplied — the COVERED score is
   already resolution-scaled by the converter; records `dt_days_since_last` but applies NO decay —
   decay is WU-5B) + `event_to_row_specs`. Consumes ONLY `apollo.grading.event_model` +
   `AMBIGUOUS_ORDER_SCORE`; no DB/LLM/Neo4j. Persistence + Done wiring are WU-5A2."
4. **A short §Non-obvious conventions bullet** noting: cold-start mastery is 0.50 (the formula),
   not the spec-prose 0.40 (corrected in the §3 commit); the LIKELIHOOD_FLOOR fix; and that the
   misconception flag is two-step.

NOTE the date tension: the task binding instruction says set `last_verified=2026-06-16` for this
work; the file currently reads `2026-06-18`. Setting it back to 2026-06-16 follows the explicit
binding constraint — FLAGGED as a risk for the orchestrator to confirm (it may prefer keeping the
later 2026-06-18). The executor sets `2026-06-16` unless the orchestrator overrides.

## Downstream consumers

- **WU-5A2** (next sub-unit, not built here): `apollo/learner_model/persistence.py` +
  `apollo/handlers/learner_update.py` import the FROZEN API (`apply_event`, `event_to_row_specs`,
  `COLD_START_PRIOR`, the `*RowSpec` dataclasses). The package-seam test locks the surface.
- **No frontend** consumes this — there is no HTTP endpoint, no URL pattern. (Grepped: the
  Layer-3 belief is a write-side concern; teacher-dashboard readouts of `apollo_learner_state`
  are a later read-side unit, split proposal §8.)
- **No other current importer** — grep confirms `apollo/learner_model/` does not yet exist, so no
  back-compat surface to preserve.

## Risks

- **[LOW] Float-equality brittleness.** Golden vectors are pinned to 3-dp. Mitigation: every
  assert uses `math.isclose`/`pytest.approx` with abs_tol 1e-3 (or 1e-4 for the q-no-double-count
  posterior). Exact `==` used ONLY for `mastery_of(cold)==0.5` (exact rational) and the floor
  boundary equalities. Confidence: HIGH the tols are correct (reproduced numerically).
- **[LOW] `confidence_of` cold-start value.** 0.135026 is the reproduced
  `1 - entropy([.2,.6,.2])/log3`. Pin abs_tol 1e-4. Confidence: HIGH (reproduced).
- **[MEDIUM] `last_verified` date.** Binding says 2026-06-16; file says 2026-06-18. Executor sets
  2026-06-16 per the constraint; orchestrator may override. Does not affect code/tests.
- **[LOW] `misconception_code_of` argmax tie rule.** LOCKED to `belief[0] == max(belief)` (a tie
  at the max counts p_misc as argmax). Harmless given the ≥0.5 threshold gate; pinned by
  `test_misconception_code_requires_argmax`. Confidence: HIGH.
- **[LOW] `LearnerStateRowSpec.last_evidence_at` omission.** Deliberately NOT a 5A1 field
  (WU-5A2 sets it to `done_ts` at persist time). Documented in the dataclass docstring + the
  test so the executor does not add it. Confidence: HIGH (split proposal §3 separates persist
  concerns).
- **[NONE] Real-infra gate.** No changed files under `tests/database/`, `database/migrations/`,
  `apollo/knowledge_graph/`, or `neo4j_client.py` → the REAL-INFRA verify gate must NOT trigger.
  If the executor reaches for a container/DB, the unit is mis-scoped — STOP.

## Out-of-scope boundaries (WU-5A2 / WU-5B)

Explicitly NOT in WU-5A1 (do not implement, do not test here):
- **Any DB/persistence** — `apollo_mastery_events` append, `apollo_learner_state` upsert,
  `SELECT … FOR UPDATE`, supersede-by-delete, event-log recompute base, transactions, `flush`/
  `commit`. → WU-5A2 (`persistence.py` + `handlers/learner_update.py`).
- **`convert_findings_to_events` invocation** — WU-5A1 consumes a `LearnerEvent`, never produces
  one from findings. → WU-5A2 (step 16 call).
- **`entity_id` resolution** (canonical_key → `apollo_kg_entities.id`), unmapped-skip,
  `done_ts` capture/threading, `parser_confidence` sourcing from `student_graph`,
  `grader_confidence = normalization_confidence × 1.0`, the `APOLLO_GRAPH_SIM_LAYER3_ENABLED`
  flag, `learner_update_pending` set-on-failure. → WU-5A2.
- **`stamp_graded_at` signature widening** (`store.py`). → WU-5A2.
- **The decay multiply** (`belief ← (1-w)·belief + w·prior`, w from Δt), the
  `learner_update_pending` janitor retry driver, the RQ5 chat-keyword hedge. → WU-5B.
- **Parameter fitting** (EM emission fit, grid-search decay k, Elo backfill). → offline, never v1.
- **Negotiation multiplier / `negotiation_move` sourcing** — column NULL, multiplier not applied.
  → follow-up.
- **Teacher-dashboard / longitudinal readouts** of mastery. → read-side unit, later.

## Deviations I'd allow the executor

- **Helper-function factoring inside `belief.py`** — e.g. a private `_covered_likelihood(s)` or
  `_argmax(t)` is fine if it keeps modules < 800 lines and tests green. The PUBLIC signatures and
  the NUMBERS are not negotiable.
- **Test file granularity** — splitting `test_belief.py` into `test_likelihood.py` +
  `test_readouts.py`, or merging the small ones, is fine as long as EVERY named assertion above
  exists and no assertion is dropped/weakened.
- **`MasteryEventRowSpec` field defaults** — making the identity columns positional-required vs
  keyword-defaulted-None is a judgment call; LOCKED default is `None` (so WU-5A1 can build a spec
  without ids). The executor may keep them keyword-only.
- **`event_to_row_specs` may be deferred to a follow-up commit within the same PR** if it risks
  the coverage gate, PROVIDED its test still ships and passes (it is the WU-5A2 seam; preferable
  to keep it in 5A1). Do NOT drop it silently.
- **NOT allowed:** changing γ, the floor value or clamp style (no s-clamp), the cold-start vector,
  the mastery/confidence formulas, any golden vector, the threshold 0.5, or re-multiplying
  `event.confidence` into q. Those are LOCKED.
