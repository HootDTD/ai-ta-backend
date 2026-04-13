# Spike A — Parser Accuracy Dataset

**Purpose:** measure what fraction of real student utterances about Bernoulli the spike parser (LLM-only) extracts into valid KG entries, silently misreads, or cleanly rejects.

**Target:** 30–50 utterances by Monday of Week 2. Measure on Monday–Tuesday.

**Thresholds to proceed (spec §5, Spike A):**
- ≥60% correctly extracted into valid KG entries
- ≤10% silent failure (extracted as something the student didn't say)

## Sourcing (priority order)

1. **Existing Hoot fluid mechanics chat logs.** Mine for student messages that look like explanations (not questions). Owner: [ISHAAN to confirm access path with cofounder by Week 1 Wed]. Target: 20+ utterances from real Hoot users.
2. **Recruited physics students.** 3–5 students record a 10-min "teach me Bernoulli" audio clip; transcribe. Target: 10–15 utterances across students.
3. **Mock transcripts (last resort).** Weakest signal. Use only to round the count up to 30 if sources 1–2 are thin.

## Dataset format

Each row in the final dataset is a dict:
```json
{
  "id": "U001",
  "source": "hoot_logs | recorded_student_X | mock",
  "utterance": "...",
  "expected_entries": [ /* zero or more KG entries the utterance SHOULD yield, hand-labeled */ ],
  "notes": "why this utterance is interesting or representative"
}
```

## Seed examples (to expand)

1. Clean equation statement:
   *"Bernoulli's equation is P plus one-half rho v squared plus rho g h is constant along a streamline."*
   Expected: 1 equation entry.

2. Condition statement:
   *"This only works if the fluid is incompressible."*
   Expected: 1 condition entry.

3. Informal variable-mapping:
   *"By pressure I mean the static pressure in the pipe."*
   Expected: 1 variable_mapping entry.

4. Messy multi-claim:
   *"So if the pipe is horizontal, the height doesn't matter and velocity goes up when area goes down."*
   Expected: 1 simplification + 1 equation (continuity implied).

5. Wrong equation (parser should extract as-stated, not correct):
   *"Bernoulli says pressure times volume equals a constant."*
   Expected: 1 equation entry with the wrong formula.

6. Non-physics chatter (parser should ignore):
   *"Sorry, my dog is barking, give me a sec."*
   Expected: 0 entries.

## Measurement procedure (Week 2 Monday)

1. Load each utterance into a script that calls `apollo.spike.spike_parser.parse_utterance`.
2. For each utterance, compare extracted entries to `expected_entries`:
   - **Correct:** all expected entries present and no extra incorrect ones
   - **Silent failure:** extra entry NOT in expected (parser hallucinated) — WORST case
   - **Miss:** expected entry absent (parser didn't catch it)
   - **Rejected cleanly:** zero entries on an utterance that should yield zero
3. Tally percentages. Record failure modes for Week 2 report.

## Outstanding

- [ ] Confirm Hoot logs access
- [ ] Dataset filled to ≥30 rows by end of Week 1
- [ ] Script `apollo/spike/run_spike_a.py` drafted Week 2 Monday
