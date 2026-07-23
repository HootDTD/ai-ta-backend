# DB cutover / rollback runbook

Date: 2026-07-21 · Owner: DB-18 (release gate) · Status: rehearsed locally,
NOT applied to any remote project

Authority: `.planning/cleanup/db-redesign-plan.md` section 8 (cutover and
rollback runbook) + section 10 row DB-18 + section 13 (acceptance criteria);
`.planning/cleanup/DECISIONS.md` section 2 (env-var removal list);
`.planning/cleanup/db-plan-amendments-2026-07-20.md` A5 (maintenance-window
condition) and A7 (graph-grader abandonment, five Railway vars);
`docs/_archive/handoffs/2026-07-16-db-history-repair-handoff.md` (history
repair remote steps, referenced not repeated below).

Every step below is labeled **AGENT-DONE** (rehearsed locally in this branch,
with an evidence pointer into `styx/handoffs/db18-report.txt` or a cited
DB-16/DB-17/DB-05 test file) or **HUMAN** (a remote/production action no
agent may execute — per workspace rule, agents never link to, push to, reset,
or query a remote Supabase project). Nothing in this document has been run
against `supabase-test` (`hjevtxdtrkxjcaaexdxt`) or prod
(`uinkseewnxvumrxksnew`).

## 0. Preconditions

1. **HUMAN — A5 quiescence check (binding, non-negotiable).** The
   `learning_activities` cutover requires **zero in-flight Apollo tutoring
   sessions** before the copy/reconcile step runs. Raw integer session IDs
   are embedded in live client URLs; the copy's ID remap (chat IDs copied
   as-is, tutoring IDs offset `+1,000,000`) means any session a student has
   open mid-cutover breaks the moment its old integer ID resolves to a
   *different* row after remap. Verify quiescence by:
   - confirming the maintenance-window write-pause (step 3 below) is already
     in effect for *all* traffic, not only writes — Apollo session polling/
     resume must also be blocked at the load balancer or app layer during
     the window, not just POST/PATCH routes;
   - querying `apollo_sessions` (legacy) / the pre-cutover session table for
     any row with an "active"/non-terminal status and a `last_touched_at`
     inside the maintenance window before it started; escalate and do not
     proceed if any exist;
   - this is a stronger condition than "no writes" — a paused-but-open tab
     still holds a raw integer ID the student may resume after cutover, so
     the maintenance-window messaging/UX must make sessions terminate, not
     merely pause, or the id-remap must be deferred to a later external-ID
     hardening pass (explicitly out of scope for this branch per A5).
2. **HUMAN — backup/PITR confirmation.** Confirm a restorable backup or PITR
   checkpoint exists for the target project (test first, prod second) dated
   at or after the last schema change, and record its reference/timestamp in
   the change ticket before any remote step below.
3. **HUMAN — maintenance window scheduled**, with a named deployer and a
   named rollback owner (may be the same person), per plan section 8.3 step
   1.
4. **AGENT-DONE — local rehearsal evidence exists and is current.** See
   section 1 below and `styx/handoffs/db18-report.txt` for the exact
   commands and numbers. Do not start a real maintenance window on rehearsal
   evidence older than the current `supabase/migrations/` + `scripts/db/`
   tree — re-run section 1 if either changed since the evidence was
   captured.

## 1. Local Docker rehearsal (AGENT-DONE — this task)

All of the following were run in `ai-ta-backend/.worktrees/code-cleanup-massive`
against the worktree's local Supabase Docker stack (project id
`e2e-harness`) and/or Testcontainers Postgres. Full transcripts, exact
counts, and checksums are in `styx/handoffs/db18-report.txt`; do not
duplicate them here — this section is the runbook's pointer into that
evidence, organized in the order a human would want to re-verify it.

1. **Two clean resets, zero drift.** `node scripts/db/reset-local.mjs` run
   twice from a clean state (with `SUPABASE_BIN` pointed at the pinned
   2.109.0 native binary — see report for the exact local Windows setup
   note); `node scripts/db/check-migration-drift.mjs` clean on both runs;
   `supabase db dump --local` schema captured after each reset and diffed
   with `scripts/db/compare-schema-dump.mjs` — see report for the exact
   diff-tool output and object count.
2. **Forward rehearsal (reset → seed → forward chain + copy → reconcile,
   same session).** `tests/database/test_copy_app_schema_v1.py::
   test_forward_copy_reconcile_idempotency_and_reverse_delta` (DB-05's own
   rehearsal test, reused verbatim, not re-authored) applies
   `legacy_public_snapshot` → `create_app_schema_v1` → the scrubbed
   42-table seed fixture (`tests/database/fixtures/db05_legacy_seed.sql`) →
   `copy_app_schema_v1` → `reconcile_copy.sql`, all on one `asyncpg`
   connection (the quarantine table is `pg_temp`, so splitting sessions
   silently loses the quarantine accounting — this is exactly the
   MIG-AMEND round-1 hazard the brief calls out, and the fixture's
   `copied_conn` fixture structurally cannot split them: copy+reconcile run
   inside one `pytest_asyncio.fixture` on one held connection). Asserts
   exact mapped counts across all 28 target tables, a representative sample
   of promoted/typed columns, the id-remap rule (`chat` IDs as-is,
   `tutoring` IDs `+1,000,000`, applied consistently to
   `tutoring_messages`/`problem_attempts`/`question_opportunities`), and
   re-running copy+reconcile a second time is a byte-identical no-op
   (idempotency).
3. **Reverse rehearsal (`rollback_reverse_copy.sql` restores legacy
   pre-copy state, checksums verified).** The same test then simulates
   post-cutover target-only writes (a new course/document/chat activity/
   tutoring activity/two provisioning runs), reverse-copies them into the
   legacy `public` tables under a session-local `db05.rollback_watermark`,
   and asserts each legacy row exists with the correct remapped identity
   (session id `-1,000,000` for tutoring-derived rows). Reconciliation is
   re-run afterward and the identity-sequence repair is asserted directly
   (`app.courses_id_seq` next value strictly above `max(id)`).
4. **DB-16 teardown rehearsal — cited, not duplicated.**
   `tests/database/test_remove_legacy_public_schema.py` (Testcontainers
   Postgres): forward chain → copy → `remove_legacy_public_schema.sql`,
   proving preconditions pass on a correctly-copied DB, the full 42-table
   child-first DROP order succeeds with zero `CASCADE`, and a tampered
   precondition (a deleted copied row, an out-of-band no-copy table) aborts
   before any DROP runs. Green run cited from `styx/handoffs/db16-report.txt`
   (commit `2bc782c`): 4 passed (`test_teardown_succeeds_on_correctly_copied_database`,
   `test_teardown_aborts_when_a_copied_row_is_missing`,
   `test_teardown_aborts_when_a_no_copy_table_is_missing`,
   `test_teardown_script_drop_order_is_full_child_first_and_never_cascades`
   [static]).
5. **DB-17 index-hygiene rehearsal — cited, not duplicated.**
   `tests/database/test_drop_duplicate_indexes.py` (4 passed: exact-13
   removal, reapply-is-no-op, no-transaction-wrapped-CONCURRENTLY, every
   drop names its surviving twin) and
   `tests/database/test_db17_index_allowlist.py` (3 passed: complete
   `app`/`internal` index inventory matches the pinned allowlist exactly,
   zero duplicate indexes on any table, FK coverage matches the pinned
   `KNOWN_UNCOVERED_FKS` gap set — 60/75 FKs leftmost-covered, 15 pinned
   as a tracked, non-blocking gap per plan section 13's acceptance criteria,
   which gates advisor output only on `mutable-search-path`/`open-anon`/
   `initplan`, not `unindexed_foreign_keys`). Green runs cited from
   `styx/handoffs/db17-report.txt` (commit `e21b2267...`).
   `scripts/db/unused_index_review.sql` was also manually executed against a
   real Postgres schema and confirmed to run clean (documented review
   query, not an automated drop — do not run it as part of cutover; it is a
   post-soak, 30-day-later step, see section 4 below).
6. **Extension relocation / HNSW validity (plan section 7).** Covered by
   DB-06's own rehearsal (empty-search-path tests, old/new result parity,
   HNSW validity) — out of this task's re-verification scope; not
   re-rehearsed here. If DB-06's evidence predates the current migration
   tree, re-run its cited tests before trusting this runbook's later steps.
7. **Backend unit/integration/e2e tests + patch coverage ≥95% against
   `origin/staging`.** See section 5 (e2e coverage statement) and
   `styx/handoffs/db18-report.txt` for the diff-cover report and exact
   percentage.
8. **Both rollback paths.** Old-code switchback (section 3's "before writes
   resume" path) requires no rehearsal beyond "legacy `public` tables are
   never mutated by anything through `activate_app_schema_v1`" — true by
   construction (`copy_app_schema_v1` is `INSERT ... SELECT` only, asserted
   by `test_copy_migration_is_non_destructive_and_names_exactly_seven_drops`
   in the DB-05 test file: no `DELETE`/`TRUNCATE`/`DROP TABLE`/`ALTER TABLE`
   token appears in the executable SQL). The reverse-copy-of-target-writes
   path is rehearsal item 3 above.

## 2. Test-project rehearsal (HUMAN — plan section 8.2)

Do not skip to production. Every one of the following is a **HUMAN, remote**
step; an agent must never execute any command in this section.

1. Confirm the `supabase-test` project ref (`hjevtxdtrkxjcaaexdxt`), create/
   verify a restorable backup, and capture: `migration list`, a schema-only
   dump, row counts/checksums, function definitions, index definitions,
   grants/policies, extension version/schema, and `get_advisors` output.
2. Perform the history snapshot/repair from plan section 5.1 on test —
   follow `docs/_archive/handoffs/2026-07-16-db-history-repair-handoff.md`
   verbatim (H2 capture, then H4 repair-on-test). Stop if the post-repair
   migration list differs from local.
3. Apply only `create_app_schema_v1` and `copy_app_schema_v1` using the
   pinned CLI (2.109.0). Do not deploy `remove_legacy_public_schema` or any
   cleanup. Run `reconcile_copy.sql` in read-only mode (its own transaction
   ends in `ROLLBACK`, so this is safe to run without a follow-up commit
   step — the only durable side effect is identity-sequence repair, which
   is intentional and non-transactional in Postgres).
4. Deploy target-aware backend code to test with user writes paused. Run
   `activate_app_schema_v1` and smoke the student/teacher matrices, worker
   queues, reporting, grading (transcript grader + topic score — the graph-
   grader shadow chain no longer exists per A7), retrieval, and explicit
   cross-tenant denial.
5. Resume test writes for a soak. Compare error rate, query latency, RLS/
   advisor output, index usage, queue depth, retrieval quality, and old/new
   aggregate counts. Inject at least one record through every live write
   path.
6. **Rollback drill (blocking gate — production is blocked until this
   passes):** pause writes, reverse-copy the test-only delta using
   `rollback_reverse_copy.sql` with a watermark set to the moment
   `activate_app_schema_v1` ran, deploy old code, and prove old reads work
   against the restored legacy tables. Then restore target code and re-copy
   (a second application of `copy_app_schema_v1` is idempotent — see
   rehearsal item 2 above).

## 3. Production cutover (HUMAN — plan section 8.3)

1. Schedule the maintenance/write-pause window (section 0.3). Confirm PITR/
   backup restore, the test-project evidence from section 2, migration
   checksums, current prod migration list, zero unexpected schema drift
   (`compare-schema-dump.mjs` against the reviewed snapshot), and the named
   deployer/rollback owner.
2. **Reconcile history** (if not already done — see the history-repair
   handoff) then apply `create_app_schema_v1` only. Validate the extension/
   index gates from plan section 7. If any assertion differs from the test
   run, **stop** — do not improvise remote DDL.
3. Re-confirm the A5 quiescence check (section 0.1) immediately before this
   step, not just at window scheduling time. Pause user/background writes,
   drain/stop workers, take final source counts/checksums, and run
   `copy_app_schema_v1` in one bounded transaction. Because volumes are
   tiny (pilot; reseeded 2026-07-14), prefer a clean final re-copy over
   dual-write races — this matches how the rehearsal test exercises
   idempotent re-copy.
4. Run `reconcile_copy.sql` **in the SAME `psql` session as the
   `copy_app_schema_v1` step above — this is non-negotiable.** The copy
   writes its quarantine/accounting rows into a `pg_temp` table
   (`db05_copy_quarantine`), which is session-scoped: if the copy and the
   reconcile run in two different `psql` connections, the quarantine table
   is silently empty when reconcile reads it and the reconciliation passes
   on incomplete accounting (this is the exact MIG-AMEND round-1 hazard —
   splitting the sessions loses the quarantine accounting with no error).
   Run both from one interactive `psql` (or one `psql -f copy.sql -f
   reconcile.sql` invocation on a single connection); do not reconnect
   between them, and do not run reconcile from the MCP/console while the
   copy ran from a CLI session. The gate is: exact mapped counts/checksums
   for all 28 target tables, zero orphan FKs, zero tenant-attribution
   exceptions, valid constraints/indexes, exactly 7 intentionally-uncopied
   source tables present and untouched, `pg_temp.db05_copy_quarantine`
   inspected (zero rows expected on a clean copy; any row is a hard stop),
   and a successful representative old/new query comparison. Do not proceed
   past a single reconciliation failure — the script raises a hard exception
   per mismatched mapping by design (rehearsed in section 1 item 2).
5. Deploy target-aware backend, apply `activate_app_schema_v1`, run
   authenticated smoke tests as two tenants plus teacher/student roles,
   then run retrieval and grading checks (transcript grader + topic score
   only — no graph-grader shadow chain to smoke per A7). Keep writes paused
   until all pass.
6. **Deploy + env-var checklist**, applied together with the code deploy in
   this step, verbatim from `.planning/cleanup/DECISIONS.md` section 2 plus
   the five A7 vars (`.planning/cleanup/db-plan-amendments-2026-07-20.md`):

   | Railway prod backend var | Action | Source |
   |---|---|---|
   | `APOLLO_DONE_GATE_ENABLED` | DELETE | DECISIONS.md §2 (8-var list) |
   | `APOLLO_SMART_QUESTIONS_ENABLED` | DELETE | DECISIONS.md §2 |
   | `RETRIEVAL_BACKEND` | DELETE | DECISIONS.md §2 |
   | `APOLLO_NLI_ENABLED` | DELETE | DECISIONS.md §2 |
   | `APOLLO_MISCONCEPTION_DETECTOR` | DELETE | DECISIONS.md §2 |
   | `APOLLO_MISC_STRUCT_COKEY` | DELETE | DECISIONS.md §2 |
   | `APOLLO_EMERGENT_MAP_CAPTURE` | DELETE | DECISIONS.md §2 |
   | `APOLLO_CLARIFICATION_ENABLED` | DELETE | DECISIONS.md §2 |
   | `APOLLO_GRAPH_SIM_SHADOW_ENABLED` | DELETE | A7 (5-var list) |
   | `APOLLO_GRAPH_GRADER_LIVE` | DELETE | A7 |
   | `APOLLO_GRAPH_CONTRACTION_ENABLED` | DELETE | A7 (A7 text calls this "GRAPH_SIM_CONTRACTION"; exact Railway name confirmed in `.planning/cleanup/inputs/prod-env.md`) | 
   | `APOLLO_ABSTENTION_COMPOSITE` | DELETE | A7 |
   | `APOLLO_COMPOSITE_COVERAGE_MIN` | DELETE | A7 |

   Verify each is actually absent from backend AND worker services after
   the change (both Railway services can drift independently — see the
   OPEN item in `prod-promotion-2026-07-16` memory that the worker was not
   yet synced to `main` as of that date). This list is exhaustive for DB-18;
   it does not include non-DB-related var cleanup tracked elsewhere in
   DECISIONS.md section 1 (unrelated repo/env hygiene, not part of this
   cutover).
7. Resume workers, then user writes. Monitor tightly for 24 hours and
   retain legacy tables/functions for at least 14 days (section 4 below
   governs when teardown may run). Take a new backup after the first
   stable hour.

## 4. Post-cutover: observation window, then separately-gated cleanup

1. **14-day observation window (HUMAN).** No teardown or index cleanup
   during this window. Monitor error rate, latency, advisor output
   (`get_advisors`), and legacy-table read/write activity (should be zero
   after step 3.5 above — any non-zero legacy write during the window is a
   rollback trigger, not a thing to silently tolerate).
2. **Dup-index script timing (HUMAN, independent of the 14-day window).**
   `scripts/db/drop_duplicate_indexes.sql` may be applied to prod at ANY
   time before or during this window — it targets the legacy `public`
   schema only, is `IF EXISTS`-guarded, and is harmless to skip entirely
   since `remove_legacy_public_schema.sql` will remove the whole legacy
   schema (duplicates included) later anyway. No ordering dependency on the
   teardown below.
3. **Teardown gate (HUMAN, after 14-day soak + explicit sign-off — never
   combined with the initial cutover per plan section 8.3 step 7).** Apply
   `scripts/db/remove_legacy_public_schema.sql` per its own top-of-file
   APPLY banner: confirm zero in-flight legacy reads, optionally re-run
   `reconcile_copy.sql` as an informational sanity check (the teardown
   script re-asserts copy fidelity itself and will abort loudly on its own
   if preconditions fail — see rehearsal item 4 above), then apply.
4. **30-day unused-index review (HUMAN, only after the target schema has
   real traffic — do not run within days of activation).** Reset stats
   right after activation (`pg_stat_reset()`), wait ≥30 representative days
   covering upload/grading/report/teacher workflows (not idle calendar
   days), then run `scripts/db/unused_index_review.sql`. For every
   candidate row: corroborate with query logs / `EXPLAIN (ANALYZE,
   BUFFERS)`, capture size/definition/constraint-ownership before touching
   anything, never drop a constraint-owned index here, and drop
   `CONCURRENTLY` one at a time outside a transaction.

## 5. Rollback decision tree (plan section 8.4)

- **Before writes resume (pre-write):** deploy old code, revert activation
  grants/config, and use the untouched legacy `public` tables — no
  destructive rollback needed. Target schemas (`app`/`internal`) can remain
  in place for diagnosis.
- **After writes resume but before legacy cleanup (post-write
  reverse-delta):** pause writes/workers, run the rehearsed
  `rollback_reverse_copy.sql` target-to-legacy delta with the watermark set
  to the activation instant, verify checksums via `reconcile_copy.sql`,
  deploy old code, then resume. Preserve target rows (the reverse copy
  never deletes anything from `app`/`internal`). If reverse reconciliation
  fails, **do not** reopen on stale legacy data — restore from backup/PITR
  or forward-fix instead of guessing.
- **After legacy cleanup (post-cleanup PITR):** the legacy tables no longer
  exist, so rollback is backup/PITR restore or a forward migration. This is
  exactly why teardown is gated a separate 14+ days after cutover — it is
  the point past which "just switch back" stops being an option.

## 6. e2e coverage statement

See `styx/handoffs/db18-report.txt` for the full statement (what real-PG
integration coverage exists, what remains manual, and the UI-repos-untested
note per workspace rule). Summary: no new e2e infrastructure was built for
this task (per brief — a wired harness must already exist to reuse it); the
existing real-Postgres integration suites (`tests/database/*`, `pytest -m
integration`) are the closest thing to an e2e proof this branch has, and they
were run as part of this rehearsal. Full HTTP-level / browser-level e2e
against a running FastAPI + both Next.js UIs was NOT exercised — that
requires the test-project deployment in section 2, which is human-gated.
