---
doc: apollo/provisioning/scrape-sections
description: Pure section reconstruction plus fail-open problem-likelihood triage feeding stage-1 scrape.
owns:
  - apollo/provisioning/section_grouping.py
  - apollo/provisioning/section_triage.py
related:
  - apollo/provisioning/scrape
  - apollo/provisioning/metered-chat
last_verified: 2026-07-25
stub: false
---

# provisioning/scrape-sections

Phase-2 section preparation for `scrape_document`. The layout-aware indexer emits
line/phrase-level retrieval micro-chunks (median ~17 chars) with `chunk_type='heading'`
markers and `section_path` labels; per-chunk scraping wasted ~92% of tokens and never
saw a whole problem. `section_grouping` regroups those micro-chunks into whole-document
sections; `section_triage` ranks the sections so scrape hits problem-likely ones first.

## Interface

- `Section` — frozen reconstructed-section dataclass (title, document_id, page span, text, `source_content_hash`, `member_chunk_ids`).
- `group_into_sections(chunk_rows) -> list[Section]` — regroup id-ordered micro-chunks into sections.
- `section_content_hash(text) -> str` — normalized-content sha256 (survives a re-index).
- `SectionVerdict` — per-section triage result (`is_problem_likely`, `priority`, concept guess).
- `triage_sections(sections, *, chat_fn) -> list[SectionVerdict]` — one cheap LLM ranking pass.
- `build_triage_payload(sections) -> str` — the indexed title+stats JSON handed to the triage `chat_fn`.

## Data flow

`group_into_sections` walks `chunk_rows` in ascending `id` order (the orchestrator's load
order): a heading chunk or a change in non-empty `section_path` opens a new section; body
chunks accumulate into `text` (heading lines excluded, they become `title`).
`triage_sections` sends the section titles + light stats through one cheap LLM pass and
returns a verdict per section that `scrape_document` sorts on (problem-likely first, then
priority desc, then original order).

## Invariants & gotchas

- **Both modules are pure** — no DB, and `section_grouping` has no LLM at all. Chunk rows
  are duck-typed (`id`, `content`, `document_id`, `page_number`, `section_path`, `chunk_type`).
- **Triage FAILS OPEN.** A malformed/empty/non-array triage response yields every section
  at equal priority (degrading to an exhaustive scrape) — triage NEVER aborts the run. A
  section the model omits defaults to problem-likely so the fallback still covers it.
- **The concept guess is a HINT only.** Stage-4 `tag_and_mint` stays the authoritative
  concept resolver; the triage guess just seeds the scrape `concept_hint`.
- The triage `chat_fn` is the positional-string `MeteredChat.scrape_chat_fn` seam.

## Related

- `provisioning/scrape` — the stage-1 consumer that splits/triages/scrapes these sections.
- `provisioning/metered-chat` — supplies the metered cheap-tier triage `chat_fn`.
