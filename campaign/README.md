# Apollo campaign tooling

The retained campaign surface exercises Apollo's live teaching loop and the
permanent transcript grader. The abandoned graph-grader replay, paired-artifact
comparison, configuration freeze, and report adapters were removed by A7 on
2026-07-20.

Use `campaign/transcript_replay.py` for deterministic transcript-grader fixture
replay. Persona casts, subject fixtures, ingestion checks, and the S1–S5 judge
helpers remain available under `campaign/cast/`, `campaign/infra/`, and
`campaign/judges/`.
