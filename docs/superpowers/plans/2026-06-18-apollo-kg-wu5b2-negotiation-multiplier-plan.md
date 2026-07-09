# WU-5B2 — §3 negotiation-move likelihood multiplier (TDD implementation plan)

**Status:** PLAN — build-ready. No code/tests touched yet.
**Date:** 2026-06-19
**Branch (checked out, do NOT switch):** `feat/apollo-kg-wu5b2-negotiation-multiplier`
**Stacks on:** WU-5B1 (`feat/apollo-kg-wu5b1-between-session-decay`) — the injected-parameter seam + dt-hoist in `persistence.py` are ALREADY LANDED on this branch. Diff-cover compares vs `feat/apollo-kg-wu5b1-between-session-decay`.
**Spec:** `docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md` §3 Step-1 (L419-431, the `negotiation move | — | ×1.2 | ×1.1` row + "never 0"), §3.1 damper (multiplier BEFORE damp).
**Adjudication source:** `docs/superpowers/plans/2026-06-18-apollo-kg-wu5b-split-proposal.md` — WU-5B2 section + ORCHESTRATOR ADJUDICATION block (decisions #1, #3, #4 incl. #4b RATIFIED misconception-suppression).

---

## 0. Scope contract (read first)

**Goal (one line):** behind `APOLLO_LEARNER_NEGOTIATION_ENABLED` (default OFF), multiply a qualifying entity's COMBINED §3 likelihood by `NEGOTIATION_LIKELIHOOD=(1.0,1.2,1.1)` ONCE per entity BEFORE the damper, persist the representative `negotiation_move` on each event row, and SUPPRESS the multiplier (→ identity ×1.0) on any entity carrying a misconception so a negotiation move can never dilute a real misconception.

**This unit IS** the §3 negotiation MULTIPLIER (L428 table row). It is **NOT** the §10 chat-keyword persist (that is the SEPARATE later unit WU-5B4 — explicitly out of scope, §11).

**Architecture:** additive, identity-default. A NEW pure module `apollo/learner_model/negotiation.py` (constants + a tiny async read + 3 pure functions) + ADDITIVE keyword-only edits to `persistence.py` and `learner_update.py` that ride the SAME injected-parameter seam WU-5B1 already added for decay. NO migration (the `negotiation_move` column and `apollo_kg_negotiations` table both exist — verified §1). NO Neo4j read at fold time (moves live in Postgres `apollo_kg_negotiations`; the bridge is the events' `evidence_node_ids`, already in memory).

**Tech stack:** Supabase Postgres 16 + pgvector; SQLAlchemy async + asyncpg; pytest + Testcontainers (real `pgvector pg16` via Docker 29.5.3) for the integration layer. Interpreter: `.venv/Scripts/python.exe` (py3.12 — bare `python` is anaconda base with no testcontainers; switch, do not escalate).

**Hard rules carried in from CLAUDE.md / the prompt:**
- Files + LOCAL Docker only. NEVER apply migrations to any remote DB (none needed here regardless).
- Patch coverage of changed lines >= 95% via `diff-cover` vs `feat/apollo-kg-wu5b1-between-session-decay` (target 100%).
- Frozen-file edits (`persistence.py`, `learner_update.py`) are PERMITTED but ONLY additively / identity-default. If a non-additive change seems required → STOP and ESCALATE.
- The FULL WU-5A2 + WU-5B1 suites stay BYTE-IDENTICAL green with the NEGOTIATION flag OFF (all 17 `test_done_layer3_route_postgres.py` cases incl. the decay test + `test_done_shadow_route_postgres.py` byte-identity). Flag OFF ⇒ injected kwargs default to identity (`None`) ⇒ no multiplier, no `negotiation_move` override.
- `tests/database/**` is touched ⇒ the REAL-INFRA gate triggers ⇒ `pytest tests/database` MUST be GREEN-not-skipped (Docker UP). A SKIP = STOP.
- Reconcile `docs/architecture/apollo.md` in the SAME commit; bump `last_verified` to 2026-06-19.
- Immutable style (return NEW objects). Small files (<800 lines). No new packages.

## 1. Ground truth (verified file:line)

### The negotiation source table (`apollo_kg_negotiations`) — VERIFIED, do NOT assume
- Migration `database/migrations/021_apollo_kg_negotiations.sql:29-48`. Columns (also `apollo/persistence/models.py:316-329`, class `KGNegotiation`):
  - `id` BIGINT identity PK
  - `attempt_id` BIGINT NOT NULL → `apollo_problem_attempts(id)` ON DELETE CASCADE
  - **`entry_id`** TEXT NOT NULL — the Neo4j node_id (NOT a FK; same keyspace as `LearnerEvent.evidence_node_ids`)
  - **`actor`** TEXT NOT NULL CHECK IN (`'student'`,`'parser'`,`'system'`) — `store._log_negotiation` always writes `'student'`, but the column ALLOWS the other two, so the read MUST filter `actor='student'`.
  - **`move`** TEXT NOT NULL CHECK IN (`'challenge'`,`'paraphrase'`,`'skip'`)
  - `payload` JSONB, `created_at` TIMESTAMPTZ DEFAULT now()
  - Index `apollo_kg_negotiations_attempt_entry_idx (attempt_id, entry_id)`.
- The operative move is "the latest row per `(attempt_id, entry_id)`" (`models.py:309-310`, `021…sql:50-54`). The read therefore orders by `created_at` and keeps latest-wins per `entry_id`.
- `done.py._entries_with_moves` ALREADY reads this table but LOSSILY (returns a SET of `entry_id`, drops `move` and `actor`) — WU-5B2 needs a NEW richer read `{node_id: move}`, `actor='student'` only, latest-wins. (Do not reuse the lossy one.)

### The target column (`apollo_mastery_events.negotiation_move`) — VERIFIED
- Migration `026_apollo_learner_model.sql:155` — `negotiation_move TEXT NULL  -- challenge|paraphrase|skip|null`. ORM `models.py:459` — `negotiation_move = Column(Text, nullable=True)`. EXISTS; NO migration needed.
- Currently hard-NULLed at the ORM-build provenance: `update.py:103` (`negotiation_move=None,  # NULL v1 (multiplier not applied)`) feeds `MasteryEventRowSpec.negotiation_move` (`state_model.py:63`), copied straight onto the ORM row by `persistence.py:127`. WU-5B2 OVERRIDES the frozen `None` at the ORM-build site in `persistence.py` (NOT by editing `update.py`/`state_model.py`).

### The §3 fold seam (already extended by WU-5B1) — where the multiplier folds in
- `apollo/learner_model/persistence.py:324-389` `_combined_belief_update`: computes `dt_days_since_last` at the TOP (WU-5B1 hoist), defaults `prior`, applies `prior_transform` (decay), then **`combined_likelihood`** = product of per-event `likelihood_for_event` (L367-372), `damped = damp(combined_likelihood, q)` (L373), `posterior = bayes_update(prior, damped)` (L374). The multiplier folds into `combined_likelihood` AFTER the product loop and BEFORE `damp` (L373).
- `_persist_entity_group` (`persistence.py:392-465`) loops `event_to_row_specs` (L445) → `db.add(_mastery_event_orm_from_spec(mastery_spec))` (L453). The `negotiation_move` override happens between these — either by passing the resolved move into the ORM-build, or by rebuilding the spec. (Design §3 picks the exact site.)
- `persist_learner_update` (`persistence.py:234-321`) already takes `prior_transform` kw (L243) and groups events by entity (`events_by_entity`, L290-298). The per-entity multiplier + move are resolved here from the injected maps and threaded down to `_persist_entity_group`.
- `run_learner_update` (`handlers/learner_update.py:56-111`) already reads a flag internally (`_learner_decay_enabled`, L43-44) and builds the `prior_transform` closure (L86-92) — WU-5B2 mirrors this EXACT shape for `APOLLO_LEARNER_NEGOTIATION_ENABLED`.

### The event model — the bridge + the suppression predicate inputs
- `apollo/grading/event_model.py:41-58` `LearnerEvent`: fields `canonical_key`, `event_kind` (a `LearnerEventKind`), `score`, `confidence`, `misconception_code` (set on misconception/corrected/partial-conflict events), **`evidence_node_ids: tuple[str, ...]`** (= the finding's `student_node_ids` = Neo4j node_ids — the bridge). `LearnerEvent` carries NO `negotiation_move` field — thread the move as an injected map, do NOT add a field to the frozen event.
- `LearnerEventKind` (`event_model.py:30-38`): `COVERED|MISSING|PARTIAL|MISCONCEPTION|CORRECTED`. The suppression rule trips on `MISCONCEPTION` OR a `PARTIAL` carrying a non-`None` `misconception_code` (the ambiguous-order conflict event, `events.py:243-251`, sets both). A `CORRECTED` event (`events.py:222-230`) carries a `misconception_code` too BUT the multiplier is ALLOWED on it (the student fixed it — genuine metacognition); so the predicate keys on `event_kind`, treating ONLY `MISCONCEPTION` and `PARTIAL`-with-code as suppressing.

### Harness facts (integration layer)
- `tests/database/test_done_layer3_route_postgres.py` is the target file (17 tests today, pytestmark `integration`). The `db_session` fixture (real PG, `Base.metadata.create_all`, savepoint rollback) is provided by the shared `tests/database` conftest. Seed helpers `_seed_course_with_entity` / `_seed_session_attempt` / `_covered_finding` / `_misconception_finding` / `_audited` / `_shadow` / `_belief_close` already exist and are reused.
- `KGNegotiation` is importable from `apollo.persistence.models` and is in `Base.metadata`, so the integration tests seed `apollo_kg_negotiations` rows directly via ORM (mirrors the `LearnerState` seeding pattern).
- Flag-OFF byte-identity is already gated by `test_layer3_flag_off_no_write` + `test_done_shadow_route_postgres.py`.

## 2. LOCKED constants + golden vectors

All reproduced 2026-06-19 with `.venv/Scripts/python.exe` against the FROZEN `apollo/learner_model/belief.py`. HARDCODE these in the tests — never assert against the function's own output.

| constant | value | source |
|---|---|---|
| `NEGOTIATION_LIKELIHOOD` | `(1.0, 1.2, 1.1)` — order `(p_misc, p_shaky, p_mastered)` | spec L428; "blank `L_misc`" = 1.0 (L430) |
| `MULTIPLIER_MOVES` | `frozenset({"challenge", "paraphrase"})` | adjudication #4 — `skip` is NOT a confidence signal → no-op identity ×1.0 |
| `NO_OP_LIKELIHOOD` | `(1.0, 1.0, 1.0)` | imported from `belief.py` (never re-literal'd) — the identity multiplier |
| applied | ONCE per qualifying entity, folded into `combined_likelihood` BEFORE `damp` | adjudication; §3.1 damper invariant |
| suppressed | identity ×1.0 on any entity whose events include `MISCONCEPTION` OR `PARTIAL`-with-`misconception_code` | adjudication #4b (RATIFIED — implement it) |

### Golden vectors (q=1 unless noted; cold-start base `COLD_START_PRIOR=(0.20,0.60,0.20)`)

**GV-1 — misconception entity + negotiation move, suppression PINS identity.** A misconception event on an entity that ALSO has a qualifying move:
- WITHOUT suppression (the rejected literal-spec path, for the distinctness assertion only): combined `L = MISCONCEPTION_LIKELIHOOD ⊙ NEG`, damp(q=1), bayes(cold-start) → `(0.4399, 0.5279, 0.0323)`, **p_misc = 0.4399**.
- WITH the ratified suppression (multiplier → identity): combined `L = MISCONCEPTION_LIKELIHOOD` only → `(0.4839, 0.4839, 0.0323)`, **p_misc = 0.4839**. **PIN THE SUPPRESSED 0.4839.** Suppression RESTORES the otherwise-reproduced −0.044 p_misc cancel (0.4839 − 0.4399 = 0.0440): a negotiation move must NOT dilute a misconception.

**GV-2 — CORRECTED/COVERED entity + move, the boost APPLIES.** Single covered@0.6, q=0.7, multiplier BEFORE damp:
- combined `L = _covered_likelihood(0.6) ⊙ NEG`, damp(q=0.7), bayes(cold-start) → posterior `(0.1102, 0.7379, 0.1519)`, **BEFORE-damp mastery = 0.5209** (LOCK this). (Contrast AFTER-damp mastery = 0.5232 — proves BEFORE-damp is the locked form: at low q the BEFORE-form shrinks the bump toward the no-hedge posterior, honoring the §3.1 damper; do NOT fold the multiplier into `q`.)

**GV-3 — never-0 holds.** `covered(1.0)` floored = `(0.02, 0.02, 1.0)`; `⊙ NEG` = `(0.02, 0.024, 1.1)`, min `0.02 > 0`. All multipliers `≥ 1` push UP, never annihilate.

**GV-4 — `skip` → no-op identity ×1.0.** `negotiation_multiplier("skip") == NO_OP_LIKELIHOOD`; `negotiation_multiplier(None) == NO_OP_LIKELIHOOD`. A skip (or no move) leaves the fold byte-identical.

**GV-5 — representative move (once-per-entity).** `entity_negotiation_move` returns the LATEST qualifying move among the entity's `evidence_node_ids` — exactly one per entity, so a student spamming moves cannot stack/inflate.

**GV-6 — latest-wins + actor filter (the read).** Two `apollo_kg_negotiations` rows for one `entry_id` → `load_student_moves` keeps the one with the greater `created_at`; a non-`student` actor row for the same `entry_id` is IGNORED entirely.

## 3. Files to CREATE / EDIT — public signatures

### 3.1 NEW — `apollo/learner_model/negotiation.py` (pure + one tiny async read)

Module header mirrors `decay.py`/`belief.py` (named constants, no IO except the one async read; returns NEW objects). PUBLIC surface:

```python
from __future__ import annotations
from collections.abc import Mapping, Sequence
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from apollo.grading.event_model import LearnerEvent, LearnerEventKind
from apollo.learner_model.belief import NO_OP_LIKELIHOOD
from apollo.persistence.models import KGNegotiation

# §3 L428 negotiation row, order (p_misc, p_shaky, p_mastered). Blank L_misc = 1.0
# (L430 "never 0"). LOCKED — do NOT re-derive or re-parameterize.
NEGOTIATION_LIKELIHOOD: tuple[float, float, float] = (1.0, 1.2, 1.1)

# Which moves count as a metacognition signal. `skip` ("I won't fight about it")
# is NOT a confidence signal -> no-op (adjudication #4). LOCKED.
MULTIPLIER_MOVES: frozenset[str] = frozenset({"challenge", "paraphrase"})

# The actor whose rows are metacognitive evidence (021 CHECK allows
# student|parser|system; store always writes 'student'). LOCKED.
STUDENT_ACTOR: str = "student"


async def load_student_moves(
    db: AsyncSession, *, attempt_id: int
) -> dict[str, str]:
    """{entry_id (Neo4j node_id) -> latest student move} for this attempt.

    Reads apollo_kg_negotiations WHERE attempt_id = :id AND actor = 'student',
    ordered by created_at ASC, building a dict so the LAST (latest) row per
    entry_id wins. parser/system rows are EXCLUDED. NO Neo4j read."""
    rows = (await db.execute(
        select(KGNegotiation.entry_id, KGNegotiation.move)
        .where(
            KGNegotiation.attempt_id == attempt_id,
            KGNegotiation.actor == STUDENT_ACTOR,
        )
        .order_by(KGNegotiation.created_at.asc(), KGNegotiation.id.asc())
    )).all()
    return {entry_id: move for entry_id, move in rows}  # last write wins -> latest


def entity_negotiation_move(
    entity_events: Sequence[LearnerEvent], moves: Mapping[str, str]
) -> str | None:
    """The representative move for ONE entity = the move on the LAST (by the
    deterministic converter order) of the entity's evidence_node_ids that has a
    move; None when none of the entity's nodes were negotiated. ONE move per
    entity (a student spamming moves cannot stack)."""
    representative: str | None = None
    for event in entity_events:
        for node_id in event.evidence_node_ids:
            move = moves.get(node_id)
            if move is not None:
                representative = move
    return representative


def negotiation_multiplier(move: str | None) -> tuple[float, float, float]:
    """NEGOTIATION_LIKELIHOOD when move in MULTIPLIER_MOVES, else NO_OP_LIKELIHOOD
    (skip / None / unknown -> identity ×1.0). Never returns a 0 component."""
    if move in MULTIPLIER_MOVES:
        return NEGOTIATION_LIKELIHOOD
    return NO_OP_LIKELIHOOD


def suppresses_hedge(entity_events: Sequence[LearnerEvent]) -> bool:
    """RATIFIED adjudication #4b: True when any event is a MISCONCEPTION, OR a
    PARTIAL carrying a non-None misconception_code (the ambiguous-order conflict
    event). CORRECTED is ALLOWED (the student fixed it). When True the caller
    forces the multiplier to identity ×1.0 — a negotiation move must NOT dilute a
    misconception (restores the −0.044 p_misc cancel)."""
    for event in entity_events:
        if event.event_kind == LearnerEventKind.MISCONCEPTION:
            return True
        if (
            event.event_kind == LearnerEventKind.PARTIAL
            and event.misconception_code is not None
        ):
            return True
    return False
```

Note on `entity_negotiation_move` representative choice: "last with a move in iteration order" is deterministic because `convert_findings_to_events` returns a sorted tuple and `persist_learner_update` preserves that order into `events_by_entity`. The move's USE is the multiplier (suppression-gated) + the persisted provenance; the exact tie-break among multiple qualifying moves on one entity is not load-bearing for the belief (any qualifying move yields the same `NEGOTIATION_LIKELIHOOD`), only for the persisted string — so "latest in converter order" is a stable, documented choice.

### 3.2 EDIT (additive, identity-default) — `apollo/learner_model/persistence.py`

A new module-level type alias next to `PriorTransform` (`persistence.py:99`):

```python
# WU-5B2 §3 negotiation multiplier seam (adjudication #4). Two identity-default
# keyword-only callables threaded through the fold:
#   - likelihood_multiplier_for_entity(entity_events) -> (m_misc, m_shaky, m_mastered)
#       multiplies the COMBINED likelihood ONCE per entity BEFORE damp; identity
#       (1,1,1) when no qualifying/suppressed move. Default None = no-op.
#   - negotiation_move_for_entity(entity_events) -> str | None
#       the representative move string persisted onto each event row (overrides
#       the frozen MasteryEventRowSpec.negotiation_move=None). Default None.
LikelihoodMultiplierForEntity = Callable[[list], tuple[float, float, float]]
NegotiationMoveForEntity = Callable[[list], "str | None"]
```

Signature additions (ALL keyword-only, default `None`):
- `persist_learner_update(..., prior_transform=None, *, likelihood_multiplier_for_entity: LikelihoodMultiplierForEntity | None = None, negotiation_move_for_entity: NegotiationMoveForEntity | None = None)` — pass both straight through to `_persist_entity_group`.
- `_persist_entity_group(..., prior_transform=None, likelihood_multiplier_for_entity=None, negotiation_move_for_entity=None)`:
  - compute `entity_multiplier = likelihood_multiplier_for_entity(entity_events) if likelihood_multiplier_for_entity is not None else NO_OP_LIKELIHOOD`;
  - compute `move = negotiation_move_for_entity(entity_events) if negotiation_move_for_entity is not None else None`;
  - pass `likelihood_multiplier=entity_multiplier` into `_combined_belief_update`;
  - at the ORM-build site (the `event_to_row_specs` → `_mastery_event_orm_from_spec` loop, `persistence.py:441-453`), OVERRIDE the spec's `negotiation_move` with `move`. Use `dataclasses.replace(mastery_spec, negotiation_move=move)` (immutable — `MasteryEventRowSpec` is frozen) BEFORE `_mastery_event_orm_from_spec`. When `move is None` the replace is a no-op-equivalent (spec already `None`) → byte-identical.
- `_combined_belief_update(..., prior_transform=None, *, likelihood_multiplier: tuple[float,float,float] = NO_OP_LIKELIHOOD)`:
  - after the per-event product loop builds `combined_likelihood` (L367-372) and BEFORE `damped = damp(...)` (L373), insert:
    `combined_likelihood = tuple(a * m for a, m in zip(combined_likelihood, likelihood_multiplier, strict=True))`.
  - With the default `NO_OP_LIKELIHOOD` this multiply is the identity (a*1.0=a) → byte-identical to the WU-5B1 body. (Defaulting to `NO_OP_LIKELIHOOD` rather than `None` keeps the hot-path branch-free; the only new line is an always-safe identity multiply.)

**Why `dataclasses.replace` and not edit `update.py`:** the prompt mandates the override happen at the ORM-build site in `persistence.py`, NOT by adding a field to the frozen `LearnerEvent` or editing `update.py:103`/`state_model.py:63`. `replace` is immutable and frozen-dataclass-safe.

### 3.3 EDIT (additive) — `apollo/handlers/learner_update.py`

Mirror the EXACT shape of the WU-5B1 decay flag (`learner_update.py:34-44, 86-92`):

```python
from apollo.learner_model.negotiation import (
    entity_negotiation_move,
    load_student_moves,
    negotiation_multiplier,
    suppresses_hedge,
)

_LEARNER_NEGOTIATION_FLAG: str = "APOLLO_LEARNER_NEGOTIATION_ENABLED"

def _learner_negotiation_enabled() -> bool:
    return os.environ.get(_LEARNER_NEGOTIATION_FLAG, "").lower() in ("1", "true", "yes")
```

Inside `run_learner_update`'s `try:` (after the decay closure block, BEFORE the `persist_learner_update` call), add:

```python
likelihood_multiplier_for_entity = None
negotiation_move_for_entity = None
if _learner_negotiation_enabled():
    moves = await load_student_moves(db, attempt_id=attempt.id)

    def negotiation_move_for_entity(entity_events):
        return entity_negotiation_move(entity_events, moves)

    def likelihood_multiplier_for_entity(entity_events):
        # RATIFIED suppression: a misconception entity forces identity ×1.0.
        if suppresses_hedge(entity_events):
            return NO_OP_LIKELIHOOD
        return negotiation_multiplier(entity_negotiation_move(entity_events, moves))
```

Then thread both into the existing `persist_learner_update(...)` call (keyword args). `run_learner_update`'s PUBLIC signature is UNCHANGED (flag read internally; `done.py` call site stays byte-identical). Import `NO_OP_LIKELIHOOD` from `apollo.learner_model.belief` (already used by the decay path's sibling imports). When the flag is OFF both vars stay `None` → identity-default → byte-identical to WU-5B1.

**Single-`load_student_moves` call:** the read runs ONCE per `run_learner_update` (not per entity). The closures capture the in-memory `moves` dict; `entity_negotiation_move` is pure over it.

## 4. TDD build order

Write REAL tests FIRST at each step (RED → GREEN → next). No skip-marks, no xfail, no assert-nothing.

1. **RED — pure golden-vector tests** (`apollo/learner_model/tests/test_negotiation.py`, §5). Write all of §5 against the not-yet-existing `negotiation.py` public surface. Run with `.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_negotiation.py` → import error / failures.
2. **GREEN — create `apollo/learner_model/negotiation.py`** (§3.1). The pure functions (`entity_negotiation_move`, `negotiation_multiplier`, `suppresses_hedge`) make the pure tests pass with no DB. The async `load_student_moves` is covered by §6 (real PG); for the pure file it is import-only.
3. **RED — persistence/handler integration tests** (`tests/database/test_done_layer3_route_postgres.py`, §6). Add the new cases driving the flag-ON path (boost, suppression, persist round-trip, latest-wins, actor filter, skip no-op, flag-OFF byte-identity). Run `pytest tests/database/test_done_layer3_route_postgres.py` (Docker UP) → new tests fail.
4. **GREEN — additive edits** to `persistence.py` (§3.2) then `learner_update.py` (§3.3). Re-run the integration file → all new tests pass AND all 17 prior tests stay green.
5. **Non-regression sweep:** `pytest tests/database` (full real-infra gate, GREEN-not-skipped) + `pytest apollo/learner_model/tests` + the byte-identity guard `test_done_shadow_route_postgres.py`. Run `diff-cover coverage.xml --compare-branch=origin/feat/apollo-kg-wu5b1-between-session-decay --fail-under=95`.
6. **Owner doc** (§8) reconciled in the SAME commit; `last_verified` → 2026-06-19.

## 5. Test list — PURE golden-vector layer (`apollo/learner_model/tests/test_negotiation.py`)

Pure imports only — NO DB, NO LLM, NO Neo4j, NO network (mirrors `test_decay.py`). Every expected number is a HARDCODED golden constant from §2, NEVER the function's own output. Build `LearnerEvent`s directly (frozen dataclass; no DB). Module-level locked constants to declare at top:

```python
_NEG = (1.0, 1.2, 1.1)              # NEGOTIATION_LIKELIHOOD (hardcoded, asserted == the constant)
_NO_OP = (1.0, 1.0, 1.0)
# GV-1 suppressed misconception posterior (hand-reproduced):
_MISC_SUPPRESSED = (0.4839, 0.4839, 0.0323)   # p_misc 0.4839 (suppression identity)
_MISC_BOOSTED   = (0.4399, 0.5279, 0.0323)    # p_misc 0.4399 (no-suppression, distinctness only)
# GV-2 covered@0.6 q=0.7 BEFORE-damp:
_COV_BEFORE_DAMP_MASTERY = 0.5209
_COV_AFTER_DAMP_MASTERY  = 0.5232
```

Tests (each name → asserts → mocking):

| # | Test name | Asserts | Mocking |
|---|---|---|---|
| 1 | `test_negotiation_likelihood_constant_locked` | `NEGOTIATION_LIKELIHOOD == (1.0, 1.2, 1.1)` and order is `(p_misc, p_shaky, p_mastered)` (L_misc is the blank 1.0) | none (pure) |
| 2 | `test_multiplier_moves_locked` | `MULTIPLIER_MOVES == frozenset({"challenge", "paraphrase"})`; `"skip" not in MULTIPLIER_MOVES` | none |
| 3 | `test_negotiation_multiplier_challenge` | `negotiation_multiplier("challenge") == _NEG` (assert equals the hardcoded tuple, also `is NEGOTIATION_LIKELIHOOD`) | none |
| 4 | `test_negotiation_multiplier_paraphrase` | `negotiation_multiplier("paraphrase") == _NEG` | none |
| 5 | `test_negotiation_multiplier_skip_is_identity` | `negotiation_multiplier("skip") == _NO_OP` and `is NO_OP_LIKELIHOOD` (GV-4) | none |
| 6 | `test_negotiation_multiplier_none_is_identity` | `negotiation_multiplier(None) == _NO_OP` (GV-4 — no move → identity) | none |
| 7 | `test_negotiation_multiplier_unknown_is_identity` | `negotiation_multiplier("trace") == _NO_OP` (defensive: any non-qualifying string → identity, never raises) | none |
| 8 | `test_negotiation_multiplier_never_zero` | for every move in `{challenge, paraphrase, skip, None}`, every component of the result `> 0` (GV-3 precondition) | none |
| 9 | `test_entity_negotiation_move_picks_node_with_move` | a covered event with `evidence_node_ids=("n1",)`, `moves={"n1":"challenge"}` → `entity_negotiation_move([ev], moves) == "challenge"` (GV-5) | none |
| 10 | `test_entity_negotiation_move_none_when_no_node_negotiated` | event nodes `("nX",)`, `moves={"n1":"challenge"}` → `None` | none |
| 11 | `test_entity_negotiation_move_one_per_entity_no_stack` | one event with `evidence_node_ids=("n1","n2","n3")` all in `moves` → returns a SINGLE move string (NOT a list), proving once-per-entity (GV-5) | none |
| 12 | `test_entity_negotiation_move_representative_is_deterministic` | two events for the entity, the later (in list order) carrying the move-bearing node → returns that move; reordering inputs is documented deterministic | none |
| 13 | `test_suppresses_hedge_true_on_misconception` | `suppresses_hedge([misconception_event])` is `True` (event_kind MISCONCEPTION) | none |
| 14 | `test_suppresses_hedge_true_on_partial_with_code` | a PARTIAL event with `misconception_code="misc.x"` → `True` (the ambiguous-order conflict event) | none |
| 15 | `test_suppresses_hedge_false_on_partial_without_code` | a PARTIAL event with `misconception_code=None` → `False` (a plain partial does not suppress) | none |
| 16 | `test_suppresses_hedge_false_on_corrected` | a CORRECTED event (carries a code) → `False` — the boost is ALLOWED on corrected (adjudication #4: student fixed it) | none |
| 17 | `test_suppresses_hedge_false_on_covered` | a plain COVERED event → `False` | none |
| 18 | `test_suppresses_hedge_true_if_any_event_misconception` | a list `[covered_event, misconception_event]` → `True` (any-event semantics) | none |
| 19 | `test_gv1_misconception_suppressed_identity_pins_0_4839` | reproduce the FOLD MANUALLY using frozen `belief.py` primitives: `combined = _clamp_each(MISCONCEPTION_LIKELIHOOD)` (suppression → identity, NO multiply), `bayes_update(COLD_START_PRIOR, damp(combined, 1.0))` → `pytest.approx(_MISC_SUPPRESSED, abs=5e-5)`, and p_misc `== pytest.approx(0.4839, abs=5e-5)`. Asserts the SUPPRESSED value is what the multiplier+suppression must produce. | none — uses frozen belief fns directly |
| 20 | `test_gv1_misconception_unsuppressed_distinct_proves_cancel` | reproduce WITHOUT suppression: `combined = MISCONCEPTION_LIKELIHOOD ⊙ NEGOTIATION_LIKELIHOOD`, fold → `pytest.approx(_MISC_BOOSTED, abs=5e-5)`, p_misc `0.4399`; assert `0.4839 - 0.4399 == pytest.approx(0.0440, abs=5e-4)` (the −0.044 cancel suppression restores) | none |
| 21 | `test_gv2_covered_boost_before_damp_mastery_0_5209` | `combined = _covered_likelihood(0.6) ⊙ NEGOTIATION_LIKELIHOOD` (multiplier BEFORE damp), `post = bayes_update(COLD_START_PRIOR, damp(combined, 0.7))`, `mastery_of(post) == pytest.approx(0.5209, abs=5e-5)`; AND assert `≠ _COV_AFTER_DAMP_MASTERY` (proves BEFORE-damp form) | none |
| 22 | `test_gv3_never_zero_covered_one_times_neg` | `_covered_likelihood(1.0) == (0.02,0.02,1.0)`; `⊙ NEGOTIATION_LIKELIHOOD == (0.02, 0.024, 1.1)`; `min(...) == 0.02 > 0` (GV-3) | none |
| 23 | `test_gv4_skip_noop_fold_byte_identical` | covered@0.6 folded with `negotiation_multiplier("skip")` equals the SAME fold with `NO_OP_LIKELIHOOD` (skip leaves the belief unchanged vs no-move) | none |

(23 pure tests. Tests 19-23 exercise the multiplier *math* by composing frozen `belief.py` primitives the way the production fold will — they pin the contract the persistence edit must satisfy, independent of the production code.)

## 6. Test list — real-PG integration layer (`tests/database/test_done_layer3_route_postgres.py`)

`pytest.mark.integration` (inherited from the file's `pytestmark`). Reuse the existing harness helpers (`_seed_course_with_entity`, `_seed_session_attempt`, `_covered_finding`, `_misconception_finding`, `_audited`, `_shadow`, `_belief_close`). NO live LLM/Neo4j — the multiplier is pure and the read is plain SQL over the seeded `apollo_kg_negotiations` rows. Add a NEW seed helper:

```python
from apollo.persistence.models import KGNegotiation

async def _seed_negotiation(db, *, attempt_id, entry_id, move, actor="student", created_at=None):
    db.add(KGNegotiation(
        attempt_id=attempt_id, entry_id=entry_id, move=move, actor=actor,
        payload={}, **({"created_at": created_at} if created_at else {}),
    ))
    await db.flush()
```

The covered/misconception findings already use `student_node_ids=("stu_1",)` / `("stu_m",)` → those become the event `evidence_node_ids` → the negotiation `entry_id` must MATCH them. Independent oracle for every belief assertion: recompute INLINE from frozen `belief.py` + `negotiation.py` constants (the `_expected_update`-style pattern already in the file), NEVER by calling the production fold.

Tests (all set `monkeypatch.setenv("APOLLO_LEARNER_NEGOTIATION_ENABLED", "1")` except the flag-OFF case):

| # | Test name | Asserts | Setup / mocking |
|---|---|---|---|
| I1 | `test_negotiation_flag_on_covered_entity_gets_boost` | persisted `LearnerState.belief` for the covered entity == the INLINE oracle `bayes_update(COLD_START_PRIOR, damp(_covered_likelihood(s) ⊙ NEGOTIATION_LIKELIHOOD, q))` (boost applied BEFORE damp); AND `≠` the no-boost posterior (distinctness — proves the multiplier fired) | seed covered finding on `stu_1`; seed `_seed_negotiation(entry_id="stu_1", move="challenge")`; flag ON; `run_learner_update` |
| I2 | `test_negotiation_flag_on_misconception_entity_unchanged` | a MISCONCEPTION entity that ALSO has a qualifying move on its node persists a posterior BYTE-IDENTICAL to the no-negotiation misconception posterior (suppression → identity 0.4839 p_misc); assert `_belief_close(state.belief, misc_only_oracle)` AND p_misc `== approx(0.4839)` | seed misconception finding on `stu_m` (key `_CONTINUITY`); `_seed_negotiation(entry_id="stu_m", move="paraphrase")`; flag ON |
| I3 | `test_negotiation_move_persisted_on_event_row` | the persisted `MasteryEvent.negotiation_move == "challenge"` (the representative move) for the covered entity; the override replaced the frozen `None` | covered on `stu_1`; `_seed_negotiation(entry_id="stu_1", move="challenge")`; flag ON |
| I4 | `test_negotiation_latest_wins_two_moves_one_node` | two negotiation rows for `stu_1` (`paraphrase` at t0, `challenge` at t1>t0) → persisted `MasteryEvent.negotiation_move == "challenge"` AND the belief reflects the (still-qualifying) boost | `_seed_negotiation` twice with explicit ascending `created_at`; flag ON |
| I5 | `test_negotiation_actor_filter_ignores_non_student` | a `parser`-actor row on `stu_1` (later `created_at`) is IGNORED: persisted `negotiation_move` reflects ONLY the `student` row's move; if NO student row exists the move is `None` and the belief is the no-boost posterior | seed one `actor="parser"` row (move `challenge`) + assert no boost / `negotiation_move is None`; separately a student row case | 
| I6 | `test_negotiation_skip_is_noop` | a `skip` student move on `stu_1` → belief BYTE-IDENTICAL to the no-negotiation covered posterior (GV-4); BUT `MasteryEvent.negotiation_move == "skip"` is STILL persisted (the move is recorded for the corpus even though it does not boost) | `_seed_negotiation(entry_id="stu_1", move="skip")`; flag ON |
| I7 | `test_negotiation_no_move_for_entity_identity` | a qualifying move exists for a DIFFERENT node not in this entity's `evidence_node_ids` → this entity's belief is the no-boost posterior and `negotiation_move is None` | `_seed_negotiation(entry_id="other_node", move="challenge")`; flag ON |
| I8 | `test_negotiation_flag_off_byte_identical` | flag OFF (delenv): belief AND `MasteryEvent.negotiation_move` are byte-identical to the WU-5A2/5B1 no-negotiation result even though a `challenge` row is seeded; `negotiation_move is None`; belief == the plain covered posterior | seed `_seed_negotiation(entry_id="stu_1", move="challenge")`; `monkeypatch.delenv("APOLLO_LEARNER_NEGOTIATION_ENABLED", raising=False)`; `run_learner_update` |
| I9 | `test_negotiation_suppressed_then_move_persisted` | the misconception+move case (I2) ALSO persists `MasteryEvent.negotiation_move == "paraphrase"` — suppression mutes the BELIEF multiplier but the move STRING is still recorded (so the corpus can later fit the sign) | reuse I2 setup; flag ON |

(9 integration tests. I2 + I9 together prove suppression mutes the belief but not the provenance; I8 is the byte-identity flag-OFF guard for the new path.)

**Why suppression persists the move string (I9):** the multiplier identity and the persisted `negotiation_move` are DECOUPLED — `negotiation_move_for_entity` returns the representative move unconditionally; only `likelihood_multiplier_for_entity` applies the suppression. This is intentional: the move happened and must be in the refit corpus even when it did not move the belief.

## 7. Byte-identity / non-regression guardrail

**The contract:** with `APOLLO_LEARNER_NEGOTIATION_ENABLED` OFF (the default and the only deployed state), every prior test stays BYTE-IDENTICAL green:
- All 17 `tests/database/test_done_layer3_route_postgres.py` cases — including the WU-5B1 decay test `test_decay_flag_on_decays_base_prior` (which sets the DECAY flag but NOT the negotiation flag) — unchanged.
- `tests/database/test_done_shadow_route_postgres.py` (the byte-identity / me==0 ls==0 guard) unchanged.
- All `apollo/learner_model/tests/` (belief/update/decay) unchanged.

**Why it holds (the identity-default proof):**
- `_combined_belief_update`'s new `likelihood_multiplier` defaults to `NO_OP_LIKELIHOOD` → the only new line is `a*1.0=a` per component → byte-identical product.
- `persist_learner_update`/`_persist_entity_group`'s two new kwargs default to `None`; when `None`, `entity_multiplier = NO_OP_LIKELIHOOD` and `move = None`, and the `dataclasses.replace(spec, negotiation_move=None)` reproduces the spec's existing `None` → byte-identical ORM row.
- `run_learner_update` builds the closures ONLY inside `if _learner_negotiation_enabled():`; flag OFF → both stay `None` → identity path. The PUBLIC signature is unchanged, so `done.py` is untouched.

**STOP condition:** if ANY prior test changes output with the flag OFF, the injection is NOT identity-default → STOP and ESCALATE (do not "fix" the prior test).

**Negotiation flag ON but no rows / no matching nodes** is ALSO byte-identical (I7/I8 prove it): `load_student_moves` returns `{}`, `entity_negotiation_move` returns `None`, multiplier is identity, move override is `None`. So enabling the flag on data that has no student negotiations is a safe no-op.

## 8. Owner-doc updates (`docs/architecture/apollo.md`)

Owner doc owns `apollo/**` (frontmatter `owns:`). Reconcile in the SAME commit; set `last_verified: 2026-06-19` (already 2026-06-19 in the file — keep/confirm).

1. **`apollo/learner_model/` module-map row** (the row listing `belief.py`, `state_model.py`, `update.py`, `persistence.py`, `decay.py`, `__init__.py`): add `negotiation.py` to the file list and append a WU-5B2 paragraph after the WU-5B1 decay paragraph:
   - `negotiation.py` is the PURE §3 negotiation multiplier (`NEGOTIATION_LIKELIHOOD=(1.0,1.2,1.1)` order `(p_misc,p_shaky,p_mastered)`, blank `L_misc`=1.0; `MULTIPLIER_MOVES={challenge,paraphrase}`, `skip`→no-op; `negotiation_multiplier`/`entity_negotiation_move`/`suppresses_hedge` pure + the tiny async `load_student_moves` reading `apollo_kg_negotiations` `actor='student'` latest-wins, NO Neo4j).
   - The RATIFIED suppression (adjudication #4b): a MISCONCEPTION or PARTIAL-with-code entity forces identity ×1.0 — a negotiation move never dilutes a misconception (restores the −0.044 p_misc cancel); the boost is ALLOWED on CORRECTED/COVERED.
   - The additive `persistence.py` seam: `likelihood_multiplier_for_entity` + `negotiation_move_for_entity` identity-default keyword-only callables threaded `persist_learner_update`→`_persist_entity_group`→`_combined_belief_update`; the multiplier folds into `combined_likelihood` ONCE BEFORE damp (so a low-q attempt mutes the metacognitive bump; `q` stays parser·grader — the multiplier is NOT folded into `q`); the `negotiation_move` override is at the ORM-build site via `dataclasses.replace` (frozen-spec-safe), NOT by editing `update.py`/`state_model.py`. Defaults identity → byte-identical to WU-5B1.
   - The `APOLLO_LEARNER_NEGOTIATION_ENABLED` flag in `learner_update.py` (default OFF EVERYWHERE; mirrors `_learner_decay_enabled`): ON → `load_student_moves` once + per-entity closures (suppression applied in the multiplier closure) threaded into `persist_learner_update`; OFF → `None` → byte-identical. `run_learner_update`'s public signature UNCHANGED; `done.py` untouched. No migration (`negotiation_move` + `apollo_kg_negotiations` exist).

2. **`apollo/handlers/` row** (the `learner_update.py` description): append the `_LEARNER_NEGOTIATION_FLAG`/`_learner_negotiation_enabled()` addition mirroring the existing WU-5B1 decay-flag sentence.

3. No interface table / HTTP-route changes (no new route; `run_learner_update` signature unchanged).

4. Optionally note in the negotiation/OLM mention (the `negotiate.py` line) that the §3 multiplier now CONSUMES `apollo_kg_negotiations` at the fold (read-only, student-actor, latest-wins) — distinct from the Done-gate's existing lossy read.

## 9. Coverage plan (>=95% patch, target 100%)

Changed lines = `negotiation.py` (new) + the additive lines in `persistence.py` + `learner_update.py`. Every changed line must be exercised:

- **`negotiation.py` pure fns** — all branches covered by §5: `negotiation_multiplier` (qualifying / skip / None / unknown — tests 3-7), `entity_negotiation_move` (hit / miss / multi-node / deterministic — tests 9-12), `suppresses_hedge` (misconception / partial-with-code / partial-no-code / corrected / covered / any-event — tests 13-18). `load_student_moves` (the async read incl. `actor='student'` filter + latest-wins ORDER BY) — covered by integration I4/I5 (real PG), since it is IO.
- **`persistence.py` additive lines** — the identity-default multiply (flag-OFF byte-identity tests + I1 boost), the `dataclasses.replace` override (I3 move persisted, I8 None when off), the per-entity multiplier/move resolution branch (I1/I2/I6/I7).
- **`learner_update.py` additive lines** — `_learner_negotiation_enabled` true/false (I1 ON, I8 OFF), the closures + `load_student_moves` call (I1-I7), the suppression branch inside the multiplier closure (I2/I9).

**Commands (interpreter `.venv/Scripts/python.exe`):**
```bash
.venv/Scripts/python.exe -m pytest apollo/learner_model/tests/test_negotiation.py -q
.venv/Scripts/python.exe -m pytest tests/database/test_done_layer3_route_postgres.py -q   # Docker UP, GREEN-not-skipped
.venv/Scripts/python.exe -m pytest tests/database -q                                       # full real-infra gate
.venv/Scripts/python.exe -m pytest --cov=apollo.learner_model.negotiation \
    --cov=apollo.learner_model.persistence --cov=apollo.handlers.learner_update \
    --cov-report=xml apollo tests/database
.venv/Scripts/python.exe -m diff_cover.diff_cover_tool coverage.xml \
    --compare-branch=origin/feat/apollo-kg-wu5b1-between-session-decay --fail-under=95
```
A SKIP in `tests/database` (Docker down / testcontainers missing) = STOP. If `python` resolves to anaconda base (no testcontainers), switch to `.venv/Scripts/python.exe` — do NOT escalate for that.

## 10. Risks

| # | Risk | Confidence | Mitigation |
|---|---|---|---|
| R1 | **Identity-default leak** — a non-`None` default or an accidental behavioral change to a frozen body breaks a prior test with the flag OFF. | HIGH if rushed | §7 proof + I8 + the full 17-case sweep. If ANY prior test moves with the flag OFF → STOP/ESCALATE. The only frozen-body line changes are an identity multiply (`a*1.0`) and a `replace(...,None)` no-op. |
| R2 | **`entry_id` ↔ `evidence_node_ids` keyspace mismatch in fixtures** — if the seeded `entry_id` does not equal the finding's `student_node_ids`, the bridge silently misses and tests give false greens. | MEDIUM | Tests assert BOTH the boost AND its distinctness (I1) and the persisted move (I3) — a missed bridge fails the boost/move assertion, not just silently passes. The seed helper uses the SAME literals (`stu_1`/`stu_m`) the existing finding helpers use. |
| R3 | **Suppression vs persistence coupling** — implementer ties the move-persist to the suppression and drops the move string on misconception entities. | MEDIUM | I9 pins: suppressed belief BUT persisted `negotiation_move`. The two closures are deliberately separate (§3.3). |
| R4 | **`load_student_moves` latest-wins relies on `created_at` ordering** — equal `created_at` (same-instant default `now()`) makes the winner nondeterministic. | LOW | Order by `(created_at ASC, id ASC)` so the higher `id` (later insert) wins on a tie; I4 seeds explicit ascending `created_at` to pin the latest. |
| R5 | **Misconception-cancel is a spec OVERRIDE, not the literal §3 rule** — the literal "multiply per evidence item" would dilute p_misc by −0.044. | RATIFIED (adjudication #4b) | This unit IMPLEMENTS the suppression (it is NOT optional). The persisted `negotiation_move` lets a later refit re-fit the sign if the override is ever revisited. Documented in apollo.md. |
| R6 | **REAL[] single-precision tolerance** — Postgres `REAL[]` stores single-precision; exact-equality assertions on belief floats flake. | LOW | Reuse the existing `_belief_close` (rel 1e-5 / abs 1e-6); the pure tests use `pytest.approx(abs=5e-5)` on the 4-dp golden constants. |
| R7 | **Lock duration** — none. The fold already holds `SELECT ... FOR UPDATE` on the learner-state rows (`persistence.py:172-191`); `load_student_moves` is a plain indexed read on `apollo_kg_negotiations (attempt_id, entry_id)` (021 index), single attempt, sub-ms. No new lock, no large-table scan. |

## 11. Out-of-scope boundaries (do NOT build here)

- **The §10 chat-keyword persist (WU-5B4)** — persisting `extract_and_filter_keywords` output as JSONB on chat turns on the `/ask` path. This is the LITERAL §10/§12-phase-5 "RQ5 hedge (persist chat keywords)" and is a SEPARATE later unit. WU-5B2 is the §3 *fold-side* multiplier ONLY.
- **The retry janitor (WU-5B3)** — no worker loop, no `learner_update_pending` drain, no migration 028.
- **Any change to the §6.5 converter, the resolver, the grading core, `done.py`, or the OLM `negotiate.py` write path.** WU-5B2 only READS `apollo_kg_negotiations`.
- **No new `negotiation_move` field on the frozen `LearnerEvent`** — the move is threaded as an injected map; the persisted column is overridden at the ORM-build site only.
- **No new migration** — `negotiation_move` (026) and `apollo_kg_negotiations` (021) already exist; verified §1.
- **No `done.py` edit** — the flag is read inside `learner_update.py`; `run_learner_update`'s signature is unchanged.
- **No fitting/calibration of the multiplier magnitude** — `(1.0,1.2,1.1)` is LOCKED for v1; the refit corpus (persisted `negotiation_move` + the event log) is the later EM/sign-fit hook, not this unit.
- **No remote DB writes of any kind** (test or prod). Migration files are not even produced here.

## 12. Deploy handoff (HUMAN/CI only — never executed by feller agents)

- No migration to deploy (no DDL produced). Nothing for a human to apply to test or prod Supabase.
- The feature ships DORMANT: `APOLLO_LEARNER_NEGOTIATION_ENABLED` is unset (OFF) in every environment. Enabling it is a deliberate post-calibration decision, gated like the LAYER3 flag — set the env var on the Railway service ONLY after the §6.7 calibration window blesses the multiplier sign/magnitude. Until then the column stays `NULL`-persisting (move recorded only when the flag is on).
- Standard CI runs the patch-coverage gate + the real-infra `tests/database` gate on the PR into `staging`; promotion to `ApolloV3` is the separate `staging → ApolloV3` PR. Agents do not push or open PRs (per the binding constraints).
