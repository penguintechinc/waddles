"""Twitch handle freshness: a renamed login must not keep routing to its old owner.

Twitch logins can be renamed and a released name can later be claimed by someone else. Before
this revision ``ephemeral_pseudonyms.handle`` was only refreshed for UNLINKED accounts
(``resolve_identity_uuid()`` returned early for a linked hub user) and never carried a
timestamp, and hub-api also matched the OAuth-time ``hub_user_identities.platform_username``,
so ``@oldname`` could keep resolving to the original person after a rename.

Changes:

- ``ephemeral_pseudonyms.handle_seen_at`` -- when an ingest event for that numeric platform id
  last asserted the handle. hub-api only accepts a Twitch handle with a recent
  ``handle_seen_at`` (see ``identity_resolution_service.TWITCH_HANDLE_FRESHNESS_DAYS``);
  legacy rows have NULL and never match until a fresh event refreshes them.
- ``resolve_identity_uuid()`` records every Twitch handle assertion against the NUMERIC id,
  linked or not, and clears the same handle (NFKC + lower) from every OTHER id in that tenant,
  so the newest claimant owns the name. ``handle:<name>`` fallback keys are never recorded.
  The refresh is throttled (only when the handle changed or the stamp is over an hour old) so
  the chat hot path does not rewrite the row on every message.

Revision ID: 0100_twitch_handle_freshness
Revises: 0099_discord_role_mirror
Create Date: 2026-11-12
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "0100_twitch_handle_freshness"
down_revision = "0099_discord_role_mirror"
branch_labels = None
depends_on = None

_SCHEMA_SQL = """
ALTER TABLE ephemeral_pseudonyms ADD COLUMN IF NOT EXISTS handle_seen_at TIMESTAMPTZ;
COMMENT ON COLUMN ephemeral_pseudonyms.handle_seen_at IS
    'When an ingest event for this platform id last asserted handle (Twitch freshness gate).';
"""

_RESOLVE_FN_SQL = """
CREATE OR REPLACE FUNCTION resolve_identity_uuid(
    p_tenant_id integer, p_platform text, p_platform_user_id text,
    p_user_id text, p_handle text, p_member_asserted boolean DEFAULT false
) RETURNS uuid LANGUAGE plpgsql AS $fn$
DECLARE
    v uuid;
    v_old_handle text;
    v_handle text := NULLIF(btrim(p_handle), '');
    v_seen timestamptz;
    v_found boolean := false;
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
    -- Twitch: the login is the handle, and it can be renamed and re-claimed. Record the
    -- assertion against the NUMERIC id (linked or not) and strip the name from every other id
    -- in the tenant, so the newest claimant owns it. Throttled to avoid a write per message.
    IF p_platform = 'twitch' AND v_handle IS NOT NULL
       AND p_platform_user_id NOT LIKE 'handle:%' THEN
        SELECT ep.handle, ep.handle_seen_at, true INTO v_old_handle, v_seen, v_found
          FROM ephemeral_pseudonyms ep
         WHERE ep.tenant_id = p_tenant_id AND ep.platform = p_platform
           AND ep.platform_user_id = p_platform_user_id;
        IF NOT coalesce(v_found, false) OR v_old_handle IS DISTINCT FROM v_handle
           OR v_seen IS NULL OR v_seen < now() - interval '1 hour' THEN
            UPDATE ephemeral_pseudonyms SET handle = NULL, handle_seen_at = NULL
             WHERE tenant_id = p_tenant_id AND platform = p_platform
               AND platform_user_id <> p_platform_user_id AND handle IS NOT NULL
               AND lower(normalize(handle, NFKC)) = lower(normalize(v_handle, NFKC));
            INSERT INTO ephemeral_pseudonyms
                   (tenant_id, platform, platform_user_id, handle, handle_seen_at)
            VALUES (p_tenant_id, p_platform, p_platform_user_id, v_handle, now())
            ON CONFLICT (tenant_id, platform, platform_user_id)
            DO UPDATE SET handle = EXCLUDED.handle, handle_seen_at = EXCLUDED.handle_seen_at;
        END IF;
        v_old_handle := NULL;
    END IF;
    -- A hub link only resolves INSIDE a tenant the hub user belongs to (or when the
    -- membership trigger vouches for the row): the global hub UUID is never handed to
    -- a tenant the person is not part of, so it cannot be used to correlate them.
    v := NULL;
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
            UPDATE ephemeral_pseudonyms SET handle = v_handle, handle_seen_at = now()
             WHERE pseudonym = v;
        END IF;
        RETURN v;
    END IF;
    INSERT INTO ephemeral_pseudonyms (tenant_id, platform, platform_user_id, handle, handle_seen_at)
    VALUES (p_tenant_id, p_platform, p_platform_user_id, v_handle,
            CASE WHEN v_handle IS NULL THEN NULL ELSE now() END)
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


def _load_0048() -> object:
    """Load 0048's module (digit-leading filename, so not importable by name) for downgrade."""
    path = Path(__file__).with_name("0048_identity_forged_uuid.py")
    spec = importlib.util.spec_from_file_location("m0048_identity_forged_uuid", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    """Add ``handle_seen_at`` and replace ``resolve_identity_uuid()`` with the fresh-handle one."""
    op.execute(_SCHEMA_SQL)
    op.execute(_RESOLVE_FN_SQL)


def downgrade() -> None:
    """Restore 0048's ``resolve_identity_uuid()`` and drop ``handle_seen_at``."""
    m0048 = _load_0048()
    op.execute(m0048._RESOLVE_FN_SQL)  # type: ignore[attr-defined]
    op.execute("ALTER TABLE ephemeral_pseudonyms DROP COLUMN IF EXISTS handle_seen_at")
