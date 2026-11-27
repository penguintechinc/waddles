-- 0148: data-plane contracts read a member's community role through the LINKED role row
-- (`community_members.community_role_id` -> `community_roles`), never the legacy
-- `community_members.role` string (dropped by alembic 0148 right after this file runs).
-- Shared verbatim by alembic 0148 and the Rust integration tests so the shipped contract and the
-- tested contract cannot drift. Idempotent. Prerequisites: `community_roles` (id, community_id,
-- name) and `community_members.community_role_id` / `claims_cache` exist.
--
-- 1. community_member_identities: same twelve columns, same names/types/order (0070/0116), so
--    `CREATE OR REPLACE` swaps only the `role` expression. `role` is now the linked role's
--    `name` (NULL when the member links no role -> the stage resolves no scopes, fail closed).
--    The vocabulary bundles/the stage see is unchanged: system roles keep their seeded names
--    (community-owner | community-admin | moderator | member | ...), custom roles their name.

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
    cr.name::text AS role,
    hu.id AS hub_user_id
FROM community_members cm
LEFT JOIN hub_users hu ON hu.id::text = cm.user_id
LEFT JOIN communities c ON c.id = cm.community_id
LEFT JOIN community_roles cr
    ON cr.id = cm.community_role_id AND cr.community_id = cm.community_id;

COMMENT ON VIEW community_member_identities IS
    'Data-plane read contract (PII-free): community_members platform identity (opaque platform_user_id only, never a handle/display name) -> its stable community user_uuid, tenant-scoped, with the active-membership predicate and the member''s LINKED community role name (community_roles via community_role_id; NULL = no linked role, no scopes). user_uuid NULL = identity not yet resolved (the identity capability reports not_linked). No PII column is projected.';

-- 2. Grants (roles are provisioned elsewhere; skipped when absent so ordering never matters).
--    waddles_bundle_reader keeps SELECT on the replaced view. waddles_platform_table reads and
--    writes the member's role LINK (+ clears the cached claims so scopes re-resolve) and may
--    look up the community's role rows by name -- nothing else on community_roles.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reader') THEN
        GRANT SELECT ON community_member_identities TO waddles_bundle_reader;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_platform_table') THEN
        GRANT SELECT (community_role_id) ON community_members TO waddles_platform_table;
        GRANT UPDATE (community_role_id, claims_cache, updated_at)
            ON community_members TO waddles_platform_table;
        GRANT SELECT (id, community_id, name, is_system) ON community_roles
            TO waddles_platform_table;
    END IF;
END
$$;
