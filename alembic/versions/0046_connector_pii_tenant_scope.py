"""Tenant-scope `waddles_connector_pii_reader`: security-barrier views, raw grants revoked.

SECURITY REVIEW (PII / tenant isolation). `0044_connector_pii_reader_role`
granted the connector PII reader column-scoped `SELECT` directly on three
shared tables -- `hub_user_identities.platform_username` and
`community_members.display_name` (raw PII) plus `hub_users.(id, uuid)` -- with
**no row filter at all**. A session authenticated as the role could therefore
read every tenant's handles and display names in one query. The tenant scoping
the design relies on (`docs/superpowers/specs/2026-09-28-connector-bundles.md`
S3.3: "names only inside hub-api, tenant-scoped") lived only in an unimplemented
host handler, i.e. nowhere that could actually refuse a cross-tenant read.

This migration moves the boundary into the database, where it cannot be
skipped by a buggy or missing caller:

- `connector_pii_current_tenant()` -- reads the session GUC `waddles.tenant_id`
  (the same GUC `hub_api/services/bundle_data_ddl.py`'s per-app RLS policies key
  on, set per transaction with `SET LOCAL` by the host from the invoking
  scope, never from guest input). **Fail-closed and fail-loud**: unset, empty
  or non-numeric raises `insufficient_privilege` (SQLSTATE 42501) instead of
  quietly matching nothing, so a missing tenant can never be mistaken for
  "no such identity" by a caller.
- `connector_pii_identities` -- `hub_user_identities` joined to `hub_users`,
  restricted to hub users who are a member of at least one community of the
  session's tenant. Exposes `platform_username` (as the raw handle column) and
  `hub_users.uuid` only; never an internal `hub_users.id`.
- `connector_pii_members` -- `community_members` joined to `communities`,
  restricted to the session's tenant. Exposes `display_name`.

Both views are `security_barrier` (the row predicate runs before any
caller-supplied qual, so a leaky function in the caller's `WHERE` cannot
observe another tenant's rows) and run with the view owner's privileges, so
the reader needs no access to the underlying tables. The role's column-level
grants from 0044 are **revoked** and replaced by `SELECT` on exactly these two
views: no cross-tenant raw PII is reachable by any query shape. The role also
gets `USAGE ON SCHEMA public` (0030 revoked the PUBLIC-wide grant; 0044 never
restored it for this role, so in a real chain it could not resolve a table name).

**Why views and not `ENABLE ROW LEVEL SECURITY` on the base tables.**
`hub_users`, `hub_user_identities` and `community_members` are shared legacy
tables read and written by many roles (hub-api, the `mod_*` module roles,
`waddles_bundle_reader`). Turning RLS on for them is default-deny for every
role without an explicit policy, a blast radius far beyond this one role. A
view confines the change to the connector reader. The DB-level guard is
defense in depth beside the host's own tenant check
(`core/bundle_executor/src/host/connector_imports.rs`): the GUC is
session-settable, so the host process holding this role's connection must set
it only from the host-derived invocation scope.

Revision ID: 0046_connector_pii_tenant_scope
Revises: 0045_identity_resolution
Create Date: 2026-10-09
"""

from __future__ import annotations

from alembic import op

revision = "0046_connector_pii_tenant_scope"
down_revision = "0045_identity_resolution"
branch_labels = None
depends_on = None

#: The role scoped by this migration (created by 0044).
_ROLE = "waddles_connector_pii_reader"

#: Session GUC carrying the host-derived tenant id; shared with the per-app
#: bundle-table RLS policies (`hub_api/services/bundle_data_ddl.py`).
_TENANT_GUC = "waddles.tenant_id"

_TENANT_FN_SQL = f"""
CREATE OR REPLACE FUNCTION connector_pii_current_tenant() RETURNS integer
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v text := NULLIF(current_setting('{_TENANT_GUC}', true), '');
BEGIN
    IF v IS NULL THEN
        RAISE EXCEPTION 'connector PII read denied: {_TENANT_GUC} is not set'
            USING ERRCODE = '42501';
    END IF;
    IF v !~ '^[0-9]{{1,9}}$' THEN
        RAISE EXCEPTION 'connector PII read denied: {_TENANT_GUC} is not a tenant id'
            USING ERRCODE = '42501';
    END IF;
    RETURN v::integer;
END $fn$;
REVOKE ALL ON FUNCTION connector_pii_current_tenant() FROM PUBLIC;
"""

# `(SELECT connector_pii_current_tenant())` is an uncorrelated scalar subquery:
# Postgres evaluates it once per query (an InitPlan) rather than once per row,
# and the resulting parameter still drives index scans.
_IDENTITIES_VIEW_SQL = """
CREATE OR REPLACE VIEW connector_pii_identities WITH (security_barrier = true) AS
SELECT (SELECT connector_pii_current_tenant()) AS tenant_id,
       hu.uuid                                  AS hub_user_uuid,
       hui.platform                             AS platform,
       hui.platform_user_id                     AS platform_user_id,
       hui.platform_username                    AS handle
  FROM hub_user_identities hui
  JOIN hub_users hu ON hu.id = hui.hub_user_id
 WHERE EXISTS (
        SELECT 1
          FROM community_members cm
          JOIN communities c ON c.id = cm.community_id
         WHERE c.tenant_id = (SELECT connector_pii_current_tenant())
           AND (cm.user_id = hu.id::text
                OR (cm.platform = hui.platform
                    AND cm.platform_user_id = hui.platform_user_id))
       );
REVOKE ALL ON connector_pii_identities FROM PUBLIC;
COMMENT ON VIEW connector_pii_identities IS
    'Tenant-scoped (waddles.tenant_id, fail-closed) handle/uuid reads for '
    'waddles_connector_pii_reader. Raw PII: tenant-bounded by construction.';
"""

_MEMBERS_VIEW_SQL = """
CREATE OR REPLACE VIEW connector_pii_members WITH (security_barrier = true) AS
SELECT c.tenant_id          AS tenant_id,
       cm.community_id      AS community_id,
       cm.platform          AS platform,
       cm.platform_user_id  AS platform_user_id,
       cm.display_name      AS display_name
  FROM community_members cm
  JOIN communities c ON c.id = cm.community_id
 WHERE c.tenant_id = (SELECT connector_pii_current_tenant());
REVOKE ALL ON connector_pii_members FROM PUBLIC;
COMMENT ON VIEW connector_pii_members IS
    'Tenant-scoped (waddles.tenant_id, fail-closed) display-name reads for '
    'waddles_connector_pii_reader. Raw PII: tenant-bounded by construction.';
"""


def _role_exists_guard(body: str) -> str:
    """Wrap `body` in a `DO $$ ... $$` block that only runs if `_ROLE` exists.

    Same order-independent posture as 0044: the role is created there, but a
    guarded no-op keeps this migration safe on any partially provisioned DB.
    """
    return (
        "DO $$ BEGIN\n"  # nosec B608 -- role name is a fixed literal
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}') THEN\n"
        f"    {body}\n"
        "  END IF;\n"
        "END $$;"
    )


def upgrade() -> None:
    """Create the tenant guard + scoped views, then swap the role's raw grants for view grants."""
    op.execute(_TENANT_FN_SQL)
    op.execute(_IDENTITIES_VIEW_SQL)
    op.execute(_MEMBERS_VIEW_SQL)

    # Revoke BEFORE granting so a failure part-way never leaves the broader
    # access in place beside the new one. Table-level REVOKE also clears the
    # column-level grants 0044 issued (`GRANT SELECT (cols) ON ...`).
    op.execute(_role_exists_guard(f"REVOKE ALL PRIVILEGES ON hub_users FROM {_ROLE};"))
    op.execute(
        _role_exists_guard(
            f"REVOKE ALL PRIVILEGES ON hub_user_identities FROM {_ROLE};"
        )
    )
    op.execute(
        _role_exists_guard(f"REVOKE ALL PRIVILEGES ON community_members FROM {_ROLE};")
    )
    op.execute(
        _role_exists_guard(
            f"GRANT SELECT ON connector_pii_identities, connector_pii_members TO {_ROLE};"
        )
    )
    # Table access inside a view is checked as the view OWNER, but a function
    # called by the view is checked as the INVOKING user -- so the reader needs
    # EXECUTE on the guard (granted to no one else; PUBLIC was revoked above).
    op.execute(
        _role_exists_guard(
            f"GRANT EXECUTE ON FUNCTION connector_pii_current_tenant() TO {_ROLE};"
        )
    )
    # 0030 revoked the implicit PUBLIC USAGE on schema public (and 0032 re-grants
    # it explicitly to waddles_bundle_reader for exactly this reason). 0044 never
    # did for this role, so in a real chain it could not even resolve a table
    # name. USAGE only lets it name objects in `public`; what it may then touch
    # is still limited to the two views above.
    op.execute(_role_exists_guard(f"GRANT USAGE ON SCHEMA public TO {_ROLE};"))


def downgrade() -> None:
    """Restore 0044's column-scoped raw grants and drop the guard + views."""
    op.execute(_role_exists_guard(f"REVOKE USAGE ON SCHEMA public FROM {_ROLE};"))
    op.execute(
        _role_exists_guard(
            "REVOKE ALL PRIVILEGES ON connector_pii_identities, "
            f"connector_pii_members FROM {_ROLE};"
        )
    )
    op.execute("DROP VIEW IF EXISTS connector_pii_identities")
    op.execute("DROP VIEW IF EXISTS connector_pii_members")
    op.execute("DROP FUNCTION IF EXISTS connector_pii_current_tenant()")

    # Verbatim 0044 grants (the pre-0046 state this revision narrowed).
    op.execute(_role_exists_guard(f"GRANT SELECT (id, uuid) ON hub_users TO {_ROLE};"))
    op.execute(
        _role_exists_guard(
            "GRANT SELECT (hub_user_id, platform, platform_user_id, platform_username) "
            f"ON hub_user_identities TO {_ROLE};"
        )
    )
    op.execute(
        _role_exists_guard(
            "GRANT SELECT (community_id, platform, platform_user_id, display_name) "
            f"ON community_members TO {_ROLE};"
        )
    )
