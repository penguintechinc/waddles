-- Migration 097: ephemeral_identities -- pseudonym store for the
-- PII-tokenization boundary (docs/superpowers/specs/2026-09-28-bundle-
-- permissions-and-capability-gate.md S10.1/S10.3). Security-review fix to
-- PR #429: an ephemeral pseudonym for an unknown/unlinked platform
-- identity must be minted INSIDE the PII boundary (hub-api), never as a
-- client-computable UUIDv5 over a fixed public namespace (reversible by
-- dictionary attack against known handles) -- see
-- POST /api/v1/internal/identities/ephemeral.
--
-- Part of the PII store: covered by the existing DSAR/erasure job (walks
-- every PII-adjacent table for a linked hub_users row) and additionally
-- subject to its own TTL (expires_at, default 30 days from last_seen)
-- independent of any erasure request -- an unlinked platform identity
-- nobody ever claims should eventually age out on its own.
--
-- NOTE: numbered against the 09x raw-SQL migration track this repo's
-- general schema uses (hub_users/community_members both live here, not
-- the separate alembic/ track used for the bundle/RBAC schema area) --
-- may need renumbering if a concurrent PR also claims 097.

CREATE TABLE IF NOT EXISTS ephemeral_identities (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         INTEGER NOT NULL,
    pseudonym         UUID NOT NULL,
    platform          VARCHAR(50) NOT NULL,
    platform_user_id  VARCHAR(255) NOT NULL,
    handle            VARCHAR(255),
    last_seen         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at        TIMESTAMPTZ NOT NULL,
    UNIQUE (tenant_id, platform, platform_user_id)
);

CREATE INDEX IF NOT EXISTS idx_ephemeral_identities_pseudonym
    ON ephemeral_identities (pseudonym);
CREATE INDEX IF NOT EXISTS idx_ephemeral_identities_expires_at
    ON ephemeral_identities (expires_at);

COMMENT ON TABLE ephemeral_identities IS
    'Per-tenant HMAC-derived pseudonym store for unknown/unlinked platform identities -- hub-api-only (PII boundary), never queried directly by svc_process/svc_action. TTL via expires_at; covered by DSAR/erasure.';
