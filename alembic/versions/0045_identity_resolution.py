"""Identity resolution (#429): ephemeral pseudonyms + community_members.user_uuid.

Completes the identity layer on top of 0043's ``hub_users.uuid``:

- ``ephemeral_pseudonyms`` -- per-(tenant, platform, platform_user_id) stable
  UUID for platform accounts with no linked hub user. The only place a raw
  handle is stored (``handle``, PII); the table is hub-api-owned and revoked
  from PUBLIC -- no data-plane role gets any grant on it.
- ``resolve_identity_uuid()`` -- the single source of truth for
  "platform identity -> stable UUID": explicit ``user_id`` link, else
  ``hub_user_identities`` link, else get-or-create pseudonym. Raises on
  insufficient input (never a silent default).
- ``community_members.user_uuid`` (nullable, added IF NOT EXISTS so it is
  order-independent with the reputation-store migration that also needs it)
  populated by a BEFORE INSERT/UPDATE trigger that calls the function, plus a
  one-time backfill that re-fires the trigger per existing row.
- ``community_member_identities`` view gains ``user_uuid`` (appended; no PII).

Rows that cannot be resolved (no hub link and no platform/platform_user_id, or
a second platform account of the same hub user inside one community, which the
(community_id, user_uuid) unique index forbids) keep ``user_uuid`` NULL with a
WARNING -- consumers (reputation, economy) fail closed on NULL.

Revision ID: 0045_identity_resolution
Revises: 0044_connector_pii_reader_role
Create Date: 2026-10-09
"""

from __future__ import annotations

from alembic import op

revision = "0045_identity_resolution"
down_revision = "0044_connector_pii_reader_role"
branch_labels = None
depends_on = None

_READER = "waddles_bundle_reader"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ephemeral_pseudonyms (
    pseudonym        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    platform         VARCHAR(50) NOT NULL,
    platform_user_id VARCHAR(255) NOT NULL,
    handle           VARCHAR(255),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_ephemeral_pseudonyms_identity UNIQUE (tenant_id, platform, platform_user_id)
);
REVOKE ALL ON ephemeral_pseudonyms FROM PUBLIC;
COMMENT ON TABLE ephemeral_pseudonyms IS
    'PII boundary: hub-api only. handle is raw PII; never grant to data-plane roles.';
"""

_RESOLVE_FN_SQL = """
CREATE OR REPLACE FUNCTION resolve_identity_uuid(
    p_tenant_id integer, p_platform text, p_platform_user_id text,
    p_user_id text, p_handle text
) RETURNS uuid LANGUAGE plpgsql AS $fn$
DECLARE
    v uuid;
BEGIN
    IF p_user_id IS NOT NULL THEN
        SELECT hu.uuid INTO v FROM hub_users hu WHERE hu.id::text = p_user_id;
        IF v IS NOT NULL THEN
            RETURN v;
        END IF;
    END IF;
    IF p_tenant_id IS NULL OR coalesce(p_platform, '') = ''
       OR coalesce(p_platform_user_id, '') = '' THEN
        RAISE EXCEPTION
            'resolve_identity_uuid: tenant_id, platform and platform_user_id are required'
            USING ERRCODE = '22023';
    END IF;
    SELECT hu.uuid INTO v
      FROM hub_user_identities hui JOIN hub_users hu ON hu.id = hui.hub_user_id
     WHERE hui.platform = p_platform AND hui.platform_user_id = p_platform_user_id
     ORDER BY hu.id LIMIT 1;
    IF v IS NOT NULL THEN
        RETURN v;
    END IF;
    INSERT INTO ephemeral_pseudonyms (tenant_id, platform, platform_user_id, handle)
    VALUES (p_tenant_id, p_platform, p_platform_user_id, p_handle)
    ON CONFLICT (tenant_id, platform, platform_user_id) DO NOTHING;
    SELECT ep.pseudonym INTO v FROM ephemeral_pseudonyms ep
     WHERE ep.tenant_id = p_tenant_id AND ep.platform = p_platform
       AND ep.platform_user_id = p_platform_user_id;
    IF v IS NULL THEN
        RAISE EXCEPTION 'resolve_identity_uuid: pseudonym mint failed' USING ERRCODE = 'XX000';
    END IF;
    RETURN v;
END $fn$;
REVOKE ALL ON FUNCTION resolve_identity_uuid(integer, text, text, text, text) FROM PUBLIC;
"""

_TRIGGER_FN_SQL = """
CREATE OR REPLACE FUNCTION community_members_set_user_uuid() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    v_tenant integer;
    v uuid;
BEGIN
    IF NEW.user_uuid IS NOT NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.user_id IS NOT NULL THEN
        SELECT hu.uuid INTO v FROM hub_users hu WHERE hu.id::text = NEW.user_id;
    END IF;
    IF v IS NULL THEN
        IF coalesce(NEW.platform, '') = '' OR coalesce(NEW.platform_user_id, '') = '' THEN
            RAISE WARNING '0045 user_uuid unresolved: no link, no platform id (community_id=%)',
                NEW.community_id;
            RETURN NEW;
        END IF;
        SELECT c.tenant_id INTO v_tenant FROM communities c WHERE c.id = NEW.community_id;
        IF v_tenant IS NULL THEN
            RAISE WARNING '0045 user_uuid unresolved: community has no tenant (community_id=%)',
                NEW.community_id;
            RETURN NEW;
        END IF;
        v := resolve_identity_uuid(
            v_tenant, NEW.platform, NEW.platform_user_id, NEW.user_id, NEW.display_name);
    END IF;
    IF EXISTS (SELECT 1 FROM community_members m
                WHERE m.community_id = NEW.community_id AND m.user_uuid = v
                  AND m.id IS DISTINCT FROM NEW.id) THEN
        RAISE WARNING '0045 user_uuid NULL: uuid already bound in community (community_id=%)',
            NEW.community_id;
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

_BACKFILL_SQL = """
DO $bf$
DECLARE r record;
BEGIN
    FOR r IN SELECT id FROM community_members WHERE user_uuid IS NULL ORDER BY id LOOP
        UPDATE community_members SET user_id = user_id WHERE id = r.id;
    END LOOP;
END $bf$;
"""

_VIEW_BASE = (
    "SELECT cm.community_id, cm.platform, cm.platform_user_id, hu.uuid AS hub_user_uuid"
)
_VIEW_JOIN = (
    " FROM community_members cm LEFT JOIN hub_users hu ON hu.id::text = cm.user_id"
)


def _grant_reader() -> str:
    return (
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_READER}') THEN "
        f"GRANT SELECT ON community_member_identities TO {_READER}; END IF; END $$;"
    )


def upgrade() -> None:
    """Add pseudonym store, resolver, membership trigger, backfill and view column."""
    op.execute("ALTER TABLE community_members ADD COLUMN IF NOT EXISTS user_uuid UUID")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_community_members_community_user_uuid "
        "ON community_members (community_id, user_uuid) WHERE user_uuid IS NOT NULL"
    )
    op.execute(_TABLE_SQL)
    op.execute(_RESOLVE_FN_SQL)
    op.execute(_TRIGGER_FN_SQL)
    op.execute(_BACKFILL_SQL)
    op.execute(
        "CREATE OR REPLACE VIEW community_member_identities AS "  # nosec B608 # noqa: S608 -- literals only
        + _VIEW_BASE
        + ", cm.user_uuid"
        + _VIEW_JOIN
    )
    op.execute(_grant_reader())


def downgrade() -> None:
    """Drop everything upgrade() added (restores the 0043 view shape)."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_community_members_user_uuid ON community_members"
    )
    op.execute("DROP FUNCTION IF EXISTS community_members_set_user_uuid()")
    op.execute("DROP VIEW IF EXISTS community_member_identities")
    op.execute(
        "CREATE VIEW community_member_identities AS "  # nosec B608 # noqa: S608 -- literals only
        + _VIEW_BASE
        + _VIEW_JOIN
    )
    op.execute(_grant_reader())
    op.execute(
        "DROP FUNCTION IF EXISTS resolve_identity_uuid(integer, text, text, text, text)"
    )
    op.execute("DROP TABLE IF EXISTS ephemeral_pseudonyms")
    op.execute("DROP INDEX IF EXISTS uq_community_members_community_user_uuid")
    op.execute("ALTER TABLE community_members DROP COLUMN IF EXISTS user_uuid")
