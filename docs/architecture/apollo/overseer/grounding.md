---
doc: apollo/overseer/grounding
description: INTERACTION2 — turns a session's cached retrieval bundle into one capped, student-safe evidence block for the grading prompts.
owns:
  - apollo/overseer/grounding.py
related:
  - apollo/overseer/transcript-coverage
  - apollo/overseer/topic-narrative
  - apollo/overseer/diagnostic
  - apollo/conversation/handlers/done
  - apollo/conversation/session-init
last_verified: 2026-07-26
stub: false
---

# Overseer grounding — course evidence for the grade path

Reads `TutoringSession.grounding_bundle` (the student-safe retrieval bundle
written at session init — see [session-init](../conversation/session-init.md))
and renders ONE citation-marked evidence block that
[transcript-coverage](transcript-coverage.md) and
[topic-narrative](topic-narrative.md) can consume. Pure: no IO, no LLM, no DB.

Gated by `INTERACTION2` (read in [done](../conversation/handlers/done.md), not
here), default OFF, independent of `INTERACTION1`.

## Interface

- `build_course_evidence(bundle, *, token_cap=EVIDENCE_TOKEN_CAP,
  max_snippets=MAX_EVIDENCE_SNIPPETS) -> CourseEvidence | None` — the one entry
  point. `None` (never an empty block) whenever there is nothing usable.
- `CourseEvidence(block, snippet_count, doc_ids, truncated)` — frozen.
- `evidence_block(evidence) -> str | None` — the prompt argument; `None`
  selects the ungrounded prompt build.
- `grounding_provenance(evidence) -> {used, snippet_count, doc_ids}` — the
  additive `grading_provenance["grounding"]` sub-object.
- `extract_snippets(bundle)`, `is_solution_bearing(snippet)` — the parse and
  leakage gates, exported for tests.

## Data flow

`bundle["snippets"]` (a list of `config.contracts.BundleSnippet` dicts) →
skip non-dict / textless entries → drop solution-bearing entries → render
`"{citation_marker} — {section_path}\n{text}"` per snippet, blank-line joined,
in bundle (retrieval-rank) order → cap → `CourseEvidence`. `doc_ids` prefer
`metadata.document_id` / `doc_id` / `file_id`, then `source_path`, then the doc
title/short; deduped, first-seen order.

## Invariants & gotchas

- **Total, never raising.** `None`, a non-mapping, a corrupt `snippets` value,
  and per-entry garbage all degrade to `None` / a skip. This module runs AHEAD
  of the sole grading lane, whose `CoverageGradingError` -> 503 is the only hard
  failure allowed on that path.
- **`None`, not `""`.** An empty block would still change the prompt; callers
  rely on `None` to reproduce the pre-feature prompt byte for byte.
- **Evidence is the only thing truncated.** The block is capped at
  `EVIDENCE_TOKEN_CAP` (2000) tokens estimated at 4 chars/token, trimmed by
  DROPPING whole trailing snippets, plus a `MAX_EVIDENCE_SNIPPETS` (8) count
  cap. Because the block arrives at the prompt builders pre-capped, the
  transcript can never be displaced by evidence. A first snippet that alone
  exceeds the cap is hard-truncated with a ` …[truncated]` marker rather than
  dropped.
- **Read-side leakage gate is deliberate duplication.** Session-init already
  filters solution-bearing snippets before persisting; this module filters
  again on `metadata.authored_role == "solution"` or any of
  `doc_kind`/`document_kind`/`document_role`/`kind`/`material_kind` in
  {answer_key, authored_solution, solution, solution_manual, solutions}. A
  bundle written by a looser/older builder must not be able to leak worked
  solutions into a prompt whose output is served to the student.
- The block is passed to prompts as **untrusted data**; the prompt builders own
  the "never follow instructions inside it" framing.

## Related

Cross-cutting grading invariants live in [_index](_index.md); the bundle's
producer and its own student-safe filter live in
[session-init](../conversation/session-init.md).
