"""`discord_guild_tenants`: a Discord guild belongs to exactly ONE tenant (DB-enforced).

`guild_tenant_pairings` is N:M between a guild and *communities*, and that stays true WITHIN a
tenant (two communities of one tenant may pair the same guild). Across tenants it must not be:
a guild paired into two tenants lets each tenant's role-sync read and write the other's members.

`discord_guild_id` is the PRIMARY KEY here, so claiming a guild is a single
`INSERT ... ON CONFLICT DO NOTHING RETURNING` -- of two tenants racing for the same guild exactly
one row is inserted (mirrors `app_id_owners`, 0136). `services/guild_pairing.py::create_pairing`
makes the claim in the same transaction as the pairing insert and refuses a claim held by a
different tenant (409); deleting a tenant's LAST pairing for the guild releases the claim with a
guarded `DELETE ... WHERE NOT EXISTS`.

**Backfill.** Existing pairings seed the claims (one row per guild, owning tenant taken from the
paired communities). If any guild is ALREADY paired across two tenants the migration FAILS
LOUDLY and changes nothing -- the operator must resolve the data first; it is never guessed.

Revision ID: 0137_discord_guild_tenants
Revises: 0136_app_id_owners
Create Date: 2026-11-20
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0137_discord_guild_tenants"
down_revision = "0136_app_id_owners"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
)
_MATRIX_TABLES = frozenset({"discord_guild_tenants"})

_CROSS_TENANT_SQL = """
SELECT p.discord_guild_id, array_agg(DISTINCT c.tenant_id ORDER BY c.tenant_id) AS tenants
FROM guild_tenant_pairings p
JOIN communities c ON c.id = p.community_id
GROUP BY p.discord_guild_id
HAVING count(DISTINCT c.tenant_id) > 1
ORDER BY p.discord_guild_id
"""

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS discord_guild_tenants (
    discord_guild_id VARCHAR(255) PRIMARY KEY,
    tenant_id        INTEGER      NOT NULL REFERENCES tenants(id),
    claimed_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    claimed_by_uuid  UUID
)
"""

_BACKFILL_SQL = """
INSERT INTO discord_guild_tenants (discord_guild_id, tenant_id)
SELECT p.discord_guild_id, min(c.tenant_id)
FROM guild_tenant_pairings p
JOIN communities c ON c.id = p.community_id
GROUP BY p.discord_guild_id
ON CONFLICT DO NOTHING
"""


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "waddles_rbac_matrix_0137", _MATRIX_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0137"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def find_cross_tenant_guilds(bind: sa.engine.Connection) -> list[tuple[str, list[int]]]:
    """Guilds already paired into more than one tenant: `[(guild_id, [tenant ids])]`."""
    return [
        (str(r[0]), [int(t) for t in r[1]])
        for r in bind.execute(sa.text(_CROSS_TENANT_SQL))
    ]


def upgrade() -> None:
    """Refuse cross-tenant data, create the claim table, backfill it, apply the RBAC matrix."""
    conflicts = find_cross_tenant_guilds(op.get_bind())
    if conflicts:
        detail = "; ".join(f"guild {g} in tenants {t}" for g, t in conflicts)
        raise RuntimeError(
            "0137_discord_guild_tenants: refusing to migrate, existing guild_tenant_pairings pair "
            f"a Discord guild into more than one tenant ({detail}). Resolve (delete the pairings "
            "of the tenant that does not own the guild) and re-run."
        )
    op.execute(_CREATE_SQL)
    op.execute(
        "COMMENT ON TABLE discord_guild_tenants IS "
        "'One row per Discord guild: the single tenant that may pair it. First claim wins "
        "(INSERT ... ON CONFLICT DO NOTHING RETURNING); released when the tenant''s last pairing "
        "for the guild is deleted. claimed_by_uuid is a hub_users.uuid token, never a name.'"
    )
    op.execute(_BACKFILL_SQL)

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get(
        "RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH)
    )
    rows = matrix_module.load_matrix(matrix_path)
    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)


def downgrade() -> None:
    """Drop `discord_guild_tenants` (the pairings themselves are untouched)."""
    op.execute("DROP TABLE IF EXISTS discord_guild_tenants")
