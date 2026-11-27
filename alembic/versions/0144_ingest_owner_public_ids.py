"""Column-scoped reads for svc-ingest to resolve EventSub owners' public UUIDs to numeric keys.

hub-api's internal EventSub secret route (``SecretDTO``) now names the subscription's owners by
``public_id`` UUID only -- numeric PKs never cross an API boundary (entity-identity rule). The data
plane keys on numeric ids, so svc-ingest resolves the UUIDs itself from its PRIMARY database
(``core/svc_ingest/src/twitch_unban.rs`` ``OWNER_IDS_SQL``) -- the data plane reads, hub-api writes.

Grants (SELECT only, column-scoped; no write anywhere):

  - ``waddles_svc_ingest``: ``communities (public_id)`` (on top of 0101's ``id, tenant_id``) and
    ``platform_connections (id, tenant_id, public_id)``. NO token, resource or status column of
    ``platform_connections``; still NOTHING on ``tenants`` (the tenant is derived from the
    community row, so the 0055 ``SELECT 1 FROM tenants`` denial for this role still holds).

The same grant is declared in ``config/postgres/service-roles.yaml`` (``columns:``), which
``migrations/run-alembic.sh`` re-asserts with ``service_roles.py reconcile --strict``; this revision
records the change in the chain and applies it immediately. Idempotent and order-independent: a
missing role or table is a no-op. Downgrade revokes exactly these column grants.

Revision ID: 0144_ingest_owner_public_ids
Revises: 0143_eventsub_channel_unban
Create Date: 2026-11-25
"""

from __future__ import annotations

from alembic import op

revision = "0144_ingest_owner_public_ids"
down_revision = "0143_eventsub_channel_unban"
branch_labels = None
depends_on = None

#: role -> table -> columns granted SELECT. Fixed literals, never user input.
GRANTS: dict[str, dict[str, tuple[str, ...]]] = {
    "waddles_svc_ingest": {
        "communities": ("public_id",),
        "platform_connections": ("id", "tenant_id", "public_id"),
    },
}


def _guarded(role: str, table: str, body: str) -> str:
    """Run `body` only if both `role` and `table` exist (fresh-replay / order safe)."""
    return (
        "DO $$ BEGIN\n"  # nosec B608 -- fixed literals only
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}')\n"
        f"     AND to_regclass('public.{table}') IS NOT NULL THEN\n"
        f"    {body}\n"
        "  END IF;\n"
        "END $$;"
    )


def upgrade() -> None:
    """Grant column-scoped SELECT on the owner public-id resolution columns."""
    for role, tables in GRANTS.items():
        for table, cols in tables.items():
            op.execute(
                _guarded(
                    role,
                    table,
                    f"GRANT SELECT ({', '.join(cols)}) ON {table} TO {role};",
                )
            )


def downgrade() -> None:
    """Revoke exactly the column grants this revision added (back to 0143's state)."""
    for role, tables in GRANTS.items():
        for table, cols in tables.items():
            op.execute(
                _guarded(
                    role,
                    table,
                    f"REVOKE SELECT ({', '.join(cols)}) ON {table} FROM {role};",
                )
            )
