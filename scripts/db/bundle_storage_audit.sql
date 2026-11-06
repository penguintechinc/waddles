-- Bundle object-storage audit ledger (spec 2026-11-06-bundle-object-storage Sec4):
-- one row per cross-prefix access (allowed AND denied) and per owner tag change,
-- written by `core/bundle_host_storage::pg_audit` under the INSERT-only role
-- `waddles_bundle_storage_audit`. Shared verbatim by alembic 0080 and the Rust
-- integration test so shipped and tested schema cannot drift. Idempotent.

CREATE TABLE IF NOT EXISTS bundle_storage_audit (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    community_id BIGINT NOT NULL,
    caller_app_id VARCHAR(255) NOT NULL,
    owner_app_id VARCHAR(255) NOT NULL,
    op VARCHAR(16) NOT NULL,
    object_key VARCHAR(600) NOT NULL,
    outcome VARCHAR(8) NOT NULL CHECK (outcome IN ('allowed', 'denied')),
    reason VARCHAR(400) NOT NULL,
    request_id VARCHAR(64) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bundle_storage_audit_scope
    ON bundle_storage_audit (tenant_id, community_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_bundle_storage_audit_owner
    ON bundle_storage_audit (owner_app_id, occurred_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_storage_audit') THEN
        GRANT USAGE ON SCHEMA public TO waddles_bundle_storage_audit;
        GRANT INSERT ON bundle_storage_audit TO waddles_bundle_storage_audit;
        GRANT USAGE ON SEQUENCE bundle_storage_audit_id_seq TO waddles_bundle_storage_audit;
    END IF;
END
$$;
