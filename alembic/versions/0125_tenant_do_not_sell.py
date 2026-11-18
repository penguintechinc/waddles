"""Tenant-scoped Do-Not-Sell: `tenant_do_not_sell` (a tenant admin's DSAR opt-out is per tenant).

Until now a tenant admin recording a Do-Not-Sell DSAR wrote `cookie_consent.preferences.doNotSell`,
which is the user's PLATFORM-WIDE consent record, so one tenant's admin changed the user's state in
EVERY tenant. The user's own choice (cookie banner / self-service / GPC) stays that global
preference; an admin's DSAR action is now a row here, scoped to `(tenant_id, user_id)`.

Effective state for a user inside tenant T = global user preference OR a row here for T
(`services/cookie_consent_service.effective_do_not_sell`).

- `source_code` (SMALLINT enum): 1 tenant_admin_dsar (console / inbox action), 2 dsar_request.
- `recorded_by`: the acting admin (NULL once that account is erased: ON DELETE SET NULL).
- `UNIQUE (tenant_id, user_id)`: one record per tenant and user (the write is idempotent).
- The table holds `user_id`, so Art. 17 erasure deletes the row (`services/erasure_registry.py`).

No data backfill: existing `cookie_consent` rows with `doNotSell=true` cannot be told apart as the
user's own choice or an earlier admin's action, so they stay as the user's global preference
(conservative: the opt-out keeps applying everywhere it already did).

Revision ID: 0125_tenant_do_not_sell
Revises: 0124_dsar_open_public_unique
Create Date: 2026-11-17
"""

from __future__ import annotations

import os

import sqlalchemy as sa

from alembic import op

revision = "0125_tenant_do_not_sell"
down_revision = "0124_dsar_open_public_unique"
branch_labels = None
depends_on = None

_UPGRADE = (
    """
CREATE TABLE IF NOT EXISTS tenant_do_not_sell (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id    UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
    source_code  SMALLINT NOT NULL,
    recorded_by  INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_tenant_dns_source CHECK (source_code BETWEEN 1 AND 2),
    CONSTRAINT uq_tenant_dns_tenant_user UNIQUE (tenant_id, user_id)
)
""",
    "CREATE INDEX IF NOT EXISTS ix_tenant_dns_user ON tenant_do_not_sell (user_id)",
)

_DOWNGRADE = ("DROP TABLE IF EXISTS tenant_do_not_sell",)

#: Set to `1` to knowingly discard recorded tenant opt-outs on downgrade.
_ALLOW_ENV = "ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE"
_COUNT = (
    "SELECT CASE WHEN to_regclass('tenant_do_not_sell') IS NULL THEN 0 "
    "ELSE (SELECT count(*) FROM tenant_do_not_sell) END"
)


def upgrade() -> None:
    """Create the tenant-scoped Do-Not-Sell table."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Drop the table; REFUSES while it holds opt-outs unless `ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE=1`.

    Each row is a statutory Do-Not-Sell the downgraded code would not enforce; dropping them
    silently would let a tenant sell data of a user who opted out. Export or migrate the rows
    first, or set the env var to confirm the loss.
    """
    held = op.get_bind().execute(sa.text(_COUNT)).scalar() or 0
    if held and os.environ.get(_ALLOW_ENV) != "1":
        raise RuntimeError(
            f"Refusing to drop tenant_do_not_sell: it holds {held} Do-Not-Sell record(s) that "
            f"the downgraded code would not enforce. Preserve them, or set {_ALLOW_ENV}=1."
        )
    for statement in _DOWNGRADE:
        op.execute(statement)
