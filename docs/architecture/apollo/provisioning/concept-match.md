---
doc: apollo/provisioning/concept-match
description: Closed-list concept matching for reversed provisioning — classify against a premade concept list.
owns:
  - apollo/provisioning/concept_match.py
related:
  - apollo/provisioning/authored-sets/orchestrator
  - apollo/conversation/curriculum/db
last_verified: 2026-07-25
stub: false
---

# provisioning/concept-match

Closed-list concept matching for reversed provisioning: classify each scraped problem against
the course's PREMADE concept list instead of minting new concepts. The default provisioning
mode when a course has registered concepts.

## Interface

- `match_concept(problem_text, concepts, *, chat_fn) -> ConceptMatch` — classify one problem.
- `ConceptMatch` — `concept_id` (None ⇔ `no_match`), `slug` (the registered spelling), `secondary`, `confidence`, `rationale`, `no_match`, `retried`.
- `build_match_schema() -> dict` — strict json_schema envelope.
- `norm_slug(slug) -> str` — hyphen/underscore/case-insensitive slug key (shared with `scripts/seed_premade_concepts.py`).

## Data flow

The model is BLIND to provenance: it sees `problem_text` + the full slug/name/description list
and emits primary + secondary + confidence + rationale. `match_concept` retries ONCE at
`reasoning_effort='medium'` when pass 1 is unparseable, is the self-contradiction slip
(`primary=NO_MATCH` while the rationale names a listed concept), or names an off-list slug. The
returned `slug` is the REGISTERED row's spelling, so downstream persistence uses the course's
own vocabulary.

## Invariants & gotchas

- **`NO_MATCH` is allowed and NEVER force-matched** — the orchestrator holds `NO_MATCH`
  problems for teacher review. A persistent hallucinated slug also resolves to `no_match`.
- **`norm_slug` keys hyphens and underscores identically** so a hyphenated list slug
  (`integration-by-parts`) matches a registry underscore row (`integration_by_parts`) instead
  of duplicating it — the same key `concepts_api.mint_slug` and the seed script use.
- `chat_fn` is injected main-tier-shaped (`metered_chat.main`); Tier-1 tests run with no network.

## Related

- `provisioning/authored-sets/orchestrator` — the reversed-provisioning consumer.
- `apollo/conversation/curriculum/db` — `RegisteredConcept` / `list_registered_concepts`.
