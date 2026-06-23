# Plan: WU-3B2h — Per-problem anomaly quarantine + shadow verification (CLOSES the Apollo KG spec)

> **AS-BUILT CORRECTION (supersedes every `entity_id` reference below).** During
> execution the per-node aggregation key was changed from `findings.entity_id` to
> `findings.reference_node_ids`. The production grading core hardcodes
> `entity_id = None` (`apollo/grading/persistence.py:139`) and carries the missing
> reference node's identity in the `reference_node_ids` JSONB list
> (`apollo/graph_compare/findings.py` `missing_finding`); keying on the always-NULL
> `entity_id` would have made the quarantine sweep permanently INERT against real
> data. The shipped `quarantine.py` keys on `_node_key(reference_node_ids)` and is
> proven end-to-end against a real production finding by
> `test_shadow_findings_feed_sweep_end_to_end`. The owner doc `docs/architecture/apollo.md`
> is authoritative and already reflects this; do NOT re-introduce `entity_id` keying.

**Goal:** Add the §8B.3 per-problem anomaly-quarantine backstop (pure statistic + real-PG findings→runs→attempts sweep that writes `quarantined_at`, reversibly) and the §9 SPEC-4 shadow-gate verification (SHADOW ON + LAYER3 OFF → auto-provisioned Tier-2 problem is teachable, produces findings, moves ZERO Layer-3 beliefs); extend the sole Tier-2 selection chokepoint with `quarantined_at IS NULL` so a quarantined problem is no longer selectable.
**Architecture:** `apollo/provisioning/` (new `quarantine.py` + `quarantine_constants.py`), one ~1-line edit to `apollo/overseer/problem_selector.py`; one pure test file in `apollo/provisioning/tests/` and one real-PG test file in `tests/database/`.
**Tech stack:** Python 3.12, FastAPI, SQLAlchemy async + asyncpg, pytest + pytest-asyncio, Testcontainers pgvector:pg16. **stdlib `statistics` ONLY** (ADJ #8: NO numpy import needed for v1, NO scipy — point-biserial is v1.1).

---
provides:
  - apollo.provisioning.quarantine_constants — N_MIN / THETA_MISS / CONCENTRATION_MARGIN (env-overridable, calibration-tunable)
  - apollo.provisioning.quarantine.quarantine_decision(coverage_matrix, *, n_min, theta_miss, margin) -> QuarantineVerdict (PURE)
  - apollo.provisioning.quarantine.QuarantineVerdict (frozen dataclass)
  - apollo.provisioning.quarantine.sweep_quarantine(db, *, search_space_id=None) -> SweepReport (async, real-PG; reversible; observability log per fire/clear)
  - apollo.overseer.problem_selector.list_problems_for_concept — now also filters quarantined_at IS NULL (gates BOTH select_problem AND select_problem_personalized)
consumes:
  - migration 030 columns: apollo_concept_problems.quarantined_at, .tier, .search_space_id (already shipped by WU-3B2a; NO new migration)
  - apollo_graph_comparison_findings (finding_kind='missing_node', run_id, entity_id) — populated by the §6 SHADOW run
  - apollo_graph_comparison_runs (id, attempt_id, search_space_id)
  - apollo_problem_attempts (id, problem_id TEXT == problem_code)
  - apollo_concept_problems (id BIGINT, concept_id, problem_code, tier, quarantined_at)
  - apollo.handlers.done.handle_done + flags APOLLO_GRAPH_SIM_SHADOW_ENABLED / APOLLO_GRAPH_SIM_LAYER3_ENABLED (frozen; shadow-verification only DRIVES them, never edits)
depends_on:
  - WU-3B2g orchestrator (branch feat/apollo-kg-wu3b2g-orchestrator — the diff-cover compare branch); the full pipeline must exist so an auto-provisioned Tier-2 problem is teachable end-to-end
  - WU-3B2a migration 030 (quarantined_at column already present + ORM mapped) + the tier==2 selector predicate already in list_problems_for_concept
---

## Overview

This is the LAST sub-unit of WU-3B2 and the LAST unit of the whole Apollo KG learner-model build — when it lands the spec is built. It is a runtime BACKSTOP, not a new pipeline stage. Two independent deliverables share one branch:

1. **Per-problem anomaly quarantine (§8B.3 / §8B.7).** Once auto-provisioned Tier-2 problems are taught in shadow, the §6 grading core writes `apollo_graph_comparison_findings` rows. If, for one problem P, the class-wide coverage concentrates "missing" on a single reference node n (most students miss the SAME node), that is the signature of a wrong/mispaired reference solution — the automated replacement for a teacher noticing a bad problem. The quarantine pulls P from the selectable pool by stamping `apollo_concept_problems.quarantined_at`. It is **per-problem** (one bad reference cannot hide in a course-wide average) and **reversible** (re-cleared as N grows and the concentration no longer fires). Split into a PURE statistic over a hand-buildable coverage matrix and an async real-PG SWEEP that does the aggregation + the write.

2. **Shadow verification (§9 SPEC-4 — the spec-closing invariant).** A real-PG test proving that with `APOLLO_GRAPH_SIM_SHADOW_ENABLED` ON + `APOLLO_GRAPH_SIM_LAYER3_ENABLED` OFF, an auto-provisioned Tier-2 problem (a) IS teachable, (b) produces `apollo_graph_comparison_findings` (so quarantine has data), AND (c) moves ZERO Layer-3 beliefs (`apollo_mastery_events` + `apollo_learner_state` UNCHANGED). This proves quarantine has a data source without any belief movement — the headline safety posture of the whole §8B auto-provisioning design.

The selector edit (`quarantined_at IS NULL`) threads quarantine into the SOLE selection chokepoint `list_problems_for_concept`, which both `select_problem` and `select_problem_personalized` funnel through (verified `problem_selector.py:34-58` + `:73` + `:113`), so a quarantined problem is no longer selectable with NO signature change and NO separate selector edit.

**KNOWN LIMIT (§9 OPS-4, document honestly — do NOT over-claim).** The simple miss-concentration rule WILL false-quarantine a genuinely-hard prerequisite node (legitimately missed by most students) — it has the SAME signature as a mispaired solution. v1 does NOT distinguish them. The three v1 mitigations are: (a) **reversibility** (re-clear as N grows); (b) a **per-fire observability log row** so a human can audit false positives; (c) the **calibration step** (ADJ #12) that must precede flipping `APOLLO_AUTOPROVISION_ENABLED` treats v1 quarantine as ADVISORY. The point-biserial discrimination refinement (a hard node has POSITIVE point-biserial; a mispaired reference near-zero/negative) is **v1.1** (ADJ #1 / ADJ #8) — do NOT add scipy, do NOT build it now.

**RECON: every dependency this unit needs already exists.** Migration 030 already shipped `quarantined_at TIMESTAMPTZ` on `apollo_concept_problems` (verified `models.py:183`), the `tier == 2` predicate already lives in `list_problems_for_concept` (verified `problem_selector.py:50`, with the explicit in-code note "The `quarantined_at IS NULL` clause is added by WU-3B2h"), and the `apollo/provisioning/` package + the full 3B2b-3B2g pipeline are present. So this unit adds NO migration, NO new package, and only the two new files + one ~1-line predicate.

## Prior art (sibling modules)

- **Constants convention to mirror:** `apollo/provisioning/cost_constants.py:1-58` and `apollo/provisioning/dedup_constants.py:1-32` — module docstring explaining each constant, `from __future__ import annotations`, `import os`, `float(os.getenv("APOLLO_…", "<default>"))` env-overridable pattern with committed defaults pinned by tests, NO logic beyond arithmetic, NO imports beyond stdlib. `test_cost_constants.py:69-82` shows the `importlib.reload` + `monkeypatch.setenv`/`delenv` env-override test idiom (restore defaults in a `finally`).
- **Frozen-DTO + async-DB-write convention:** `apollo/provisioning/queue.py:49-176` — `@dataclass(frozen=True)` DTO (`ClaimedJob`), `_now() -> datetime` returning `datetime.now(UTC)` (so lease/timestamp arithmetic is test-controllable, NOT SQL `now()`), structured `_LOG.info("event_name", extra={"event": …})` observability rows, `_MAX_ERROR_LEN` defensive cap, explicit `__all__`. Mirror this for `QuarantineVerdict`/`SweepReport` + `_now()` + the per-fire log.
- **Real-PG savepoint harness (shadow verification + sweep):** `tests/database/test_done_shadow_route_postgres.py:1-204` — the `db_session` fixture (real pgvector, `join_transaction_mode="create_savepoint"`, rolled back per test; `tests/conftest.py:163`), `seed_course(db, *, subject_slug, concept_slug, problems)` returning `(sid, cid, codes)` from `apollo/subjects/tests/_curriculum_fixtures.py:166-188`, the `_neo_stubs` Neo4j-boundary patches, the `monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")` driver, and the EXACT belief-movement assertion `me == 0 and ls == 0` (`:156-166`) this unit re-uses verbatim for the shadow-verification.
- **Committed-engine + migration-chain harness (only if the sweep needs the migration-only partial-unique-index — it does NOT):** `tests/database/test_apollo_provisioning_orchestration.py:1-191` shows the heavier committed pattern. The sweep touches ONLY ORM-mapped tables that `create_all` builds, so the lighter savepoint `db_session` fixture suffices — do NOT pay for the committed-engine harness.
- **The pinned join surface:** `apollo/persistence/models.py` — `GraphComparisonFinding` (`:564-592`: `run_id` FK→runs, `entity_id` FK→kg_entities nullable, `finding_kind` TEXT, valid value `'missing_node'` ∈ `FINDING_KINDS` `:66-75`), `GraphComparisonRun` (`:516-561`: `attempt_id` FK→attempts, `search_space_id`), `ProblemAttempt` (`:285-318`: `problem_id` TEXT == `problem_code`, NOT the BIGINT id), `ConceptProblem` (`:160-189`: `id` BIGINT, `concept_id`, `problem_code` TEXT, `tier`, `quarantined_at`).

## Out-of-scope boundaries (held firmly)

- **NO migration.** `quarantined_at` already exists (migration 030, `models.py:183`). This unit writes/clears it; it does not add or alter any column. NEVER apply a migration to any remote DB.
- **NO new package.** stdlib `statistics` only (mean of per-node miss rates). NO numpy import, NO scipy. If the executor reaches for a package: BLOCK + escalate (ADJ #8).
- **NO point-biserial / discrimination refinement.** v1.1 (ADJ #1). v1 ships ONLY the three-clause miss-concentration rule.
- **NO change to `done.py`, the §6 grading core (`apollo/graph_compare/**`), or `done_grading.py`/`done_inputs.py`.** The shadow-verification READS the findings the existing shadow path already populates; it patches the Neo4j/LLM boundaries exactly as `test_done_shadow_route_postgres.py` does, but edits ZERO production code there.
- **NO flag flip.** `APOLLO_AUTOPROVISION_ENABLED` and `APOLLO_GRAPH_SIM_LAYER3_ENABLED` stay OFF. The shadow-verification SETS env vars inside the test only.
- **NO scheduling / worker wiring for the sweep.** `sweep_quarantine` is an importable async function with a clean signature; whether it runs on the nightly janitor schedule or a Tier-2 harness is a 3B2g/ops concern already scaffolded there. This unit ships the function + its tests, not a Procfile line.
- **NO teacher-UI surfacing** of quarantine status. Backend-only.
- **NO new exception hierarchy.** The sweep is best-effort observability infra; it logs and returns a report, it does not raise domain exceptions to callers (see Error contract).

## Layered tasks (TDD-ordered, RED first)

Each numbered task writes its REAL test(s) FIRST (RED), then the minimal implementation (GREEN). No skip-marks, no xfail, no assert-nothing. Build order is forced by the dependency DAG: constants → pure statistic → sweep → selector → shadow.

### 1. DB migration
- [ ] **NONE.** Justification: `quarantined_at TIMESTAMPTZ` already exists on `apollo_concept_problems` (migration 030, WU-3B2a; ORM `models.py:183`). This unit consumes it. Explicitly out of scope per the unit contract.
- Verify: `Grep "quarantined_at" database/migrations/030_apollo_autoprovisioning.sql` returns the column (it does).

### 2. Constants — `apollo/provisioning/quarantine_constants.py` (NEW)
- [ ] **RED:** write `apollo/provisioning/tests/test_quarantine_constants.py` (see Layer 1 list) pinning the three committed defaults + the env-override reload path.
- [ ] **GREEN:** create the module. Mirror `cost_constants.py`/`dedup_constants.py` EXACTLY: `from __future__ import annotations`, `import os`, no other imports, no logic. Three constants:
  - `N_MIN: int = int(os.getenv("APOLLO_QUARANTINE_N_MIN", "8"))` — minimum graded attempts of P in a course before the rule can fire.
  - `THETA_MISS: float = float(os.getenv("APOLLO_QUARANTINE_THETA_MISS", "0.80"))` — a single node's miss rate must reach this.
  - `CONCENTRATION_MARGIN: float = float(os.getenv("APOLLO_QUARANTINE_CONCENTRATION_MARGIN", "0.40"))` — that node's miss rate must exceed the mean per-node miss rate by at least this (the "concentrated, not uniformly hard" test).
  - Module docstring: each constant's meaning + "hand-set v1 (ADJ #1); env-overridable for calibration; the committed defaults are pinned by tests; point-biserial discrimination is v1.1 — NOT built here; stdlib `statistics` only, NO scipy/numpy."
- Verify: `pytest apollo/provisioning/tests/test_quarantine_constants.py -q`

### 3. Pure statistic — `apollo/provisioning/quarantine.py` :: `quarantine_decision` + `QuarantineVerdict` (NEW)
- [ ] **RED:** write the fixture-only tests (Layer 1 list) FIRST. The §8B.7 Tier-3 "mispaired fixture" becomes a deterministic Tier-1 unit test: a hand-built `coverage_matrix` where one node is missed by ≥80% of N≥8 attempts and the others rarely → FIRES; and the three mutation-proof fixtures (below N_MIN, below THETA_MISS, below CONCENTRATION_MARGIN) that do NOT fire.
- [ ] **GREEN:** implement the PURE function (no DB, no IO, no LLM). Signature + contract pinned in "Public signatures". Input is a `CoverageMatrix` (a typed alias — `Mapping[str, Sequence[bool]]` keyed by reference-node key → per-attempt "missing?" booleans, all sequences the same length N, OR an explicit small frozen dataclass; the executor MAY pick either, see Deviations). Algorithm:
  1. `N = number of graded attempts`. If `N < n_min` → `QuarantineVerdict(quarantine=False, reason="below_n_min", ...)`.
  2. For each node `n`: `m(n) = count(missing for n) / N`.
  3. `mean_m = statistics.fmean(m.values())` (stdlib).
  4. Find the node with the max `m(n)`. Quarantine iff `m(top) >= theta_miss AND (m(top) - mean_m) >= margin`. The conjunction is THREE clauses (`N>=n_min`, `m(top)>=theta_miss`, concentration `>=margin`) — each independently mutation-proven by a discriminating fixture.
  5. Return a frozen `QuarantineVerdict` carrying `quarantine: bool`, `n_attempts: int`, `top_node_key: str | None`, `top_miss_rate: float`, `mean_miss_rate: float`, `concentration: float`, `reason: str` (one of `"fired"|"below_n_min"|"below_theta"|"below_margin"|"empty"`). Immutable — return a NEW object, never mutate the input matrix.
  6. Empty matrix / zero nodes → `QuarantineVerdict(quarantine=False, reason="empty", ...)`. NEVER divide by zero.
- Verify: `pytest apollo/provisioning/tests/test_quarantine.py -q`

### 4. Real-PG sweep — `apollo/provisioning/quarantine.py` :: `sweep_quarantine` + `SweepReport` (NEW, same file)
- [ ] **RED:** write the real-PG sweep tests (Layer 2 list) FIRST against the `db_session` savepoint fixture. Seed findings→runs→attempts→concept_problems via `seed_course` + direct ORM inserts; assert the RIGHT BIGINT row gets `quarantined_at` set, the re-clear/reversibility path, and the mutation proofs.
- [ ] **GREEN:** implement the async sweep. It:
  1. Runs ONE aggregation query (the pinned §9 OPS-3 join — see "The pinned aggregation join") grouped by `(attempts.problem_id, runs.search_space_id, findings.entity_id)` producing per-(problem_code, course, node) miss counts, plus a per-(problem_code, course) total graded-attempt count N. (N = COUNT(DISTINCT runs.attempt_id) per problem in the course — every graded attempt yields one run; a node absent from the findings for an attempt is NOT a miss, so per-node miss count comes from rows WHERE `finding_kind='missing_node'`.)
  2. Optionally scoped by `search_space_id` (param; `None` = all courses).
  3. For each `(problem_code, search_space_id)` group, builds the in-memory `coverage_matrix` and calls the PURE `quarantine_decision(...)` with the three constants. (Pure core stays DB-free; the sweep is the only DB-aware layer.)
  4. Resolves `problem_code` (TEXT) → the BIGINT `apollo_concept_problems` row via `(concept_id, problem_code == problem_id)` course-scoped — see the namespace contract (ADJ #6). To get `concept_id`, the sweep joins `apollo_concept_problems` on `(search_space_id, problem_code)` (the denormalized `search_space_id` from migration 030 makes this course-scoped without re-walking subjects).
  5. On FIRE for a currently-live row (`quarantined_at IS NULL`): set `quarantined_at = _now()`, emit `_LOG.info("quarantine_fire", extra={"event":"quarantine_fire", "problem_code":…, "search_space_id":…, "node_key":…, "n_attempts":…, "top_miss_rate":…, "concentration":…})` (the per-fire audit row — ADJ #1 mitigation b).
  6. On NOT-FIRE for a currently-quarantined row (`quarantined_at IS NOT NULL`): set `quarantined_at = None` (RE-CLEAR — reversibility), emit `_LOG.info("quarantine_clear", extra={"event":"quarantine_clear", "problem_code":…, "reason":…})`.
  7. Commits once, returns a frozen `SweepReport(n_evaluated, n_quarantined, n_cleared, fired_problem_codes: tuple[str, ...], cleared_problem_codes: tuple[str, ...])`.
  8. Immutable style: builds new dicts/tuples; mutates only the SQLAlchemy ORM rows it is persisting (the one allowed in-place — that IS the write).
- Verify: `pytest tests/database/test_apollo_quarantine_shadow.py -q -k sweep` (green, NOT skipped, Docker up)

### 5. Selector predicate edit — `apollo/overseer/problem_selector.py` (EDIT, ~1 line)
- [ ] **RED:** write the selector-exclusion test (Layer 2 list) FIRST: a quarantined Tier-2 problem is EXCLUDED by `list_problems_for_concept`; a live Tier-2 problem is included. MUTATION-PROVE: dropping the new predicate makes the quarantined problem selectable → the test REDs.
- [ ] **GREEN:** add `ConceptProblem.quarantined_at.is_(None)` to the existing `.where(...)` in `list_problems_for_concept` (`:48-51`), alongside the existing `ConceptProblem.concept_id == concept_id` and `ConceptProblem.tier == 2`. Update the docstring `:39-44` — the in-code note already says "The `quarantined_at IS NULL` clause is added by WU-3B2h"; replace that note with the now-present statement. NO signature change; gates BOTH `select_problem` and `select_problem_personalized` (the sole chokepoint, ADJ #9).
- Verify: `pytest apollo/overseer/tests/test_problem_selector.py -q && pytest tests/database/test_apollo_quarantine_shadow.py -q -k selector`

### 6. Shadow verification — `tests/database/test_apollo_quarantine_shadow.py` (real-PG)
- [ ] **RED + GREEN together (this is a VERIFICATION test of already-shipped behavior, not new production code):** write the §9 SPEC-4 test (Layer 3) asserting the FULL conjunction. It drives `handle_done` with `APOLLO_GRAPH_SIM_SHADOW_ENABLED=true` and `APOLLO_GRAPH_SIM_LAYER3_ENABLED` UNSET/false on an auto-provisioned-shaped Tier-2 problem and asserts: (a) the done call returns a teachable grade, (b) `apollo_graph_comparison_findings` rows exist for the attempt (quarantine has data — at least one row; ideally a `missing_node` row), (c) `apollo_mastery_events` count == 0 AND `apollo_learner_state` count == 0 for the user (ZERO belief movement). Mirror `test_done_shadow_route_postgres.py:_neo_stubs` + `_run_done` + the `me==0/ls==0` assertion verbatim; the ONLY new wrinkle is asserting the findings exist (so the link to quarantine's data source is proven in the SAME test).
- Verify: `pytest tests/database/test_apollo_quarantine_shadow.py -q -k shadow` (green, NOT skipped, Docker up)

## The three test layers (full test list)

Every test below is REAL — no skip-marks, no xfail, no assert-nothing. The pure tests need no container; the real-PG tests are GREEN-NOT-SKIPPED on Testcontainers pgvector:pg16 with Docker up via `.venv/Scripts/python.exe` (a SKIP is a contract FAIL).

### Layer 0 — constants (apollo/provisioning/tests/test_quarantine_constants.py)
Mirrors `test_cost_constants.py`. Pure, no network, no DB.
- `test_n_min_pinned` — `quarantine_constants.N_MIN == 8`.
- `test_theta_miss_pinned` — `quarantine_constants.THETA_MISS == 0.80`.
- `test_concentration_margin_pinned` — `quarantine_constants.CONCENTRATION_MARGIN == 0.40`.
- `test_env_override_reimport` — `monkeypatch.setenv` the three `APOLLO_QUARANTINE_*` vars, `importlib.reload(quarantine_constants)`, assert overrides applied; restore defaults via `delenv` + reload in a `finally` (the exact `test_cost_constants.py:69-82` idiom). External deps: none (env only).

### Layer 1 — PURE statistic (apollo/provisioning/tests/test_quarantine.py)
Fixture-only. Hand-built `coverage_matrix` dicts; NO LLM, NO DB, NO network. Each fixture is deterministic. The four conjunction-discriminating tests each mutation-prove one clause (loosen/drop the clause → the discriminating fixture flips).
- `test_fires_on_concentrated_miss` — the §8B.7 mispaired fixture: N=10 attempts, node `n_bad` missing in 9 (m=0.9 ≥ 0.80), other 3 nodes missing in 0–1 (mean across all ≈ 0.23, concentration ≈ 0.67 ≥ 0.40) → `verdict.quarantine is True`, `verdict.top_node_key == "n_bad"`, `verdict.reason == "fired"`. **Asserts the positive path + that the right node is named in the audit.**
- `test_no_fire_below_n_min` (MUTATION-PROOF clause 1) — the SAME concentrated shape but N=7 (< N_MIN=8) → `quarantine is False`, `reason == "below_n_min"`. Discriminating: drop the `N >= n_min` clause and this fixture would fire.
- `test_no_fire_below_theta_miss` (MUTATION-PROOF clause 2) — N=10, the worst node missed in 7/10 (m=0.70 < 0.80) but still concentrated (others ≈ 0.05, concentration 0.61 ≥ 0.40) → `quarantine is False`, `reason == "below_theta"`. Discriminating: loosen THETA to 0.70 and this fires.
- `test_no_fire_below_concentration_margin` (MUTATION-PROOF clause 3) — "uniformly hard problem" fixture: N=10, EVERY node missed ~8/10 (each m≈0.80 ≥ THETA) but the top exceeds the mean by < 0.40 (concentration ≈ 0.05) → `quarantine is False`, `reason == "below_margin"`. **This is the §9 OPS-4 false-positive boundary — a genuinely-hard-but-uniform problem must NOT quarantine.** Discriminating: drop the margin clause and this fires.
- `test_empty_matrix_does_not_fire_or_divide_by_zero` — `{}` and a matrix with zero-length sequences → `quarantine is False`, `reason == "empty"`, no `ZeroDivisionError`.
- `test_verdict_is_frozen_and_carries_audit_fields` — `QuarantineVerdict` is a frozen dataclass; assert `n_attempts`, `top_node_key`, `top_miss_rate`, `mean_miss_rate`, `concentration` are populated on a fired verdict (these are what the sweep's audit log emits). Attempting to set an attribute raises `FrozenInstanceError`.
- `test_input_matrix_not_mutated` — pass a matrix, call `quarantine_decision`, assert the input dict is byte-identical afterward (immutability contract).
- `test_constants_are_the_call_defaults_via_sweep_path` — assert the sweep passes `N_MIN`/`THETA_MISS`/`CONCENTRATION_MARGIN` from the constants module (covered indirectly in Layer 2; here a light unit assertion that calling with the module constants reproduces the fired verdict). External deps: none.

### Layer 2 — real-PG sweep + selector (tests/database/test_apollo_quarantine_shadow.py)
`pytestmark = pytest.mark.integration`. Uses the savepoint `db_session` fixture (real pgvector, `create_all`, rolled back per test) + `seed_course` + direct ORM inserts of `ProblemAttempt`/`GraphComparisonRun`/`GraphComparisonFinding`. NO committed-engine harness needed (all tables are ORM-mapped; the join is plain SELECT). The seed helper builds, for one course + concept: M Tier-2 problems, and for the target problem P, N graded attempts each with a `GraphComparisonRun` and `missing_node` findings concentrated on one entity. External deps: none beyond the container — no LLM, no Neo4j (the sweep never touches them).
- `test_sweep_quarantines_concentrated_problem_on_right_bigint_row` — seed P with N=10 attempts, 9 missing the SAME `entity_id`; run `sweep_quarantine(db_session, search_space_id=sid)`; assert the BIGINT `apollo_concept_problems` row for `(concept_id, problem_code==P)` has `quarantined_at IS NOT NULL`, and `report.n_quarantined == 1` with `P in report.fired_problem_codes`. **Proves the TEXT→BIGINT resolution writes the right row (§9 OPS-3).**
- `test_sweep_does_not_quarantine_uniform_or_sparse_problem` — seed a second problem Q with N=10 but uniform misses (concentration < margin) and a third R with N=4 (< N_MIN); run the sweep; assert Q and R rows stay `quarantined_at IS NULL`, `report.n_quarantined == 1` (only P).
- `test_sweep_is_course_scoped_by_run_search_space_id` — seed the SAME `problem_code` string under TWO different `search_space_id`s, concentrated-missing in course A only; run `sweep_quarantine(db, search_space_id=A)`; assert only course A's BIGINT row is quarantined, course B's untouched. **Proves `runs.search_space_id` course scoping + the `(search_space_id, problem_code)` resolution (no cross-course leak).**
- `test_sweep_reclears_when_concentration_no_longer_fires` (REVERSIBILITY) — seed P already quarantined (`quarantined_at` set) but the CURRENT findings no longer concentrate (N grew, misses now uniform/below threshold); run the sweep; assert P's `quarantined_at` is back to NULL and `P in report.cleared_problem_codes`, `report.n_cleared == 1`. **Proves the reversible re-clear path (ADJ #1 mitigation a).**
- `test_sweep_missing_node_filter_is_load_bearing` (MUTATION-PROOF the join filter) — seed P with N=10 attempts where the concentrated node has `finding_kind='covered_node'` (NOT `missing_node`); run the sweep; assert P is NOT quarantined. Discriminating: dropping the `WHERE finding_kind='missing_node'` filter would count covered nodes as misses → P would wrongly fire → REDs.
- `test_sweep_emits_audit_log_on_fire` (caplog) — capture logs; assert exactly one `event=quarantine_fire` record with `problem_code`, `search_space_id`, `node_key`, `n_attempts`, `top_miss_rate`, `concentration` in `extra`. **Proves the per-fire observability row exists for human audit.**
- `test_sweep_emits_clear_log_on_reclear` (caplog) — the reclear path emits exactly one `event=quarantine_clear`.
- `test_sweep_idempotent_second_run_no_change` — run the sweep twice on the fired state; second run reports `n_quarantined == 0` (already quarantined, no re-stamp), the row's `quarantined_at` unchanged. **No double-stamp; idempotent.**
- `test_quarantined_tier2_problem_excluded_by_selector` (SELECTOR) — seed two Tier-2 problems on one concept, quarantine one (`quarantined_at` set directly); call `list_problems_for_concept(db, concept_id=cid)`; assert ONLY the live problem returns. **MUTATION-PROOF:** the test docstring records that removing the `quarantined_at.is_(None)` predicate makes the quarantined problem selectable → this test REDs (the executor must verify by neutering locally, then restore).
- `test_live_tier2_problem_still_selectable` — a non-quarantined Tier-2 problem is still returned (non-regression: the predicate didn't over-filter).

### Layer 3 — THE shadow verification (tests/database/test_apollo_quarantine_shadow.py)
The §9 SPEC-4 spec-closing invariant, in the SAME file (one more `integration` test). Mirrors `test_done_shadow_route_postgres.py` harness: `db_session`, `seed_course` (bernoulli intro payloads via `load_bernoulli_problem_payloads`), `_seed_session` building a `ProblemAttempt` + one student `Message`, the `_neo_stubs` Neo4j/LLM boundary patches (`KGStore.read_graph/freeze/stamp_graded_at/read_node_graded_at`, `done_grading.write_resolution`, `done_turn_order...read_node_created_at`, `main_chat_adjudicator`/`main_chat_auditor` deterministic stubs), and the `monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")` driver. The "auto-provisioned Tier-2 problem" is represented by a Tier-2 `ConceptProblem` payload that satisfies `Problem` + `validate_reference_graph` (the bernoulli intro payload IS such a problem — an auto-provisioned one is byte-identical in shape, so the harness payload is faithful; documented in the test docstring). External deps mocked: Neo4j boundary (AsyncMock stubs), LLM adjudicator/auditor (deterministic dict stubs). NO live API, NO real Neo4j.
- `test_shadow_on_layer3_off_teachable_produces_findings_zero_belief_movement` — the FULL conjunction in one test:
  - (a) `out = await handle_done(...)` returns a dict with `rubric`/`coverage`/`xp_earned` (teachable; OLD-path student-facing grade present), and the attempt row ends `result == "graded"`, `learner_update_pending is False`.
  - (b) `select(func.count()).select_from(GraphComparisonFinding).join(GraphComparisonRun, GraphComparisonFinding.run_id == GraphComparisonRun.id).where(GraphComparisonRun.attempt_id == attempt.id)` ≥ 1 (findings exist → **quarantine has its data source**). Also assert a `GraphComparisonRun` row exists for the attempt (the shadow persist fired).
  - (c) with `APOLLO_GRAPH_SIM_LAYER3_ENABLED` UNSET (delenv to be explicit), `MasteryEvent` count == 0 AND `LearnerState` count == 0 for `TEST_USER_ID` → **ZERO belief movement**. This is the verbatim `me==0/ls==0` assertion from `test_done_shadow_route_postgres.py:156-166`.
  - The test asserts the conjunction with all three parts — a failure of ANY part fails the test. This is the invariant that CLOSES the spec.
- `test_shadow_findings_feed_sweep_end_to_end` (the bridge, optional-but-recommended) — after the shadow `handle_done` above produces real findings, seed enough additional concentrated attempts (direct ORM, same `problem_code`) to cross N_MIN with a concentrated miss, then run `sweep_quarantine` and assert the problem quarantines. **Proves the two deliverables compose: shadow findings → quarantine data → quarantine fire.** External deps: same shadow stubs for the one real done call; the rest is ORM seed. If the executor finds this end-to-end seed too heavy, it MAY split the bridge into a pure-ORM Layer-2 test that hand-writes findings (already covered by `test_sweep_quarantines_...`) — the bridge is belt-and-suspenders, not load-bearing for coverage.

## The pinned aggregation join (§9 OPS-3)

The TEXT-vs-BIGINT namespace is the trap. `ProblemAttempt.problem_id` is **TEXT** == `ConceptProblem.problem_code` (verified `models.py:290`), NOT the BIGINT `ConceptProblem.id`. The quarantine WRITE targets `apollo_concept_problems.quarantined_at` on the **BIGINT** `id` row. So the resolution is: findings → runs → attempts (yields `problem_id` TEXT + `search_space_id`) → `(search_space_id, problem_code)` → the BIGINT `apollo_concept_problems` row.

Conceptual SQL the sweep issues (executor expresses via SQLAlchemy `select` against the ORM; this is the contract, not literal code):

```sql
-- Per-(problem, course, node) miss counts + per-(problem, course) attempt total.
SELECT
  a.problem_id                          AS problem_code,   -- TEXT
  r.search_space_id                     AS search_space_id,
  f.entity_id                           AS node_key,       -- BIGINT entity id (the per-node key)
  count(*) FILTER (WHERE f.id IS NOT NULL) AS miss_count,  -- rows are already missing_node-filtered
  -- N is computed separately as COUNT(DISTINCT r.attempt_id) per (problem_code, search_space_id)
  ...
FROM apollo_graph_comparison_findings f
JOIN apollo_graph_comparison_runs     r ON f.run_id = r.id
JOIN apollo_problem_attempts          a ON r.attempt_id = a.id
WHERE f.finding_kind = 'missing_node'           -- LOAD-BEARING filter (mutation-proven)
  AND (:search_space_id IS NULL OR r.search_space_id = :search_space_id)
GROUP BY a.problem_id, r.search_space_id, f.entity_id;
```

Notes binding the executor:
- **N (the denominator)** = number of GRADED attempts of P in the course = `COUNT(DISTINCT r.attempt_id)` per `(problem_code, search_space_id)`, computed from the runs (one run per graded attempt). A reference node absent from an attempt's findings is NOT a miss for that attempt — only an explicit `missing_node` finding counts. So `m(n) = miss_count(n) / N`. The executor MAY compute N with a second grouped query over runs⋈attempts, or a window/subquery — either is fine; the value must be COUNT-DISTINCT of attempts, not COUNT of findings.
- **The per-node key** is `findings.entity_id` (BIGINT, nullable — a finding whose entity was pruned is `SET NULL`, `models.py:578-582`). A NULL `entity_id` cannot be a stable per-node key; the sweep MUST exclude `entity_id IS NULL` from the per-node aggregation (those findings carry no resolvable node identity) — document this as an explicit guard so a pruned-entity finding never becomes a phantom "node n".
- **Resolution to the BIGINT row:** join `apollo_concept_problems cp ON cp.search_space_id = r.search_space_id AND cp.problem_code = a.problem_id` (the denormalized `search_space_id` from migration 030 — `models.py:188` — makes this a direct course-scoped join, no subjects walk). Write `cp.quarantined_at`. The `concept_id` falls out of that same row (needed only for the audit log / observability, not for the write target).
- **Course scoping is by `runs.search_space_id`** (the authoritative course on the graded run), reconciled against `cp.search_space_id` in the resolution join — both must agree (they will, since the attempt's session is course-scoped). This is the §1.4 isolation invariant carried through the join.

## Public signatures (backward-compat contract)

```python
# apollo/provisioning/quarantine_constants.py
N_MIN: int                      # 8
THETA_MISS: float               # 0.80
CONCENTRATION_MARGIN: float     # 0.40

# apollo/provisioning/quarantine.py
from dataclasses import dataclass

# A per-problem coverage matrix: reference-node key -> per-attempt "missing?" flags.
# All sequences share length N (the attempt count). Keyed by the node's stable id
# (str(entity_id) at the sweep boundary; arbitrary hashable in fixtures).
CoverageMatrix = Mapping[str, Sequence[bool]]

@dataclass(frozen=True)
class QuarantineVerdict:
    quarantine: bool
    n_attempts: int
    top_node_key: str | None
    top_miss_rate: float
    mean_miss_rate: float
    concentration: float          # top_miss_rate - mean_miss_rate
    reason: str                   # "fired"|"below_n_min"|"below_theta"|"below_margin"|"empty"

def quarantine_decision(
    coverage_matrix: CoverageMatrix,
    *,
    n_min: int,
    theta_miss: float,
    margin: float,
) -> QuarantineVerdict: ...
    # PURE. No DB/IO/LLM. Never mutates the input. Never divides by zero.

@dataclass(frozen=True)
class SweepReport:
    n_evaluated: int
    n_quarantined: int
    n_cleared: int
    fired_problem_codes: tuple[str, ...]
    cleared_problem_codes: tuple[str, ...]

async def sweep_quarantine(
    db: AsyncSession,
    *,
    search_space_id: int | None = None,
) -> SweepReport: ...
    # Reads the §9 OPS-3 join, calls quarantine_decision per (problem, course),
    # sets/clears quarantined_at REVERSIBLY, logs one audit row per fire/clear,
    # commits once, returns the report. Best-effort: catches/logs per-problem
    # resolution misses, never raises to the caller (see Error contract).

__all__ = ["CoverageMatrix", "QuarantineVerdict", "quarantine_decision",
           "SweepReport", "sweep_quarantine"]
```

**Backward-compat — the ONE edited public symbol:**
```python
# apollo/overseer/problem_selector.py :: list_problems_for_concept
# Signature UNCHANGED: async def list_problems_for_concept(db, *, concept_id: int) -> list[Problem]
# Only the WHERE gains one predicate:
.where(
    ConceptProblem.concept_id == concept_id,
    ConceptProblem.tier == 2,
    ConceptProblem.quarantined_at.is_(None),   # <-- WU-3B2h adds this line
)
```
No caller changes anywhere — `select_problem` (`:73`) and `select_problem_personalized` (`:113`) both already call `list_problems_for_concept` and are unaffected by the added predicate. This preserves every existing test of those two functions (they seed live Tier-2 problems, which still pass `quarantined_at IS NULL`).

## Transaction scope decisions

- **`sweep_quarantine` writes to ONE table (`apollo_concept_problems.quarantined_at`)** across potentially many rows. Strategy: **single service-layer transaction** — accumulate all set/clear mutations on the ORM rows in the session, then ONE `await db.commit()` at the end. All-or-nothing per sweep: if the commit fails the whole sweep rolls back (no half-quarantined state). This mirrors the queue module's commit-per-operation discipline but batches because a sweep is a periodic bulk reconciliation, not a per-job action. The aggregation SELECT is read-only and precedes the writes.
- **No external service touched** (no LLM, no Neo4j) → no compensation/saga needed. The sweep is pure-Postgres.
- **The shadow-verification test commits nothing of its own beyond what `handle_done` already does** under the savepoint `db_session` (rolled back at teardown).
- **`quarantine_decision` is pure** — no transaction concern.

## Error contract decisions

- **`quarantine_decision`** never raises for ordinary inputs (empty matrix, ragged-but-handled, zero attempts → a `reason`-tagged non-firing verdict). It MAY assume sequences in one matrix share length N (a programming-error precondition the sweep guarantees by construction); it does not validate that defensively.
- **`sweep_quarantine` is best-effort observability infrastructure, NOT a request handler.** It does NOT introduce or raise a new exception hierarchy. A per-problem resolution miss (e.g. a `problem_code` with no matching `apollo_concept_problems` row — an orphan finding) is caught, logged at `WARNING` with `event=quarantine_resolution_miss`, and skipped; the sweep continues. Only a genuine DB/connection failure on the final `commit()` propagates (the caller — a janitor/cron/ops harness — handles retry, exactly as `learner_janitor` does). This matches the repo convention: audit/janitor sweeps log-and-continue rather than abort the whole pass on one bad row.
- **No HTTP shape** — there is no endpoint in this unit. The function returns a `SweepReport`; an ops caller logs it. (If a future endpoint surfaces quarantine status, it reuses the existing FastAPI error envelope — out of scope here.)
- **The selector predicate** adds no error surface — `list_problems_for_concept` raising behavior (`PoolExhaustedError` upstream in `select_problem`) is unchanged.

## Owner-doc updates (drift contract)

Owner doc: `docs/architecture/apollo.md` (frontmatter `owns: apollo/**` covers both `apollo/provisioning/**` and `apollo/overseer/problem_selector.py`; `last_verified` is already `2026-06-20`). Reconcile in the SAME commit as the code. The doc already has a provisioning section (lines 34/39) that names quarantine and the Tier-2 selector as PENDING/forward-references — convert those to SHIPPED statements:

- [ ] In the module map / provisioning narrative, register the two NEW files: `apollo/provisioning/quarantine.py` (`quarantine_decision` PURE statistic + `sweep_quarantine` real-PG reversible sweep) and `apollo/provisioning/quarantine_constants.py` (`N_MIN=8`/`THETA_MISS=0.80`/`CONCENTRATION_MARGIN=0.40`, env-overridable, calibration-tunable).
- [ ] Update the Tier-2 selector line (line ~43, `problem_selector`) to state the selectability gate is now `tier == 2 AND quarantined_at IS NULL`, both predicates live in `list_problems_for_concept` (the sole chokepoint), and it gates both `select_problem` and `select_problem_personalized`.
- [ ] Document the §9 OPS-3 quarantine JOIN (findings→runs→attempts TEXT problem_code → BIGINT concept_problems row, course-scoped by `runs.search_space_id`) and the §9 OPS-4 KNOWN LIMIT honestly: the simple rule false-quarantines genuinely-hard uniform nodes; v1 mitigations are reversibility + per-fire audit log + advisory-during-calibration; point-biserial is v1.1.
- [ ] Document the §9 SPEC-4 shadow invariant: SHADOW ON + LAYER3 OFF → auto-provisioned Tier-2 problem is teachable, produces `apollo_graph_comparison_findings`, moves ZERO Layer-3 beliefs; this is the data source quarantine consumes.
- [ ] Record the ADJ #12 calibration checklist pointer (the criteria that must pass before flipping `APOLLO_AUTOPROVISION_ENABLED`) if not already present — quarantine is ADVISORY until then.
- [ ] Bump `last_verified` to `2026-06-20` (already that date; re-confirm it is set, since the contract requires bumping it in the same work).
- [ ] **No `domain-data.md` change** — this unit adds no migration, no ORM class, no `teacher_weekly.py` edit (all of that was WU-3B2a/3B2g). Note in the PR description that domain-data is intentionally untouched.

Verify: `Grep "quarantine.py|quarantine_constants|quarantined_at IS NULL" docs/architecture/apollo.md` returns the new registrations.

## Downstream consumers

- **`apollo.overseer.problem_selector.select_problem` / `select_problem_personalized`** — the only runtime consumers of the selector predicate; they get quarantine-exclusion for free (no edit). The student-facing problem pick now silently excludes quarantined problems.
- **`apollo.handlers.next` / session orchestration** — calls `select_problem*`; benefits transitively. No change.
- **Frontend (student-ui)** — consumes the selected problem via the existing Apollo session API; a quarantined problem simply never appears. No URL/contract change, so no frontend edit. (Grep confirmed: the student-ui talks to the Apollo session endpoints, not to any per-problem selectability flag; quarantine is server-side invisible to it.)
- **Ops / nightly harness (3B2g-scaffolded)** — the only NEW consumer of `sweep_quarantine`. This unit ships the function with a clean `(db, *, search_space_id=None) -> SweepReport` signature; wiring it to a schedule is an ops follow-up already anticipated in 3B2g's scaffolding (out of scope here).
- **Calibration reviewers (human)** — consume the per-fire `event=quarantine_fire` log rows to audit false positives before flipping `APOLLO_AUTOPROVISION_ENABLED` (ADJ #12).

## Risks

- **[HIGH] False-quarantine of genuinely-hard uniform nodes (§9 OPS-4).** The simple rule cannot distinguish a mispaired solution from a legitimately-hard prerequisite that most students miss. MITIGATION (all v1, all in this plan): reversibility (`test_sweep_reclears_...`), the per-fire audit log (`test_sweep_emits_audit_log_on_fire`), the `below_margin` uniform-hard boundary test (`test_no_fire_below_concentration_margin` — proves a UNIFORMLY hard problem does NOT fire, only a CONCENTRATED one does), and advisory-during-calibration. Documented honestly in `apollo.md`. Point-biserial refinement is v1.1 — explicitly deferred (ADJ #1/#8). Confidence the mitigations are correctly scoped: HIGH.
- **[HIGH] TEXT-vs-BIGINT join resolves the WRONG row.** If the sweep resolved `problem_code` against `apollo_concept_problems` without course scoping, the same `problem_code` string in two courses would cross-quarantine. MITIGATION: the resolution join is `cp.search_space_id = r.search_space_id AND cp.problem_code = a.problem_id`, and `test_sweep_is_course_scoped_by_run_search_space_id` proves no cross-course leak. Confidence: HIGH (the test is explicitly designed to catch this).
- **[MEDIUM] N denominator computed wrong (findings count vs distinct-attempt count).** If N were `count(findings)` instead of `COUNT(DISTINCT attempt)`, miss rates would be garbage. MITIGATION: the plan pins N = `COUNT(DISTINCT runs.attempt_id)` and the seed fixtures use a known N so a miscount flips an assertion. Confidence: MEDIUM-HIGH — the executor must implement N carefully; the fixtures are the guard.
- **[MEDIUM] NULL `entity_id` findings becoming phantom nodes.** A pruned-entity finding has `entity_id IS NULL`. MITIGATION: the plan mandates excluding `entity_id IS NULL` from the per-node aggregation. Confidence: MEDIUM — flagged explicitly so the executor adds the guard; no dedicated test specified (could add one — see Deviations).
- **[MEDIUM] Shadow-verification flakiness from the `handle_done` harness.** The §6 chain is heavy; the bernoulli payload must satisfy `validate_reference_graph` or the shadow run raises before findings. MITIGATION: reuse the EXACT working `test_done_shadow_route_postgres.py` harness + bernoulli intro payloads (already green in the repo), changing only the added findings assertion. Confidence: HIGH (mirroring a passing test).
- **[LOW] diff-cover patch < 95%.** The sweep's error/edge branches (resolution-miss WARNING, empty-course no-op) must be hit. MITIGATION: Layer-2 tests include the audit-log, reclear, idempotent-second-run, and missing-node-filter branches; add a resolution-miss test if coverage flags the WARNING branch. Confidence: MEDIUM — the executor should run `diff-cover` and patch any uncovered branch with a targeted test before declaring done.
- **[LOW] Compare branch correctness.** diff-cover MUST compare against `feat/apollo-kg-wu3b2g-orchestrator` (the parent), NOT staging/main. Stated in Verify commands.

## Deviations I'd allow the executor

- **`CoverageMatrix` shape.** The plan specifies `Mapping[str, Sequence[bool]]`. The executor MAY instead use a small frozen dataclass (`@dataclass(frozen=True) class CoverageMatrix: node_keys: tuple[str,...]; miss_flags: Mapping[str, tuple[bool,...]]; n_attempts: int`) if that reads cleaner at the sweep boundary — as long as the pure function stays DB-free, immutable, and the fixtures remain hand-buildable. Either is acceptable; pick ONE and keep it consistent.
- **N-computation query shape.** Second grouped query, window function, or subquery — any is fine provided N == `COUNT(DISTINCT runs.attempt_id)` per `(problem_code, search_space_id)`.
- **The Layer-3 "bridge" test (`test_shadow_findings_feed_sweep_end_to_end`).** RECOMMENDED but not load-bearing for coverage. The executor MAY drop it if the per-deliverable tests already give ≥95% patch coverage and the seed proves too heavy; the two deliverables are independently proven by Layer-2 (`test_sweep_quarantines_...` hand-writes findings) and Layer-3 (`test_shadow_..._produces_findings_...`). Keeping it is preferred (it proves composition).
- **A dedicated `entity_id IS NULL` exclusion test.** The plan flags the guard as MEDIUM risk without a named test. The executor SHOULD add `test_sweep_ignores_null_entity_id_findings` if it cleanly seeds a NULL-entity finding; if create_all/FK constraints make that awkward, document the guard's presence in the sweep code instead.
- **Verdict `reason` string values.** The exact tokens (`"fired"|"below_n_min"|"below_theta"|"below_margin"|"empty"`) are a suggestion; the executor MAY rename for clarity as long as the tests assert the renamed tokens and each non-firing branch is distinguishable (the tests rely on `reason` to mutation-prove WHICH clause blocked).
- **NOT negotiable:** stdlib `statistics` only (no numpy/scipy); no migration; no `done.py`/grading-core edit; the selector predicate is `quarantined_at.is_(None)` and nothing more; every real-PG test GREEN-not-skipped; diff-cover ≥95% vs `feat/apollo-kg-wu3b2g-orchestrator`; `apollo.md` reconciled with `last_verified=2026-06-20` in the same commit.

## Verify (full gate)

```bash
# pure layers (no container)
.venv/Scripts/python.exe -m pytest apollo/provisioning/tests/test_quarantine.py apollo/provisioning/tests/test_quarantine_constants.py -q
# selector unit
.venv/Scripts/python.exe -m pytest apollo/overseer/tests/test_problem_selector.py -q
# real-PG (Docker up; green-NOT-skipped — a skip is a contract FAIL)
.venv/Scripts/python.exe -m pytest tests/database/test_apollo_quarantine_shadow.py -q
# whole apollo suite (no regressions)
.venv/Scripts/python.exe -m pytest apollo -q
# patch coverage gate
.venv/Scripts/python.exe -m pytest --cov=. --cov-report=xml -q
diff-cover coverage.xml --compare-branch=feat/apollo-kg-wu3b2g-orchestrator --fail-under=95
```
