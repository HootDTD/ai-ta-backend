# Weekend Worker B handoff — integrity & provisioning session (2026-07-02 → 2026-07-04)

**Session:** Worker B, replicated macOS machine, autonomous. Control tower: issue #82.
**Freeze SHA:** `0f5d4be` (agreed via tower; A's F2 rerun + B's validation subset both ran on it).
**Written:** 2026-07-04. Campaign closed early — all lanes exhausted by Friday morning UTC.

## Lanes closed / delivered

| Lane | Deliverable | Final state |
|------|-------------|-------------|
| B0 stack replication | Full local stack (Supabase 57321/22, Neo4j 57687, migrations→035, NLI offline cache) + F1c fluid smoke PASS all 6 dimensions | CLOSED |
| B1 / G3 shadow crash | PR #84 — shadow isolation, typed propagate-list pinned to route contract, byte-identity proven | **MERGED** |
| B3a / D1 slice | PR #85 — empty-bank reason removed (finding: it never set `abstained=True` — histogram pollution only); `misconceptions_status` marker plumbed artifact→persistence→scorecard (rework) | **MERGED** |
| B2.1 / G4.1 | PR #86 — variable_mapping consumable (diagnosis causality was INVERTED: mint emits, consumer map omitted); `GraphSimDegradation` tolerance | **MERGED** |
| B2.2 / G4.2 | PR #87 — parser tolerance (chained eq, `^`), resolver `_zero_form` unified onto `parse_zero_form`, gate-4 structured-only grounding (rework) | **MERGED** |
| B2.3 / G4.3 | PR #89 — within-mint content-equivalence collapse + cross-upload deterministic pre-match; m≡M false-merge class negative-tested (rework) | **MERGED** |
| B2.4 / G4.4 | PR #90 — `apollo_ingest_page_evidence` (migration 036), runs/errors populated, run-opened-before-indexing failure evidence (rework); GET exposes S2 inputs | **MERGED** |
| B2 lane gate | Re-mint on combined branch: **17 entities (was 37)**, done→200, first-ever eq credit on a WU-AAS-minted subject | PASS |
| B4 / Q2 | PR #88 — evidence_span `""`→`null` + renderer omits absent spans; struggle_signals correctness ripple | **MERGED** |
| B4 / Q1 | Re-verify → **THE weekend finding** (below) → PR #94 one-line prefix fix | **MERGED** (by A) |
| B3b / D1 store | Design memo + PR #91 increment-1 (`apollo_misconception_observations`, migration 037, `APOLLO_EMERGENT_MISCONCEPTIONS` default OFF, flag-OFF byte-identity proven) | **OPEN — human-review-required. Do NOT merge without human sign-off.** Rebased current, CI green. |
| B-stretch / O2 | 422 triage: chat 422s are `parser_could_not_extract` (diagnosis mislabel); the 8% attrition is harness-side (driver's broad except discards attempts) | CLOSED (diagnosis) |

Cross-review/merge-gate duty: gated and merged A's #83 (after pin-test REQUEST-CHANGES), #92 (canonical clarification trace), #93 (S1 calibration — after 2-BLOCKER REQUEST-CHANGES: plausibility clause, stranded cycle check). #95 (A's handoff) is at REQUEST-CHANGES: the `campaign/scripts/run_s1_s2.py` S2 fix has 2 test failures + 35% diff-cover + a set↔run positional-mapping defect.

## THE weekend finding (Q1)

Misconception detection was **structurally dead course-wide**: the bank seeder strips the `misc.` prefix
into the `code` column; `candidate_assembly._misconceptions_dict` read it back unprefixed;
`is_misconception_key()` keys on the prefix → the contradiction path was unreachable **regardless of
resolver recall**. Explains S5 vacuous in F1b, F1c, Q1-rerun, and F2. Fixed in #94 (red-first proven).
**S5's first real precision number requires #94 AND the resolution-recall improvements (A's iter-2
equivalence tier / the §10 floor decision) together — the bugs stack.** In my freeze validation the
seam demonstrably routes (`misc.pressure_velocity_same_direction` appeared live as a clarification
candidate key — first `misc.*` key ever observed in the wild); assertions still don't clear the floor.
Secondary Monday design question: in shadow mode the SERVED scorecard hardcodes `misconceptions: []`
on the LLM artifact (`artifact_build.py` LLM path) — teacher-visible watch_out needs a decision.

## §9 freeze validation (two machines agreeing)

A's F2 (isolated stack, 38 personas, 34 ok) vs my subset (fluid chunk @ freeze SHA): **6/7 claims
independently CONFIRMED** — 100% abstention w/ sole reason `unresolved_rate_above_threshold` at
recall 1.00 (the §10 denominator finding), traces 11/11, zero variable_mapping crashes, latency
agreement, identical leak signature (`cond.incompressibility`), misconception 0-assertions (G1-gated).
7th claim (vague-class traces) covered by b0smoke 4/4 on the same machine. Full table:
`docs/_archive/experiments/2026-07-04-weekend-b-freeze-validation.md`.
**Cut short at 12/16 by OpenAI `insufficient_quota` (billing, hard)** — my linear_motion attempt and
the WU-AAS re-mint (incl. the first live cross-upload-dedup observation) did NOT run; ready-to-run
drivers staged in untracked `campaign/out/freezeval/`. Bonus live proof: a quota 500 hit a shadow
chain mid-run and #84's isolation held in production conditions ("shadow isolated; live grade
unaffected").

## Evidence artifacts (committed with this handoff)
- `docs/_archive/experiments/2026-07-03-weekend-b-o2-422-triage.md`
- `docs/_archive/experiments/2026-07-03-weekend-b-g4-lanegate-evidence.md`
- `docs/_archive/experiments/2026-07-03-weekend-b-q1-verification.md`
- `docs/_archive/experiments/2026-07-04-weekend-b-freeze-validation.md`

## Open / BLOCKED
- **BLOCKED: OpenAI API quota exhausted (billing)** — blocks freeze-validation steps 2–3 and any live run from this machine. Staged drivers: `campaign/out/freezeval/run_linear_motion.py`, `provision_wu_aas.py`.
- PR #91 — Monday human review (see decisions below).
- PR #95 — A's rework of the S2 script (tests + coverage + set↔run join).
- Residual cross-upload dedup gap (different-label, payloadless non-equation kinds) — test-documented, harmless at 0 promoted (#82 ~2026-07-03T23:5xZ comment).
- SESSION-DOWN occurred twice (01:02–16:17Z and 00:31–~02:45Z 07-04, both context-window compaction on A's side, both recovered cleanly; no merges made under either).

## Monday decisions list
1. **§10 abstention-floor calibration** — the gate to ANY graph-grader throughput. Evidence: 100% abstention at recall 0.88–1.0 on both machines; A's iter-3 floor matrix is the decision table. Recommend including a matrix cell with #94 applied (misconception statements stop inflating the unresolved denominator on that persona class).
2. **PR #91 human review** — trust-gradient defaults (K=3, τ_assert=0.5, τ_project=0.2, 30d half-life), assertion semantics (promoted → watch_out), forward-only. Design memo OQ list: #82 comment ~2026-07-03T00:00Z. Both G1 flags (`APOLLO_ABSTENTION_DENOM_V2`, `APOLLO_EQUIV_RESOLUTION`) flip decisions are human too.
3. **Shadow-mode teacher-visible watch_out** (LLM artifact hardcodes `misconceptions: []`).
4. **OpenAI billing** (see BLOCKED).
5. O2 items: campaign driver catches parse 422s as non-fatal `parse_gap`; fix the diagnosis doc's `malformed_equation` label; optional product question — degrade content-empty teaching turns to a nudge.
6. linear_motion personas: re-author expected ledgers against real minted keys (17-entity mint makes this possible).
7. Diagnosis-doc corrections this weekend surfaced: G4.1 causality inverted; empty-bank never abstained; 422 label; Q1 root cause; vague-archetype 422s are the shadow-posture item (A's F2 note).
8. Teardown: A's F2 containers (f2-postgres :57422, f2-neo4j :57787-88). My machine keeps the campaign stack (57320-29/57687) + backend shim (`campaign_launch.py` pattern documented in the state file) for the next round.

## Stack / process notes for the next session
- `server.py` `load_dotenv(override=True)` trap: NEVER boot plain `uvicorn server:app` — repo `.env` points at REMOTES. Use the neuter-shim pattern.
- supabase CLI needs ≥2.109 for the repo config (this machine upgraded 2.45.5→2.109.0).
- Serial pytest for regression signals (xdist single-container contention noise); CI blocks on `ruff format --check` over ADDED files; branch protection wants head-up-to-date and repo auto-merge is disabled.
- Migrations: 036 (#90, merged) · 037 (#91, unmerged). Next free: 038.
- Registry final state: see #82 body (all weekend/* rows merged+deleted except `weekend/d1-emergent-store` (PR 91) and A's `weekend/worker-a-handoff` (PR 95) + this branch).
