---
doc: apollo/overseer/diagnostic
description: Generates the student-facing narrative that explains — never decides — the grade.
owns:
  - apollo/overseer/diagnostic.py
  - apollo/overseer/remediation.py
  - apollo/overseer/narrative_consistency.py
related:
  - apollo/overseer/topic-narrative
  - apollo/overseer/grounding
  - apollo/overseer/topic-score
  - apollo/overseer/aside-penalty
  - apollo/overseer/rubric
  - apollo/conversation/handlers/done
last_verified: 2026-08-07
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
- `narrative_consistency.enforce_narrative_consistency(feedback, *, topics)
  -> feedback` — pure, total, idempotent verdict-consistency gate (P2.1);
  `PRAISE_FLOOR = 0.6` and `FALLBACK_HEADLINE` are its public constants.

## Data flow

Two prompt paths. When a `TopicScoreResult` is passed, it delegates to
[topic-narrative](topic-narrative.md)`.build_topic_narrative_prompt` and requests
strict structured JSON from one `MAIN_MODEL` completion via `bounded_client()`
(`agent/llm-client`, 2026-08-04 — was a bare `OpenAI()`). `handlers/done.py`
now calls `generate_diagnostic` through `asyncio.to_thread` (see `handlers/done`)
so this narrative LLM call no longer blocks the event loop. Code validates topic
keys/order, exact-gates each quote against that topic's `evidence_span`,
sanitizes every prose field, appends deterministic misconception + negotiation
entries in `recap[]`, runs the consistency gate LAST, then flattens headline →
topic notes → recap → prefixed next step for back compatibility. Otherwise it
uses the unchanged axis prompt and returns the legacy sanitized narrative plus
null feedback.

`narrative_consistency` (P2.1, 2026-08-07) is the code half of "the narrative
is written FROM the verdicts" — the prompt half lives in
[topic-narrative](topic-narrative.md). It takes the sanitized payload plus
`TopicScoreResult.topics` and, for every UNCREDITED topic, strips each
PURE-praise sentence (a credit claim or praise word with no gap named) and — when
that topic counted toward the grade — guarantees the gap is named, appending one
deterministic quoted-reference sentence if the model named none. Headline and
next step lose only pure-praise sentences that are demonstrably ABOUT an
uncredited topic; emptied fields fall back to deterministic text.

"Uncredited" is `credit < PRAISE_FLOOR`, minus one carve-out: a **Hoot-assisted**
topic (INTERACTION5) whose credit is above zero is exempt, because
[aside-penalty](aside-penalty.md)'s flat `0.5` cap is unconditionally below the
floor — its sub-floor credit is a policy penalty, not missing evidence, and
`min(evidence, 0.5)` can only reach exactly `0` from a pre-cap `0`. A
**zero-weight** topic (P1.2b `unprobed` — Apollo never asked, so it left the
denominator) still loses praise but never receives a gap sentence; a note that
stripping empties gets a neutral "did not count toward your grade" line instead,
and such a topic is never chosen as the next-step subject.

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
- **The consistency gate never decides anything and never raises.** It edits
  prose only; credits, letters, quotes, `recap[]`, `hoot_assisted`, and every
  other key pass through. With no topic under `PRAISE_FLOOR` — and for a topic
  whose note needs no repair — the payload is returned unchanged, so a fully
  credited attempt is byte-identical to the pre-P2.1 build. A defect inside it
  is caught by the same structured-path `except` as a JSON failure: legacy
  narrative, null feedback, no second completion.
- **It runs on sanitized text and its own sentences are code-owned**, quoting a
  shortened (≤90 chars, word-boundary) reference name — never re-sanitized, so
  a topic display name reaches the student as authored, and never a snake_case
  key (`humanize_key` is the no-display-name fallback). The quoted span is
  bracket-balanced (a clip landing inside a parenthetical drops it) and embedded
  double quotes become single ones, and it is quoted at most ONCE per payload:
  a next step that falls back for a topic whose note already quotes it uses the
  no-quote variant.
- **Headline/next-step praise is deleted only on strong evidence.** Emptying a
  one-sentence headline replaces the whole thing, so the sentence must share at
  least two topic-name words that appear in NO credited topic (one of them 6+
  chars; scaled down for one/two-word names) AND overlap that topic more than any
  credited one. Measured on the 14 exported prod problems with 2+ graded nodes —
  240 ledger-supported praise headlines — the earlier one-shared-word rule
  false-stripped 57.9%; this one strips 0% at unchanged recall. The trade is
  deliberate: praise naming a credited AND an uncredited topic in one sentence
  survives (the prompt half and the per-topic note repair still cover it).
- **Remediation is copy-on-success and all-or-nothing:** empty/unsafe results or
  any failure publish no `review` key. Solution-bearing snippets use the same
  metadata filter as Interaction 1; snippet quotes never enter the payload.

## Env flags

- `INTERACTION3` — remediation citations, default OFF.
- `INTERACTION_CONCEPTS` — optional normalized concept-slug allowlist; unset or
  empty preserves unrestricted flag-on behavior.

## Related

`MAIN_MODEL` is pinned in `platform/config-model-pins`.
