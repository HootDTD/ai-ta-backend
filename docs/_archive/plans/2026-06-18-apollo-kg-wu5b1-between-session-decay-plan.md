# WU-5B1 — between-session decay (§3 Step 0) — implementation plan (TDD-ordered)

**Date:** 2026-06-19
**Branch:** `feat/apollo-kg-wu5b1-between-session-decay` (already checked out — do NOT create/switch branches)
**Compare branch for diff-cover:** `feat/apollo-kg-wu5a2-layer3-persist`
**Flag:** `APOLLO_LEARNER_DECAY_ENABLED` (default OFF)
**Stacks on:** WU-5A2 (`feat/apollo-kg-wu5a2-layer3-persist`, PR #39 tip)
**Authoritative inputs:** spec `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §3 Step 0 (L412-418) + "never 0" (L430-431); proposal `docs/superpowers/plans/2026-06-18-apollo-kg-wu5b-split-proposal.md` §3 WU-5B1 + ORCHESTRATOR ADJUDICATION #1/#3/#5 (2026-06-18)

---

## 0. One-line goal

Decay the recompute-base prior toward `COLD_START_PRIOR` by `w = 1 − e^(−k·Δt_days)` (`k=0.05`) at UPDATE time, BEFORE the §3 Bayes step, with `Δt` anchored to the frozen `done_ts` (never `now()`), integer-day, clamped `Δt ≥ 0` — all behind `APOLLO_LEARNER_DECAY_ENABLED` (default OFF). The frozen WU-5A2 persistence is extended ONLY additively (identity-default kwarg + a pure dt-hoist).

## 1. Scope boundary (in / out)

**IN (this unit owns):**
- The decay arithmetic (a new pure module `apollo/learner_model/decay.py`).
- The ONE pure dt-hoist + ONE identity-default `prior_transform` kwarg threaded through the frozen `persistence.py` fold (adjudication #1).
- Reading `APOLLO_LEARNER_DECAY_ENABLED` in `apollo/handlers/learner_update.py` and, when ON, building the decay `prior_transform` closure and passing it into `persist_learner_update`; when OFF, passing `None`.
- Two test layers: pure golden-vector tests + ONE real-PG integration test.
- Reconciling the owner doc `docs/architecture/apollo.md`.

**OUT (explicitly NOT this unit — see §12):**
- Negotiation-move hedge (WU-5B2), retry janitor (WU-5B3), chat-keyword persist (WU-5B4).
- Read-time / "unseen entity drifts to unknown" readout (§3 L415) — a PHASE-6 reader concern. Update-time decay here fires ONLY for an entity that RECEIVES an event this Done.
- Any migration (the `dt_days_since_last` column already exists, populated by WU-5A2 — NO migration in this unit).
- Re-parameterizing the decay math (`DECAY_K`, the formula, the golden vectors are LOCKED).

## 2. Files to create / edit (exact)

| # | File | Action | What |
|---|---|---|---|
| 1 | `apollo/learner_model/decay.py` | **NEW** | Pure module: `DECAY_K`, `decay_weight`, `decay_toward_prior`. No IO. Imports `math` only. |
| 2 | `apollo/learner_model/tests/test_decay.py` | **NEW** | Pure golden-vector tests (hardcoded LOCKED constants). |
| 3 | `apollo/learner_model/persistence.py` | **EDIT (additive, adjudication #1)** | (a) hoist the `dt_days_since_last` computation to the TOP of `_combined_belief_update` (pure reordering); (b) add keyword-only `prior_transform` kwarg threaded `persist_learner_update` → `_persist_entity_group` → `_combined_belief_update`, applied to the base prior BEFORE damp/bayes. Default `None` = identity = byte-identical to WU-5A2. |
| 4 | `apollo/handlers/learner_update.py` | **EDIT (~8 lines)** | Add `_LEARNER_DECAY_FLAG` constant + `_learner_decay_enabled()` helper (mirror `done.py:_graph_sim_layer3_enabled` shape); build the decay `prior_transform` closure when ON; pass `prior_transform=...` (or `None`) into `persist_learner_update`. |
| 5 | `tests/database/test_done_layer3_route_postgres.py` | **EDIT (ADD 1 test)** | One `pytest.mark.integration` test: flag-ON + prior-attempt event-log base + a dt gap → persisted posterior reflects the DECAYED base (numerically distinct from no-decay). Reuse the existing harness. |
| 6 | `docs/architecture/apollo.md` | **EDIT** | Reconcile the `apollo/learner_model/` row + register `decay.py`, the `prior_transform` seam, the dt-hoist, the new flag; bump `last_verified` to **2026-06-19**. |

**READ-ONLY frozen (do NOT edit — import/keep green only):**
- `apollo/learner_model/belief.py` — import `COLD_START_PRIOR` (NEVER re-literal `(0.20,0.60,0.20)`).
- `apollo/learner_model/update.py`, `apollo/learner_model/state_model.py` — the dt-hoist must NOT touch these (the `apply_event` dt computation in `update.py:62-66` stays exactly where it is; it is a DIFFERENT function, not on this path).
- `apollo/handlers/done.py` — the `_graph_sim_layer3_enabled()` shape is the mirror template ONLY; do not edit done.py.
- `tests/database/test_done_shadow_route_postgres.py`, the 16 existing WU-5A2 tests in `test_done_layer3_route_postgres.py` — byte-identity guard, must stay green (me==0/ls==0 with flag OFF).

## 3. Public signatures (the frozen API for this unit)

### `apollo/learner_model/decay.py` (NEW, pure)

```python
"""WU-5B1 §3 Step 0 — between-session decay toward the cold-start prior.

PURE: NO DB, NO LLM, NO Neo4j, NO containers, NO network. Mirrors belief.py
(named module constants, `import math`, no IO). Imports COLD_START_PRIOR is NOT
done here — the caller passes the prior in (keeps this module dependency-free
and the convex blend explicit); the closure in learner_update.py binds
COLD_START_PRIOR from belief.py.

Step 0 (spec L412-418): belief <- (1-w)*belief + w*prior, w = 1 - e^(-k*dt),
k=0.05 (~14-day half-life). Pull target = COLD_START_PRIOR (every component
>= 0.20 > 0), so the blend REPAIRS zeros and honors the §3 'never 0' invariant.
Δt is INTEGER days, anchored to the frozen done_ts (never now()), clamped >= 0.
"""

from __future__ import annotations

import math

# Decay rate per day (spec L413; half-life = ln 2 / k = 13.8629 days). LOCKED.
DECAY_K: float = 0.05


def decay_weight(dt_days: float, k: float = DECAY_K) -> float:
    """The convex-blend weight w = 1 - e^(-k * max(0, dt_days)).

    The max(0, .) clamp kills negative-Δt sharpening (clock skew / out-of-order
    retries): a negative dt -> 0 -> w=0 -> identity. dt_days=0 -> w=0 -> identity
    (no spurious forgetting on a same-day re-attempt). LOCKED.
    """
    return 1.0 - math.exp(-k * max(0.0, dt_days))


def decay_toward_prior(
    belief: tuple[float, float, float],
    prior: tuple[float, float, float],
    dt_days: float,
    k: float = DECAY_K,
) -> tuple[float, float, float]:
    """Convex blend (1-w)*belief + w*prior, component-wise. Returns a NEW tuple.

    A convex combination of two sum-1 probability vectors is itself sum-1 (no
    renormalize needed). Because every `prior` component >= 0.20 > 0, the result
    is strictly > 0 for dt>0 (zero-repair). PURE — never mutates inputs.
    """
    w = decay_weight(dt_days, k)
    return tuple((1.0 - w) * b + w * p for b, p in zip(belief, prior, strict=True))
```

Notes:
- `decay_toward_prior` takes `prior` as a parameter (NOT hardcoded) so the module stays import-free; the binding to `COLD_START_PRIOR` happens in the closure (§5). This matches the proposal's "import `COLD_START_PRIOR` from `belief`" intent — the import lives at the closure site, never re-literal'd in `decay.py`.
- Return type is `tuple[float, float, float]` (a 3-tuple); `zip(..., strict=True)` enforces the 3-component shape — a length mismatch raises rather than silently truncating.

### `apollo/learner_model/persistence.py` (EDIT — additive)

The `prior_transform` callable threaded through:

```python
PriorTransform = Callable[[tuple[float, float, float], int | None], tuple[float, float, float]]
```

(`from collections.abc import Callable` — `Mapping` is already imported from `collections.abc`.) Added as a keyword-only parameter `prior_transform: PriorTransform | None = None` (default `None` = identity) on:
- `persist_learner_update(..., canon_key_by_canonical_key, prior_transform=None)`
- `_persist_entity_group(..., grader_confidence, prior_transform=None)`
- `_combined_belief_update(entity_events, *, prior_belief, prior_last_evidence_at, parser_confidence, grader_confidence, done_ts, prior_transform=None)`

The closure signature the executor passes is `(base_belief, dt_days) -> decayed_belief` where `dt_days` is the integer `dt_days_since_last` (or `None` → transform returns the prior unchanged).

### `apollo/handlers/learner_update.py` (EDIT)

```python
_LEARNER_DECAY_FLAG: str = "APOLLO_LEARNER_DECAY_ENABLED"


def _learner_decay_enabled() -> bool:
    return os.environ.get(_LEARNER_DECAY_FLAG, "").lower() in ("1", "true", "yes")
```

`run_learner_update` builds the closure when the flag is ON and threads it; signature of `run_learner_update` is UNCHANGED (the flag is read internally, mirroring how `done.py` reads its flags) — this keeps the WU-5A2 call site in `done.py:435-442` byte-identical.

## 4. The DECAY math (LOCKED — do NOT re-derive) + golden vectors

The executor does NOT re-derive or re-parameterize ANY of these. They are pinned as **hardcoded constants in the tests**, NOT asserted against the function's own output (mirrors the `test_update.py` "every expected number is the hand-computed golden constant" convention).

| Quantity | Value |
|---|---|
| `DECAY_K` | `0.05` per day |
| half-life | `ln 2 / 0.05 = 13.8629` days |
| `COLD_START_PRIOR` (pull target) | `(0.20, 0.60, 0.20)` — imported from `belief.py`, NEVER re-literal'd |
| formula | `w = 1 − e^(−k·max(0, dt))`; `decay = (1−w)·belief + w·prior` |

**LOCKED golden vectors (pin as hardcoded literals):**

| Case | Input | w | Expected output |
|---|---|---|---|
| Zero-repair | `decay((0,0,1), dt=3)` | `1−e^(−0.15) = 0.13929` | `(0.02786, 0.08358, 0.88857)` |
| Identity at dt=0 | `decay(b, dt=0)` | `w=0` | `b` (any `b`, identity) |
| Negative-dt clamp | `decay(b, dt=−2)` → `max(0,−2)=0` | `w=0` | `b` (identity) |
| Sums to 1 | any convex blend of two sum-1 vectors | — | output sums to `1.0` |
| Never-0 | every output component for `dt>0` | — | `≥ w·0.20 > 0` |

**Worked zero-repair (independently reproduced 2026-06-19 against frozen `belief.py`):**
- `w = 1 − e^(−0.05·3) = 1 − e^(−0.15) = 0.139292`
- comp0 = `(1−0.139292)·0 + 0.139292·0.20 = 0.0278584` → **0.02786**
- comp1 = `(1−0.139292)·0 + 0.139292·0.60 = 0.0835752` → **0.08358**
- comp2 = `(1−0.139292)·1 + 0.139292·0.20 = 0.860708 + 0.0278584 = 0.8885664` → **0.88857**
- sum = `0.02786 + 0.08358 + 0.88857 ≈ 1.00000` ✓ ; min component `0.02786 > 0` (a zero pulled OFF zero) ✓

**Test tolerance:** pure tests assert at `rel=1e-5, abs=1e-6` (`pytest.approx`) against the 5-dp golden constants for the zero-repair vector; exact equality for the identity cases (`w=0` makes `(1-0)*b + 0*p == b` exactly). The half-life is asserted as `math.log(2)/DECAY_K == pytest.approx(13.8629, abs=1e-4)`.

## 5. The PERMITTED frozen-file additive edit (adjudication #1)

Adjudication #1 (proposal, 2026-06-18) PERMITS this — it is NOT a violation of the "do not modify frozen" rule, because every change is identity-default (a no-arg / flag-off call is byte-identical) and the hoist is a pure reordering. **GUARDRAIL: if any WU-5A2 test output changes, the edit is NOT truly identity-default → STOP and ESCALATE (do not "fix" the WU-5A2 test).**

### 5a. The dt-hoist (`_combined_belief_update`) — a PURE reordering, reproduced byte-identical

Today (`persistence.py:337-356`), `_combined_belief_update` computes `dt_days_since_last` at the BOTTOM, AFTER `bayes_update`:

```python
    prior = prior_belief if prior_belief is not None else COLD_START_PRIOR   # :337
    q = parser_confidence * grader_confidence
    combined_likelihood = NO_OP_LIKELIHOOD
    for event in entity_events:
        ...
    damped = damp(combined_likelihood, q)            # :345
    posterior = bayes_update(prior, damped)          # :346
    misconception_code = None
    for event in entity_events:
        ...
    dt_days_since_last = (                            # :352-356  (computed LAST)
        (done_ts - prior_last_evidence_at).days
        if prior_last_evidence_at is not None
        else None
    )
    return BeliefUpdate(..., dt_days_since_last=dt_days_since_last)
```

**The hoist** moves the `dt_days_since_last = (...)` block to the TOP of the function body (immediately after the docstring, BEFORE the `prior = ...` line at `:337`). This is reproduced byte-identical because:
- `dt_days_since_last` is a function of two parameters (`done_ts`, `prior_last_evidence_at`) only.
- Neither parameter is mutated anywhere in the body (immutable `datetime`s).
- `dt_days_since_last` feeds ONLY `BeliefUpdate.dt_days_since_last` (a readout) — it does NOT feed `prior`, `q`, `combined_likelihood`, `damped`, `posterior`, or `misconception_code` in the WU-5A2 code. So moving it up cannot change any belief math.

After the hoist, the body reads:

```python
    dt_days_since_last = (
        (done_ts - prior_last_evidence_at).days
        if prior_last_evidence_at is not None
        else None
    )
    prior = prior_belief if prior_belief is not None else COLD_START_PRIOR
    if prior_transform is not None:
        prior = prior_transform(prior, dt_days_since_last)   # decay, BEFORE damp/bayes
    q = parser_confidence * grader_confidence
    combined_likelihood = NO_OP_LIKELIHOOD
    for event in entity_events:
        ...
    damped = damp(combined_likelihood, q)
    posterior = bayes_update(prior, damped)
    ...
    return BeliefUpdate(prior_belief=prior, posterior_belief=posterior, ...,
                        dt_days_since_last=dt_days_since_last)
```

**Critical correctness point:** `BeliefUpdate.prior_belief` is now the DECAYED prior when the transform fires (the posterior is `bayes_update(decayed_prior, damped)`). That is the intended §3 Step-0 → Step-3 chain (decay the prior, THEN Bayes). With `prior_transform=None` the line is skipped and `prior` is exactly the WU-5A2 value → byte-identical.

### 5b. Thread the kwarg (default `None`)

- `persist_learner_update`: add `prior_transform: PriorTransform | None = None` (keyword-only, after `canon_key_by_canonical_key`). Pass it through to `_persist_entity_group(..., prior_transform=prior_transform)` in the per-entity loop (`:293-302`).
- `_persist_entity_group`: add `prior_transform: PriorTransform | None = None`; pass it into `_combined_belief_update(..., prior_transform=prior_transform)` (`:406-413`).
- `_combined_belief_update`: add `prior_transform: PriorTransform | None = None`; apply as in §5a.

Add `from collections.abc import Callable` (or extend the existing `from collections.abc import Mapping` import to `from collections.abc import Callable, Mapping`). Define the `PriorTransform` type alias near the top.

### 5c. The handler closure (`learner_update.py`)

Add the flag constant + helper (§3). In `run_learner_update`, BEFORE the `persist_learner_update` call, build the transform when ON:

```python
    prior_transform = None
    if _learner_decay_enabled():
        def prior_transform(belief, dt_days):
            if dt_days is None:
                return belief
            return decay_toward_prior(belief, COLD_START_PRIOR, dt_days)
    result = await persist_learner_update(
        db, sess=sess, attempt=attempt, shadow=shadow, done_ts=done_ts,
        parser_confidence=parser_confidence,
        canon_key_by_canonical_key=canon_key_by_canonical_key,
        prior_transform=prior_transform,
    )
```

New imports in `learner_update.py`: `import os`; `from apollo.learner_model.belief import COLD_START_PRIOR`; `from apollo.learner_model.decay import decay_toward_prior`.

**Closure-side `dt_days is None` guard:** the closure returns the belief unchanged when `dt_days` is `None` (cold-start / no prior anchor) — defensive symmetry with the `decay_weight` clamp. (With the persistence-side default-`None` path, `prior_transform` is simply never called; with the flag ON and a `None` dt, the closure returns identity.) Both paths are tested.

**Note on `done_ts` anchor:** the proposal text says "dt anchored to the frozen `done_ts`, integer `.days`". This is ALREADY how `dt_days_since_last` is computed inside `_combined_belief_update` (`(done_ts − prior_last_evidence_at).days`, integer). The closure consumes that already-correct integer dt — it does NOT recompute dt from `now()`. No `now()` anywhere on this path.

## 6. TDD step order (RED → GREEN, real tests first)

Real tests FIRST. No skip-marks, no xfail, no assert-nothing. Interpreter: `.venv/Scripts/python.exe` (bare `python` is anaconda base with no testcontainers).

1. **RED — pure golden-vector tests.** Write `apollo/learner_model/tests/test_decay.py` (§7 Layer 1) referencing `from apollo.learner_model.decay import DECAY_K, decay_weight, decay_toward_prior`. Run: `ImportError` / module-not-found = RED.
2. **GREEN — `decay.py`.** Write the pure module (§3). Run `.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_decay.py -v` → all green. This module is fully covered by the pure tests (no DB needed).
3. **RED — the dt-hoist parity is proven by the EXISTING WU-5A2 suite.** Before touching `persistence.py`, run the full WU-5A2 suite and capture the baseline: `.venv/Scripts/python.exe -m pytest tests/database/test_done_layer3_route_postgres.py tests/database/test_done_shadow_route_postgres.py apollo/learner_model/tests -v`. Record GREEN counts (16 PG tests + the shadow byte-identity me==0/ls==0).
4. **GREEN — apply the additive `persistence.py` edit** (§5a + §5b): hoist dt, add the `prior_transform` kwarg + the `if prior_transform is not None` line. Re-run the SAME baseline suite from step 3 → MUST be byte-identical green (no output change anywhere). If ANY WU-5A2 test changes output → STOP and ESCALATE (the edit is not identity-default).
5. **RED — the decay integration test.** Add the new test in `tests/database/test_done_layer3_route_postgres.py` (§7 Layer 2, `test_decay_flag_on_decays_base_prior`). With the flag wiring not yet in `learner_update.py`, the test (flag ON, expecting a decayed posterior) FAILS because the no-op path still produces the no-decay posterior = RED.
6. **GREEN — wire the flag + closure in `learner_update.py`** (§5c). Re-run the new integration test → GREEN. Re-run the FULL WU-5A2 PG suite once more → still byte-identical green (the new flag defaults OFF, so every existing test is unaffected).
7. **Reconcile `docs/architecture/apollo.md`** (§9) in the SAME commit; bump `last_verified` to `2026-06-19`.
8. **Coverage gate** (§10): `diff-cover` vs `feat/apollo-kg-wu5a2-layer3-persist` ≥ 95% (target 100%).

## 7. Full test list (Layer 1 pure + Layer 2 real-PG)

### Layer 1 — pure golden-vector tests: `apollo/learner_model/tests/test_decay.py`

No DB, no LLM, no Neo4j, no network — pure imports only (mirrors `test_update.py` / `test_belief.py`). Every expected number is a **hardcoded golden constant**, NEVER the function's own output. Module-level constants pinned at the top:

```python
_ZERO_REPAIR_DT3 = (0.02786, 0.08358, 0.88857)   # decay((0,0,1), dt=3)
_W_DT3 = 0.139292                                  # 1 - e^(-0.15)
_HALF_LIFE = 13.8629                               # ln 2 / 0.05
```

| # | Test name | Asserts | Notes |
|---|---|---|---|
| L1-1 | `test_decay_weight_dt3_golden` | `decay_weight(3) == pytest.approx(_W_DT3, abs=1e-6)` | the LOCKED weight at dt=3; hardcoded, not recomputed. |
| L1-2 | `test_decay_weight_dt0_is_zero` | `decay_weight(0) == 0.0` | exact; identity weight. |
| L1-3 | `test_decay_weight_negative_dt_clamped_to_zero` | `decay_weight(-2) == 0.0` and `decay_weight(-100) == 0.0` | the `max(0,·)` clamp — negative dt → 0 → w=0 (kills clock-skew sharpening). |
| L1-4 | `test_decay_weight_monotone_increasing` | `decay_weight(1) < decay_weight(7) < decay_weight(30)` and all in `(0,1)` | bounded convex-blend weight. |
| L1-5 | `test_half_life_is_ln2_over_k` | `math.log(2)/DECAY_K == pytest.approx(_HALF_LIFE, abs=1e-4)` | the locked half-life; also assert `decay_weight(_HALF_LIFE) == pytest.approx(0.5, abs=1e-4)`. |
| L1-6 | `test_decay_toward_prior_zero_repair_dt3` | `decay_toward_prior((0.0,0.0,1.0), (0.20,0.60,0.20), 3) == pytest.approx(_ZERO_REPAIR_DT3, rel=1e-5, abs=1e-6)` | THE headline golden vector; a zero pulled OFF zero. Pass `COLD_START_PRIOR` imported from `belief.py` as `prior` and assert it equals the hardcoded tuple. |
| L1-7 | `test_decay_toward_prior_dt0_is_identity` | `decay_toward_prior(b, prior, 0) == b` (exact) for a non-trivial `b=(0.1,0.3,0.6)` | w=0 → `(1-0)*b + 0*prior == b`. |
| L1-8 | `test_decay_toward_prior_negative_dt_is_identity` | `decay_toward_prior(b, prior, -2) == b` (exact) | clamp → w=0 → identity. |
| L1-9 | `test_decay_toward_prior_sums_to_one` | for `b=(0.0,0.0,1.0)` and `dt∈{1,3,7,14,30}`, `sum(decay_toward_prior(b, COLD_START_PRIOR, dt)) == pytest.approx(1.0, abs=1e-9)` | convex blend of two sum-1 vectors stays sum-1. |
| L1-10 | `test_decay_toward_prior_never_zero` | for `b=(0.0,0.0,1.0)` and `dt∈{1,3,7,30}`, every component `> 0` (specifically `>= decay_weight(dt)*0.20 - 1e-12`) | the §3 'never 0' invariant — decay REPAIRS zeros, cannot create them. |
| L1-11 | `test_decay_toward_prior_returns_new_tuple_no_mutation` | the input `belief` tuple is unchanged after the call; the result `is not` the input object | immutability (tuples are immutable, but assert the contract explicitly + that a 3-tuple is returned). |
| L1-12 | `test_decay_toward_prior_full_decay_approaches_prior` | for a very large `dt` (e.g. 1000), `decay_toward_prior((0.0,0.0,1.0), COLD_START_PRIOR, 1000) == pytest.approx(COLD_START_PRIOR, abs=1e-3)` | w→1 → result → prior ("drifts to unknown"). |
| L1-13 | `test_decay_weight_custom_k_override` | `decay_weight(10, k=0.1) == pytest.approx(1 - math.exp(-1.0), abs=1e-9)` | the `k` override param path (covers the default-arg branch). |

This set drives every line/branch of `decay.py`: the `max(0,·)` clamp (L1-3/L1-8), the default-`k` and override-`k` paths (L1-1/L1-13), the convex blend (L1-6..L1-12), `zip(strict=True)` (implicitly via 3-tuples). → 100% coverage of `decay.py`.

### Layer 2 — ONE real-PG integration test (added to `tests/database/test_done_layer3_route_postgres.py`)

`pytest.mark.integration` (module already carries `pytestmark = pytest.mark.integration`). Reuses the existing harness: `db_session` fixture, `_seed_course_with_entity`, `_seed_session_attempt`, `_covered_finding`, `_audited`, `_shadow`, `_belief_close`, and `monkeypatch.setenv`. NO Neo4j, NO LLM (the `ShadowGradeResult` is hand-built; the entity map is seeded via ORM — same as the 16 existing tests).

| Test name | Asserts | How deps are mocked |
|---|---|---|
| `test_decay_flag_on_decays_base_prior` | With `APOLLO_LEARNER_DECAY_ENABLED=1`, a SECOND attempt whose recompute-base prior comes from a FIRST attempt's event-log posterior, plus a dt gap between the first attempt's `last_evidence_at` and the second `done_ts`, persists a posterior that reflects the **decayed** base — and is numerically DISTINCT from the no-decay posterior. | `monkeypatch.setenv("APOLLO_LEARNER_DECAY_ENABLED", "1")`. No Neo4j/LLM — hand-built `_shadow(_audited([...]))`. `run_learner_update` (which reads the flag internally) is the entry point. |

**Exact construction of `test_decay_flag_on_decays_base_prior` (deterministic, no live API):**

1. `sid, cid, by_key = await _seed_course_with_entity(db_session)`.
2. Seed session + **attempt1** (`problem_code="p_decay1"`). Run `run_learner_update` for attempt1 at `done_ts1 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)` with a `_covered_finding(score=1.0)` → this writes attempt1's event-log `posterior_belief` (the recompute base for attempt2) AND sets `LearnerState.last_evidence_at = done_ts1`.
3. Seed **attempt2** (`problem_code="p_decay2"`, distinct attempt id, same session/user/entity). Set `done_ts2 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)` → integer `dt_days_since_last = (done_ts2 − done_ts1).days = 7`.
4. **Compute the EXPECTED decayed posterior INLINE** (NOT by calling the production fold — re-implement the chain from the frozen primitives + `decay_toward_prior`, so the test is an independent oracle):
   - `base = <attempt1's persisted posterior_belief>` read back from the attempt1 `MasteryEvent` row (or recomputed inline via `bayes_update(COLD_START_PRIOR, damp(L_covered(1.0), q1))`).
   - `decayed_base = decay_toward_prior(base, COLD_START_PRIOR, 7)`.
   - `q2 = 0.9 * (0.8 * 1.0)`; `combined_l = likelihood_for_event(covered_event)`; `expected = bayes_update(decayed_base, damp(combined_l, q2))`.
5. Run `run_learner_update` for attempt2 (flag ON) at `done_ts2`. Read back `LearnerState.belief` for the entity → `assert _belief_close(state.belief, expected)`.
6. **Distinctness assertion (proves decay actually fired):** compute `no_decay_expected = bayes_update(base, damp(combined_l, q2))` (base WITHOUT decay) and `assert not _belief_close(state.belief, no_decay_expected)` — i.e. with a 7-day gap (w=0.295) the decayed and undecayed posteriors are numerically distinct. Also `assert state.belief == pytest.approx(list(expected), rel=1e-5, abs=1e-6)`.

This single test exercises: the flag-ON closure path, the dt-hoist providing the integer dt to the transform, the decay applied to the EVENT-LOG recompute base (not the state row), and the BEFORE-damp/bayes ordering. The flag-OFF byte-identity is already covered by the 16 existing WU-5A2 tests (which run with the flag absent → default OFF) — see §8.

**Why not a `test_decay_flag_off_*` duplicate:** the existing `test_layer3_flag_off_no_write` and all 15 other WU-5A2 tests run with `APOLLO_LEARNER_DECAY_ENABLED` unset (default OFF) and MUST stay byte-identical green — that IS the flag-off regression proof. Adding a redundant flag-off decay test would assert nothing new. (If diff-cover flags the `prior_transform is None` branch in `_combined_belief_update` as uncovered, it is covered by every existing WU-5A2 test that exercises the fold with the default `None`; the new line is `if prior_transform is not None:` whose False branch is the WU-5A2 path and whose True branch is the new integration test — both branches covered.)

## 8. Non-regression guardrail (WU-5A2 byte-identity with flag OFF)

This is the binding proof from adjudication #1 that the frozen-file edit is truly identity-default.

**The full WU-5A2 suite MUST stay byte-identical green with the decay flag OFF**, especially:
- All **16** tests in `tests/database/test_done_layer3_route_postgres.py`.
- The byte-identity guard `tests/database/test_done_shadow_route_postgres.py` (`me==0`/`ls==0` — no mastery events / learner state written when the Layer-3 path is dormant).
- The pure WU-5A1 suites `apollo/learner_model/tests/test_belief.py`, `test_update.py`, `test_state_model.py`, `test_package_seam.py`.

**Procedure (BINDING):**
1. Capture the baseline BEFORE editing `persistence.py` (TDD step 3).
2. After the additive edit (TDD step 4), re-run and diff: every test name, pass/fail, and every asserted posterior MUST be unchanged.
3. If ANY WU-5A2 test changes output (a different posterior, a different count, a new failure) → the dt-hoist or the kwarg is NOT identity-default → **STOP and ESCALATE to the orchestrator. Do NOT "fix" the WU-5A2 test** (that would mask a real behavior change).

**Why the hoist is safe (the escalation should never trigger):** `dt_days_since_last` feeds only `BeliefUpdate.dt_days_since_last` (a readout column), never the belief math; both its inputs are immutable and unmutated; with `prior_transform=None` the new `if` line is a no-op. The WU-5A2 tests already assert `dt_days_since_last` is recorded (via the event rows) — the hoist changes WHERE it is computed, not its VALUE.

## 9. Owner-doc reconciliation (docs/architecture/apollo.md)

`apollo.md` declares `owns: apollo/**` — it is the authority on `apollo/learner_model/`. Reconcile in the SAME commit (drift contract) and bump `last_verified: 2026-06-18` → `2026-06-19`.

**Edits to make:**
1. **Frontmatter:** `last_verified: 2026-06-18` → `last_verified: 2026-06-19`.
2. **The `apollo/learner_model/` row** (the table row currently listing `belief.py`, `state_model.py`, `update.py`, `persistence.py`, `__init__.py`): add `decay.py` to the file list, and append a **WU-5B1** paragraph describing:
   - `decay.py` (NEW, PURE): `DECAY_K=0.05` (half-life `ln2/k=13.86` d); `decay_weight(dt_days,k)=1−e^(−k·max(0,dt_days))` (the `max(0,·)` clamp kills negative-Δt sharpening); `decay_toward_prior(belief,prior,dt_days,k)` = the convex blend `(1−w)·belief+w·prior` toward `COLD_START_PRIOR` (every component ≥0.20>0 → REPAIRS zeros, honors the §3 'never 0' invariant; sums to 1 with no renormalize). PURE, returns a NEW tuple.
   - **The `prior_transform` injected seam + dt-hoist in `persistence.py`** (additive, adjudication #1): `_combined_belief_update` now computes `dt_days_since_last` at the TOP (a pure reordering — dt feeds only the readout) and accepts a keyword-only `prior_transform: Callable[[tuple,int|None],tuple] | None = None` threaded from `persist_learner_update` → `_persist_entity_group`; when provided it decays the recompute-base prior (read off the EVENT LOG) BEFORE damp/bayes; default `None` = identity = byte-identical to WU-5A2 (every WU-5A2 test stays green — the non-regression proof).
   - **Decay is UPDATE-time, anchored to the frozen `done_ts` (never `now()`), integer-day, clamped Δt≥0**, pulled toward `COLD_START_PRIOR`. It fires ONLY for an entity that RECEIVES an event this Done; the "unseen entity drifts to unknown" READOUT (§3 L415) is a PHASE-6 reader concern, explicitly NOT WU-5B1.
   - **`done.py` is UNCHANGED** — the flag is read inside `learner_update.py`, so the `run_learner_update` call site stays byte-identical.
3. **The `apollo/handlers/` row** (`learner_update.py` description): note the new `_LEARNER_DECAY_FLAG = "APOLLO_LEARNER_DECAY_ENABLED"` (+ `_learner_decay_enabled()`, default OFF EVERYWHERE) — when ON, `run_learner_update` builds the decay `prior_transform` closure (binding `decay_toward_prior(b, COLD_START_PRIOR, dt)`) and passes it into `persist_learner_update`; when OFF, passes `None` (byte-identical to WU-5A2). `run_learner_update`'s public signature is UNCHANGED. No migration (`dt_days_since_last` already populated by WU-5A2).

Keep the prose dense and consistent with the existing WU-4*/WU-5A* paragraph style in that table cell.

## 10. Coverage + verification commands (all LOCAL)

All commands use `.venv/Scripts/python.exe` (py3.12). Bare `python` is anaconda base (no testcontainers) — a `ModuleNotFoundError` there is an interpreter-selection error, switch, do NOT escalate.

**GATE — real-infra (changed files include `tests/database/**`):** Docker MUST be UP (29.5.3 / pgvector pg16). `pytest tests/database` must be GREEN-not-skipped. A SKIP means Docker is down → STOP (do not accept a skip as a pass).

```bash
# 1. Pure decay tests (no Docker needed)
.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_decay.py -v

# 2. Full pure WU-5A1/5B1 learner_model suite (byte-identity of the pure core)
.venv/Scripts/python.exe -m pytest apollo/learner_model/tests -v

# 3. REAL-PG gate — the WU-5A2 suite + the new decay integration test (Docker UP, NOT skipped)
.venv/Scripts/python.exe -m pytest tests/database/test_done_layer3_route_postgres.py \
    tests/database/test_done_shadow_route_postgres.py -v
#   -> expect: 16 prior WU-5A2 tests byte-identical green + 1 new decay test green;
#      shadow byte-identity me==0/ls==0 green. A SKIP = Docker down = STOP.

# 4. Full tests/database (the build-unit real-infra gate)
.venv/Scripts/python.exe -m pytest tests/database -q
#   -> MUST be green-not-skipped.

# 5. Patch coverage >= 95% (target 100%) vs the WU-5A2 parent branch
.venv/Scripts/python.exe -m pytest --cov=apollo --cov-report=xml \
    apollo/learner_model/tests tests/database/test_done_layer3_route_postgres.py
.venv/Scripts/python.exe -m diff_cover coverage.xml \
    --compare-branch=feat/apollo-kg-wu5a2-layer3-persist --fail-under=95
#   -> changed lines: decay.py (100% via Layer-1), the persistence.py additive
#      lines (the `if prior_transform is not None` True branch via the new PG test,
#      False branch via every existing WU-5A2 test), learner_update.py closure
#      (True branch via the new PG test; the `dt_days is None` guard via... see note).
```

**Coverage note on the `dt_days is None` closure guard:** the new PG integration test exercises the `dt_days is not None` path (a real 7-day gap). The `dt_days is None` branch (cold-start, no prior anchor, flag ON) is reachable by adding a SECOND tiny assertion inside the same integration test OR a focused variant: run `run_learner_update` flag-ON for a FIRST (cold-start) attempt where `prior_last_evidence_at is None` → the closure receives `dt_days=None` → returns the cold-start prior unchanged → posterior equals the no-decay cold-start posterior. Add this as a second attempt-1 assertion in `test_decay_flag_on_decays_base_prior` (assert the cold-start posterior is identical with and without the flag) so BOTH closure branches are covered without a redundant test function. If diff-cover still flags it, promote it to a named `test_decay_flag_on_cold_start_is_identity` integration test.

**Lint/format (repo convention — black/isort/ruff):**
```bash
.venv/Scripts/python.exe -m ruff check apollo/learner_model/decay.py apollo/learner_model/persistence.py apollo/handlers/learner_update.py
.venv/Scripts/python.exe -m black --check apollo/learner_model/decay.py apollo/learner_model/tests/test_decay.py
```

## 11. Risks (confidence-rated)

| Severity | Risk | Mitigation |
|---|---|---|
| **HIGH** | The dt-hoist or `prior_transform` kwarg accidentally changes a WU-5A2 posterior (the edit is not truly identity-default). | §8 procedure: capture the WU-5A2 baseline BEFORE the edit, diff after. Any change → STOP + ESCALATE, do NOT patch the WU-5A2 test. Reproduced-safe: dt feeds only the readout; default `None` skips the new line. |
| **MEDIUM** | `BeliefUpdate.prior_belief` now records the DECAYED prior (when the flag is ON) rather than the raw base. This is CORRECT (the §3 Step-0→Step-3 chain) but is a semantic change to the event-log `prior_belief` column on the decay path. | Intended + documented in apollo.md. The undecayed base is still recoverable (it is the prior attempt's `posterior_belief`); decay is re-derived from the recorded `dt_days_since_last` on each subsequent update (consistent with the §3 replay/refit path). No WU-5A2 row changes (flag OFF). |
| **MEDIUM** | diff-cover vs `feat/apollo-kg-wu5a2-layer3-persist` flags an uncovered branch (the `if prior_transform is not None` False arm or the `dt_days is None` closure guard). | False arm covered by every existing WU-5A2 fold test; True arm by the new PG test. The `dt_days is None` guard covered by the cold-start assertion in §10. Target 100%. |
| **LOW** | Integer `.days` truncation: a 20h gap records dt=0 → w=0 → no decay. | LOCKED accepted (adjudication #3) — matches the weekly/sparse cadence `k=0.05` targets; switching to fractional days would break the WU-5A2 `dt_days_since_last` column semantics + need a migration. Documented. |
| **LOW** | Real-PG `REAL[]` single-precision rounding makes the decayed-vs-undecayed distinctness assertion flaky at a tiny dt. | Use a 7-day gap (w≈0.295) so the decayed and undecayed posteriors differ well above REAL tolerance (`_belief_close` uses `rel=1e-5, abs=1e-6`); the distinctness assertion is `not _belief_close(...)`, comfortably separated. |
| **LOW** | Docker down → `tests/database` SKIPs and the gate silently "passes." | GATE rule: a SKIP is a FAIL → STOP (do not accept). Verify Docker 29.5.3 / pgvector pg16 is UP before running. |
| **LOW** | Executor edits `update.py` (the `apply_event` dt computation) thinking it needs the hoist too. | Explicit OUT (§2 read-only list): `apply_event` is a DIFFERENT function NOT on the persist path; only `_combined_belief_update` in `persistence.py` is hoisted. |

## 12. Out-of-scope boundaries (held firmly)

- **NO negotiation-move hedge** (WU-5B2). Do NOT add `likelihood_multiplier_for_entity` / `negotiation_move_for_entity` kwargs, do NOT touch `apollo_kg_negotiations`, do NOT create `negotiation.py`. WU-5B1 adds ONLY `prior_transform`.
- **NO retry janitor** (WU-5B3). No worker, no `learner_janitor.py`, no `Procfile` change, no clear-flag edit, no dead-letter/backoff, no migration 028.
- **NO chat-keyword persist** (WU-5B4).
- **NO migration.** `dt_days_since_last` already exists and is populated by WU-5A2. If the executor believes a migration is needed → ESCALATE (it is not).
- **NO read-time decay / "unseen entity drifts to unknown" readout** (§3 L415) — PHASE-6 reader concern. Update-time decay here fires ONLY for an entity that receives an event this Done. Do NOT add `now()` anywhere.
- **NO re-parameterizing the math.** `DECAY_K=0.05`, the formula, and the golden vectors are LOCKED. Do not re-derive, "improve," or grid-search `k`.
- **NO edits to `belief.py` / `update.py` / `state_model.py` / `done.py`.** `decay.py` imports nothing from them except the closure's `COLD_START_PRIOR` import in `learner_update.py`. The `apply_event` dt computation in `update.py` is NOT hoisted (different function, not on the persist path).
- **NO new packages** (decay is pure stdlib `math`). If a package seems needed → ESCALATE.
- **NO branch/PR operations.** Stay on `feat/apollo-kg-wu5b1-between-session-decay`; do not create/switch branches, do not push, do not open PRs.
- **NEVER apply migrations to any remote DB** (test or prod). All DB work is LOCAL Docker/Testcontainers only.
- **Frozen-file edits are PERMITTED but ONLY additively / identity-default.** A non-additive change to `persistence.py` (anything that alters a WU-5A2 posterior or side-effect) → ESCALATE, do not push through.

## 13. Deviations the executor MAY take without escalating

- **Exact wording/structure of the pure tests** (helper functions, parametrization via `@pytest.mark.parametrize`) — as long as every expected number is a HARDCODED golden constant (not the function's own output) and all §7 Layer-1 behaviors are covered.
- **Whether `decay_toward_prior` imports `COLD_START_PRIOR` internally vs takes `prior` as a param.** The plan recommends the param form (keeps `decay.py` import-free) but the import form is acceptable IF it still imports from `belief.py` and never re-literals `(0.20,0.60,0.20)`. Either keeps the golden vectors identical.
- **Splitting the `dt_days is None` cold-start coverage into its own named integration test** vs folding it into `test_decay_flag_on_decays_base_prior` (§10) — executor's call based on what diff-cover reports.
- **The `PriorTransform` type-alias name/placement** in `persistence.py`.
- **Minor `apollo.md` prose phrasing** — as long as `decay.py`, the `prior_transform` seam, the dt-hoist, the flag, the update-time/never-`now()`/integer-day/clamp facts, and the read-time-decay deferral are all registered, and `last_verified` is bumped to `2026-06-19`.
- **Commit message wording** (conventional commit, e.g. `feat: apollo KG WU-5B1 between-session decay (§3 Step 0) behind APOLLO_LEARNER_DECAY_ENABLED`) — NO Co-Authored-By / NO attribution; code + apollo.md reconciliation in the SAME commit.

**Escalate (do NOT deviate) if:** any WU-5A2 test output changes; a migration seems required; a non-additive `persistence.py` change seems required; a new package seems required; or the golden vectors don't reproduce against the frozen `belief.py`.

