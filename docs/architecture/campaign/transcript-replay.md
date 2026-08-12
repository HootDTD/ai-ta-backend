---
doc: campaign/transcript-replay
description: Deterministic transcript-grader fixture-replay entrypoint plus the shared offline grading core.
owns:
  - campaign/transcript_replay.py
  - campaign/__init__.py
  - campaign/README.md
related:
  - apollo/overseer/transcript-coverage
  - apollo/overseer/topic-score
  - apollo/schemas/problem
  - apollo/conversation/handlers/done
  - campaign/turn-replay
last_verified: 2026-08-12
stub: false
---

# campaign/transcript-replay — deterministic grader replay

The permanent offline gate that feeds a frozen transcript fixture through the
live transcript grader. No network or DB is contacted (`_call_adjudication` is
patched with the fixture's recorded adjudicator output).

## Interface

- `LedgerRow` — a **mutable** `QuestionOpportunity` stand-in, duck-typed against
  `done._probed_node_ids`, `done._latest_student_quote` and
  `controller._build_tally_state`. Mutable on purpose: the production writer
  `controller._apply_tally_updates` assigns `state`/`evidence` in place, and a
  frozen row would force `turn_replay` to reimplement the one function it exists
  to exercise. `evidence` is a `list`, never a tuple — both production readers
  gate on `isinstance(value, list)` and yield nothing for any other sequence.
- `ledger_rows(payload) -> list[LedgerRow] | None` — fresh rows from a fixture's
  `question_opportunities` (deep-copied); `None` in, `None` out.
- `ReplayOutcome(name, score, letter, credited_topics, validated_spans,
  asked_node_ids, topic_credits)` dataclass. The last two are additive (P3.2):
  `asked_node_ids` is reported so a replay can never again claim to agree with
  production while silently running P1.2b OFF.
- `fixture_paths(directory) -> tuple[Path, ...]` — every `*.json`, or
  `SystemExit` naming the directory. An empty/missing directory is a HARNESS
  defect, not a failed gate.
- `grade_replay(*, problem, transcript, adjudicator_output, rows=None,
  wrongness_candidates=None, name="")` — the shared core, also called by
  `campaign/turn_replay.py`. Uses `compute_transcript_coverage_with_spans` (the
  call `handlers/done` makes), so the coverage dict and the narrative spans come
  from ONE adjudication. `wrongness_candidates` (seam S5) rides straight through
  so a level-≥1 campaign arm exercises the corroboration lane offline.
- `replay_fixture(path) -> ReplayOutcome` — loads a fixture (`problem`,
  `transcript`, `adjudicator_output`, `gate`, and the ADDITIVE optional
  `question_opportunities`) and hands it to `grade_replay`.
- `_passes_gate(outcome, fixture) -> bool` — applies the fixture's expected-grade
  gate (min/max score, max credited topics, require-validated-spans).
- `run(fixtures: Path) -> (list[ReplayOutcome], bool)` — runs a fixture dir,
  aggregating pass.
- `main() -> int` — CLI (`--fixtures`).

## Data flow

Consumes fixtures under `campaign/fixtures/transcript_grader/` (data, not owned).
`README.md` documents the retained campaign surface after the A7 removals.
`campaign/turn_replay.py` imports `LedgerRow`, `fixture_paths`, `ledger_rows`
and `grade_replay` from here — one grading core, two harnesses.

## Fixture directories (data, not owned source)

- `campaign/fixtures/transcript_grader/` — the calibration set `replay_fixture`
  reads. Still README-only; `run()` on an empty directory now raises `SystemExit`
  naming it (it used to fold into `passed=False`, which reads as "the fixtures
  regressed" when in fact none were ever exported).
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
- **P1.2b is no longer inert in replay (CORRECTED 2026-08-12, P3.2 W2-C).** This
  module used to call `compute_topic_score` with neither `asked_node_ids` nor
  `evidence_spans`, so the 2026-08-07 bimodal-fix denominator scoping was
  silently OFF here and replay graded by different arithmetic than production.
  Both now come from the SAME producers production uses — `done._probed_node_ids`
  (imported, never copied) and `transcript_coverage.narrative_evidence_spans`
  (via `compute_transcript_coverage_with_spans`). A fixture with no
  `question_opportunities` key still yields `asked_node_ids=None`, which
  `compute_topic_score` documents as reproducing the pre-fix arithmetic exactly,
  so every pre-P3.2 fixture is unaffected. Pinned by
  `campaign/tests/test_transcript_replay_asked_nodes.py`.
- **`MIN_GRADED_DENOMINATOR = 2` makes P1.2b structurally inert on a rubric with
  ≤2 graded nodes** — the floor re-admits every dropped node. Every committed
  turn-replay fixture has one or two, so the regression test widens a real
  authored payload to four graded nodes rather than asserting on a fixture that
  cannot move.
- **`evidence_spans` are display-only**: they populate `TopicCredit.evidence_span`
  and never move the score.
- **The numeric-only twin `compute_transcript_coverage` now has NO non-test
  caller.** Replay switched to `..._with_spans` because the twin returns no
  verdicts, so `evidence_spans` would otherwise need a second adjudication or a
  private copy of `narrative_evidence_spans`. The corroboration lane the twin's
  `wrongness_candidates` docstring mentions stays reachable — through
  `grade_replay`'s own pass-through, pinned by
  `test_wrongness_candidates_ride_through_to_the_corroborator`.
- **Fixture prose is real student text.** It is data only: nothing under
  `campaign/fixtures/turn_replay/` may be used as a prompt exemplar (the P1
  never-quote-real-student-text rule, pinned by
  `apollo/overseer/tests/test_transcript_coverage_exemplars.py`, governs prompts).

## Related

- [apollo/overseer/transcript-coverage](../apollo/overseer/transcript-coverage.md)
  (the grader of record), [apollo/overseer/topic-score](../apollo/overseer/topic-score.md),
  [apollo/schemas/problem](../apollo/schemas/problem.md),
  [apollo/conversation/handlers/done](../apollo/conversation/handlers/done.md)
  (`_probed_node_ids`, the single P1.2b producer),
  [campaign/turn-replay](turn-replay.md) (the per-turn harness built on this core).
