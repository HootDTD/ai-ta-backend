---
doc: ai-ta-backend/platform/vendor-openai-client
description: vendors/openai_client.py — the reports-only OpenAI Chat Completions wrapper (token budgeting, retry/backoff, deterministic fake mode)
owns:
  - vendors/openai_client.py
  - vendors/__init__.py
related:
  - ai-ta-backend/reports/ai-use-service
last_verified: 2026-07-25
stub: false
---

# platform/vendor-openai-client — reports OpenAI wrapper

A narrow, **reports-specific** OpenAI client — distinct from the main QA /
embeddings OpenAI usage in `ai/`. `vendors/__init__.py` is empty glue.

## Interface

- `generate_ai_use_markdown(evidence_pack, style, length) -> dict` — returns
  `{markdown, jsonld, model_fingerprint}`; asks the model for both artifacts as
  one JSON object for reliable parsing.
- `_budget_evidence(evidence_pack, token_budget=6000)` — drops oldest turns until
  under budget and marks truncation.
- `_call_openai(messages, *, response_json, retry)` — `_RetryConfig`
  (max 5 retries, exponential backoff + jitter, 30s total timeout).
- `_estimate_tokens` (tiktoken with a ~4-chars/token fallback), `_sha256_hex16`.

## Data flow

`reports/ai_use/service.generate_report` → `generate_ai_use_markdown` → budget
evidence → `_call_openai` (JSON mode) → validate keys → fingerprint the markdown.

## Invariants & gotchas

- **Deterministic fake mode**: when `TEST_FAKE_OPENAI=1` **or** no
  `OPENAI_API_KEY`, it returns a canned markdown/jsonld/fingerprint without any
  network call — so tests and keyless envs never hit the API.
- `openai` is imported lazily inside `_call_openai` (no hard dependency at
  import time).

## Env flags

`OPENAI_API_KEY`, `REPORTS_MODEL` (default `gpt-4o-mini`), `TEST_FAKE_OPENAI`,
`REPORTS_TOKEN_BUDGET`.

## Related

Sole consumer: `reports/ai-use-service`.
