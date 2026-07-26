---
doc: campaign/scripts-run-s1-s2
description: The S1/S2 raw-input harness runner — builds the JSON the S1/S2 judges consume, reading internal ingest tables directly.
owns:
  - campaign/scripts/run_s1_s2.py
  - campaign/scripts/__init__.py
related:
  - campaign/judges-s1-s2
  - database/supabase-migrations
last_verified: 2026-07-25
stub: false
---

# campaign/scripts-run-s1-s2 — S1/S2 raw-input harness

The CANONICAL runner that assembles S1/S2 judge inputs from the real campaign
stack and runs them through the real `OpenAIJudgeClient`.

## Interface

- `parse_subjects(specs) -> dict[str, list[int]]` — `subject_key:cid[,cid...]`.
- `_fetch_subject_graph(...)` + `build_s1_raw(pg_dsn, subject_concepts) ->
  list[dict]` — pull the reference KG per subject/concept from
  `app.learner_entities` + `internal.entity_prerequisites` + `app.problems`.
- `_fetch_page_evidence(pg_dsn)`, `_fetch_run_ids_by_document(pg_dsn, ids)`,
  `_document_ids_from_fixtures(paths)`, `build_s2_raw(out_dir, page_evidence,
  run_id_by_document)` — assemble the ingestion page-evidence rows for S2 from
  `internal.ingest_page_evidence` (migration 036) + `internal.content_ingest_runs`.
- `dump(result, path)` writes UTF-8 JSON; `run(pg_dsn, out_dir, subject_concepts)`
  orchestrates (dumps S1 immediately so a S2 crash never loses it); `main()` CLI.

## Invariants & gotchas

- **Emits `DEPENDS_ON` for every prereq row** — `PRECEDES` is legal ONLY for
  `(procedure_step, procedure_step)` pairs (`apollo/ontology/edges.py`); the
  mislabel drove 26/57 S1 failures in the frozen f1/f1c runs.
- Reads the `internal.*` ingest/evidence tables directly (DB-12);
  `_fetch_run_ids_by_document` resolves each set's real ingest run via
  `problem_document_id` (not a positional "set N == run N" assumption).
- Missing `internal.ingest_page_evidence` (pre-036 DB) degrades gracefully to
  thin-input behavior instead of crashing.
- LOCAL campaign DSN only (default `127.0.0.1:57322`).

## Related

- [judges-s1-s2](judges-s1-s2.md), [database/supabase-migrations](../database/supabase-migrations.md)
  (the `internal` schema it reads).
