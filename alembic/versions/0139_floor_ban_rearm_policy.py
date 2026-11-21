"""Tenant-admin `lift_rearm_drop`: one knob for the ban-lift re-arm threshold.

0138 introduced the re-arm drop as a per-community AND per-tenant policy column. The product
decision is a single TENANT-admin setting (exposed on the tenant reputation-policy API and web UI)
that governs community- and tenant-scope bans alike, so the per-community column is dropped, the
tenant column gets a 1..100 range, and `reputation_floor_lift_rearm_drop()` reads the tenant policy
only (default 5 when no policy row exists).

Revision ID: 0139_floor_ban_rearm_policy
Revises: 0138_floor_ban_platform_sync
Create Date: 2026-11-21
"""

from __future__ import annotations

from alembic import op

revision = "0139_floor_ban_rearm_policy"
down_revision = "0138_floor_ban_platform_sync"
branch_labels = None
depends_on = None

_FUNCTION_TENANT_ONLY = """
CREATE OR REPLACE FUNCTION reputation_floor_lift_rearm_drop(
    p_tenant bigint, p_community bigint, p_scope smallint
) RETURNS int
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $rd$
DECLARE
    v_drop int;
BEGIN
    SELECT lift_rearm_drop INTO v_drop FROM bundle_reputation_tenant_policy
     WHERE tenant_id = p_tenant;
    RETURN COALESCE(v_drop, 5);
END
$rd$
"""

_FUNCTION_PER_SCOPE = """
CREATE OR REPLACE FUNCTION reputation_floor_lift_rearm_drop(
    p_tenant bigint, p_community bigint, p_scope smallint
) RETURNS int
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $rd$
DECLARE
    v_drop int;
BEGIN
    IF p_scope = 2 THEN
        SELECT lift_rearm_drop INTO v_drop FROM bundle_reputation_tenant_policy
         WHERE tenant_id = p_tenant;
    ELSE
        SELECT lift_rearm_drop INTO v_drop FROM bundle_reputation_policy
         WHERE community_id = p_community AND tenant_id = p_tenant;
    END IF;
    RETURN COALESCE(v_drop, 5);
END
$rd$
"""


def upgrade() -> None:
    """Range-check the tenant knob, drop the per-community column, read the tenant policy only."""
    op.execute(
        "UPDATE bundle_reputation_tenant_policy SET lift_rearm_drop = LEAST(lift_rearm_drop, 100)"
    )
    op.execute(
        "ALTER TABLE bundle_reputation_tenant_policy "
        "DROP CONSTRAINT IF EXISTS ck_bundle_reputation_tenant_policy_lift_rearm_drop"
    )
    op.execute(
        "ALTER TABLE bundle_reputation_tenant_policy "
        "ADD CONSTRAINT ck_bundle_reputation_tenant_policy_lift_rearm_drop "
        "CHECK (lift_rearm_drop BETWEEN 1 AND 100)"
    )
    op.execute(_FUNCTION_TENANT_ONLY)
    op.execute(
        "ALTER TABLE bundle_reputation_policy DROP COLUMN IF EXISTS lift_rearm_drop"
    )


def downgrade() -> None:
    """Restore the per-community column and the per-scope reader."""
    op.execute(
        "ALTER TABLE bundle_reputation_policy "
        "ADD COLUMN IF NOT EXISTS lift_rearm_drop INTEGER NOT NULL DEFAULT 5 "
        "CHECK (lift_rearm_drop > 0)"
    )
    op.execute(
        "ALTER TABLE bundle_reputation_tenant_policy "
        "DROP CONSTRAINT IF EXISTS ck_bundle_reputation_tenant_policy_lift_rearm_drop"
    )
    op.execute(_FUNCTION_PER_SCOPE)
