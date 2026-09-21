"""Tests for `bundles.twitch_shoutout_action.shoutout` -- `!so`/`!vso` text+video shoutouts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest
from flask_core import (
    AsyncDAL,
    PlatformEvent,
    StageEnvelope,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from waddle_transports import NonRetryableTransportError, RetryableTransportError
from waddle_transports.transports.irc_relay import outbound_queue_key

import bundles.twitch_shoutout_action as shoutout_bundle
from bundles.twitch_shoutout_action import _ensure_shoutout_tables, _render_template, shoutout
from services.twitch_helix import TwitchHelixError

_COMMUNITY_ID = 1


def _envelope(
    payload: dict[str, Any] | None = None,
    *,
    platform: str = "twitch",
    community: str = str(_COMMUNITY_ID),
    actor: str | None = "alice",
) -> StageEnvelope:
    base_payload: dict[str, Any] = {
        "subcommand": "shoutout",
        "kind": "text",
        "target": "shroud",
        "channel_name": "shoutout-channel",
        "channel_id": "chan-123",
    }
    if payload:
        base_payload.update(payload)
    return StageEnvelope(
        tenant="1",
        community=community,
        app_id="waddles.bot.twitch.default",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat_message",
            actor=actor,
            payload=base_payload,
            occurred_at="2026-09-11T12:00:00Z",
        ),
        ts="2026-09-11T12:00:00Z",
    )


def _config(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {}
    base.update(overrides)
    return base


class _FakeHelix:
    """Stands in for `TwitchHelixClient` -- records calls, returns canned data or raises."""

    def __init__(
        self,
        *,
        user: dict[str, Any] | None = None,
        stream: dict[str, Any] | None = None,
        channel: dict[str, Any] | None = None,
        clip: dict[str, Any] | None = None,
        raise_error: TwitchHelixError | None = None,
    ) -> None:
        default_user = {"id": "999", "login": "shroud", "display_name": "Shroud"}
        self._user = user if user is not None else default_user
        self._stream = stream
        self._channel = channel
        self._clip = clip
        self._raise_error = raise_error
        self.calls: list[str] = []

    async def get_user(self, login: str) -> dict[str, Any]:
        self.calls.append(f"get_user:{login}")
        if self._raise_error is not None:
            raise self._raise_error
        return self._user

    async def get_stream(self, broadcaster_id: str) -> dict[str, Any] | None:
        self.calls.append(f"get_stream:{broadcaster_id}")
        return self._stream

    async def get_channel(self, broadcaster_id: str) -> dict[str, Any] | None:
        self.calls.append(f"get_channel:{broadcaster_id}")
        return self._channel

    async def get_top_clip(self, broadcaster_id: str) -> dict[str, Any] | None:
        self.calls.append(f"get_top_clip:{broadcaster_id}")
        return self._clip


@pytest.fixture
async def dal(tmp_path: Path) -> AsyncIterator[AsyncDAL]:
    """Real sqlite `AsyncDAL` -- `tenants`/`communities`/shoutout tables physically created.

    Production (`_ensure_shoutout_tables`) always defines `shoutout_config`/
    `shoutout_history` with `migrate=False` (schema owned by migration 046,
    assumed to already exist against real Postgres) -- a throwaway sqlite
    file has no such table until something actually creates it, so this
    fixture defines the identical column set with `migrate=True` first
    (same two-tier convention `test_dispatch_log.py`'s own `dal` fixture
    uses for `action_dispatch_log`). `_ensure_shoutout_tables`'s own
    `if "shoutout_config" not in dal.tables` guard then finds both tables
    already registered and is a no-op when the bundle runs.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path}/shoutout_test.db", pool_size=1, migrate=True)
    d = async_dal.dal
    d.define_table("tenants", migrate=True)
    d.define_table("communities", d.Field("tenant_id", "reference tenants"), migrate=True)
    d.define_table(
        "shoutout_config",
        d.Field("community_id", "reference communities", notnull=True),
        d.Field("cooldown_minutes", "integer", default=60),
        migrate=True,
    )
    d.define_table(
        "shoutout_history",
        d.Field("community_id", "reference communities", notnull=True),
        d.Field("platform", "string", notnull=True),
        d.Field("target_username", "string", notnull=True),
        d.Field("shoutout_type", "string", default="text"),
        d.Field("triggered_by_username", "string"),
        d.Field("trigger_type", "string", default="manual"),
        d.Field("created_at", "datetime", default=datetime.utcnow),
        migrate=True,
    )
    d.tenants.insert()
    d.communities.insert(tenant_id=1)
    d.commit()
    set_bundle_dal(async_dal)
    try:
        yield async_dal
    finally:
        reset_bundle_dal_for_tests()
        # `close_async()` runs pydal's `DAL.close()` inside its own
        # ThreadPoolExecutor, on a different thread than the one that
        # created the DAL -- pydal's `close()` reads THREAD_LOCAL state
        # only ever populated on the *creating* thread, so a cross-thread
        # close can raise (the exact gotcha `app.py::shutdown` documents
        # and defends against with this same try/except). `test_dispatch_
        # log.py`'s own `dal` fixture sidesteps this entirely by never
        # calling `close_async()` at all; this fixture calls it anyway
        # (for pool/thread hygiene across many tests in one session) but
        # never lets a cross-thread failure fail the test that already ran.
        try:
            await async_dal.close_async()
        except Exception:  # noqa: BLE001, S110 -- known pydal cross-thread close gotcha
            pass  # nosec B110 -- same rationale as the noqa above


@pytest.fixture
async def fake_redis() -> AsyncIterator[Any]:
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def _patch_redis_client(monkeypatch: pytest.MonkeyPatch, fake_redis: Any) -> None:
    monkeypatch.setattr(shoutout_bundle, "_get_redis_client", lambda config: fake_redis)


def _patch_helix(monkeypatch: pytest.MonkeyPatch, fake: _FakeHelix) -> None:
    monkeypatch.setattr(shoutout_bundle, "_get_helix_client", lambda http_client: fake)


def _noop_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200)), follow_redirects=False
    )


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


class TestPayloadValidation:
    """Config/payload errors raise immediately -- no DAL/Helix touched."""

    async def test_missing_community_raises(self) -> None:
        envelope = _envelope(community="")
        async with _noop_http_client() as client:
            with pytest.raises(NonRetryableTransportError, match="community"):
                await shoutout(envelope, _config(), http_client=client)

    async def test_non_numeric_community_raises(self) -> None:
        envelope = _envelope(community="not-a-number")
        async with _noop_http_client() as client:
            with pytest.raises(NonRetryableTransportError, match="not a valid integer"):
                await shoutout(envelope, _config(), http_client=client)

    async def test_wrong_subcommand_raises(self) -> None:
        envelope = _envelope({"subcommand": "somethingelse"})
        async with _noop_http_client() as client:
            with pytest.raises(NonRetryableTransportError, match="subcommand"):
                await shoutout(envelope, _config(), http_client=client)

    async def test_invalid_kind_raises(self) -> None:
        envelope = _envelope({"kind": "audio"})
        async with _noop_http_client() as client:
            with pytest.raises(NonRetryableTransportError, match="kind"):
                await shoutout(envelope, _config(), http_client=client)

    async def test_missing_target_raises(self) -> None:
        envelope = _envelope({"target": ""})
        async with _noop_http_client() as client:
            with pytest.raises(NonRetryableTransportError, match="target"):
                await shoutout(envelope, _config(), http_client=client)


class TestTextShoutout:
    async def test_offline_target_renders_default_template_and_records_history(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeHelix(
            user={"id": "999", "login": "shroud", "display_name": "Shroud"},
            stream=None,
            channel={"game_name": "Chess"},
        )
        _patch_helix(monkeypatch, fake)

        async with _noop_http_client() as client:
            result = await shoutout(_envelope(), _config(), http_client=client)

        assert result.transport == "irc_relay"

        raw = await shoutout_bundle._get_redis_client({}).rpop(outbound_queue_key("twitch"))
        import json

        sent = json.loads(raw)
        assert sent["channel"] == "shoutout-channel"
        assert "Go check out Shroud at https://twitch.tv/shroud" in sent["text"]
        assert "they were last playing Chess!" in sent["text"]
        assert "LIVE" not in sent["text"]

        history_rows = dal.dal(dal.dal.shoutout_history.target_username == "shroud").select()
        assert len(history_rows) == 1
        assert history_rows[0].platform == "twitch"
        assert history_rows[0].shoutout_type == "text"
        assert history_rows[0].triggered_by_username == "alice"

    async def test_live_target_appends_viewer_count_suffix(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeHelix(
            user={"id": "999", "login": "shroud", "display_name": "Shroud"},
            stream={"game_name": "Valorant", "viewer_count": 500},
        )
        _patch_helix(monkeypatch, fake)

        async with _noop_http_client() as client:
            await shoutout(_envelope(), _config(), http_client=client)

        raw = await shoutout_bundle._get_redis_client({}).rpop(outbound_queue_key("twitch"))
        import json

        sent = json.loads(raw)
        assert "they were last playing Valorant!" in sent["text"]
        assert "(LIVE now with 500 viewers)" in sent["text"]
        # get_channel is never needed when the stream lookup already found them live.
        assert not any(c.startswith("get_channel") for c in fake.calls)

    async def test_unknown_game_falls_back_to_default_text(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeHelix(channel={})
        _patch_helix(monkeypatch, fake)

        async with _noop_http_client() as client:
            await shoutout(_envelope(), _config(), http_client=client)

        raw = await shoutout_bundle._get_redis_client({}).rpop(outbound_queue_key("twitch"))
        import json

        assert "they were last playing something!" in json.loads(raw)["text"]


class TestVideoShoutout:
    async def test_video_with_clip_pushes_media_overlay(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRESENTATION_URL", "http://8.8.8.8:8207")
        fake = _FakeHelix(
            channel={"game_name": "Chess"},
            clip={
                "id": "clip1",
                "thumbnail_url": "https://img.example.test/thumb.jpg",
                "embed_url": "https://clips.twitch.tv/embed?clip=clip1",
                "url": "https://clips.twitch.tv/clip1",
            },
        )
        _patch_helix(monkeypatch, fake)

        captured: dict[str, Any] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = request.content
            return httpx.Response(200, json={"status": "published"})

        async with _mock_client(_handler) as client:
            await shoutout(_envelope({"kind": "video"}), _config(), http_client=client)

        import json

        assert captured["url"] == f"http://8.8.8.8:8207/overlay/{_COMMUNITY_ID}/media/push"
        body = json.loads(captured["body"])
        assert body["title"] == "Shoutout: Shroud"
        assert body["image_url"] == "https://img.example.test/thumb.jpg"
        assert body["video_url"] == "https://clips.twitch.tv/embed?clip=clip1"
        assert body["duration_s"] == 30

        history_rows = dal.dal(dal.dal.shoutout_history.target_username == "shroud").select()
        assert history_rows[0].shoutout_type == "video"

    async def test_video_duration_s_config_override(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRESENTATION_URL", "http://8.8.8.8:8207")
        clip = {"id": "c1", "thumbnail_url": None, "embed_url": None, "url": "https://x"}
        fake = _FakeHelix(channel={}, clip=clip)
        _patch_helix(monkeypatch, fake)

        captured: dict[str, Any] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(200, json={"status": "published"})

        async with _mock_client(_handler) as client:
            await shoutout(
                _envelope({"kind": "video"}), _config(video_duration_s=15), http_client=client
            )

        import json

        assert json.loads(captured["body"])["duration_s"] == 15
        assert json.loads(captured["body"])["video_url"] == "https://x"

    async def test_video_with_no_clip_skips_push_but_still_replies(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeHelix(channel={"game_name": "Chess"}, clip=None)
        _patch_helix(monkeypatch, fake)

        push_called = False

        def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal push_called
            push_called = True
            return httpx.Response(200)

        async with _mock_client(_handler) as client:
            result = await shoutout(_envelope({"kind": "video"}), _config(), http_client=client)

        assert result.transport == "irc_relay"
        assert push_called is False

    async def test_overlay_push_failure_does_not_fail_the_shoutout(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRESENTATION_URL", "http://8.8.8.8:8207")
        fake = _FakeHelix(
            channel={"game_name": "Chess"},
            clip={
                "id": "c1",
                "thumbnail_url": "https://x",
                "embed_url": "https://y",
                "url": "https://z",
            },
        )
        _patch_helix(monkeypatch, fake)

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="presentation is down")

        async with _mock_client(_handler) as client:
            result = await shoutout(_envelope({"kind": "video"}), _config(), http_client=client)

        assert result.transport == "irc_relay"
        history_rows = dal.dal(dal.dal.shoutout_history.target_username == "shroud").select()
        assert len(history_rows) == 1


class TestCooldown:
    async def test_within_cooldown_blocks_and_replies_without_calling_helix(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_shoutout_tables(dal)
        dal.dal.shoutout_history.insert(
            community_id=_COMMUNITY_ID,
            platform="twitch",
            target_username="shroud",
            shoutout_type="text",
            triggered_by_username="bob",
            trigger_type="manual",
            created_at=datetime.utcnow() - timedelta(minutes=5),
        )
        dal.dal.commit()

        fake = _FakeHelix()
        _patch_helix(monkeypatch, fake)

        async with _noop_http_client() as client:
            await shoutout(_envelope(), _config(), http_client=client)

        raw = await shoutout_bundle._get_redis_client({}).rpop(outbound_queue_key("twitch"))
        import json

        sent = json.loads(raw)
        assert sent["text"] == "!so shroud is on cooldown for 55 more minutes"
        assert fake.calls == []  # never reached Helix

    async def test_cooldown_respects_per_community_override(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_shoutout_tables(dal)
        dal.dal.shoutout_config.insert(community_id=_COMMUNITY_ID, cooldown_minutes=5)
        dal.dal.shoutout_history.insert(
            community_id=_COMMUNITY_ID,
            platform="twitch",
            target_username="shroud",
            shoutout_type="text",
            triggered_by_username="bob",
            trigger_type="manual",
            created_at=datetime.utcnow() - timedelta(minutes=10),
        )
        dal.dal.commit()

        fake = _FakeHelix(channel={"game_name": "Chess"})
        _patch_helix(monkeypatch, fake)

        async with _noop_http_client() as client:
            await shoutout(_envelope(), _config(), http_client=client)

        # 10 elapsed minutes > 5-minute community override -- cooldown expired, Helix IS reached.
        assert any(c.startswith("get_user") for c in fake.calls)

    async def test_expired_cooldown_allows_a_new_shoutout(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_shoutout_tables(dal)
        dal.dal.shoutout_history.insert(
            community_id=_COMMUNITY_ID,
            platform="twitch",
            target_username="shroud",
            shoutout_type="text",
            triggered_by_username="bob",
            trigger_type="manual",
            created_at=datetime.utcnow() - timedelta(minutes=61),
        )
        dal.dal.commit()

        fake = _FakeHelix(channel={"game_name": "Chess"})
        _patch_helix(monkeypatch, fake)

        async with _noop_http_client() as client:
            await shoutout(_envelope(), _config(), http_client=client)

        assert any(c.startswith("get_user") for c in fake.calls)
        history_rows = dal.dal(dal.dal.shoutout_history.target_username == "shroud").select()
        assert len(history_rows) == 2  # the seeded row + this new one


class TestHelixFailure:
    async def test_helix_error_becomes_a_friendly_reply_and_records_no_history(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeHelix(raise_error=TwitchHelixError("twitch user 'ghost' not found"))
        _patch_helix(monkeypatch, fake)

        async with _noop_http_client() as client:
            result = await shoutout(_envelope({"target": "ghost"}), _config(), http_client=client)

        assert result.transport == "irc_relay"
        raw = await shoutout_bundle._get_redis_client({}).rpop(outbound_queue_key("twitch"))
        import json

        assert json.loads(raw)["text"] == "shoutout failed: twitch user 'ghost' not found"
        history_rows = dal.dal(dal.dal.shoutout_history.target_username == "ghost").select()
        assert len(history_rows) == 0


class TestReplyDispatch:
    async def test_no_resolvable_channel_is_non_retryable(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeHelix(channel={"game_name": "Chess"})
        _patch_helix(monkeypatch, fake)

        async with _noop_http_client() as client:
            with pytest.raises(NonRetryableTransportError, match="channel"):
                await shoutout(
                    _envelope({"channel_name": None, "channel_id": None}),
                    _config(),
                    http_client=client,
                )

    async def test_transport_retryable_error_propagates_unchanged(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeHelix(channel={"game_name": "Chess"})
        _patch_helix(monkeypatch, fake)

        async def _raise(self: Any, config: Any, payload: Any) -> Any:
            raise RetryableTransportError("valkey unavailable", http_status=503)

        monkeypatch.setattr(shoutout_bundle.RelayOutboundIrcTransport, "send", _raise)

        async with _noop_http_client() as client:
            with pytest.raises(RetryableTransportError, match="valkey unavailable"):
                await shoutout(_envelope(), _config(), http_client=client)

    async def test_discord_reply_sends_rendered_text(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_DISCORD_BOT_TOKEN", "fake-bot-token")
        fake = _FakeHelix(channel={"game_name": "Chess"})
        _patch_helix(monkeypatch, fake)

        captured: dict[str, Any] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = request.content
            return httpx.Response(200, json={"id": "msg-1"})

        async with _mock_client(_handler) as client:
            result = await shoutout(
                _envelope(platform="discord"),
                _config(bot_token_ref="TEST_DISCORD_BOT_TOKEN", api_base="https://8.8.8.8/api/v10"),
                http_client=client,
            )

        assert result.transport == "bundle"
        assert captured["url"] == "https://8.8.8.8/api/v10/channels/chan-123/messages"
        assert captured["headers"]["authorization"] == "Bot fake-bot-token"
        import json

        assert "Go check out Shroud" in json.loads(captured["body"])["content"]


class TestHistoryWriteFailureTolerated:
    async def test_history_write_failure_does_not_raise(
        self, dal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeHelix(channel={"game_name": "Chess"})
        _patch_helix(monkeypatch, fake)

        async def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("db is on fire")

        monkeypatch.setattr(shoutout_bundle, "_record_history", _boom)

        async with _noop_http_client() as client:
            result = await shoutout(_envelope(), _config(), http_client=client)

        assert result.transport == "irc_relay"


class TestRenderTemplate:
    """Direct unit coverage of the override-render/fallback path (no DB column exists yet)."""

    def test_renders_supplied_variables(self) -> None:
        text = _render_template(
            "{display_name} ({login}): {game_name}",
            display_name="Shroud",
            login="shroud",
            game_name="Chess",
        )
        assert text == "Shroud (shroud): Chess"

    def test_falls_back_to_default_on_bad_placeholder(self) -> None:
        text = _render_template(
            "{display_name} plays {not_a_real_variable}",
            display_name="Shroud",
            login="shroud",
            game_name="Chess",
        )
        assert "Go check out Shroud at https://twitch.tv/shroud" in text
