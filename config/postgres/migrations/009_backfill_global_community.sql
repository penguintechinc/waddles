-- Migration: Backfill all existing users into global community
-- This ensures all users are members of the global community for cross-community reputation tracking

-- Add all existing users to global community if not already members.
-- community_members.user_id is VARCHAR (hub_users.id is INTEGER), so cast; there
-- is no UNIQUE(community_id, user_id), so ON CONFLICT cannot be used -- the
-- NOT EXISTS guard + follow-up UPDATE give the same upsert semantics.
INSERT INTO community_members (community_id, user_id, role, is_active, joined_at)
SELECT
    g.id,
    u.id::text,
    'member',
    TRUE,
    NOW()
FROM hub_users u
CROSS JOIN (SELECT id FROM communities WHERE is_global = TRUE LIMIT 1) g
WHERE NOT EXISTS (
    SELECT 1 FROM community_members cm
    WHERE cm.user_id = u.id::text
    AND cm.community_id = g.id
);

UPDATE community_members SET is_active = TRUE
WHERE community_id IN (SELECT id FROM communities WHERE is_global = TRUE)
  AND user_id IN (SELECT id::text FROM hub_users)
  AND is_active IS DISTINCT FROM TRUE;

-- Update member count for global community
UPDATE communities SET member_count = (
    SELECT COUNT(*) FROM community_members WHERE community_id = communities.id AND is_active = TRUE
) WHERE is_global = TRUE;

-- Log the migration
DO $$
DECLARE
    affected_count INTEGER;
BEGIN
    GET DIAGNOSTICS affected_count = ROW_COUNT;
    RAISE NOTICE 'Backfilled % users into global community', affected_count;
END $$;
