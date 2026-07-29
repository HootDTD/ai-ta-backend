---
doc: apollo/projections/scorecard
description: Pure student-scorecard template over the canonical grading artifact dict.
owns:
  - apollo/projections/scorecard.py
related:
  - apollo/grading/artifact-build
  - apollo/projections/mastery
  - apollo/conversation/handlers/done
last_verified: 2026-07-25
stub: false
---

# Projections scorecard — pure artifact template

`render_scorecard` is a pure template over an already-built canonical grading
artifact dict (the shape [artifact-build](../grading/artifact-build.md)
produces). No computation beyond formatting; `done.py` calls it on the payload
`write_artifacts` just persisted.

## Interface

- `render_scorecard(artifact) -> dict` — called live by `handlers/done.py`.
- `load_bands()`, `BANDS`, and the `WATCH_OUT_*` / band-env constants.

## Data flow

Reads `artifact["scores"]["composite"]` → `score_0_100` + a band via `_band_for`
over `load_bands()` (env-tunable thresholds). Reshapes `node_ledger` into
`taught_well` (credited rows, with a verbatim span only when non-empty) +
`missing_or_unclear` (unresolved rows as next-time guidance), `misconceptions`
into `watch_out`, and `clarification_trace` into inline exchanges.

## Invariants & gotchas

- **DRIFT — `composite` is the legacy axis blend, not the served grade.** On the
  live path `scores.composite` is set by
  [artifact-build](../grading/artifact-build.md) to the normalized axis-rubric
  overall (never `None`, so the band is that value — not the `0.0` default), so
  the scorecard band reflects the OLD rubric blend, while the student's actual
  grade of record is the [topic score](../overseer/topic-score.md). Do not
  present `scores.composite` as the current grade.
- **Stale module references.** The module + `_env_float` docstrings cite
  `apollo.grading.composite.load_weights` / `composite.py` and
  `build_graph_artifact` — none of which exist; the env-float band logic is inline
  here (and mirrored in [mastery](mastery.md)).
- **Cold-start watch-out is unreachable on the live LLM path.** `_watch_out_status`
  keys off `abstention.misconceptions_status`, which `build_llm_artifact` never
  writes, and `misconceptions` is always empty — so live scorecards always render
  `watch_out=[]` with status `checked`.

## Env flags

- `APOLLO_BAND_STRONG` / `APOLLO_BAND_PROFICIENT` / `APOLLO_BAND_DEVELOPING` —
  band thresholds, read fresh every call (defaults 0.85 / 0.70 / 0.50).
