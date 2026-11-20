-- Floor-ban LIFT -- SQL half. Applied by alembic 0137_floor_ban_lift. Idempotent.
--
-- A reversed automated ban must return the user to normal access, so the outbox gets an explicit
-- terminal LIFTED state (status 8) instead of an enforced row that denies forever:
--   status: 1 queued, 2 done, 3 failed, 4 dead, 5 rate limited, 6 refused, 7 dispatched,
--           8 lifted (an admin reversed the ban; never enforced or retried again).
-- `lifted_at` / `lifted_by` (the acting admin's hub_users.uuid -- a UUID, never PII) are the audit
-- record. hub-api holds no UPDATE on the table, so the lift is a SECURITY DEFINER function.

ALTER TABLE bundle_reputation_floor_bans
    ADD COLUMN IF NOT EXISTS lifted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lifted_by UUID;

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
           lease_token = NULL, lease_expires_at = NULL, next_retry_at = NULL,
           completed_at = COALESCE(b.completed_at, NOW()), updated_at = NOW()
     WHERE b.tenant_id = p_tenant AND b.user_uuid = ANY(p_user_uuids)
       AND b.status IN (1, 2, 3, 4, 5, 7);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END
$fl$;

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

REVOKE ALL ON FUNCTION reputation_floor_ban_lift(bigint, uuid[], uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION reputation_floor_ban_erase_subject(uuid, bigint, text, bytea) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hub_api') THEN
        GRANT EXECUTE ON FUNCTION reputation_floor_ban_lift(bigint, uuid[], uuid) TO hub_api;
        GRANT EXECUTE ON FUNCTION reputation_floor_ban_erase_subject(uuid, bigint, text, bytea)
            TO hub_api;
    END IF;
END
$$;
