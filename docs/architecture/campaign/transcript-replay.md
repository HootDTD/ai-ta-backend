---
doc: campaign/transcript-replay
description: Deterministic transcript-grader fixture-replay entrypoint — the surface A7 kept.
owns:
  - campaign/transcript_replay.py
  - campaign/__init__.py
  - campaign/README.md
related:
  - apollo/overseer/transcript-coverage
  - apollo/overseer/topic-score
  - apollo/schemas/problem
last_verified: 2026-07-25
stub: false
---

# campaign/transcript-replay — deterministic grader replay

The permanent offline gate that feeds a frozen transcript fixture through the
live transcript grader. No network or DB is contacted (`_call_adjudication` is
patched with the fixture's recorded adjudicator output).

## Interface

- `ReplayOutcome(name, score, letter, credited_topics, validated_spans)` dataclass.
- `replay_fixture(path) -> ReplayOutcome` — loads a fixture (`problem`,
  `transcript`, `adjudicator_output`, `gate`), builds the KG graph via
  `Problem.to_kg_graph`, runs `compute_transcript_coverage` (patched) →
  `compute_topic_score` (+ `compute_centrality`), and validates every credited
  verdict's `evidence_span` by reusing the serving-lane `validate_span` rail.
- `_passes_gate(outcome, fixture) -> bool` — applies the fixture's expected-grade
  gate (min/max score, max credited topics, require-validated-spans).
- `run(fixtures: Path) -> (list[ReplayOutcome], bool)` — runs a fixture dir,
  aggregating pass (empty dir → not-passed).
- `main() -> int` — CLI (`--fixtures`).

## Data flow

Consumes fixtures under `campaign/fixtures/transcript_grader/` (data, not owned).
`README.md` documents the retained campaign surface after the A7 removals.

## Invariants & gotchas

- **Reuses the serving-lane `validate_span`** rather than reimplementing it — a
  private copy would silently drift from the per-message check it mirrors.

## Related

- [apollo/overseer/transcript-coverage](../apollo/overseer/transcript-coverage.md)
  (the grader of record), [apollo/overseer/topic-score](../apollo/overseer/topic-score.md),
  [apollo/schemas/problem](../apollo/schemas/problem.md).
