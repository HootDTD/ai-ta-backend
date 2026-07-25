---
doc: ai-ta-backend/platform/ops-eval-scripts
description: The manual eval/smoke/spike scripts (live-LLM, cost money, never on the CI path) — DAG-4 granularity, iterative-scan recall, pgvector smoke, wave-1 live smoke, and the RQ3 edge-extraction spike
owns:
  - scripts/dag4_granularity_eval.py
  - scripts/eval_iterative_scan_recall.py
  - scripts/test_search.py
  - scripts/wave1_live_smoke.py
  - scripts/spikes/rq3_edge_extraction.py
  - scripts/spikes/rq3_results.json
related:
  - ai-ta-backend/rag-pipeline/hybrid-search
  - ai-ta-backend/apollo/provisioning/authored-sets/graph-derivation
last_verified: 2026-07-25
stub: false
---

# platform/ops-eval-scripts — manual eval / smoke / spikes

Grouped one-shots (R3): non-pytest, live-LLM diagnostics that hit the network and
cost money — **never on the CI path**. Each reads `OPENAI_API_KEY` from the
checkout `.env` and never modifies it.

## Interface

- **`dag4_granularity_eval.py`** — compares legacy vs KC-grained
  `derive_reference_graph` with `APOLLO_KC_GRANULARITY` off/on over six inline
  fixtures (three calculus, three qualitative management); reports node
  counts + defect/retry behavior.
- **`eval_iterative_scan_recall.py`** — retrieval-tuning gate comparing a TRUE
  brute-force scan (planner forced off HNSW) vs the relaxed HNSW iterative-scan
  top-k of `hybrid_search`, over ≥3 query types.
- **`test_search.py`** — a quick pgvector hybrid-search smoke against the Gen-3
  schema (`Course`/`Document`/`DocumentChunk`); loads `.env`.
- **`wave1_live_smoke.py`** — four live DAG-3 prompt scenarios end-to-end through
  the defect-retry harness + gate-9 lint.
- **`spikes/rq3_edge_extraction.py`** — the RQ3 spike measuring GPT-4o typed-KG-
  edge validity by replaying teaching transcripts; `spikes/rq3_results.json` is
  its captured output.

## Invariants & gotchas

- **`scripts/test_search.py` is a live harness, NOT a pytest test** — it is
  OWNED here and must not be caught by a `test_*`/test-dir exclusion glob (§6.2).
- These are diagnostics, not shipped code — `.coveragerc` omits the live-LLM
  smokes (`wave1_live_smoke.py`, `dag4_granularity_eval.py`) so they can't starve
  the patch-coverage gate.
- `dag4`/`wave1`/`rq3` carry hardcoded local `.env` paths from their author's
  machine — adjust before running elsewhere.

## Env flags

`OPENAI_API_KEY`, `SUPABASE_DB_URL`, `APOLLO_KC_GRANULARITY`,
`HNSW_ITERATIVE_SCAN`/`HNSW_EF_SEARCH` (read via the systems under test).

## Related

`rag-pipeline/hybrid-search` (the retrieval system the recall/smoke evals probe),
`apollo/provisioning/authored-sets/graph-derivation` (the KG derivation
`dag4`/`wave1` exercise).
