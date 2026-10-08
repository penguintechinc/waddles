"""Bar Citizen: N:M guild<->tenant pairing, platform-generic tenant credentials, Twitch<->Discord role sync bindings.

**Serialization anchor for the Bar Citizen workstream (owner-confirmed
objectives, 2026-10-03).** Supersedes the schema halves of the parked,
stale PRs #501 (`0038_guild_tenant_pairing`, forked off dead branch
`0030_bundle_app_schemas`) and #504 (`0039_platform_creds_generic`,
forked off #501's own dead 0038) -- neither is reachable from today's
head (`0033_artifact_digest_not_unique`) and both predate this session's
owner-confirmed scope narrowing (subscriber tiers + moderators only,
admin-chosen sync direction, per-community role-name prefix). This is a
fresh migration against the live head, not a rebase of either parked
branch; their `tenant_platform_credentials` / `guild_tenant_pairings`
table designs are reused as reference where they still fit, reusing
column names/comments verified against a real Postgres 17 container,
but the role-sync shape (`community_role_sync_bindings`, new in this
migration) and the credential blob shape (single encrypted-JSON column
via `hub_api/services/platform_integrations_crypto.py`'s wire format,
not #501/#504's split ciphertext/iv columns) are both new designs
scoped to this session's confirmed requirements, not carried over.

**`hub_users` has no uuid column on this branch** -- every actor FK here
is `INTEGER REFERENCES hub_users(id)`, matching 0023/0025's own actor-FK
convention (`approved_by`, `granted_by`, `installed_by_user_id` precedent).
The parked PRs' plan to repoint actor FKs at a `hub_users.uuid` column
(PR #434, never merged) does not apply here and is not assumed.

Tables:

- `tenant_platform_credentials` -- one row per `(tenant_id, platform)`
  holding that tenant's own platform app credentials. **Platform-generic
  from day one** (owner requirement): every tenant beyond tenant 0
  (`tenants.is_global = TRUE`) brings its own app credentials for EVERY
  platform (Discord, Twitch, and whatever follows) -- there is no
  Discord-shaped column here that a future platform's credential shape
  wouldn't fit, unlike #501's original Discord-only columns that #504
  had to retroactively relax. A single `credentials_ciphertext` column
  holds a JSON payload (shape is platform-defined by convention, e.g.
  `{"client_id": ..., "client_secret": ..., "bot_token": ..., "extra": {...}}`)
  encrypted as one AES-256-GCM blob via `platform_integrations_crypto.
  encrypt_token()`'s wire format (base64(iv(12) || ciphertext || tag) --
  same primitive and layout `platform_integrations.access_token`
  already uses, migration 030 of `config/postgres/migrations/`), not a
  split ciphertext/iv column pair. `key_ref` is reserved, nullable, for
  a future per-tenant DEK broker (no such broker exists yet on this
  branch -- left as a placeholder the application layer does not
  currently read, same caveat #501's own docstring gave for its
  `key_ref`). Tenant 0 (global) never gets a row here -- it uses the
  cluster-managed SaaS credentials -- enforced by a `BEFORE INSERT/
  UPDATE` trigger, not just documentation, matching #501's own
  `reject_global_tenant_credentials()` design (reused verbatim here;
  verified against a real Postgres container).

- `guild_tenant_pairings` -- N:M: a Discord guild may be paired with
  more than one tenant/community (owner requirement). Every pairing is
  **opt-in** (`sync_enabled`, default FALSE) and the admin chooses the
  sync `direction` (`discord_to_twitch` / `twitch_to_discord` /
  `bidirectional`) per pairing. `role_name_prefix` is the per-community
  Discord role-name prefix (e.g. `[BCSEA]`) so two communities sharing a
  guild don't collide on role names -- a human-readable convention only,
  never the authorization mechanism (role ownership, if/when a
  `managed_roles`-style registry is built, resolves by Discord role ID,
  never by name -- see the Known Limitation note below). `UNIQUE
  (community_id, discord_guild_id)` -- one pairing per community per
  guild; a guild may have many such rows (one per paired community), a
  community may pair with many guilds.

- `community_role_sync_bindings` -- maps one pairing's platform-role
  concept to a Discord role id. `sync_scope` is `subscriber_tier` (with
  `subscriber_tier` set to 1/2/3, T1/T2/T3 per the owner-confirmed
  design doc) or `moderator` (`subscriber_tier` NULL) -- VIP and
  follower are explicitly out of v3.0 scope per the same doc, so no
  third scope value exists. A partial unique index caps each pairing to
  at most one binding per subscriber tier and at most one moderator
  binding, so "tier 2 -> role X" and "moderator -> role Y" coexist under
  the same pairing without colliding. `discord_role_id` is the external
  Discord role id (never a role name) -- the one piece of "resolve by
  ID, never by name" discipline this migration's scope actually needs;
  a full ownership/anti-spoofing registry (`managed_roles`) is
  deliberately deferred, see below.

**Known limitation / deliberately deferred (`managed_roles`).** The
parked PR #501 design included a `managed_roles` ownership registry
(one owning community per external Discord role id per guild, with an
adopt/approve workflow in #503's follow-up). This migration does NOT
include it: without the consuming sync-engine/API service layer (also
not in this schema-only migration), a lifecycle/approval table would be
pure speculation about columns a future service might not actually need
in this shape -- the exact mistake that made #501/#503/#504 fork into
three incompatible migrations off the same base. `community_role_sync_
bindings.discord_role_id` suffices for binding a role concept to a role
ID; a dedicated ownership/anti-spoofing/approval registry is left for
the follow-on PR that actually implements the sync engine, informed by
#501/#503's design once it's rebased against this migration instead of
a dead branch.

RBAC: `hub_api` is the sole writer for all three tables (every other
role gets an explicit empty-privilege row per spec D28's "every role x
table has an explicit row" convention, `config/postgres/rbac-matrix.
yaml`) -- these are hub-api-owned control-plane tables; no data-plane
service reads them directly on this branch (reflects the current state,
not a permanent restriction).

Revision ID: 0034_bar_citizen_guild_pairing
Revises: 0033_artifact_digest_not_unique
Create Date: 2026-10-03
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0034_bar_citizen_guild_pairing"
down_revision = "0033_artifact_digest_not_unique"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset(
    {
        "tenant_platform_credentials",
        "guild_tenant_pairings",
        "community_role_sync_bindings",
    }
)


def _load_matrix_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0034", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0034"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def upgrade() -> None:
    # Defensive: `tenants.is_global` already exists in every real deployment
    # (`config/postgres/migrations/058_tenants_and_claims.sql`), but
    # `alembic/tests/pg_docker.py`'s minimal real-Postgres test harness
    # bootstraps a bare `tenants(id, slug, is_active, logo_url, config)` and
    # replays only migrations 0020+ -- it predates 058 entirely. This
    # migration's own trigger (below) is the first one in the 0020+ chain
    # to read `tenants.is_global`, so it's added here, idempotently, rather
    # than widening the shared test bootstrap for one migration's need.
    op.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_global BOOLEAN NOT NULL DEFAULT FALSE"
    )

    # -- tenant_platform_credentials -------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_platform_credentials (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            platform VARCHAR(50) NOT NULL,
            credentials_ciphertext TEXT NOT NULL,
            key_ref VARCHAR(255),
            installed_by_user_id INTEGER REFERENCES hub_users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (tenant_id, platform)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE tenant_platform_credentials IS "
        "'Platform-generic per-tenant app credentials (tenant 0/global excluded -- "
        "uses SaaS creds, enforced by trigger below). credentials_ciphertext is a "
        "single AES-256-GCM blob (platform_integrations_crypto.encrypt_token() wire "
        "format) of a platform-defined JSON payload -- never split ciphertext/iv columns.'"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_global_tenant_credentials() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM tenants WHERE id = NEW.tenant_id AND is_global = TRUE) THEN
                RAISE EXCEPTION
                    'tenant_platform_credentials: tenant % is the global tenant and must use '
                    'the platform''s own SaaS credentials, not a DB-stored app',
                    NEW.tenant_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_reject_global_tenant_credentials ON tenant_platform_credentials;
        CREATE TRIGGER trg_reject_global_tenant_credentials
            BEFORE INSERT OR UPDATE ON tenant_platform_credentials
            FOR EACH ROW EXECUTE FUNCTION reject_global_tenant_credentials()
        """
    )

    # -- guild_tenant_pairings --------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_tenant_pairings (
            id BIGSERIAL PRIMARY KEY,
            community_id INTEGER NOT NULL REFERENCES communities(id),
            discord_guild_id VARCHAR(255) NOT NULL,
            direction VARCHAR(20) NOT NULL
                CHECK (direction IN ('discord_to_twitch', 'twitch_to_discord', 'bidirectional')),
            sync_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            role_name_prefix VARCHAR(50) NOT NULL,
            created_by_user_id INTEGER REFERENCES hub_users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (community_id, discord_guild_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE guild_tenant_pairings IS "
        "'N:M Discord guild <-> community pairing. Opt-in only (sync_enabled default "
        "FALSE); admin chooses direction per pairing. role_name_prefix disambiguates "
        "role names when >1 community shares a guild -- a human-readable convention "
        "only, never the authorization mechanism.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_guild_tenant_pairings_guild "
        "ON guild_tenant_pairings (discord_guild_id)"
    )

    # -- community_role_sync_bindings --------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS community_role_sync_bindings (
            id BIGSERIAL PRIMARY KEY,
            pairing_id BIGINT NOT NULL REFERENCES guild_tenant_pairings(id) ON DELETE CASCADE,
            sync_scope VARCHAR(20) NOT NULL
                CHECK (sync_scope IN ('subscriber_tier', 'moderator')),
            subscriber_tier SMALLINT
                CHECK (subscriber_tier IS NULL OR subscriber_tier IN (1, 2, 3)),
            discord_role_id VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_role_sync_binding_scope_tier CHECK (
                (sync_scope = 'subscriber_tier' AND subscriber_tier IS NOT NULL)
                OR (sync_scope = 'moderator' AND subscriber_tier IS NULL)
            )
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE community_role_sync_bindings IS "
        "'Per-pairing role-concept -> Discord role id map. sync_scope=subscriber_tier "
        "(subscriber_tier 1/2/3, Twitch sub tiers are read-only so this direction is "
        "always twitch_to_discord in practice) or sync_scope=moderator (subscriber_tier "
        "NULL). VIP/follower are out of v3.0 scope -- no third sync_scope value.'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_role_sync_binding_tier "
        "ON community_role_sync_bindings (pairing_id, subscriber_tier) "
        "WHERE sync_scope = 'subscriber_tier'"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_role_sync_binding_moderator "
        "ON community_role_sync_bindings (pairing_id) "
        "WHERE sync_scope = 'moderator'"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute(
        "GRANT USAGE ON SEQUENCE tenant_platform_credentials_id_seq, "
        "guild_tenant_pairings_id_seq, community_role_sync_bindings_id_seq TO hub_api;"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS community_role_sync_bindings")
    op.execute("DROP TABLE IF EXISTS guild_tenant_pairings")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_reject_global_tenant_credentials ON tenant_platform_credentials"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_global_tenant_credentials()")
    op.execute("DROP TABLE IF EXISTS tenant_platform_credentials")
