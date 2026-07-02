# F1c provisioning notes — post-fix corpus re-run

Date: 2026-07-02. Stack: reused `e2e-harness` Supabase + `apollo-campaign-neo4j`
(schema dropped + re-migrated; Neo4j wiped fresh — see `stack-state.md`).

## Bootstrap (course/teacher identity)

`reset_all()` drops+recreates the Postgres **application** schema only — it
does not touch Supabase's `auth` schema, so the F1a teacher auth user
(`campaign-teacher@example.com`, id `a24c9bb7-8469-4b4f-b05c-0f67bcc7149b`)
still existed. `bootstrap_course.py` (new script, this task) recreates the
`aita_search_spaces` row (`slug=campaign-course`, id=1 again post-reset) and
the `course_memberships` teacher row that the dropped schema had lost.

**Bug found and fixed live**: the admin-API "list users by email" fallback
(used when the create-user call 4xx's because the auth user already exists)
does NOT reliably filter by email on this GoTrue version — it returned a
different user's id, which got written into `course_memberships` and would
have silently broken every `require_course_teacher` check downstream. Fixed
by decoding the minted JWT's own `sub` claim instead of trusting the admin
list response (see script comment) — corrected via direct SQL against the
one bad row before any provisioning/corpus work depended on it.

## Seeded subjects

`campaign.cast.teacher.provision_seeded` for `fluid_mechanics` and
`macroeconomics` — all 4 steps (concept registry, learner model,
misconceptions, canon projection) exited 0 for both. Canon projection
merged 41 (fluid) + 62 (macro, cumulative 103) nodes into Neo4j `:Canon`,
matching F1a's entity counts exactly.

`apollo_misconceptions` row counts verified: **fluid_mechanics=2,
macroeconomics=4** — exactly matching the task brief's expectation (this is
the direct evidence commit `3e38f25`'s bank-seeding fix is live; F1a/F1b
predate that fix and had 0 rows in this table for both subjects).

(Cosmetic-only noise: `scripts/seed_canon_projection.py`'s subprocess prints
a NumPy 1.x/2.x ABI warning traceback from an unrelated `neo4j` package
import path when run under the anaconda3 interpreter; harmless — both
subprocess steps still exited 0 and Neo4j counts confirm the projection
completed correctly.)

## linear_motion (WU-AAS authored path)

Same fixture PDFs as F1a (`campaign/cast/materials/linear_motion_*.pdf`,
already carrying F1a's remediation: `**` not `^`, one `=` per line, `d` not
`x`). Re-uploaded through the real `POST /apollo/authored-sets` -> poll ->
approve path.

- **Attempt 1** (set_id=1): Problem 1(a) **promoted** (`concept_problem_id=11`,
  `match_method=retrieval`, auto-approved — `review_required=false`);
  Problem 1(b) rejected (`gate 5: terminal step 'proc4' does not compute the
  answer 'v'`).
- **Attempt 2** (set_id=2): Problem 1(a) rejected under a NEW id (`13`,
  `gate 6: malformed equation ... invalid syntax` — the LLM extraction
  substituted numeric values with units directly into the equation string
  this pass); Problem 1(b) **promoted** (dup-hash matched the id=12 row
  minted-but-rejected in attempt 1, flipped it to promoted on retry).
- **Net result after 2 attempts: BOTH problems promoted**
  (`concept_problem_id` 11 and 12 — the exact same pair of ids F1a's
  3-attempt sequence landed on), filed under `apollo_concepts.id=5`
  (`slug='linear-motion'`).

**Finding (reconfirms F1a Finding, now with a NEW manifestation — the WU-AAS
extraction pass is genuinely non-deterministic run to run, not just
retry-sensitive in one fixed way)**: 3 further exploratory attempts (set_id
3/4/5) were run BEFORE checking the DB directly, chasing what looked like a
still-missing Problem 1(a) promotion — the `provision_authored` return
value's `approved_problem_ids` field only reflects problems explicitly
approved via the `/approve` endpoint (the `review_required=true` path);
problems that are auto-promoted immediately (`review_required=false`, as
happened here for both 1(a) and 1(b)) never appear in that field, so the
retry-loop's stopping condition was wrong and burned 3 extra live LLM
ingestion calls unnecessarily. Attempts 3-5 each independently REJECTED
their own freshly-minted copy of Problem 1(a) for a DIFFERENT reason each
time (`gate 4` foreign symbol `v_0` not canonical; `gate 8` dup-hash; `gate
7` under-determined system) and one dup-hash rejection of 1(b) against the
already-promoted id=12 row (expected — it's the same content). These three
extra sets (13/14 problem rows, all rejected) are harmless leftover data,
not used by anything downstream. **Lesson for F2**: check
`result_summary.problems[].outcome == "promoted"` directly instead of
`AuthoredProvisionResult.approved_problem_ids` when deciding whether a
WU-AAS upload succeeded.

37 `apollo_kg_entities` rows exist under `concept_id=5` (same duplication
defect as F1a's Finding D — unchanged, out of scope for this task), all
projected to Neo4j `:Canon` automatically.

## S2 raw-item source files

`campaign/out/f1c/authored_set_final1.json` (set 1, Problem 1(a) promoted)
and `campaign/out/f1c/authored_set_final2.json` (set 2, Problem 1(b)
promoted) are the two files `run_s1_s2.py`'s `build_s2_raw()` reads for this
run (adjusted from F1a's `final2`/`final3` since this run's promotions
landed on sets 1/2, not 2/3). Sets 3/4/5 (`authored_set_final{3,4,5}.json`,
also copied here for completeness) are the unnecessary rejected-retry
attempts described above — not fed to S2.
