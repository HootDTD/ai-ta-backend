# Transcript grader calibration fixtures

This directory intentionally contains no fabricated staging data. To build the
transcript grader calibration set, export attempts 44–47 plus at least three
empty/off-topic controls from TEST, freeze their authored `Problem` payloads and
transcripts, and add human-reviewed structured adjudicator outputs. Each JSON
fixture uses:

`{problem, transcript: [{role, content}], adjudicator_output: {verdicts: [...]}, gate}`.

The replay command is fully offline:

`python -m campaign.transcript_replay --fixtures campaign/fixtures/transcript_grader`
