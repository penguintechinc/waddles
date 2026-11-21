"""Register + tenant-wide activate the moderation-enforcement action bundle (gh-304).

`core/svc_process/services/moderation_gate.py` (gh-304, this same chunk)
stamps `event.payload["moderation_enforcement"]` +
`flask_core.PROCESS_TARGET_APP_ID_KEY` onto a matched message when the
community has opted the category into enforcement (not just
classification) -- but without a corresponding `app_catalog` row,
svc_process/svc_action never subscribe to `waddles.community.moderation.
default`'s own `:process`/`:action` Valkey keys and the routed envelope is
never actually dispatched. Mirrors `0009_music_catalog`'s exact shape
(tenant-wide activation via `app_tenant_availability`, not per-community
`app_activations`) -- verified against the LIVE `waddles.social.music.
default` catalog row (`kubectl exec` psql, read-only) to match its column
list and `stages` JSON shape exactly.

Action-only (no `process` stage): the moderation gate itself -- not a
catalog-registered process bundle -- is what decides whether to route a
message here (`services/moderation_gate.py`'s own docstring); this app_id
exists purely so `bundles.moderation_enforce_action:enforce` is reachable
as an action-stage entrypoint once routed, mirroring `093_streaming_stream
_bundle`'s own action-only precedent (ported by `0014_wave1a_bundle_seeds`).

`config_defaults.bot_token_ref` is set directly in this same migration's
`app_tenant_availability` INSERT (unlike music, which needed a follow-up
`0010_music_bot_token_ref` migration because `0009` never set it) --
reuses the same shared Discord bot connection/secret (`DISCORD_BOT_TOKEN`)
already live for `penguintechinc.waddles.discord-pipeline`/`waddles.social.music.
default`'s own `global`-tenant rows, since `moderation_enforce_action.py`'s
Discord warn/timeout calls are sent by that same bot, not a per-community
one.

Both INSERTs are idempotent upserts (`ON CONFLICT ... DO UPDATE`) so a
partially-seeded or drifted row self-heals back to this migration's exact
`stages`/`config_defaults` on re-run, matching `0014_wave1a_bundle_seeds`'s
own `app_catalog` upsert convention -- extended here to
`app_tenant_availability` too since, unlike 0014's nine bundles, this is a
brand-new app_id with no pre-existing admin-set `config_defaults` state a
`DO UPDATE` could clobber.

Revision ID: 0016_moderation_enforce_app
Revises: 0015_loyalty_core_tables
Create Date: 2026-09-11
"""

from alembic import op

revision = "0016_moderation_enforce_app"
down_revision = "0015_loyalty_core_tables"
branch_labels = None
depends_on = None

APP_ID = "penguintechinc.waddles.moderation-pipeline"
TENANT_SLUG = "global"
BOT_TOKEN_REF = "DISCORD_BOT_TOKEN"


def upgrade() -> None:
    # Static seed data -- no user/request input, so literals are embedded
    # directly (matching 0009_music_catalog's own rationale: `alembic
    # upgrade --sql`'s offline literal_binds renderer can silently emit
    # NULL for a bind param cast into `::jsonb`). The full `(... || ...)
    # ::jsonb` wrap is required even for this shorter, single-stage blob --
    # a bare `... || '...'::jsonb` casts ONLY the last literal, not the
    # full concatenation (the exact regression 0014_wave1a_bundle_seeds'
    # own test suite guards against).
    op.execute(
        f"""
        INSERT INTO app_catalog (
            app_id, manifest_version, module, feature, provider,
            execution_model, is_default, platform_compatibility,
            status, stages
        ) VALUES (
            '{APP_ID}',
            '1.0.0',
            'community',
            'waddles.community.moderation',
            'builtin',
            'native',
            FALSE,
            '{{"tested_with": "release/v3.0.X", "min_version": null, "max_version": null}}'::jsonb,
            'active',
            (
                '{{"action": {{"entrypoint": "bundles.moderation_enforce_action:enforce", ' ||
                '"config": {{"api_base": "https://discord.com/api/v10"}}, "spec": ' ||
                '{{"required_config": ["bot_token_ref"]}}}}}}'
            )::jsonb
        )
        ON CONFLICT (app_id) DO UPDATE SET
            manifest_version = EXCLUDED.manifest_version,
            module = EXCLUDED.module,
            feature = EXCLUDED.feature,
            provider = EXCLUDED.provider,
            execution_model = EXCLUDED.execution_model,
            is_default = EXCLUDED.is_default,
            platform_compatibility = EXCLUDED.platform_compatibility,
            status = EXCLUDED.status,
            stages = EXCLUDED.stages
        """
    )

    # Activate for the global tenant (all communities), same shape 0009
    # (music), 0014 (wave1a) use -- with config_defaults.bot_token_ref set
    # directly here (see module docstring for why this app_id doesn't need
    # a follow-up migration the way music's 0010 did). Merge (`||` over a
    # COALESCEd existing value), never a bare overwrite, so re-running this
    # migration can never clobber a config_defaults key an admin already
    # hand-set for this row.
    op.execute(
        f"""
        INSERT INTO app_tenant_availability (tenant_id, app_id, available, config_defaults)
        SELECT t.id, '{APP_ID}', TRUE, '{{"bot_token_ref": "{BOT_TOKEN_REF}"}}'::jsonb
        FROM tenants t
        WHERE t.slug = '{TENANT_SLUG}'
        ON CONFLICT (tenant_id, app_id) DO UPDATE SET
            config_defaults = COALESCE(app_tenant_availability.config_defaults, '{{}}'::jsonb)
                || EXCLUDED.config_defaults
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DELETE FROM app_tenant_availability
        WHERE app_id = '{APP_ID}'
          AND tenant_id IN (SELECT id FROM tenants WHERE slug = '{TENANT_SLUG}')
        """
    )
    op.execute(f"DELETE FROM app_catalog WHERE app_id = '{APP_ID}'")
