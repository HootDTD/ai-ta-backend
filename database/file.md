Database: Single-File Source of Truth
This project uses a single, idempotent SQL script as the source of truth for the database schema and RPCs. Do not add separate migration files.

Single file: database/postgres.sql
Re-run safe: the script includes DROP ... IF EXISTS/CREATE OR REPLACE and other idempotent patterns so it can be applied repeatedly.

What postgres.sql must contain
Drops for existing app-owned objects so the script can be re-run cleanly.
Tables, types, views, functions, triggers (only those owned by this app).
Indexes, constraints, and RLS policies (if used).

Editing rules (AI agents and humans)
Single file only:
All schema and helper SQL lives in database/postgres.sql. Do not create additional .sql files or migration folders.
Idempotency first:
Use DROP ... IF EXISTS (use CASCADE judiciously).
For additive changes prefer ALTER TABLE ... ADD COLUMN IF NOT EXISTS and CREATE INDEX IF NOT EXISTS to preserve data.
Functions: use CREATE OR REPLACE FUNCTION and keep a stable signature when possible.
Order of sections (top ➜ bottom):
1) Drops, 2) Types/enums, 3) Tables, 4) Indexes/constraints, 5) Policies, 6) Views, 7) Functions, 8) Triggers, 9) Seeds.
Concurrency & atomicity:
Security & search path:
Put RPCs in public. For elevated actions use SECURITY DEFINER and set search_path inside the function body.
Naming & comments:
Snakecase names. Prefix: `idx for indexes, fk_ for FKs, pk* for PKs, uq*` for uniques.
Add COMMENT ON TABLE/COLUMN/FUNCTION for non-obvious structures.
Destructive operations:
Only drop/replace objects owned by this app. Never drop unrelated schemas.
Avoid TRUNCATE/data wipes except for local dev and clearly marked sections.