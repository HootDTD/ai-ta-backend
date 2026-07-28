---
doc: apollo/conversation/handlers/intent
description: apollo/handlers/intent.py — the chat intent classifier + confirmation gate that routes "I'm done" to grading
owns:
  - apollo/handlers/intent.py
related:
  - apollo/conversation/handlers/chat
  - apollo/conversation/agent/llm-client
last_verified: 2026-07-27
stub: false
---

# handlers/intent — chat intent classifier

`apollo/handlers/intent.py` is the Item-#5 AgentTutor-style state machine that
lets a conversational "I'm done teaching" reach `handle_done` and classifies
restart/next/return-to-Hoot/help intents behind a confirmation gate.

## Interface

- `classify_intent(*, utterance, history, concept) -> IntentVerdict` — one
  `cheap_chat` call → `Intent` label + confidence; guarded by `_safe_intent` /
  `_safe_confidence`. Soft-fails CLOSED to `teaching`/0.0 on any error.
- `detect_confirmation(utterance) -> ConfirmationVerdict` — deterministic
  yes/no regex check (no LLM); mixed signal → ambiguous.
- `confirmation_prompt_for(intent) -> str` — the confused-student-voice
  confirmation prompt (empty string for `teaching`/`off_topic`).
- `Intent`, `IntentVerdict`, `ConfirmationVerdict`, `ALL_INTENTS`,
  `INTENT_CONFIDENCE_THRESHOLD` — imported by `handlers/chat`'s intent SM.

## Data flow

`handlers/chat` calls `detect_confirmation` when a `pending_intent` is set, and
`classify_intent` + `confirmation_prompt_for` otherwise. Above-threshold
non-teaching intents arm `pending_intent`; only `done` currently has a wired
executor; other intents log and fall through to teaching.

The label set is always `teaching`, `done`, `restart`, `next`,
`return_to_hoot`, `help`, and `off_topic`. `reference_question` is deliberately
absent from `Intent`, `ALL_INTENTS`, and `_CLASSIFIER_PROMPT` under every flag
state. The INTERACTION4 hint lane is an explicit `ask_hoot` request handled
before this classifier by `handlers/chat`.

## Invariants & gotchas

- **Any non-affirmative reply to a pending confirmation clears `pending_intent`**
  and proceeds as a normal teaching turn (`ConfirmationVerdict.ambiguous` and
  rejection both fall through).
- Conservative bias: ambiguous classification returns `teaching` — silent
  misclassification is preferred over hijacking a teaching turn.
- `off_topic` deliberately falls through (never gated) so on-topic teaching is
  never blocked.
- `reference_question` is treated as any unknown model output and coerced to
  `teaching`; neither `INTERACTION4` nor `INTERACTION_CONCEPTS` changes the
  classifier prompt.

## Env flags

- `APOLLO_CHEAP_MODEL` — the model behind `cheap_chat` (resolved in
  `agent/llm-client`; named here, not valued).

## Related

Consumed by `handlers/chat`; LLM call via `agent/llm-client` (`cheap_chat`).
The explicit hint-lane contract is owned by `handlers/chat`, outside this
classifier.
