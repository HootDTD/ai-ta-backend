# DB history repair handoff

Date: 2026-07-16

Scope: one-time Supabase migration-history reconciliation after DB-03

Authority: `.planning/cleanup/db-redesign-plan.md` sections 5.1 steps 2 and 4

This runbook deliberately contains remote operations that an automated agent
must never execute. The checked-in snapshot is a draft reconstructed from the
legacy files, not a production dump. Production schema is authoritative.

## Preconditions

- Obtain an approved backup/PITR checkpoint and a change window.
- Use the repository-pinned Supabase CLI 2.109.0 and discover flags again with
  `supabase db pull --help`, `supabase migration repair --help`, and
  `supabase migration list --help` before acting.
- Work from a clean checkout of the reviewed DB-03 commit. Never commit a data
  dump, credentials, `.env`, access token, database password, or managed-schema
  contents.
- Archive the checked-in DRAFT SQL outside `supabase/migrations/` for comparison
  before pulling. Do not discard it until review is complete.
- Record the draft filename/version, the pulled filename/version, UTC time,
  operator, backup reference, and before/after migration lists in the change
  ticket.

## H2 - capture production truth

**HUMAN-ONLY REMOTE STEP:** authenticate the CLI and link the clean checkout to
the production project. Confirm the project reference in both the command and
Dashboard before continuing.

```console
supabase login
supabase link --project-ref <PROD_PROJECT_REF>
supabase migration list --linked
```

Save the pre-change `migration list` output. It should show the known recorded
legacy versions `043`, `044`, `045`, `046`, and `047`; stop for investigation if
the list differs.

Move the draft snapshot temporarily outside `supabase/migrations/`, leaving the
active migration directory empty. Then:

**HUMAN-ONLY REMOTE STEP:** pull the production `public` schema using the pinned
CLI. This reads production DDL and may offer to record the generated snapshot as
applied in remote migration history. Review the prompt; never allow schema/data
pushes or resets.

```console
supabase db pull legacy_public_snapshot --linked --schema public
```

If `db pull` refuses because history is mismatched, stop and escalate. Do not
repair `043`-`047` early, use `db push`, or improvise around the ordering below.

Review the generated migration before it becomes authoritative:

1. Retain application tables, sequences, constraints, indexes, functions,
   triggers, grants, RLS enablement, and policies in `public`.
2. Strip Supabase-managed schemas and objects, data rows, ownership clauses,
   credentials, tokens, passwords, connection strings, and environment-specific
   secrets. The file must remain schema-only.
3. Keep the generated timestamp as the canonical snapshot version because that
   is the version `db pull` records remotely. Replace the checked-in DRAFT file
   with this reviewed generated file (one active snapshot, never two).
4. Preserve a copy of the old draft outside the repository solely long enough
   to compare it; do not commit that copy.

**HUMAN-ONLY REMOTE STEP:** capture a schema-only production `public` dump for
comparison. The CLI dump excludes ownership and privilege restoration; inspect
the output again for managed objects and secrets.

```console
supabase db dump --linked --schema public --file <production-public-dump.sql>
```

After the local reset rehearsal, create its schema-only dump without any remote
flags and compare the reviewed pull, production dump, and local re-dump:

```console
supabase db dump --local --schema public --file <rehearsal-redump.sql>
node scripts/db/compare-schema-dump.mjs <reviewed-pull.sql> <rehearsal-redump.sql>
node scripts/db/compare-schema-dump.mjs <production-public-dump.sql> <rehearsal-redump.sql>
```

Also manually reconcile the normalized object list and table list against the
workspace plan input `.planning/cleanup/inputs/db-inventory.md`. Explain every
difference, especially the duplicate 023 history and any untracked pre-043 DDL.
The SQL comparator does not compare row counts or interpret that Markdown file.

Run `node scripts/db/reset-local.mjs`, dump `public` again with schema-only,
no-owner, and no-privileges settings, and require the comparison to pass before
history repair. Commit the reviewed replacement snapshot through normal review.

## H4 - repair history on test, then production

The following changes migration metadata only; it must not apply or remove
application tables. Use the reviewed snapshot timestamp as `<SNAPSHOT_VERSION>`.

**HUMAN-ONLY REMOTE STEP (TEST FIRST):** link to the test project, take a fresh
backup, capture `migration list`, and confirm the reviewed snapshot DDL already
matches the test schema. Record the snapshot as applied if the pull workflow did
not already do so, then mark only the five obsolete legacy versions reverted.

```console
supabase link --project-ref <TEST_PROJECT_REF>
supabase migration list --linked
supabase migration repair <SNAPSHOT_VERSION> --status applied --linked
supabase migration repair 043 044 045 046 047 --status reverted --linked
supabase migration list --linked
```

If `<SNAPSHOT_VERSION>` is already applied, verify that fact and omit the
duplicate `--status applied` command. Require exact local/remote timestamp parity
in the final list. Exercise the application smoke checks and retain rollback
evidence before touching production.

**HUMAN-ONLY REMOTE STEP (PRODUCTION SECOND):** after test sign-off, re-link to
production, verify the project reference, take a fresh backup, and capture a
new migration list. Record the reviewed snapshot as applied if necessary, then
mark only `043`-`047` reverted.

```console
supabase link --project-ref <PROD_PROJECT_REF>
supabase migration list --linked
supabase migration repair <SNAPSHOT_VERSION> --status applied --linked
supabase migration repair 043 044 045 046 047 --status reverted --linked
supabase migration list --linked
```

If the production `db pull` already recorded `<SNAPSHOT_VERSION>` as applied,
omit the duplicate applied repair after verifying it in the pre-repair list.
Require exact local/remote parity and attach both lists to the change ticket.

## Stop conditions and rollback

- Stop on any unexpected migration version, application-schema diff, managed
  object in the pull, secret, command that proposes DDL/data mutation, or test
  failure.
- Do not use `db reset --linked`, `db push`, Dashboard DDL, MCP DDL, or direct
  SQL against either remote project during this reconciliation.
- Before application DDL changes, rollback is migration-history repair using the
  captured before-list. If application objects changed unexpectedly, stop and
  follow the approved backup/PITR incident procedure rather than guessing.
