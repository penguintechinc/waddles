"""Identity resolution security hardening (post-merge adversarial review of #429).

Closes the findings the review raised against 0045_identity_resolution:

1. **Forgeable / stale user_uuid (HIGH).** 0045's membership trigger returned early
   when ``NEW.user_uuid`` was already set, so an INSERT could plant an arbitrary
   (forged) UUID and an UPDATE that relinked ``user_id`` (NULL -> 1) or reassigned a
   row (A -> B) kept the old pseudonym / the previous person's UUID. The trigger now
   ALWAYS derives ``user_uuid`` itself -- any caller-supplied value is discarded and
   recorded as an audit event -- and re-derives on every change to ``user_id`` /
   ``platform`` / ``platform_user_id`` / ``user_uuid``. The upgrade also re-derives
   every existing row (stale or forged values from the 0045 window are replaced).
2. **Cross-tenant correlation (HIGH).** ``resolve_identity_uuid()`` looked up
   ``hub_user_identities`` with no tenant filter, so any caller could resolve a platform
   account to the global hub UUID of its owner from ANY tenant and correlate the person
   across tenants. A hub link now only resolves inside a tenant the hub user is a member
   of (or when the membership trigger itself vouches for the row, ``p_member_asserted``);
   everywhere else the account gets that tenant's own pseudonym. The tenant-from-token-claim
   half of this finding is enforced in hub-api's gRPC layer (no SQL change).
3. **Silent fall-through / handle poisoning (MED).** An explicit ``user_id`` that did not
   resolve silently fell through to a pseudonym -- the function now RAISES, and the trigger
   records ``dangling_user_id`` (fail-closed NULL) instead of minting. The pseudonym's
   ``handle`` was first-writer-wins (``ON CONFLICT DO NOTHING``): the most recent
   non-empty platform-asserted handle now wins, and the membership trigger no longer copies
   ``community_members.display_name`` into ``ephemeral_pseudonyms`` at all.
4. **Silent NULL (MED).** Rows left without a ``user_uuid`` now carry a
   ``user_uuid_unavailable_reason`` (``uuid_collision`` -- a second platform account of one
   hub user in a community --, ``dangling_user_id``, ``unresolvable``, ``no_tenant``), each
   transition is written to ``identity_resolution_events`` (ids/counts only, no PII), and
   ``community_member_identities`` gains ``user_uuid_status`` ('resolved'/'unavailable') +
   the reason, so a consumer can tell "unavailable" from "not a member". hub-api exports the
   count as a gauge. GDPR erasure for ``ephemeral_pseudonyms.handle`` is
   ``erase_ephemeral_pseudonym_handles()`` (wipe the handle, or delete the mapping).

Revision ID: 0048_identity_forged_uuid
Revises: 0047_builtin_handler_paths
Create Date: 2026-10-09
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "0048_identity_forged_uuid"
down_revision = "0047_builtin_handler_paths"
branch_labels = None
depends_on = None

_READER = "waddles_bundle_reader"

#: The CHECK constraint's reason list mirrors
#: ``identity_resolution_service.UNAVAILABLE_REASONS``.
_SCHEMA_SQL = """
ALTER TABLE community_members ADD COLUMN IF NOT EXISTS user_uuid_unavailable_reason TEXT;
DO $c$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_community_members_uuid_reason'
                      AND conrelid = 'community_members'::regclass) THEN
        ALTER TABLE community_members ADD CONSTRAINT ck_community_members_uuid_reason CHECK (
            (user_uuid IS NULL OR user_uuid_unavailable_reason IS NULL)
            AND (user_uuid_unavailable_reason IS NULL
                 OR user_uuid_unavailable_reason IN
                    ('uuid_collision', 'dangling_user_id', 'unresolvable', 'no_tenant'))
        );
    END IF;
END $c$;

CREATE TABLE IF NOT EXISTS identity_resolution_events (
    id           BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    event        VARCHAR(40) NOT NULL,
    tenant_id    INTEGER,
    community_id INTEGER,
    member_id    INTEGER,
    affected     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_identity_resolution_events_event_ts
    ON identity_resolution_events (event, occurred_at DESC);
REVOKE ALL ON identity_resolution_events FROM PUBLIC;
COMMENT ON TABLE identity_resolution_events IS
    'Identity audit trail (hub-api only): ids and counts, never a handle, platform id or name.';
"""

_RESOLVE_FN_SQL = """
DROP FUNCTION IF EXISTS resolve_identity_uuid(integer, text, text, text, text);
CREATE OR REPLACE FUNCTION resolve_identity_uuid(
    p_tenant_id integer, p_platform text, p_platform_user_id text,
    p_user_id text, p_handle text, p_member_asserted boolean DEFAULT false
) RETURNS uuid LANGUAGE plpgsql AS $fn$
DECLARE
    v uuid;
    v_old_handle text;
    v_handle text := NULLIF(btrim(p_handle), '');
BEGIN
    IF p_tenant_id IS NULL THEN
        RAISE EXCEPTION 'resolve_identity_uuid: tenant_id is required' USING ERRCODE = '22023';
    END IF;
    IF p_user_id IS NOT NULL THEN
        SELECT hu.uuid INTO v FROM hub_users hu WHERE hu.id::text = p_user_id;
        IF v IS NULL THEN
            RAISE EXCEPTION
                'resolve_identity_uuid: explicit user_id does not resolve to a hub user'
                USING ERRCODE = 'P0002';
        END IF;
        IF NOT p_member_asserted AND NOT EXISTS (
            SELECT 1 FROM community_members cm JOIN communities c ON c.id = cm.community_id
             WHERE c.tenant_id = p_tenant_id AND cm.user_id = p_user_id) THEN
            RAISE EXCEPTION 'resolve_identity_uuid: hub user is not a member of the tenant'
                USING ERRCODE = '42501';
        END IF;
        RETURN v;
    END IF;
    IF coalesce(p_platform, '') = '' OR coalesce(p_platform_user_id, '') = '' THEN
        RAISE EXCEPTION
            'resolve_identity_uuid: platform and platform_user_id are required'
            USING ERRCODE = '22023';
    END IF;
    -- A hub link only resolves INSIDE a tenant the hub user belongs to (or when the
    -- membership trigger vouches for the row): the global hub UUID is never handed to
    -- a tenant the person is not part of, so it cannot be used to correlate them.
    SELECT hu.uuid INTO v
      FROM hub_user_identities hui JOIN hub_users hu ON hu.id = hui.hub_user_id
     WHERE hui.platform = p_platform AND hui.platform_user_id = p_platform_user_id
       AND (p_member_asserted OR EXISTS (
            SELECT 1 FROM community_members cm JOIN communities c ON c.id = cm.community_id
             WHERE c.tenant_id = p_tenant_id
               AND (cm.user_id = hu.id::text
                    OR (cm.platform = hui.platform
                        AND cm.platform_user_id = hui.platform_user_id))))
     ORDER BY hu.id LIMIT 1;
    IF v IS NOT NULL THEN
        RETURN v;
    END IF;
    -- Read-only fast path for an existing pseudonym; the handle is platform-asserted, so
    -- the most recent non-empty assertion wins (never first-writer-wins).
    SELECT ep.pseudonym, ep.handle INTO v, v_old_handle FROM ephemeral_pseudonyms ep
     WHERE ep.tenant_id = p_tenant_id AND ep.platform = p_platform
       AND ep.platform_user_id = p_platform_user_id;
    IF v IS NOT NULL THEN
        IF v_handle IS NOT NULL AND v_handle IS DISTINCT FROM v_old_handle THEN
            UPDATE ephemeral_pseudonyms SET handle = v_handle WHERE pseudonym = v;
        END IF;
        RETURN v;
    END IF;
    INSERT INTO ephemeral_pseudonyms (tenant_id, platform, platform_user_id, handle)
    VALUES (p_tenant_id, p_platform, p_platform_user_id, v_handle)
    ON CONFLICT (tenant_id, platform, platform_user_id)
    DO UPDATE SET handle = COALESCE(EXCLUDED.handle, ephemeral_pseudonyms.handle)
    RETURNING pseudonym INTO v;
    IF v IS NULL THEN
        RAISE EXCEPTION 'resolve_identity_uuid: pseudonym mint failed' USING ERRCODE = 'XX000';
    END IF;
    RETURN v;
END $fn$;
REVOKE ALL ON FUNCTION resolve_identity_uuid(integer, text, text, text, text, boolean)
    FROM PUBLIC;
"""

_TRIGGER_FN_SQL = """
CREATE OR REPLACE FUNCTION community_members_set_user_uuid() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    v_tenant integer;
    v uuid;
    v_reason text;
BEGIN
    -- user_uuid is DERIVED, never accepted: a caller-supplied value (INSERT, or an UPDATE
    -- that changes it) is discarded and audited, then re-derived like any other row.
    IF NEW.user_uuid IS NOT NULL
       AND (TG_OP = 'INSERT' OR NEW.user_uuid IS DISTINCT FROM OLD.user_uuid) THEN
        INSERT INTO identity_resolution_events (event, community_id, member_id)
        VALUES ('caller_user_uuid_ignored', NEW.community_id, NEW.id);
        RAISE WARNING '0047 caller-supplied user_uuid ignored (community_id=%)',
            NEW.community_id;
    END IF;
    NEW.user_uuid := NULL;
    NEW.user_uuid_unavailable_reason := NULL;

    IF NEW.user_id IS NOT NULL THEN
        SELECT hu.uuid INTO v FROM hub_users hu WHERE hu.id::text = NEW.user_id;
        IF v IS NULL THEN
            v_reason := 'dangling_user_id';
        END IF;
    ELSIF coalesce(NEW.platform, '') = '' OR coalesce(NEW.platform_user_id, '') = '' THEN
        v_reason := 'unresolvable';
    END IF;
    IF v_reason IS NULL AND v IS NULL THEN
        SELECT c.tenant_id INTO v_tenant FROM communities c WHERE c.id = NEW.community_id;
        IF v_tenant IS NULL THEN
            v_reason := 'no_tenant';
        ELSE
            -- The row itself is the membership evidence (p_member_asserted); no handle is
            -- passed, so community display names never reach ephemeral_pseudonyms.
            v := resolve_identity_uuid(
                v_tenant, NEW.platform, NEW.platform_user_id, NULL, NULL, true);
        END IF;
    END IF;
    IF v_reason IS NULL AND EXISTS (
            SELECT 1 FROM community_members m
             WHERE m.community_id = NEW.community_id AND m.user_uuid = v
               AND m.id IS DISTINCT FROM NEW.id) THEN
        v_reason := 'uuid_collision';
    END IF;
    IF v_reason IS NOT NULL THEN
        NEW.user_uuid_unavailable_reason := v_reason;
        IF TG_OP = 'INSERT' OR OLD.user_uuid_unavailable_reason IS DISTINCT FROM v_reason THEN
            INSERT INTO identity_resolution_events (event, tenant_id, community_id, member_id)
            VALUES (v_reason, v_tenant, NEW.community_id, NEW.id);
            RAISE WARNING '0047 user_uuid unavailable: % (community_id=%)',
                v_reason, NEW.community_id;
        END IF;
        RETURN NEW;
    END IF;
    NEW.user_uuid := v;
    RETURN NEW;
END $fn$;
DROP TRIGGER IF EXISTS trg_community_members_user_uuid ON community_members;
CREATE TRIGGER trg_community_members_user_uuid
    BEFORE INSERT OR UPDATE OF user_id, platform, platform_user_id, user_uuid
    ON community_members FOR EACH ROW EXECUTE FUNCTION community_members_set_user_uuid();
"""

#: Re-derive EVERY row (not just NULL ones): clear all derived state with the trigger
#: off first so no row's stale value blocks another's re-derivation via the unique index,
#: then let the trigger recompute each row from scratch in id order.
_REDERIVE_SQL = """
DO $rd$
DECLARE r record;
BEGIN
    ALTER TABLE community_members DISABLE TRIGGER trg_community_members_user_uuid;
    UPDATE community_members SET user_uuid = NULL, user_uuid_unavailable_reason = NULL
     WHERE user_uuid IS NOT NULL OR user_uuid_unavailable_reason IS NOT NULL;
    ALTER TABLE community_members ENABLE TRIGGER trg_community_members_user_uuid;
    FOR r IN SELECT id FROM community_members ORDER BY id LOOP
        UPDATE community_members SET user_id = user_id WHERE id = r.id;
    END LOOP;
END $rd$;
"""

_ERASE_FN_SQL = """
CREATE OR REPLACE FUNCTION erase_ephemeral_pseudonym_handles(
    p_platform text, p_platform_user_id text,
    p_tenant_id integer DEFAULT NULL, p_delete_mapping boolean DEFAULT false
) RETURNS integer LANGUAGE plpgsql AS $fn$
DECLARE
    n integer;
BEGIN
    IF coalesce(p_platform, '') = '' OR coalesce(p_platform_user_id, '') = '' THEN
        RAISE EXCEPTION
            'erase_ephemeral_pseudonym_handles: platform and platform_user_id are required'
            USING ERRCODE = '22023';
    END IF;
    IF p_delete_mapping THEN
        DELETE FROM ephemeral_pseudonyms
         WHERE platform = p_platform AND platform_user_id = p_platform_user_id
           AND (p_tenant_id IS NULL OR tenant_id = p_tenant_id);
    ELSE
        UPDATE ephemeral_pseudonyms SET handle = NULL
         WHERE platform = p_platform AND platform_user_id = p_platform_user_id
           AND (p_tenant_id IS NULL OR tenant_id = p_tenant_id) AND handle IS NOT NULL;
    END IF;
    GET DIAGNOSTICS n = ROW_COUNT;
    INSERT INTO identity_resolution_events (event, tenant_id, affected)
    VALUES (CASE WHEN p_delete_mapping THEN 'pseudonym_erased'
                 ELSE 'pseudonym_handle_erased' END, p_tenant_id, n);
    RETURN n;
END $fn$;
REVOKE ALL ON FUNCTION erase_ephemeral_pseudonym_handles(text, text, integer, boolean)
    FROM PUBLIC;
"""

_VIEW_BASE = (
    "SELECT cm.community_id, cm.platform, cm.platform_user_id, hu.uuid AS hub_user_uuid, "
    "cm.user_uuid"
)
_VIEW_JOIN = (
    " FROM community_members cm LEFT JOIN hub_users hu ON hu.id::text = cm.user_id"
)
_VIEW_STATUS_COLS = (
    ", CASE WHEN cm.user_uuid IS NOT NULL THEN 'resolved' ELSE 'unavailable' END "
    "AS user_uuid_status, cm.user_uuid_unavailable_reason"
)


def _grant_reader() -> str:
    return (
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_READER}') THEN "
        f"GRANT SELECT ON community_member_identities TO {_READER}; END IF; END $$;"
    )


def _load_0045() -> object:
    """Load 0045's module (digit-leading filename, so not importable by name) for downgrade."""
    path = Path(__file__).with_name("0045_identity_resolution.py")
    spec = importlib.util.spec_from_file_location("m0045_identity_resolution", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path} to restore the 0045 definitions")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    """Install hardened resolver/trigger, audit + reason columns, erasure fn, status view."""
    op.execute(_SCHEMA_SQL)
    op.execute(_RESOLVE_FN_SQL)
    op.execute(_TRIGGER_FN_SQL)
    op.execute(_ERASE_FN_SQL)
    op.execute(_REDERIVE_SQL)
    op.execute(
        "CREATE OR REPLACE VIEW community_member_identities AS "  # nosec B608 # noqa: S608 -- literals only
        + _VIEW_BASE
        + _VIEW_STATUS_COLS
        + _VIEW_JOIN
    )
    op.execute(_grant_reader())


def downgrade() -> None:
    """Restore the 0045 resolver/trigger/view and drop everything upgrade() added."""
    m0045 = _load_0045()
    op.execute("DROP VIEW IF EXISTS community_member_identities")
    op.execute(
        "CREATE VIEW community_member_identities AS "  # nosec B608 # noqa: S608 -- literals only
        + _VIEW_BASE
        + _VIEW_JOIN
    )
    op.execute(_grant_reader())
    op.execute(
        "DROP FUNCTION IF EXISTS erase_ephemeral_pseudonym_handles(text, text, integer, boolean)"
    )
    op.execute(m0045._RESOLVE_FN_SQL)  # type: ignore[attr-defined]
    op.execute(m0045._TRIGGER_FN_SQL)  # type: ignore[attr-defined]
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "resolve_identity_uuid(integer, text, text, text, text, boolean)"
    )
    op.execute("DROP TABLE IF EXISTS identity_resolution_events")
    op.execute(
        "ALTER TABLE community_members DROP CONSTRAINT IF EXISTS ck_community_members_uuid_reason"
    )
    op.execute("ALTER TABLE community_members DROP COLUMN IF EXISTS user_uuid_unavailable_reason")
