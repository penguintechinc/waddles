"""Split the `storage.objects` permission key into `.read` and `.write` (per-action ids).

The catalog replaced the single `storage.objects` id with `storage.objects.read`
(get/head/list/presign_get) and `storage.objects.write` (put/delete/tag). Every table
keyed by a permission id would otherwise be orphaned -- most dangerously an instance
`deny` (kill-switch) on `storage.objects` that would silently stop matching anything.

This data migration rewrites every `storage.objects` row into equivalent rows for BOTH new
keys (action and param_scope preserved; a conflicting existing new-key row is kept, upgraded
to `deny` when the old row denied) and then removes the old key:

- `instance_permission_policies` (+ one audit row per new key in
  `instance_permission_policy_audit`; historical audit rows are left untouched),
- `community_permission_grants`, `app_permission_requests`,
  `app_tenant_permission_restrictions`.

Explicit downgrade (lossy only where the two halves diverge, always toward LESS access):
policies collapse to `deny` if either half denies; a grant is restored ACTIVE only when both
halves exist and are active (otherwise restored REVOKED, revoked_at/by from the revoked half);
the approval ceiling is restored only when BOTH halves are approved (the old row has the
stricter risk, earlier approved_at, merged params; a row's existence IS the approval, so a
half-approved permission is simply left unapproved); a tenant restriction is restored if
EITHER half is restricted.

Note: `app_permission_grant_versions.permission_snapshot_hash` is computed application-side
and is NOT recomputed here; a hub-api/data-plane restart (or any grant change) refreshes it.
No shipped bundle manifest declared `storage.objects`, so no manifest rewrite is needed.

Revision ID: 0119_storage_objects_split
Revises: 0108_bundle_precompile_requests, 0109_platform_vault
Create Date: 2026-11-15

The `down_revision` tuple merges the two heads that exist on `release/v3.0.X` today;
re-point it to the release head at merge time (ALEMBIC ORDER rule, `.PLAN`).
"""

from __future__ import annotations

from alembic import op

revision = "0119_storage_objects_split"
down_revision = ("0108_bundle_precompile_requests", "0109_platform_vault")
branch_labels = None
depends_on = None

_OLD = "storage.objects"
_NEW = ("storage.objects.read", "storage.objects.write")


def upgrade() -> None:
    for new in _NEW:
        # -- instance policies: a pre-existing new-key row is escalated to deny when the old row denied
        op.execute(
            f"""
            UPDATE instance_permission_policies n
               SET action = 'deny'
              FROM instance_permission_policies o
             WHERE o.permission_key = '{_OLD}' AND o.action = 'deny'
               AND n.permission_key = '{new}'
               AND n.param_scope IS NOT DISTINCT FROM o.param_scope
            """
        )
        op.execute(
            f"""
            INSERT INTO instance_permission_policies (permission_key, param_scope, action, set_by, set_at)
            SELECT '{new}', o.param_scope, o.action, o.set_by, o.set_at
              FROM instance_permission_policies o
             WHERE o.permission_key = '{_OLD}'
               AND NOT EXISTS (
                   SELECT 1 FROM instance_permission_policies n
                    WHERE n.permission_key = '{new}'
                      AND n.param_scope IS NOT DISTINCT FROM o.param_scope)
            """
        )
        op.execute(
            f"""
            INSERT INTO instance_permission_policy_audit
                (permission_key, param_scope, previous_action, new_action, cascaded_revocations, set_by)
            SELECT '{new}', o.param_scope, NULL, o.action, 0, NULL
              FROM instance_permission_policies o
             WHERE o.permission_key = '{_OLD}'
            """
        )
        # -- community grants (PK community_id, app_id, permission_id)
        op.execute(
            f"""
            INSERT INTO community_permission_grants
                (community_id, tenant_id, app_id, permission_id, params_json,
                 granted_by, granted_at, revoked_by, revoked_at)
            SELECT community_id, tenant_id, app_id, '{new}', params_json,
                   granted_by, granted_at, revoked_by, revoked_at
              FROM community_permission_grants WHERE permission_id = '{_OLD}'
            ON CONFLICT (community_id, app_id, permission_id) DO NOTHING
            """
        )
        # -- global approval ceiling (UNIQUE app_id, version, permission_id)
        op.execute(
            f"""
            INSERT INTO app_permission_requests
                (app_id, version, permission_id, risk, params_json, justification,
                 approved_by, approval_source, approved_at)
            SELECT app_id, version, '{new}', risk, params_json, justification,
                   approved_by, approval_source, approved_at
              FROM app_permission_requests WHERE permission_id = '{_OLD}'
            ON CONFLICT (app_id, version, permission_id) DO NOTHING
            """
        )
        # -- tenant restrictions (PK tenant_id, app_id, permission_id): a restriction on both halves
        op.execute(
            f"""
            INSERT INTO app_tenant_permission_restrictions
                (tenant_id, app_id, permission_id, restricted_by, restricted_at)
            SELECT tenant_id, app_id, '{new}', restricted_by, restricted_at
              FROM app_tenant_permission_restrictions WHERE permission_id = '{_OLD}'
            ON CONFLICT (tenant_id, app_id, permission_id) DO NOTHING
            """
        )
    for table in (
        "instance_permission_policies",
        "community_permission_grants",
        "app_permission_requests",
        "app_tenant_permission_restrictions",
    ):
        col = (
            "permission_key"
            if table == "instance_permission_policies"
            else "permission_id"
        )
        op.execute(f"DELETE FROM {table} WHERE {col} = '{_OLD}'")


def downgrade() -> None:
    read, write = _NEW
    # policies: deny wins if either half denies; one old row per distinct param_scope
    op.execute(
        f"""
        INSERT INTO instance_permission_policies (permission_key, param_scope, action, set_by, set_at)
        SELECT '{_OLD}', s.param_scope,
               CASE WHEN bool_or(s.action = 'deny') THEN 'deny' ELSE 'allow' END,
               NULL, NOW()
          FROM instance_permission_policies s
         WHERE s.permission_key IN ('{read}', '{write}')
         GROUP BY s.param_scope
        """
    )
    op.execute(
        f"DELETE FROM instance_permission_policies WHERE permission_key IN ('{read}', '{write}')"
    )
    # grants: the old grant is ACTIVE only when BOTH halves exist and are active; any other
    # combination (a revoked half, or a missing half) is restored as REVOKED -- never widen access.
    op.execute(
        f"""
        INSERT INTO community_permission_grants
            (community_id, tenant_id, app_id, permission_id, params_json,
             granted_by, granted_at, revoked_by, revoked_at)
        SELECT COALESCE(r.community_id, w.community_id),
               COALESCE(r.tenant_id, w.tenant_id),
               COALESCE(r.app_id, w.app_id),
               '{_OLD}',
               COALESCE(r.params_json, '{{}}'::jsonb) || COALESCE(w.params_json, '{{}}'::jsonb),
               COALESCE(r.granted_by, w.granted_by),
               LEAST(r.granted_at, w.granted_at),
               CASE WHEN r.community_id IS NULL OR w.community_id IS NULL THEN NULL
                    WHEN r.revoked_at IS NOT NULL THEN r.revoked_by
                    ELSE w.revoked_by END,
               CASE WHEN r.community_id IS NULL OR w.community_id IS NULL THEN NOW()
                    WHEN r.revoked_at IS NULL AND w.revoked_at IS NULL THEN NULL
                    ELSE LEAST(r.revoked_at, w.revoked_at) END
          FROM (SELECT * FROM community_permission_grants WHERE permission_id = '{read}') r
          FULL OUTER JOIN
               (SELECT * FROM community_permission_grants WHERE permission_id = '{write}') w
            ON w.community_id = r.community_id AND w.app_id = r.app_id
        ON CONFLICT (community_id, app_id, permission_id) DO NOTHING
        """
    )
    op.execute(
        f"DELETE FROM community_permission_grants WHERE permission_id IN ('{read}', '{write}')"
    )
    op.execute(
        f"""
        INSERT INTO app_permission_requests
            (app_id, version, permission_id, risk, params_json, justification,
             approved_by, approval_source, approved_at)
        SELECT r.app_id, r.version, '{_OLD}',
               CASE WHEN 'dangerous' IN (r.risk, w.risk) THEN 'dangerous' ELSE 'normal' END,
               r.params_json || w.params_json,
               r.justification,
               CASE WHEN r.approved_at <= w.approved_at THEN r.approved_by ELSE w.approved_by END,
               CASE WHEN 'human' IN (r.approval_source, w.approval_source) THEN 'human'
                    ELSE r.approval_source END,
               LEAST(r.approved_at, w.approved_at)
          FROM app_permission_requests r
          JOIN app_permission_requests w
            ON w.app_id = r.app_id AND w.version = r.version AND w.permission_id = '{write}'
         WHERE r.permission_id = '{read}'
        ON CONFLICT (app_id, version, permission_id) DO NOTHING
        """
    )
    op.execute(
        f"DELETE FROM app_permission_requests WHERE permission_id IN ('{read}', '{write}')"
    )
    # restrictions: restricted if EITHER half is restricted
    op.execute(
        f"""
        INSERT INTO app_tenant_permission_restrictions
            (tenant_id, app_id, permission_id, restricted_by, restricted_at)
        SELECT DISTINCT ON (tenant_id, app_id) tenant_id, app_id, '{_OLD}', restricted_by, restricted_at
          FROM app_tenant_permission_restrictions
         WHERE permission_id IN ('{read}', '{write}')
         ORDER BY tenant_id, app_id, restricted_at
        ON CONFLICT (tenant_id, app_id, permission_id) DO NOTHING
        """
    )
    op.execute(
        f"DELETE FROM app_tenant_permission_restrictions WHERE permission_id IN ('{read}', '{write}')"
    )
