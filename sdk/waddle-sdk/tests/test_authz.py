"""Tests for `waddle_sdk.authz` (caller_can over the WIT authz import)."""

from __future__ import annotations

import sys
import types

import pytest

import waddle_sdk
import waddle_sdk.authz as authz
from waddle_sdk.testing import FakeAuthzHost, install_fake_authz_host


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("authz coroutine unexpectedly suspended")


def _world(monkeypatch: pytest.MonkeyPatch, imports: types.SimpleNamespace) -> None:
    world = types.ModuleType("wit_world")
    world.imports = imports  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", world)


def test_reexports_and_constants() -> None:
    assert waddle_sdk.caller_can is authz.caller_can
    assert waddle_sdk.SCOPE_ADMIN == "community:admin"
    assert waddle_sdk.SCOPE_BAN == "community:ban"
    assert waddle_sdk.SCOPE_TIMEOUT == "community:timeout"
    assert waddle_sdk.SCOPE_MODERATE == "community:moderate"
    assert waddle_sdk.SCOPE_OWNER == "community:owner"
    assert waddle_sdk.SCOPE_MEMBER == "community:member"


@pytest.mark.parametrize("scope", [authz.SCOPE_BAN, authz.SCOPE_TIMEOUT, authz.SCOPE_MODERATE])
def test_moderator_holds_moderation_scopes(monkeypatch: pytest.MonkeyPatch, scope: str) -> None:
    host = install_fake_authz_host(monkeypatch, role="moderator")
    assert _run(authz.caller_can(scope)) is True
    assert host.calls == [scope]


@pytest.mark.parametrize("scope", [authz.SCOPE_ADMIN, authz.SCOPE_OWNER])
def test_moderator_lacks_admin_and_owner(monkeypatch: pytest.MonkeyPatch, scope: str) -> None:
    install_fake_authz_host(monkeypatch, role="moderator")
    assert _run(authz.caller_can(scope)) is False


def test_member_holds_only_member(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_authz_host(monkeypatch, role="member")
    assert _run(authz.caller_can(authz.SCOPE_MEMBER)) is True
    for scope in (authz.SCOPE_BAN, authz.SCOPE_TIMEOUT, authz.SCOPE_MODERATE, authz.SCOPE_ADMIN):
        assert _run(authz.caller_can(scope)) is False


def test_role_map_matches_contract() -> None:
    assert FakeAuthzHost.for_role("owner").scopes == {
        f"community:{n}" for n in ("member", "vip", "moderate", "ban", "timeout", "admin", "owner")
    }
    owner = FakeAuthzHost.for_role("owner").scopes
    assert FakeAuthzHost.for_role("community-owner").scopes == owner
    assert "community:owner" not in FakeAuthzHost.for_role("community-admin").scopes
    assert "community:admin" in FakeAuthzHost.for_role("community-admin").scopes
    assert FakeAuthzHost.for_role("vip").scopes == {"community:member", "community:vip"}
    assert FakeAuthzHost.for_role("stranger").scopes == set()


def test_unknown_role_holds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_authz_host(monkeypatch, role="stranger")
    assert _run(authz.caller_can(authz.SCOPE_MEMBER)) is False


def test_explicit_scope_set(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_authz_host(monkeypatch, {"community:ban"})
    assert _run(authz.caller_can("community:ban")) is True
    assert _run(authz.caller_can("community:timeout")) is False


def test_unavailable_host_fails_closed_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    host = install_fake_authz_host(monkeypatch, role="owner", unavailable=True)
    logged: list[tuple[str, dict]] = []
    import waddle_sdk.log as log

    monkeypatch.setattr(log, "warn", lambda msg, **f: logged.append((msg, f)))
    assert _run(authz.caller_can(authz.SCOPE_ADMIN)) is False
    assert host.call_count == 1
    assert logged and logged[0][1]["scope"] == "community:admin"
    assert logged[0][1]["error_kind"] == "Error_Unavailable"
    assert logged[0][1]["reason"] == "host_error"


def test_log_failure_never_changes_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_authz_host(monkeypatch, role="owner", unavailable=True)  # fake world has no `log`
    assert _run(authz.caller_can(authz.SCOPE_ADMIN)) is False


def test_missing_binding_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _world(monkeypatch, types.SimpleNamespace())
    assert _run(authz.caller_can(authz.SCOPE_MEMBER)) is False


@pytest.mark.parametrize(
    "scope",
    [
        "",
        "community",
        "Community:ban",
        "community:Ban",
        "1community:ban",
        "community:ban!",
        "community:",
        ":ban",
        "community:ban:x",
        "a:" + "b" * 64,
        " community:ban",
        "community:ban\n",
    ],
)
def test_invalid_scope_is_false_without_host_call(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    host = install_fake_authz_host(monkeypatch, role="owner")
    assert _run(authz.caller_can(scope)) is False
    assert host.call_count == 0


def test_non_string_scope_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    host = install_fake_authz_host(monkeypatch, role="owner")
    assert _run(authz.caller_can(None)) is False  # type: ignore[arg-type]
    assert host.call_count == 0


def test_scope_length_boundary() -> None:
    ok = "a:" + "b" * 62  # 64 bytes
    assert authz.is_valid_scope(ok)
    assert not authz.is_valid_scope(ok + "b")


def test_non_true_host_value_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _world(monkeypatch, types.SimpleNamespace(authz=types.SimpleNamespace(caller_can=lambda s: 1)))
    assert _run(authz.caller_can(authz.SCOPE_MEMBER)) is False
