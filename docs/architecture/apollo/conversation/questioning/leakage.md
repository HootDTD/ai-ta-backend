---
doc: apollo/conversation/questioning/leakage
description: Log-only private-atom leakage belt for the questioning reply, plus the loop's single text normalizer.
owns:
  - apollo/smart_questions/leakage.py
related:
  - apollo/conversation/questioning/unified
  - apollo/conversation/agent/output-filter
  - apollo/ontology/graph
last_verified: 2026-08-07
stub: false
---

`apollo/smart_questions/leakage.py` classifies one candidate Apollo reply against
the **private** reference graph. It was carved out of `questioning/unified` when
that file crossed the repo's 800-line ceiling; the behaviour is unchanged.

## Interface

- `belt_verdict(reply, *, reference_graph, public_text, student_messages) -> BeltVerdict`
  — the entry point, called by `questioning/unified` on the draft and (when there
  is one) the regenerated reply.
- `BeltVerdict` (frozen): `malformed`, `digits`, `private_vocabulary`,
  `private_phrases`, plus derived `hit` / `offending_atoms`.
- `leaks_private_content(...) -> bool` — shorthand for `belt_verdict(...).hit`.
- `private_strings(reference_graph) -> list[str]` — every string reachable in the
  private graph (nodes **and** edges, so edge labels count as private).
- `normalized(text) -> str` and `WORD_RE` — the loop's one text key.

## Data flow

`public_text` (the problem statement) plus every student message form the **safe**
token set: anything the student or the public prompt already said may be echoed.
Three atom classes are then flagged in the reply — tokens containing a digit,
tokens ≥3 chars that appear in `private_strings` and are not function words
(`_FUNCTION_WORDS`, deliberately small: it is not a vocabulary allowlist), and
whole normalized private strings ≥4 chars appearing inside the reply. `malformed`
is a separate, non-leakage judgment: the reply must contain exactly one `?` and
end with it.

## Invariants & gotchas

- **Telemetry only.** A hit is logged (`belt_hit_served`) and the reply is served
  anyway. Nothing here rewrites or blocks — contrast the dead
  `agent/output-filter`, which raised. Only `malformed` changes control flow, and
  it does so in `questioning/unified`, not here.
- **`normalized` is shared on purpose.** The belt and the engine's evidence /
  repeated-question checks must agree on what "the same words" means; two copies
  would drift.
- Word tokens are ASCII-only (`[a-zA-Z0-9]+`), so accented or non-Latin private
  vocabulary is not caught by the token classes — the phrase class still catches
  it when the whole string is echoed.
- The module is pure: no DB, no LLM, no I/O.

## Related

Sole caller `questioning/unified`; dead hard-filter predecessor
`agent/output-filter`; `KGGraph` authority `ontology/graph`.
