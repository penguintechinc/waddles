"""Regression: legacy ingest handlers must never promote a non-moderator to a moderator.

Defect (same class PR #794 fixed across 40 app bundles): `builtin_handlers/
{kick,youtube_live,twitch}_ingest.normalize` read `bool(raw.get("is_mod"))`
on the raw event. A raw event is decoded from a Valkey list entry
(`json.loads`), so the flag can be ANY JSON type -- and the string `"false"`
is truthy in Python, so a non-moderator whose producer emitted `"false"`
(or `"0"`, `"no"`, a badge name, ...) was handed `is_mod=True` and passed every
downstream mod gate. The same coercion sat in the Kick and YouTube receivers
that build the raw event from the upstream platform payload.

Contract proven here, fail closed: ONLY the real boolean `True` grants a role
flag; every other value (strings included, `"true"` too) yields `False`, and a
genuine moderator (`True`) still passes. Covered at every layer a flag crosses:
the helper, each handler's `normalize`, each receiver's payload parser, and the
real `IngestRunner` over a real (fakeredis) Valkey round trip.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from flask_core import StageEnvelope
from flask_core.stage_runner import BundlePoller
from flask_core.stream_pipeline import bundle_stream_key

from builtin_handlers.kick_ingest import normalize as kick_normalize
from builtin_handlers.twitch_ingest import normalize as twitch_normalize
from builtin_handlers.youtube_live_ingest import normalize as youtube_normalize
from receivers.badge_flags import strict_flag
from receivers.kick_pusher import KickPusherReceiver, _parse_frame, _parse_pusher_data
from receivers.youtube_live_poll import YouTubeLivePollReceiver
from runner import IngestRunner

#: Every non-`True` shape a hostile or buggy producer can put in a role flag.
#: Each one MUST be treated as "not privileged".
NON_MOD_VALUES: list[Any] = [
    "false",
    "False",
    "FALSE",
    "0",
    "",
    " ",
    "no",
    "off",
    "null",
    "none",
    "moderator",
    "true",  # fail closed: even a truthy-LOOKING string is not the real boolean
    "1",
    0,
    1,  # fail closed: a truthy int is not the real boolean either
    2,
    0.0,
    1.5,
    [],
    ["moderator"],
    {},
    {"is_mod": True},
    None,
    False,
]

IDS = [repr(v) for v in NON_MOD_VALUES]

TWITCH_FLAGS = ("is_mod", "is_subscriber", "is_vip", "is_broadcaster")
KICK_FLAGS = ("is_mod", "is_subscriber", "is_owner")
YOUTUBE_FLAGS = ("is_mod", "is_owner", "is_sponsor")


def _twitch_raw(**flags: Any) -> dict[str, Any]:
    return {
        "platform": "twitch",
        "channel_name": "somechannel",
        "author_username": "viewer",
        "content": "!ping",
        **flags,
    }


def _kick_raw(**flags: Any) -> dict[str, Any]:
    return {
        "platform": "kick",
        "text": "!ping",
        "chatroom_id": 999,
        "channel_slug": "somechannel",
        "author_id": "7",
        **flags,
    }


def _youtube_raw(**flags: Any) -> dict[str, Any]:
    return {
        "platform": "youtube",
        "text": "!ping",
        "live_chat_id": "chat-1",
        "author_id": "UCviewer",
        **flags,
    }


class TestStrictFlag:
    """The shared helper every handler/receiver reads role flags through."""

    @pytest.mark.parametrize("value", NON_MOD_VALUES, ids=IDS)
    def test_anything_but_real_true_is_not_privileged(self, value: Any) -> None:
        assert strict_flag(value, field="is_mod", platform="twitch") is False

    def test_real_true_is_privileged(self) -> None:
        assert strict_flag(True, field="is_mod", platform="twitch") is True

    @pytest.mark.parametrize("value", [None, False], ids=["missing", "explicit-false"])
    def test_normal_non_privileged_values_stay_silent(
        self, value: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="receivers.badge_flags"):
            assert strict_flag(value, field="is_mod", platform="kick") is False
        assert caplog.records == []

    @pytest.mark.parametrize("value", ["false", 1, ["moderator"]], ids=repr)
    def test_non_boolean_is_logged_without_the_raw_value(
        self, value: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-boolean flag is an anomaly worth a WARNING -- but never leaks the value."""
        with caplog.at_level(logging.WARNING, logger="receivers.badge_flags"):
            assert strict_flag(value, field="is_mod", platform="youtube") is False
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert caplog.records[0].levelno == logging.WARNING
        assert "platform=youtube" in message
        assert "field=is_mod" in message
        assert f"value_type={type(value).__name__}" in message
        assert str(value) not in message.replace(type(value).__name__, "")

    def test_attacker_controlled_string_never_reaches_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret_marker = "SENTINEL-user-typed-text-9f3a"
        with caplog.at_level(logging.DEBUG, logger="receivers.badge_flags"):
            strict_flag(secret_marker, field="is_mod", platform="kick")
        assert secret_marker not in caplog.text


class TestHandlersRejectNonModInput:
    """regression: ingest mod-gate bypass -- `bool("false") is True` in every legacy handler."""

    @pytest.mark.parametrize("value", NON_MOD_VALUES, ids=IDS)
    async def test_twitch_non_mod_value_is_not_a_mod(self, value: Any) -> None:
        event = await twitch_normalize(_twitch_raw(**dict.fromkeys(TWITCH_FLAGS, value)))
        for flag in TWITCH_FLAGS:
            assert event.payload[flag] is False, flag

    @pytest.mark.parametrize("value", NON_MOD_VALUES, ids=IDS)
    async def test_kick_non_mod_value_is_not_a_mod(self, value: Any) -> None:
        event = await kick_normalize(_kick_raw(**dict.fromkeys(KICK_FLAGS, value)))
        for flag in KICK_FLAGS:
            assert event.payload[flag] is False, flag

    @pytest.mark.parametrize("value", NON_MOD_VALUES, ids=IDS)
    async def test_youtube_non_mod_value_is_not_a_mod(self, value: Any) -> None:
        event = await youtube_normalize(_youtube_raw(**dict.fromkeys(YOUTUBE_FLAGS, value)))
        for flag in YOUTUBE_FLAGS:
            assert event.payload[flag] is False, flag

    async def test_genuine_moderator_still_passes_on_every_platform(self) -> None:
        twitch = await twitch_normalize(_twitch_raw(**dict.fromkeys(TWITCH_FLAGS, True)))
        kick = await kick_normalize(_kick_raw(**dict.fromkeys(KICK_FLAGS, True)))
        youtube = await youtube_normalize(_youtube_raw(**dict.fromkeys(YOUTUBE_FLAGS, True)))
        assert all(twitch.payload[f] is True for f in TWITCH_FLAGS)
        assert all(kick.payload[f] is True for f in KICK_FLAGS)
        assert all(youtube.payload[f] is True for f in YOUTUBE_FLAGS)

    async def test_flags_are_independent(self) -> None:
        """A real `is_mod` must not leak into the sibling flags, nor a string flag into is_mod."""
        event = await twitch_normalize(_twitch_raw(is_mod=True, is_broadcaster="true"))
        assert event.payload["is_mod"] is True
        assert event.payload["is_broadcaster"] is False
        event = await twitch_normalize(_twitch_raw(is_mod="false", is_broadcaster=True))
        assert event.payload["is_mod"] is False
        assert event.payload["is_broadcaster"] is True

    async def test_missing_flags_default_to_not_privileged(self) -> None:
        assert (await twitch_normalize(_twitch_raw())).payload["is_mod"] is False
        assert (await kick_normalize(_kick_raw())).payload["is_mod"] is False
        assert (await youtube_normalize(_youtube_raw())).payload["is_mod"] is False

    async def test_non_boolean_flag_is_logged_without_the_raw_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        marker = "SENTINEL-badge-value-77c1"
        with caplog.at_level(logging.WARNING, logger="receivers.badge_flags"):
            await twitch_normalize(_twitch_raw(is_mod=marker))
            await kick_normalize(_kick_raw(is_mod=marker))
            await youtube_normalize(_youtube_raw(is_mod=marker))
        platforms = sorted(r.getMessage().split("platform=")[1].split()[0] for r in caplog.records)
        assert platforms == ["kick", "twitch", "youtube"]
        assert marker not in caplog.text


class TestReceiversRejectNonBooleanUpstreamFlags:
    """The receivers that BUILD the raw event apply the same rule to the upstream payload."""

    @pytest.mark.parametrize("value", NON_MOD_VALUES, ids=IDS)
    def test_kick_string_sender_flags_do_not_grant_roles(self, value: Any) -> None:
        data = {
            "content": "hi",
            "sender": {
                "id": 1,
                "username": "bob",
                "is_moderator": value,
                "is_subscriber": value,
                "is_channel_owner": value,
            },
        }
        item = KickPusherReceiver._normalize_chat_message(data, "acme", 999)
        assert item is not None
        assert item["is_mod"] is False
        assert item["is_subscriber"] is False
        assert item["is_owner"] is False

    def test_kick_real_badge_still_grants_role_when_sender_flag_is_a_string(self) -> None:
        """The badge list is an independent, structured source: a string flag cannot veto it."""
        data = {
            "content": "hi",
            "sender": {
                "id": 1,
                "is_moderator": "false",
                "identity": {"badges": [{"type": "moderator"}]},
            },
        }
        item = KickPusherReceiver._normalize_chat_message(data, "acme", 999)
        assert item is not None
        assert item["is_mod"] is True
        assert item["is_subscriber"] is False

    @pytest.mark.parametrize("value", NON_MOD_VALUES, ids=IDS)
    def test_youtube_string_author_flags_do_not_grant_roles(self, value: Any) -> None:
        item = YouTubeLivePollReceiver._parse_message(
            {
                "id": "m1",
                "snippet": {"displayMessage": "hi", "publishedAt": "2026-10-10T00:00:00Z"},
                "authorDetails": {
                    "channelId": "UC1",
                    "isChatModerator": value,
                    "isChatOwner": value,
                    "isChatSponsor": value,
                },
            }
        )
        assert item is not None
        assert item["is_mod"] is False
        assert item["is_owner"] is False
        assert item["is_sponsor"] is False

    def test_youtube_real_booleans_still_grant_roles(self) -> None:
        item = YouTubeLivePollReceiver._parse_message(
            {
                "id": "m1",
                "snippet": {"displayMessage": "hi"},
                "authorDetails": {
                    "channelId": "UC1",
                    "isChatModerator": True,
                    "isChatOwner": True,
                    "isChatSponsor": True,
                },
            }
        )
        assert item is not None
        assert (item["is_mod"], item["is_owner"], item["is_sponsor"]) == (True, True, True)


class TestKickBadgeListParsingFailsClosed:
    """The Kick `identity.badges` list is the second role source -- hostile shapes grant nothing."""

    @pytest.mark.parametrize(
        "identity",
        [
            None,
            "moderator",
            ["moderator"],
            {},
            {"badges": None},
            {"badges": "moderator"},
            {"badges": ["moderator", "broadcaster"]},  # bare strings, not {"type": ...} objects
            {"badges": [None, 7, [], {"type": None}, {"type": ""}, {"type": 3}, {"text": "mod"}]},
            {"badges": [{"type": ["moderator"]}]},
        ],
        ids=repr,
    )
    def test_malformed_badge_shapes_grant_no_role(self, identity: Any) -> None:
        data = {"content": "hi", "sender": {"id": 1, "username": "bob", "identity": identity}}
        item = KickPusherReceiver._normalize_chat_message(data, "acme", 999)
        assert item is not None
        assert item["badges"] == []
        assert (item["is_mod"], item["is_subscriber"], item["is_owner"]) == (False, False, False)

    def test_a_non_mod_badge_does_not_grant_mod(self) -> None:
        badges = [{"type": "subscriber"}, {"type": "vip"}, {"type": "og"}]
        data = {"content": "hi", "sender": {"id": 1, "identity": {"badges": badges}}}
        item = KickPusherReceiver._normalize_chat_message(data, "acme", 999)
        assert item is not None
        assert item["badges"] == ["subscriber", "vip", "og"]
        assert item["is_mod"] is False
        assert item["is_subscriber"] is True
        assert item["is_owner"] is False

    @pytest.mark.parametrize("raw", [None, 5, "", "not json{", "[1, 2]", "42", '"str"'], ids=repr)
    def test_malformed_pusher_data_decodes_to_empty(self, raw: Any) -> None:
        assert _parse_pusher_data(raw) == {}

    @pytest.mark.parametrize("raw", ["not json{", "[1]", "7", "null", b"\xff\xfe", None], ids=repr)
    def test_malformed_pusher_frames_are_skipped(self, raw: Any) -> None:
        assert _parse_frame(raw) is None

    def test_well_formed_pusher_input_round_trips(self) -> None:
        assert _parse_frame('{"event": "x"}') == {"event": "x"}
        assert _parse_pusher_data('{"content": "hi"}') == {"content": "hi"}


TENANT = "acme-corp"

#: (app_id, entrypoint, raw-event builder, flags the handler emits)
PLATFORMS = [
    ("waddles.bot.twitch.default", "twitch_ingest", _twitch_raw, TWITCH_FLAGS),
    ("waddles.bot.kick.default", "kick_ingest", _kick_raw, KICK_FLAGS),
    ("waddles.bot.youtube.default", "youtube_live_ingest", _youtube_raw, YOUTUBE_FLAGS),
]


def _runner(redis_client: Any, http_client_factory: Any, app_id: str, module: str) -> IngestRunner:
    bundles = [
        {
            "appId": app_id,
            "communityId": 42,
            "entrypoint": f"builtin_handlers.{module}:normalize",
            "spec": {},
            "config": {},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": True, "stage": "ingest", "bundles": bundles, "meta": {}}
        )

    poller = BundlePoller(
        http_client_factory(handler),
        "http://hub-api/api/v1/distribution/bundles",
        stage="ingest",
        jwt_provider=lambda: "t",
    )
    return IngestRunner(poller=poller, redis_client=redis_client, tenant_slug=TENANT)


class TestRealPathThroughIngestRunner:
    """Real Valkey round trip: hostile JSON on `:ingest` -> real handler -> envelope on `:process`.

    No mocked capability boundary: fakeredis LIST semantics, the real
    `IngestRunner`, the real entrypoint resolved by `load_entrypoint`.
    """

    @pytest.mark.parametrize(
        ("app_id", "module", "build", "flags"),
        PLATFORMS,
        ids=[p[1] for p in PLATFORMS],
    )
    @pytest.mark.parametrize("wire_value", ["false", "0", "", "no", "moderator", "true", 1])
    async def test_non_mod_on_the_wire_reaches_process_as_not_a_mod(
        self,
        redis_client: Any,
        http_client_factory: Any,
        app_id: str,
        module: str,
        build: Any,
        flags: tuple[str, ...],
        wire_value: Any,
    ) -> None:
        runner = _runner(redis_client, http_client_factory, app_id, module)
        ingest_key = bundle_stream_key(TENANT, "42", app_id, "ingest")
        process_key = bundle_stream_key(TENANT, "42", app_id, "process")
        await redis_client.lpush(ingest_key, json.dumps(build(**dict.fromkeys(flags, wire_value))))

        assert await runner.run_once() == 1

        raw_out = await redis_client.rpop(process_key)
        assert raw_out is not None
        envelope = StageEnvelope.from_dict(json.loads(raw_out))
        for flag in flags:
            assert envelope.event.payload[flag] is False, flag

    @pytest.mark.parametrize(
        ("app_id", "module", "build", "flags"),
        PLATFORMS,
        ids=[p[1] for p in PLATFORMS],
    )
    async def test_real_mod_on_the_wire_reaches_process_as_a_mod(
        self,
        redis_client: Any,
        http_client_factory: Any,
        app_id: str,
        module: str,
        build: Any,
        flags: tuple[str, ...],
    ) -> None:
        runner = _runner(redis_client, http_client_factory, app_id, module)
        ingest_key = bundle_stream_key(TENANT, "42", app_id, "ingest")
        process_key = bundle_stream_key(TENANT, "42", app_id, "process")
        await redis_client.lpush(ingest_key, json.dumps(build(**dict.fromkeys(flags, True))))

        assert await runner.run_once() == 1

        raw_out = await redis_client.rpop(process_key)
        assert raw_out is not None
        envelope = StageEnvelope.from_dict(json.loads(raw_out))
        for flag in flags:
            assert envelope.event.payload[flag] is True, flag
