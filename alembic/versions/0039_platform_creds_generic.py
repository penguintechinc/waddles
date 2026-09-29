"""Generalize `tenant_platform_credentials` beyond Discord (#500/#501, owner clarification).

**Owner clarification (this migration's own trigger):** every tenant other
than tenant 0 (global/default) ALWAYS requires its own app integration for
EVERY platform (Discord, Twitch, YouTube, Kick, Slack, Mattermost, Teams,
etc.), not only Discord and not only when a guild is shared. Migration
0038 already made this table `(tenant_id, platform)`-keyed with
`platform` free-form (`VARCHAR(50) DEFAULT 'discord'`), so storage was
already platform-agnostic in its key shape -- this migration removes the
two column-level assumptions that were still Discord-shaped:

1. `bot_token_ciphertext`/`bot_token_iv` were `NOT NULL` -- fine for
   Discord (every install has a bot token) but wrong for platforms whose
   app credential is client id/secret only (no separate "bot token"
   concept, e.g. a pure OAuth app). Relaxed to nullable; a platform with
   no bot-token concept simply never sets them.
2. No column existed for a platform-specific extra secret some
   connectors need beyond client id/secret/bot token (e.g. a Twitch
   EventSub webhook secret, a Slack signing secret). Adds
   `extra_secret_ciphertext`/`extra_secret_iv` (same ciphertext+IV shape
   as every other secret column here), nullable, unused by platforms that
   don't need it.

No change to `UNIQUE (tenant_id, platform)`, the global-tenant trigger, or
any other table -- this is a narrow, additive follow-up to 0038's schema,
not a redesign.

Revision ID: 0039_platform_creds_generic
Revises: 0038_guild_tenant_pairing
Create Date: 2026-09-29
"""

from __future__ import annotations

from alembic import op

revision = "0039_platform_creds_generic"
down_revision = "0038_guild_tenant_pairing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Relax `bot_token_*` to nullable; add nullable `extra_secret_ciphertext`/`extra_secret_iv`."""
    op.execute(
        "ALTER TABLE tenant_platform_credentials "
        "ALTER COLUMN bot_token_ciphertext DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE tenant_platform_credentials ALTER COLUMN bot_token_iv DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE tenant_platform_credentials "
        "ADD COLUMN IF NOT EXISTS extra_secret_ciphertext BYTEA"
    )
    op.execute(
        "ALTER TABLE tenant_platform_credentials ADD COLUMN IF NOT EXISTS extra_secret_iv BYTEA"
    )
    op.execute(
        "COMMENT ON COLUMN tenant_platform_credentials.extra_secret_ciphertext IS "
        "'Optional platform-specific extra secret (e.g. Twitch EventSub secret, Slack "
        "signing secret) -- NULL for platforms/installs that do not need one.'"
    )


def downgrade() -> None:
    """Drop `extra_secret_*`; does NOT re-impose `bot_token_*` `NOT NULL` -- see note below."""
    op.execute("ALTER TABLE tenant_platform_credentials DROP COLUMN IF EXISTS extra_secret_iv")
    op.execute(
        "ALTER TABLE tenant_platform_credentials DROP COLUMN IF EXISTS extra_secret_ciphertext"
    )
    # NOTE: re-imposing NOT NULL on bot_token_ciphertext/bot_token_iv on
    # downgrade is intentionally skipped -- any row written for a
    # no-bot-token platform while this migration was applied would break
    # a blind re-imposition. Operators downgrading past this revision
    # must first null-check/backfill those rows themselves.
