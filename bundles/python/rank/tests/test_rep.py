"""`!rep` / `!reputation` regressions: process-stage lookup via the reputation service tool.

Stage-faithful fakes (identity is process-only, relay action-only) over the real SDK encoding;
no kv/db capability is wired, proving `!rep` never touches the rank ledger.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest
from waddle_sdk import tool
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import StageGuard

import app

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
MENTION = "{user:opaque-token}"
GUARD = StageGuard()
transform = GUARD.process_stage(app.transform)
dispatch = GUARD.action_stage(app.dispatch)


class Host:
    """Records tool calls and relay pushes at the WIT seam."""

    def __init__(self) -> None:
        """Start with no calls and no injected failure."""
        self.calls: list[tuple[str, str, dict[str, Any], str | None]] = []
        self.replies: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def invoke(self, target: str, name: str, args: bytes, key: str | None) -> bytes:
        """Return the reputation service's `rep` shape, recording the wire args."""
        if self.error:
            raise self.error
        self.calls.append((target, name, json.loads(args), key))
        return json.dumps({"text": "Reputation 650 (Fair); 50 more for Good"}).encode()

    def resolve_actor(self) -> str:
        """Host-verified caller."""
        return ALICE

    def resolve_mention(self, token: str) -> str:
        """Resolve only the exact mention in the event."""
        assert token == "opaque-token"  # noqa: S105 -- public mention id, not a secret
        return BOB

    def push(self, provider: str, payload: str) -> None:
        """Record the action-stage relay."""
        self.replies.append(json.loads(payload))


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Iterator[Host]:
    """Install the narrow WIT seam (identity + tool + relay + log)."""
    GUARD.reset()
    value = Host()
    imports = types.SimpleNamespace(
        identity=types.SimpleNamespace(
            resolve_actor=GUARD.process_only("identity.resolve_actor", value.resolve_actor),
            resolve_mention=GUARD.process_only("identity.resolve_mention", value.resolve_mention),
        ),
        tool=types.SimpleNamespace(invoke=value.invoke),
        relay=types.SimpleNamespace(push=GUARD.action_only("relay.push", value.push)),
        log=types.SimpleNamespace(
            Level={"INFO": "info", "WARN": "warn"}, write=lambda *_: None
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", types.SimpleNamespace(imports=imports))
    yield value
    GUARD.assert_clean()


def ev(text: str, channel: str | None = "123") -> PlatformEvent:
    """Build a chat event."""
    payload: dict[str, Any] = {"text": text}
    if channel is not None:
        payload["channel_id"] = channel
    return PlatformEvent("discord", "chat.message", "untrusted", payload, "2026-11-07T00:00:00Z")


def run(text: str) -> PlatformEvent:
    """Drive transform synchronously; `!rep` always yields a reply event."""
    out = asyncio.run(transform(ev(text)))
    assert out is not None
    return out


@pytest.mark.parametrize("head", ["!rep", "!reputation", "!REP"])
def test_self_lookup_uses_verified_actor(host: Host, head: str) -> None:
    """The subject is the host-verified caller, never chat-supplied text."""
    out = run(head)
    assert host.calls == [(app._REP_SERVICE, "rep", {"user": ALICE}, None)]
    assert out.payload == {
        "command": "rep_reply",
        "channel_id": "123",
        "reply": "Reputation 650 (Fair); 50 more for Good",
    }


def test_mention_lookup_resolves_host_token(host: Host) -> None:
    """A single host-issued mention selects another member."""
    run(f"!rep {MENTION}")
    assert host.calls[0][2] == {"user": BOB}


@pytest.mark.parametrize("arg", ["bob", f"{MENTION} extra", "{user:a} {user:b}"])
def test_typed_handles_and_extra_args_get_usage_without_tool_call(host: Host, arg: str) -> None:
    """Raw handles are never resolved; usage is the explicit reply."""
    out = run(f"!rep {arg}")
    assert out.payload["reply"] == app._REP_USAGE
    assert host.calls == []


def test_missing_channel_fails_loud(host: Host) -> None:
    """No origin channel is an error, not a silent drop."""
    with pytest.raises(ValueError, match="origin channel"):
        asyncio.run(transform(ev("!rep", channel=None)))


def test_refusal_is_explicit_and_outage_propagates(host: Host) -> None:
    """Denied grants reply; backend outages raise instead of fabricating a score."""
    host.error = RuntimeError("boom")
    with pytest.raises(tool.ToolError, match="target_unavailable"):
        run("!rep")


def test_forbidden_tool_error_replies_refusal(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A not-granted/forbidden service answer is an explicit refusal reply."""

    async def deny(*_a: object, **_k: object) -> object:
        raise tool.ToolError("forbidden")

    monkeypatch.setattr(tool, "invoke", deny)
    assert run("!rep").payload["reply"] == "Reputation lookup refused (forbidden)."


def test_unavailable_tool_error_propagates(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    """target_unavailable is never papered over."""

    async def down(*_a: object, **_k: object) -> object:
        raise tool.ToolError("target_unavailable")

    monkeypatch.setattr(tool, "invoke", down)
    with pytest.raises(tool.ToolError):
        run("!rep")


def test_action_relays_reply_only(host: Host) -> None:
    """Action-stage relays the computed reply and makes no tool call."""
    out = run("!rep")
    env = StageEnvelope("tenant", "community", "rank", "action", out, "now")
    res = asyncio.run(dispatch(env, {}, http_client=None))
    assert res.detail == "rep_reply"
    assert host.replies == [{"channel": "123", "text": out.payload["reply"]}]
    assert len(host.calls) == 1


def test_rank_command_unaffected(host: Host) -> None:
    """`!rank` still parses; `!rep` heads never reach the rank grammar."""
    out = asyncio.run(transform(ev("!rank")))
    assert out is not None and out.payload["command"] == "rank_self"
    assert host.calls == []
