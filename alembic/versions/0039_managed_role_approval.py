"""Adds a real `pending` state to `managed_roles` for adopted-role approval (#500/#501 follow-up).

**Why this, not `audit_log`, and not a new table.** The first pass of the
guild-pairing REST API (PR #503) staged an `adopted` role registration
request as an `audit_log` row and only inserted the real `managed_roles`
row once guild-authority approved it. Reviewed and rejected: `audit_log`
is append-only evidence of what happened, never the source of truth for
what *is currently true* -- treating a row there as "the pending request"
made the audit trail double as mutable workflow state, which is exactly
the anti-pattern audit logs exist to prevent.

A brand-new `managed_role_requests` table was also considered and
rejected as needless duplication: a "pending adopted role registration"
and "an active managed role" are the same conceptual resource
(`(platform, guild_id, role_id)` claimed by one community) at different
points in its lifecycle, not two different resources. Reusing
`managed_roles` with a new `approval_status` column means the table's
own `UNIQUE (platform, guild_id, role_id)` continues to do double duty
as the request-uniqueness guard for free -- a second pending or active
claim on the same role is rejected by the same constraint that already
prevents two communities owning one role, with no new index to maintain
and no cross-table sync between "the request" and "the grant" once
approved (an UPDATE in place, not an INSERT into a second table plus a
DELETE from the first).

Changes:
- `managed_roles.approval_status` (`pending` / `approved` / `rejected`),
  `DEFAULT 'approved'` so every pre-existing row (and every future
  `created`-kind row, which needs no approval) is unaffected.
- `managed_roles.status`'s CHECK gains a `pending_approval` value --
  `v_managed_roles_active` already filters on `status = 'active'`, so a
  pending row is invisible to the data plane with no view change.
- `chk_managed_roles_adopted_approval` is relaxed: `approved_by_user_id`
  is required only once `approval_status = 'approved'`, not for every
  `adopted` row unconditionally -- a pending adopted row can now exist
  before anyone has approved it.

**Known, documented limitation carried over from migration 0038, not
introduced here:** `UNIQUE (platform, guild_id, role_id)` is
unconditional, not a partial index scoped to `active`/`pending_approval`
rows (contrast `community_channel_bindings`'s partial unique indexes) --
a `rejected` or `removed` row permanently occupies that role's slot.
Narrowing it to a partial index is a follow-up, not implemented here to
keep this migration to the single, requested change (a pending-approval
state); no `reject` endpoint is implemented in this PR for the same
reason -- `rejected` is defined as a value applications may use, not yet
reachable via this API.

Revision ID: 0039_managed_role_approval
Revises: 0038_guild_tenant_pairing
Create Date: 2026-09-29
"""

from __future__ import annotations

from alembic import op

revision = "0039_managed_role_approval"
down_revision = "0038_guild_tenant_pairing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE managed_roles ADD COLUMN IF NOT EXISTS approval_status VARCHAR(20) "
        "NOT NULL DEFAULT 'approved'"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = "
        "'chk_managed_roles_approval_status') THEN "
        "ALTER TABLE managed_roles ADD CONSTRAINT chk_managed_roles_approval_status "
        "CHECK (approval_status IN ('pending', 'approved', 'rejected')); "
        "END IF; END $$"
    )
    op.execute(
        "COMMENT ON COLUMN managed_roles.approval_status IS "
        "'pending: an adopted-role registration awaiting guild-authority approval "
        "(status stays pending_approval, invisible to v_managed_roles_active). "
        "approved: the default for every created row and every approved adopted row. "
        "rejected: defined for future use -- no reject endpoint exists yet, see the "
        "migration module docstring known-limitation note.'"
    )

    op.execute(
        "ALTER TABLE managed_roles DROP CONSTRAINT IF EXISTS managed_roles_status_check"
    )
    op.execute(
        "ALTER TABLE managed_roles ADD CONSTRAINT managed_roles_status_check "
        "CHECK (status IN ('active', 'pending_cleanup', 'removed', 'pending_approval'))"
    )

    op.execute(
        "ALTER TABLE managed_roles DROP CONSTRAINT IF EXISTS chk_managed_roles_adopted_approval"
    )
    op.execute(
        "ALTER TABLE managed_roles ADD CONSTRAINT chk_managed_roles_adopted_approval "
        "CHECK (registered_via = 'created' OR approval_status <> 'approved' "
        "OR approved_by_user_id IS NOT NULL)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE managed_roles DROP CONSTRAINT IF EXISTS chk_managed_roles_adopted_approval"
    )
    op.execute(
        "ALTER TABLE managed_roles ADD CONSTRAINT chk_managed_roles_adopted_approval "
        "CHECK (registered_via = 'created' OR approved_by_user_id IS NOT NULL)"
    )
    op.execute(
        "ALTER TABLE managed_roles DROP CONSTRAINT IF EXISTS managed_roles_status_check"
    )
    op.execute(
        "ALTER TABLE managed_roles ADD CONSTRAINT managed_roles_status_check "
        "CHECK (status IN ('active', 'pending_cleanup', 'removed'))"
    )
    op.execute(
        "ALTER TABLE managed_roles DROP CONSTRAINT IF EXISTS chk_managed_roles_approval_status"
    )
    op.execute("ALTER TABLE managed_roles DROP COLUMN IF EXISTS approval_status")
