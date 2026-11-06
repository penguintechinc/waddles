"""Host-native tests for the Wix full bundle -- fake `wit_world` (kv/identity/http/clock/relay/log)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Iterator
from typing import Any, cast

import pytest
from app import (
    _DENIED,
    _NOT_CONFIGURED,
    _UNAVAILABLE,
    _USAGE,
    STATUS_TTL_SECONDS,
    WIX_MEMBER_URL,
    dispatch as _app_dispatch,
    transform as _app_transform,
)
from waddle_sdk.flask_core import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeIdentityHost, FakeKvHost, StageGuard, install_fake_kv_host

ALICE = "11111111-2222-3333-4444-555555555555"
BOB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
WIX_A = "99999999-8888-7777-6666-555555555555"
WIX_B = "01234567-89ab-cdef-0123-456789abcdef"


#: Stage-faithful fakes: identity is served to `transform` only, relay to `dispatch` only; a call
#: from the wrong stage is recorded and fails the test (asserted in the `host` fixture teardown).
GUARD = StageGuard()
transform = GUARD.process_stage(_app_transform)
dispatch = GUARD.action_stage(_app_dispatch)


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
def host(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Host]:
    GUARD.reset()
    kv = install_fake_kv_host(monkeypatch)
    ident = FakeIdentityHost(members={ALICE, BOB}, actor=ALICE)
    state = _Host(kv, ident)
    wit = sys.modules["wit_world"]
    wit.imports.identity = types.SimpleNamespace(
        resolve_actor=GUARD.process_only("identity.resolve_actor", ident.resolve_actor),
        resolve_mention=GUARD.process_only("identity.resolve_mention", ident.resolve_mention),
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

    wit.imports.clock = types.SimpleNamespace(now_millis=lambda: state.now)
    wit.imports.relay = types.SimpleNamespace(
        push=GUARD.action_only(
            "relay.push", lambda p, m: state.relay_calls.append((p, json.loads(m)))
        )
    )
    wit.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: state.log_calls.append((lvl, msg, fields)),
    )
    yield state
    GUARD.assert_clean()


def _say(host: _Host, text: str, *, community: str | None = "c1", **badges: Any) -> str:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1", **badges}
    event = PlatformEvent("discord", "chat.message", "raw-handle", payload, "2026-11-01T00:00:00Z")
    with bundle_context(tenant="t", community=community, app_id="waddles.core.integration.wix"):
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
        long = PlatformEvent("discord", "chat.message", "a", {"text": "!wix " + "x" * 2000}, "t")
        assert _run(transform(long)) is None


def test_usage(host: _Host) -> None:
    assert _say(host, "!wix wat") == _USAGE
    assert _say(host, "!wix bogus") == _USAGE
    assert host.http_calls == []


def test_link_then_status_roundtrip(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    assert _say(host, f"!wix set {{user:tokA}} {WIX_A.upper()}", **MOD).startswith("Linked")
    assert (
        host.kv.store[next(k for k in host.kv.store if k.endswith(f"wix.link.{ALICE}"))]
        == WIX_A.encode()
    )
    host.wix(member_status="PENDING")
    assert _say(host, "!wix") == "Your Wix membership status: pending"
    req = host.http_calls[0]
    assert req.url == WIX_MEMBER_URL + WIX_A
    assert req.method == "GET"
    assert req.body is None
    assert sorted(n for n, _ in req.secret_refs) == ["Authorization", "wix-site-id"]
    assert sorted(v for _, v in req.secret_refs) == ["WIX_API_KEY", "WIX_SITE_ID"]


def test_status_is_cached_with_ttl(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!wix set {{user:tokA}} {WIX_A}", **MOD)
    host.wix()
    assert _say(host, "!wix status").endswith("approved")
    assert _say(host, "!wix status").endswith("approved")
    assert len(host.http_calls) == 1
    sets = [c for c in host.kv.calls if c[0] == "set" and "wix.status" in c[1][0]]
    assert sets[0][1][2] == STATUS_TTL_SECONDS


def test_relink_invalidates_status_cache(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!wix set {{user:tokA}} {WIX_A}", **MOD)
    host.wix(member_status="APPROVED")
    _say(host, "!wix")
    _say(host, f"!wix set {{user:tokA}} {WIX_B}", **MOD)
    host.wix(member_status="BLOCKED")
    assert _say(host, "!wix").endswith("blocked")
    assert len(host.http_calls) == 2


def test_status_unlinked(host: _Host) -> None:
    assert "aren't linked" in _say(host, "!wix")
    host.ident.add_mention("tokB", BOB)
    assert "isn't linked" in _say(host, "!wix status {user:tokB}", **MOD)


def test_status_other_needs_mod(host: _Host) -> None:
    host.ident.add_mention("tokB", BOB)
    assert _say(host, "!wix status {user:tokB}") == _DENIED
    assert _say(host, "!wix status {user:tokB}", is_mod="true") == _DENIED
    assert host.ident.calls == []


def test_status_other_as_mod(host: _Host) -> None:
    host.ident.add_mention("tokB", BOB)
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!wix set {{user:tokB}} {WIX_B}", is_broadcaster=True)
    host.wix(member_status="OFFLINE")
    assert (
        _say(host, "!wix status {user:tokB}", **MOD)
        == "That member's Wix membership status: offline"
    )


def test_link_validation_and_denial(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    assert _say(host, f"!wix set {{user:tokA}} {WIX_A}") == _DENIED
    assert "must be a UUID" in _say(host, "!wix set {user:tokA} not-a-uuid", **MOD)
    assert "must be a UUID" in _say(host, "!wix set {user:tokA} " + WIX_A.replace("-", ""), **MOD)
    assert _say(host, f"!wix set {WIX_A}", **MOD) == _USAGE
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
    assert message in _say(host, f"!wix set {{user:tokX}} {WIX_A}", **MOD)


def test_not_a_member_target(host: _Host) -> None:
    host.ident.add_mention("tokX", "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert "isn't a member" in _say(host, f"!wix set {{user:tokX}} {WIX_A}", **MOD)


def test_actor_resolution_errors(host: _Host) -> None:
    host.ident.actor = None
    assert "isn't linked to Waddles" in _say(host, "!wix")
    host.ident.granted = False
    assert _say(host, "!wix") == _UNAVAILABLE
    assert "wix.identity_error" in host.logs_text()
    host.ident.add_mention("t", BOB)
    assert _say(host, f"!wix set {{user:t}} {WIX_A}", **MOD) == _UNAVAILABLE


def test_unlink_self_and_other(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    host.ident.add_mention("tokB", BOB)
    assert _say(host, "!wix remove") == "No Wix link to remove."
    _say(host, f"!wix set {{user:tokA}} {WIX_A}", **MOD)
    _say(host, f"!wix set {{user:tokB}} {WIX_B}", **MOD)
    assert _say(host, "!wix remove") == "Wix link removed."
    assert _say(host, "!wix remove {user:tokB}") == _DENIED
    assert _say(host, "!wix remove {user:tokB}", **MOD) == "Wix link removed."
    assert not any("wix.link" in k for k in host.kv.store)
    assert _say(host, "!wix remove {user:tokB}", **MOD) == "No Wix link to remove."
    assert _say(host, "!wix remove", **MOD) == "No Wix link to remove."


def test_unlink_target_error_propagates(host: _Host) -> None:
    assert "couldn't find" in _say(host, "!wix remove {user:ghost}", **MOD)


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
    _say(host, f"!wix set {{user:tokA}} {WIX_A}", **MOD)
    host.http_responses.append(response)
    assert expected in _say(host, "!wix")
    assert not any("wix.status" in k for k in host.kv.store)
    assert any(lvl in (0, 2) for lvl, _m, _f in host.log_calls)


def test_wix_pii_never_stored_or_logged(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!wix set {{user:tokA}} {WIX_A}", **MOD)
    host.wix()
    reply = _say(host, "!wix")
    stored = b" ".join(host.kv.store.values()).decode()
    for blob in (reply, stored, host.logs_text()):
        assert "leak@example.com" not in blob
        assert "raw-handle" not in blob
    assert WIX_A not in host.logs_text()
    assert ALICE not in host.logs_text()


def test_kv_failure_replies_unavailable(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    wit = sys.modules["wit_world"]

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("kv down")

    for op in ("get", "set", "delete"):
        monkeypatch.setattr(wit.imports.kv, op, boom)
    assert _say(host, "!wix") == _UNAVAILABLE
    host.ident.add_mention("tokA", ALICE)
    assert _say(host, f"!wix set {{user:tokA}} {WIX_A}", **MOD) == _UNAVAILABLE
    assert _say(host, "!wix remove") == _UNAVAILABLE
    assert "wix.kv_error" in host.logs_text()


def test_kv_set_and_delete_failures_individually(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    wit = sys.modules["wit_world"]
    host.ident.add_mention("tokA", ALICE)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("kv down")

    monkeypatch.setattr(wit.imports.kv, "set", boom)
    assert _say(host, f"!wix set {{user:tokA}} {WIX_A}", **MOD) == _UNAVAILABLE
    monkeypatch.undo()


def test_corrupt_stored_state_fails_closed(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!wix set {{user:tokA}} {WIX_A}", **MOD)
    link_key = next(k for k in host.kv.store if "wix.link" in k)
    for bad in (b"\xff", b"not-a-uuid"):
        host.kv.store[link_key] = bad
        assert _say(host, "!wix") == _UNAVAILABLE


def test_cached_status_garbage_is_refetched(host: _Host) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!wix set {{user:tokA}} {WIX_A}", **MOD)
    key = f"c.c1.wix.status.{ALICE}"
    host.kv.store[key] = b"\xff\xfe"
    host.wix(member_status="APPROVED")
    assert _say(host, "!wix").endswith("approved")
    assert len(host.http_calls) == 1


def _env(payload: dict[str, Any]) -> StageEnvelope:
    ev = PlatformEvent("discord", "chat.message", "a", payload, "t")
    return StageEnvelope("t", "c", "waddles.core.integration.wix", "action", ev, "ts")


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
    assert "couldn't find" in _say(host, "!wix status {user:ghost}", **MOD)


def test_kv_delete_failure_on_unlink(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.ident.add_mention("tokA", ALICE)
    _say(host, f"!wix set {{user:tokA}} {WIX_A}", **MOD)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("kv down")

    monkeypatch.setattr(sys.modules["wit_world"].imports.kv, "delete", boom)
    assert _say(host, "!wix remove") == _UNAVAILABLE


@pytest.mark.parametrize("module", ["roles", "events"])
def test_feature_modules_default_off_and_refuse_without_capability(
    host: _Host, module: str
) -> None:
    reply = _say(host, f"!wix enable {module}", **MOD)
    assert "can't be enabled" in reply
    assert not any(f"wix.module.{module}" in k for k in host.kv.store)
    assert "wix.module_capability_missing" in host.logs_text()


@pytest.mark.parametrize("module", ["roles", "events"])
def test_feature_modules_enable_disable_with_capability(host: _Host, module: str) -> None:
    sys.modules["wit_world"].imports.__dict__["tool"] = object()
    assert _say(host, f"!wix enable {module}", **MOD) == f"Wix {module} module enabled."
    assert any(k.endswith(f"wix.module.{module}") for k in host.kv.store)
    assert _say(host, f"!wix disable {module}", **MOD) == f"Wix {module} module disabled."
    assert not any(f"wix.module.{module}" in k for k in host.kv.store)


def test_feature_module_commands_need_mod_and_valid_name(host: _Host) -> None:
    assert _say(host, "!wix enable roles") == _DENIED
    assert _say(host, "!wix disable events", is_mod="true") == _DENIED
    assert _say(host, "!wix enable nope", **MOD) == _USAGE
    assert _say(host, "!wix enable", **MOD) == _USAGE


def test_capability_check_without_wit_world(monkeypatch: pytest.MonkeyPatch) -> None:
    import event_sync
    import plan_roles

    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert plan_roles.capability_available() is False
    assert event_sync.capability_available() is False
