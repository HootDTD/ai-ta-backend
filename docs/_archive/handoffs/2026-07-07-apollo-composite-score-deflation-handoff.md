# Handoff — Apollo graph-grader composite-score deflation (post-§10 gate calibration)

**Date:** 2026-07-07 · **Author:** Claude (Fable) session with product owner
**Predecessor state:** PR #105 (`feat/apollo-composite-gate-calibration` → staging) — abstention is FIXED
(0/31 abstained on the frozen F1c corpus, was 31/31). This handoff is the NEXT problem: attempts now
*grade*, but the graph composite under-scores correct answers so badly that every strong persona bands
Beginning/Developing. Mission: make the graph composite/bands honest enough to measure band-agreement
against the LLM grader (the promotion criterion).

---

## 1. Where the system stands (read this before anything)

- **PR #105 open** (calibrated composite gate: `APOLLO_COMPOSITE_COVERAGE_MIN` default 0.1; ≥1
  contradiction finding grants grading; per-attempt replay instrumentation). Merge it (or branch off it)
  before starting — this work stacks on top.
- `APOLLO_ABSTENTION_COMPOSITE` is still **default-OFF**; staging Railway env flip is a pending human
  step. All grading here is SHADOW (`APOLLO_GRAPH_GRADER_LIVE=0`); students always get the LLM grade.
- Decision record: `docs/_archive/design/2026-07-06-abstention-signal-decision-memo.md` §6 addendum
  (2026-07-07) — the calibration data, the pre/post-audit finding, and the ranked next-lane list.
- Owner doc: `docs/architecture/apollo.md` (composite-gate bullet updated 2026-07-07; drift contract
  applies to everything you touch under `apollo/`).

## 2. The deflation, measured (F1c frozen corpus, replay `replay-composite-calibrated-0p1.json`)

Composite = `w_n·node_coverage + w_e·edge_coverage − p·misc_penalty` with defaults
**w_n=0.6, w_e=0.25, p=0.15** (`apollo/grading/composite.py:24-26`, env-overridable
`APOLLO_COMPOSITE_W_NODE/W_EDGE/P_MISC`). Bands: Strong ≥ **0.85**, Proficient ≥ 0.70,
Developing ≥ 0.50 (`apollo/projections/scorecard.py:33`, `BANDS` at :63).

**The structural ceiling:** max possible composite = 0.6·1.0 + 0.25·1.0 = **0.85 = the Strong cut**.
A mathematically perfect attempt scores exactly the Strong minimum; any imperfection lands below.
The band cuts were scaled for the LLM composite, not this formula. No threshold in the gate causes
this — it is score-scale mismatch plus two deflated inputs:

| class | node_cov (pre-audit) | edge_cov | composite | band |
|---|---|---|---|---|
| strong (10) | 0.20–0.75 | 0–0.25 (5/10 zero) | **0.12–0.486** | all Beginning |
| partial (8) | 0.20–1.00 | 0–0.25 (5/8 zero) | 0.12–0.662 | Beginning; one Developing |
| misconception (7) | 0.20–0.80 | 0–0.25 | 0.12–0.542 | Beginning/Developing |
| vague (6) | 0.25–1.00 | 0–0.25 | 0.15–0.662 | Beginning/Developing |

Full per-attempt table: query below (§6) or the replay JSON's `band_vs_expected` rows (each carries
`node_coverage`, `graph_composite`, `unresolved_rate` — added in PR #105).

## 3. The three deflating components (evidence + fix direction each)

### A. `node_coverage_score` is PRE-transcript-audit (the dominant deflator)
- `grade_attempt` computes scores from resolver output only; `build_audited_grade` then rewrites
  MISSING→COVERED findings via the §6.3 transcript audit (cap 0.75) but **scores are never recomputed**.
  Example (attempt 15, strong): resolver resolved 1/5 path nodes → node_cov 0.20; audit upgraded the
  other 4 → findings say 5/5 covered, ledger recall 1.00, score still 0.20.
- **DO NOT naively recompute scores post-audit.** Measured 2026-07-07: post-audit coverage saturates at
  1.0 for **29/31 attempts including 6/7 misconception controls and 4/8 node-omitting partial personas**
  (attempt 40 taught 3, audit credited 4/4). The audit over-credits; switching scores to it would inflate
  wrong answers identically. This is the **audit over-credit hazard** — memo §6.
- **Prerequisite lane:** audit over-credit probe. Bound the auditor's false-credit rate (why does it
  credit nodes a persona never taught? span quality? prompt?). Only after it's bounded can a score
  variant count audit-upgraded nodes (possibly at their 0.75 confidence as a discount weight). S3 does
  not catch this — it audits the ledger *against the transcript*, and the transcript usually contains
  adjacent text the auditor accepts.

### B. Resolver recall (the honest long-pole fix)
- Pre-audit recall is the real signal; raising it raises node_coverage honestly. Existing lanes:
  the clarification loop (live product feature, G2 — replay can NOT exercise it: replay passes
  `clarification_trace=[]`), and the NLI tier (thresholds tuned precision-first 2026-07-01; the
  large-model retune note is in the `apollo-nli-resolver-tier` memory / branch history).
- Per-problem resolver texture varies wildly (strong personas: 0.20 on attempt 15 vs 0.75 on 16/21) —
  worth a per-tier breakdown (which tiers fire per resolved node: `method` on `ResolvedNode`) before
  touching thresholds.

### C. `edge_coverage` is weak (NOT dead — corrected 2026-07-07)
- 12/31 attempts nonzero, **max observed 0.25**, 19/31 zero. Edges need BOTH endpoints resolved
  (`build_student_canonical` drops unresolved-endpoint edges, counted in `dropped_edge_count`), so edge
  recall compounds node recall (~quadratic). Reference USES/PRECEDES edges from PR #59 are real; the
  student side starves.
- Fix follows B mostly; separate check worth doing: of edges whose endpoints DID both resolve, what
  fraction match a reference edge? (If low, there's an independent edge-matching defect.)

### D. Weights/bands mis-scaled for the graph composite (cheapest lever, do FIRST)
- Even with A/B/C perfect, Strong is a knife-edge. Options, all measurable offline from existing data:
  1. Renormalize weights so max = 1.0 (e.g. w_n=0.706, w_e=0.294 — keeps 0.6/0.25 ratio), or
  2. Recalibrate band cuts against the corpus distribution, or
  3. Divide composite by (w_n + w_e) at scorecard-render time (graph path only).
- Constraint: changing served-scale semantics is a grading-behavior change → keep it env-gated /
  shadow-only, and pick values by **band-agreement vs the LLM grade** (`campaign/report.py
  paired_comparison`; the frozen `attempts.jsonl` rows carry the LLM composite/band per attempt, so a
  weights/bands sweep joins replay graph scores to frozen LLM bands with zero new runs).

## 4. Recommended attack order

1. **Offline weights/bands sweep (hours):** join `replay-composite-calibrated-0p1.json` per-attempt
   `node_coverage`/`graph_composite` with the frozen LLM bands from `campaign/out/f1c/attempts.jsonl`;
   sweep (w_n, w_e, band cuts); report band-agreement per class. Gives the honest ceiling of "rescale
   only" and quantifies how much A/B must deliver.
2. **Audit over-credit probe:** unblocks lane A. Sample the audit-upgraded findings from the 2026-07-07
   replay runs (query §6 — `message = 'upgraded by transcript_audit'`), diff against each persona's
   `expected` ledger, classify false credits, then attack the auditor prompt/acceptance rule.
3. **Resolver recall per-tier breakdown** → clarification-loop live data + NLI retune (lane B).
4. **Edge-matching liveness check** (lane C second half).

## 5. Non-negotiable contracts

- Branch off **staging** (never ApolloV3); PR back to staging. Stack on PR #105 if unmerged.
- **95% patch coverage** (diff-cover vs origin/staging) — CI enforces; probe harnesses that can't be
  tested stay OUT of the PR (precedent: PR #66 stripped 3.6k untested harness lines).
- Flag-OFF paths byte-identical; grade-math changes shadow-only behind env/flags.
- Drift contract: update `docs/architecture/apollo.md` in the same commit as any `apollo/` code change.
- Freeze discipline: NEVER edit `campaign/out/f1c/**` frozen files (attempts.jsonl, config.json,
  replay-baseline-*.json); new measurement outputs are new files; grep every replay log for
  `degrading_without_nli` (0 hits required) before trusting numbers; rebaseline explicitly if the
  harness changes.

## 6. Instrument runbook (everything was verified working 2026-07-07)

```bash
# Stack (volumes persist; F1c state intact: 57 attempts / 41 sessions / 36+ runs / 753 Neo4j nodes)
# 1. Start Docker Desktop  2. from ai-ta-backend:
supabase start                                                  # e2e-harness, DB :57322
docker compose -f campaign/infra/docker-compose.neo4j.yml up -d # bolt :57687, auth neo4j/campaignpass

# Replay (minutes per full-corpus run; venv = repo .venv, torch 2.6.0+cpu)
set -a; source .env.campaign; set +a
export APOLLO_NLI_ENABLED=1 APOLLO_NLI_MISC_POSITIVE_CERTIFY=1 APOLLO_ABSTENTION_COMPOSITE=1 HF_HUB_OFFLINE=1
./.venv/Scripts/python.exe -m campaign.replay --run-dir campaign/out/f1c --out campaign/out/f1c/<new-name>.json
```

- Replay is NOT read-only: it appends `apollo_graph_comparison_runs` rows (SUPERSEDE per
  attempt×version) and MERGEs Neo4j edges. Metrics reproduce deterministically anyway.
- Per-run scores: `SELECT node_coverage_score, edge_coverage_score, ... FROM
  apollo_graph_comparison_runs WHERE attempt_id = $1 ORDER BY created_at DESC LIMIT 1`.
- Findings (incl. audit upgrades): `SELECT finding_kind, evidence_spans, message FROM
  apollo_graph_comparison_findings WHERE run_id = $1` — audit-upgraded rows carry
  `message = 'upgraded by transcript_audit'` and confidence 0.75.
- Personas + expected ledgers: `campaign/out/f1c/attempts.jsonl` (36 rows, 31 gradeable) and
  `campaign/cast/personas/`.

## 7. Loose ends inherited from 2026-07-07 (not this handoff's mission, don't trip on them)

- Stale invalid `GITHUB_TOKEN` in workspace `.claude/settings.local.json` breaks gh/git — prefix
  network git/gh calls with `env -u GITHUB_TOKEN` until the user deletes the env block.
- 4 fluid problem JSONs (`problem_02..05`) show uncommitted key-reorder-only diffs of unknown origin
  (suspect OneDrive sync of the uncommitted NLI-branch label backfill) — do not commit or discard
  blindly; reconcile against `feat/apollo-nli-resolver-tier` first.
- Exited `f2-postgres`/`f2-neo4j` containers still await teardown (weekend leftover).
- Misconception detection recall is 1/7 (certify path works, zero spurious) — separate lane, but its
  penalty term barely moves the composite today.
