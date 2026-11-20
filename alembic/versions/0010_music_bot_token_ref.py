"""Populate the music app's missing `bot_token_ref` in `config_defaults`.

0009_music_catalog activated `penguintechinc.waddles.music-pipeline` tenant-wide via
`app_tenant_availability` but only ever set `(tenant_id, app_id, available)`
-- `config_defaults` was left at its column default (`{}`), so
`social_music_action.enqueue_song_request`'s Discord reply-in-place send
always hit `config.get("bot_token_ref")` as `None` and raised
`NonRetryableTransportError("social music bundle config missing required
'bot_token_ref'")` on every `!sr`/`!songrequest`.

Per `distribution_service.py`'s merge (`{**stage_data.config,
**app_tenant_availability.config_defaults}`), `config_defaults` -- not
`app_catalog.stages.action.config` -- is the correct layer for a
per-activation value like `bot_token_ref`: `082_discord_send_action_bundle
.sql` and `085_social_quote_bundle.sql`'s own docstrings are explicit that
`bot_token_ref`/`channel_id` are "per-activation config ... never seeded"
into `app_catalog`. This mirrors the live value already set for
`penguintechinc.waddles.discord-pipeline`'s own `global`-tenant `config_defaults` row
(`{"bot_token_ref": "DISCORD_BOT_TOKEN"}`) -- the same shared Discord bot
connection/secret (`k8s/helm/waddlebot/templates/secrets.yaml`'s single
`DISCORD_BOT_TOKEN`, wired into svc-action's env), since music's reply is
sent by that same bot, not a per-community one.

Idempotent UPDATE (not a fresh INSERT..ON CONFLICT) since 0009 already
applied here and elsewhere with `config_defaults` defaulted to `{}` --
merges the key in rather than clobbering any `config_defaults` an admin
may have already set by hand.

Revision ID: 0010_music_bot_token_ref
Revises: 0009_music_catalog
Create Date: 2026-09-10
"""

from alembic import op

revision = "0010_music_bot_token_ref"
down_revision = "0009_music_catalog"
branch_labels = None
depends_on = None

APP_ID = "penguintechinc.waddles.music-pipeline"
TENANT_SLUG = "global"
BOT_TOKEN_REF = "DISCORD_BOT_TOKEN"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE app_tenant_availability ata
        SET config_defaults = COALESCE(ata.config_defaults, '{{}}'::jsonb)
            || '{{"bot_token_ref": "{BOT_TOKEN_REF}"}}'::jsonb
        FROM tenants t
        WHERE ata.tenant_id = t.id
          AND t.slug = '{TENANT_SLUG}'
          AND ata.app_id = '{APP_ID}'
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE app_tenant_availability ata
        SET config_defaults = (COALESCE(ata.config_defaults, '{{}}'::jsonb) - 'bot_token_ref')
        FROM tenants t
        WHERE ata.tenant_id = t.id
          AND t.slug = '{TENANT_SLUG}'
          AND ata.app_id = '{APP_ID}'
        """
    )
