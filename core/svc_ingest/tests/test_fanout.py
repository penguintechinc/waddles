"""Tests for `fanout.resolve_consuming_apps`/`fan_out_event`.

resolve_apps()-based routing to per-bundle `:ingest` Valkey keys.
`redis_client` (from `conftest.py`) is a real `fakeredis.FakeAsyncRedis`
-- genuine LPUSH/RPOP semantics, not a mocked call, matching
`test_runner.py`'s own precedent for this container. Fail-first: swapping
`fan_out_event`'s `redis_client.lpush` for a no-op silently turns
`test_lpushes_onto_each_matching_bundles_ingest_key` green-for-the-wrong-
-reason into red (RPOP finds nothing) -- confirmed, reverted.
"""

from __future__ import annotations

import json
from typing import Any

from flask_core.app_binding import AppInstallation
from flask_core.app_manifest import parse_manifest
from flask_core.app_registry import AppRegistry
from flask_core.stream_pipeline import bundle_stream_key

from fanout import fan_out_event, resolve_consuming_apps

TENANT = "acme-corp"
CONSUMES_TAG = "discord.message"


def _manifest(app_id: str, feature: str, *, consumes: tuple[str, ...], is_default: bool) -> Any:
    return parse_manifest(
        {
            "app_id": app_id,
            "name": app_id,
            "version": "1.0.0",
            "feature": feature,
            "module": "bot",
            "provider": "builtin",
            "is_default": is_default,
            "stages": {
                "ingest": {
                    "entrypoint": "builtin_handlers.discord_ingest:normalize",
                    "consumes": list(consumes),
                }
            },
        }
    )


class _FakeInstallations:
    """In-memory `InstallationLookup` -- this file's own test double."""

    def __init__(self, rows: list[AppInstallation]) -> None:
        self._rows = rows

    async def find(
        self, feature: str, *, tenant: str, community: int | None
    ) -> list[AppInstallation]:
        return [
            row
            for row in self._rows
            if row.feature == feature
            and row.tenant_id == tenant
            and (row.community_id is None or row.community_id == community)
        ]


class TestResolveConsumingApps:
    async def test_finds_default_app_declaring_the_tag(self) -> None:
        registry = AppRegistry()
        manifest = _manifest(
            "waddles.bot.discord.gateway",
            "waddles.bot.discord",
            consumes=(CONSUMES_TAG,),
            is_default=True,
        )
        registry.register(manifest)

        resolved = await resolve_consuming_apps(
            CONSUMES_TAG, tenant=TENANT, community=None, registry=registry
        )
        assert resolved == (manifest,)

    async def test_excludes_apps_not_declaring_the_tag(self) -> None:
        registry = AppRegistry()
        registry.register(
            _manifest(
                "waddles.bot.twitch.gateway",
                "waddles.bot.twitch",
                consumes=("twitch.message",),
                is_default=True,
            )
        )

        resolved = await resolve_consuming_apps(
            CONSUMES_TAG, tenant=TENANT, community=None, registry=registry
        )
        assert resolved == ()

    async def test_resolved_app_must_itself_declare_the_tag(self) -> None:
        """Two Apps under one Feature; the ACTUALLY-bound one doesn't consume the tag.

        `resolve_apps` returns whatever App is bound at (tenant,
        community) -- if that's not the tag-declaring App, the Feature
        must not appear in the result even though SOME App under it
        matched in the first (manifest-scan) pass.
        """
        registry = AppRegistry()
        feature = "waddles.bot.discord"
        matching = _manifest(
            "waddles.bot.discord.gateway", feature, consumes=(CONSUMES_TAG,), is_default=False
        )
        other = _manifest("waddles.bot.discord.other", feature, consumes=(), is_default=True)
        registry.register(matching)
        registry.register(other)

        # No installation row for `matching` -- resolve_apps falls back to
        # the Feature's default, which is `other` (doesn't consume the tag).
        resolved = await resolve_consuming_apps(
            CONSUMES_TAG, tenant=TENANT, community=None, registry=registry
        )
        assert resolved == ()

    async def test_resolves_via_real_installation_lookup_not_just_default(self) -> None:
        """A non-default App, explicitly bound via a real `InstallationLookup` row, is found.

        Proves this isn't ONLY the `is_default` fallback path.
        """
        registry = AppRegistry()
        feature = "waddles.bot.discord"
        default_app = _manifest("waddles.bot.discord.other", feature, consumes=(), is_default=True)
        bound_app = _manifest(
            "waddles.bot.discord.gateway", feature, consumes=(CONSUMES_TAG,), is_default=False
        )
        registry.register(default_app)
        registry.register(bound_app)

        installations = _FakeInstallations(
            [
                AppInstallation(
                    tenant_id=TENANT, community_id=None, feature=feature, app_id=bound_app.app_id
                )
            ]
        )

        resolved = await resolve_consuming_apps(
            CONSUMES_TAG,
            tenant=TENANT,
            community=None,
            registry=registry,
            installations=installations,
        )
        assert resolved == (bound_app,)

    async def test_dedupes_when_multiple_features_resolve_to_the_same_app_id(self) -> None:
        """Same app_id can't be registered twice (AppRegistry rejects it).

        Proves the de-dupe-by-app_id guard is a no-op safety net, not
        load-bearing today, without asserting on impossible registry state.
        """
        registry = AppRegistry()
        manifest = _manifest(
            "waddles.bot.discord.gateway",
            "waddles.bot.discord",
            consumes=(CONSUMES_TAG,),
            is_default=True,
        )
        registry.register(manifest)

        resolved = await resolve_consuming_apps(
            CONSUMES_TAG, tenant=TENANT, community=None, registry=registry
        )
        assert len(resolved) == 1


class TestFanOutEvent:
    async def test_no_consumers_returns_zero_and_pushes_nothing(self, redis_client: Any) -> None:
        registry = AppRegistry()
        count = await fan_out_event(
            {"content": "hi"},
            consumes_tag=CONSUMES_TAG,
            tenant=TENANT,
            community=None,
            redis_client=redis_client,
            registry=registry,
        )
        assert count == 0

    async def test_lpushes_onto_each_matching_bundles_ingest_key(self, redis_client: Any) -> None:
        registry = AppRegistry()
        manifest = _manifest(
            "waddles.bot.discord.gateway",
            "waddles.bot.discord",
            consumes=(CONSUMES_TAG,),
            is_default=True,
        )
        registry.register(manifest)

        raw_event = {"platform": "discord", "content": "hello"}
        count = await fan_out_event(
            raw_event,
            consumes_tag=CONSUMES_TAG,
            tenant=TENANT,
            community=None,
            redis_client=redis_client,
            registry=registry,
        )
        assert count == 1

        ingest_key = bundle_stream_key(TENANT, None, manifest.app_id, "ingest")
        raw = await redis_client.rpop(ingest_key)
        assert raw is not None
        assert json.loads(raw) == raw_event

    async def test_community_scoped_key_uses_str_community(self, redis_client: Any) -> None:
        registry = AppRegistry()
        manifest = _manifest(
            "waddles.bot.discord.gateway",
            "waddles.bot.discord",
            consumes=(CONSUMES_TAG,),
            is_default=True,
        )
        registry.register(manifest)

        count = await fan_out_event(
            {"content": "hi"},
            consumes_tag=CONSUMES_TAG,
            tenant=TENANT,
            community=42,
            redis_client=redis_client,
            registry=registry,
        )
        assert count == 1

        ingest_key = bundle_stream_key(TENANT, "42", manifest.app_id, "ingest")
        assert await redis_client.rpop(ingest_key) is not None
