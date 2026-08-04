---
doc: apollo/overseer/diagnostic
description: Generates the student-facing narrative that explains — never decides — the grade.
owns:
  - apollo/overseer/diagnostic.py
  - apollo/overseer/remediation.py
related:
  - apollo/overseer/topic-narrative
  - apollo/overseer/grounding
  - apollo/overseer/topic-score
  - apollo/overseer/rubric
  - apollo/conversation/handlers/done
last_verified: 2026-08-04
stub: false
---

# Overseer diagnostic — explains the verdict

`generate_diagnostic` produces the short student-facing narrative that explains
the grade. It never decides the grade — the [rubric](rubric.md) /
[topic score](topic-score.md) is the verdict; this only narrates it.

## Interface

- `generate_diagnostic(*, coverage, reference_steps, problem_text, rubric,
  model=None, topic_score=None, student_utterances=(), course_evidence=None)
  -> (narrative, feedback_or_none)` — imported by `handlers/done.py`. Topic
  feedback is `{headline, topic_feedback: [{canonical_key, note, quote,
  hoot_assisted}], recap, next_step}`.
- `add_remediation_reviews(*, db, search_space_id, topic_score, feedback,
  grounding_bundle) -> decorated_feedback_or_none` — copy-on-success citation
  decoration for at most three `partial`/`missing` topics.

## Data flow

Two prompt paths. When a `TopicScoreResult` is passed, it delegates to
[topic-narrative](topic-narrative.md)`.build_topic_narrative_prompt` and requests
strict structured JSON from one `MAIN_MODEL` completion via `bounded_client()`
(`agent/llm-client`, 2026-08-04 — was a bare `OpenAI()`). `handlers/done.py`
now calls `generate_diagnostic` through `asyncio.to_thread` (see `handlers/done`)
so this narrative LLM call no longer blocks the event loop. Code validates topic
keys/order, exact-gates each quote against that topic's `evidence_span`,
sanitizes every prose field, appends deterministic misconception + negotiation
entries in `recap[]`, then flattens headline → topic notes → recap → prefixed
next step for back compatibility. Otherwise it uses the unchanged axis prompt
and returns the legacy sanitized narrative plus null feedback.

`course_evidence` (INTERACTION2, supplied by `handlers/done.py` from
[grounding](grounding.md)) is forwarded to the topic-narrative builder ONLY.
The axis prompt is the soft-fail fallback and stays frozen — grounding must not
change the shape of a degraded narrative.
With `INTERACTION3` enabled and the problem concept allowed by
`INTERACTION_CONCEPTS`, `done.py` passes successful structured feedback to
`remediation.py`. A non-null session grounding bundle is reused exclusively;
only a null bundle triggers fresh per-topic retrieval (`top_k=3`, 800 tokens).
The helper returns citation-only `{doc_id, label, page, upload_id}` pointers — `upload_id` (int `app.uploads.id` from chunk metadata `teacher_upload_id`, None on pre-storage paths or junk values) lets the UI link the chip to the stored source PDF via `GET /materials/file-url`.

## Invariants & gotchas

- **Soft-fail:** an LLM exception yields the fixed unavailable narrative; a
  JSON parse/validation failure preserves the same completion's raw text as the
  legacy narrative. Both return null feedback and never raise into grading.
- **Quotes are code-gated:** a quote survives only when it exactly equals the
  gated `evidence_span` for its canonical topic and is already sanitizer-clean;
  otherwise it becomes null.
- **Attribution rules** match the topic narrative: address the student as
  "you"/"your"; never present a reference detail as something the student said.
- **`topic_feedback[].hoot_assisted` (INTERACTION5) is code-injected from the
  ledger, never the LLM** — copied from each `TopicCredit.hoot_assisted` so the
  flat Hoot-assist cap can't be argued away by prose. `False` for un-assisted
  topics and absent-safe; it survives `remediation.py`'s copy of the feedback.
- **Recap lines are deterministic:** the topic path puts the existing appender
  outputs in code-owned `recap[]`; the axis/soft-fail path appends them to the
  legacy string exactly as before.
- `sanitize_narrative` runs per structured prose field and at the legacy return
  boundary. It leaves `[Marker, p. N]` citations intact, so a grounded note can
  carry a real citation through to the served payload.
- **`course_evidence=None` (the default, and what an OFF flag or NULL bundle
  produces) keeps both prompt paths byte-identical to the pre-INTERACTION2
  build**, so grounding can never silently move a grade.
  boundary.
- **Remediation is copy-on-success and all-or-nothing:** empty/unsafe results or
  any failure publish no `review` key. Solution-bearing snippets use the same
  metadata filter as Interaction 1; snippet quotes never enter the payload.

## Env flags

- `INTERACTION3` — remediation citations, default OFF.
- `INTERACTION_CONCEPTS` — optional normalized concept-slug allowlist; unset or
  empty preserves unrestricted flag-on behavior.

## Related

`MAIN_MODEL` is pinned in `platform/config-model-pins`.
