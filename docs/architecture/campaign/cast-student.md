---
doc: campaign/cast-student
description: The AI-student session driver — plays one persona through a full Apollo attempt and writes the attempt JSONL ledger.
owns:
  - campaign/cast/student.py
  - campaign/cast/__init__.py
related:
  - campaign/cast-personas
  - campaign/judges-s3-s4
  - apollo/persistence/models
  - apollo/conversation/handlers/grading-artifact-writer
  - apollo/conversation/handlers/done
  - apollo/projections/performance-insights
  - platform/config-model-pins
last_verified: 2026-08-12
stub: false
---

# campaign/cast-student — AI-student driver

The largest campaign module (~709 lines). Drives one `PersonaAttempt` through a
real Apollo session end-to-end over the student-facing HTTP routes, then reads
back the persisted `GradingRun` rows. Every real I/O boundary is an injected
seam so the flow unit-tests with fakes.

## Interface

- Protocols `ApolloClient` (`create_session`/`next`/`chat`/`done`) and
  `ArtifactReader` (`read(attempt_id) -> (canonical, pair)`); `ChatFn` type.
- `AttemptRecord` dataclass + `append_attempt_record(record, path)` — the single
  JSONL ledger writer feeding the S3/S4/S5 judges.
- `build_hoot_transcript(persona)`, `_parse_gap_detail(exc)`.
- `run_attempt(persona, *, client, chat_fn, artifact_reader, token,
  search_space_id, …) -> AttemptRecord` — create session → `_resolve_problem`
  (re-roll via `/next`) → `_play_scripted_beats` → `_play_clarification_followups`
  → `done` → `artifact_reader.read`. **Never raises** (records `status="error"`).
- `run_corpus(personas, …) -> list[AttemptRecord]` — drives many, appending each.
- Real adapters: `HttpxApolloClient`, `SqlArtifactReader`, `default_chat_fn`
  (real LLM, pragma no-cover), `mint_student_token` (Supabase admin API,
  pragma no-cover).

## Invariants & gotchas

- **`append_attempt_record` is the single ledger the downstream judges read.**
- Session creation infers concept from free-text `hoot_transcript` (no
  `problem_id` lever); `_resolve_problem` re-rolls via **`/next`** (not `/retry`,
  which never re-selects) and re-captures each re-roll's new `attempt_id`.
- A chat-route 422 `parser_could_not_extract` is a non-fatal `parse_gaps` entry,
  not an attempt failure — the attempt still proceeds to `/done` as `status="ok"`.
- **`SqlArtifactReader._row_to_payload`** reassembles the grading-artifact dict
  from the typed `GradingRun` columns (DB-14/A7 artifacts-only merge) — the
  inverse of the artifact writer; referenced cross-domain by the apollo grading docs.
- **`misconceptions` is the one key that is FILTERED, not copied.** From Apollo
  P3.2 wrongness level 1, `grader_payload -> 'misconceptions'` is a superset of
  the served array — it also carries the internal record (`shadow`-marked below
  level 3, plus `resolved` entries the XP dedup subtracts,
  [done](../apollo/conversation/handlers/done.md)). The reader imports
  `performance_insights.teacher_visible_misconception`
  ([performance-insights](../apollo/projections/performance-insights.md)) so the
  campaign measures exactly what a Done served; re-spelling it here would let a
  measuring instrument disagree with the surface it measures.

## Related

- [cast-personas](cast-personas.md) (`PersonaAttempt`), [judges-s3-s4](judges-s3-s4.md)
  (consume the ledger), [apollo/persistence/models](../apollo/persistence/models.md)
  (`GradingRun`),
  [apollo grading-artifact-writer](../apollo/conversation/handlers/grading-artifact-writer.md)
  (the inverse mapping), [platform/config-model-pins](../platform/config-model-pins.md)
  (`MAIN_MODEL`).
