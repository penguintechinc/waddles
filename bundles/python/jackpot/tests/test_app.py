"""Host-native tests: fake `wit_world` (kv/flags/relay/log + SDK economy/identity fakes)."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import re
import sys
import types
from typing import Any

import pytest
from waddle_sdk import community_kv, event_dedupe
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeEconomyHost, FakeIdentityHost, FakeKvHost, rfc3339_from_millis

import app
from app import BASE_POT, COOLDOWN_SECONDS, dispatch, transform

COMM = "comm-1"
ACTOR_UUID = "11111111-1111-4111-8111-111111111111"
POT_KEY = _scoped_key(COMM, "jackpot.pot")


_MSG_IDS = itertools.count(1)
_CURRENT_HOST: list[Any] = []


def _next_msg_id() -> str:
    """A fresh platform message id per delivery, like the real pipeline."""
    return f"msg-{next(_MSG_IDS)}"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _now_stamp(offset_seconds: int = 0) -> str:
    """The ingest stamp of an event arriving NOW on the fake host clock (or that long ago/ahead)."""
    return rfc3339_from_millis(_CURRENT_HOST[-1].now_ms + offset_seconds * 1000)


class _Host:
    def __init__(self, mp: pytest.MonkeyPatch) -> None:
        self.kv = FakeKvHost()
        self.eco = FakeEconomyHost(members={ACTOR_UUID}, balances={ACTOR_UUID: 1000})
        self.eco.actor = ACTOR_UUID
        self.ident = FakeIdentityHost(members={ACTOR_UUID}, actor=ACTOR_UUID)
        self.flag = True
        self.now_ms = 1_700_000_000_000
        _CURRENT_HOST.append(self)
        self.relayed: list[str] = []
        self.logs: list[tuple[str, str]] = []
        kv, ns = self.kv, types.SimpleNamespace
        mod = types.ModuleType("wit_world")
        mod.imports = ns(  # type: ignore[attr-defined]
            flags=ns(enabled=lambda key, default_value: self.flag),
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

    @property
    def pot(self) -> int:
        return int(self.kv.store.get(POT_KEY, b"0"))

    def bal(self) -> int:
        return self.eco.balances[ACTOR_UUID]

    def expire_cooldown(self) -> None:
        for k in [k for k in self.kv.store if ".jackpot.cd." in k or k.startswith("c.")]:
            if "jackpot.cd." in k:
                del self.kv.store[k]


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    return _Host(monkeypatch)


def _env(command: str, arg: str | None = None, msg_id: str | None = None) -> StageEnvelope:
    payload: dict[str, Any] = {
        "command": command,
        "channel_id": "123",
        "message_id": msg_id or _next_msg_id(),
    }
    if arg is not None:
        payload["arg"] = arg
    return StageEnvelope(
        tenant="t",
        community=COMM,
        app_id="waddles.core.example.jackpot",
        stage="action",
        event=PlatformEvent("twitch", "chat.message", "viewer-1", payload, _now_stamp()),
        ts="2026-10-21T00:00:00Z",
    )


def _say(host: _Host, command: str, arg: str | None = None, msg_id: str | None = None) -> str:
    _run(dispatch(_env(command, arg, msg_id), {}, http_client=None))
    return host.relayed[-1]


def _force(mp: pytest.MonkeyPatch, win: bool) -> None:
    mp.setattr(app, "_roll_win", lambda: win)


def test_status_shows_base_pot(host: _Host) -> None:
    assert f"{BASE_POT} points" in _say(host, "status")


def test_loss_accumulates_pot_and_debits(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, False)
    assert f"{BASE_POT + 50}" in _say(host, "play", "50")
    assert (host.pot, host.bal()) == (50, 950)
    host.expire_cooldown()
    assert f"{BASE_POT + 80}" in _say(host, "play", "30")
    assert (host.pot, host.bal()) == (80, 920)
    assert f"{BASE_POT + 80} points" in _say(host, "status")


def test_win_pays_whole_pot_and_resets(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, False)
    _say(host, "play", "50")
    host.expire_cooldown()
    _force(monkeypatch, True)
    reply = _say(host, "play", "10")
    assert "JACKPOT" in reply and f"wins {BASE_POT + 50} points" in reply
    assert host.pot == 0  # reset to base
    assert host.bal() == 950 + BASE_POT + 50  # stake returned + whole pot
    assert f"{BASE_POT} points" in _say(host, "status")


def test_win_on_empty_pot_pays_base(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, True)
    _say(host, "play", "20")
    assert host.bal() == 1000 + BASE_POT and host.pot == 0


@pytest.mark.parametrize("arg", ["0", "abc", None, "-5", "1.5"])
def test_invalid_stake_replies_usage_and_moves_nothing(host: _Host, arg: str | None) -> None:
    reply = _say(host, "play", arg)
    assert "Usage" in reply or "at least" in reply
    assert (host.pot, host.bal()) == (0, 1000)


def test_insufficient_funds_fails_loud(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, True)
    assert "only have 1000" in _say(host, "play", "5000")
    assert (host.pot, host.bal()) == (0, 1000)


def test_over_max_bet_replies_cap(host: _Host) -> None:
    host.eco.balances[ACTOR_UUID] = 5000
    host.eco.max_bet_cap = 100
    assert "max stake right now is 100" in _say(host, "play", "500")
    assert host.bal() == 5000


def test_cooldown_blocks_then_allows(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, False)
    _say(host, "play", "5")
    assert "slow down" in _say(host, "play", "5")
    assert (host.pot, host.bal()) == (5, 995)
    cd = [c for c in host.kv.calls if c[0] == "increment" and ".jackpot.cd." in c[1][0]]
    assert cd and cd[0][1][2] == COOLDOWN_SECONDS
    host.expire_cooldown()
    assert "slow down" not in _say(host, "play", "5")


def test_failed_precheck_does_not_burn_cooldown(host: _Host) -> None:
    _say(host, "play", "999999")
    assert not any(".jackpot.cd." in k for k in host.kv.store)


def test_unlinked_actor_is_told_to_link(host: _Host) -> None:
    host.ident.actor = None
    assert "link your account" in _say(host, "play", "5")


def test_actor_mismatch_fails_loud_and_moves_nothing(host: _Host) -> None:
    host.eco.actor = "someone-else"  # anomalous: host-resolved actor != payer
    with pytest.raises(RuntimeError, match="jackpot wager failed"):
        _say(host, "play", "5")
    assert "temporarily unavailable" in host.relayed[-1]
    assert (host.pot, host.bal()) == (0, 1000)


def test_win_overcap_pays_cap_and_returns_excess(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, False)
    host.kv.store[POT_KEY] = b"1000"
    _force(monkeypatch, True)
    host.eco.payout_multiple = 10  # stake 20 -> payout cap 200
    _say(host, "play", "20")
    assert host.bal() == 1000 - 20 + 200
    assert host.pot == 920  # excess over the host payout cap returns to the pot


def test_wager_refusal_on_win_restores_pot(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.kv.store[POT_KEY] = b"300"
    _force(monkeypatch, True)
    host.eco.payout_multiple = 0  # cap < stake -> over-cap refusal that cannot be paid
    reply = _say(host, "play", "10")
    assert "max stake" in reply
    assert host.pot == 300 and host.bal() == 1000


def test_kv_failure_is_loud(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any) -> Any:
        raise RuntimeError("down")

    sys.modules["wit_world"].imports.kv.get = boom  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="jackpot get failed"):
        _say(host, "status")
    assert "temporarily unavailable" in host.relayed[-1]


def test_no_community_and_bad_command_raise(host: _Host) -> None:
    env = _env("status")
    env.community = None  # type: ignore[misc]
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(env, {}, http_client=None))
    with pytest.raises(ValueError, match="unrecognized"):
        _run(dispatch(_env("bogus"), {}, http_client=None))


@pytest.mark.parametrize(
    ("text", "command", "arg"),
    [("!jackpot 25", "play", "25"), ("!jackpot", "status", None), ("!jackpot nope", "usage", None)],
)
def test_transform_matches(host: _Host, text: str, command: str, arg: str | None) -> None:
    out = _run(transform(PlatformEvent("twitch", "chat.message", "v", {"text": text}, "t")))
    assert out is not None and out.payload["command"] == command
    assert out.payload.get("arg") == arg


def test_pii_free_keys_and_logs(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, False)
    _say(host, "play", "40")
    pseud = hashlib.sha256(f"{COMM}|{ACTOR_UUID}".encode()).hexdigest()
    keys = list(host.kv.store)
    assert any(pseud in k for k in keys)
    blob = " ".join(k for k in keys) + " ".join(m + fj for m, fj in host.logs)
    for raw in ("viewer-1", ACTOR_UUID):
        assert raw not in blob
    assert all(":" not in k.removeprefix("c.") for k in keys)


# --- pot CAS correctness under interleaving (kv facade yields between read and write) -----------


def _yielding_kv(mp: pytest.MonkeyPatch) -> None:
    real_get, real_inc = community_kv.get, community_kv.increment

    async def get(c: str | None, k: str) -> bytes | None:
        value = await real_get(c, k)
        await asyncio.sleep(0)
        return value

    async def inc(c: str | None, k: str, d: int, ttl: int) -> int:
        await asyncio.sleep(0)
        return await real_inc(c, k, d, ttl)

    mp.setattr(app.community_kv, "get", get)
    mp.setattr(app.community_kv, "increment", inc)


def _pot_obj() -> app._Pot:
    return app._Pot(COMM, "twitch", "123")


def test_concurrent_increments_lose_nothing(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _yielding_kv(monkeypatch)

    async def go() -> None:
        pot = _pot_obj()
        await asyncio.gather(*(pot.add(7) for _ in range(100)))

    _run(go())
    assert host.pot == 700


def test_concurrent_claims_pay_pot_exactly_once(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.kv.store[POT_KEY] = b"500"
    _yielding_kv(monkeypatch)

    async def go() -> list[int]:
        pot = _pot_obj()
        return list(await asyncio.gather(*(pot.claim_all() for _ in range(8))))

    claims = _run(go())
    assert sum(claims) == 500, claims  # no double payout, nothing lost
    assert sorted(claims, reverse=True)[0] == 500
    assert host.pot == 0  # floor restored after the negative dips


def test_claims_racing_contributions_conserve_money(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.kv.store[POT_KEY] = b"200"
    _yielding_kv(monkeypatch)

    async def go() -> int:
        pot = _pot_obj()
        res = await asyncio.gather(
            pot.claim_all(), pot.claim_all(), *(pot.add(10) for _ in range(20))
        )
        return sum(r for r in res[:2])

    taken = _run(go())
    assert taken + host.pot == 200 + 20 * 10


def test_missing_channel_id_raises(host: _Host) -> None:
    env = _env("status")
    env.event.payload.pop("channel_id")
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(env, {}, http_client=None))


def test_ambiguous_wager_backend_error_retries_identical_call(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, False)
    real = host.eco.wager
    calls: list[tuple[str, int, int]] = []

    def flaky(user: str, stake: int, payout: int) -> int:
        calls.append((user, stake, payout))
        if len(calls) == 1:
            raise RuntimeError("storage down")  # classified as BackendError by the facade
        return real(user, stake, payout)

    sys.modules["wit_world"].imports.economy.wager = flaky  # type: ignore[attr-defined]
    assert "join the pot" in _say(host, "play", "10")
    assert len(calls) == 2 and calls[0] == calls[1]
    assert host.pot == 10


def test_win_backend_error_twice_fails_loud_without_restoring(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.kv.store[POT_KEY] = b"300"
    _force(monkeypatch, True)

    def down(user: str, stake: int, payout: int) -> int:
        raise RuntimeError("storage down")

    sys.modules["wit_world"].imports.economy.wager = down  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="jackpot wager failed"):
        _say(host, "play", "10")
    assert host.pot == 0  # ambiguous commit: never risk a double payout
    assert host.bal() == 1000


def test_cooldown_kv_failure_is_loud(host: _Host) -> None:
    real = host.kv.increment

    def inc(key: str, delta: int, ttl: int) -> int:
        if "jackpot.cd." in key:
            raise RuntimeError("down")
        return real(key, delta, ttl)

    sys.modules["wit_world"].imports.kv.increment = inc  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="jackpot cooldown failed"):
        _say(host, "play", "5")
    assert host.bal() == 1000


# hardening: output sanitizer / activation-only
ZWSP = "\u200b"


def _say_as(host: _Host, actor: str | None, arg: str | None = None) -> str:
    payload: dict[str, Any] = {
        "command": "play",
        "channel_id": "123",
        "message_id": _next_msg_id(),
    }
    if arg is not None:
        payload["arg"] = arg
    env = StageEnvelope(
        tenant="t",
        community=COMM,
        app_id="waddles.core.example.jackpot",
        stage="action",
        event=PlatformEvent("twitch", "chat.message", actor, payload, _now_stamp()),
        ts="2026-10-21T00:00:00Z",
    )
    _run(dispatch(env, {}, http_client=None))
    return host.relayed[-1]


def test_minted_actor_token_stays_live_in_the_reply(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline's own `{user:<token>}` actor must still resolve to a display name."""
    _force(monkeypatch, False)
    assert _say_as(host, "{user:tok-actor}", "50").startswith(
        "\U0001f3b0 {user:tok-actor} missed -- 50 points"
    )


@pytest.mark.parametrize(
    ("actor", "label"),
    [
        ("evil@everyone", "evil@" + ZWSP + "everyone"),
        ("{user:handle:victim} <@123>", "\\{user:handle:victim\\} <@" + ZWSP + "123>"),
        ("{user:tok-1} @here", "\\{user:tok-1\\} @" + ZWSP + "here"),
    ],
)
def test_raw_actor_cannot_forge_mentions(
    host: _Host, monkeypatch: pytest.MonkeyPatch, actor: str, label: str
) -> None:
    _force(monkeypatch, False)
    assert _say_as(host, actor, "50").startswith("\U0001f3b0 " + label + " missed -- 50 points")


def test_missing_actor_falls_back_to_someone(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, False)
    assert _say_as(host, None, "50").startswith("\U0001f3b0 someone missed -- 50 points")


def test_transform_ignores_nonmatch_and_non_string(host: _Host) -> None:
    ev = PlatformEvent("twitch", "chat.message", "v", {"text": "hello"}, "t")
    assert _run(transform(ev)) is None
    assert _run(transform(PlatformEvent("t", "chat.message", "v", {"text": 5}, "t"))) is None


# hardening: activation-only
def test_runs_on_activation_alone_when_flag_backend_says_off(host: _Host) -> None:
    """Activation-gated only: an OFF `waddles.command-jackpot` flag must not drop the command."""
    host.flag = False
    out = _run(transform(PlatformEvent("twitch", "chat.message", "v", {"text": "!jackpot 5"}, "t")))
    assert out is not None


# hardening: replay safety (at-most-once on pipeline redelivery)
def _wagers(host: _Host) -> int:
    return sum(1 for kind, _user, _delta in host.eco.ledger if kind == "wager")


def test_transform_forwards_the_platform_message_id(host: _Host) -> None:
    out = _run(
        transform(
            PlatformEvent(
                "twitch", "chat.message", "v", {"text": "!jackpot 5", "message_id": "m-9"}, "t"
            )
        )
    )
    assert out is not None and out.payload["message_id"] == "m-9"


@pytest.mark.parametrize("bad", [None, 123, ["m"], {"id": "m"}])
def test_transform_does_not_forward_a_non_string_message_id(host: _Host, bad: Any) -> None:
    ev = PlatformEvent(
        "twitch", "chat.message", "v", {"text": "!jackpot 5", "message_id": bad}, "t"
    )
    out = _run(transform(ev))
    assert out is not None and "message_id" not in out.payload


def test_redelivered_loss_credits_the_pot_once(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regression: a redelivered losing play double-credited the pot (a mint on the next win)."""
    _force(monkeypatch, False)
    assert "join the pot" in _say(host, "play", "50", msg_id="dup-1")
    assert (host.pot, host.bal(), _wagers(host)) == (50, 950, 1)
    sent = len(host.relayed)
    for _ in range(3):
        host.expire_cooldown()  # the redelivery arrives after the cooldown window
        _say_dup = _env("play", "50", msg_id="dup-1")
        _run(dispatch(_say_dup, {}, http_client=None))
    assert (host.pot, host.bal(), _wagers(host)) == (50, 950, 1)
    assert len(host.relayed) == sent, "a redelivery must not send a second reply"


def test_redelivery_inside_the_cooldown_window_changes_nothing(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, False)
    _say(host, "play", "50", msg_id="dup-2")
    sent = len(host.relayed)
    cooldown_incs = sum(
        1 for c in host.kv.calls if c[0] == "increment" and ".jackpot.cd." in c[1][0]
    )
    _run(dispatch(_env("play", "50", msg_id="dup-2"), {}, http_client=None))
    assert (host.pot, host.bal(), _wagers(host)) == (50, 950, 1)
    assert len(host.relayed) == sent
    assert cooldown_incs == sum(
        1 for c in host.kv.calls if c[0] == "increment" and ".jackpot.cd." in c[1][0]
    ), "a skipped redelivery must not touch (and so re-arm) the actor's cooldown"


def test_redelivered_win_does_not_pay_twice(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.kv.store[POT_KEY] = b"300"
    _force(monkeypatch, True)
    assert "JACKPOT" in _say(host, "play", "10", msg_id="win-1")
    after_win = (host.pot, host.bal(), _wagers(host))
    assert after_win == (0, 1000 + BASE_POT + 300, 1)
    host.expire_cooldown()
    _run(dispatch(_env("play", "10", msg_id="win-1"), {}, http_client=None))
    assert (host.pot, host.bal(), _wagers(host)) == after_win


def test_redelivery_that_would_roll_differently_is_still_skipped(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-roll on redelivery must never get a second outcome for the same message."""
    host.kv.store[POT_KEY] = b"300"
    _force(monkeypatch, False)
    _say(host, "play", "10", msg_id="flip-1")
    snapshot = (host.pot, host.bal(), _wagers(host))
    host.expire_cooldown()
    _force(monkeypatch, True)  # the redelivered roll would have won the whole pot
    _run(dispatch(_env("play", "10", msg_id="flip-1"), {}, http_client=None))
    assert (host.pot, host.bal(), _wagers(host)) == snapshot


def test_distinct_messages_from_one_actor_each_process(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, False)
    for i in range(5):
        host.expire_cooldown()
        _say(host, "play", "10", msg_id=f"distinct-{i}")
    assert (host.pot, host.bal(), _wagers(host)) == (50, 950, 5)


def test_redelivery_is_recognised_after_other_plays_in_between(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, False)
    _say(host, "play", "10", msg_id="old-1")
    for i in range(10):
        host.expire_cooldown()
        _say(host, "play", "10", msg_id=f"between-{i}")
    snapshot = (host.pot, host.bal(), _wagers(host))
    host.expire_cooldown()
    _run(dispatch(_env("play", "10", msg_id="old-1"), {}, http_client=None))
    assert (host.pot, host.bal(), _wagers(host)) == snapshot


def test_the_claim_is_recorded_before_the_mutation(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At-most-once: a crash mid-settlement is never replayed into a second pot credit."""
    _force(monkeypatch, False)

    def down(user: str, stake: int, payout: int) -> int:
        raise RuntimeError("storage down")  # ambiguous BackendError, twice -> fail loud

    real = sys.modules["wit_world"].imports.economy.wager  # type: ignore[attr-defined]
    sys.modules["wit_world"].imports.economy.wager = down  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="jackpot wager failed"):
        _say(host, "play", "10", msg_id="crash-1")
    sys.modules["wit_world"].imports.economy.wager = real  # type: ignore[attr-defined]
    host.expire_cooldown()
    sent = len(host.relayed)
    _run(dispatch(_env("play", "10", msg_id="crash-1"), {}, http_client=None))
    assert (host.pot, host.bal(), _wagers(host)) == (0, 1000, 0)
    assert len(host.relayed) == sent


@pytest.mark.parametrize("missing", [None, "", "   ", 12345])
def test_a_play_without_a_message_id_fails_loud_and_moves_nothing(
    host: _Host, monkeypatch: pytest.MonkeyPatch, missing: Any
) -> None:
    _force(monkeypatch, False)
    env = _env("play", "10")
    if missing is None:
        env.event.payload.pop("message_id")
    else:
        env.event.payload["message_id"] = missing
    with pytest.raises(RuntimeError, match="jackpot event_id failed: EventIdError"):
        _run(dispatch(env, {}, http_client=None))
    assert "temporarily unavailable" in host.relayed[-1]
    assert (host.pot, host.bal(), _wagers(host)) == (0, 1000, 0)
    assert not host.kv.store, "nothing may be written without a replay-stable id"
    assert any("jackpot.error" in m and "EventIdError" in fj for m, fj in host.logs)


def test_status_and_usage_need_no_message_id(host: _Host) -> None:
    for command in ("status", "usage"):
        env = _env(command)
        env.event.payload.pop("message_id")
        _run(dispatch(env, {}, http_client=None))
    assert len(host.relayed) == 2


def _yielding_kv_all(mp: pytest.MonkeyPatch) -> None:
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


@pytest.mark.parametrize("copies", [2, 6])
def test_concurrent_duplicate_deliveries_process_exactly_once(
    host: _Host, monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    """Two consumers holding the same stream entry at once must not both reach the wager."""
    _force(monkeypatch, False)
    _yielding_kv_all(monkeypatch)

    async def go() -> None:
        await asyncio.gather(
            *(
                dispatch(_env("play", "50", msg_id="race-1"), {}, http_client=None)
                for _ in range(copies)
            )
        )

    _run(go())
    assert (host.pot, host.bal(), _wagers(host)) == (50, 950, 1)


def _markers(host: _Host) -> list[str]:
    return [k for k in host.kv.store if ".jackpot.seen." in k]


def test_dedupe_kv_failure_is_loud(host: _Host) -> None:
    real = host.kv.get

    def get(key: str) -> bytes | None:
        if ".jackpot.seen." in key:
            raise RuntimeError("down")
        return real(key)

    sys.modules["wit_world"].imports.kv.get = get  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="jackpot dedupe failed"):
        _say(host, "play", "5")
    assert (host.pot, host.bal(), _wagers(host)) == (0, 1000, 0)


def test_a_full_key_quota_fails_closed_and_moves_no_money(host: _Host) -> None:
    """The host refuses a NEW key once 10,000 are live: the claim raises, nothing settles."""
    real = host.kv.increment

    def inc(key: str, delta: int, ttl: int) -> int:
        if ".jackpot.seen." in key:
            raise RuntimeError("quota exceeded")
        return real(key, delta, ttl)

    sys.modules["wit_world"].imports.kv.increment = inc  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="jackpot dedupe failed"):
        _say(host, "play", "5", msg_id="q-1")
    assert (host.pot, host.bal(), _wagers(host)) == (0, 1000, 0)
    assert "temporarily unavailable" in host.relayed[-1]


@pytest.mark.parametrize("bad", [b"not-a-number", b"-5"])
def test_a_corrupt_event_marker_never_lets_the_event_settle_again(
    host: _Host, monkeypatch: pytest.MonkeyPatch, bad: bytes
) -> None:
    """The peek sees an existing marker and skips; if the peek were raced past, `claim` raises."""
    _force(monkeypatch, False)
    _say(host, "play", "5", msg_id="corrupt-1")
    (key,) = _markers(host)
    host.kv.store[key] = bad
    host.expire_cooldown()
    snapshot, sent = (host.pot, host.bal(), _wagers(host)), len(host.relayed)
    _run(dispatch(_env("play", "5", msg_id="corrupt-1"), {}, http_client=None))
    assert (host.pot, host.bal(), _wagers(host)) == snapshot and len(host.relayed) == sent

    async def peek_misses(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(app.event_dedupe, "already_seen", peek_misses)
    host.expire_cooldown()
    with pytest.raises(RuntimeError, match="jackpot dedupe failed"):
        _run(dispatch(_env("play", "5", msg_id="corrupt-1"), {}, http_client=None))
    assert (host.pot, host.bal(), _wagers(host)) == snapshot


def test_dedupe_state_is_one_pii_free_marker_per_event(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, False)
    for i in range(6):
        host.expire_cooldown()
        _say(host, "play", "5", msg_id=f"private-platform-id-{i}")
    assert len(_markers(host)) == 6, "exactly one marker per processed event"
    for key in _markers(host):
        assert re.fullmatch(rf"c\.{COMM}\.jackpot\.seen\.[0-9a-f]{{32}}", key)
    blob = " ".join(host.kv.store) + " ".join(v.decode() for v in host.kv.store.values())
    assert "private-platform-id" not in blob
    assert "private-platform-id" not in " ".join(m + fj for m, fj in host.logs)


def test_a_redelivery_older_than_any_ring_horizon_is_still_skipped(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regression: the last-16-ids ring forgot an event after 16 later plays (pot double-credit)."""
    _force(monkeypatch, False)
    _say(host, "play", "5", msg_id="oldest")
    for i in range(40):
        host.expire_cooldown()
        _say(host, "play", "5", msg_id=f"later-{i}")
    snapshot, sent = (host.pot, host.bal(), _wagers(host)), len(host.relayed)
    for win in (False, True):  # even a redelivery that would now roll the other way
        _force(monkeypatch, win)
        host.expire_cooldown()
        _run(dispatch(_env("play", "5", msg_id="oldest"), {}, http_client=None))
    assert (host.pot, host.bal(), _wagers(host)) == snapshot and len(host.relayed) == sent


@pytest.mark.parametrize("copies", [2, 6])
def test_concurrent_duplicates_process_once_even_with_no_cooldown_gate(
    host: _Host, monkeypatch: pytest.MonkeyPatch, copies: int
) -> None:
    """regression: the ring was a get-then-set (TOCTOU) only safe behind the cooldown CAS.

    With the cooldown gate out of the picture only the atomic per-event `increment` can let
    exactly one copy through.
    """
    _force(monkeypatch, False)
    _yielding_kv_all(monkeypatch)

    async def always_allowed(self: Any, pseudonym: str) -> bool:
        return True

    monkeypatch.setattr(app._Pot, "cooldown_claim", always_allowed)

    async def go() -> None:
        await asyncio.gather(
            *(
                dispatch(_env("play", "50", msg_id="race-2"), {}, http_client=None)
                for _ in range(copies)
            )
        )

    _run(go())
    assert (host.pot, host.bal(), _wagers(host)) == (50, 950, 1)
    assert len(_markers(host)) == 1


def test_the_claim_is_one_ttl_increment_on_a_per_event_marker(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, False)
    _say(host, "play", "5", msg_id="m-ttl")
    marker_calls = [c for c in host.kv.calls if ".jackpot.seen." in c[1][0]]
    assert [c[0] for c in marker_calls] == ["get", "increment"]  # advisory peek, then the claim
    assert marker_calls[1][1][1:] == (1, event_dedupe.SEEN_TTL_SECONDS)


def test_an_actor_on_cooldown_never_creates_a_marker(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One chatter's marker rate is bounded by its cooldown: refused attempts mint no keys."""
    _force(monkeypatch, False)
    _say(host, "play", "5")
    assert len(_markers(host)) == 1
    for _ in range(20):
        assert "slow down" in _say(host, "play", "5")
    assert len(_markers(host)) == 1


@pytest.mark.parametrize("age", [event_dedupe.MAX_EVENT_AGE_SECONDS + 1, 3600, 86_400 * 7])
def test_an_event_too_old_to_dedupe_is_dropped_silently_and_moves_nothing(
    host: _Host, monkeypatch: pytest.MonkeyPatch, age: int
) -> None:
    """The marker TTL is the horizon, so an event past the age fence must never be processed."""
    _force(monkeypatch, False)
    env = _env("play", "5")
    env.event.occurred_at = _now_stamp(-age)
    _run(dispatch(env, {}, http_client=None))
    assert host.relayed == [] and not host.kv.store
    assert (host.pot, host.bal(), _wagers(host)) == (0, 1000, 0)
    assert any("jackpot.stale_event_skipped" in m for m, _fj in host.logs)


def test_an_event_at_the_age_limit_is_still_processed(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, False)
    env = _env("play", "5")
    env.event.occurred_at = _now_stamp(-event_dedupe.MAX_EVENT_AGE_SECONDS)
    _run(dispatch(env, {}, http_client=None))
    assert "join the pot" in host.relayed[-1]


def test_a_replay_after_the_marker_lapsed_is_dropped_by_the_fence(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outage > marker TTL: the marker is gone, but the event is too old to be re-processed."""
    _force(monkeypatch, False)
    first = _env("play", "5", msg_id="late-1")
    _run(dispatch(first, {}, http_client=None))
    for key in _markers(host):
        del host.kv.store[key]  # the fake ignores TTL: the host expired the marker
    host.expire_cooldown()
    host.now_ms += (event_dedupe.SEEN_TTL_SECONDS + 1) * 1000
    snapshot, sent = (host.pot, host.bal(), _wagers(host)), len(host.relayed)
    _run(dispatch(first, {}, http_client=None))  # same envelope, same ingest stamp, much later
    assert (host.pot, host.bal(), _wagers(host)) == snapshot and len(host.relayed) == sent


@pytest.mark.parametrize("stamp", ["", "   ", "yesterday", "2026-10-21T00:00:00", None, 12345])
def test_a_missing_or_malformed_occurred_at_fails_loud_and_moves_nothing(
    host: _Host, stamp: Any
) -> None:
    env = _env("play", "5")
    env.event.occurred_at = stamp
    with pytest.raises(RuntimeError, match="jackpot event_id failed: EventIdError"):
        _run(dispatch(env, {}, http_client=None))
    assert "temporarily unavailable" in host.relayed[-1]
    assert (host.pot, host.bal(), _wagers(host)) == (0, 1000, 0)
    assert not host.kv.store


def test_a_future_dated_event_fails_loud_and_moves_nothing(host: _Host) -> None:
    env = _env("play", "5")
    env.event.occurred_at = _now_stamp(event_dedupe.EVENT_CLOCK_SKEW_SECONDS + 60)
    with pytest.raises(RuntimeError, match="jackpot event_id failed: EventIdError"):
        _run(dispatch(env, {}, http_client=None))
    assert (host.pot, host.bal(), _wagers(host)) == (0, 1000, 0) and not host.kv.store
