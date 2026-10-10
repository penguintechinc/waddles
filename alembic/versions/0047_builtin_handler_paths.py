"""Re-point stored stage entrypoints at the renamed `builtin_handlers` packages.

The native first-party stage handlers that run **inside** svc-ingest, svc-process
and svc-action used to live in `core/svc_{ingest,process,action}/bundles/`, a
name that read as if they were installable app bundles (they are not: they are
plain Python modules imported in-process, shipped with the service image). The
directories are now `core/svc_{ingest,process,action}/builtin_handlers/`.

The directory name is also the **Python import package**, and the package is
what `app_catalog.stages.<stage>.entrypoint` stores: a dotted
`<package>.<module>:<function>` path resolved by
`flask_core.stage_runner.load_entrypoint` via `importlib`. Renaming the package
without rewriting the stored entrypoints would make every built-in app fail to
load (`cannot import module 'bundles.<module>'`) -- so this revision rewrites
`bundles.<module>:<fn>` to `builtin_handlers.<module>:<fn>` for exactly the 45
modules that were renamed.

The package is `builtin_handlers`, not `builtins`: a package called `builtins`
can never be imported (`import builtins.x` fails with "'builtins' is not a
package" because Python's own `builtins` module always wins).

Design notes:

- Scope is an explicit module allowlist (`_MODULES`, frozen at the rename), not a
  blanket `bundles.%` prefix match: the installable Python (WASI) bundles are free
  to use a `bundles` package of their own, and this revision must never touch an
  entrypoint it does not own.
- Only the three stage entrypoints (`ingest`/`process`/`action`) are rewritten;
  every other key of `app_catalog.stages` (`config`, `spec`, `consumes`, ...) is
  preserved byte-for-byte by `jsonb_set`.
- Idempotent in both directions: the `WHERE` clause only matches rows still in the
  source form, so a re-run (or a row already rewritten by hand) is a no-op.
- The historical revisions that originally seeded `bundles.<module>` entrypoints
  (0007, 0009, 0014, 0016-0019 and the legacy SQL 071/082-094) are immutable
  history and are deliberately left untouched; a fresh-DB replay seeds the old form
  and this revision, applied last, converts it.

Revision ID: 0047_builtin_handler_paths
Revises: 0046_connector_pii_tenant_scope
Create Date: 2026-10-09
"""

from __future__ import annotations

from alembic import op

revision = "0047_builtin_handler_paths"
down_revision = "0046_connector_pii_tenant_scope"
branch_labels = None
depends_on = None

OLD_PACKAGE = "bundles"
NEW_PACKAGE = "builtin_handlers"

#: The three pipeline stages whose `app_catalog.stages.<stage>.entrypoint` can hold a
#: built-in handler path.
STAGES = ("ingest", "process", "action")

#: Every module that moved from `core/svc_*/bundles/` to `core/svc_*/builtin_handlers/`,
#: frozen at the rename. A module added later is born under `builtin_handlers` and never
#: needs rewriting, so this list must not grow.
_MODULES = (
    "bot_process",
    "community_announcements_action",
    "community_announcements_process",
    "community_chat_process",
    "community_context_process",
    "community_forums_action",
    "community_forums_process",
    "community_loyalty_action",
    "community_loyalty_process",
    "community_polls_action",
    "community_polls_process",
    "community_reputation_process",
    "discord_gateway_manifest",
    "discord_ingest",
    "discord_send_action",
    "echo_ingest",
    "echo_process",
    "integrations_waddleai_action",
    "inventory_process",
    "kick_gateway_manifest",
    "kick_ingest",
    "kick_send_action",
    "marketing_engagement_action",
    "marketing_engagement_process",
    "moderation_enforce_action",
    "slack_gateway_manifest",
    "slack_ingest",
    "slack_send_action",
    "social_alias_action",
    "social_alias_process",
    "social_music_action",
    "social_music_process",
    "social_quote_action",
    "social_quote_process",
    "social_shoutout_process",
    "social_welcome_action",
    "social_welcome_process",
    "streaming_stream_action",
    "twitch_eventsub_ingest",
    "twitch_gateway_manifest",
    "twitch_ingest",
    "twitch_send_action",
    "twitch_shoutout_action",
    "youtube_live_ingest",
    "youtube_send_action",
)


def _rewrite_sql(stage: str, old_package: str, new_package: str) -> str:
    """Build the `UPDATE` that swaps `old_package` for `new_package` in one stage's entrypoint.

    Matches only `^<old_package>.<module>:` for a module in `_MODULES` (the trailing
    colon pins the match to a whole module name, so `bot_process` never matches a
    hypothetical `bot_process_v2`), then replaces just the package prefix.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
    modules = "|".join(_MODULES)
    path = "{" + f"{stage},entrypoint" + "}"
    return f"""
        UPDATE app_catalog
        SET stages = jsonb_set(
            stages,
            '{path}',
            to_jsonb(regexp_replace(
                stages #>> '{path}',
                '^{old_package}\\.',
                '{new_package}.'
            ))
        )
        WHERE stages #>> '{path}' ~ '^{old_package}\\.({modules}):'
        """


def upgrade() -> None:
    """Rewrite `bundles.<module>:<fn>` entrypoints to `builtin_handlers.<module>:<fn>`."""
    for stage in STAGES:
        op.execute(_rewrite_sql(stage, OLD_PACKAGE, NEW_PACKAGE))


def downgrade() -> None:
    """Restore the pre-rename `bundles.<module>:<fn>` entrypoints."""
    for stage in STAGES:
        op.execute(_rewrite_sql(stage, NEW_PACKAGE, OLD_PACKAGE))
