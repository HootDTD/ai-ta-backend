---
doc: campaign/turn-replay
description: Turn-level replay of recorded prod attempts through the live questioning engine, with an injectable client seam.
owns:
  - campaign/turn_replay.py
  - campaign/turn_replay_clients.py
related:
  - campaign/transcript-replay
  - apollo/conversation/questioning/unified
  - apollo/conversation/questioning/controller
  - apollo/conversation/handlers/done
last_verified: 2026-08-12
stub: false
---

# campaign/turn-replay — per-turn producer replay

Apollo P3.2 (spec `2026-08-12-apollo-p32-wrongness-signal-design.md` §4).
`transcript-replay` replays the at-Done grader; this replays the **per-turn**
producer. It feeds a recorded attempt's student turns one at a time through the
real `unified.evaluate_and_ask` with a client injected through seam **S3**,
rebuilds the question-opportunity ledger with the real controller writers, and
finishes by grading through `transcript_replay.grade_replay` — the same core the
grader gate uses, so the two harnesses can never grade differently.

Nothing here touches a database. Playback mode touches no network either, and
that is asserted rather than assumed.

## Interface

Two files, one leaf. `turn_replay_clients.py` holds the S3 seam and the network
guard; `turn_replay.py` holds the replay itself and **re-exports every client
name**, so a caller imports from `campaign.turn_replay` either way. The split is
by measurement, not preference: the single module crossed the 800-line cap and
the client plumbing is its most self-contained piece.

### `turn_replay_clients.py`

- `RecordedClient(responses, *, repeat_last=False)` / `LiveClient(factory=None)`
  — the two S3 client modes, chosen by constructor argument, never by env sniff.
  Both subclass `ReplayClient` and expose only `.chat.completions.create(**kwargs)`
  plus `.recorded` / `.requests` / `.calls`. A LIVE arm's `recorded` tuple is
  exactly what a later PLAYBACK arm replays.
- `loopback_only_sockets()` — context manager refusing every non-loopback
  connect AND `connect_ex` (`NetworkBlockedError`). Loopback stays open for the
  campaign's local Postgres/Neo4j.
- `TurnReplayError` / `NetworkBlockedError`.

### `turn_replay.py`
- `load_fixture(path)` / `load_fixtures(dir=DEFAULT_FIXTURE_DIR)` →
  `TurnReplayFixture` (`fixture_version: 1`; a mismatch raises `TurnReplayError`).
- `reconstruct_producer_responses(fixture)` → `tuple[TurnResponse, ...]`, one per
  student turn.
- `replay_recorded(fixture, ...)` / `replay_live(fixture, ...)` /
  `run(fixtures, *, live, samples)` → `FixtureReplay` (`refusal`, `turns`,
  `grade`, `ledger`).
- `turn_row` / `summary_row` / `to_jsonl` / `load_jsonl` / `compare_arms`.
- `main(argv)` — CLI (`--fixtures`, `--mode {recorded,live}`, `--samples`,
  `--allow-single-draw`, `--out`, `--compare A B`).

## Data flow

`campaign/fixtures/turn_replay/*.json` → `TurnReplayFixture` → per student turn:
`controller._build_tally_state` → `unified.evaluate_and_ask(client=…)` →
`controller._apply_tally_updates` → `_charge_ask` →
`controller._write_opportunity_audit` → `selection.build_selection_policy` →
`TurnRecord`. After the last turn: `transcript_replay.grade_replay` over the
REPLAYED ledger. Output is JSONL, one `kind:"turn"` row per turn plus one
`kind:"summary"` row per (fixture, sample).

## Reusability contract (P3.1 Phase 0 reuses this UNCHANGED)

Three things are the contract: the injected client (S3), the frozen fixture
schema, and the JSONL row schema. P3.1 **adds** per-node `credit`/`basis` fields
to the turn rows; it does not change the seam, the fixture schema, or the row
kinds. `test_jsonl_row_schema_is_the_p31_contract` pins both key sets.

## Invariants & gotchas

- **Imports the production writers, never a copy.** `_build_tally_state`,
  `_apply_tally_updates` and `_write_opportunity_audit` are the controller's own;
  a private rebuild would drift silently the moment the tally-state enum or the
  evidence shape moved. Pinned by identity assertion.
- **`_charge_ask` is the ONE mirrored rule**, because the real one
  (`controller._bump_times_asked`) is an atomic `UPDATE … RETURNING` and there is
  no database here. It copies the free-pass rule verbatim from its call site: a
  degenerate `fallback_served` serve on a node never probed before spends no
  probe; every later serve charges.
- **A replay starts with NO ledger rows**, as the attempt did, and mints them
  turn by turn. Seeding the fixture's recorded END state would start the run with
  the final `times_asked` already spent and every node already settled.
  `fixture.recorded_ledger_rows()` exposes that end state for comparison only.
- **Reconstruction, not recording.** Prod never stored the raw producer response,
  so `reconstruct_producer_responses` rebuilds it from what the controller
  PERSISTED. Two documented approximations: `status` is the row's FINAL state
  (the ledger keeps no per-turn history), and only the LAST ask per node survives
  in `asked_turn`, so an earlier ask on a re-asked node replays with no resolved
  target. **It never synthesizes student text** (§4.1).
- **Replayed evidence quotes are token-identical to the recorded ones, not
  byte-identical.** They are re-derived by today's `unified._verbatim_span`,
  whose slice ends at the last word token, so several historical prod rows lose a
  trailing `.`. Same words, same order, same source message — the 2026-07/08 rows
  simply are not fixed points of today's gate.
- **`recorded.served_score` is provenance, not an assertion target.** The
  fixtures' verdict credits were reconstructed at curation time from stored topic
  credits, and the live adjudicator path snaps credit onto
  `transcript_coverage.CREDIT_ANCHORS` (0.3→0.0, 0.9→0.85, 0.95→1.0). Replay and
  the recorded prod score therefore differ on 083/124/167 — verified NOT to be a
  harness artifact: recorded-ledger, replayed-ledger and ledger-less grading all
  agree with each other.
- **`done_gate_fired` is read off the seam** ("the model said done and the engine
  served ask"), never by importing W2-A gate internals, so it stays correct before
  and after the level-2 done-gate lands.
- **A live arm needs ≥4 samples** (`LIVE_SAMPLE_MINIMUM`, spec §4); fewer is a
  `SystemExit` unless `--allow-single-draw`. Nondeterminism at temp 0 moves 7-18%
  of letters between identical runs.
- **What replay cannot show (§4.1):** it cannot generate a student's answer to a
  question that was never asked, so a gated turn measures *gating*, never grade
  movement. Attempt 083 is the named case — do not claim the gate "kills 83".

## Related

- [campaign/transcript-replay](transcript-replay.md) (the grading core and the
  `LedgerRow` type), [apollo/conversation/questioning/unified](../apollo/conversation/questioning/unified.md)
  (the S3 seam), [apollo/conversation/questioning/controller](../apollo/conversation/questioning/controller.md),
  [apollo/conversation/handlers/done](../apollo/conversation/handlers/done.md).
