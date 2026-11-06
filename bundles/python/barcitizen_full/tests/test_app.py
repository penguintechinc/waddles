"""Host-native tests for the Bar Citizen full bundle -- fake `wit_world` (kv/identity/http/clock/relay/log)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any, cast

import pytest
from app import (
    _DENIED,
    _NOT_CONFIGURED,
    _UNAVAILABLE,
    _UPGRADE,
    _USAGE,
    MAX_ANNOUNCE_LEN,
    NEWS_CAP,
    STATUS_TTL_SECONDS,
    WIX_MEMBER_URL,
    dispatch,
    transform,
)
from waddle_sdk.flask_core import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeIdentityHost, FakeKvHost, install_fake_kv_host

ALICE = "11111111-2222-3333-4444-555555555555"
BOB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
WIX_A = "99999999-8888-7777-6666-555555555555"
WIX_B = "01234567-89ab-cdef-0123-456789abcdef"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    """Fake host: kv, identity, scriptable Wix http responses, recorded relay/log."""

    def __init__(self, kv: FakeKvHost, ident: FakeIdentityHost) -> None:
        self.kv = kv
        self.ident = ident
        self.http_calls: list[Any] = []
        self.http_responses: list[Any] = []
        self.relay_calls: list[tuple[str, dict[str, Any]]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.now = 1_790_000_000_000
        self.tier: str | None = "professional"

    def logs_text(self) -> str:
        return " ".join(f"{m} {f}" for _l, m, f in self.log_calls)

    def wix(self, status: int = 200, member_status: str | None = "APPROVED", **kw: Any) -> None:
        body = kw.get("body")
        if body is None:
            body = json.dumps(
                {
                    "member": {
                        "id": WIX_A,
                        "status": member_status,
                        "contact": {"email": "leak@example.com"},
                    }
                }
            ).encode()
        self.http_responses.append(("ok", status, body, kw.get("truncated", False)))


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    kv = install_fake_kv_host(monkeypatch)
    ident = FakeIdentityHost(members={ALICE, BOB}, actor=ALICE)
    state = _Host(kv, ident)
    wit = sys.modules["wit_world"]
    wit.imports.identity = types.SimpleNamespace(
        resolve_actor=ident.resolve_actor, resolve_mention=ident.resolve_mention
    )

    def send(request: Any) -> Any:
        state.http_calls.append(request)
        kind, *rest = state.http_responses.pop(0)
        if kind == "err":
            raise rest[0]
        status, body, truncated = rest
        return types.SimpleNamespace(status=status, headers=[], body=body, truncated=truncated)

    wit.imports.http = types.SimpleNamespace(
        Header=lambda name, value: (name, value),
        Request=lambda **kw: types.SimpleNamespace(**kw),
        send=send,
    )

    def _tier() -> str:
        if state.tier is None:
            raise RuntimeError("license state unavailable")
        return state.tier

    wit.imports.flags = types.SimpleNamespace(enabled=lambda k, d: d, tier=_tier)
    wit.imports.clock = types.SimpleNamespace(now_millis=lambda: state.now)
    wit.imports.relay = types.SimpleNamespace(
        push=lambda p, m: state.relay_calls.append((p, json.loads(m)))
    )
    wit.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: state.log_calls.append((lvl, msg, fields)),
    )
    return state


def _say(host: _Host, text: str, *, community: str | None = "c1", **badges: Any) -> str:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1", **badges}
    event = PlatformEvent("discord", "chat.message", "raw-handle", payload, "2026-11-01T00:00:00Z")
    with bundle_context(tenant="t", community=community, app_id="waddles.core.barcitizen.full"):
        out = _run(transform(event))
    assert out is not None
    return cast(str, out.payload["text"])


MOD = {"is_mod": True}


def test_non_matching_ignored(host: _Host) -> None:
    ev = PlatformEvent("discord", "chat.message", "a", {"text": "hello"}, "t")
    with bundle_context(tenant="t", community="c", app_id="a"):
        assert _run(transform(ev)) is None
        assert (
            _run(transform(PlatformEvent("discord", "chat.message", "a", {"text": 5}, "t"))) is None
        )
        long = PlatformEvent("discord", "chat.message", "a", {"text": "!bcm " + "x" * 2000}, "t")
        assert _run(transform(long)) is None


def test_usage(host: _Host) -> None:
    assert _say(host, "!bcm wat") == _USAGE
    assert _say(host, "!bcm news extra") == _USAGE
    assert host.http_calls == []


def test_link_then_status_roundtrip(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    assert _say(host, f"!bcm link {{user:tokA}} {WIX_A.upper()}", **MOD).startswith("Linked")
    assert (
        host.kv.store[next(k for k in host.kv.store if k.endswith(f"bcf.link.{ALICE}"))]
        == WIX_A.encode()
    )
    host.wix(member_status="PENDING")
    assert _say(host, "!bcm") == "Your Wix membership status: pending"
    req = host.http_calls[0]
    assert req.url == WIX_MEMBER_URL + WIX_A
    assert req.method == "GET"
    assert req.body is None
    assert sorted(n for n, _ in req.secret_refs) == ["Authorization", "wix-site-id"]
    assert sorted(v for _, v in req.secret_refs) == ["WIX_API_KEY", "WIX_SITE_ID"]


def test_status_is_cached_with_ttl(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!bcm link {{user:tokA}} {WIX_A}", **MOD)
    host.wix()
    assert _say(host, "!bcm status").endswith("approved")
    assert _say(host, "!bcm status").endswith("approved")
    assert len(host.http_calls) == 1
    sets = [c for c in host.kv.calls if c[0] == "set" and "bcf.status" in c[1][0]]
    assert sets[0][1][2] == STATUS_TTL_SECONDS


def test_relink_invalidates_status_cache(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!bcm link {{user:tokA}} {WIX_A}", **MOD)
    host.wix(member_status="APPROVED")
    _say(host, "!bcm")
    _say(host, f"!bcm link {{user:tokA}} {WIX_B}", **MOD)
    host.wix(member_status="BLOCKED")
    assert _say(host, "!bcm").endswith("blocked")
    assert len(host.http_calls) == 2


def test_status_unlinked(host: _Host) -> None:
    assert "aren't linked" in _say(host, "!bcm")
    host.ident.add_mention("tokB", BOB)
    assert "isn't linked" in _say(host, "!bcm status {user:tokB}", **MOD)


def test_status_other_needs_mod(host: _Host) -> None:
    host.ident.add_mention("tokB", BOB)
    assert _say(host, "!bcm status {user:tokB}") == _DENIED
    assert _say(host, "!bcm status {user:tokB}", is_mod="true") == _DENIED
    assert host.ident.calls == []


def test_status_other_as_mod(host: _Host) -> None:
    host.ident.add_mention("tokB", BOB)
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!bcm link {{user:tokB}} {WIX_B}", is_broadcaster=True)
    host.wix(member_status="OFFLINE")
    assert (
        _say(host, "!bcm status {user:tokB}", **MOD)
        == "That member's Wix membership status: offline"
    )


def test_link_validation_and_denial(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    assert _say(host, f"!bcm link {{user:tokA}} {WIX_A}") == _DENIED
    assert "must be a UUID" in _say(host, "!bcm link {user:tokA} not-a-uuid", **MOD)
    assert "must be a UUID" in _say(host, "!bcm link {user:tokA} " + WIX_A.replace("-", ""), **MOD)
    assert _say(host, f"!bcm link {WIX_A}", **MOD) == _USAGE
    assert host.kv.store == {}


@pytest.mark.parametrize(
    ("setup", "message"),
    [("unlinked", "isn't linked"), ("ambiguous", "more than one"), ("unknown", "couldn't find")],
)
def test_target_resolution_errors(host: _Host, setup: str, message: str) -> None:
    if setup == "unlinked":
        host.ident.add_unlinked_mention("tokX")
    elif setup == "ambiguous":
        host.ident.add_ambiguous_mention("tokX")
    assert message in _say(host, f"!bcm link {{user:tokX}} {WIX_A}", **MOD)


def test_not_a_member_target(host: _Host) -> None:
    host.ident.add_mention("tokX", "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert "isn't a member" in _say(host, f"!bcm link {{user:tokX}} {WIX_A}", **MOD)


def test_actor_resolution_errors(host: _Host) -> None:
    host.ident.actor = None
    assert "isn't linked to Waddles" in _say(host, "!bcm")
    host.ident.granted = False
    assert _say(host, "!bcm") == _UNAVAILABLE
    assert "bcf.identity_error" in host.logs_text()
    host.ident.add_mention("t", BOB)
    assert _say(host, f"!bcm link {{user:t}} {WIX_A}", **MOD) == _UNAVAILABLE


def test_unlink_self_and_other(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    host.ident.add_mention("tokB", BOB)
    assert _say(host, "!bcm unlink") == "No Wix link to remove."
    _say(host, f"!bcm link {{user:tokA}} {WIX_A}", **MOD)
    _say(host, f"!bcm link {{user:tokB}} {WIX_B}", **MOD)
    assert _say(host, "!bcm unlink") == "Wix link removed."
    assert _say(host, "!bcm unlink {user:tokB}") == _DENIED
    assert _say(host, "!bcm unlink {user:tokB}", **MOD) == "Wix link removed."
    assert not any("bcf.link" in k for k in host.kv.store)
    assert _say(host, "!bcm unlink {user:tokB}", **MOD) == "No Wix link to remove."
    assert _say(host, "!bcm unlink", **MOD) == "No Wix link to remove."


def test_unlink_target_error_propagates(host: _Host) -> None:
    assert "couldn't find" in _say(host, "!bcm unlink {user:ghost}", **MOD)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (("ok", 401, b"", False), _NOT_CONFIGURED),
        (("ok", 403, b"", False), _NOT_CONFIGURED),
        (("ok", 404, b"", False), "Wix doesn't know"),
        (("ok", 500, b"", False), _UNAVAILABLE),
        (("ok", 200, b"{}", True), _UNAVAILABLE),
        (("ok", 200, b"not json", False), _UNAVAILABLE),
        (("ok", 200, b"\xff", False), _UNAVAILABLE),
        (("ok", 200, b'{"member": {"status": "WEIRD"}}', False), _UNAVAILABLE),
        (("ok", 200, b'{"member": {}}', False), _UNAVAILABLE),
        (("err", RuntimeError("down")), _UNAVAILABLE),
    ],
)
def test_wix_failures_are_explicit_and_not_cached(
    host: _Host, response: Any, expected: str
) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!bcm link {{user:tokA}} {WIX_A}", **MOD)
    host.http_responses.append(response)
    assert expected in _say(host, "!bcm")
    assert not any("bcf.status" in k for k in host.kv.store)
    assert any(lvl in (0, 2) for lvl, _m, _f in host.log_calls)


def test_wix_pii_never_stored_or_logged(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!bcm link {{user:tokA}} {WIX_A}", **MOD)
    host.wix()
    reply = _say(host, "!bcm")
    stored = b" ".join(host.kv.store.values()).decode()
    for blob in (reply, stored, host.logs_text()):
        assert "leak@example.com" not in blob
        assert "raw-handle" not in blob
    assert WIX_A not in host.logs_text()
    assert ALICE not in host.logs_text()


def test_announce_and_news(host: _Host) -> None:
    assert _say(host, "!bcm news") == "No announcements yet."
    assert _say(host, "!bcm announce hello") == _DENIED
    assert _say(host, "!bcm announce @everyone  meetup   tonight", **MOD).startswith(
        "Announcement: "
    )
    out = _say(host, "!bcm news")
    assert "@everyone" not in out
    assert "meetup tonight" in out
    for i in range(NEWS_CAP + 3):
        host.now += 1000
        _say(host, f"!bcm announce item{i}", **MOD)
    key = next(k for k in host.kv.store if k.endswith("bcf.news"))
    assert len(json.loads(host.kv.store[key])) == NEWS_CAP
    news = _say(host, "!bcm news")
    assert "item" + str(NEWS_CAP + 2) in news
    assert news.count("|") == 2


def test_announce_validation(host: _Host) -> None:
    assert "announcements must be" in _say(host, "!bcm announce", **MOD)
    assert "announcements must be" in _say(
        host, "!bcm announce " + "x" * (MAX_ANNOUNCE_LEN + 1), **MOD
    )
    assert "announcements must be" in _say(host, "!bcm announce a\x00b", **MOD)


def test_announce_logs_are_text_free(host: _Host) -> None:
    _say(host, "!bcm announce SECRETPLAN tonight", **MOD)
    assert "SECRETPLAN" not in host.logs_text()


def test_tenant_wide_and_per_community_isolation(host: _Host) -> None:
    _say(host, "!bcm announce one", community="c1", **MOD)
    assert _say(host, "!bcm news", community="c2") == "No announcements yet."
    _say(host, "!bcm announce two", community=None, **MOD)
    assert "two" in _say(host, "!bcm news", community=None)


def test_kv_failure_replies_unavailable(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    wit = sys.modules["wit_world"]

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("kv down")

    for op in ("get", "set", "delete"):
        monkeypatch.setattr(wit.imports.kv, op, boom)
    assert _say(host, "!bcm news") == _UNAVAILABLE
    assert _say(host, "!bcm announce hi", **MOD) == _UNAVAILABLE
    host.ident.add_mention("tokA", ALICE)
    assert _say(host, f"!bcm link {{user:tokA}} {WIX_A}", **MOD) == _UNAVAILABLE
    assert _say(host, "!bcm unlink") == _UNAVAILABLE
    assert "bcf.kv_error" in host.logs_text()


def test_kv_set_and_delete_failures_individually(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    wit = sys.modules["wit_world"]
    host.ident.add_mention("tokA", ALICE)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("kv down")

    monkeypatch.setattr(wit.imports.kv, "set", boom)
    assert _say(host, f"!bcm link {{user:tokA}} {WIX_A}", **MOD) == _UNAVAILABLE
    monkeypatch.undo()


def test_corrupt_stored_state_fails_closed(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!bcm link {{user:tokA}} {WIX_A}", **MOD)
    _say(host, "!bcm announce hi", **MOD)
    link_key = next(k for k in host.kv.store if "bcf.link" in k)
    news_key = next(k for k in host.kv.store if k.endswith("bcf.news"))
    for bad in (b"\xff", b"not-a-uuid"):
        host.kv.store[link_key] = bad
        assert _say(host, "!bcm") == _UNAVAILABLE
    for bad in (b"\xff", b"nope", b"{}", b'[{"ms": "x", "text": "t"}]'):
        host.kv.store[news_key] = bad
        assert _say(host, "!bcm news") == _UNAVAILABLE


def test_cached_status_garbage_is_refetched(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!bcm link {{user:tokA}} {WIX_A}", **MOD)
    key = f"c.c1.bcf.status.{ALICE}"
    host.kv.store[key] = b"\xff\xfe"
    host.wix(member_status="APPROVED")
    assert _say(host, "!bcm").endswith("approved")
    assert len(host.http_calls) == 1


def _env(payload: dict[str, Any]) -> StageEnvelope:
    ev = PlatformEvent("discord", "chat.message", "a", payload, "t")
    return StageEnvelope("t", "c", "waddles.core.barcitizen.full", "action", ev, "ts")


def test_dispatch_relays_lead_guarded(host: _Host) -> None:
    res = _run(dispatch(_env({"channel_id": "c1", "text": "/kick everyone"}), {}, http_client=None))
    assert res.detail == "relayed"
    sent = host.relay_calls[0][1]
    assert sent["channel"] == "c1"
    assert not sent["text"].lstrip().startswith("/")


def test_dispatch_validation(host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_env({"text": "x"}), {}, http_client=None))
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(_env({"channel_id": "c", "text": ""}), {}, http_client=None))


def test_status_other_unresolved_target(host: _Host) -> None:
    assert "couldn't find" in _say(host, "!bcm status {user:ghost}", **MOD)


def test_kv_delete_failure_on_unlink(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!bcm link {{user:tokA}} {WIX_A}", **MOD)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("kv down")

    monkeypatch.setattr(sys.modules["wit_world"].imports.kv, "delete", boom)
    assert _say(host, "!bcm unlink") == _UNAVAILABLE


@pytest.mark.parametrize(
    ("tier", "allowed"),
    [
        ("free", False),
        ("professional", True),
        ("enterprise", True),
        (None, False),
        ("bogus", False),
    ],
)
def test_professional_tier_gate(host: _Host, tier: str | None, allowed: bool) -> None:
    host.tier = tier
    reply = _say(host, "!bcm news", **MOD)
    assert (reply != _UPGRADE) is allowed or tier is None
    if tier is None:
        assert reply == _UPGRADE
        assert "bcf.tier_error" in host.logs_text()
    if not allowed:
        assert host.kv.calls == []
        assert host.http_calls == []
        assert _say(host, "!bcm announce x", **MOD) == _UPGRADE


def test_missing_flags_binding_degrades_to_free(host: _Host) -> None:
    del sys.modules["wit_world"].imports.flags
    assert _say(host, "!bcm news", **MOD) == _UPGRADE
