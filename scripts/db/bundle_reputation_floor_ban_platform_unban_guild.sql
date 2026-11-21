-- Floor-ban platform-originated unban (Discord-native), time-bounded + guild-wide. Applied by alembic
-- 0139_floor_ban_unban_observed. Idempotent.
--
-- 1. `reputation_floor_ban_lift_from_platform` gains `p_observed_ms` (epoch ms the platform event was
--    OBSERVED). A platform unban lifts only bans that already EXISTED when it happened
--    (`created_at <= observed`): a stale/late event never lifts a NEWER ban (a re-ban after the unban).
--    `p_source_app` stays mandatory. The 5-arg overload is dropped (no stale overload).
-- 2. `reputation_floor_ban_lift_from_platform_guild` lifts for EVERY community of ONE tenant that is
--    linked to a platform guild (approved `community_servers` links, else the legacy
--    `communities.platform_server_id`, the same pool rule as hub-api's scope resolution). A guild
--    claimed by more than one tenant lifts NOTHING (fail closed); it never crosses tenants.

DROP FUNCTION IF EXISTS reputation_floor_ban_lift_from_platform(bigint, bigint, text, uuid, text);

CREATE OR REPLACE FUNCTION reputation_floor_ban_lift_from_platform(
    p_tenant bigint, p_community bigint, p_platform text, p_user_uuid uuid,
    p_source_app text, p_observed_ms bigint
) RETURNS TABLE(r_lifted int, r_soft_cleared int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fp$
DECLARE
    -- Fixed marker UUID of the `system:platform_sync` actor (never a person).
    c_sync_actor CONSTANT uuid := '00000000-0000-4000-8000-00000000f5c0';
    v_lifted int := 0;
    v_soft int := 0;
BEGIN
    IF p_tenant IS NULL OR p_community IS NULL OR p_user_uuid IS NULL OR p_platform IS NULL
       OR p_platform !~ '^[a-z][a-z0-9]{1,31}$'
       OR p_source_app IS NULL OR p_source_app !~ '^[a-z0-9._:-]{1,128}$'
       OR p_observed_ms IS NULL OR p_observed_ms <= 0 THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM communities WHERE id = p_community AND tenant_id = p_tenant) THEN
        RAISE EXCEPTION 'reputation_floor_ban_lift_from_platform: community is not in the tenant'
            USING ERRCODE = '42501';
    END IF;
    UPDATE bundle_reputation_floor_bans b
       SET status = 8, lifted_at = NOW(), lifted_by = c_sync_actor, lift_origin = 2,
           lift_source_app = p_source_app,
           score_at_lift = reputation_floor_current_score(
               b.tenant_id, b.community_id, b.user_uuid, b.scope),
           recovered_at = CASE WHEN reputation_verify_floor_breach(
                                  b.tenant_id, b.community_id, b.user_uuid, b.scope)
                               THEN NULL ELSE NOW() END,
           lease_token = NULL, lease_expires_at = NULL, next_retry_at = NULL,
           completed_at = COALESCE(b.completed_at, NOW()), updated_at = NOW()
     WHERE b.tenant_id = p_tenant AND b.community_id = p_community AND b.user_uuid = p_user_uuid
       AND b.platform = p_platform AND b.action = 1 AND b.scope = 1
       AND b.status IN (1, 2, 3, 4, 5, 7)
       AND b.created_at <= to_timestamp(p_observed_ms / 1000.0);
    GET DIAGNOSTICS v_lifted = ROW_COUNT;
    -- The soft-ban clear is PLATFORM-scoped: only when THIS call lifted a ban row of this platform
    -- for this user, and only a floor-set soft ban (reason 1) -- never a manual (reason 2) one.
    IF v_lifted > 0 AND to_regclass('bundle_reputation_soft_bans') IS NOT NULL THEN
        DELETE FROM bundle_reputation_soft_bans
         WHERE tenant_id = p_tenant AND community_id = p_community
           AND user_uuid = p_user_uuid AND reason_code = 1;
        GET DIAGNOSTICS v_soft = ROW_COUNT;
        IF v_soft > 0 THEN
            INSERT INTO bundle_reputation_adjustments
                (app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code,
                 reason_code_id, kind, actor, source_event)
            VALUES ((SELECT c.app_id FROM app_catalog c WHERE c.app_id = p_source_app),
                    p_tenant, p_community, p_user_uuid::text, 'community', 0, '1', 1,
                    'soft_ban_clear', 'system:platform_sync',
                    'soft_ban.platform_unban:' || p_source_app);
        END IF;
    END IF;
    RETURN QUERY SELECT v_lifted, v_soft;
END
$fp$;

CREATE OR REPLACE FUNCTION reputation_floor_ban_lift_from_platform_guild(
    p_tenant bigint, p_platform text, p_guild_id text, p_user_uuid uuid,
    p_source_app text, p_observed_ms bigint
) RETURNS TABLE(r_communities int, r_lifted int, r_soft_cleared int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fg$
DECLARE
    v_pool int[];
    v_tenants int;
    v_cid int;
    v_n int := 0;
    v_lifted int := 0;
    v_soft int := 0;
    v_l int;
    v_s int;
BEGIN
    IF p_tenant IS NULL OR p_platform IS NULL OR p_platform !~ '^[a-z][a-z0-9]{1,31}$'
       OR p_guild_id IS NULL OR p_guild_id !~ '^[A-Za-z0-9_.:-]{1,255}$' THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023';
    END IF;
    -- Pool rule of hub-api's scope resolution: approved links beat the legacy primary-server claim.
    v_pool := '{}';
    -- `community_servers` is created by the config/postgres base schema, not alembic: guard it.
    IF to_regclass('community_servers') IS NOT NULL THEN
        SELECT COALESCE(array_agg(DISTINCT cs.community_id), '{}') INTO v_pool
          FROM community_servers cs
         WHERE cs.platform = p_platform AND cs.platform_server_id = p_guild_id
           AND cs.status = 'approved';
    END IF;
    IF cardinality(v_pool) = 0 THEN
        SELECT COALESCE(array_agg(co.id), '{}') INTO v_pool FROM communities co
         WHERE co.platform = p_platform AND co.platform_server_id = p_guild_id;
    END IF;
    -- A guild claimed across tenants is contested: lift nothing (fail closed, never cross tenants).
    SELECT count(DISTINCT co.tenant_id) INTO v_tenants FROM communities co
     WHERE co.id = ANY (v_pool) AND co.tenant_id IS NOT NULL;
    IF v_tenants <> 1 THEN
        RETURN QUERY SELECT 0, 0, 0;
        RETURN;
    END IF;
    FOR v_cid IN
        SELECT co.id FROM communities co
         WHERE co.id = ANY (v_pool) AND co.tenant_id = p_tenant AND co.is_active IS NOT FALSE
         ORDER BY co.id
    LOOP
        SELECT f.r_lifted, f.r_soft_cleared INTO v_l, v_s
          FROM reputation_floor_ban_lift_from_platform(
                   p_tenant, v_cid, p_platform, p_user_uuid, p_source_app, p_observed_ms) f;
        v_n := v_n + 1;
        v_lifted := v_lifted + v_l;
        v_soft := v_soft + v_s;
    END LOOP;
    RETURN QUERY SELECT v_n, v_lifted, v_soft;
END
$fg$;

REVOKE ALL ON FUNCTION
    reputation_floor_ban_lift_from_platform(bigint, bigint, text, uuid, text, bigint),
    reputation_floor_ban_lift_from_platform_guild(bigint, text, text, uuid, text, bigint)
    FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_svc_action') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_floor_ban_lift_from_platform(bigint, bigint, text, uuid, text, bigint),
            reputation_floor_ban_lift_from_platform_guild(bigint, text, text, uuid, text, bigint)
            TO waddles_svc_action;
    END IF;
END
$$;
