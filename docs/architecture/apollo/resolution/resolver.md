---
doc: apollo/resolution/resolver
description: resolve_attempt — the §5 resolver orchestration that maps student nodes onto the closed candidate set (dormant).
owns:
  - apollo/resolution/resolver.py
  - apollo/resolution/__init__.py
related:
  - apollo/resolution/candidates
  - apollo/resolution/content-tiers
  - apollo/resolution/guardrails
  - apollo/resolution/assignment
  - apollo/resolution/result
last_verified: 2026-07-25
stub: false
---

# Resolution — resolver

> **DORMANT:** `resolve_attempt` has **no runtime caller** (see
> [_index](_index.md)).

The orchestration that composes candidates + content-tiers + guardrails +
assignment into a `ResolutionResult`. Also owns the package facade.

## Interface

- `resolve_attempt(student_graph, candidates, *, fuzzy_threshold=0.9,
  symbolic_mappings=None) → ResolutionResult` — resolve every student evidence
  node against the closed candidate set.
- Package facade `apollo/resolution/__init__.py` re-exports `resolve_attempt`,
  `build_candidate_set`, `candidates_from_reference_solution`,
  `candidates_from_misconceptions`, `Candidate`, `ResolvedNode`,
  `ResolutionResult`, `METHOD_CONFIDENCE_CAP`, `RESOLUTION_METHODS`.

## Data flow

Per node, `_content_match` runs the tiers in precedence order — exact → symbolic
→ derived → then the lexical (alias/fuzzy) tiers — behind the `type_compatible`
HARD constraint. First deterministic hit wins outright; in the lexical tiers ALL
above-threshold hits are collected (deduped per candidate, alias kept over
fuzzy), polarity-screened, and passed to `apply_misconception_competition`.
`greedy_global_assignment` then assigns each node its single best match; the
per-node `ResolvedNode` re-derives confidence from `METHOD_CONFIDENCE_CAP[method]`.

## Invariants & gotchas

- **≤1 LLM adjudication per attempt — actually 0:** the silent LLM path was
  retired; a post-tier non-match stays `unresolved` DATA and `llm_calls == 0`.
- **Never grades, simulates, or persists** (persistence lives in
  [knowledge-graph/resolution-store](../knowledge-graph/resolution-store.md)).
- **Symbolic mappings are per-problem declared data, never a global default:**
  with none supplied the symbolic tier applies no substitution (a global `d=2r`
  produced false matches).
- **Over `MAX_STUDENT_NODES` → the whole attempt abstains** (every node
  unresolved, no unbounded solve, no LLM).
- Pure + deterministic: same `(student_graph, candidates)` → same result.

## Related

- [resolution/candidates](candidates.md), [content-tiers](content-tiers.md),
  [guardrails](guardrails.md), [assignment](assignment.md) — the composed parts.
- [resolution/result](result.md) — the return shape.
