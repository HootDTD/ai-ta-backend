---
doc: apollo/provisioning/metered-chat
description: The metered LLM client (the only token signal in provisioning) plus its cost/config table.
owns:
  - apollo/provisioning/metered_chat.py
  - apollo/provisioning/cost_constants.py
related:
  - apollo/provisioning/_index
  - apollo/provisioning/scrape
  - platform/config-model-pins
last_verified: 2026-07-25
stub: false
---

# provisioning/metered-chat

`MeteredChat` is the ONLY programmatic token signal in §8B provisioning.
`apollo/agent/_llm.py` reads `response.usage` only to log it and returns `str`, so
`MeteredChat` re-invokes the OpenAI client itself, captures usage, accumulates it onto
the per-document ingest-run row, and enforces a per-document ceiling. `cost_constants`
is the pure config table backing it.

## Interface

- `MeteredChat(*, ingest_run, client=None, ceiling=…, document_id=None)` — the metered client.
  - `.cheap(*, purpose, messages, response_format=None, temperature=0.0, model=None, reasoning_effort=None)` — cheap-tier call (`cheap_chat`-shaped).
  - `.main(...)` — main-tier call (`main_chat`-shaped; routes to `config.models.MAIN_MODEL`).
  - `.scrape_chat_fn(system_prompt)` — adapter for the positional-string `chat_fn(text)->str` scrape seam.
  - `.cumulative_tokens()` / `.record_usage(*, model, usage)` — read-only aggregate + one-call accrual.
- `CostBudgetExceeded` — raised at the ceiling (carries `tokens`, `ceiling`, `document_id`).
- `cost_usd_for(model, *, tokens_in, tokens_out) -> Decimal`, `MODEL_PRICES`, `PER_DOCUMENT_TOKEN_CEILING`.
- `structured_scrape_enabled()`, `structure_pairing_mode()` — per-call flag readers.
- Scrape bounds `APOLLO_SCRAPE_MAX_SECTIONS` / `APOLLO_SCRAPE_MIN_CANDIDATES` / `APOLLO_SCRAPE_SECTION_CHAR_CAP`.

`MeteredChat`, `CostBudgetExceeded`, `PER_DOCUMENT_TOKEN_CEILING`, `MODEL_PRICES`, and
`cost_usd_for` are re-exported by the package facade (`provisioning/_index`).

## Data flow

Every provisioning LLM call goes through a `MeteredChat` tier. `.cheap`/`.main` mirror
`_llm`'s keyword shape so a stage written against an injected `chat_fn` plugs in unchanged.
After each call `record_usage` does `+=` onto `ingest_run.llm_calls / llm_tokens_in /
llm_tokens_out / llm_cost_usd`, then compares the cumulative (in+out) total against the
ceiling, raising `CostBudgetExceeded` on breach (the breaching call's counts ARE recorded
first). The orchestrator flushes/commits the ingest row and maps `CostBudgetExceeded` to a
content-ingest error + failed run.

## Invariants & gotchas

- **The ingest-run row is mutated IN PLACE** via SQLAlchemy attribute assignment — this is
  the INTENDED durable ORM write, not a value-object mutation. The orchestrator owns the txn.
- **The ceiling is a runaway circuit breaker, not a tight budget.** The generous default is
  env-overridable (`APOLLO_PROVISION_TOKEN_CEILING`); a normal chapter scrapes well below it.
- **`cost_usd_for` never raises:** an unknown model returns `Decimal('0')` (token counts
  still accrue). `Decimal` is load-bearing — the cost column is `NUMERIC(12,6)`.
- **Security:** the OpenAI client reads the key from `OPENAI_API_KEY` (SDK); no key is ever
  an argument or logged. Structured logs carry purpose/model/token-COUNTS/ids only — no bodies, no PII.
- **DRIFT (deprecated):** `MAX_ATTEMPTS` and its `queue.fail_job` docstring describe the
  removed async provisioning queue/dead-letter cap — there is NO live consumer in
  provisioning scope. Treat `MAX_ATTEMPTS` as vestigial config (the package facade still
  re-exports it for historical compatibility).

## Env flags

- `APOLLO_CHEAP_MODEL` (default `gpt-4o-mini`) / `config.models.MAIN_MODEL` — the two tiers.
- `APOLLO_PROVISION_TOKEN_CEILING` — per-document token circuit breaker.
- `APOLLO_STRUCTURED_SCRAPE` — read by `structured_scrape_enabled()`.
- `APOLLO_STRUCTURE_PAIRING` (`off`/`shadow`/`on`, unknown → `off`) — read by `structure_pairing_mode()`.
- `APOLLO_PROVISION_MAX_ATTEMPTS` — feeds the vestigial `MAX_ATTEMPTS` (deprecated).

## Related

- `provisioning/scrape` — consumes the scrape bounds and the `scrape_chat_fn` seam.
- `platform/config-model-pins` — owns `config/models.py` (`MAIN_MODEL`).
