---
doc: apollo/resolution/candidates
description: Construction of the closed per-attempt candidate set — reference nodes plus course misconception entities.
owns:
  - apollo/resolution/candidates.py
related:
  - apollo/resolution/resolver
  - apollo/resolution/guardrails
last_verified: 2026-07-25
stub: false
---

# Resolution — candidates

> Part of the §5 resolver, currently **unwired** (see [_index](_index.md)).

Builds the **closed** candidate set the resolver matches against: this problem's
reference nodes + the course's misconception entities. Pure + DB-free.

## Interface

- `Candidate` (frozen) — one resolution target: `canonical_key` (matching-space
  key), `canon_key` (`:Canon` surrogate id, `-1` when unprojected), `node_type`,
  `is_misconception`, `symbolic`, `aliases`, `display_name`, `opposes_key`,
  `exact_aliases`.
- `build_candidate_set(*, reference_nodes, misconception_entities)` — the closed
  set (refs first, misconceptions always appended; no dedup).
- `candidates_from_reference_solution(problem, *, canon_key_by_canonical_key)` —
  one candidate per reference-solution step.
- `candidates_from_misconceptions(misc, *, canon_key_by_canonical_key)` — one per
  `misconceptions.json` entry (`trigger_phrases`→`aliases`, `opposes`→
  `opposes_key`, node type `definition`).
- `unknown_reference_entry_types(problem)` — the distinct `entry_type`s with no
  ontology `NodeType` (degradation marker).
- Constants `METHOD_CONFIDENCE_CAP` / `RESOLUTION_METHODS` (re-exported via the
  facade).

## Data flow

The caller supplies a `canon_key_by_canonical_key` map (the WU-3C1 `:Canon`
surrogate-id projection keyed on `apollo_kg_entities.id`) so each `Candidate`
carries both its matching key and its edge target. `_node_type_for_entry` maps a
reference-step `entry_type` to its ontology `NodeType`; misconceptions always
carry node type `definition`.

## Invariants & gotchas

- **Misconceptions always compete** (§5 anti-over-normalization) — appended to
  every candidate set so a polar near-miss can out-compete a lexically-close
  correct entry.
- **The set is small (~15-25)** — that is what makes resolution a tiny matching
  problem, not a global ontology search.
- **G4 tolerance:** an `entry_type` outside `_ENTRY_TYPE_TO_NODE_TYPE` degrades
  to *no candidate* (the step is dropped) rather than `KeyError`-ing the whole
  attempt; `variable_mapping` MUST stay in the map (its absence caused the F1c
  `linear_motion` crash). Keep this map and the mint map
  (`persistence.learner_model_seed._ENTRY_TYPE_TO_KIND_PREFIX`) in lock-step.

## Related

- [resolution/resolver](resolver.md) — consumes the closed set.
- [resolution/guardrails](guardrails.md) — misconception competition over these.
