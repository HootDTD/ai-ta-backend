---
doc: apollo/resolution/_index
description: Router for the §5 reference-anchored resolver package (WU-3C2) — a fully built but currently unwired subsystem.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# Apollo resolution — §5 reference-anchored resolver

> **DORMANCY BANNER (verified in staging).** The resolver is a fully-built but
> currently **UNWIRED** subsystem. `resolve_attempt` has **no non-test caller**
> and its persistence ([knowledge-graph/resolution-store](../knowledge-graph/resolution-store.md))
> is unwired. The only symbols reused live are the `tiers.py` helpers imported by
> `overseer/coverage.py` — which is **itself dormant**. Do **not** treat this as
> part of the live grading path; the live path is `transcript_coverage` +
> `topic_score`.

Maps each student evidence node onto a small closed candidate set with
content-first tiers, a structural type constraint, misconception competition, and
bounded greedy assignment. Pure, synchronous, deterministic; never grades,
simulates, or persists.

## Leaves

| Doc | One-liner | Owns |
|---|---|---|
| [resolver](resolver.md) | `resolve_attempt` orchestration + package facade | `apollo/resolution/resolver.py`, `apollo/resolution/__init__.py` |
| [result](result.md) | `ResolutionResult` / `ResolvedNode` models | `apollo/resolution/result.py` |
| [candidates](candidates.md) | Closed candidate set (refs + course misconceptions) | `apollo/resolution/candidates.py` |
| [guardrails](guardrails.md) | Type-compat HARD constraint + misconception competition/polarity | `apollo/resolution/structural.py`, `apollo/resolution/competition.py` |
| [content-tiers](content-tiers.md) | exact → symbolic → derived → alias → fuzzy matching ladder | `apollo/resolution/tiers.py`, `apollo/resolution/equation_alignment.py` |
| [assignment](assignment.md) | Bounded greedy global assignment | `apollo/resolution/assignment.py` |
| [embedding](embedding.md) | Cosine primitives + candidate embedding cache | `apollo/resolution/embedding.py` |

## Cross-cutting invariants

- **Confidence caps by method** (`METHOD_CONFIDENCE_CAP`) are the reported
  confidence; raw tier scores only rank competition/assignment.
- **A non-match is DATA, not an error** — an unresolved node emits no edge and no
  exception; the retired one-LLM-adjudication path means `llm_calls == 0`.
- **Anti-over-normalization** is the design goal: the closed candidate set stays
  small (~15-25), misconceptions always compete, and the type constraint is HARD.
