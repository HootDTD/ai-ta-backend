-- Grant app_runtime column-level SELECT on auth.users so the teacher
-- class-performance projection (apollo/projections/performance.py::_identities)
-- can resolve student email / display name. Unblocks the prod
-- InsufficientPrivilegeError for app_runtime confirmed in Railway prod logs.
-- Twin of legacy database/migrations/049_auth_users_identity_grant.sql; both
-- chains must carry it.
--
-- Column-level grant ONLY — encrypted_password and every other auth.users
-- column stay unreadable. Guarded (DO block, no-op with a NOTICE) for
-- environments where the auth schema/table or the app_runtime role is absent
-- (local Docker / Testcontainers), matching the defensive style of
-- 20260722120000_db08b_rls_enforcement_grants.sql. GRANT is idempotent, so a
-- re-run is a harmless no-op. Applying to remote projects (TEST, prod) is a
-- human step — agents never apply remote migrations.

DO $auth_users_identity_grant$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        RAISE NOTICE
            'auth_users_identity_grant: role app_runtime absent - skipping (expected on local Docker/Testcontainers).';
        RETURN;
    END IF;
    IF to_regclass('auth.users') IS NULL THEN
        RAISE NOTICE
            'auth_users_identity_grant: auth.users absent - skipping (expected on local Docker/Testcontainers).';
        RETURN;
    END IF;

    GRANT USAGE ON SCHEMA auth TO app_runtime;
    GRANT SELECT (id, email, raw_user_meta_data) ON auth.users TO app_runtime;
END
$auth_users_identity_grant$;
