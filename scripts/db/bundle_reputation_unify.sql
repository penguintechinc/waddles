-- Reputation unification DDL (single store = bundle store). Read by BOTH
-- `alembic/versions/0097_reputation_unify.py` (production) and the Rust crate's
-- integration test (`include_str!`), so the tested schema is the shipped schema.
-- Idempotent; runs AFTER `bundle_reputation_store.sql` (0050) and
-- `bundle_reputation_host_ops.sql` (0072). The legacy-data backfill and the one-off
-- 0-based -> 300-850 rebase live in the migration, NOT here (they must run exactly once).

-- 1. Per-community policy: score scale + auto-ban + at-risk. Absent row == defaults below
-- (300-850, start 600, auto-ban OFF). Scale is fixed by product decision; the columns exist
-- so the scale is policy-driven, not hardcoded in the function.
CREATE TABLE IF NOT EXISTS bundle_reputation_policy (
    tenant_id         INTEGER     NOT NULL REFERENCES tenants(id),
    community_id      INTEGER     NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    score_min         INTEGER     NOT NULL DEFAULT 300,
    score_max         INTEGER     NOT NULL DEFAULT 850,
    score_default     INTEGER     NOT NULL DEFAULT 600,
    -- two independent floors, each with its own flag (both default OFF): score < soft floor
    -- -> soft ban (cannot WIN anything in this community); score < hard floor -> ban here.
    soft_ban_enabled  BOOLEAN     NOT NULL DEFAULT FALSE,
    soft_ban_floor    INTEGER     NOT NULL DEFAULT 550,
    hard_ban_enabled  BOOLEAN     NOT NULL DEFAULT FALSE,
    hard_ban_floor    INTEGER     NOT NULL DEFAULT 400,
    at_risk_threshold INTEGER     NOT NULL DEFAULT 600,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (community_id),
    CONSTRAINT ck_bundle_reputation_policy_scale
        CHECK (score_min < score_max AND score_default BETWEEN score_min AND score_max),
    CONSTRAINT ck_bundle_reputation_policy_thresholds
        CHECK (hard_ban_floor >= score_min AND hard_ban_floor < soft_ban_floor
               AND soft_ban_floor < at_risk_threshold AND at_risk_threshold <= score_max)
);
REVOKE ALL ON bundle_reputation_policy FROM PUBLIC;

-- 1a. SQL-enforced rolling-24h caps live in the policy row (the function takes no cap params, so
-- no caller -- Rust host or hub-api -- ever computes or supplies a cap). Absent row == defaults.
ALTER TABLE bundle_reputation_policy
    ADD COLUMN IF NOT EXISTS activity_user_daily_cap INTEGER NOT NULL DEFAULT 100
        CHECK (activity_user_daily_cap > 0),
    ADD COLUMN IF NOT EXISTS activity_app_daily_cap INTEGER NOT NULL DEFAULT 2000
        CHECK (activity_app_daily_cap > 0),
    ADD COLUMN IF NOT EXISTS moderation_user_daily_cap INTEGER NOT NULL DEFAULT 200
        CHECK (moderation_user_daily_cap > 0),
    ADD COLUMN IF NOT EXISTS manual_soft_ban_daily_quota INTEGER NOT NULL DEFAULT 25
        CHECK (manual_soft_ban_daily_quota > 0);

-- 1b. Per-TENANT policy (tenant admins only): compared against the user's tenant aggregate
-- score; effect (enforced by the accrual bundle, flag waddles.reputation-auto-ban) is a ban
-- across every community of the tenant. Independent of the community policy above.
CREATE TABLE IF NOT EXISTS bundle_reputation_tenant_policy (
    tenant_id                  INTEGER     NOT NULL REFERENCES tenants(id),
    tenant_auto_ban_enabled    BOOLEAN     NOT NULL DEFAULT FALSE,
    tenant_auto_ban_floor      INTEGER     NOT NULL DEFAULT 350,
    tenant_at_risk_threshold   INTEGER     NOT NULL DEFAULT 450,
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id),
    CONSTRAINT ck_bundle_reputation_tenant_policy_thresholds
        CHECK (tenant_auto_ban_floor >= 300 AND tenant_auto_ban_floor < tenant_at_risk_threshold
               AND tenant_at_risk_threshold <= 850)
);
REVOKE ALL ON bundle_reputation_tenant_policy FROM PUBLIC;

-- 1c. Soft bans, per (community, user): the user cannot WIN anything in that community (games,
-- raffles, giveaways, economy payouts) but is not removed. reason_code: 1 = score floor,
-- 2 = manual. expires_at NULL = until cleared. There is deliberately NO platform/global ban.
CREATE TABLE IF NOT EXISTS bundle_reputation_soft_bans (
    tenant_id    INTEGER     NOT NULL REFERENCES tenants(id),
    community_id INTEGER     NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    user_uuid    UUID        NOT NULL,
    reason_code  SMALLINT    NOT NULL,
    set_by       TEXT        NOT NULL,
    set_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ,
    PRIMARY KEY (community_id, user_uuid),
    CONSTRAINT ck_bundle_reputation_soft_bans_reason CHECK (reason_code IN (1, 2))
);
REVOKE ALL ON bundle_reputation_soft_bans FROM PUBLIC;

-- 2. Tenant aggregate: one row per (tenant, user), moved by the same atomic apply as the
-- community score. A user reads ONLY their own aggregate (never another user's or tenant's).
CREATE TABLE IF NOT EXISTS bundle_reputation_tenant_scores (
    tenant_id     INTEGER     NOT NULL REFERENCES tenants(id),
    user_uuid     UUID        NOT NULL,
    score         INTEGER     NOT NULL DEFAULT 600,
    event_count   BIGINT      NOT NULL DEFAULT 0,
    last_event_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_uuid)
);
REVOKE ALL ON bundle_reputation_tenant_scores FROM PUBLIC;

-- 3. Ledger: who/why/where. `app_id` is nullable (hub-api/admin writes have no app); `actor` is
-- a human admin's UUID text or 'system:<name>' -- NEVER a numeric key; `reason_code_id` is the
-- numeric reason enum (legacy varchar `reason_code` keeps its decimal text for old readers).
ALTER TABLE bundle_reputation_adjustments ALTER COLUMN app_id DROP NOT NULL;
ALTER TABLE bundle_reputation_adjustments ADD COLUMN IF NOT EXISTS actor TEXT;
ALTER TABLE bundle_reputation_adjustments ADD COLUMN IF NOT EXISTS source_event TEXT;
ALTER TABLE bundle_reputation_adjustments ADD COLUMN IF NOT EXISTS reason_code_id SMALLINT;

-- Ledger kinds (contract): adjust = capped activity, moderation = capped negative moderation,
-- admin_override = uncapped admin write, soft_ban / soft_ban_clear = soft-ban history (delta 0).
ALTER TABLE bundle_reputation_adjustments
    DROP CONSTRAINT IF EXISTS ck_bundle_reputation_adjustments_kind;
ALTER TABLE bundle_reputation_adjustments
    ADD CONSTRAINT ck_bundle_reputation_adjustments_kind
    CHECK (kind IN ('adjust', 'moderation', 'admin_override', 'soft_ban', 'soft_ban_clear'));

-- app-less keyed rows are idempotent per (scope, key, target) -- the 0072 index cannot dedup
-- them (NULL app_id never collides).
CREATE UNIQUE INDEX IF NOT EXISTS uq_bundle_reputation_adjustments_idem_noapp
    ON bundle_reputation_adjustments (tenant_id, community_id, idempotency_key, target_user_uuid)
    WHERE idempotency_key IS NOT NULL AND app_id IS NULL;

-- rolling-24h cap scans (per community+kind, per app).
CREATE INDEX IF NOT EXISTS idx_bundle_reputation_adjustments_kind_window
    ON bundle_reputation_adjustments (community_id, kind, occurred_at);

-- Retire the pre-contract signatures (the contract-shaped functions replace them).
DROP FUNCTION IF EXISTS
    reputation_apply(bigint, bigint, uuid, int, smallint, text, text, text);
DROP FUNCTION IF EXISTS reputation_clear_soft_ban(bigint, bigint, uuid);

-- 4. The one score write path. SECURITY DEFINER, pinned search_path, no dynamic SQL; every check
-- below runs inside ONE transaction under the (community, user) score-row lock, so no caller --
-- bundle role or hub-api -- can skip membership, validation, caps, idempotency or the clamp.
--
--   p_reason_class: 1 = activity (kind 'adjust'), 2 = moderation (kind 'moderation', delta must
--   be negative), 3 = admin_override (kind 'admin_override', uncapped, delta 0 allowed).
--   Only a privileged session (superuser or member of role `hub_api`) may use class 3 or a
--   human-UUID actor; every other caller (the bundle role) must pass actor 'system:<name>'.
--   Caps (rolling 24h, sum of |applied delta|) are read from bundle_reputation_policy
--   (defaults 100 per-user activity, 2000 per-app activity, 200 per-user moderation); breach =
--   SQLSTATE 54000 'daily_cap_exceeded'. Idempotent on (community, idempotency_key): a replay
--   returns the original balance with applied = false and writes NO ledger row.
CREATE OR REPLACE FUNCTION reputation_apply(
    p_tenant bigint,
    p_community bigint,
    p_user_uuid uuid,
    p_delta int,
    p_reason_class smallint,
    p_reason_code smallint,
    p_app_id text,
    p_actor text,
    p_source_event text,
    p_idempotency_key text
) RETURNS TABLE(new_score int, applied bool)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    v_min int := 300;
    v_max int := 850;
    v_default int := 600;
    v_soft int := 550;
    v_user_cap int := 100;
    v_app_cap int := 2000;
    v_mod_cap int := 200;
    v_balance bigint;
    v_new bigint;
    v_applied int;
    v_prior bigint;
    v_used bigint;
    v_kind text;
    v_privileged boolean;
    v_cleared int;
BEGIN
    IF p_user_uuid IS NULL OR p_delta IS NULL OR p_reason_class IS NULL
       OR p_reason_code IS NULL OR p_source_event IS NULL OR p_idempotency_key IS NULL
       OR p_actor IS NULL THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023',
            DETAIL = 'user, delta, reason, actor, source_event and idempotency_key are required';
    END IF;
    IF p_reason_class NOT IN (1, 2, 3) OR p_reason_code < 1 THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023', DETAIL = 'bad reason';
    END IF;
    IF p_source_event !~ '^[a-z0-9._:-]{1,128}$' OR p_idempotency_key !~ '^[a-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023',
            DETAIL = 'source_event/idempotency_key must match [a-z0-9._:-]{1,128}';
    END IF;
    IF NOT (p_actor ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            OR p_actor ~ '^system:[a-z_]+$') THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023',
            DETAIL = 'actor must be a UUID or system:<name>';
    END IF;
    IF p_app_id IS NOT NULL AND length(p_app_id) NOT BETWEEN 1 AND 255 THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023', DETAIL = 'bad app_id';
    END IF;
    IF p_delta = 0 AND p_reason_class <> 3 THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023', DETAIL = 'delta must be non-zero';
    END IF;
    IF p_reason_class = 2 AND p_delta > 0 THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023',
            DETAIL = 'moderation delta must be negative (positive requires admin_override)';
    END IF;

    v_privileged := COALESCE((SELECT rolsuper FROM pg_roles WHERE rolname = session_user), false)
        OR (to_regrole('hub_api') IS NOT NULL AND pg_has_role(session_user, 'hub_api', 'member'));
    IF NOT v_privileged AND (p_reason_class = 3 OR p_actor !~ '^system:[a-z_]+$') THEN
        RAISE EXCEPTION 'reputation_apply: admin_override and human actors need the hub-api role'
            USING ERRCODE = '42501';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM communities WHERE id = p_community AND tenant_id = p_tenant) THEN
        RAISE EXCEPTION 'reputation_apply: community is not in the named tenant'
            USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM community_members
     WHERE community_id = p_community AND user_uuid = p_user_uuid
       AND is_active AND left_at IS NULL AND removed_at IS NULL
       FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'not_a_member' USING ERRCODE = '42501';
    END IF;

    SELECT score_min, score_max, score_default, soft_ban_floor, activity_user_daily_cap,
           activity_app_daily_cap, moderation_user_daily_cap
      INTO v_min, v_max, v_default, v_soft, v_user_cap, v_app_cap, v_mod_cap
      FROM bundle_reputation_policy WHERE community_id = p_community;
    IF NOT FOUND THEN
        v_min := 300; v_max := 850; v_default := 600; v_soft := 550;
        v_user_cap := 100; v_app_cap := 2000; v_mod_cap := 200;
    END IF;

    IF p_app_id IS NOT NULL THEN
        -- serialize the per-app (community-wide) cap across users
        PERFORM pg_advisory_xact_lock(
            hashtextextended('rep_app:' || p_community || ':' || p_app_id, 0));
    END IF;

    INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance)
    VALUES (p_tenant, p_community, p_user_uuid, v_default)
    ON CONFLICT (community_id, user_uuid) DO NOTHING;

    SELECT balance INTO v_balance FROM bundle_reputation_scores
     WHERE community_id = p_community AND user_uuid = p_user_uuid
       AND tenant_id = p_tenant FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'reputation_apply: score row belongs to another tenant'
            USING ERRCODE = '42501';
    END IF;

    SELECT balance_after INTO v_prior FROM bundle_reputation_adjustments
     WHERE tenant_id = p_tenant AND community_id = p_community
       AND target_user_uuid = p_user_uuid::text AND idempotency_key = p_idempotency_key
     LIMIT 1;
    IF FOUND THEN
        RETURN QUERY SELECT COALESCE(v_prior, v_balance)::int, false;
        RETURN;
    END IF;

    v_new := LEAST(GREATEST(v_balance + p_delta, v_min), v_max);
    v_applied := (v_new - v_balance)::int;
    v_kind := CASE p_reason_class WHEN 1 THEN 'adjust' WHEN 2 THEN 'moderation'
                                  ELSE 'admin_override' END;

    IF p_reason_class = 1 THEN
        SELECT COALESCE(SUM(abs(delta)), 0) INTO v_used FROM bundle_reputation_adjustments
         WHERE community_id = p_community AND target_user_uuid = p_user_uuid::text
           AND kind = 'adjust' AND occurred_at > NOW() - INTERVAL '24 hours';
        IF v_used + abs(v_applied) > v_user_cap THEN
            RAISE EXCEPTION 'daily_cap_exceeded' USING ERRCODE = '54000', DETAIL = 'per-user';
        END IF;
        IF p_app_id IS NOT NULL THEN
            SELECT COALESCE(SUM(abs(delta)), 0) INTO v_used FROM bundle_reputation_adjustments
             WHERE community_id = p_community AND app_id = p_app_id
               AND kind = 'adjust' AND occurred_at > NOW() - INTERVAL '24 hours';
            IF v_used + abs(v_applied) > v_app_cap THEN
                RAISE EXCEPTION 'daily_cap_exceeded' USING ERRCODE = '54000', DETAIL = 'per-app';
            END IF;
        END IF;
    ELSIF p_reason_class = 2 THEN
        SELECT COALESCE(SUM(abs(delta)), 0) INTO v_used FROM bundle_reputation_adjustments
         WHERE community_id = p_community AND target_user_uuid = p_user_uuid::text
           AND kind = 'moderation' AND occurred_at > NOW() - INTERVAL '24 hours';
        IF v_used + abs(v_applied) > v_mod_cap THEN
            RAISE EXCEPTION 'daily_cap_exceeded' USING ERRCODE = '54000', DETAIL = 'moderation';
        END IF;
    END IF;

    INSERT INTO bundle_reputation_adjustments
        (app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code,
         reason_code_id, kind, requested_value, balance_after, idempotency_key, actor,
         source_event)
    VALUES
        (p_app_id, p_tenant, p_community, p_user_uuid::text, 'community', v_applied,
         p_reason_code::text, p_reason_code, v_kind, p_delta, v_new, p_idempotency_key,
         p_actor, p_source_event);

    UPDATE bundle_reputation_scores
       SET balance = v_new, adjustment_count = adjustment_count + 1, updated_at = NOW()
     WHERE community_id = p_community AND user_uuid = p_user_uuid;

    -- recovery: a FLOOR soft ban (reason 1) clears once back at/above the soft floor; manual
    -- soft bans (reason 2) are never auto-cleared. The clear is ledgered.
    IF v_new >= v_soft THEN
        DELETE FROM bundle_reputation_soft_bans
         WHERE community_id = p_community AND user_uuid = p_user_uuid AND reason_code = 1;
        GET DIAGNOSTICS v_cleared = ROW_COUNT;
        IF v_cleared > 0 THEN
            INSERT INTO bundle_reputation_adjustments
                (app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code,
                 reason_code_id, kind, actor, source_event)
            VALUES (NULL, p_tenant, p_community, p_user_uuid::text, 'community', 0, '1', 1,
                    'soft_ban_clear', 'system:floor', 'soft_ban.auto_clear');
        END IF;
    END IF;

    -- tenant aggregate: FIXED product bounds 300..850 (never the community scale), start 600.
    INSERT INTO bundle_reputation_tenant_scores
        (tenant_id, user_uuid, score, event_count, last_event_at)
    VALUES
        (p_tenant, p_user_uuid, LEAST(GREATEST(600 + v_applied, 300), 850), 1, NOW())
    ON CONFLICT (tenant_id, user_uuid) DO UPDATE
       SET score = LEAST(GREATEST(bundle_reputation_tenant_scores.score + v_applied, 300), 850),
           event_count = bundle_reputation_tenant_scores.event_count + 1,
           last_event_at = NOW(),
           updated_at = NOW();

    RETURN QUERY SELECT v_new::int, true;
END
$fn$;

-- 5. Soft bans. Manual (reason 2) set/clear: human-UUID set_by, membership/pairing verified,
-- per-community daily quota (policy.manual_soft_ban_daily_quota, SQLSTATE 54000
-- 'quota_exceeded'); EXECUTE granted to hub_api ONLY. Floor (reason 1) bans never consume or
-- hit the quota and are reachable by the bundle role only via the floor wrappers below.
CREATE OR REPLACE FUNCTION reputation_set_soft_ban(
    p_tenant bigint, p_community bigint, p_user_uuid uuid, p_reason_code smallint,
    p_set_by text, p_expires_at timestamptz
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $ss$
DECLARE
    v_quota int := 25;
    v_used bigint;
    v_existing smallint;
BEGIN
    IF p_user_uuid IS NULL OR p_reason_code IS NULL OR p_set_by IS NULL
       OR p_reason_code NOT IN (1, 2) THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023', DETAIL = 'bad reason/set_by';
    END IF;
    IF p_reason_code = 2
       AND p_set_by !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023',
            DETAIL = 'manual soft ban needs a human admin UUID';
    END IF;
    IF p_reason_code = 1 AND p_set_by !~ '^system:[a-z_]+$' THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023',
            DETAIL = 'floor soft ban needs a system:<name> actor';
    END IF;
    IF p_expires_at IS NOT NULL AND p_expires_at <= NOW() THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023', DETAIL = 'expires_at in the past';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM communities WHERE id = p_community AND tenant_id = p_tenant) THEN
        RAISE EXCEPTION 'reputation_set_soft_ban: community is not in the named tenant'
            USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM community_members
     WHERE community_id = p_community AND user_uuid = p_user_uuid
       AND is_active AND left_at IS NULL AND removed_at IS NULL
       FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'not_a_member' USING ERRCODE = '42501';
    END IF;

    IF p_reason_code = 2 THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('rep_sb:' || p_community, 0));
        SELECT manual_soft_ban_daily_quota INTO v_quota
          FROM bundle_reputation_policy WHERE community_id = p_community;
        IF NOT FOUND THEN v_quota := 25; END IF;
        SELECT COUNT(*) INTO v_used FROM bundle_reputation_adjustments
         WHERE community_id = p_community AND kind IN ('soft_ban', 'soft_ban_clear')
           AND reason_code_id = 2 AND occurred_at > NOW() - INTERVAL '24 hours';
        IF v_used >= v_quota THEN
            RAISE EXCEPTION 'quota_exceeded' USING ERRCODE = '54000';
        END IF;
    END IF;

    SELECT reason_code INTO v_existing FROM bundle_reputation_soft_bans
     WHERE community_id = p_community AND user_uuid = p_user_uuid FOR UPDATE;
    IF FOUND AND v_existing = 2 AND p_reason_code = 1 THEN
        RETURN;  -- a manual ban is never downgraded to a floor ban
    END IF;

    INSERT INTO bundle_reputation_soft_bans
        (tenant_id, community_id, user_uuid, reason_code, set_by, expires_at)
    VALUES (p_tenant, p_community, p_user_uuid, p_reason_code, p_set_by, p_expires_at)
    ON CONFLICT (community_id, user_uuid) DO UPDATE
       SET reason_code = EXCLUDED.reason_code, set_by = EXCLUDED.set_by,
           set_at = NOW(), expires_at = EXCLUDED.expires_at;

    INSERT INTO bundle_reputation_adjustments
        (app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code,
         reason_code_id, kind, actor, source_event)
    VALUES (NULL, p_tenant, p_community, p_user_uuid::text, 'community', 0, p_reason_code::text,
            p_reason_code, 'soft_ban', p_set_by,
            CASE p_reason_code WHEN 1 THEN 'soft_ban.floor' ELSE 'soft_ban.manual' END);
END
$ss$;

CREATE OR REPLACE FUNCTION reputation_clear_soft_ban(
    p_tenant bigint, p_community bigint, p_user_uuid uuid, p_set_by text, p_only_floor bool
) RETURNS bool
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $sc$
DECLARE
    v_quota int := 25;
    v_used bigint;
    v_reason smallint;
BEGIN
    IF p_user_uuid IS NULL OR p_set_by IS NULL OR p_only_floor IS NULL THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023', DETAIL = 'missing argument';
    END IF;
    IF p_only_floor AND p_set_by !~ '^system:[a-z_]+$' THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023',
            DETAIL = 'floor clear needs a system:<name> actor';
    END IF;
    IF NOT p_only_floor
       AND p_set_by !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RAISE EXCEPTION 'invalid_input' USING ERRCODE = '22023',
            DETAIL = 'manual clear needs a human admin UUID';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM communities WHERE id = p_community AND tenant_id = p_tenant) THEN
        RAISE EXCEPTION 'reputation_clear_soft_ban: community is not in the named tenant'
            USING ERRCODE = '42501';
    END IF;
    IF NOT p_only_floor THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('rep_sb:' || p_community, 0));
    END IF;

    DELETE FROM bundle_reputation_soft_bans
     WHERE tenant_id = p_tenant AND community_id = p_community AND user_uuid = p_user_uuid
       AND (NOT p_only_floor OR reason_code = 1)
    RETURNING reason_code INTO v_reason;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF v_reason = 2 THEN  -- clearing a MANUAL ban counts against the manual quota
        SELECT manual_soft_ban_daily_quota INTO v_quota
          FROM bundle_reputation_policy WHERE community_id = p_community;
        IF NOT FOUND THEN v_quota := 25; END IF;
        SELECT COUNT(*) INTO v_used FROM bundle_reputation_adjustments
         WHERE community_id = p_community AND kind IN ('soft_ban', 'soft_ban_clear')
           AND reason_code_id = 2 AND occurred_at > NOW() - INTERVAL '24 hours';
        IF v_used >= v_quota THEN
            RAISE EXCEPTION 'quota_exceeded' USING ERRCODE = '54000';  -- rolls the DELETE back
        END IF;
    END IF;

    INSERT INTO bundle_reputation_adjustments
        (app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code,
         reason_code_id, kind, actor, source_event)
    VALUES (NULL, p_tenant, p_community, p_user_uuid::text, 'community', 0, v_reason::text,
            v_reason, 'soft_ban_clear', p_set_by,
            CASE v_reason WHEN 1 THEN 'soft_ban.floor_clear' ELSE 'soft_ban.manual_clear' END);
    RETURN true;
END
$sc$;

-- Bundle-role wrappers: FLOOR (reason 1) only. The set wrapper refuses unless the policy has
-- soft_ban_enabled AND the member's score (COALESCE 600) really is below soft_ban_floor, so the
-- bundle role cannot soft-ban an arbitrary member.
CREATE OR REPLACE FUNCTION reputation_set_floor_soft_ban(
    p_tenant bigint, p_community bigint, p_user_uuid uuid
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $sf$
DECLARE
    v_enabled boolean;
    v_floor int;
    v_default int;
    v_balance int;
BEGIN
    SELECT soft_ban_enabled, soft_ban_floor, score_default INTO v_enabled, v_floor, v_default
      FROM bundle_reputation_policy WHERE community_id = p_community AND tenant_id = p_tenant;
    IF NOT FOUND OR NOT v_enabled THEN
        RAISE EXCEPTION 'floor_not_breached' USING ERRCODE = '22023', DETAIL = 'auto-ban disabled';
    END IF;
    SELECT balance INTO v_balance FROM bundle_reputation_scores
     WHERE community_id = p_community AND user_uuid = p_user_uuid AND tenant_id = p_tenant;
    IF COALESCE(v_balance, v_default) >= v_floor THEN
        RAISE EXCEPTION 'floor_not_breached' USING ERRCODE = '22023',
            DETAIL = 'score at/above floor';
    END IF;
    PERFORM reputation_set_soft_ban(p_tenant, p_community, p_user_uuid, 1::smallint,
                                    'system:floor', NULL);
END
$sf$;

CREATE OR REPLACE FUNCTION reputation_clear_floor_soft_ban(
    p_tenant bigint, p_community bigint, p_user_uuid uuid
) RETURNS bool
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp
AS $cf$
    SELECT reputation_clear_soft_ban(p_tenant, p_community, p_user_uuid, 'system:floor', true);
$cf$;

-- Read path (bundles + host): expiry-aware, tenant-pairing verified.
CREATE OR REPLACE FUNCTION reputation_is_soft_banned(
    p_tenant bigint, p_community bigint, p_user_uuid uuid
) RETURNS bool
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $sb$
    SELECT EXISTS (
        SELECT 1 FROM bundle_reputation_soft_bans
         WHERE tenant_id = p_tenant AND community_id = p_community AND user_uuid = p_user_uuid
           AND (expires_at IS NULL OR expires_at > NOW())
    );
$sb$;

-- 6. Exclusivity: the tenant's ACTIVE `provides: reputation-logic` app (the one the hub-api 409
-- exclusivity allows): tenant-available + current global install whose stored manifest provides
-- reputation-logic. Exactly one -> its app_id; none OR ambiguous (>1) -> NULL (fail closed).
-- The host allows activity-class `reputation.adjust` only from this app (or core internals).
CREATE OR REPLACE FUNCTION reputation_active_logic_app(p_tenant bigint) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $al$
DECLARE
    v_apps text[];
BEGIN
    SELECT array_agg(DISTINCT a.app_id) INTO v_apps
      FROM bundle_tenant_availability a
      JOIN app_global_installs g ON g.app_id = a.app_id
           AND g.superseded_by IS NULL AND g.revoked_at IS NULL
      JOIN app_version_uploads u ON u.app_id = g.app_id AND u.version = g.version
     WHERE a.tenant_id = p_tenant AND a.available
       AND u.manifest_json -> 'provides' @> '"reputation-logic"'::jsonb;
    IF v_apps IS NULL OR array_length(v_apps, 1) <> 1 THEN
        RETURN NULL;
    END IF;
    RETURN v_apps[1];
END
$al$;

REVOKE ALL ON FUNCTION
    reputation_apply(bigint, bigint, uuid, int, smallint, smallint, text, text, text, text),
    reputation_set_soft_ban(bigint, bigint, uuid, smallint, text, timestamptz),
    reputation_clear_soft_ban(bigint, bigint, uuid, text, bool),
    reputation_set_floor_soft_ban(bigint, bigint, uuid),
    reputation_clear_floor_soft_ban(bigint, bigint, uuid),
    reputation_is_soft_banned(bigint, bigint, uuid),
    reputation_active_logic_app(bigint)
    FROM PUBLIC;

-- 7. Grants (roles are provisioned elsewhere; skipped when absent so ordering never matters).
-- Bundle role: apply (class 1/2, system actors only -- enforced inside), floor wrappers, reads.
-- hub_api: apply, MANUAL set/clear, reads. Manual set/clear are NEVER granted to the bundle role.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reputation') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_apply(bigint, bigint, uuid, int, smallint, smallint, text, text, text, text),
            reputation_set_floor_soft_ban(bigint, bigint, uuid),
            reputation_clear_floor_soft_ban(bigint, bigint, uuid),
            reputation_is_soft_banned(bigint, bigint, uuid),
            reputation_active_logic_app(bigint)
            TO waddles_bundle_reputation;
        REVOKE EXECUTE ON FUNCTION
            reputation_set_soft_ban(bigint, bigint, uuid, smallint, text, timestamptz),
            reputation_clear_soft_ban(bigint, bigint, uuid, text, bool)
            FROM waddles_bundle_reputation;
        GRANT SELECT ON bundle_reputation_policy, bundle_reputation_tenant_policy,
            bundle_reputation_tenant_scores, bundle_reputation_soft_bans
            TO waddles_bundle_reputation;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_apply(bigint, bigint, uuid, int, smallint, smallint, text, text, text, text),
            reputation_set_soft_ban(bigint, bigint, uuid, smallint, text, timestamptz),
            reputation_clear_soft_ban(bigint, bigint, uuid, text, bool),
            reputation_is_soft_banned(bigint, bigint, uuid),
            reputation_active_logic_app(bigint)
            TO hub_api;
        GRANT SELECT, INSERT, UPDATE ON bundle_reputation_policy,
            bundle_reputation_tenant_policy TO hub_api;
        GRANT SELECT, DELETE ON bundle_reputation_soft_bans TO hub_api;
        GRANT SELECT, DELETE ON bundle_reputation_scores, bundle_reputation_tenant_scores,
            bundle_reputation_adjustments TO hub_api;
    END IF;
END
$$;
