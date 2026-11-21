-- Floor-ban PLATFORM SYNC -- SQL half. Applied by alembic 0138_floor_ban_platform_sync. Idempotent.
--
-- Lifts sync with the platform in BOTH directions, over the SAME outbox the ban used:
--   * `action` (1 ban, 2 unban): a Waddles lift of a ban whose platform command was DELIVERED
--     (status 2 done, or 7 in flight) enqueues exactly ONE unban row (key 'unban:<ban id>',
--     idempotent) that the existing action-stage dispatcher claims/retries/dead-letters like a ban.
--     Unban rows never deny access (every ban check filters action = 1) and are never throttled by
--     the hourly ban quota or cancelled by a recovered score.
--   * `lift_origin` (1 admin, 2 platform): a lift that ORIGINATES on the platform
--     (`reputation_floor_ban_lift_from_platform`, tenant + community derived by the host from the
--     verified guild binding, never from the event) never enqueues an unban, so a sync cannot echo.
-- A lift stays sticky (0137) in both directions.

ALTER TABLE bundle_reputation_floor_bans
    ADD COLUMN IF NOT EXISTS action SMALLINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS source_ban_id BIGINT,
    ADD COLUMN IF NOT EXISTS lift_origin SMALLINT,
    ADD COLUMN IF NOT EXISTS score_at_lift INTEGER;
ALTER TABLE bundle_reputation_floor_bans
    DROP CONSTRAINT IF EXISTS ck_bundle_reputation_floor_bans_action;
ALTER TABLE bundle_reputation_floor_bans
    ADD CONSTRAINT ck_bundle_reputation_floor_bans_action CHECK (action IN (1, 2));
ALTER TABLE bundle_reputation_floor_bans
    DROP CONSTRAINT IF EXISTS ck_bundle_reputation_floor_bans_lift_origin;
ALTER TABLE bundle_reputation_floor_bans
    ADD CONSTRAINT ck_bundle_reputation_floor_bans_lift_origin
    CHECK (lift_origin IS NULL OR lift_origin IN (1, 2));

-- Re-arm on a NEW LOW. A lift records the user's score at lift time (`score_at_lift`, set in the
-- same statement as the lift); a later score at least `lift_rearm_drop` below that snapshot is a
-- new offence and bans again even without a recover-then-cross. The threshold is policy-driven:
-- the per-community / per-tenant `lift_rearm_drop` column, default 5 = the largest single activity
-- delta the host accepts (`ACTIVITY_CAP`), i.e. one maximal ordinary penalty step. There is no
-- other per-offence penalty knob in the reputation config (penalties are caller-named deltas).
ALTER TABLE bundle_reputation_policy
    ADD COLUMN IF NOT EXISTS lift_rearm_drop INTEGER NOT NULL DEFAULT 5
        CHECK (lift_rearm_drop > 0);
ALTER TABLE bundle_reputation_tenant_policy
    ADD COLUMN IF NOT EXISTS lift_rearm_drop INTEGER NOT NULL DEFAULT 5
        CHECK (lift_rearm_drop > 0);

-- The subject's CURRENT score on the scope's own scale (community: balance, unscored = the
-- policy default; tenant: the fixed 300..850 aggregate, unscored = 600).
CREATE OR REPLACE FUNCTION reputation_floor_current_score(
    p_tenant bigint, p_community bigint, p_user_uuid uuid, p_scope smallint
) RETURNS int
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $cs$
DECLARE
    v_score int;
    v_default int;
BEGIN
    IF p_scope = 2 THEN
        SELECT score INTO v_score FROM bundle_reputation_tenant_scores
         WHERE tenant_id = p_tenant AND user_uuid = p_user_uuid;
        RETURN COALESCE(v_score, 600);
    END IF;
    SELECT balance INTO v_score FROM bundle_reputation_scores
     WHERE community_id = p_community AND user_uuid = p_user_uuid AND tenant_id = p_tenant;
    IF v_score IS NULL THEN
        SELECT score_default INTO v_default FROM bundle_reputation_policy
         WHERE community_id = p_community AND tenant_id = p_tenant;
        RETURN COALESCE(v_default, 600);
    END IF;
    RETURN v_score;
END
$cs$;

CREATE OR REPLACE FUNCTION reputation_floor_lift_rearm_drop(
    p_tenant bigint, p_community bigint, p_scope smallint
) RETURNS int
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $rd$
DECLARE
    v_drop int;
BEGIN
    IF p_scope = 2 THEN
        SELECT lift_rearm_drop INTO v_drop FROM bundle_reputation_tenant_policy
         WHERE tenant_id = p_tenant;
    ELSE
        SELECT lift_rearm_drop INTO v_drop FROM bundle_reputation_policy
         WHERE community_id = p_community AND tenant_id = p_tenant;
    END IF;
    RETURN COALESCE(v_drop, 5);
END
$rd$;

-- Admin lift (hub-api): lifts the ban rows (action 1) and enqueues one unban per delivered ban.
CREATE OR REPLACE FUNCTION reputation_floor_ban_lift(
    p_tenant bigint, p_user_uuids uuid[], p_lifted_by uuid
) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fl$
DECLARE
    v_n int;
BEGIN
    IF p_tenant IS NULL OR p_lifted_by IS NULL OR p_user_uuids IS NULL
       OR cardinality(p_user_uuids) < 1 OR cardinality(p_user_uuids) > 64 THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023';
    END IF;
    WITH target AS (
        SELECT id, status AS old_status FROM bundle_reputation_floor_bans
         WHERE tenant_id = p_tenant AND user_uuid = ANY(p_user_uuids) AND action = 1
           AND status IN (1, 2, 3, 4, 5, 7)
         FOR UPDATE
    ), lifted AS (
        UPDATE bundle_reputation_floor_bans b
           SET status = 8, lifted_at = NOW(), lifted_by = p_lifted_by, lift_origin = 1,
               score_at_lift = reputation_floor_current_score(
                   b.tenant_id, b.community_id, b.user_uuid, b.scope),
               recovered_at = CASE WHEN reputation_verify_floor_breach(
                                      b.tenant_id, b.community_id, b.user_uuid, b.scope)
                                   THEN NULL ELSE NOW() END,
               lease_token = NULL, lease_expires_at = NULL, next_retry_at = NULL,
               completed_at = COALESCE(b.completed_at, NOW()), updated_at = NOW()
          FROM target t WHERE b.id = t.id
        RETURNING b.id, b.tenant_id, b.community_id, b.origin_community_id, b.user_uuid, b.scope,
                  b.reason, b.platform, t.old_status
    ), unbans AS (
        INSERT INTO bundle_reputation_floor_bans
            (tenant_id, community_id, origin_community_id, user_uuid, scope, idempotency_key,
             reason, platform, status, action, source_ban_id)
        SELECT l.tenant_id, l.community_id, l.origin_community_id, l.user_uuid, l.scope,
               'unban:' || l.id, l.reason, l.platform, 1, 2, l.id
          FROM lifted l WHERE l.old_status IN (2, 7)
        ON CONFLICT (tenant_id, community_id, user_uuid, scope, idempotency_key) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO v_n FROM lifted;
    RETURN v_n;
END
$fl$;

-- Platform lift (action stage, after the platform confirmed/announced an unban): lifts THIS
-- community's in-force ban rows for the user on that platform with NO unban echo, and clears the
-- community soft ban (ledgered). Tenant + community come from the host's verified binding; the
-- community must belong to the tenant and every write is filtered by that tenant, so an event for
-- tenant B can never lift tenant A's ban. Returns (rows lifted, soft bans cleared).
CREATE OR REPLACE FUNCTION reputation_floor_ban_lift_from_platform(
    p_tenant bigint, p_community bigint, p_platform text, p_user_uuid uuid
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
       OR p_platform !~ '^[a-z][a-z0-9]{1,31}$' THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM communities WHERE id = p_community AND tenant_id = p_tenant) THEN
        RAISE EXCEPTION 'reputation_floor_ban_lift_from_platform: community is not in the tenant'
            USING ERRCODE = '42501';
    END IF;
    UPDATE bundle_reputation_floor_bans b
       SET status = 8, lifted_at = NOW(), lifted_by = c_sync_actor, lift_origin = 2,
           score_at_lift = reputation_floor_current_score(
               b.tenant_id, b.community_id, b.user_uuid, b.scope),
           recovered_at = CASE WHEN reputation_verify_floor_breach(
                                  b.tenant_id, b.community_id, b.user_uuid, b.scope)
                               THEN NULL ELSE NOW() END,
           lease_token = NULL, lease_expires_at = NULL, next_retry_at = NULL,
           completed_at = COALESCE(b.completed_at, NOW()), updated_at = NOW()
     WHERE b.tenant_id = p_tenant AND b.community_id = p_community AND b.user_uuid = p_user_uuid
       AND b.platform = p_platform AND b.action = 1 AND b.status IN (1, 2, 3, 4, 5, 7);
    GET DIAGNOSTICS v_lifted = ROW_COUNT;
    IF to_regclass('bundle_reputation_soft_bans') IS NOT NULL THEN
        DELETE FROM bundle_reputation_soft_bans
         WHERE tenant_id = p_tenant AND community_id = p_community AND user_uuid = p_user_uuid;
        GET DIAGNOSTICS v_soft = ROW_COUNT;
        IF v_soft > 0 THEN
            INSERT INTO bundle_reputation_adjustments
                (app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code,
                 reason_code_id, kind, actor, source_event)
            VALUES (NULL, p_tenant, p_community, p_user_uuid::text, 'community', 0, '1', 1,
                    'soft_ban_clear', 'system:platform_sync', 'soft_ban.platform_unban');
        END IF;
    END IF;
    RETURN QUERY SELECT v_lifted, v_soft;
END
$fp$;

-- Enqueue: as 0137, but the quota / per-user cap count BANS only (action 1).
CREATE OR REPLACE FUNCTION reputation_floor_ban_enqueue(
    p_tenant bigint, p_community bigint, p_origin bigint, p_user_uuid uuid,
    p_scope smallint, p_key text, p_platform text
) RETURNS TABLE(r_state smallint, r_id bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fe$
DECLARE
    v_id bigint;
    v_status smallint;
    v_quota int := 60;
    v_recent bigint;
    v_oldest timestamptz;
    v_reason text;
    v_user_cap int := 2;
    v_user_recent bigint;
BEGIN
    IF p_scope IS NULL OR p_scope NOT IN (1, 2) THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023';
    END IF;
    IF p_key IS NULL OR p_key !~ '^[a-z0-9._:-]{1,128}$' OR p_user_uuid IS NULL
       OR p_platform IS NULL OR p_platform !~ '^[a-z][a-z0-9]{1,31}$' THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM communities WHERE id = p_community AND tenant_id = p_tenant)
       OR NOT EXISTS (SELECT 1 FROM communities WHERE id = p_origin AND tenant_id = p_tenant) THEN
        RAISE EXCEPTION 'reputation_floor_ban_enqueue: community is not in the named tenant'
            USING ERRCODE = '42501';
    END IF;
    v_reason := CASE p_scope WHEN 1 THEN 'community_hard_floor' ELSE 'tenant_floor' END;
    PERFORM pg_advisory_xact_lock(hashtextextended('rep_floor_ban:' || p_community, 0));

    SELECT b.id, b.status INTO v_id, v_status FROM bundle_reputation_floor_bans b
     WHERE b.tenant_id = p_tenant AND b.community_id = p_community AND b.user_uuid = p_user_uuid
       AND b.scope = p_scope AND b.idempotency_key = p_key;
    -- 8 (lifted) is terminal for the SAME key: a lift is sticky for that ban occurrence.
    IF FOUND AND v_status IN (1, 2, 3, 4, 7, 8) THEN
        r_state := 1; r_id := v_id; RETURN NEXT; RETURN;
    END IF;

    IF reputation_floor_ban_halted() THEN
        INSERT INTO bundle_reputation_floor_bans
            (tenant_id, community_id, origin_community_id, user_uuid, scope, idempotency_key,
             reason, platform, status, last_error, completed_at)
        VALUES (p_tenant, p_community, p_origin, p_user_uuid, p_scope, p_key, v_reason,
                p_platform, 6, 'kill_switch', NOW())
        ON CONFLICT (tenant_id, community_id, user_uuid, scope, idempotency_key)
        DO UPDATE SET status = 6, last_error = 'kill_switch', next_retry_at = NULL,
                      updated_at = NOW()
        RETURNING id INTO v_id;
        r_state := 3; r_id := v_id; RETURN NEXT; RETURN;
    END IF;

    -- A LIFTED ban stays lifted until the user genuinely recovers: while a lifted row of this
    -- (tenant, user, scope[, community]) has not seen the score back at/above the floor
    -- (`recovered_at` IS NULL), a NEW event key (the key is event-derived) must not re-ban. A new
    -- floor-crossing -- recover, then fall below again -- finds no such row and bans normally.
    -- Only the MOST RECENT lift of this subject/scope governs. It blocks until the score
    -- recovers (recovered_at) OR makes a NEW LOW: at least `lift_rearm_drop` below the score
    -- recorded at lift time (`score_at_lift`) -- a lift forgives past behaviour, not a new
    -- offence. (No snapshot, e.g. a pre-0138 lift: only recovery re-arms.)
    IF EXISTS (SELECT 1 FROM bundle_reputation_floor_bans l
                WHERE l.id = (SELECT m.id FROM bundle_reputation_floor_bans m
                               WHERE m.tenant_id = p_tenant AND m.user_uuid = p_user_uuid
                                 AND m.scope = p_scope AND m.status = 8
                                 AND (p_scope = 2 OR m.community_id = p_community)
                               ORDER BY m.lifted_at DESC, m.id DESC LIMIT 1)
                  AND l.recovered_at IS NULL
                  AND NOT (l.score_at_lift IS NOT NULL
                           AND reputation_floor_current_score(
                                   l.tenant_id, l.community_id, l.user_uuid, l.scope)
                               <= l.score_at_lift - reputation_floor_lift_rearm_drop(
                                   l.tenant_id, l.community_id, l.scope))) THEN
        INSERT INTO bundle_reputation_floor_bans
            (tenant_id, community_id, origin_community_id, user_uuid, scope, idempotency_key,
             reason, platform, status, last_error, completed_at)
        VALUES (p_tenant, p_community, p_origin, p_user_uuid, p_scope, p_key, v_reason,
                p_platform, 6, 'lift_sticky', NOW())
        ON CONFLICT (tenant_id, community_id, user_uuid, scope, idempotency_key)
        DO UPDATE SET status = 6, last_error = 'lift_sticky', next_retry_at = NULL,
                      updated_at = NOW()
        RETURNING id INTO v_id;
        r_state := 3; r_id := v_id; RETURN NEXT; RETURN;
    END IF;

    IF NOT reputation_verify_floor_breach(p_tenant, p_community, p_user_uuid, p_scope) THEN
        INSERT INTO bundle_reputation_floor_bans
            (tenant_id, community_id, origin_community_id, user_uuid, scope, idempotency_key,
             reason, platform, status, last_error, completed_at)
        VALUES (p_tenant, p_community, p_origin, p_user_uuid, p_scope, p_key, v_reason,
                p_platform, 6, 'floor_not_breached', NOW())
        ON CONFLICT (tenant_id, community_id, user_uuid, scope, idempotency_key)
        DO UPDATE SET status = 6, last_error = 'floor_not_breached', next_retry_at = NULL,
                      updated_at = NOW()
        RETURNING id INTO v_id;
        r_state := 3; r_id := v_id; RETURN NEXT; RETURN;
    END IF;

    SELECT COUNT(*) INTO v_user_recent FROM bundle_reputation_floor_bans
     WHERE community_id = p_community AND user_uuid = p_user_uuid
       AND created_at > NOW() - INTERVAL '1 hour' AND status IN (1, 2, 3, 5, 7) AND action = 1
       AND NOT (scope = p_scope AND idempotency_key = p_key);
    IF v_user_recent >= v_user_cap THEN
        r_state := 4; r_id := 0; RETURN NEXT; RETURN;
    END IF;

    SELECT floor_ban_hourly_quota INTO v_quota FROM bundle_reputation_policy
     WHERE community_id = p_community AND tenant_id = p_tenant;
    IF NOT FOUND THEN
        v_quota := 60;
    END IF;
    SELECT COUNT(*), MIN(created_at) INTO v_recent, v_oldest FROM bundle_reputation_floor_bans
     WHERE community_id = p_community AND created_at > NOW() - INTERVAL '1 hour'
       AND status IN (1, 2, 3, 7) AND action = 1
       AND NOT (user_uuid = p_user_uuid AND scope = p_scope AND idempotency_key = p_key);
    IF v_recent >= v_quota THEN
        INSERT INTO bundle_reputation_floor_bans
            (tenant_id, community_id, origin_community_id, user_uuid, scope, idempotency_key,
             reason, platform, status, last_error, next_retry_at)
        VALUES (p_tenant, p_community, p_origin, p_user_uuid, p_scope, p_key, v_reason,
                p_platform, 5, 'rate_limited',
                GREATEST(NOW() + INTERVAL '30 seconds', v_oldest + INTERVAL '1 hour'))
        ON CONFLICT (tenant_id, community_id, user_uuid, scope, idempotency_key)
        DO UPDATE SET status = 5, last_error = 'rate_limited', updated_at = NOW(),
                      next_retry_at = GREATEST(NOW() + INTERVAL '30 seconds',
                                               v_oldest + INTERVAL '1 hour')
        RETURNING id INTO v_id;
        r_state := 2; r_id := v_id; RETURN NEXT; RETURN;
    END IF;

    INSERT INTO bundle_reputation_floor_bans
        (tenant_id, community_id, origin_community_id, user_uuid, scope, idempotency_key,
         reason, platform, status)
    VALUES (p_tenant, p_community, p_origin, p_user_uuid, p_scope, p_key, v_reason, p_platform, 1)
    ON CONFLICT (tenant_id, community_id, user_uuid, scope, idempotency_key)
    DO UPDATE SET status = 1, last_error = NULL, next_retry_at = NULL, updated_at = NOW()
    RETURNING id INTO v_id;
    r_state := 0; r_id := v_id; RETURN NEXT;
END
$fe$;

-- Claim: also returns `r_action`; the hourly ban quota never defers an unban.
DROP FUNCTION IF EXISTS reputation_floor_ban_claim(int, int);
CREATE OR REPLACE FUNCTION reputation_floor_ban_claim(p_limit int, p_lease_seconds int)
RETURNS TABLE(r_id bigint, r_tenant_id int, r_community_id int, r_user_uuid uuid,
              r_scope smallint, r_platform text, r_attempt int, r_lease_token uuid,
              r_action smallint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fc$
DECLARE
    cand record;
    v_limit int;
    v_lease int;
    v_quota int;
    v_count bigint;
    v_oldest timestamptz;
    v_n int := 0;
BEGIN
    IF reputation_floor_ban_halted() THEN
        RETURN;
    END IF;
    v_limit := LEAST(GREATEST(COALESCE(p_limit, 1), 1), 50);
    v_lease := LEAST(GREATEST(COALESCE(p_lease_seconds, 120), 30), 900);
    FOR cand IN
        SELECT b.id, b.tenant_id, b.community_id, b.action FROM bundle_reputation_floor_bans b
         WHERE b.attempts < 8
           AND (b.status = 1
                OR (b.status IN (3, 5) AND b.next_retry_at IS NOT NULL AND b.next_retry_at <= NOW())
                OR (b.status = 7 AND b.lease_expires_at < NOW()))
         ORDER BY b.updated_at
         LIMIT v_limit * 4
    LOOP
        EXIT WHEN v_n >= v_limit;
        IF NOT pg_try_advisory_xact_lock(
                hashtextextended('rep_floor_ban:' || cand.community_id, 0)) THEN
            CONTINUE;
        END IF;
        PERFORM 1 FROM bundle_reputation_floor_bans b
         WHERE b.id = cand.id AND b.attempts < 8
           AND (b.status = 1
                OR (b.status IN (3, 5) AND b.next_retry_at IS NOT NULL AND b.next_retry_at <= NOW())
                OR (b.status = 7 AND b.lease_expires_at < NOW()))
           FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        SELECT floor_ban_hourly_quota INTO v_quota FROM bundle_reputation_policy
         WHERE community_id = cand.community_id AND tenant_id = cand.tenant_id;
        IF NOT FOUND THEN
            v_quota := 60;
        END IF;
        SELECT COUNT(*), MIN(b.dispatched_at) INTO v_count, v_oldest
          FROM bundle_reputation_floor_bans b
         WHERE b.community_id = cand.community_id AND b.id <> cand.id AND b.action = 1
           AND b.dispatched_at > NOW() - INTERVAL '1 hour';
        -- The hourly BAN quota never throttles an UNBAN (action 2): it restores access.
        IF cand.action = 1 AND v_count >= v_quota THEN
            UPDATE bundle_reputation_floor_bans u
               SET status = 5, last_error = 'rate_limited', lease_token = NULL,
                   lease_expires_at = NULL, updated_at = NOW(),
                   next_retry_at = GREATEST(NOW() + INTERVAL '30 seconds',
                                            v_oldest + INTERVAL '1 hour')
                                   + random() * INTERVAL '60 seconds'
             WHERE u.id = cand.id;
            CONTINUE;
        END IF;
        UPDATE bundle_reputation_floor_bans u
           SET status = 7, attempts = u.attempts + 1, lease_token = gen_random_uuid(),
               lease_expires_at = NOW() + make_interval(secs => v_lease),
               dispatched_at = NOW(), next_retry_at = NULL, updated_at = NOW()
         WHERE u.id = cand.id
        RETURNING u.id, u.tenant_id, u.community_id, u.user_uuid, u.scope, u.platform,
                  u.attempts, u.lease_token, u.action
          INTO r_id, r_tenant_id, r_community_id, r_user_uuid, r_scope, r_platform,
               r_attempt, r_lease_token, r_action;
        v_n := v_n + 1;
        RETURN NEXT;
    END LOOP;
END
$fc$;

-- Re-check: an unban is never cancelled by a recovered score.
CREATE OR REPLACE FUNCTION reputation_floor_ban_recheck(p_id bigint, p_lease uuid)
RETURNS smallint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fk$
DECLARE
    v_tenant int;
    v_community int;
    v_user uuid;
    v_scope smallint;
    v_action smallint;
BEGIN
    SELECT tenant_id, community_id, user_uuid, scope, action
      INTO v_tenant, v_community, v_user, v_scope, v_action
      FROM bundle_reputation_floor_bans
     WHERE id = p_id AND status = 7 AND lease_token = p_lease FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 2;
    END IF;
    IF reputation_floor_ban_halted() THEN
        UPDATE bundle_reputation_floor_bans
           SET status = 1, attempts = GREATEST(attempts - 1, 0), lease_token = NULL,
               lease_expires_at = NULL, next_retry_at = NULL, updated_at = NOW()
         WHERE id = p_id;
        RETURN 3;
    END IF;
    -- An UNBAN restores access: never gated on a floor breach (a recovered score must not
    -- cancel it), only on the kill switch above.
    IF v_action = 2 OR reputation_verify_floor_breach(v_tenant, v_community, v_user, v_scope)
    THEN
        RETURN 0;
    END IF;
    UPDATE bundle_reputation_floor_bans
       SET status = 6, last_error = 'floor_not_breached', lease_token = NULL,
           lease_expires_at = NULL, completed_at = NOW(), updated_at = NOW()
     WHERE id = p_id;
    RETURN 1;
END
$fk$;

-- Erasure: done BAN rows need the keyed suppression code; lifted rows and done UNBAN rows get a
-- random pseudonym (a reversed ban needs no code and must not block the erasure).
CREATE OR REPLACE FUNCTION reputation_floor_ban_erase_subject(
    p_user_uuid uuid, p_tenant bigint, p_platform text, p_suppression_code bytea
) RETURNS TABLE(r_deleted int, r_pseudonymised int, r_pending int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fe$
DECLARE
    v_deleted int := 0;
    v_done int := 0;
    v_pseudonymised int := 0;
    v_pending int := 0;
    v_lifted int := 0;
BEGIN
    IF p_user_uuid IS NULL OR p_tenant IS NULL OR p_platform IS NULL
       OR p_platform !~ '^[a-z][a-z0-9]{1,31}$' THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023';
    END IF;
    DELETE FROM bundle_reputation_floor_bans
     WHERE user_uuid = p_user_uuid AND tenant_id = p_tenant AND platform = p_platform
       AND status = 6;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    SELECT count(*) INTO v_done FROM bundle_reputation_floor_bans
     WHERE user_uuid = p_user_uuid AND tenant_id = p_tenant AND platform = p_platform
       AND status = 2 AND action = 1;
    IF v_done > 0 THEN
        IF p_suppression_code IS NULL OR octet_length(p_suppression_code) < 16 THEN
            RAISE EXCEPTION 'suppression_code_required' USING ERRCODE = '22023';
        END IF;
        UPDATE bundle_reputation_floor_bans b
           SET user_uuid = substr(
                   encode(sha256(p_suppression_code || int8send(b.id)), 'hex'), 1, 32)::uuid,
               updated_at = NOW()
         WHERE b.user_uuid = p_user_uuid AND b.tenant_id = p_tenant AND b.platform = p_platform
           AND b.status = 2 AND b.action = 1;
        GET DIAGNOSTICS v_pseudonymised = ROW_COUNT;
    END IF;
    UPDATE bundle_reputation_floor_bans b
       SET user_uuid = gen_random_uuid(), updated_at = NOW()
     WHERE b.user_uuid = p_user_uuid AND b.tenant_id = p_tenant AND b.platform = p_platform
       AND (b.status = 8 OR (b.action = 2 AND b.status = 2));
    GET DIAGNOSTICS v_lifted = ROW_COUNT;
    v_pseudonymised := v_pseudonymised + v_lifted;
    SELECT count(*) INTO v_pending FROM bundle_reputation_floor_bans
     WHERE user_uuid = p_user_uuid AND tenant_id = p_tenant AND platform = p_platform
       AND status IN (1, 3, 4, 5, 7);
    RETURN QUERY SELECT v_deleted, v_pseudonymised, v_pending;
END
$fe$;

REVOKE ALL ON FUNCTION
    reputation_floor_current_score(bigint, bigint, uuid, smallint),
    reputation_floor_lift_rearm_drop(bigint, bigint, smallint),
    reputation_floor_ban_lift(bigint, uuid[], uuid),
    reputation_floor_ban_lift_from_platform(bigint, bigint, text, uuid),
    reputation_floor_ban_enqueue(bigint, bigint, bigint, uuid, smallint, text, text),
    reputation_floor_ban_claim(int, int),
    reputation_floor_ban_recheck(bigint, uuid),
    reputation_floor_ban_erase_subject(uuid, bigint, text, bytea)
    FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reputation') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_floor_ban_enqueue(bigint, bigint, bigint, uuid, smallint, text, text)
            TO waddles_bundle_reputation;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_svc_action') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_floor_ban_claim(int, int),
            reputation_floor_ban_recheck(bigint, uuid),
            reputation_floor_ban_lift_from_platform(bigint, bigint, text, uuid)
            TO waddles_svc_action;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_floor_ban_lift(bigint, uuid[], uuid),
            reputation_floor_ban_erase_subject(uuid, bigint, text, bytea)
            TO hub_api;
    END IF;
END
$$;
