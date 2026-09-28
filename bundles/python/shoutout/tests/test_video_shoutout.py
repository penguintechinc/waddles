"""Host-native tests for the `!vso` video-shoutout path (`video_shoutout.py`).

Same no-WASM/wasmtime pattern as `test_app.py` (a fake `wit_world` standing in for the WIT
`db`/`kv`/`flags`/`relay`/`log` host imports), plus a fake `http_client` duck-typing
`waddle_sdk.http.HttpClient`'s `get`/`post` shape for the clip-source fetchers and the overlay
push -- no real network or WASI calls anywhere in this file.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest
from waddle_sdk.db import AsyncDB
from waddle_sdk.flask_core.bundle_runtime import (
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

import video_shoutout as vso
from app import dispatch, transform

TENANT = "tenant-1"
COMMUNITY = "42"
COMMUNITY_ID = 42

MOD_ACTOR = "mod_user"
NON_MOD_ACTOR = "rando"

TARGET_UUID = "11111111-2222-4333-8444-555555555555"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _event(
    text: str, *, actor: str | None = MOD_ACTOR, **payload_overrides: object
) -> PlatformEvent:
    payload: dict[str, object] = {
        "text": text,
        "channel_id": "12345",
        "author_id": "platform-user-1",
        **payload_overrides,
    }
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-09-28T00:00:00.000Z",
    )


def _token(uuid: str) -> str:
    return f"{{user:{uuid}}}"


# --------------------------------------------------------------------------
# Fake `wit_world` -- db, kv, flags, log. No `http`/`relay` needed on the WIT
# side: `http_client`/`relay.push` are exercised via `waddle_sdk.relay` (a
# thin wrapper we fake the same way `test_app.py` does) and a plain fake
# `http_client` object passed directly into `dispatch_vso`.
# --------------------------------------------------------------------------


@dataclass
class _WitValue:
    value: Any = None


def _make_value_classes() -> types.SimpleNamespace:
    def _cls(name: str) -> type:
        return type(name, (_WitValue,), {})

    ns = types.SimpleNamespace()
    ns.Value_NullValue = _cls("Value_NullValue")
    ns.Value_BoolValue = _cls("Value_BoolValue")
    ns.Value_IntValue = _cls("Value_IntValue")
    ns.Value_FloatValue = _cls("Value_FloatValue")
    ns.Value_TextValue = _cls("Value_TextValue")
    ns.Value_BytesValue = _cls("Value_BytesValue")
    return ns


class _FakeRows:
    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.columns = columns
        self.rows = rows
        self.rows_affected = 0


class _FakeDbHarness:
    """Answers `db.execute()` for permission reuse and target channel resolution.

    Covers `shoutout_config`/`community_members` (permission reuse) and
    `user_platform_identities` (target channel resolution, module docstring gap 1).
    """

    def __init__(self) -> None:
        self.shoutout_config: dict[int, dict[str, Any]] = {}
        self.roles_by_platform: dict[tuple[int, str, str], str] = {}
        self.roles_by_display_name: dict[tuple[int, str], str] = {}
        self.target_channels: dict[str, list[tuple[str, str]]] = {}
        self.value_mod = _make_value_classes()

    def _wrap(self, value: Any) -> _WitValue:
        if value is None:
            return self.value_mod.Value_NullValue()
        if isinstance(value, bool):
            return self.value_mod.Value_BoolValue(value)
        if isinstance(value, int):
            return self.value_mod.Value_IntValue(value)
        return self.value_mod.Value_TextValue(str(value))

    def execute(self, statement: str, params: list[Any]) -> _FakeRows:
        unwrapped = [p.value if isinstance(p, _WitValue) else p for p in params]
        if "FROM shoutout_config" in statement:
            (community_id,) = unwrapped
            row = self.shoutout_config.get(community_id)
            if row is None:
                return _FakeRows(["so_permission", "cooldown_minutes"], [])
            return _FakeRows(
                ["so_permission", "cooldown_minutes"],
                [[self._wrap(row["so_permission"]), self._wrap(row["cooldown_minutes"])]],
            )
        if "platform_user_id" in statement:
            community_id, platform, platform_user_id = unwrapped
            role = self.roles_by_platform.get((community_id, platform, platform_user_id))
            return _FakeRows(["role"], [[self._wrap(role)]] if role else [])
        if "display_name" in statement:
            community_id, display_name = unwrapped
            role = self.roles_by_display_name.get((community_id, display_name))
            return _FakeRows(["role"], [[self._wrap(role)]] if role else [])
        if "user_platform_identities" in statement:
            (target_uuid,) = unwrapped
            rows = self.target_channels.get(target_uuid, [])
            return _FakeRows(
                ["platform", "platform_channel_ref"],
                [[self._wrap(p), self._wrap(r)] for p, r in rows],
            )
        raise AssertionError(f"unexpected SQL in test fake: {statement!r}")


class _FakeKv:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.set_calls: list[tuple[str, bytes, int]] = []

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self.store[key] = value
        self.set_calls.append((key, value, ttl_seconds))


class _FakeRelay:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def push(self, provider: str, message_json: str) -> None:
        self.calls.append((provider, message_json))


@dataclass
class _Harness:
    db: _FakeDbHarness
    kv: _FakeKv
    relay: _FakeRelay
    flags_enabled: bool
    log_calls: list[tuple[str, str, str]]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    db = _FakeDbHarness()
    kv = _FakeKv()
    relay = _FakeRelay()
    log_calls: list[tuple[str, str, str]] = []
    state = _Harness(db=db, kv=kv, relay=relay, flags_enabled=True, log_calls=log_calls)

    db_mod = types.SimpleNamespace(
        execute=lambda statement, params: db.execute(statement, params), **vars(db.value_mod)
    )
    kv_mod = types.SimpleNamespace(
        get=lambda key: kv.get(key), set=lambda key, value, ttl: kv.set(key, value, ttl)
    )
    relay_mod = types.SimpleNamespace(push=lambda provider, message: relay.push(provider, message))
    flags_mod = types.SimpleNamespace(enabled=lambda key, default: state.flags_enabled)
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
    )

    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        db=db_mod, kv=kv_mod, relay=relay_mod, flags=flags_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    set_bundle_dal(AsyncDB())
    yield state
    reset_bundle_dal_for_tests()


@pytest.fixture(autouse=True)
def _ctx() -> Any:
    with bundle_context(tenant=TENANT, community=COMMUNITY, app_id="waddles.core.shoutout.default"):
        yield


# --------------------------------------------------------------------------
# resolve_source_order -- pure function, table-driven, every origin.
# --------------------------------------------------------------------------


class TestResolveSourceOrder:
    @pytest.mark.parametrize(
        ("origin", "expected"),
        [
            ("twitch", ("twitch", "kick", "youtube")),
            ("kick", ("kick", "twitch", "youtube")),
            ("youtube", ("youtube", "twitch", "kick")),
            ("discord", ("twitch", "kick", "youtube")),
            ("slack", ("twitch", "kick", "youtube")),
        ],
    )
    def test_default_config_every_origin(self, origin: str, expected: tuple[str, ...]) -> None:
        assert vso.resolve_source_order(origin, None) == expected

    def test_configured_reorder_of_twitch_and_kick_from_non_clip_origin(self) -> None:
        assert vso.resolve_source_order("discord", ["kick", "twitch", "youtube"]) == (
            "kick",
            "twitch",
            "youtube",
        )

    def test_configured_reorder_does_not_move_youtube_off_last_when_origin_is_twitch(self) -> None:
        """Invariant: origin first, YouTube last unless origin.

        Configured Twitch/Kick reordering can't violate it even if the config tries to.
        """
        assert vso.resolve_source_order("twitch", ["kick", "twitch", "youtube"]) == (
            "twitch",
            "kick",
            "youtube",
        )

    def test_configured_reorder_from_kick_origin(self) -> None:
        assert vso.resolve_source_order("kick", ["kick", "twitch", "youtube"]) == (
            "kick",
            "twitch",
            "youtube",
        )

    def test_configured_reorder_affects_youtube_origin_tail_order(self) -> None:
        assert vso.resolve_source_order("youtube", ["kick", "twitch", "youtube"]) == (
            "youtube",
            "kick",
            "twitch",
        )

    @pytest.mark.parametrize(
        "bad_order",
        [
            ["twitch", "kick"],  # missing youtube
            ["twitch", "kick", "youtube", "kick"],  # duplicate
            ["twitch", "kick", "discord"],  # not a clip platform
            [],
        ],
    )
    def test_invalid_configured_order_falls_back_to_default(self, bad_order: list[str]) -> None:
        assert vso.resolve_source_order("discord", bad_order) == ("twitch", "kick", "youtube")


# --------------------------------------------------------------------------
# transform_vso / app.transform dispatch
# --------------------------------------------------------------------------


class TestTransformVsoCommandParsing:
    def test_matches_via_app_transform(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event(f"!vso {_token(TARGET_UUID)}")))
        assert result is not None
        assert result.payload["kind"] == vso.KIND_VIDEO_SHOUTOUT
        assert result.payload["vso_target_user"] == TARGET_UUID

    def test_case_insensitive_prefix(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event(f"!VSO {_token(TARGET_UUID)}")))
        assert result is not None

    def test_uppercase_uuid_body_normalized_lowercase(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event(f"!vso {{user:{TARGET_UUID.upper()}}}")))
        assert result is not None
        assert result.payload["vso_target_user"] == TARGET_UUID

    def test_flag_off_returns_none(self, harness: _Harness) -> None:
        harness.flags_enabled = False
        assert _run(transform(_event(f"!vso {_token(TARGET_UUID)}"))) is None

    def test_bare_vso_returns_usage(self, harness: _Harness) -> None:
        result = _run(transform(_event("!vso")))
        assert result is not None
        assert result.payload["text"] == vso.VSO_USAGE

    @pytest.mark.parametrize(
        "raw_target",
        ["clubpenguinfan", "{user:not-a-uuid}", "{user:11111111-2222-4333-8444}", "user:1234"],
    )
    def test_non_uuid_target_is_invalid_mention(self, harness: _Harness, raw_target: str) -> None:
        result = _run(transform(_event(f"!vso {raw_target}")))
        assert result is not None
        assert result.payload["text"] == vso.INVALID_MENTION_REPLY

    def test_self_shoutout_denied_when_actor_uuid_matches(self, harness: _Harness) -> None:
        result = _run(transform(_event(f"!vso {_token(TARGET_UUID)}", actor_user_uuid=TARGET_UUID)))
        assert result is not None
        assert result.payload["text"] == vso.SELF_VSO_REPLY

    def test_self_check_skipped_without_actor_uuid_logs_debug(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        result = _run(transform(_event(f"!vso {_token(TARGET_UUID)}")))
        assert result is not None
        assert result.payload["vso_target_user"] == TARGET_UUID
        assert any("skipped" in call[1] for call in harness.log_calls)


class TestTransformVsoPermissionAndCooldown:
    def test_mod_permission_denies_non_mod(self, harness: _Harness) -> None:
        harness.db.shoutout_config[COMMUNITY_ID] = {"so_permission": "mod", "cooldown_minutes": 60}
        result = _run(transform(_event(f"!vso {_token(TARGET_UUID)}", actor=NON_MOD_ACTOR)))
        assert result is not None
        assert result.payload["text"] == vso.PERMISSION_DENIED_REPLY

    def test_everyone_permission_allows_non_mod(self, harness: _Harness) -> None:
        harness.db.shoutout_config[COMMUNITY_ID] = {
            "so_permission": "everyone",
            "cooldown_minutes": 60,
        }
        result = _run(transform(_event(f"!vso {_token(TARGET_UUID)}", actor=NON_MOD_ACTOR)))
        assert result is not None
        assert result.payload["vso_target_user"] == TARGET_UUID

    def test_on_target_cooldown_returns_reply_without_forwarding(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        harness.kv.store[f"shoutout:vcd:{COMMUNITY}:{TARGET_UUID}"] = b"1"
        result = _run(transform(_event(f"!vso {_token(TARGET_UUID)}")))
        assert result is not None
        assert "kind" not in result.payload
        assert "recently" in result.payload["text"]

    def test_on_channel_cooldown_returns_reply_without_forwarding(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"
        harness.kv.store[f"shoutout:vcd:{COMMUNITY}:_channel_"] = b"1"
        result = _run(transform(_event(f"!vso {_token(TARGET_UUID)}")))
        assert result is not None
        assert result.payload["text"] == vso.CHANNEL_ON_COOLDOWN_REPLY

    def test_kv_denied_degrades_to_not_on_cooldown(self, harness: _Harness) -> None:
        harness.db.roles_by_display_name[(COMMUNITY_ID, MOD_ACTOR)] = "moderator"

        def _denied(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("kv capability denied")

        harness.kv.get = _denied  # type: ignore[method-assign]
        result = _run(transform(_event(f"!vso {_token(TARGET_UUID)}")))
        assert result is not None
        assert result.payload["vso_target_user"] == TARGET_UUID


# --------------------------------------------------------------------------
# dispatch_vso / app.dispatch dispatch
# --------------------------------------------------------------------------


def _sample_envelope(
    *, channel_id: str | None = "12345", target_uuid: str | None = TARGET_UUID
) -> StageEnvelope:
    payload: dict[str, Any] = {"channel_id": channel_id, "kind": vso.KIND_VIDEO_SHOUTOUT}
    if target_uuid is not None:
        payload["vso_target_user"] = target_uuid
    return StageEnvelope(
        tenant=TENANT,
        community=COMMUNITY,
        app_id="waddles.core.shoutout.default",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor=MOD_ACTOR,
            payload=payload,
            occurred_at="2026-09-28T00:00:00.000Z",
        ),
        ts="2026-09-28T00:00:00.000Z",
    )


_TWITCH_REFS = vso.TargetChannelRefs(
    twitch_broadcaster_id="12345", kick_channel_slug=None, youtube_channel_id=None
)
_ALL_PLATFORM_REFS = vso.TargetChannelRefs(
    twitch_broadcaster_id="12345", kick_channel_slug="the-slug", youtube_channel_id="UC-abc"
)


def _resolver(refs: vso.TargetChannelRefs | None) -> vso.TargetChannelResolver:
    async def _inner(_target_uuid: str) -> vso.TargetChannelRefs | None:
        return refs

    return _inner


class TestDispatchVsoGating:
    def test_missing_channel_id_raises(self, harness: _Harness) -> None:
        envelope = _sample_envelope(channel_id=None)
        with pytest.raises(ValueError, match="channel_id"):
            _run(dispatch(envelope, {}, http_client=None))

    def test_missing_target_raises(self, harness: _Harness) -> None:
        envelope = _sample_envelope(target_uuid=None)
        with pytest.raises(ValueError, match="vso_target_user"):
            _run(dispatch(envelope, {}, http_client=None))

    def test_target_cooldown_short_circuits_without_resolving(self, harness: _Harness) -> None:
        harness.kv.store[f"shoutout:vcd:{COMMUNITY}:{TARGET_UUID}"] = b"1"

        async def _boom(_uuid: str) -> vso.TargetChannelRefs | None:
            raise AssertionError("resolver must not be called while on cooldown")

        envelope = _sample_envelope()
        result = _run(vso.dispatch_vso(envelope, {}, http_client=None, resolver=_boom))
        assert result.detail == "on_cooldown"
        assert harness.relay.calls

    def test_channel_cooldown_short_circuits(self, harness: _Harness) -> None:
        harness.kv.store[f"shoutout:vcd:{COMMUNITY}:_channel_"] = b"1"
        envelope = _sample_envelope()
        result = _run(vso.dispatch_vso(envelope, {}, http_client=None, resolver=_resolver(None)))
        assert result.detail == "channel_on_cooldown"

    def test_unresolved_target_replies_and_sets_no_cooldown(self, harness: _Harness) -> None:
        envelope = _sample_envelope()
        result = _run(vso.dispatch_vso(envelope, {}, http_client=None, resolver=_resolver(None)))
        assert result.detail == "target_unresolved"
        assert harness.kv.set_calls == []
        provider, message_json = harness.relay.calls[0]
        assert _token(TARGET_UUID) in json.loads(message_json)["text"]

    def test_no_eligible_clip_replies_and_sets_no_cooldown(self, harness: _Harness) -> None:
        envelope = _sample_envelope()
        resolver = _resolver(_TWITCH_REFS)
        result = _run(vso.dispatch_vso(envelope, {}, http_client=None, resolver=resolver))
        assert result.detail == "no_clip_found"
        assert harness.kv.set_calls == []


class _StubHttpClient:
    """Answers `.get` for Twitch/Kick/YouTube clip lookups and `.post` for the overlay push."""

    def __init__(
        self,
        *,
        twitch_clips: list[dict[str, Any]] | None = None,
        kick_clips: list[dict[str, Any]] | None = None,
        youtube_search_items: list[dict[str, Any]] | None = None,
        youtube_video_items: list[dict[str, Any]] | None = None,
        overlay_status: int = 200,
        raise_on_overlay: bool = False,
    ) -> None:
        self.twitch_clips = twitch_clips or []
        self.kick_clips = kick_clips or []
        self.youtube_search_items = youtube_search_items or []
        self.youtube_video_items = youtube_video_items or []
        self.overlay_status = overlay_status
        self.raise_on_overlay = raise_on_overlay
        self.get_urls: list[str] = []
        self.post_calls: list[tuple[str, bytes | None]] = []

    async def get(self, url: str, **_kwargs: Any) -> dict[str, Any]:
        self.get_urls.append(url)
        if "api.twitch.tv" in url:
            return {"status": 200, "body": json.dumps({"data": self.twitch_clips}).encode()}
        if "api.kick.com" in url:
            return {"status": 200, "body": json.dumps({"data": self.kick_clips}).encode()}
        if "youtube/v3/search" in url:
            return {
                "status": 200,
                "body": json.dumps({"items": self.youtube_search_items}).encode(),
            }
        if "youtube/v3/videos" in url:
            return {"status": 200, "body": json.dumps({"items": self.youtube_video_items}).encode()}
        raise AssertionError(f"unexpected GET url in test stub: {url!r}")

    async def post(self, url: str, *, body: bytes | None = None, **_kwargs: Any) -> dict[str, Any]:
        if self.raise_on_overlay:
            raise RuntimeError("simulated overlay egress denial")
        self.post_calls.append((url, body))
        return {"status": self.overlay_status}


def _twitch_clip(clip_id: str = "c1", duration: float = 12.0) -> dict[str, Any]:
    return {
        "id": clip_id,
        "url": f"https://clips.twitch.tv/{clip_id}",
        "duration": duration,
        "title": "t",
    }


class TestDispatchVsoClipFlow:
    def test_twitch_clip_found_relays_and_pushes_overlay(self, harness: _Harness) -> None:
        envelope = _sample_envelope()
        http_client = _StubHttpClient(twitch_clips=[_twitch_clip()])
        resolver = _resolver(_TWITCH_REFS)
        result = _run(
            vso.dispatch_vso(
                envelope,
                {"overlay_push_host": "svc-presentation"},
                http_client=http_client,
                resolver=resolver,
            )
        )
        assert result.detail == "overlay_pushed"
        assert http_client.post_calls
        provider, message_json = harness.relay.calls[0]
        message = json.loads(message_json)["text"]
        assert _token(TARGET_UUID) in message
        assert "clips.twitch.tv" in message

    def test_no_overlay_host_configured_skips_push_but_still_relays(
        self, harness: _Harness
    ) -> None:
        envelope = _sample_envelope()
        http_client = _StubHttpClient(twitch_clips=[_twitch_clip()])
        resolver = _resolver(_TWITCH_REFS)
        result = _run(vso.dispatch_vso(envelope, {}, http_client=http_client, resolver=resolver))
        assert result.detail == "relayed_chat_only"
        assert http_client.post_calls == []
        assert harness.relay.calls

    def test_overlay_push_failure_degrades_without_failing_dispatch(
        self, harness: _Harness
    ) -> None:
        envelope = _sample_envelope()
        http_client = _StubHttpClient(twitch_clips=[_twitch_clip()], raise_on_overlay=True)
        resolver = _resolver(_TWITCH_REFS)
        result = _run(
            vso.dispatch_vso(
                envelope,
                {"overlay_push_host": "svc-presentation"},
                http_client=http_client,
                resolver=resolver,
            )
        )
        assert result.detail == "relayed_chat_only"
        assert harness.relay.calls

    def test_clips_over_30s_are_filtered_out(self, harness: _Harness) -> None:
        envelope = _sample_envelope()
        http_client = _StubHttpClient(twitch_clips=[_twitch_clip(duration=45.0)])
        resolver = _resolver(_TWITCH_REFS)
        result = _run(vso.dispatch_vso(envelope, {}, http_client=http_client, resolver=resolver))
        assert result.detail == "no_clip_found"

    def test_sets_both_cooldowns_on_success(self, harness: _Harness) -> None:
        envelope = _sample_envelope()
        http_client = _StubHttpClient(twitch_clips=[_twitch_clip()])
        resolver = _resolver(_TWITCH_REFS)
        _run(vso.dispatch_vso(envelope, {}, http_client=http_client, resolver=resolver))
        keys = {key for key, _value, _ttl in harness.kv.set_calls}
        assert f"shoutout:vcd:{COMMUNITY}:{TARGET_UUID}" in keys
        assert f"shoutout:vcd:{COMMUNITY}:_channel_" in keys

    def test_recent_clip_excluded_from_random_pick(self, harness: _Harness) -> None:
        harness.kv.store[f"shoutout:vso:recent:{TARGET_UUID}"] = json.dumps(["c1"]).encode()
        envelope = _sample_envelope()
        http_client = _StubHttpClient(twitch_clips=[_twitch_clip("c1"), _twitch_clip("c2")])
        resolver = _resolver(_TWITCH_REFS)
        _run(vso.dispatch_vso(envelope, {}, http_client=http_client, resolver=resolver))
        _, message_json = harness.relay.calls[0]
        assert "c2" in json.loads(message_json)["text"]
        assert "c1" not in json.loads(message_json)["text"]

    def test_origin_platform_tried_first_even_with_all_platforms_resolved(
        self, harness: _Harness
    ) -> None:
        envelope = _sample_envelope()  # platform="twitch"
        http_client = _StubHttpClient(
            twitch_clips=[_twitch_clip("twitch-clip")],
            kick_clips=[{"id": "kick-clip", "clip_url": "https://kick.com/c", "duration": 10}],
        )
        resolver = _resolver(_ALL_PLATFORM_REFS)
        _run(vso.dispatch_vso(envelope, {}, http_client=http_client, resolver=resolver))
        assert any("api.twitch.tv" in url for url in http_client.get_urls)
        assert not any("api.kick.com" in url for url in http_client.get_urls)

    def test_falls_through_to_next_source_when_origin_has_no_eligible_clip(
        self, harness: _Harness
    ) -> None:
        envelope = _sample_envelope()  # platform="twitch", origin tried first
        http_client = _StubHttpClient(
            twitch_clips=[],  # origin has nothing eligible
            kick_clips=[{"id": "kick-clip", "clip_url": "https://kick.com/c", "duration": 10}],
        )
        resolver = _resolver(_ALL_PLATFORM_REFS)
        result = _run(vso.dispatch_vso(envelope, {}, http_client=http_client, resolver=resolver))
        assert result.detail in ("overlay_pushed", "relayed_chat_only")
        _, message_json = harness.relay.calls[0]
        assert "kick.com" in json.loads(message_json)["text"]

    @pytest.mark.parametrize(
        ("configured_order", "expected_first_call_fragment"),
        [
            (["twitch", "kick", "youtube"], "api.twitch.tv"),
            (["kick", "twitch", "youtube"], "api.kick.com"),
        ],
    )
    def test_configurable_default_order_reorders_twitch_and_kick_for_non_clip_origin(
        self, harness: _Harness, configured_order: list[str], expected_first_call_fragment: str
    ) -> None:
        """From a non-clip-capable origin (discord), the configured order's relative Twitch/Kick.

        Order applies -- but YouTube stays last regardless (the invariant a config can never
        violate, see `resolve_source_order`'s own docstring).
        """
        envelope = StageEnvelope(
            tenant=TENANT,
            community=COMMUNITY,
            app_id="waddles.core.shoutout.default",
            stage="action",
            event=PlatformEvent(
                platform="discord",
                event_type="chat.message",
                actor=MOD_ACTOR,
                payload={
                    "channel_id": "12345",
                    "vso_target_user": TARGET_UUID,
                    "kind": vso.KIND_VIDEO_SHOUTOUT,
                },
                occurred_at="2026-09-28T00:00:00.000Z",
            ),
            ts="2026-09-28T00:00:00.000Z",
        )
        http_client = _StubHttpClient(
            twitch_clips=[_twitch_clip("twitch-clip")],
            kick_clips=[{"id": "kick-clip", "clip_url": "https://kick.com/c", "duration": 10}],
            youtube_search_items=[{"id": {"videoId": "yt1"}}],
            youtube_video_items=[
                {
                    "id": "yt1",
                    "contentDetails": {"duration": "PT15S"},
                    "snippet": {"title": "short"},
                }
            ],
        )
        resolver = _resolver(_ALL_PLATFORM_REFS)
        _run(
            vso.dispatch_vso(
                envelope,
                {"clip_source_order": configured_order},
                http_client=http_client,
                resolver=resolver,
            )
        )
        assert expected_first_call_fragment in http_client.get_urls[0]
        assert not any("youtube/v3" in url for url in http_client.get_urls[:-1])


class TestDefaultTargetChannelResolver:
    def test_missing_table_degrades_to_none(self, harness: _Harness) -> None:
        """The `user_platform_identities` table doesn't exist in any migration yet.

        Module docstring gap 1 -- simulated here as any DB error, not a special case.
        """

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError('relation "user_platform_identities" does not exist')

        harness.db.execute = _boom  # type: ignore[method-assign]
        result = _run(vso.default_target_channel_resolver(TARGET_UUID))
        assert result is None

    def test_resolves_rows_into_refs(self, harness: _Harness) -> None:
        harness.db.target_channels[TARGET_UUID] = [
            ("twitch", "12345"),
            ("kick", "the-slug"),
            ("youtube", "UC-abc"),
        ]
        result = _run(vso.default_target_channel_resolver(TARGET_UUID))
        assert result == _ALL_PLATFORM_REFS

    def test_no_rows_degrades_to_none(self, harness: _Harness) -> None:
        result = _run(vso.default_target_channel_resolver(TARGET_UUID))
        assert result is None


# --------------------------------------------------------------------------
# Clip-source fetchers -- direct unit tests.
# --------------------------------------------------------------------------


class TestFetchTwitchClips:
    def test_no_http_client_returns_empty(self, harness: _Harness) -> None:
        assert _run(vso._fetch_twitch_clips(None, "123")) == []

    def test_filters_over_30s(self, harness: _Harness) -> None:
        http_client = _StubHttpClient(
            twitch_clips=[_twitch_clip("short", 20), _twitch_clip("long", 31)]
        )
        clips = _run(vso._fetch_twitch_clips(http_client, "123"))
        assert [c.clip_id for c in clips] == ["short"]

    def test_non_200_returns_empty(self, harness: _Harness) -> None:
        class _NotFound:
            async def get(self, *_a: Any, **_k: Any) -> dict[str, Any]:
                return {"status": 404, "body": b"{}"}

        assert _run(vso._fetch_twitch_clips(_NotFound(), "123")) == []

    def test_transport_error_returns_empty(self, harness: _Harness) -> None:
        class _Boom:
            async def get(self, *_a: Any, **_k: Any) -> Any:
                raise RuntimeError("egress denied")

        assert _run(vso._fetch_twitch_clips(_Boom(), "123")) == []


class TestFetchKickClips:
    def test_no_channel_slug_returns_empty(self, harness: _Harness) -> None:
        assert _run(vso._fetch_kick_clips(_StubHttpClient(), "")) == []

    def test_parses_eligible_clip(self, harness: _Harness) -> None:
        http_client = _StubHttpClient(
            kick_clips=[{"id": "k1", "clip_url": "https://kick.com/c1", "duration": 5}]
        )
        clips = _run(vso._fetch_kick_clips(http_client, "slug"))
        assert clips[0].source == "kick"
        assert clips[0].video_url == "https://kick.com/c1"

    def test_credential_broker_failure_degrades_to_empty(self, harness: _Harness) -> None:
        class _Boom:
            async def get(self, *_a: Any, **_k: Any) -> Any:
                raise RuntimeError("no KICK_API_BEARER secret resolvable")

        assert _run(vso._fetch_kick_clips(_Boom(), "slug")) == []


class TestFetchYoutubeClips:
    def test_no_channel_id_returns_empty(self, harness: _Harness) -> None:
        assert _run(vso._fetch_youtube_clips(_StubHttpClient(), "")) == []

    def test_search_then_videos_round_trip(self, harness: _Harness) -> None:
        http_client = _StubHttpClient(
            youtube_search_items=[{"id": {"videoId": "abc"}}],
            youtube_video_items=[
                {"id": "abc", "contentDetails": {"duration": "PT29S"}, "snippet": {"title": "hi"}}
            ],
        )
        clips = _run(vso._fetch_youtube_clips(http_client, "UC1"))
        assert len(clips) == 1
        assert clips[0].duration_seconds == 29
        assert clips[0].video_url == "https://www.youtube.com/shorts/abc"

    def test_over_30s_filtered_out(self, harness: _Harness) -> None:
        http_client = _StubHttpClient(
            youtube_search_items=[{"id": {"videoId": "abc"}}],
            youtube_video_items=[{"id": "abc", "contentDetails": {"duration": "PT1M"}}],
        )
        assert _run(vso._fetch_youtube_clips(http_client, "UC1")) == []

    def test_no_search_results_skips_videos_call(self, harness: _Harness) -> None:
        http_client = _StubHttpClient(youtube_search_items=[])
        assert _run(vso._fetch_youtube_clips(http_client, "UC1")) == []
        assert not any("youtube/v3/videos" in url for url in http_client.get_urls)


class TestParseIso8601Duration:
    @pytest.mark.parametrize(
        ("duration", "expected"),
        [
            ("PT30S", 30),
            ("PT1M", 60),
            ("PT1H2M3S", 3723),
            ("PT0S", 0),
            ("", None),
            ("bogus", None),
        ],
    )
    def test_parses(self, duration: str, expected: int | None) -> None:
        assert vso._parse_iso8601_duration_seconds(duration) == expected


class TestRecentClipHistory:
    def test_round_trip(self, harness: _Harness) -> None:
        _run(vso._remember_clip_id(TARGET_UUID, "c1"))
        assert _run(vso._recent_clip_ids(TARGET_UUID)) == ["c1"]
        _run(vso._remember_clip_id(TARGET_UUID, "c2"))
        assert _run(vso._recent_clip_ids(TARGET_UUID)) == ["c2", "c1"]

    def test_kv_error_degrades_to_empty(self, harness: _Harness) -> None:
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("kv denied")

        harness.kv.get = _boom  # type: ignore[method-assign]
        assert _run(vso._recent_clip_ids(TARGET_UUID)) == []

    def test_history_size_capped(self, harness: _Harness) -> None:
        for i in range(vso._RECENT_CLIP_HISTORY_SIZE + 2):
            _run(vso._remember_clip_id(TARGET_UUID, f"c{i}"))
        recent = _run(vso._recent_clip_ids(TARGET_UUID))
        assert len(recent) == vso._RECENT_CLIP_HISTORY_SIZE


class TestEntryWiringStillWiresBothCommands:
    def test_wires_bundle_transform_and_dispatch_to_app_module(self) -> None:
        import _entry_wiring

        assert _entry_wiring.bundle_transform is transform
        assert _entry_wiring.bundle_dispatch is dispatch
