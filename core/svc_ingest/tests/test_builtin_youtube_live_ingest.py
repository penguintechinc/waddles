"""Tests for `builtin_handlers.youtube_live_ingest` -- `normalize()` + seeded in-process manifest.

Combines what Twitch's tests split across `test_builtin_twitch_ingest.py`
and `test_builtin_twitch_gateway_manifest.py`, matching this repo's own
`builtin_handlers/youtube_live_ingest.py` combining both concerns into one module
(see that module's own docstring for why).
"""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent
from flask_core.app_registry import AppRegistry

from builtin_handlers.youtube_live_ingest import (
    CONSUMES_TAG,
    YOUTUBE_LIVE_MANIFEST,
    normalize,
    register_default_bundles,
)


class TestNormalize:
    async def test_normalizes_a_real_chat_message(self) -> None:
        raw = {
            "platform": "youtube",
            "channel_id": "UCabc123",
            "video_id": "vid123",
            "live_chat_id": "chat123",
            "author_id": "UCviewer1",
            "display_name": "Alice",
            "is_mod": False,
            "is_owner": False,
            "is_sponsor": False,
            "text": "  hello chat  ",
            "message_id": "msg-1",
            "published_at": "2026-09-11T00:00:00Z",
        }
        event = await normalize(raw)

        assert isinstance(event, PlatformEvent)
        assert event.platform == "youtube"
        assert event.event_type == "message"
        assert event.actor == "UCviewer1"
        assert event.payload["text"] == "hello chat"
        assert event.payload["video_id"] == "vid123"
        assert event.payload["live_chat_id"] == "chat123"
        assert event.payload["author_id"] == "UCviewer1"
        assert event.payload["display_name"] == "Alice"
        assert event.payload["is_mod"] is False
        assert event.payload["is_owner"] is False
        assert event.payload["is_sponsor"] is False
        assert event.payload["message_id"] == "msg-1"
        assert event.payload["published_at"] == "2026-09-11T00:00:00Z"
        assert event.occurred_at == "2026-09-11T00:00:00Z"

    async def test_falls_back_to_unknown_when_author_id_missing(self) -> None:
        raw = {"live_chat_id": "chat123", "text": "hi"}
        event = await normalize(raw)
        assert event.actor == "unknown"

    async def test_missing_text_raises(self) -> None:
        with pytest.raises(ValueError, match="text"):
            await normalize({"live_chat_id": "chat123"})

    async def test_missing_live_chat_id_raises(self) -> None:
        with pytest.raises(ValueError, match="live_chat_id"):
            await normalize({"text": "hi"})

    async def test_blank_text_raises(self) -> None:
        with pytest.raises(ValueError, match="text"):
            await normalize({"live_chat_id": "chat123", "text": ""})

    async def test_occurred_at_falls_back_to_published_at(self) -> None:
        raw = {"live_chat_id": "chat123", "text": "hi", "published_at": "2026-01-01T00:00:00Z"}
        event = await normalize(raw)
        assert event.occurred_at == "2026-01-01T00:00:00Z"

    async def test_explicit_occurred_at_overrides_published_at(self) -> None:
        raw = {
            "live_chat_id": "chat123",
            "text": "hi",
            "published_at": "2026-01-01T00:00:00Z",
            "occurred_at": "2026-02-02T00:00:00Z",
        }
        event = await normalize(raw)
        assert event.occurred_at == "2026-02-02T00:00:00Z"

    async def test_occurred_at_defaults_to_now_when_neither_present(self) -> None:
        raw = {"live_chat_id": "chat123", "text": "hi"}
        event = await normalize(raw)
        assert event.occurred_at

    async def test_missing_optional_fields_default_absent_never_raise(self) -> None:
        raw = {"live_chat_id": "chat123", "text": "hi"}
        event = await normalize(raw)

        assert event.payload["video_id"] is None
        assert event.payload["author_id"] is None
        assert event.payload["display_name"] is None
        assert event.payload["message_id"] is None
        assert event.payload["published_at"] is None
        assert event.payload["is_mod"] is False
        assert event.payload["is_owner"] is False
        assert event.payload["is_sponsor"] is False

    async def test_truthy_non_bool_flags_are_coerced_to_bool(self) -> None:
        raw = {"live_chat_id": "chat123", "text": "hi", "is_mod": 1, "is_owner": "yes"}
        event = await normalize(raw)
        assert event.payload["is_mod"] is True
        assert event.payload["is_owner"] is True

    async def test_platform_defaults_to_youtube_when_absent(self) -> None:
        raw = {"live_chat_id": "chat123", "text": "hi"}
        event = await normalize(raw)
        assert event.platform == "youtube"


class TestRegisterDefaultBundles:
    def test_registers_the_youtube_manifest(self) -> None:
        registry = AppRegistry()
        manifest = register_default_bundles(registry)

        assert manifest.app_id == "waddles.bot.youtube.default"
        assert manifest.feature == "waddles.bot.youtube"
        assert manifest.is_default is True

    def test_ingest_stage_declares_youtube_message(self) -> None:
        """Regression guard for the `communication_model` `ManifestError` Twitch's own draft hit.

        `communication_model` is thirdparty-vendor-only
        (`flask_core.app_manifest.KNOWN_COMMUNICATION_MODELS` ==
        `{webhook_push, rest_pull}`) -- deliberately unset here, matching
        `builtin_handlers/twitch_gateway_manifest.py`'s own documented fix.
        """
        registry = AppRegistry()
        manifest = register_default_bundles(registry)

        ingest_spec = manifest.stage_specs["ingest"]
        assert ingest_spec.communication_model is None
        assert ingest_spec.consumes == (CONSUMES_TAG,)
        assert ingest_spec.entrypoint == "builtin_handlers.youtube_live_ingest:normalize"

    def test_manifest_is_retrievable_from_the_registry(self) -> None:
        registry = AppRegistry()
        register_default_bundles(registry)

        assert registry.get("waddles.bot.youtube.default").app_id == "waddles.bot.youtube.default"

    def test_raw_manifest_dict_matches_the_seeded_shape(self) -> None:
        """Loose coupling check against the `app_catalog` DB row this PR's report describes.

        The SQL migration itself is out of this PR's file-creation scope
        (not executable here) -- see `builtin_handlers/youtube_live_ingest.py`'s
        own docstring and this PR's report for the exact seed JSON.
        """
        stages = YOUTUBE_LIVE_MANIFEST["stages"]
        assert YOUTUBE_LIVE_MANIFEST["app_id"] == "waddles.bot.youtube.default"
        assert YOUTUBE_LIVE_MANIFEST["feature"] == "waddles.bot.youtube"
        assert YOUTUBE_LIVE_MANIFEST["is_default"] is True
        assert stages["ingest"]["entrypoint"] == "builtin_handlers.youtube_live_ingest:normalize"
        assert stages["ingest"]["consumes"] == [CONSUMES_TAG]
        assert "communication_model" not in stages["ingest"]
