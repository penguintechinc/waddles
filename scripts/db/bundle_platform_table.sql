-- Bundle `platform-table` capability (spec 2026-11-06-bundle-table-access-grants):
-- the per-write audit ledger + the least-privilege `waddles_platform_table` role.
-- Shared verbatim by alembic 0078 and the Rust integration test so the shipped
-- schema and the tested schema cannot drift. Idempotent.

CREATE TABLE IF NOT EXISTS bundle_platform_table_audit (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    community_id INTEGER NOT NULL,
    app_id VARCHAR(255) NOT NULL,
    app_version BIGINT NOT NULL,
    user_uuid UUID NOT NULL,
    table_name VARCHAR(63) NOT NULL,
    column_name VARCHAR(63) NOT NULL,
    op VARCHAR(8) NOT NULL CHECK (op IN ('write')),
    old_value TEXT,
    new_value TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bundle_platform_table_audit_scope
    ON bundle_platform_table_audit (tenant_id, community_id, occurred_at DESC);

-- Role grants: column-scoped. SELECT/UPDATE only on the allowlisted columns of
-- community_members (+ the key/scope columns), tenant lookup on communities,
-- INSERT on the audit ledger. No DDL, no other table.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_platform_table') THEN
        GRANT USAGE ON SCHEMA public TO waddles_platform_table;
        GRANT SELECT (id, community_id, user_uuid, role, is_active, joined_at, left_at, removed_at)
            ON community_members TO waddles_platform_table;
        GRANT UPDATE (role, updated_at) ON community_members TO waddles_platform_table;
        GRANT SELECT (id, tenant_id) ON communities TO waddles_platform_table;
        GRANT INSERT ON bundle_platform_table_audit TO waddles_platform_table;
        GRANT USAGE ON SEQUENCE bundle_platform_table_audit_id_seq TO waddles_platform_table;
    END IF;
END
$$;
