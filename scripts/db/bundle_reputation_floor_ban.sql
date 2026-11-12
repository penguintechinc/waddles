-- Automated hard ban (reputation floor) -- SQL half. Applied by alembic 0098_reputation_floor_ban
-- (and by the Rust crate's integration test, after bundle_reputation_unify.sql). Idempotent.
--
-- The platform ban tool re-verifies every floor-triggered ban here before acting
-- (reputation_verify_floor_breach), claims an idempotency/rate-limit slot
-- (reputation_floor_ban_begin), and records the outcome (reputation_floor_ban_record). The table
-- is the full audit trail (actor, reason, scope, community, target uuid, outcome) AND the durable
-- retry record: a failed community ban is status 3 with a backoff `next_retry_at`, picked up again
-- through reputation_floor_ban_due -- never silently dropped. Everything is SECURITY DEFINER with a
-- pinned search_path and no dynamic SQL; the bundle role has no direct table access.

-- 1. Per-community throttle for floor bans (policy column; absent policy row == 60).
ALTER TABLE bundle_reputation_policy
    ADD COLUMN IF NOT EXISTS floor_ban_hourly_quota INTEGER NOT NULL DEFAULT 60
        CHECK (floor_ban_hourly_quota > 0);

-- 2. Audit + idempotency + durable retry. One row per (target community, user, scope, key).
-- scope: 1 = community hard floor, 2 = tenant floor (one row per fanned-out community).
-- status: 1 pending, 2 done, 3 failed (retry due at next_retry_at; NULL = attempts exhausted),
--         4 refused (floor not breached at re-verification), 5 rate limited.
CREATE TABLE IF NOT EXISTS bundle_reputation_floor_bans (
    id                  BIGSERIAL   PRIMARY KEY,
    tenant_id           INTEGER     NOT NULL REFERENCES tenants(id),
    community_id        INTEGER     NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    origin_community_id INTEGER     NOT NULL,
    user_uuid           UUID        NOT NULL,
    scope               SMALLINT    NOT NULL CHECK (scope IN (1, 2)),
    idempotency_key     TEXT        NOT NULL CHECK (idempotency_key ~ '^[a-z0-9._:-]{1,128}$'),
    actor               TEXT        NOT NULL DEFAULT 'system:reputation_floor'
                        CHECK (actor = 'system:reputation_floor'),
    reason              TEXT        NOT NULL,
    platform            TEXT        NOT NULL CHECK (platform ~ '^[a-z][a-z0-9]{1,31}$'),
    status              SMALLINT    NOT NULL DEFAULT 1 CHECK (status IN (1, 2, 3, 4, 5)),
    attempts            INTEGER     NOT NULL DEFAULT 0,
    last_error          TEXT,
    next_retry_at       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    CONSTRAINT uq_bundle_reputation_floor_bans UNIQUE
        (tenant_id, community_id, user_uuid, scope, idempotency_key),
    CONSTRAINT ck_bundle_reputation_floor_bans_reason
        CHECK (reason IN ('community_hard_floor', 'tenant_floor'))
);
CREATE INDEX IF NOT EXISTS idx_bundle_reputation_floor_bans_community_recent
    ON bundle_reputation_floor_bans (community_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bundle_reputation_floor_bans_due
    ON bundle_reputation_floor_bans (tenant_id, status, next_retry_at);
REVOKE ALL ON bundle_reputation_floor_bans FROM PUBLIC;

-- 3. Re-verification: TRUE only if the scope's ban is enabled AND the user's score is below the
-- floor. p_scope 1 = community hard (hard_ban_enabled + hard_ban_floor, the community score,
-- unscored = policy default); 2 = tenant (tenant_auto_ban_enabled + tenant_auto_ban_floor on the
-- FIXED 300..850 tenant aggregate, unscored = 600). The community must belong to the tenant in
-- both scopes. Any malformed/unknown input is FALSE (fail closed), never an error oracle.
CREATE OR REPLACE FUNCTION reputation_verify_floor_breach(
    p_tenant bigint, p_community bigint, p_user_uuid uuid, p_scope smallint
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $vf$
DECLARE
    v_enabled boolean;
    v_floor int;
    v_default int;
    v_score int;
BEGIN
    IF p_tenant IS NULL OR p_community IS NULL OR p_user_uuid IS NULL
       OR p_scope IS NULL OR p_scope NOT IN (1, 2) THEN
        RETURN false;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM communities WHERE id = p_community AND tenant_id = p_tenant) THEN
        RETURN false;
    END IF;
    IF p_scope = 1 THEN
        SELECT hard_ban_enabled, hard_ban_floor, score_default INTO v_enabled, v_floor, v_default
          FROM bundle_reputation_policy
         WHERE community_id = p_community AND tenant_id = p_tenant;
        IF NOT FOUND OR NOT v_enabled THEN
            RETURN false;
        END IF;
        SELECT balance INTO v_score FROM bundle_reputation_scores
         WHERE community_id = p_community AND user_uuid = p_user_uuid AND tenant_id = p_tenant;
        RETURN COALESCE(v_score, v_default) < v_floor;
    END IF;
    SELECT tenant_auto_ban_enabled, tenant_auto_ban_floor INTO v_enabled, v_floor
      FROM bundle_reputation_tenant_policy WHERE tenant_id = p_tenant;
    IF NOT FOUND OR NOT v_enabled THEN
        RETURN false;
    END IF;
    SELECT score INTO v_score FROM bundle_reputation_tenant_scores
     WHERE tenant_id = p_tenant AND user_uuid = p_user_uuid;
    RETURN COALESCE(v_score, 600) < v_floor;
END
$vf$;

-- 4. Claim the (target community, user, scope, key) slot. Returns 0 = proceed (attempt recorded as
-- pending), 1 = already done (idempotent replay: do nothing), 2 = rate limited (recorded, do
-- nothing). Rate limit: floor_ban_hourly_quota (default 60) begun attempts per target community
-- per rolling hour. Serialised per slot by an advisory lock so concurrent replays cannot double-act.
CREATE OR REPLACE FUNCTION reputation_floor_ban_begin(
    p_tenant bigint, p_community bigint, p_origin bigint, p_user_uuid uuid,
    p_scope smallint, p_key text, p_platform text
) RETURNS smallint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fb$
DECLARE
    v_status smallint;
    v_quota int := 60;
    v_recent bigint;
    v_reason text;
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
        RAISE EXCEPTION 'reputation_floor_ban_begin: community is not in the named tenant'
            USING ERRCODE = '42501';
    END IF;
    v_reason := CASE p_scope WHEN 1 THEN 'community_hard_floor' ELSE 'tenant_floor' END;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'rep_floor_ban:' || p_community || ':' || p_user_uuid || ':' || p_scope || ':' || p_key, 0));

    SELECT status INTO v_status FROM bundle_reputation_floor_bans
     WHERE tenant_id = p_tenant AND community_id = p_community AND user_uuid = p_user_uuid
       AND scope = p_scope AND idempotency_key = p_key;
    IF FOUND AND v_status = 2 THEN
        RETURN 1;
    END IF;

    SELECT floor_ban_hourly_quota INTO v_quota FROM bundle_reputation_policy
     WHERE community_id = p_community AND tenant_id = p_tenant;
    IF NOT FOUND THEN
        v_quota := 60;
    END IF;
    SELECT COUNT(*) INTO v_recent FROM bundle_reputation_floor_bans
     WHERE community_id = p_community AND created_at > NOW() - INTERVAL '1 hour'
       AND status IN (1, 2, 3)
       AND NOT (user_uuid = p_user_uuid AND scope = p_scope AND idempotency_key = p_key);
    IF v_recent >= v_quota THEN
        INSERT INTO bundle_reputation_floor_bans
            (tenant_id, community_id, origin_community_id, user_uuid, scope, idempotency_key,
             reason, platform, status, last_error)
        VALUES (p_tenant, p_community, p_origin, p_user_uuid, p_scope, p_key, v_reason,
                p_platform, 5, 'rate_limited')
        ON CONFLICT (tenant_id, community_id, user_uuid, scope, idempotency_key)
        DO UPDATE SET status = 5, last_error = 'rate_limited', updated_at = NOW();
        RETURN 2;
    END IF;

    INSERT INTO bundle_reputation_floor_bans
        (tenant_id, community_id, origin_community_id, user_uuid, scope, idempotency_key,
         reason, platform, status, attempts)
    VALUES (p_tenant, p_community, p_origin, p_user_uuid, p_scope, p_key, v_reason,
            p_platform, 1, 1)
    ON CONFLICT (tenant_id, community_id, user_uuid, scope, idempotency_key)
    DO UPDATE SET status = 1, attempts = bundle_reputation_floor_bans.attempts + 1,
                  next_retry_at = NULL, updated_at = NOW();
    RETURN 0;
END
$fb$;

-- 5. Record the outcome of a begun attempt (only a pending row can be recorded). p_outcome:
-- 1 done, 2 failed (durable retry, exponential backoff 30s..1h, exhausted after 8 attempts: kept as
-- status 3 with next_retry_at NULL so it stays visible), 3 refused (re-verification said no).
-- p_error is a stable machine code (^[a-z_]{1,64}$) or stored as 'unknown'; never free text.
CREATE OR REPLACE FUNCTION reputation_floor_ban_record(
    p_tenant bigint, p_community bigint, p_user_uuid uuid, p_scope smallint, p_key text,
    p_outcome smallint, p_error text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fr$
DECLARE
    v_err text;
BEGIN
    IF p_outcome IS NULL OR p_outcome NOT IN (1, 2, 3) THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023';
    END IF;
    v_err := CASE WHEN p_error ~ '^[a-z_]{1,64}$' THEN p_error ELSE 'unknown' END;
    UPDATE bundle_reputation_floor_bans b
       SET status = CASE p_outcome WHEN 1 THEN 2 WHEN 2 THEN 3 ELSE 4 END,
           last_error = CASE WHEN p_outcome = 1 THEN NULL ELSE v_err END,
           completed_at = CASE WHEN p_outcome = 1 THEN NOW() ELSE b.completed_at END,
           next_retry_at = CASE
               WHEN p_outcome = 2 AND b.attempts < 8
                   THEN NOW() + LEAST(INTERVAL '1 hour',
                                      INTERVAL '30 seconds' * power(2, b.attempts))
               ELSE NULL END,
           updated_at = NOW()
     WHERE b.tenant_id = p_tenant AND b.community_id = p_community AND b.user_uuid = p_user_uuid
       AND b.scope = p_scope AND b.idempotency_key = p_key AND b.status = 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023',
            DETAIL = 'no pending floor-ban attempt to record';
    END IF;
END
$fr$;

-- 6. Claim up to p_limit (<= 50) retries due for the tenant: failed rows past next_retry_at and
-- stale pending rows (a crashed attempt). Each claimed row is leased for 5 minutes so two replicas
-- never retry the same row at once.
CREATE OR REPLACE FUNCTION reputation_floor_ban_due(p_tenant bigint, p_limit int)
RETURNS TABLE(r_community_id int, r_origin_community_id int, r_user_uuid uuid,
              r_scope smallint, r_key text, r_platform text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fd$
BEGIN
    RETURN QUERY
    WITH due AS (
        SELECT b.id FROM bundle_reputation_floor_bans b
         WHERE b.tenant_id = p_tenant
           AND ((b.status = 3 AND b.next_retry_at IS NOT NULL AND b.next_retry_at <= NOW())
                OR (b.status = 1 AND b.updated_at < NOW() - INTERVAL '10 minutes'))
         ORDER BY b.updated_at
         LIMIT LEAST(GREATEST(COALESCE(p_limit, 1), 1), 50)
         FOR UPDATE SKIP LOCKED
    )
    UPDATE bundle_reputation_floor_bans u
       SET next_retry_at = NOW() + INTERVAL '5 minutes', status = 3, updated_at = NOW()
      FROM due WHERE u.id = due.id
    RETURNING u.community_id, u.origin_community_id, u.user_uuid, u.scope, u.idempotency_key,
              u.platform;
END
$fd$;

-- 7. The tenant's live communities (the tenant-floor fan-out set). The host enumerates through
-- here; the caller never supplies the list. Bounded: > 1000 rows is returned as 1001 and the host
-- fails loudly rather than banning a silently truncated set.
CREATE OR REPLACE FUNCTION reputation_tenant_communities(p_tenant bigint)
RETURNS TABLE(r_community_id int)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $tc$
    SELECT c.id FROM communities c
     WHERE c.tenant_id = p_tenant AND COALESCE(c.is_active, TRUE) AND c.deleted_at IS NULL
     ORDER BY c.id LIMIT 1001;
$tc$;

REVOKE ALL ON FUNCTION
    reputation_verify_floor_breach(bigint, bigint, uuid, smallint),
    reputation_floor_ban_begin(bigint, bigint, bigint, uuid, smallint, text, text),
    reputation_floor_ban_record(bigint, bigint, uuid, smallint, text, smallint, text),
    reputation_floor_ban_due(bigint, int),
    reputation_tenant_communities(bigint)
    FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reputation') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_verify_floor_breach(bigint, bigint, uuid, smallint),
            reputation_floor_ban_begin(bigint, bigint, bigint, uuid, smallint, text, text),
            reputation_floor_ban_record(bigint, bigint, uuid, smallint, text, smallint, text),
            reputation_floor_ban_due(bigint, int),
            reputation_tenant_communities(bigint)
            TO waddles_bundle_reputation;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api') THEN
        GRANT SELECT ON bundle_reputation_floor_bans TO hub_api;
        GRANT SELECT, INSERT, UPDATE ON bundle_reputation_policy TO hub_api;
    END IF;
END
$$;
