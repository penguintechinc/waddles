-- Bundle `identity` host-capability read contract -- the single source of truth
-- read by BOTH `alembic/versions/0048_bundle_identity_resolve.py` (production
-- schema) and `core/svc_process/tests/identity_pg_e2e.rs` (`include_str!`), so
-- the SQL the stage runs is always tested against the exact DDL that ships.
-- Idempotent; lives under `scripts/db/` because that directory is already
-- copied into the migrations image.
--
-- What it adds: two columns on the existing PII-free data-plane view
-- `community_member_identities` (0043/0045), so the stage can resolve "the
-- triggering platform account" -> "its community `user_uuid`" through the
-- read-only `waddles_bundle_reader` role it already holds, with NO new role,
-- password or table grant:
--
--   tenant_id        -- `communities.tenant_id`, so every lookup is
--                       tenant-scoped (a community id alone is never trusted).
--   is_active_member -- the SAME active-membership predicate the economy and
--                       reputation stores apply (`is_active IS TRUE`, not
--                       left, not removed). `community_members.is_active` is
--                       nullable and a NULL is NOT an active member
--                       (fail-closed; never `COALESCE(.., TRUE)`).
--
-- `CREATE OR REPLACE VIEW` may only APPEND columns, so the five existing
-- columns keep their exact names/types/order (community_id, platform,
-- platform_user_id, hub_user_uuid, user_uuid) and the two new ones go last --
-- every existing consumer is unaffected.
--
-- PII posture (unchanged and re-asserted by the migration tests): the view
-- projects NO display name, handle, username, email, avatar or free text.
-- `platform_user_id` is the opaque platform account id (Twitch `user-id` tag /
-- Discord snowflake) the stage already holds from the inbound event; the view
-- lets the stage turn it into a pseudonymous UUID, nothing more. It is read by
-- the data plane only; no bundle ever sees these columns.
--
-- Prerequisites (earlier migrations / the test harness): `tenants`,
-- `communities(id, tenant_id)`, `hub_users(id, uuid)`, `community_members`
-- (incl. `user_uuid`, `is_active`, `left_at`, `removed_at`).

-- Stand-alone re-statement of the column 0045_identity_resolution owns (and
-- 0046/0047 also restate), so this file runs on its own for the Rust tests.
ALTER TABLE community_members ADD COLUMN IF NOT EXISTS user_uuid UUID;

CREATE OR REPLACE VIEW community_member_identities AS
SELECT
    cm.community_id,
    cm.platform,
    cm.platform_user_id,
    hu.uuid AS hub_user_uuid,
    cm.user_uuid,
    c.tenant_id AS tenant_id,
    (cm.is_active IS TRUE AND cm.removed_at IS NULL AND cm.left_at IS NULL)
        AS is_active_member
FROM community_members cm
LEFT JOIN hub_users hu ON hu.id::text = cm.user_id
LEFT JOIN communities c ON c.id = cm.community_id;

COMMENT ON VIEW community_member_identities IS
    'Data-plane read contract (PII-free): community_members platform identity (opaque platform_user_id only, never a handle/display name) -> its stable community user_uuid, tenant-scoped, with the active-membership predicate. user_uuid NULL = identity not yet resolved (the identity capability reports not_linked). No PII column is projected.';

-- Re-grant (guarded: the role may not exist in every environment). Grants
-- survive CREATE OR REPLACE VIEW, so this is a no-op where already granted.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reader') THEN
        GRANT SELECT ON community_member_identities TO waddles_bundle_reader;
    END IF;
END
$$;
