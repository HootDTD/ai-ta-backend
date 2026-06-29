# Handoff — Apollo Shadow Grader: Node-Recovery Hardening + Live Econ Probe

**Date:** 2026-06-26
**Author:** Claude (Opus 4.8) via feller orchestration
**Spec:** `docs/superpowers/specs/2026-06-23-apollo-shadow-grader-node-recovery-design.md`
**Merged plan:** `ai-ta-backend/docs/_archive/plans/2026-06-23-apollo-grader-MERGED-plan.md`
**Implementation branch:** `feat/apollo-grader-node-recovery` (pushed to origin; 4 commits)
**Probe branch:** `probe/macro-econ-node-recovery` (local-only; sits on top of the 4 phase commits)

---

## TL;DR

- **Implemented** the spec's Phase 0 / 1a / 1b / 1c grader node-recovery hardening
  via a feller agent system (scout → plan → merge → execute → verify → review).
  4 commits, **+1506 / −31** across 22 files, all gates green.
- **Deterministic before/after proof captured** (`scripts/econ_grading_delta.py`,
  no infra): the rearranged econ equation that the grader previously **dropped**
  now **resolves** via the new `derived@0.95` tier → `edge_coverage 0.0 → 0.25`,
  `usage 0.0 → 1.0`, `unresolved_rate 0.333 → 0.0`, `dropped_edge_count 1 → 0`.
- **Live full-stack macro probe ran** against the local Supabase/Neo4j Docker
  stack. It **confirmed both infra blockers are fixed** (0×401 auth, 0×schema
  error) and the graph-sim grader **executed live on real econ attempts** (13
  attempts each produced `comparison_runs=1`). **Caveat:** the harness hit a
  120 s read-timeout on attempt #13 (`real_gdp_growth/strong`) and exited
  non-zero, so it **did not capture a clean per-metric score matrix** from the
  live run — the matrix file on disk is stale from the prior pre-fix run. See
  [Live probe — honest status](#5-live-full-stack-macro-probe--honest-status).

---

## 1. What this work is

The Apollo **graph-sim grader** is a SHADOW subsystem: it is the *intended*
production grader but is gated OFF for students (`APOLLO_GRAPH_SIM_LIVE_ENABLED`),
so the live student grade is still produced by the separate LLM matcher
(`overseer/coverage.py::compute_coverage` → `overseer/rubric.py::compute_rubric`).
This change hardens the shadow grader's **node-recovery** path — i.e. how a
student's free-text equations/procedures get resolved against the canonical
reference graph before scoring — so that algebraically-rearranged but correct
student equations stop being silently dropped (which had been zeroing out
`edge_coverage` and `usage` on real econ attempts).

---

## 2. What was implemented (4 phases)

All four phases landed on `feat/apollo-grader-node-recovery`. Grade-math purity
was preserved (the 3 grade-math byte-identity tests stayed green) — every phase
adds *resolution recall* or *abstention safety*, none changes the scoring formula.

| Commit | Phase | Summary |
|---|---|---|
| `a58bdbf` | **0 — resolver safety** | Type-gate so the LLM adjudication tier can't fire on non-equation nodes; explicit canonical merge-type + logger; self-loop guard in canonical build. |
| `3689986` | **1a — derived equation-alignment tier** | New `apollo/resolution/equation_alignment.py`: symbolic-only SymPy solve-for-variable; resolves algebraic rearrangements of a reference equation. Confidence cap **0.95** (`"derived"`). Plus the `econ_grading_delta.py` before/after harness. |
| `c60d988` | **1b — exact-only alias channel** | `exact_aliases` field on candidates (from `content["aliases"]`); `match_alias_all` reads `(*exact_aliases, *aliases)`, fuzzy stays `aliases`-only — curated reference phrasings resolve exactly without loosening fuzzy. |
| `6b4dc5d` | **1c — normalization-confidence abstention brake** | Abstention floor **0.85** + `REASON_LOW_NORMALIZATION_CONFIDENCE`; `build_audited_grade` reordered to rewrite findings → compute normalization-confidence over the POST-rewrite set → apply abstention → construct. |

**Method confidence-cap ladder (after this work):** exact `1.00` · symbolic `0.98`
· **derived `0.95` (new)** · alias `0.92` · fuzzy `0.80` · llm `0.75` ·
unresolved `0.00`.

### Files changed (diffstat `134391b..6b4dc5d`, +1506/−31)

```
apollo/resolution/equation_alignment.py          | 168 ++ (new, Phase 1a)
apollo/resolution/resolver.py                     |  12 +  (dispatch + type-gate)
apollo/resolution/candidates.py                   |   8 +  (derived cap + exact_aliases)
apollo/resolution/tiers.py                        |  18 +  (match_alias_all channel)
apollo/graph_compare/canonical.py                 |  32 +  (merge type + self-loop guard)
apollo/grading/abstention.py                      |  24 +  (floor + reason)
apollo/grading/normalization_confidence.py        |  50 +
apollo/grading/audited_grade.py                   |  13 +  (reorder)
scripts/econ_grading_delta.py                     | 237 ++ (new, before/after harness)
docs/architecture/apollo.md                       |   8 +  (drift-contract owner-doc update)
+ test modules:
  apollo/resolution/tests/test_equation_alignment.py | 175 ++
  apollo/resolution/tests/test_resolver.py           | 148 +
  apollo/resolution/tests/test_tiers.py              | 109 +
  apollo/resolution/tests/test_candidates.py         |  75 +
  apollo/graph_compare/tests/test_canonical_types.py | 113 ++
  apollo/graph_compare/tests/test_derived_equation_resolution.py | 82 ++
  apollo/graph_compare/tests/test_student_canonical.py | 29 +
  apollo/grading/tests/test_audited_grade.py         | 101 +
  apollo/grading/tests/test_abstention.py            |  34 +
  apollo/grading/tests/test_normalization_confidence.py | 15 +
  apollo/grading/tests/test_package_seam.py          |   1 +
  scripts/tests/test_econ_grading_delta.py           |  85 ++
```

---

## 3. Tests run and their outputs

### 3a. Deterministic econ before/after delta — the captured win

`scripts/econ_grading_delta.py` drives the **real** macro Q4
(`nominal_vs_real_gdp/problem_01`) through
`build_problem_candidates → resolve_attempt → build_student_canonical →
grade_attempt` with a hand-authored STRONG student graph. **Pure + deterministic**
— no DB, Neo4j, OpenAI, or server. It exercises:

- `deflator - (nomGDP/realGDP)*100` — sign-exact **control** (resolves before & after).
- `realGDP - nomGDP/(PI/100)` — the **rearranged form under test**.
- `USES` edge `stu_proc → stu_eq_rearranged` — dropped before, retained after.

**Re-ran 2026-06-26, output matches the committed AFTER block exactly:**

| metric | BEFORE (`base=a58bdbf`) | AFTER (Phase 1a+) |
|---|---|---|
| `realGDP - nomGDP/(PI/100)` resolution | **unresolved** | **resolved** |
| └ method | `unresolved` | **`derived`** |
| `dropped_edge_count` | **1** | **0** |
| `edge_coverage` | **0.0** | **0.25** |
| `usage` | **0.0** | **1.0** |
| `unresolved_rate` | **0.333** | **0.0** |
| `coverage` | 0.667 | 0.667 *(unchanged — formula intact)* |
| `node_coverage` | 0.667 | 0.667 |
| `scoping` | 1.0 | 1.0 |

> This is the cleanest evidence of "a difference in output." The grader now
> credits a correctly-rearranged equation instead of dropping its edge.

### 3b. Focused phase test modules — fresh run 2026-06-26

```
$ pytest apollo/resolution/tests/test_equation_alignment.py \
         apollo/grading/tests/test_abstention.py \
         apollo/grading/tests/test_audited_grade.py \
         apollo/resolution/tests/test_tiers.py -q
88 passed in 6.29s
```

### 3c. Full apollo suite + patch coverage (recorded at implementation time)

- Full `apollo/` suite green (the merged-plan VERIFICATION recorded **1339
  passed** for the seeder/derived-eq baseline; this branch adds the phase tests
  above on top and stayed green through each feller-verifier gate).
- **100% patch coverage** on changed lines (diff-cover vs `origin/staging`),
  ruff + mypy clean on new files. Each phase passed an independent
  feller-reviewer (cold-eyes) PASS before the next stacked.

> If you want a fresh full-suite + diff-cover number for the PR, re-run:
> `pytest --cov --cov-report=xml -q && diff-cover coverage.xml --compare-branch=origin/staging --fail-under=95`

---

## 4. Infra fixes discovered while wiring the live probe

Two real bugs surfaced (and were root-caused via systematic-debugging) when
booting the full local stack. **Both are local/throwaway — neither is part of
the mergeable feature branch:**

1. **401 "Invalid bearer token" on every probe attempt.**
   Root cause: `server.py` calls `load_dotenv(override=True)` which reloads
   `.env` (test-**cloud** `SUPABASE_URL`) over the shell's local values, so a
   server booted inside a local-env shell validated locally-issued tokens
   against the cloud project → 401. **Fix (probe branch only, uncommitted in
   `server.py`):** after the base `load_dotenv`, also
   `load_dotenv(".env.local", override=True)` when present — local wins, no-op
   when absent. Verified: 0×401 in the nr3 run.

2. **500 → `asyncpg UndefinedColumnError: column "soundness_applicable" …`.**
   Root cause: local Docker Postgres was at migration 030, missing **031**
   (`database/migrations/031_apollo_soundness_applicable.sql`, the soundness-N/A
   column from PR #62). The grade *computed correctly* — the INSERT failed only
   at persistence. **Fix:** applied **031 to the LOCAL Docker DB only** (never
   to any remote Supabase — per workspace rule). Verified: 0×UndefinedColumn in
   the nr3 run.

> Decision needed: the `server.py` `.env.local` override is genuinely useful for
> *any* local-stack dev, not just this probe. Consider graduating it into a real
> commit on a normal branch. As-is it lives uncommitted on the probe branch.

---

## 5. Live full-stack macro probe — honest status

**Goal:** re-run the econ questions end-to-end through the booted server
(`scripts/run_macro_probe.py` → `scripts/apollo_grade_probe.py`) and compare the
live grade output before/after the node-recovery work.

**What the latest run (`scripts/_run_macro_nr3.log`, 2026-06-24 00:38–00:44) proves:**

- Setup clean: seeded concepts/learner-model/canon (`search_space_id=3`,
  `concepts:3 problems:10`, canon `103` nodes); server booted on `:8001`.
- **0×401** and **0×UndefinedColumn** across the whole run — both §4 infra fixes
  are confirmed working live.
- The graph-sim grader **executed live on real econ attempts**: 13 of 15
  problem×variation combos completed with `graph-sim: comparison_runs=1
  mastery_events=0` (attempt_ids 49–61), nodes/edges written with
  `dropped=0 invalid=0` throughout.

**What it does NOT give us (the caveat):**

- On combo #13 (`real_gdp_growth/strong`) the `/apollo/sessions/{id}/done` POST
  hit a **120 s `requests.ReadTimeout`** and `apollo_grade_probe.py` exited
  non-zero. Because the harness crashed mid-sweep, `run_macro_probe.py` printed
  an **empty score table** and **did not write a fresh score matrix**.
- ⚠️ **`scripts/macro_probe_score_matrix.json` on disk is STALE** — it is the
  prior **nr2** run (attempt_ids 34–48, every row `"done 500: Internal Server
  Error"`, all metrics `null`), captured *before* migration 031 was applied. **Do
  not read it as the post-fix result.** The nr3 log supersedes it.

So: the live probe **de-risked the pipeline** (auth + schema + grader all run
on real econ data) but the **per-metric live before/after table was not
captured**. The deterministic harness in §3a is the authoritative before/after
artifact for now.

### To finish the live capture (recommended next step)

The timeout is a harness/latency issue, not a grader bug. Options:
1. Raise the probe's read-timeout above 120 s (the live `/done` path runs the
   real LLM adjudicator) — `read timeout=120` in `apollo_grade_probe.py::_post`.
2. Or drop `real_gdp_growth` (the slow hard combo) from the variation set for a
   clean capture of the other 4 problems.
3. Re-run: `./.venv/Scripts/python.exe scripts/run_macro_probe.py …` (the same
   invocation logged at the top of `scripts/_run_macro_nr3.log`), confirm a
   fresh `macro_probe_score_matrix.json` with `done 200` rows, then diff against
   the recorded experiment baseline in
   `scripts/apollo_grade_probe_report.json` /
   `docs/experiments/2026-06-22-macro-graph-grading-probe/RESULTS.md`.

---

## 6. Branch / PR / merge state

- `feat/apollo-grader-node-recovery` — **pushed to origin**, 4 phase commits
  (`a58bdbf` → `6b4dc5d`) stacked on the merged `staging` line (PR #62 soundness-N/A
  + PR #63 derived-eq seeder already merged underneath).
- **PR-to-`staging` state could not be confirmed** — `gh`/GitHub API returns
  **HTTP 401 Bad credentials** (the `GITHUB_TOKEN` user env var is stale; see
  the `github-access-wiring` memory). Refresh the classic PAT
  (`repo`/`workflow`/`read:org`) and re-check `gh pr list --head
  feat/apollo-grader-node-recovery`, opening the PR into `staging` if absent.
- `probe/macro-econ-node-recovery` — **local-only**, throwaway. Contains the
  ported macro probe harness (`e09eca2`, +3653) plus the uncommitted local infra
  fixes (`server.py` `.env.local` override, edited macro `problem_*.json`,
  stale matrix). Not intended to merge as-is.

---

## 7. Still open (from the spec, not done here)

- **Phase 2 — edge recovery** (the spec's edge-alignment counterpart to 1a) is
  **not implemented**.
- **§10 promotion calibration** — capturing the live shadow-vs-LLM agreement
  numbers that would justify flipping `APOLLO_GRAPH_SIM_LIVE_ENABLED` on — is
  **not done** (blocked on the clean live capture in §5).
- Decide whether to graduate the `server.py` `.env.local` override into a real commit.
- Refresh `GITHUB_TOKEN` and confirm/open the `feat/apollo-grader-node-recovery → staging` PR.

---

## 8. Key paths

| Path | What |
|---|---|
| `apollo/resolution/equation_alignment.py` | Phase 1a derived tier (new) |
| `apollo/resolution/{resolver,candidates,tiers}.py` | tier dispatch / caps / alias channel |
| `apollo/grading/{abstention,normalization_confidence,audited_grade}.py` | Phase 1c brake |
| `apollo/graph_compare/canonical.py` | Phase 0 merge-type + self-loop guard |
| `scripts/econ_grading_delta.py` | **deterministic before/after harness (§3a)** |
| `scripts/run_macro_probe.py`, `scripts/apollo_grade_probe.py` | live full-stack probe |
| `scripts/_run_macro_nr3.log` | **authoritative live-run log** (UTF-16; decoded copy `_run_macro_nr3.decoded.txt`) |
| `scripts/macro_probe_score_matrix.json` | ⚠️ **STALE (nr2, pre-fix)** — do not trust |
| `database/migrations/031_apollo_soundness_applicable.sql` | applied to LOCAL DB only |
| `docs/_archive/plans/2026-06-23-apollo-grader-MERGED-plan.md` | the 17-step merged plan |
| `docs/superpowers/specs/2026-06-23-apollo-shadow-grader-node-recovery-design.md` | source spec |
