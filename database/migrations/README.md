# Frozen legacy migrations

This numbered migration chain is a read-only historical record through migration
`047`. Do not add, edit, renumber, delete, or apply files in this directory. The
normalized checksums in `legacy-manifest.sha256` are enforced by CI.

All forward schema work must use timestamped SQL files created with the pinned
Supabase CLI:

```console
npx supabase migration new <descriptive_name>
```

Those files belong in `supabase/migrations/` and are applied in ascending
14-digit timestamp order. Never copy them into this directory, and never use the
old Python/manual numbered-file runner to apply the new schema.

The duplicate legacy `023` files are preserved as history. Production's
`046_apollo_solution_source_llm_paired.sql` is the authoritative legacy `046`.
Any unmerged wave5 numbered migration that collided at `046` must be renumbered
to at least `048` if it ever joins the legacy line; after migration-history
reconciliation, it receives a timestamped name in the active chain instead.

Remote history reconciliation and remote migration application are human-only
operations. The repository harness runs only against the local Docker stack.

## Retired Python entrypoints

DB-03 permanently retired the executable `001_create_schema.py`,
`002_seed_from_supabase.py`, and `003_reindex_existing.py` entrypoints. They now
exit immediately with an error that points to `node scripts/db/reset-local.mjs`.
Their bodies remain in place as checksum-enforced historical provenance; do not
import or invoke them, and do not use them to apply either migration chain.

This repository never had a single aggregate `database.run_migrations` runner.
The three executable Python files were the only migration-apply entrypoints
found under `database/` and `scripts/`, so all three are guarded.
