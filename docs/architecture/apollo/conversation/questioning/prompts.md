---
doc: apollo/conversation/questioning/prompts
description: Model-facing contract for the unified questioning call — system prompt, optional WRONGNESS DUTY block, JSON response schema, repair feedback strings.
owns:
  - apollo/smart_questions/prompts.py
related:
  - apollo/conversation/questioning/unified
  - apollo/conversation/questioning/selection
  - apollo/overseer/transcript-coverage
last_verified: 2026-08-12
stub: false
---

`apollo/smart_questions/prompts.py` holds **what the model is shown and the shape
it must answer in** for `questioning/unified`'s one call. Split out of
`unified.py` (P3.2) when the WRONGNESS DUTY block pushed that module past the
800-line convention. `unified` keeps the control flow; nothing here touches the
DB, an LLM, or a session.

## Interface

- `SYSTEM_PROMPT` — the system turn, unchanged from the pre-P3.2 constant
  (`MAX_ASKS_PER_NODE` interpolated from `questioning/selection`, so prose cannot
  desynchronize from the code-enforced cap).
- `build_system_prompt(*, wrongness: bool = False) -> str` — `SYSTEM_PROMPT`, plus
  `WRONGNESS_DUTY_INSTRUCTION` appended when the P3.2 producer is on.
- `response_schema(*, statuses, wrongness_values=None) -> dict` — the `json_schema`
  response format. `wrongness_values=None` is the pre-P3.2 schema.
- `MALFORMED_FEEDBACK`, `off_policy_feedback(askable_ids)` — the two repair-turn
  strings `unified._resolve_served_reply` sends on its single regenerate.

## Invariants & gotchas

- **No import from `unified`** (that would be a cycle: `unified` imports this).
  The tally-state and wrongness value sets stay single-sourced in `unified` and
  are passed in as arguments — never re-declared here, which would add a fifth
  copy of the enum whose drift fails silently (`test_tally_state_enum_sync.py`).
- **Level 0 is byte-identical by construction, not by review.** The wrongness
  block is an APPENDED headed section (the pattern `transcript_coverage`'s
  optional instructions use) and the schema fields are added only when
  `wrongness_values` is supplied, so an un-flagged call cannot differ. Both are
  sha256-pinned against `origin/staging @7c51fbe` in
  `test_unified_wrongness_schema.py`.
- **Prompt exemplars are paraphrased and invented — never real student text.**
  A quoted transcript clause would ride that pilot student's words along in every
  questioning call the service ever makes. Pinned by
  `test_prompt_block_contains_no_real_student_text`, the sibling of the
  adjudicator's `test_transcript_coverage_exemplars.py`.
- The wrongness block states the label's *negative* space explicitly —
  uncertainty, hedging, vagueness, partial answers and silence are NOT
  contradictions — because that is the dominant false-positive source. `kind` is
  declared free-form and gates nothing.
- `strict: True` + `additionalProperties: false` means every declared property
  must also be listed in `required`; `contradiction`'s optionality is therefore
  carried by its nullable type, and "required iff `wrongness != none`" is
  enforced in `unified._decode_updates`, never in the schema.

## Related

Engine and decode `questioning/unified`; ask cap `questioning/selection`; the
corroborating at-Done reader `overseer/transcript-coverage`.
