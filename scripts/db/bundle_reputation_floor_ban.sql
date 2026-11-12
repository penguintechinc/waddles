-- Automated hard ban (reputation floor) -- SQL half. Applied by alembic 0102_reputation_floor_ban
-- (and by the Rust crate's integration test, after bundle_reputation_unify.sql). Idempotent.
--
-- Delivery model (a one-way outbox across two stages):
--   process stage (bundle role)  reputation_floor_ban_enqueue() re-verifies the breach under a
--                                per-community advisory lock, applies the hourly quota and records
--                                a QUEUED row -- it never talks to the platform;
--   action stage (svc_action)    reputation_floor_ban_claim() leases queued/due rows, the action
--                                stage emits the signed `mod.ban` outbound command to the sender,
--                                and reputation_floor_ban_complete() marks the row done ONLY on the
--                                sender's confirmed result (lease token checked).
-- The table is the full audit trail (actor, reason, scope, community, target uuid, outcome) AND the
-- durable retry record: nothing is silently dropped. Everything is SECURITY DEFINER with a pinned
-- search_path and no dynamic SQL; neither role has direct table access.

-- 1. Per-community throttle for floor bans (policy column; absent policy row == 60).
ALTER TABLE bundle_reputation_policy
    ADD COLUMN IF NOT EXISTS floor_ban_hourly_quota INTEGER NOT NULL DEFAULT 60
        CHECK (floor_ban_hourly_quota > 0);

-- 2. Audit + idempotency + durable outbox. One row per (target community, user, scope, key); the key
-- is HOST-derived (hash of the invocation event id + user + scope), never guest-chosen.
-- scope: 1 = community hard floor, 2 = tenant floor (one row per fanned-out community).
-- status: 1 queued (verified, awaiting dispatch), 2 done (sender-confirmed), 3 failed (retry due at
--         next_retry_at), 4 dead (attempts exhausted: alert), 5 rate limited (retried after the
--         quota window, at next_retry_at), 6 refused (floor not breached at re-verification),
--         7 dispatched (in flight under lease_token until lease_expires_at).
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
    status              SMALLINT    NOT NULL DEFAULT 1 CHECK (status IN (1, 2, 3, 4, 5, 6, 7)),
    attempts            INTEGER     NOT NULL DEFAULT 0,
    last_error          TEXT,
    next_retry_at       TIMESTAMPTZ,
    lease_token         UUID,
    lease_expires_at    TIMESTAMPTZ,
    dispatched_at       TIMESTAMPTZ,
    op_id               TEXT,
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
    ON bundle_reputation_floor_bans (status, next_retry_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_bundle_reputation_floor_bans_dispatched
    ON bundle_reputation_floor_bans (community_id, dispatched_at);
REVOKE ALL ON bundle_reputation_floor_bans FROM PUBLIC;

-- 2b. The durable KILL SWITCH, the single source of truth BOTH stages read (enqueue refuses, claim
-- returns nothing, recheck releases the row back to the queue) in addition to the PostHog flag each
-- stage checks. `halted` defaults FALSE; a missing control row reads as halted (fail closed).
CREATE TABLE IF NOT EXISTS bundle_reputation_floor_ban_control (
    id         SMALLINT    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    halted     BOOLEAN     NOT NULL DEFAULT FALSE,
    reason     TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO bundle_reputation_floor_ban_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
REVOKE ALL ON bundle_reputation_floor_ban_control FROM PUBLIC;

CREATE OR REPLACE FUNCTION reputation_floor_ban_halted() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $fh$
    SELECT COALESCE((SELECT halted FROM bundle_reputation_floor_ban_control WHERE id = 1), TRUE);
$fh$;

-- Operator switch (hub-api only): stops enqueue/dispatch of automated bans until cleared.
CREATE OR REPLACE FUNCTION reputation_floor_ban_set_halted(p_halted boolean, p_reason text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fs$
BEGIN
    IF p_halted IS NULL THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023';
    END IF;
    INSERT INTO bundle_reputation_floor_ban_control (id, halted, reason, updated_at)
    VALUES (1, p_halted, left(p_reason, 200), NOW())
    ON CONFLICT (id) DO UPDATE
        SET halted = EXCLUDED.halted, reason = EXCLUDED.reason, updated_at = NOW();
END
$fs$;

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

-- 4. Enqueue (process stage). Under ONE advisory lock per target community (it serialises the hourly
-- quota count and the slot's idempotency): (a) a slot already queued/done/in-flight/failed/dead is an
-- idempotent no-op; (b) the breach is RE-VERIFIED here, in the same transaction that records the
-- dispatch (a stale or raced decision never queues a ban); (c) the hourly quota is applied;
-- (d) a QUEUED row is recorded. A halted kill switch refuses; a user who already has
-- `v_user_cap` bans of this community inside the window is capped so one user can never exhaust
-- the community's quota. Returns (state, id): 0 queued, 1 already known, 2 rate limited (recorded,
-- retried after the quota window), 3 refused (floor not breached / halted, recorded), 4 capped
-- (the user already has pending/delivered bans in this window; nothing recorded, id 0).
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
    IF FOUND AND v_status IN (1, 2, 3, 4, 7) THEN
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
       AND created_at > NOW() - INTERVAL '1 hour' AND status IN (1, 2, 3, 5, 7)
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
       AND status IN (1, 2, 3, 7)
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

-- 5. Reap (action stage): rows that exhausted their attempts (failed past backoff, or a lease that
-- expired without a result on the final attempt) become DEAD (status 4) and are returned so the
-- caller can alert (ERROR log + metric). Never silently dropped.
CREATE OR REPLACE FUNCTION reputation_floor_ban_reap(p_limit int)
RETURNS TABLE(r_id bigint, r_tenant_id int, r_community_id int, r_scope smallint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fp$
BEGIN
    RETURN QUERY
    WITH dead AS (
        SELECT b.id FROM bundle_reputation_floor_bans b
         WHERE b.attempts >= 8
           AND ((b.status = 3 AND b.next_retry_at IS NOT NULL AND b.next_retry_at <= NOW())
                OR (b.status = 7 AND b.lease_expires_at < NOW()))
         ORDER BY b.updated_at
         LIMIT LEAST(GREATEST(COALESCE(p_limit, 1), 1), 100)
         FOR UPDATE SKIP LOCKED
    )
    UPDATE bundle_reputation_floor_bans u
       SET status = 4, last_error = 'attempts_exhausted', lease_token = NULL,
           lease_expires_at = NULL, next_retry_at = NULL, updated_at = NOW()
      FROM dead WHERE u.id = dead.id
    RETURNING u.id, u.tenant_id, u.community_id, u.scope;
END
$fp$;

-- 6. Claim (action stage): lease up to p_limit (<= 50) rows for dispatch -- queued rows, failed or
-- rate-limited rows past next_retry_at, and in-flight rows whose lease EXPIRED (a crashed
-- dispatcher). The hourly quota is enforced HERE, at dispatch, under the same per-community
-- advisory lock the enqueue takes, counting rows dispatched in the last hour by `dispatched_at`: a
-- row over quota is NOT leased -- it stays (or becomes) status 5 with a jittered next_retry_at
-- after the oldest dispatch leaves the window, so retried/promoted rows can never exceed the
-- community's quota. Nothing is claimed while the kill switch is halted. Each claim bumps
-- `attempts` and mints a fresh lease token; a row under an unexpired lease is never returned, and
-- only the current token may complete it (no double fire). Lock order is advisory-then-row and the
-- advisory lock is a TRY lock (a busy community is skipped until the next sweep): no deadlocks.
CREATE OR REPLACE FUNCTION reputation_floor_ban_claim(p_limit int, p_lease_seconds int)
RETURNS TABLE(r_id bigint, r_tenant_id int, r_community_id int, r_user_uuid uuid,
              r_scope smallint, r_platform text, r_attempt int, r_lease_token uuid)
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
        SELECT b.id, b.tenant_id, b.community_id FROM bundle_reputation_floor_bans b
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
         WHERE b.community_id = cand.community_id AND b.id <> cand.id
           AND b.dispatched_at > NOW() - INTERVAL '1 hour';
        IF v_count >= v_quota THEN
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
                  u.attempts, u.lease_token
          INTO r_id, r_tenant_id, r_community_id, r_user_uuid, r_scope, r_platform,
               r_attempt, r_lease_token;
        v_n := v_n + 1;
        RETURN NEXT;
    END LOOP;
END
$fc$;

-- 6b. Backlog (action stage alert source): rows still waiting for delivery. The dispatcher raises an
-- ERROR + metric when the pending count or the oldest wait crosses its threshold.
CREATE OR REPLACE FUNCTION reputation_floor_ban_backlog()
RETURNS TABLE(r_pending bigint, r_rate_limited bigint, r_oldest_seconds bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $fg$
    SELECT COUNT(*) FILTER (WHERE status IN (1, 3)),
           COUNT(*) FILTER (WHERE status = 5),
           COALESCE(EXTRACT(EPOCH FROM (NOW() - MIN(created_at)))::bigint, 0)
      FROM bundle_reputation_floor_bans WHERE status IN (1, 3, 5);
$fg$;

-- 7. Re-check (action stage), immediately before the command is emitted: the leased row's breach is
-- verified AGAIN and the kill switch is re-read. 0 = still breached (go), 1 = no longer breached (row
-- marked refused, do not send), 2 = the lease is not ours any more (do nothing), 3 = the kill switch
-- is halted (the row is released back to the queue, attempt refunded; nothing is sent).
CREATE OR REPLACE FUNCTION reputation_floor_ban_recheck(p_id bigint, p_lease uuid)
RETURNS smallint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fk$
DECLARE
    v_tenant int;
    v_community int;
    v_user uuid;
    v_scope smallint;
BEGIN
    SELECT tenant_id, community_id, user_uuid, scope
      INTO v_tenant, v_community, v_user, v_scope
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
    IF reputation_verify_floor_breach(v_tenant, v_community, v_user, v_scope) THEN
        RETURN 0;
    END IF;
    UPDATE bundle_reputation_floor_bans
       SET status = 6, last_error = 'floor_not_breached', lease_token = NULL,
           lease_expires_at = NULL, completed_at = NOW(), updated_at = NOW()
     WHERE id = p_id;
    RETURN 1;
END
$fk$;

-- 8. Complete (action stage): record the sender's CONFIRMED result for the lease holder. p_outcome:
-- 1 done (the sender acknowledged the ban), 2 failed (backoff 30s..1h, DEAD after 8 attempts),
-- 3 refused for good (retrying cannot help, e.g. a protected target: status 6).
-- Returns the new status, or 0 when the lease is not the caller's (a stale/expired lease: no-op).
-- p_error is a stable machine code (^[a-z_]{1,64}$) or stored as 'unknown'; never free text.
CREATE OR REPLACE FUNCTION reputation_floor_ban_complete(
    p_id bigint, p_lease uuid, p_outcome smallint, p_error text, p_op_id text
) RETURNS smallint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $fm$
DECLARE
    v_err text;
    v_attempts int;
    v_new smallint;
BEGIN
    IF p_outcome IS NULL OR p_outcome NOT IN (1, 2, 3) THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023';
    END IF;
    v_err := CASE WHEN p_error ~ '^[a-z_]{1,64}$' THEN p_error ELSE 'unknown' END;
    SELECT attempts INTO v_attempts FROM bundle_reputation_floor_bans
     WHERE id = p_id AND status = 7 AND lease_token = p_lease FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 0;
    END IF;
    v_new := CASE WHEN p_outcome = 1 THEN 2 WHEN p_outcome = 3 THEN 6
                  WHEN v_attempts >= 8 THEN 4 ELSE 3 END;
    UPDATE bundle_reputation_floor_bans b
       SET status = v_new,
           last_error = CASE WHEN p_outcome = 1 THEN NULL ELSE v_err END,
           op_id = CASE WHEN p_op_id ~ '^[A-Za-z0-9_-]{1,64}$' THEN p_op_id ELSE b.op_id END,
           completed_at = CASE WHEN v_new IN (2, 6) THEN NOW() ELSE b.completed_at END,
           next_retry_at = CASE WHEN v_new = 3
               THEN NOW() + LEAST(INTERVAL '1 hour',
                                  INTERVAL '30 seconds' * power(2, b.attempts))
               ELSE NULL END,
           lease_token = NULL, lease_expires_at = NULL, updated_at = NOW()
     WHERE b.id = p_id;
    RETURN v_new;
END
$fm$;

-- 9. The tenant's live communities (the tenant-floor fan-out set). The host enumerates through
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
    reputation_floor_ban_enqueue(bigint, bigint, bigint, uuid, smallint, text, text),
    reputation_floor_ban_reap(int),
    reputation_floor_ban_claim(int, int),
    reputation_floor_ban_backlog(),
    reputation_floor_ban_recheck(bigint, uuid),
    reputation_floor_ban_complete(bigint, uuid, smallint, text, text),
    reputation_floor_ban_halted(),
    reputation_floor_ban_set_halted(boolean, text),
    reputation_tenant_communities(bigint)
    FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reputation') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_verify_floor_breach(bigint, bigint, uuid, smallint),
            reputation_floor_ban_enqueue(bigint, bigint, bigint, uuid, smallint, text, text),
            reputation_floor_ban_halted(),
            reputation_tenant_communities(bigint)
            TO waddles_bundle_reputation;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_svc_action') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_floor_ban_reap(int),
            reputation_floor_ban_claim(int, int),
            reputation_floor_ban_backlog(),
            reputation_floor_ban_recheck(bigint, uuid),
            reputation_floor_ban_complete(bigint, uuid, smallint, text, text),
            reputation_floor_ban_halted()
            TO waddles_svc_action;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api') THEN
        GRANT SELECT ON bundle_reputation_floor_bans TO hub_api;
        GRANT SELECT ON bundle_reputation_floor_ban_control TO hub_api;
        GRANT EXECUTE ON FUNCTION reputation_floor_ban_set_halted(boolean, text),
                                  reputation_floor_ban_halted() TO hub_api;
        GRANT SELECT, INSERT, UPDATE ON bundle_reputation_policy TO hub_api;
    END IF;
END
$$;
