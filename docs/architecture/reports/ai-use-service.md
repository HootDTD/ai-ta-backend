---
doc: reports/ai-use-service
description: reports/ai_use/service.py — the evidence-pack build, PII redaction/excerpting, usage classification, and report generation (pure logic, no persistence)
owns:
  - reports/ai_use/service.py
related:
  - platform/vendor-openai-client
  - reports/ai-use-routes
last_verified: 2026-07-25
stub: false
---

# reports/ai-use-service — evidence pack + generation

Pure logic (no DB writes — persistence lives in `ai-use-routes`).

## Interface

- `build_evidence_pack(chat_id, style, length, *, chat_loader) -> dict` —
  assembles redacted, excerpted chat turns plus tool calls, file references,
  prompt hashes, and model fingerprints into the evidence structure. Requires an
  injected `chat_loader` (tests inject their own).
- `classify_usage(evidence_pack) -> List[str]` + `_classify_usage_from_text` —
  keyword heuristics mapping turns onto a fixed closed tag set (brainstorming,
  outlining, editing, translation, debugging, summarising, coding-help,
  math-derivation, data-cleaning), stable order.
- `generate_report(evidence_pack, style, length) -> dict` — delegates markdown/
  jsonld generation to `vendors.openai_client.generate_ai_use_markdown` and
  attaches the pack's `tool_calls` + `prompt_hashes`.
- Redaction/PII helpers: `redact` (sk-keys, bearer tokens, api-key patterns),
  `excerpt` (1000-char cap), `sha256_hex`/`_sha256_hex16`, `extract_file_refs`
  (parses `[Textbook, p. 12]`-style markers), `_summarize_tool_inputs`,
  `_estimate_tokens` (tiktoken with fallback).

## Data flow

`chat_loader(chat_id)` → per-turn redact + excerpt → hash user prompts, extract
file refs from assistant turns, summarize tool inputs → drop oldest turns until
under `EVIDENCE_TOKEN_BUDGET` → the pack; `generate_report` then calls the vendor
client (which enforces its own downstream token budget).

## Invariants & gotchas

- **Student content is redacted + excerpted before it leaves the service** —
  secrets/tokens are stripped and each turn is capped at 1000 chars.
- Token budgeting happens twice: an evidence-pack guard here plus the vendor
  client's own budget; a fully-dropped pack surfaces `truncated=True`.

## Env flags

`EVIDENCE_TOKEN_BUDGET` (default 8000).

## Related

`platform/vendor-openai-client` (generation delegate), consumed by
`reports/ai-use-routes`.
