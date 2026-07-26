---
doc: apollo/resolution/guardrails
description: The two §5 anti-over-normalization guardrails — the HARD type-compat constraint and misconception competition/polarity.
owns:
  - apollo/resolution/structural.py
  - apollo/resolution/competition.py
related:
  - apollo/resolution/resolver
  - apollo/resolution/content-tiers
last_verified: 2026-07-25
stub: false
---

# Resolution — guardrails

> Part of the §5 resolver, currently **unwired** (see [_index](_index.md)).

Two pure guardrails that stop the resolver over-normalizing a wrong claim onto a
lexically-close correct entry.

## Interface

`structural.py`:
- `type_compatible(student_node_type, candidate) → bool` — the HARD constraint:
  a student node only resolves to a candidate of the SAME node type
  (condition↔condition only), enforced before any content scoring.
- `ScoredMatch` (frozen) — a student-node→candidate match with a working `score`
  (the RAW competition/assignment signal, before the method cap).

`competition.py`:
- `apply_misconception_competition(student_text, matches) → ScoredMatch | None` —
  a misconception within `_MISCONCEPTION_MARGIN` (0.05) of the best
  non-misconception score wins the competition; deterministic tie-break on
  `canonical_key`.
- `polarity_screen(student_text, candidate_text) → bool` — `False` iff the two
  texts are direction-inverted over an antonym pair (physics + macro pairs).

## Data flow

The resolver filters candidates by `type_compatible` first, then — inside the
lexical tiers — polarity-screens each fuzzy hit (misconceptions exempt, since a
misconception is *supposed* to be polar) and runs
`apply_misconception_competition` over the surviving per-candidate matches.

## Invariants & gotchas

- **No cross-type resolution, ever** — a condition never resolves to an equation
  candidate even at the top text score.
- **Misconceptions compete in EVERY resolution** — this is also what keeps
  algorithmic contradiction detection cheap.
- **Neighborhood corroboration is DEFERRED** — the §5 propagate-and-veto seam is
  deliberately unbuilt in v1 (student graphs are edge-sparse); the live resolver
  performs no neighborhood corroboration.
- `polarity_screen` skips a pair that appears in BOTH texts (not discriminating)
  and passes neutral text.

## Related

- [resolution/resolver](resolver.md) — applies both guardrails.
- [resolution/content-tiers](content-tiers.md) — produces the lexical matches
  competition ranks.
