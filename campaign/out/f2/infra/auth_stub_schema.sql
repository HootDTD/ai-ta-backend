-- F2 isolated-stack minimal Supabase-compat stubs (f2-postgres, port 57422).
-- The real supabase image provides schema auth + auth.users + auth.uid() and
-- the anon/authenticated/service_role roles; the plain pgvector image does
-- not. The repo's numbered migrations reference exactly these objects
-- (auth.users FKs in 004/006/023/033, auth.uid() + anon role in RLS
-- policies). This stub creates ONLY what those statements need to parse and
-- enforce. Nothing else from GoTrue's real schema is reproduced.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS auth;

-- Minimal auth.users: id is the only column any migration references; email
-- kept so the campaign auth stub can upsert idempotently by email.
CREATE TABLE IF NOT EXISTS auth.users (
    id uuid PRIMARY KEY,
    email text UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- auth.uid() as Supabase defines it (reads the request JWT claim GUC).
-- The campaign backend connects as the table owner and bypasses RLS, so this
-- only needs to exist for CREATE POLICY statements to parse.
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
LANGUAGE sql STABLE
AS $$
  SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;

-- Roles referenced by RLS policies in 022 (anon) and general supabase DDL.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
        CREATE ROLE anon NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE ROLE service_role NOLOGIN;
    END IF;
END
$$;
