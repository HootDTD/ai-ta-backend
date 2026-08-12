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
last_verified: 2026-08-12
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

## Fixture directories (data, not owned source)

- `campaign/fixtures/transcript_grader/` — the calibration set `replay_fixture`
  reads. Still README-only; `run()` on an empty directory returns not-passed.
- `campaign/fixtures/turn_replay/` — **added 2026-08-12 (P3.2 wave 0).** Four
  committed, PII-scrubbed EXACT prod attempts (083 auto-done/unasked-credit,
  086 zero-transcript I7 artifact, 124 + 167 self-correction protection) in a
  FROZEN schema (`fixture_version 1`) that the turn-level replay harness reads.
  Different schema from `transcript_grader/` — it carries the per-turn ledger
  (`question_opportunities`) the wrongness signal is produced onto, plus
  `recorded` served score/letter. Provenance, the scrub rules, the `basis`
  normalization and the regenerate recipe live in that directory's `README.md`;
  `campaign/tests/test_turn_replay_fixtures.py` enforces schema + PII as a gate.

## Invariants & gotchas

- **Reuses the serving-lane `validate_span`** rather than reimplementing it — a
  private copy would silently drift from the per-message check it mirrors.
- **`compute_topic_score` is called WITHOUT `asked_node_ids`**, so P1.2b is inert
  in replay — a turn-level harness that wants to agree with production must pass
  it.
- **Fixture prose is real student text.** It is data only: nothing under
  `campaign/fixtures/turn_replay/` may be used as a prompt exemplar (the P1
  never-quote-real-student-text rule, pinned by
  `apollo/overseer/tests/test_transcript_coverage_exemplars.py`, governs prompts).

## Related

- [apollo/overseer/transcript-coverage](../apollo/overseer/transcript-coverage.md)
  (the grader of record), [apollo/overseer/topic-score](../apollo/overseer/topic-score.md),
  [apollo/schemas/problem](../apollo/schemas/problem.md).
