# F-struct Structural Co-key — Live Validation, 2026-07-09

**What was validated:** the structural co-key misconception docking path
(F-struct, flag `APOLLO_MISC_STRUCT_COKEY`, default OFF). Spec:
`docs/_archive/specs/2026-07-09-apollo-misconception-struct-cokey-design.md`.
Commit: `cb67e5c` (branch `feat/apollo-misc-struct-cokey`). Harness:
`campaign/validate_misconception_detector.py`, `mode=full_judge`
(real OpenAI judge, `used_real_judge=True`), NLI resolver tier **off**. n = 20
labeled attempts (macroeconomics `nominal_vs_real_gdp`-anchored set), 0 harness
errors on both runs.

## Infra note

Both runs were blocked at first by a Windows WinNAT reserved-port collision
(the same blocker documented in the prior `task-11-report.md`: campaign
Postgres/Neo4j fixed ports fell inside `netsh interface ipv4 show
excludedportrange` bands). Resolved by re-porting the local stack out of the
reserved ranges rather than fighting WinNAT: Supabase `e2e-harness` →
`15320-15329`, Neo4j → `15687` (bolt) / `15474` (http); DSNs updated in
`.env.campaign` (untracked, local-only).

## Run 1 — partial seed

Migration 038 applied via `campaign.infra.apply_migrations`. The
`seed_apollo_misconceptions` seeder was run for
`macroeconomics/nominal_vs_real_gdp`, but it seeded **only**
`search_space_id=1` / `concept_id=3` — the seeder defaults to the MIN
`search_space` for a subject slug, so `concept_id=10` (the other reference
node in the labeled set) stayed unseeded (`opposes` NULL).

| Metric | Result |
|---|---|
| Struct-docks | 1/3 (attempt 88 PASS; 95 and 112 miss — `concept_id=10` unseeded, no `opposes` input) |
| Control FP | 0/4 |
| False-Strong (misconception-class) | baseline=7 → after-penalty=3 (`{95, 100, 112}`) |

## Run 2 — complete seed

A 4-row local `UPDATE` mirrored the faithful authored seed onto concepts 9/10
(`nominal_for_real → def.real_basis`, `deflate_wrong_direction →
eq.gdp_deflator`) — a local-only DB fix, not a code/migration change.

| Metric | Result |
|---|---|
| Struct-docks | 3/3 (88, 95, 112 — `gate_row=row3s_struct_cokey_dock`, `docked_via=struct_opposes`, `struct_opposes_code=nominal_for_real`) |
| Attempt 100 | also docks via the struct path (previously self-named at baseline); judge went unkeyed — the judge-commitment coin-flip is neutralized by design (structural path names it either way) |
| Control FP | 0/4 (attempt 89: opposes index entry present + judge verdict `clear` → no dock, per invariant 4) |
| False-Strong (misconception-class) | baseline=7 → **run1=3 → run2=0** |
| Harness within-run false-Strong measure | 7 → 0 |
| Misconception-class detection | 13/16 (`>=1 misconceptions_found`) |

Misses: attempt 102 (fluids, no authored `opposes` for that concept —
expected, out of scope for this seed); attempts 114/115 (`expected_misconceptions
= []` by design — not a recall gap).

## Judge stability observation

Comparing per-node verdicts run1↔run2 (~80 nodes total): 4 node-level flips.
Only one changed the attempt-level outcome: attempt 115's confidence collapsed
1.0000 → 0.5955 (3 docks → 0), which matches the authored expectation (`[]`)
better than the run1 result did. Logged as a raw finding for future
judge-calibration work — not a defect introduced by F-struct.

## Known gaps / follow-ups

1. **`seed_apollo_misconceptions` has no `--source-subject-slug` override**
   (unlike `seed_apollo_learner_model`) — cloned courses
   (`macroeconomics-v2qa-s3`/`-s4`) silently no-op (`entries_upserted=0`).
   Durable fix: mirror the learner-model seeder's override pattern.
2. The v2qa bank rows (concepts 9-12) used in this validation were created by
   an earlier unrecorded one-off bulk clone, not by the seeder — this run
   backfilled `opposes` onto them directly rather than re-running a seed.
3. Only `nominal_for_real` and `deflate_wrong_direction` (macro) have authored
   `opposes` today; a bank-wide `opposes` audit across all subjects is
   explicitly deferred.
4. `concept_id=9` was seeded but live-unexercised — no attempt in this 20-set
   hits it.

## Artifacts

Local-only (left untracked in the working tree, not committed — matching the
prior `2026-07-08-misconception-detector-validation.md` run's convention):

- `campaign/out/misconception_trace.flagoff-baseline.jsonl` (flag-OFF baseline)
- `campaign/out/misconception_trace.run1-partial-seed.jsonl` (run 1)
- `campaign/out/misconception_trace.jsonl` (run 2 / final)
- `out11-run1.json`, `out11.json` (harness stdout capture, run 1 and run 2)
- `out11-run1.log` / `out11.log` (gitignored, `*.log`); `.env.campaign`
  (gitignored, `.env.*`)

`campaign/out/misconception_trace_summary.json` and
`campaign/out/v2-qa-2026-07-08/` are pre-existing untracked artifacts from a
prior, unrelated session — left as found, not part of this validation.
