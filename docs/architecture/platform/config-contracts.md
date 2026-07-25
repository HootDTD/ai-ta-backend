---
doc: ai-ta-backend/platform/config-contracts
description: config/contracts.py — the shared QA/solver dataclass contracts (ResearchBundle et al.) that flow through the whole /ask pipeline
owns:
  - config/contracts.py
related:
  - ai-ta-backend/platform/http-server
  - ai-ta-backend/rag-pipeline/retrieve-pipeline
  - ai-ta-backend/rag-pipeline/context-packer
  - ai-ta-backend/rag-pipeline/main-ai
last_verified: 2026-07-25
stub: false
---

# platform/config-contracts — shared QA data contracts

**Single authority on these shapes.** A pure data-contract module (no I/O); the
QA pipeline passes these objects end to end, so every consuming doc links here
instead of restating fields.

## Interface

- `ParsedTask` — parsed question (`problem_type`, asked outputs/keys, knowns,
  constraints, figure refs); `validate()` requires a `problem_type` and at least
  one of outputs/knowns/constraints.
- `BundleSnippet` — one retrieved chunk with its `citation_marker`; `validate()`
  requires a non-empty marker.
- `ResearchMetadata` — the wide retrieval-trace record (query iterations, term
  presence, k-counts, `allowed_markers`, …).
- `ResearchBundle` — **the core retrieval payload** (`metadata` + `snippets` +
  equations/glossary/known-values/…); `validate()` enforces non-empty snippets,
  a snippet floor for quantitative/multi-output tasks, an equations-or-glossary
  requirement, and derives `allowed_markers` from snippet markers when absent.
- `ProposedSolution`, `FinalAnswer`, `Proof` — solver-stage payloads.
- `Violation(Exception)` — raised on validation failure.
- `__all__` also re-exports `dataclasses.asdict`.

## Data flow

`server.py` builds a `ResearchBundle` in `_ask_pgvector`; it flows through
`retrieval/pipeline` + `context_packer` (which mints `citation_marker`s) into
`ai/main_ai` (`solve_with_bundle`/`format_answer`).

## Invariants & gotchas

- **Changing a field here ripples across the QA pipeline** — verified importers:
  `ai/main_ai`, `ai/orchestrator`, `ai/router/wiring`, `chats/bundle_cache`,
  `retrieval/context_packer`, `retrieval/pipeline`, `server.py`.
- `BundleSnippet.validate()` allows a page-less marker (e.g. `[Slides]`) — a page
  reference is preferred but not required.

## Related

`http-server`, `rag-pipeline/retrieve-pipeline`, `rag-pipeline/context-packer`,
`rag-pipeline/main-ai` all consume these shapes.
