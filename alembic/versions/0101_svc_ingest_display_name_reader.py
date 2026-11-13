"""Column-scoped read-only display-name access for `waddles_svc_ingest` (egress detokenizer).

User decision 2026-11-12: hub-api is the control-plane writer and the data plane reads the
database directly -- hub-api stays out of the hot path. svc-ingest's outbound drain is the
only platform egress and resolves `{user:<uuid>}` placeholders to display names immediately
before each send with ONE batched query per message (`core/svc_ingest/src/display_names.rs`),
under its own least-privilege role. Only svc-ingest (gateway) and svc-presentation may hold
usernames; nothing is cached.

Grants (SELECT only, column-scoped -- never email, password, IP, payment or any other
column; no INSERT/UPDATE/DELETE anywhere):

  - `hub_users (id, uuid, display_name)` -- `id` is the legacy `community_members.user_id`
    join key; Postgres needs SELECT on every column a query touches.
  - `community_members (id, community_id, user_uuid, user_id, display_name)` -- membership
    is the tenant gate (a hub user resolves only while a member of the entry's tenant) and
    the erasure gate (erasure deletes the rows, so an erased user resolves to nothing and
    the sender substitutes "Unknown User"). `id` orders the newest per-community name.
  - `communities (id, tenant_id)` -- community -> tenant.
  - `ephemeral_pseudonyms (tenant_id, pseudonym, handle)` -- platform users with no hub
    account, tenant-scoped.

The same grants are declared in `config/postgres/service-roles.yaml` (`waddles_svc_ingest`
`columns:`), which `migrations/run-alembic.sh` re-asserts with `service_roles.py reconcile
--strict` after every upgrade; this migration records the change in the chain and applies it
immediately. Idempotent and order-independent: a missing role or table is a no-op (the
minimal CI chain does not create every table).

Revision ID: 0101_svc_ingest_display_names
Revises: 0100_stream_dek_issuances
Create Date: 2026-11-12
"""

from __future__ import annotations

from alembic import op

revision = "0101_svc_ingest_display_names"
down_revision = "0100_stream_dek_issuances"
branch_labels = None
depends_on = None

_ROLE = "waddles_svc_ingest"

#: table -> columns granted SELECT. Fixed literals, never user input.
GRANTS: dict[str, tuple[str, ...]] = {
    "hub_users": ("id", "uuid", "display_name"),
    "community_members": ("id", "community_id", "user_uuid", "user_id", "display_name"),
    "communities": ("id", "tenant_id"),
    "ephemeral_pseudonyms": ("tenant_id", "pseudonym", "handle"),
}


def _guarded(table: str, body: str) -> str:
    """Run `body` only if both the role and `table` exist (fresh-replay / order safe)."""
    return (
        "DO $$ BEGIN\n"  # nosec B608 -- fixed literals only
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ROLE}')\n"
        f"     AND to_regclass('public.{table}') IS NOT NULL THEN\n"
        f"    {body}\n"
        "  END IF;\n"
        "END $$;"
    )


def upgrade() -> None:
    """Grant column-scoped SELECT on the display-name columns to svc-ingest's role."""
    for table, cols in GRANTS.items():
        op.execute(
            _guarded(table, f"GRANT SELECT ({', '.join(cols)}) ON {table} TO {_ROLE};")
        )


def downgrade() -> None:
    """Revoke exactly the column grants this revision added (back to 0099's state)."""
    for table, cols in GRANTS.items():
        op.execute(
            _guarded(
                table, f"REVOKE SELECT ({', '.join(cols)}) ON {table} FROM {_ROLE};"
            )
        )
