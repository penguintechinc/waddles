-- Seed Script: UI mock content for marketing screenshots (docs/screenshots/)
--
-- Populates the feature pages (communities, members, leaderboard, overlays,
-- chat, bundle activations) so captures are never empty-state. Idempotent:
-- every row is keyed on a fixed id/unique key, so re-running refreshes it.
-- Run via scripts/seed-ui-mock-data.sh (or `make seed-ui-mock-data`).
--
-- No credentials here: demo users have NULL password_hash (cannot log in) and
-- reserved-TLD example emails. The login account comes from seed-admin.sh.
-- Demo ids are pinned to 9001+ so the capture tooling can address them.

\set ON_ERROR_STOP on
BEGIN;

-- Demo hub users (members). NULL password_hash = non-loginable.
INSERT INTO hub_users (id, display_name, username, email, is_active, email_verified)
VALUES
    (9001, 'Aria Quill',   'demo-aria',   'demo-aria@example.invalid',   true, true),
    (9002, 'Bram Holloway','demo-bram',   'demo-bram@example.invalid',   true, true),
    (9003, 'Cleo Marsh',   'demo-cleo',   'demo-cleo@example.invalid',   true, true),
    (9004, 'Dev Okafor',   'demo-dev',    'demo-dev@example.invalid',    true, true),
    (9005, 'Esme Tanaka',  'demo-esme',   'demo-esme@example.invalid',   true, true),
    (9006, 'Finn Larsen',  'demo-finn',   'demo-finn@example.invalid',   true, true)
ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name, is_active = true;

-- Communities (4) in the global tenant.
INSERT INTO communities (id, name, display_name, description, primary_platform, platform,
                         owner_name, community_type, join_mode, member_count, is_active,
                         is_public, tenant_id)
SELECT v.id, v.name, v.display_name, v.description, v.platform, v.platform,
       'Aria Quill', v.ctype::community_type, 'open', v.members, true, true,
       (SELECT id FROM tenants WHERE is_global ORDER BY id LIMIT 1)
FROM (VALUES
    (9001, 'demo-penguin-plays', 'Penguin Plays',  'Weekly game nights, clip nights and community events.', 'twitch',  'creator', 6),
    (9002, 'demo-star-hangar',   'Star Hangar',    'Space-sim pilots swapping routes, ships and fleet plans.', 'discord', 'gaming',  4),
    (9003, 'demo-bar-meetup',    'Bar Meetup Crew','Local meetups, trivia nights and event coordination.',     'discord', 'shared_interest_group', 4),
    (9004, 'demo-maker-lab',     'Maker Lab',      'Hardware tinkerers sharing builds and weekend projects.',  'discord', 'creator', 4)
) AS v(id, name, display_name, description, platform, ctype, members)
ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name,
    description = EXCLUDED.description, member_count = EXCLUDED.member_count,
    is_active = true, deleted_at = NULL;

-- Members: community 9001 gets all six; the others get four.
INSERT INTO community_members (community_id, user_id, platform, platform_user_id, display_name,
                               role, reputation, is_active)
SELECT c.id, u.id::text, c.platform, 'demo-' || u.id,
       u.display_name,
       CASE WHEN u.id = 9001 THEN 'owner' WHEN u.id = 9002 THEN 'moderator' ELSE 'member' END,
       600 + (u.id - 9000) * 35, true
FROM communities c
JOIN hub_users u ON u.id BETWEEN 9001 AND 9006
WHERE c.id BETWEEN 9001 AND 9004
  AND (c.id = 9001 OR u.id <= 9004)
ON CONFLICT (community_id, platform, platform_user_id) DO UPDATE
    SET display_name = EXCLUDED.display_name, reputation = EXCLUDED.reputation, is_active = true;

-- Leaderboard: tenant-scoped reputation scores (migration 097 replaced the
-- old reputation_global table with reputation_tenant).
INSERT INTO reputation_tenant (tenant_id, hub_user_id, score, total_events, last_event_at)
SELECT (SELECT id FROM tenants WHERE is_global ORDER BY id LIMIT 1), id,
       600 + (7 - (id - 9000)) * 55, 20 + (7 - (id - 9000)) * 9, NOW() - ((id - 9000) || ' hours')::interval
FROM hub_users WHERE id BETWEEN 9001 AND 9006
ON CONFLICT (tenant_id, hub_user_id) DO UPDATE SET score = EXCLUDED.score,
    total_events = EXCLUDED.total_events, last_event_at = EXCLUDED.last_event_at, updated_at = NOW();

INSERT INTO community_leaderboard_config (community_id, enabled_platforms, display_limit)
SELECT id, '["twitch","discord"]'::jsonb, 10 FROM communities WHERE id BETWEEN 9001 AND 9004
ON CONFLICT (community_id) DO UPDATE SET enabled_platforms = EXCLUDED.enabled_platforms;

-- Overlays: browser-source token + presentation surfaces/theme per community.
INSERT INTO community_overlay_tokens (community_id, overlay_key, is_active, enabled_sources, access_count, last_accessed)
SELECT id, 'demo-overlay-key-' || id, true, '["alerts","chat","goals","ticker"]'::jsonb, 12 + id % 7, NOW() - interval '2 hours'
FROM communities WHERE id BETWEEN 9001 AND 9004
ON CONFLICT (community_id) DO UPDATE SET is_active = true, access_count = EXCLUDED.access_count;

INSERT INTO overlay_surfaces (community_id, surface, enabled, config)
SELECT c.id, s.surface, true, jsonb_build_object('accent', s.accent)
FROM communities c
CROSS JOIN (VALUES ('full_screen', '#f59e0b'), ('media', '#0ea5e9'), ('crawler', '#22c55e'), ('music', '#a855f7')) AS s(surface, accent)
WHERE c.id BETWEEN 9001 AND 9004
ON CONFLICT (community_id, surface) DO UPDATE SET enabled = true, config = EXCLUDED.config;

INSERT INTO presentation_config (community_id, theme, primary_color, secondary_color, music_enabled, crawler_speed_seconds)
SELECT id, 'default', '#0ea5e9', '#f59e0b', true, 30 FROM communities WHERE id BETWEEN 9001 AND 9004
ON CONFLICT (community_id) DO UPDATE SET theme = EXCLUDED.theme, primary_color = EXCLUDED.primary_color;

-- Chat history for the community chat page.
DELETE FROM hub_chat_messages WHERE community_id BETWEEN 9001 AND 9004;
INSERT INTO hub_chat_messages (community_id, channel_name, sender_hub_user_id, sender_platform,
                               sender_username, message_content, created_at)
SELECT c.id, 'general', u.id, c.platform, u.display_name, m.body, NOW() - (m.mins || ' minutes')::interval
FROM communities c
JOIN (VALUES
    (9001, 'Welcome to game night, everyone!', 42),
    (9002, 'Clip of the week is up in #clips', 31),
    (9003, 'Who is joining Friday trivia?', 18),
    (9004, 'GG all, see you next stream', 6)
) AS m(uid, body, mins) ON true
JOIN hub_users u ON u.id = m.uid
WHERE c.id BETWEEN 9001 AND 9004;

-- Modules/bundles: enable up to four real catalog bundles for each demo
-- community. Uses whatever the bundle seeder already put in app_catalog, so
-- it never invents a bundle; zero catalog rows is a loud failure below.
INSERT INTO module_installations (community_id, module_id, is_enabled, config)
SELECT c.id, b.app_id, true, '{}'::jsonb
FROM communities c
CROSS JOIN (SELECT app_id FROM app_catalog WHERE status = 'active' ORDER BY app_id LIMIT 4) b
WHERE c.id BETWEEN 9001 AND 9004
ON CONFLICT (community_id, module_id) DO UPDATE SET is_enabled = true;

INSERT INTO app_activations (community_id, tenant_id, app_id, enabled, config)
SELECT c.id, c.tenant_id, b.app_id, true, '{}'::jsonb
FROM communities c
CROSS JOIN (SELECT app_id FROM app_catalog WHERE status = 'active' ORDER BY app_id LIMIT 4) b
WHERE c.id BETWEEN 9001 AND 9004
ON CONFLICT (community_id, app_id) DO UPDATE SET enabled = true, updated_at = NOW();

DO $$
BEGIN
    IF (SELECT count(*) FROM app_catalog WHERE status = 'active') = 0 THEN
        RAISE EXCEPTION 'app_catalog is empty: run the bundle seeder before seed-ui-mock-data';
    END IF;
    IF (SELECT count(*) FROM communities WHERE id BETWEEN 9001 AND 9004) <> 4 THEN
        RAISE EXCEPTION 'expected 4 demo communities';
    END IF;
END $$;

-- Keep SERIAL sequences ahead of the pinned ids so later inserts don't collide.
SELECT setval(pg_get_serial_sequence('hub_users', 'id'), GREATEST((SELECT max(id) FROM hub_users), 9006));
SELECT setval(pg_get_serial_sequence('communities', 'id'), GREATEST((SELECT max(id) FROM communities), 9004));

COMMIT;
