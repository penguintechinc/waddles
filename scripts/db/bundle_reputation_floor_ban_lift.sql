-- Floor-ban LIFT -- SQL half. Applied by alembic 0139_floor_ban_lift. Idempotent.
--
-- A reversed automated ban must return the user to normal access, so the outbox gets an explicit
-- terminal LIFTED state (status 8) instead of an enforced row that denies forever:
--   status: 1 queued, 2 done, 3 failed, 4 dead, 5 rate limited, 6 refused, 7 dispatched,
--           8 lifted (an admin reversed the ban; never enforced or retried again).
-- `lifted_at` / `lifted_by` (the acting admin's hub_users.uuid -- a UUID, never PII) are the audit
-- record. hub-api holds no UPDATE on the table, so the lift is a SECURITY DEFINER function.

ALTER TABLE bundle_reputation_floor_bans
    ADD COLUMN IF NOT EXISTS lifted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lifted_by UUID,
    ADD COLUMN IF NOT EXISTS recovered_at TIMESTAMPTZ;

-- A lift is STICKY for its ban occurrence: `recovered_at` is set once the user's score is back
-- at/above the floor (at lift time if already so, else by the score triggers below). Until then no
-- new event key may re-ban the user (see reputation_floor_ban_enqueue); after it, a fresh
-- floor-crossing bans normally.
CREATE INDEX IF NOT EXISTS idx_bundle_reputation_floor_bans_lifted_unrecovered
    ON bundle_reputation_floor_bans (tenant_id, user_uuid) WHERE status = 8 AND recovered_at IS NULL;

ALTER TABLE bundle_reputation_floor_bans
    DROP CONSTRAINT IF EXISTS bundle_reputation_floor_bans_status_check;
ALTER TABLE bundle_reputation_floor_bans
    DROP CONSTRAINT IF EXISTS ck_bundle_reputation_floor_bans_status;
ALTER TABLE bundle_reputation_floor_bans
    ADD CONSTRAINT ck_bundle_reputation_floor_bans_status
    CHECK (status IN (1, 2, 3, 4, 5, 6, 7, 8));

-- A row that leaves LIFTED (a later enqueue re-bans the same slot) forgets the old lift, so a live
-- ban never carries lift metadata.
CREATE OR REPLACE FUNCTION reputation_floor_ban_clear_lift() RETURNS trigger
LANGUAGE plpgsql
AS $cl$
BEGIN
    IF OLD.status = 8 AND NEW.status <> 8 THEN
        NEW.lifted_at := NULL;
        NEW.lifted_by := NULL;
        NEW.recovered_at := NULL;
    END IF;
    RETURN NEW;
END
$cl$;
DROP TRIGGER IF EXISTS trg_bundle_reputation_floor_bans_clear_lift ON bundle_reputation_floor_bans;
CREATE TRIGGER trg_bundle_reputation_floor_bans_clear_lift
    BEFORE UPDATE ON bundle_reputation_floor_bans
    FOR EACH ROW EXECUTE FUNCTION reputation_floor_ban_clear_lift();

-- Lift every enforced / pending floor ban of the subject's identity tokens in ONE tenant (floor
-- bans are tenant-wide, so a partial lift would still deny). Only statuses 1,2,3,4,5,7 move to 8;
-- refused (6) and already-lifted rows are untouched, so the call is idempotent. An in-flight (7)
-- row is lifted too: its lease can no longer complete it, and it is never re-dispatched.
-- Returns the number of rows lifted (0 = nothing was in force).
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
    UPDATE bundle_reputation_floor_bans b
       SET status = 8, lifted_at = NOW(), lifted_by = p_lifted_by,
           recovered_at = CASE WHEN reputation_verify_floor_breach(
                                  b.tenant_id, b.community_id, b.user_uuid, b.scope)
                               THEN NULL ELSE NOW() END,
           lease_token = NULL, lease_expires_at = NULL, next_retry_at = NULL,
           completed_at = COALESCE(b.completed_at, NOW()), updated_at = NOW()
     WHERE b.tenant_id = p_tenant AND b.user_uuid = ANY(p_user_uuids)
       AND b.status IN (1, 2, 3, 4, 5, 7);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END
$fl$;

-- Recovery marker: when a score write leaves the user at/above the floor, every lifted-but-not-yet
-- recovered row of that subject is marked recovered (a later drop below the floor is then a NEW
-- crossing). Independent of the write path (apply, admin override, ...). The EXISTS probe keeps the
-- common case (no lifted row) to one indexed lookup.
CREATE OR REPLACE FUNCTION reputation_floor_ban_mark_recovered() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $mr$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM bundle_reputation_floor_bans l
                    WHERE l.tenant_id = NEW.tenant_id AND l.user_uuid = NEW.user_uuid
                      AND l.status = 8 AND l.recovered_at IS NULL) THEN
        RETURN NULL;
    END IF;
    IF TG_TABLE_NAME = 'bundle_reputation_scores' THEN
        UPDATE bundle_reputation_floor_bans b SET recovered_at = NOW()
         WHERE b.tenant_id = NEW.tenant_id AND b.user_uuid = NEW.user_uuid
           AND b.community_id = NEW.community_id AND b.scope = 1
           AND b.status = 8 AND b.recovered_at IS NULL
           AND NOT reputation_verify_floor_breach(b.tenant_id, b.community_id, b.user_uuid,
                                                  1::smallint);
    ELSE
        UPDATE bundle_reputation_floor_bans b SET recovered_at = NOW()
         WHERE b.tenant_id = NEW.tenant_id AND b.user_uuid = NEW.user_uuid
           AND b.scope = 2 AND b.status = 8 AND b.recovered_at IS NULL
           AND NOT reputation_verify_floor_breach(b.tenant_id, b.community_id, b.user_uuid,
                                                  2::smallint);
    END IF;
    RETURN NULL;
END
$mr$;
DROP TRIGGER IF EXISTS trg_bundle_reputation_scores_floor_recovered ON bundle_reputation_scores;
CREATE TRIGGER trg_bundle_reputation_scores_floor_recovered
    AFTER INSERT OR UPDATE OF balance ON bundle_reputation_scores
    FOR EACH ROW EXECUTE FUNCTION reputation_floor_ban_mark_recovered();
DROP TRIGGER IF EXISTS trg_bundle_reputation_tenant_scores_floor_recovered
    ON bundle_reputation_tenant_scores;
CREATE TRIGGER trg_bundle_reputation_tenant_scores_floor_recovered
    AFTER INSERT OR UPDATE OF score ON bundle_reputation_tenant_scores
    FOR EACH ROW EXECUTE FUNCTION reputation_floor_ban_mark_recovered();

-- Enqueue: same as 0114 except (a) status 8 is terminal for the same key, and (b) a NEW key is
-- refused ('lift_sticky') while a lifted row of the same subject/scope has not yet recovered.
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
    IF EXISTS (SELECT 1 FROM bundle_reputation_floor_bans l
                WHERE l.tenant_id = p_tenant AND l.user_uuid = p_user_uuid
                  AND l.scope = p_scope AND l.status = 8 AND l.recovered_at IS NULL
                  AND (p_scope = 2 OR l.community_id = p_community)) THEN
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

-- Erasure: a lifted row is the audit trail of a (reversed) sanction, so it is pseudonymised (never
-- deleted, never left naming the person) -- with a RANDOM pseudonym, since a reversed ban no longer
-- needs a suppression code and must not block the erasure. Done rows are unchanged. Replaces the
-- 0114 definition.
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
       AND status = 2;
    IF v_done > 0 THEN
        IF p_suppression_code IS NULL OR octet_length(p_suppression_code) < 16 THEN
            RAISE EXCEPTION 'suppression_code_required' USING ERRCODE = '22023';
        END IF;
        UPDATE bundle_reputation_floor_bans b
           SET user_uuid = substr(
                   encode(sha256(p_suppression_code || int8send(b.id)), 'hex'), 1, 32)::uuid,
               updated_at = NOW()
         WHERE b.user_uuid = p_user_uuid AND b.tenant_id = p_tenant AND b.platform = p_platform
           AND b.status = 2;
        GET DIAGNOSTICS v_pseudonymised = ROW_COUNT;
    END IF;
    UPDATE bundle_reputation_floor_bans b
       SET user_uuid = gen_random_uuid(), updated_at = NOW()
     WHERE b.user_uuid = p_user_uuid AND b.tenant_id = p_tenant AND b.platform = p_platform
       AND b.status = 8;
    GET DIAGNOSTICS v_lifted = ROW_COUNT;
    v_pseudonymised := v_pseudonymised + v_lifted;
    SELECT count(*) INTO v_pending FROM bundle_reputation_floor_bans
     WHERE user_uuid = p_user_uuid AND tenant_id = p_tenant AND platform = p_platform
       AND status IN (1, 3, 4, 5, 7);
    RETURN QUERY SELECT v_deleted, v_pseudonymised, v_pending;
END
$fe$;

REVOKE ALL ON FUNCTION reputation_floor_ban_enqueue(bigint, bigint, bigint, uuid, smallint, text, text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION reputation_floor_ban_mark_recovered() FROM PUBLIC;
REVOKE ALL ON FUNCTION reputation_floor_ban_lift(bigint, uuid[], uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION reputation_floor_ban_erase_subject(uuid, bigint, text, bytea) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reputation') THEN
        GRANT EXECUTE ON FUNCTION
            reputation_floor_ban_enqueue(bigint, bigint, bigint, uuid, smallint, text, text)
            TO waddles_bundle_reputation;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api') THEN
        GRANT EXECUTE ON FUNCTION reputation_floor_ban_lift(bigint, uuid[], uuid) TO hub_api;
        GRANT EXECUTE ON FUNCTION reputation_floor_ban_erase_subject(uuid, bigint, text, bytea)
            TO hub_api;
    END IF;
END
$$;
