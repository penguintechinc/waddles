-- Reputation unification DDL (single store = bundle store). Read by BOTH
-- `alembic/versions/0096_reputation_unify.py` (production) and the Rust crate's
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

-- Read path for bundles (and the Rust host op): expiry-aware, tenant-pairing verified.
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
REVOKE ALL ON FUNCTION reputation_is_soft_banned(bigint, bigint, uuid) FROM PUBLIC;

-- Write path (SECURITY DEFINER, tenant/community pairing verified): used by hub-api's admin
-- routes (reason 2) and the accrual bundle's floor enforcement (reason 1).
CREATE OR REPLACE FUNCTION reputation_set_soft_ban(
    p_tenant bigint, p_community bigint, p_user_uuid uuid, p_reason smallint,
    p_set_by text, p_expires_at timestamptz
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $ss$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM communities WHERE id = p_community AND tenant_id = p_tenant) THEN
        RAISE EXCEPTION 'reputation_set_soft_ban: community is not in the named tenant'
            USING ERRCODE = '42501';
    END IF;
    INSERT INTO bundle_reputation_soft_bans
        (tenant_id, community_id, user_uuid, reason_code, set_by, expires_at)
    VALUES (p_tenant, p_community, p_user_uuid, p_reason, p_set_by, p_expires_at)
    ON CONFLICT (community_id, user_uuid) DO UPDATE
       SET reason_code = EXCLUDED.reason_code, set_by = EXCLUDED.set_by,
           set_at = NOW(), expires_at = EXCLUDED.expires_at;
END
$ss$;

CREATE OR REPLACE FUNCTION reputation_clear_soft_ban(
    p_tenant bigint, p_community bigint, p_user_uuid uuid
) RETURNS bool
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $sc$
DECLARE
    v_n int;
BEGIN
    DELETE FROM bundle_reputation_soft_bans
     WHERE tenant_id = p_tenant AND community_id = p_community AND user_uuid = p_user_uuid;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n > 0;
END
$sc$;

REVOKE ALL ON FUNCTION reputation_set_soft_ban(bigint, bigint, uuid, smallint, text, timestamptz)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION reputation_clear_soft_ban(bigint, bigint, uuid) FROM PUBLIC;

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

-- 3. Ledger: who/why/where. `app_id` becomes nullable (hub-api and admin writes have no
-- app); `reason_code_id` is the numeric reason enum (the legacy varchar `reason_code` stays
-- populated with its decimal text for old readers).
ALTER TABLE bundle_reputation_adjustments ALTER COLUMN app_id DROP NOT NULL;
ALTER TABLE bundle_reputation_adjustments ADD COLUMN IF NOT EXISTS actor TEXT;
ALTER TABLE bundle_reputation_adjustments ADD COLUMN IF NOT EXISTS source_event TEXT;
ALTER TABLE bundle_reputation_adjustments ADD COLUMN IF NOT EXISTS reason_code_id SMALLINT;

-- app-less keyed rows are idempotent per (scope, key, target) -- the existing 0072 index
-- cannot dedup them (NULL app_id never collides).
CREATE UNIQUE INDEX IF NOT EXISTS uq_bundle_reputation_adjustments_idem_noapp
    ON bundle_reputation_adjustments (tenant_id, community_id, idempotency_key, target_user_uuid)
    WHERE idempotency_key IS NOT NULL AND app_id IS NULL;

-- 4. The one write path. Row-locks the (community, user) score, clamps to the community
-- policy scale, writes the ledger row and moves the tenant aggregate -- one transaction.
-- Idempotent on p_idempotency_key (replay returns the original balance, applied = false).
-- SECURITY DEFINER so callers need only EXECUTE; the tenant/community pairing is verified
-- inside, so a caller can never touch a community outside the tenant it names.
CREATE OR REPLACE FUNCTION reputation_apply(
    p_tenant bigint,
    p_community bigint,
    p_user_uuid uuid,
    p_delta int,
    p_reason_code smallint,
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
    v_balance bigint;
    v_new bigint;
    v_applied int;
    v_prior bigint;
BEGIN
    IF p_user_uuid IS NULL OR p_delta IS NULL OR p_reason_code IS NULL THEN
        RAISE EXCEPTION 'reputation_apply: user, delta and reason are required'
            USING ERRCODE = '22004';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM communities WHERE id = p_community AND tenant_id = p_tenant) THEN
        RAISE EXCEPTION 'reputation_apply: community is not in the named tenant'
            USING ERRCODE = '42501';
    END IF;

    SELECT score_min, score_max, score_default, soft_ban_floor
      INTO v_min, v_max, v_default, v_soft
      FROM bundle_reputation_policy WHERE community_id = p_community;
    IF NOT FOUND THEN
        v_min := 300; v_max := 850; v_default := 600; v_soft := 550;
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

    IF p_idempotency_key IS NOT NULL THEN
        SELECT balance_after INTO v_prior FROM bundle_reputation_adjustments
         WHERE tenant_id = p_tenant AND community_id = p_community
           AND target_user_uuid = p_user_uuid::text
           AND idempotency_key = p_idempotency_key AND app_id IS NULL;
        IF FOUND THEN
            RETURN QUERY SELECT COALESCE(v_prior, v_balance)::int, false;
            RETURN;
        END IF;
    END IF;

    v_new := LEAST(GREATEST(v_balance + p_delta, v_min), v_max);
    v_applied := (v_new - v_balance)::int;

    INSERT INTO bundle_reputation_adjustments
        (app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code,
         reason_code_id, kind, requested_value, balance_after, idempotency_key, actor,
         source_event)
    VALUES
        (NULL, p_tenant, p_community, p_user_uuid::text, 'community', v_applied,
         p_reason_code::text, p_reason_code,
         CASE WHEN left(p_actor, 6) = 'admin:' THEN 'admin_override' ELSE 'adjust' END,
         p_delta, v_new, p_idempotency_key, p_actor, p_source_event);

    UPDATE bundle_reputation_scores
       SET balance = v_new, adjustment_count = adjustment_count + 1, updated_at = NOW()
     WHERE community_id = p_community AND user_uuid = p_user_uuid;

    -- recovery: a FLOOR-triggered soft ban (reason 1) clears once the score is back at/above
    -- the soft floor; manual soft bans (reason 2) are never auto-cleared.
    IF v_new >= v_soft THEN
        DELETE FROM bundle_reputation_soft_bans
         WHERE community_id = p_community AND user_uuid = p_user_uuid AND reason_code = 1;
    END IF;

    INSERT INTO bundle_reputation_tenant_scores
        (tenant_id, user_uuid, score, event_count, last_event_at)
    VALUES
        (p_tenant, p_user_uuid, LEAST(GREATEST(v_default + v_applied, v_min), v_max), 1, NOW())
    ON CONFLICT (tenant_id, user_uuid) DO UPDATE
       SET score = LEAST(GREATEST(bundle_reputation_tenant_scores.score + v_applied, v_min), v_max),
           event_count = bundle_reputation_tenant_scores.event_count + 1,
           last_event_at = NOW(),
           updated_at = NOW();

    RETURN QUERY SELECT v_new::int, true;
END
$fn$;

REVOKE ALL ON FUNCTION reputation_apply(bigint, bigint, uuid, int, smallint, text, text, text)
    FROM PUBLIC;

-- 5. Grants (roles are provisioned elsewhere; skipped when absent so ordering never matters).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reputation') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_apply(bigint, bigint, uuid, int, smallint, text, text, text)
            TO waddles_bundle_reputation;
        GRANT SELECT ON bundle_reputation_policy, bundle_reputation_tenant_policy,
            bundle_reputation_tenant_scores TO waddles_bundle_reputation;
        GRANT SELECT ON bundle_reputation_soft_bans TO waddles_bundle_reputation;
        GRANT EXECUTE ON FUNCTION reputation_is_soft_banned(bigint, bigint, uuid),
            reputation_set_soft_ban(bigint, bigint, uuid, smallint, text, timestamptz),
            reputation_clear_soft_ban(bigint, bigint, uuid) TO waddles_bundle_reputation;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_apply(bigint, bigint, uuid, int, smallint, text, text, text)
            TO hub_api;
        GRANT SELECT, INSERT, UPDATE ON bundle_reputation_policy,
            bundle_reputation_tenant_policy TO hub_api;
        GRANT SELECT, DELETE ON bundle_reputation_soft_bans TO hub_api;
        GRANT EXECUTE ON FUNCTION reputation_is_soft_banned(bigint, bigint, uuid),
            reputation_set_soft_ban(bigint, bigint, uuid, smallint, text, timestamptz),
            reputation_clear_soft_ban(bigint, bigint, uuid) TO hub_api;
        GRANT SELECT ON bundle_reputation_scores, bundle_reputation_tenant_scores,
            bundle_reputation_adjustments TO hub_api;
        GRANT DELETE ON bundle_reputation_scores, bundle_reputation_tenant_scores,
            bundle_reputation_adjustments TO hub_api;
    END IF;
END
$$;
