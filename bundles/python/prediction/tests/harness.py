"""Shared host-native harness: a fake `wit_world` (kv/clock/relay/log + SDK economy/identity fakes).

Every test drives the REAL path -- a chat line goes through `transform`, the transformed event
through `dispatch` -- against in-memory hosts that enforce the real host's rules (kv key charset,
economy actor binding / caps / payout multiple), so a bug in the money flow fails a test.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
import types
import uuid
from typing import Any

import pytest
from waddle_sdk import community_kv
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeEconomyHost, FakeIdentityHost, FakeKvHost, rfc3339_from_millis

import app

COMM = "comm-1"
NS = "prediction"
START_MS = 1_700_000_000_000

_MSG_IDS = itertools.count(1)


def next_msg_id() -> str:
    """A fresh platform message id per delivery, like the real pipeline."""
    return f"msg-{next(_MSG_IDS)}"


def run(coro: Any) -> Any:
    """Run one coroutine to completion."""
    return asyncio.run(coro)


class Host:
    """The fake host: kv, economy, identity, clock, relay and log, plus assertion helpers."""

    def __init__(self, mp: pytest.MonkeyPatch) -> None:
        """Install the fake `wit_world` and register four funded members."""
        self.kv = FakeKvHost()
        self.eco = FakeEconomyHost()
        self.ident = FakeIdentityHost()
        self.users: dict[str, str] = {}
        self.now_ms = START_MS
        self.relayed: list[str] = []
        self.logs: list[tuple[str, str]] = []
        self.kv_ops: list[int] = []
        kv, ns = self.kv, types.SimpleNamespace
        mod = types.ModuleType("wit_world")
        mod.imports = ns(  # type: ignore[attr-defined]
            kv=ns(get=kv.get, set=kv.set, delete=kv.delete, increment=kv.increment),
            economy=ns(
                balance=self.eco.balance,
                wager=self.eco.wager,
                transfer=self.eco.transfer,
                max_bet=self.eco.max_bet,
                leaderboard=self.eco.leaderboard,
            ),
            identity=ns(
                resolve_actor=self.ident.resolve_actor, resolve_mention=self.ident.resolve_mention
            ),
            clock=ns(
                now_millis=lambda: self.now_ms, now_rfc3339=lambda: "x", monotonic_nanos=lambda: 0
            ),
            relay=ns(push=lambda provider, msg: self.relayed.append(json.loads(msg)["text"])),
            log=ns(
                Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
                write=lambda lvl, msg, fj: self.logs.append((msg, fj)),
            ),
        )
        mp.setitem(sys.modules, "wit_world", mod)
        for name in ("alice", "bob", "carol", "mod"):
            self.add_user(name, 1000)

    def add_user(self, name: str, balance: int) -> str:
        """Register a linked community member with a starting balance; returns their uuid."""
        uid = str(uuid.UUID(int=len(self.users) + 1))
        self.users[name] = uid
        self.eco.members.add(uid)
        self.ident.members.add(uid)
        self.eco.balances[uid] = balance
        return uid

    def act_as(self, name: str | None) -> None:
        """Bind the host's invoking actor (economy payer + identity) to `name`."""
        uid = self.users[name] if name else None
        self.eco.actor = uid
        self.ident.actor = uid

    def bal(self, name: str) -> int:
        """A member's current balance."""
        return self.eco.balances[self.users[name]]

    def total(self) -> int:
        """All points in circulation."""
        return sum(self.eco.balances.values())

    def wagers(self) -> int:
        """How many wagers the economy applied."""
        return sum(1 for kind, _user, _delta in self.eco.ledger if kind == "wager")

    def advance(self, seconds: float) -> None:
        """Move the host clock forward."""
        self.now_ms += int(seconds * 1000)

    def stamp(self, offset_seconds: int = 0) -> str:
        """The ingest stamp of an event arriving now (or that long ago / ahead)."""
        return rfc3339_from_millis(self.now_ms + offset_seconds * 1000)

    def key(self, *parts: object) -> str:
        """The full scoped kv key for `<ns>.<parts...>`."""
        return _scoped_key(COMM, ".".join((NS, *(str(p) for p in parts))))

    def keys(self, infix: str) -> list[str]:
        """Every stored key containing `infix`."""
        return [k for k in self.kv.store if infix in k]

    def counter(self, *parts: object) -> int:
        """A stored decimal counter (0 if unset)."""
        return int(self.kv.store.get(self.key(*parts), b"0"))

    def expire_cooldowns(self) -> None:
        """The fake ignores TTL: drop every cooldown counter as the host would."""
        for k in self.keys(f".{NS}.cd."):
            del self.kv.store[k]

    def expire(self, infix: str) -> None:
        """Drop every key containing `infix` (simulate TTL lapse)."""
        for k in self.keys(infix):
            del self.kv.store[k]


def make_event(
    host: Host,
    who: str | None,
    text: str,
    *,
    mod: bool = False,
    msg_id: str | None = None,
    actor: str | None = "",
    extra: dict[str, Any] | None = None,
) -> PlatformEvent:
    """Build the raw inbound chat event (`actor=""` means the minted `{user:tok-<who>}`)."""
    payload: dict[str, Any] = {
        "text": text,
        "channel_id": "123",
        "message_id": msg_id or next_msg_id(),
    }
    if mod:
        payload["is_mod"] = True
    payload.update(extra or {})
    label = f"{{user:tok-{who}}}" if actor == "" else actor
    return PlatformEvent("twitch", "chat.message", label, payload, host.stamp())


def to_envelope(event: PlatformEvent, community: str | None = COMM) -> StageEnvelope:
    """Wrap a transformed event the way the action stage receives it."""
    return StageEnvelope(
        tenant="t",
        community=community,
        app_id=f"waddles.core.example.{NS}",
        stage="action",
        event=event,
        ts="2026-10-22T00:00:00Z",
    )


def envelope(host: Host, who: str, text: str, **kw: Any) -> StageEnvelope:
    """Raw chat line -> `transform` -> action envelope (the real path)."""
    out = run(app.transform(make_event(host, who, text, **kw)))
    assert out is not None, f"transform ignored {text!r}"
    return to_envelope(out)


def say(
    host: Host,
    who: str,
    text: str,
    *,
    mod: bool = False,
    msg_id: str | None = None,
    cooldown: bool = False,
    **kw: Any,
) -> str | None:
    """Send one chat line as `who`; returns the reply (or `None` for a silent skip)."""
    if not cooldown:
        host.expire_cooldowns()
    host.act_as(who)
    env = envelope(host, who, text, mod=mod, msg_id=msg_id, **kw)
    before, ops = len(host.relayed), len(host.kv.calls)
    run(app.dispatch(env, {}, http_client=None))
    host.kv_ops.append(len(host.kv.calls) - ops)
    return host.relayed[before] if len(host.relayed) > before else None


def yielding_kv(mp: pytest.MonkeyPatch) -> None:
    """Make every kv facade call a suspension point so concurrent dispatches really interleave."""
    real_get, real_set, real_inc = community_kv.get, community_kv.set, community_kv.increment

    async def get(c: str | None, k: str) -> bytes | None:
        await asyncio.sleep(0)
        return await real_get(c, k)

    async def set_(c: str | None, k: str, v: bytes, ttl: int = 0) -> None:
        await asyncio.sleep(0)
        await real_set(c, k, v, ttl)

    async def inc(c: str | None, k: str, d: int, ttl: int = 0) -> int:
        await asyncio.sleep(0)
        return await real_inc(c, k, d, ttl)

    mp.setattr(app.community_kv, "get", get)
    mp.setattr(app.community_kv, "set", set_)
    mp.setattr(app.community_kv, "increment", inc)
