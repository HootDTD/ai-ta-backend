---
doc: rag-pipeline/context-packer
description: Greedy token-budget packing of ranked chunks into BundleSnippets with citation markers.
owns:
  - retrieval/context_packer.py
related:
  - rag-pipeline/citations-formatter
  - rag-pipeline/main-ai
  - platform/config-contracts
  - platform/config-settings
last_verified: 2026-07-26
stub: false
---

# context-packer — token-budget packing + citation markers

## Interface

- `pack_context(ranked_chunks, token_budget=6000, citation_label=None) -> list[BundleSnippet]`
- `_summarize_snippets(snippets) -> (equations, glossary, assumptions, boundary_conditions)`
  — regex extraction (`_EQ_PATTERN`/`_GLOSSARY_PATTERN`/`_ASSUME_PATTERN`); also
  reused by `router-wiring._assemble_bundle`.

## Data flow

Iterate ranked chunks, accumulate until 85% of budget
(`_TOKEN_BUDGET_FRACTION`; `_count_tokens` uses tiktoken `cl100k_base`, falling
back to `len/4`), dedupe by `chunk_id`. When `doc_title` is present, its first 30
characters are the label in every marker shape (`[Title, p. N]`,
`[Title, Week W, p. N]`, or `[Title]` without a page). Untitled chunks retain the
kind label: `_citation_label_for_kind` maps `material_kind` through
`citations.formatter.DOC_TYPE_LABELS`; the textbook label falls back to
`get_citation_label()` (`platform/config-settings`).

## Invariants & gotchas

- **Citation markers are created ONCE here**, carried on
  `BundleSnippet.citation_marker`, whitelisted into `bundle.allowed_markers`, and
  enforced later in `main-ai.format_answer`. Empty markers are rejected by
  `BundleSnippet.validate()`.

## Env flags

`CITATION_LABEL` (textbook label, via `platform/config-settings`).

## Related

`citations-formatter` (DOC_TYPE_LABELS), `main-ai` (marker enforcement),
`platform/config-contracts` (BundleSnippet).
