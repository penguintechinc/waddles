-- community_member_identities: append the member's community ROLE (bundle-MCP-tools
-- actor/scope P3c). The stage maps this opaque role enum to a fixed host-side scope
-- set (`bundle_tool_call::scopes_for_role`) so admin-scoped tools are enforced
-- HOST-SIDE against a VERIFIED actor, never a caller-attested one.
--
-- `CREATE OR REPLACE VIEW` may only APPEND columns: the eleven existing columns
-- (0043/0045/0048/0052) keep their exact names/types/order and `role` goes last.
--
-- PII posture (unchanged): `role` is one of the closed vocabulary
-- community-owner | owner (legacy community creation) | community-admin |
-- moderator | vip | member -- an opaque
-- enum, never a handle, display name or free text. No PII column is projected.
--
-- Prerequisites: everything bundle_identity_resolve.sql needs. `community_members.role`
-- is a legacy SQL-owned column; the guarded ADD below is a no-op where it exists
-- and lets this file run stand-alone for the Rust tests.

ALTER TABLE community_members ADD COLUMN IF NOT EXISTS role VARCHAR(50);

CREATE OR REPLACE VIEW community_member_identities AS
SELECT
    cm.community_id,
    cm.platform,
    cm.platform_user_id,
    hu.uuid AS hub_user_uuid,
    cm.user_uuid,
    CASE WHEN cm.user_uuid IS NOT NULL THEN 'resolved' ELSE 'unavailable' END
        AS user_uuid_status,
    cm.user_uuid_unavailable_reason,
    c.tenant_id AS tenant_id,
    (cm.is_active IS TRUE AND cm.removed_at IS NULL AND cm.left_at IS NULL)
        AS is_active_member,
    cm.role::text AS role
FROM community_members cm
LEFT JOIN hub_users hu ON hu.id::text = cm.user_id
LEFT JOIN communities c ON c.id = cm.community_id;

COMMENT ON VIEW community_member_identities IS
    'Data-plane read contract (PII-free): community_members platform identity (opaque platform_user_id only, never a handle/display name) -> its stable community user_uuid, tenant-scoped, with the active-membership predicate and the opaque community role enum. user_uuid NULL = identity not yet resolved (the identity capability reports not_linked). No PII column is projected.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reader') THEN
        GRANT SELECT ON community_member_identities TO waddles_bundle_reader;
    END IF;
END
$$;
