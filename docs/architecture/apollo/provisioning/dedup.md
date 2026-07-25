---
doc: apollo/provisioning/dedup
description: Course-local entity dedup ladder, its thresholds, and the gate-8 problem dedup hash.
owns:
  - apollo/provisioning/dedup.py
  - apollo/provisioning/dedup_constants.py
  - apollo/provisioning/problem_hash.py
related:
  - apollo/provisioning/tag-mint
  - apollo/provisioning/promotion-lint
  - apollo/persistence/models
last_verified: 2026-07-25
stub: false
---

# provisioning/dedup

Two provisioning dedup surfaces. `dedup.py` is the §8B.5 course-local ENTITY dedup
ladder used at mint time; `problem_hash.py` is the gate-8 PROBLEM dedup key used by the
promotion lint. `dedup_constants.py` holds the calibration thresholds.

## Interface

- `resolve_candidate(db, *, search_space_id, concept_id, candidate, embed_fn, judge_fn, ingest_run_id=None, exclude_entity_ids=None) -> DedupVerdict`
  — resolve one candidate entity against this course/concept's `apollo_kg_entities` inventory.
- `DedupVerdict` — frozen result (`verdict`, `method`, `similarity`, `matched_entity_id`).
- `is_false_merge_risk(candidate_key, existing_key) -> bool` — guard against merging distinct quantities.
- `problem_dup_hash(problem) -> str` — gate-8 content-only sha256 (`problem_hash.py`).
- `EMBED_MERGE_THRESHOLD`, `EMBED_JUDGE_BAND` — dedup-ladder thresholds (`dedup_constants.py`).

`DedupVerdict` and `resolve_candidate` are re-exported by the package facade; `problem_dup_hash`
is re-exported too (`provisioning/_index`).

## Data flow

The ladder is `slug-exact → scope_summary embedding cosine → injected LLM judge`,
short-circuiting on the first tier that decides and writing exactly ONE
`internal.dedup_decisions` audit row (plus per-run dedup-pressure gauges) on that tier.
`cos >= merge threshold` merges; `cos < band-low` is distinct; the in-band case escalates
to `judge_fn`. `problem_dup_hash` is a pure versioned sha256 over `problem_text` +
canonical `given_values` + `target_unknown`, membership-tested by `run_promotion_lint`.

## Invariants & gotchas

- **The candidate pool is scoped in SQL BEFORE any cosine** (the 2026-06-30 false-merge
  fix): `Concept.course_id` (two courses never merge) AND `LearnerEntity.concept_id` (two
  concepts of one course never merge), ordered by ascending id (first-writer-wins).
- **`exclude_entity_ids` blocks same-mint self-fusion** — only PRE-EXISTING entities from
  prior mints are legitimate dedup targets, so two distinct nodes of one problem (`m` vs `M`)
  can never fuse against each other. `is_false_merge_risk` also drops embed-tier candidates
  that share a base symbol but differ in casing/subscript/number.
- **`scope_summary` is embedded on the fly** — there is NO persisted entity vector.
  `embed_fn`/`judge_fn` are injected SYNC callables (deterministic, zero-network tests).
- **`problem_dup_hash` never queries the DB** — course/concept scoping is the CALLER's job
  (the concept-scoped `existing_problem_hashes` set). The version prefix makes a future
  normalization change detectable.
- `_record_decision` flushes but does not commit (the orchestrator owns the transaction).

## Env flags

- `APOLLO_DEDUP_MERGE_THRESHOLD` — embedding-tier merge cutoff (test-pinned default `0.92`).
- `APOLLO_DEDUP_JUDGE_BAND_LOW` — lower bound of the escalate-to-judge band (default `0.82`;
  band is lower-inclusive, upper-exclusive).

## Related

- `provisioning/tag-mint` — the mint pass that drives `resolve_candidate` per reference entity.
- `provisioning/promotion-lint` — gate 8 consumes `problem_dup_hash`.
- `apollo/persistence/models` — `Concept` / `LearnerEntity` / `DedupDecision` / `IngestRun` ORM.
