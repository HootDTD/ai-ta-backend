---
doc: campaign/cast-personas
description: Persona fixture schema and validation guarding the persona JSON corpus against the real reference-key universe.
owns:
  - campaign/cast/personas/schema.py
  - campaign/cast/personas/validate.py
  - campaign/cast/personas/__init__.py
related:
  - campaign/cast-student
  - campaign/judges-s3-s4
  - campaign/judges-s5
last_verified: 2026-07-25
stub: false
---

# campaign/cast-personas — persona schema + validation

A persona = scripted student beats + the expected grading ledger for one
(subject, concept, problem) triple. Four archetypes: `strong`, `partial`,
`misconception`, `vague_then_clarifies`.

## Interface

- `schema.py`: `ExpectedLedger` (`credited`/`unresolved`/`misconceptions` key
  lists + `expects_clarification`; credited∩unresolved must be disjoint;
  `to_ledger_dict()`) and `PersonaAttempt` (persona, subject, concept, problem_id,
  system_prompt, scripted_beats, clarification_policy, expected). Constants
  `PERSONA_ARCHETYPES`, `CLARIFICATION_POLICIES`.
- `validate.py`: `iter_persona_files(base)`, `load_persona_file(path)`,
  `reference_keys_for(subject, concept, problem_id) -> set[str]`,
  `misconception_keys_for(subject, concept) -> set[str]`,
  `validate_persona(persona) -> list[str]`, `validate_all(base) -> dict[Path, list[str]]`.
  Resolves the key universe from the ON-DISK reference graph, never hand-minting.

## Invariants & gotchas

- Reference keys come from real subject data under `apollo/subjects/<subject>/`,
  EXCEPT WU-AAS `PROVISIONAL_SUBJECTS` (`linear_motion`), whose keys live under a
  hand-authored `campaign/cast/personas/<subject>/reference/` tree until the real
  set is minted (a reconciliation follow-up flagged in `campaign/README.md`).
- `vague_then_clarifies` personas MUST set `expected.expects_clarification=True`.
- An authoring typo fails loudly (`validate_all`) rather than silently poisoning
  the S3/S4 audits.

## Related

- [cast-student](cast-student.md) (replays personas), [judges-s3-s4](judges-s3-s4.md)
  (`ExpectedLedger`), [judges-s5](judges-s5.md) (misconception keys).
