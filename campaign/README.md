# Apollo campaign tooling

The retained campaign surface exercises Apollo's live teaching loop and the
permanent transcript grader. The abandoned graph-grader replay, paired-artifact
comparison, configuration freeze, and report adapters were removed by A7 on
2026-07-20.

Persona casts, subject fixtures, ingestion checks, and the S1–S5 judge helpers
remain available under `campaign/cast/`, `campaign/infra/`, and
`campaign/judges/`.

## Two replay harnesses, one grading core

| | Replays | Fixtures |
|---|---|---|
| `campaign/transcript_replay.py` | the **at-Done** transcript grader | `campaign/fixtures/transcript_grader/` |
| `campaign/turn_replay.py` | the **per-turn** questioning producer, then grades through the same core | `campaign/fixtures/turn_replay/` |

Both patch the adjudicator seam with the fixture's recorded output and contact
no network or database. An empty fixture directory raises `SystemExit` naming
the directory — it is a harness defect, not a failing gate.

### Transcript-grader replay

```
python -m campaign.transcript_replay --fixtures campaign/fixtures/transcript_grader
```

Fixture schema: `{problem, transcript: [{role, content}], adjudicator_output:
{verdicts: [...]}, gate}`, plus the optional additive `question_opportunities`
ledger. Supplying the ledger activates P1.2b (`asked_node_ids`) exactly as
production does; omitting it reproduces the pre-P1.2b arithmetic.

### Turn replay

```
# PLAYBACK (deterministic, no network) over the four committed prod attempts
python -m campaign.turn_replay --out arm-a.jsonl

# LIVE per-turn LLM arm — >=4 samples, per the house rule
APOLLO_WRONGNESS_LEVEL=1 python -m campaign.turn_replay --mode live --samples 4 --out arm-l1.jsonl

# diff two arms
python -m campaign.turn_replay --compare arm-a.jsonl arm-l1.jsonl
```

Output is JSONL: one `kind:"turn"` row per replayed student turn (raw producer
response, decoded tally updates including the wrongness label, the resolved
selection policy, the action/target, and whether the done-gate fired) plus one
`kind:"summary"` row per (fixture, sample) carrying the at-Done grade and the
replayed ledger.

**What turn replay cannot show** (spec §4.1): it cannot generate a student's
answer to a question that was never asked in the recording, so a gated turn
measures *gating*, never grade movement. Do not read a fixture's
`recorded.served_score` as an assertion target either — those credits were
reconstructed at curation time and the live path snaps credit onto
`CREDIT_ANCHORS`.

See `docs/architecture/campaign/turn-replay.md` and
`docs/architecture/campaign/transcript-replay.md`.
