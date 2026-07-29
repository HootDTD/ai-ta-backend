---
doc: apollo/provisioning/scrape
description: Stage-1 LLM scrape of a document's chunks into Tier-1 (not-teachable) inventory rows.
owns:
  - apollo/provisioning/scrape.py
related:
  - apollo/provisioning/scrape-sections
  - apollo/provisioning/_index
  - apollo/persistence/models
  - apollo/conversation/curriculum/db
last_verified: 2026-07-25
stub: false
---

# provisioning/scrape

Stage-1 of §8B auto-provisioning: turn a document's already-embedded
`internal.document_chunks` rows into typed `CandidateQuestion` records and write them
as **Tier-1** `app.problems` rows. The default path is structure-aware
(`scrape_document`): sections are reconstructed and triaged once, then scraped
problem-likely-first; the legacy per-chunk path (`scrape_questions`) is the
`APOLLO_STRUCTURED_SCRAPE=0` revert.

## Interface

- `scrape_document(chunk_rows, *, chat_fn, triage_chat_fn, max_sections, min_candidates, structured=True, section_char_cap=…)` — structure-aware entry (delegates to `scrape_questions` when `structured=False`).
- `scrape_questions(chunks, *, chat_fn)` — legacy one-pass-per-chunk scrape.
- `scrape_section(section, *, concept_hint, chat_fn)` — scrape one whole reconstructed section.
- `write_tier1_problems(db, candidates, *, concept_id, search_space_id)` — persist candidates as `tier=1` rows; returns the count actually inserted.
- `resolve_or_create_provisional_concept(db, *, search_space_id)` — resolve/create the per-course `provisional.inventory` concept id.
- `CandidateQuestion` / `ScrapeResult` — the scrape value objects.
- `chunk_content_hash(content)` — normalized-content sha256 (reused by `retrieval_adapter`).
- `PROVISIONAL_CONCEPT_SLUG = "provisional.inventory"` — the reserved inventory slug.

`scrape_document`, `scrape_questions`, `write_tier1_problems`, `CandidateQuestion`, and
`ScrapeResult` are re-exported by the package facade (see `provisioning/_index`).

## Data flow

`scrape_document` splits oversized sections (page boundaries, then overlapping
character windows), triages once via `triage_sections`, scrapes problem-likely
sections first (widening into unlikely sections only while under `min_candidates`,
capped at `max_sections`), then de-dupes same-key candidates. `write_tier1_problems`
writes each row through `Problem.from_inventory_payload` with `tier=1` EXPLICIT under
a `provisional.inventory` concept id (satisfies the NOT-NULL `concept_id` before a real
tag exists); stage-4 `tag_and_mint` and `promote` later re-home a promoted row.

## Invariants & gotchas

- **Content-hash idempotency.** `problem_code` = stable document id + a content hash of
  the normalized problem text (`scrape.<document_id>.q<hash32>` on the structured path),
  keyed on the existing `(concept_id, problem_code)` uniqueness — a re-run inserts ZERO
  rows. The key never embeds `internal.document_chunks.id` (a re-index re-mints chunk ids).
- **`tier=1` EXPLICIT is the safety trap.** The ORM `tier` default is 2 (teachable); an
  omitted explicit `tier=1` would silently make scraped inventory selectable.
- **Fail-soft per chunk/section.** Malformed/empty LLM JSON, a non-array payload, or a
  candidate failing `CandidateQuestion` validation (e.g. out-of-range difficulty) drops
  that record and increments `parse_failures` — never a half-row, never a run abort. A DB
  error raises to the caller's transaction (the orchestrator owns commit/rollback).
- **Section-key stability.** The section content key is order-INDEPENDENT (the question's
  own text hash), not the section ordinal — the LLM is not order-stable across replays, so
  a positional key would re-bind old rows to different questions.
- **Reverse dependency:** `apollo/subjects/curriculum_db.py` imports
  `PROVISIONAL_CONCEPT_SLUG` from here — keep this export stable.

## Env flags

- `APOLLO_STRUCTURED_SCRAPE` — section-aware vs legacy per-chunk stage-1 (default on).
- Scrape bounds (`APOLLO_SCRAPE_MAX_SECTIONS` / `_MIN_CANDIDATES` / `_SECTION_CHAR_CAP`)
  come from `cost_constants` — see `provisioning/metered-chat`.

## Related

- `provisioning/scrape-sections` — the pure section reconstruction + triage that feeds `scrape_document`.
- `provisioning/metered-chat` — supplies the injected `chat_fn`/`triage_chat_fn` seams and scrape bounds.
- `apollo/persistence/models` — `Concept` / `Problem` ORM (owned there, referenced here).
- `apollo/conversation/curriculum/db` — imports `PROVISIONAL_CONCEPT_SLUG`.
