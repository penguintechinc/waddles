"""Command-to-service regressions over the real SDK and WIT host adapter seam.

Atomic money and scope gates are exercised by the Rust host integration suite;
these tests pin the chat adapter and ensure it has no legacy storage accesses.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import StageGuard

import app

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
MENTION = "{user:opaque-token}"

# Stage-faithful fakes: identity is process-only, relay is action-only (see StageGuard).
GUARD = StageGuard()
transform = GUARD.process_stage(app.transform)
dispatch = GUARD.action_stage(app.dispatch)


class Host:
    """WIT seam records real SDK encoding and refuses any legacy storage access."""

    def __init__(self) -> None:
        """Initialize the isolated record of SDK calls and transport replies."""
        self.calls: list[tuple[str, str, dict[str, Any], str | None]] = []
        self.replies: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def invoke(self, target: str, name: str, args: bytes, key: str | None) -> bytes:
        """Return typed service JSON while recording exact wire arguments."""
        if self.error:
            raise self.error
        decoded = json.loads(args)
        self.calls.append((target, name, decoded, key))
        return json.dumps(
            {"balance": 12, "amount": decoded.get("amount", 0), "alias": "stars", "entries": []}
        ).encode()

    def resolve_actor(self) -> str:
        """Resolve the current host actor, independent of guest-visible actor text."""
        return ALICE

    def resolve_mention(self, token: str) -> str:
        """Resolve only the exact mention in the current event."""
        assert token == "opaque-token"  # noqa: S105 -- public mention identifier, not a secret
        return BOB

    def push(self, provider: str, payload: str) -> None:
        """Record action relay output after SDK serialization."""
        self.replies.append(json.loads(payload))


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    """Install real SDK imports with a narrow WIT seam and no DB/KV capability."""
    GUARD.reset()
    value = Host()
    imports = types.SimpleNamespace(
        identity=types.SimpleNamespace(
            resolve_actor=GUARD.process_only("identity.resolve_actor", value.resolve_actor),
            resolve_mention=GUARD.process_only("identity.resolve_mention", value.resolve_mention),
        ),
        tool=types.SimpleNamespace(invoke=value.invoke),
        relay=types.SimpleNamespace(push=GUARD.action_only("relay.push", value.push)),
        log=types.SimpleNamespace(Level={"WARN": "warn"}, write=lambda *_: None),
    )
    monkeypatch.setitem(sys.modules, "wit_world", types.SimpleNamespace(imports=imports))
    yield value
    GUARD.assert_clean()


def event(text: str, message_id: str = "msg-1") -> PlatformEvent:
    """Build the delivered event with replay identity and an origin channel."""
    return PlatformEvent(
        "twitch",
        "chat.message",
        "untrusted-display",
        {
            "text": text,
            "channel_id": "123",
            "message_id": message_id,
        },
        "2026-10-30T00:00:00Z",
    )


def run(text: str) -> PlatformEvent:
    """Drive the real process-stage adapter synchronously in unit tests."""
    result = asyncio.run(transform(event(text)))
    assert result is not None
    return result


@pytest.mark.parametrize(
    ("text", "tool_name", "args"),
    [
        ("!points", "points_view", {"user": ALICE}),
        ("!points view", "points_view", {"user": ALICE}),
        (f"!points {MENTION}", "points_view", {"user": BOB}),
        ("!points leaders", "points_leaders", {"limit": 10}),
        ("!points top", "points_leaders", {"limit": 10}),
        (f"!points set 0 {MENTION}", "points_set", {"user": BOB, "balance": 0}),
        (f"!points add 4 {MENTION}", "points_adjust", {"user": BOB, "delta": 4}),
        (f"!points sub 4 {MENTION}", "points_adjust", {"user": BOB, "delta": -4}),
        (f"!give 4 {MENTION}", "give", {"from": ALICE, "to": BOB, "amount": 4}),
    ],
)
def test_commands_delegate_exactly_once(
    host: Host, text: str, tool_name: str, args: dict[str, Any]
) -> None:
    """All commands use the service and resolved UUIDs, without private storage."""
    result = run(text)
    assert len(host.calls) == 1
    target, name, encoded, key = host.calls[0]
    assert (target, name, encoded) == (app.SERVICE, tool_name, args)
    assert (key is not None) is (tool_name in {"points_set", "points_adjust", "give"})
    assert result.payload["channel_id"] == "123"


def test_replay_keys_stable_across_identical_messages(host: Host) -> None:
    """Redelivery supplies the same tool retry identity and new messages differ."""
    command = f"!give 4 {MENTION}"
    run(command)
    run(command)
    asyncio.run(transform(event(command, "msg-2")))
    assert host.calls[0][3] == host.calls[1][3]
    assert host.calls[0][3] != host.calls[2][3]


@pytest.mark.parametrize(
    "text",
    [
        "!points add -1 @alice",
        "!points set +4 @alice",
        "!points add 1_000 @alice",
        f"!give 0 {MENTION}",
        f"!points add 9999999999999999999999 {MENTION}",
        "!points leaders extra",
        "!points view @rawname",
        "!give 3 @rawname",
    ],
)
def test_bad_commands_cannot_reach_money(host: Host, text: str) -> None:
    """Invalid syntax and unverified targets reply with usage without a tool call."""
    assert run(text).payload["reply"] == app._USAGE
    assert host.calls == []


def test_unrelated_events_do_not_call_service(host: Host) -> None:
    """No matching command costs no identity, storage or service operation."""
    assert asyncio.run(transform(event("hello"))) is None
    assert host.calls == []


def test_action_only_relays_and_never_calls_tools(host: Host) -> None:
    """The action runtime needs relay only; all money work finished in process."""
    transformed = run("!points")
    before = list(host.calls)
    envelope = StageEnvelope("tenant", "community", "loyalty", "action", transformed, "now")
    result = asyncio.run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "points_reply"
    assert host.calls == before
    assert host.replies == [{"channel": "123", "text": "Balance: 12 stars."}]


@pytest.mark.parametrize(
    "text",
    [
        f'!give -a 4 -u "{MENTION}"',
        f'!give --amount 4 --user "{MENTION}"',
    ],
)
def test_schema_flags_reach_the_same_service_tool(host: Host, text: str) -> None:
    """Short/long Unix flags and quoted mentions use the shared parser."""
    run(text)
    assert host.calls[0][1:3] == ("give", {"from": ALICE, "to": BOB, "amount": 4})


def test_generated_help_never_resolves_identity_or_calls_service(host: Host) -> None:
    """Command help is generated from declarations and stays read-only."""
    result = run("!give --help")
    assert "--amount" in result.payload["reply"]
    assert host.calls == []
