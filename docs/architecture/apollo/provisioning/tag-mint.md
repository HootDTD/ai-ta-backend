---
doc: apollo/provisioning/tag-mint
description: Stage-4 concept tag plus reference-entity mint, and its BIGINT-keyed persistence helpers.
owns:
  - apollo/provisioning/tag_mint.py
  - apollo/provisioning/tag_mint_persist.py
related:
  - apollo/provisioning/authored-sets/orchestrator
  - apollo/provisioning/promote
  - apollo/provisioning/dedup
  - apollo/persistence/learner-model-seed
  - apollo/persistence/models
last_verified: 2026-07-25
stub: false
---

# provisioning/tag-mint

Stage-4: given an already-approved `ApprovedPair`, `tag_and_mint` (`tag_mint.py`) drafts
the concept tag + prereq edges, resolves/creates the concept, authors its canonical
symbols, mints reference `EntitySpec`s through the dedup ladder, and inserts prereqs.
`tag_mint_persist.py` holds the async persistence helpers (paralleled from the seed
script's write pattern). Both files are merged into this one leaf.

## Interface

`tag_mint.py`
- `tag_and_mint(db, pair, *, chat_fn, embed_fn, resolved_concept=None) -> MintPlan` — the Stage-4 entry.
- `ApprovedPair` (the Stage-2→4 input), `MintPlan` (the observability + promote handoff), `ResolvedConcept` (reversed-provisioning pre-match), `TagMintError`.

`tag_mint_persist.py` (async persistence helpers, imported directly)
- `resolve_or_create_concept(db, *, search_space_id, slug, display_name) -> int` — also reused by `concepts_api`.
- `author_concept_symbols`, `upsert_entity`, `link_opposes`, `drop_unlinkable_minted_misconceptions`.
- `load_concept_entities`, `load_concept_prereq_adjacency`, `partition_prereqs_by_concept_scope`, `insert_prereqs`.

`ApprovedPair`, `MintPlan`, `TagMintError`, and `tag_and_mint` are re-exported by the
package facade (`provisioning/_index`). `upsert_entity` is the seam the DB kind-check tests import.

## Data flow

`tag_and_mint` LLM-drafts the tag (or uses a pre-matched `ResolvedConcept` for reversed
provisioning — no draft, no concept creation), resolves the slug to a BIGINT concept id,
authors `canonical_symbols`/`normalization_map` (first-writer-wins UNION from the approved
problem's symbols — NOT a promoted problem, which is circular because gate 4 runs before
promotion), mints reference `EntitySpec`s via the frozen `reference_solution_to_entities`
converter — routing each candidate through `dedup.resolve_candidate` before upsert — then
inserts the drafted prereqs. All writes delegate to `tag_mint_persist`; the caller owns the txn.

## Invariants & gotchas

- **DB-13: misconceptions minted but EXCLUDED.** Misconception `EntitySpec`s are built via
  `misconceptions_to_entities` but are NEVER `LearnerEntity` rows (the app-schema kind CHECK
  has no `misconception`); they surface only as `MintPlan.misconception_keys`, and
  opposes-linking is a permanent no-op. Preserve this narrative — it is load-bearing.
- **FAIL-CLOSED (`TagMintError`)** on a hallucinated/unmappable tag or a PRE-EXISTING
  misconception's unlinkable `opposes` key. THIS mint's own unlinkable misconception is
  dropped-with-log; a prereq edge naming an unminted key is dropped (optional enrichment, not fatal).
- **All persistence keys on the BIGINT `app.concepts.id`, never the slug** (the §6 namespace
  contract). Idempotent throughout: entity upsert on `(concept_id, canonical_key)`; prereqs
  `(from,to)` select-then-skip; symbol authoring first-writer-wins union.
- Layer-1 prereqs keep the legacy dependent→prerequisite direction and are NOT copied into
  KG/canonical DEPENDS_ON. A `course_id` is threaded through construction (DB-13 initplan-safe RLS).
- Content-equivalent candidates minted from one authored set collapse onto one representative
  BEFORE the ladder (case-sensitive → no `m≡M` fusion). `chat_fn`/`embed_fn` injected.

## Related

- `provisioning/authored-sets/orchestrator` — drives the mint+promote savepoint.
- `provisioning/promote` — runs the lint over this mint's output and flips Tier-2.
- `provisioning/dedup` — the `resolve_candidate` ladder each entity routes through.
- `apollo/persistence/learner-model-seed` — the frozen `EntitySpec` converters reused here.
- `apollo/persistence/models` — `Concept` / `LearnerEntity` / `EntityPrereq` ORM.
