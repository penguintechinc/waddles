-- Migration 091: seed the community forums APP BUNDLE (process + action stages).
--
-- Port of Node's forum post/reply CRUD (hub_api/services/
-- community_interaction.py:create_forum_post/create_forum_reply) into the
-- App Bundle SDK's process + action stages.
--
-- Two-stage pipeline:
-- 1. Process stage (bundles.community_forums_process:transform) -- parses
--    chat commands (!forum create/reply) and extracts structured data.
-- 2. Action stage (bundles.community_forums_action:create_forum_post/
--    create_forum_reply) -- persists to hub_forum_posts/hub_forum_replies
--    tables and relays to bridged channels.
--
-- The process stage filters out non-forum messages (returns `None` for
-- ordinary chatter). The action stage handles both post creation and replies,
-- selecting the correct handler based on the `forum_action` field in the
-- transformed event's payload.
--
-- Config carries no secrets; per-activation channel_id (reply-in-place
-- target) supplied when a community activates (migration 069's 3-tier
-- precedence) -- read via config.get("channel_id")
-- (core/svc_action/bundles/community_forums_action.py:_resolve_channel_id),
-- never from the chat payload. Unconfigured activations still persist posts
-- with hub_channel_id=NULL (alembic 0006_forum_posts_channel_nullable).
-- author_id and author_username come from the inbound event's actor field.
--
-- DB access uses flask_core.get_bundle_dal(), the same shared DAL every
-- other action bundle uses (bound once at svc-action startup via
-- set_bundle_dal()). Previously the bundle built its own AsyncDAL against a
-- hardcoded URL naming a non-existent DB role, silently dropping every
-- forum post/reply -- see git history on this file for the fix.
--
-- Encryption at rest: engine-level (security.md Storage baseline). No
-- column-level action needed here.

INSERT INTO app_catalog (
    app_id, manifest_version, module, feature, provider, execution_model,
    is_default, platform_compatibility, status, stages
) VALUES (
    'penguintechinc.waddles.forums-pipeline',
    '1.0.0',
    'community',
    'penguintechinc.waddles',
    'builtin',
    'native',
    TRUE,
    '{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}'::jsonb,
    'active',
    (
        '{"process": {"entrypoint": "bundles.community_forums_process:transform", ' ||
        '"config": {}, "spec": {"required_config": []}}, ' ||
        '"action": {"entrypoint": "bundles.community_forums_action:create_forum_post", ' ||
        '"config": {}, "spec": {"required_config": ["channel_id"]}}}'
    )::jsonb
)
ON CONFLICT (app_id) DO NOTHING;

-- Activate for the global tenant (all communities)
INSERT INTO app_tenant_availability (tenant_id, app_id, available)
SELECT t.id, 'penguintechinc.waddles.forums-pipeline', TRUE
FROM tenants t
WHERE t.slug = 'global'
ON CONFLICT (tenant_id, app_id) DO NOTHING;
