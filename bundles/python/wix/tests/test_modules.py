# ruff: noqa: F811
"""Host-native tests for the Wix feature modules driven by connector events + Discord tool calls.

The fake `wit_world.imports.tool.invoke` is the WIT host-import boundary (the same seam the real
stage implements); everything above it -- `waddle_sdk.tool`, `toolcall`, the modules, `transform` --
is the real code.
"""

from __future__ import annotations

import itertools
import json
import sys
import types
from typing import Any

import pytest
from test_app import (  # noqa: F401 (fixture)
    ALICE,
    WIX_A,
    WIX_B,
    _Host,
    _run,
    _say,
    host,
)
from waddle_sdk.flask_core import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

from app import _DENIED, _USAGE
from app import transform as _app_transform

MOD = {"is_mod": True}
PLAN = "aaaaaaaa-1111-2222-3333-444444444444"
WIX_EVENT = "bbbbbbbb-1111-2222-3333-444444444444"
SERVICE = "waddles.core.service.discord"


class ToolError_NotGranted:  # mirrors the WIT variant class name the SDK classifies on
    """Fake WIT `tool-error` variant."""

    value = "x"


class _Err(Exception):
    def __init__(self, value: Any) -> None:
        super().__init__("wit err")
        self.value = value


class _Tool:
    """Scriptable `wit_world.imports.tool`: records calls, answers per tool name."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], str | None]] = []
        self.results: dict[str, Any] = {}
        self.fail: str | None = None

    def invoke(self, target: str, name: str, payload: bytes, idem: str | None) -> bytes:
        self.calls.append((target, name, json.loads(payload), idem))
        if self.fail == name:
            raise _Err(ToolError_NotGranted())
        return json.dumps(self.results.get(name, {})).encode()


@pytest.fixture
def tools(host: _Host) -> _Tool:
    t = _Tool()
    sys.modules["wit_world"].imports.tool = types.SimpleNamespace(invoke=t.invoke)
    return t


_CLOCK = itertools.count(1)


def _wix(
    host: _Host,
    kind: str,
    payload: dict[str, Any],
    actor: str | None = ALICE,
    at: str | None = None,
) -> None:
    """Deliver one connector record; each call is strictly later than the previous unless `at`."""
    when = at or f"2026-11-07T00:{next(_CLOCK):02d}:00Z"
    ev = PlatformEvent("wix", kind, actor, payload, when)  # type: ignore[arg-type]
    with bundle_context(
        tenant="t", community="c1", app_id="waddles.core.integration.wix"
    ):
        assert _run(_app_transform(ev)) is None


def _enable(host: _Host, module: str) -> None:
    assert _say(host, f"!wix enable {module}", **MOD) == f"Wix {module} module enabled."


def _sub(status: str = "ACTIVE", order: str = "order-0001") -> dict[str, Any]:
    return {"event_id": "e1", "order_id": order, "plan_id": PLAN, "status": status}


def test_roles_map_then_active_adds_and_ended_removes(
    host: _Host, tools: _Tool
) -> None:
    assert "Plan mapped" in _say(host, f"!wix plan map {PLAN} vip", **MOD)
    _enable(host, "roles")
    _wix(host, "subscription.started", _sub())
    _wix(host, "subscription.ended", _sub("CANCELED"))
    assert [(c[0], c[1], c[2]) for c in tools.calls] == [
        (SERVICE, "discord.member.roles.add", {"user": ALICE, "role_key": "vip"}),
        (SERVICE, "discord.member.roles.remove", {"user": ALICE, "role_key": "vip"}),
    ]
    assert all(c[3] for c in tools.calls)  # idempotency keys present


def test_roles_off_unmapped_pending_and_actorless_do_nothing(
    host: _Host, tools: _Tool
) -> None:
    _wix(host, "subscription.started", _sub())  # module OFF
    _say(host, f"!wix plan map {PLAN} vip", **MOD)
    _enable(host, "roles")
    _wix(host, "subscription.changed", _sub("PENDING"))
    _wix(host, "subscription.started", _sub(), actor=None)
    _wix(host, "subscription.started", {**_sub(), "plan_id": WIX_B})  # unmapped plan
    assert tools.calls == []


def test_roles_tool_refusal_is_loud_and_shown_by_modules(
    host: _Host, tools: _Tool
) -> None:
    _say(host, f"!wix plan map {PLAN} vip", **MOD)
    _enable(host, "roles")
    tools.fail = "discord.member.roles.add"
    _wix(host, "subscription.started", _sub())
    assert "wix.tool_failed" in host.logs_text()
    reply = _say(host, "!wix modules", **MOD)
    assert "roles: on" in reply and "last failure: not_granted" in reply
    tools.fail = None
    _wix(host, "subscription.started", _sub())
    assert "last failure" not in _say(host, "!wix modules", **MOD)


def test_missing_tool_import_is_loud(host: _Host, tools: _Tool) -> None:
    _say(host, f"!wix plan map {PLAN} vip", **MOD)
    _enable(host, "roles")
    del sys.modules["wit_world"].imports.__dict__["tool"]
    _wix(host, "subscription.started", _sub())
    assert tools.calls == []
    reply = _say(host, "!wix modules", **MOD)
    assert "TOOLS MISSING" in reply and "capability_missing" in reply


def test_events_create_update_cancel_with_idempotent_redelivery(
    host: _Host, tools: _Tool
) -> None:
    _enable(host, "events")
    p = {
        "wix_event_id": WIX_EVENT,
        "title": "Movie night",
        "starts_at": "2026-12-01T19:00:00Z",
        "ends_at": "2026-12-01T21:00:00Z",
        "timezone": "UTC",
        "location": None,
        "status": "UPCOMING",
    }
    tools.results["discord.scheduled_event.create"] = {"id": "1234567890"}
    _wix(host, "event.created", p)
    _wix(host, "event.created", p)  # redelivery: no second create
    _wix(host, "event.updated", {**p, "title": "Renamed"})
    _wix(host, "event.canceled", p)
    _wix(host, "event.canceled", p)  # already gone: no second cancel
    names = [(c[1], c[2]) for c in tools.calls]
    assert names == [
        (
            "discord.scheduled_event.create",
            {
                "name": "Movie night",
                "scheduled_start_time": "2026-12-01T19:00:00Z",
                "scheduled_end_time": "2026-12-01T21:00:00Z",
                "location": "TBD",
            },
        ),
        (
            "discord.scheduled_event.update",
            {
                "event_id": "1234567890",
                "name": "Renamed",
                "scheduled_start_time": "2026-12-01T19:00:00Z",
                "scheduled_end_time": "2026-12-01T21:00:00Z",
                "location": "TBD",
            },
        ),
        ("discord.scheduled_event.cancel", {"event_id": "1234567890"}),
    ]
    assert not any(f"wix.evmap.{WIX_EVENT}" in k for k in host.kv.store)


def test_events_unrepresentable_and_bad_remote_id_fail_loud(
    host: _Host, tools: _Tool
) -> None:
    _enable(host, "events")
    _wix(
        host,
        "event.created",
        {"wix_event_id": WIX_EVENT, "title": "x", "starts_at": None},
    )
    assert "unrepresentable_event" in _say(host, "!wix modules", **MOD)
    ok = {
        "wix_event_id": WIX_EVENT,
        "title": "x",
        "starts_at": "2026-12-01T19:00:00Z",
        "ends_at": "2026-12-01T20:00:00Z",
    }
    tools.results["discord.scheduled_event.create"] = {"id": "not-a-snowflake"}
    _wix(host, "event.created", ok)
    assert "no_remote_id" in _say(host, "!wix modules", **MOD)
    _wix(host, "event.created", {"title": "no id"})
    assert "wix.events_bad_payload" in host.logs_text()
    assert not any("wix.evmap" in k for k in host.kv.store)


def test_unknown_wix_event_and_module_off_are_ignored(
    host: _Host, tools: _Tool
) -> None:
    _wix(host, "member.linked", {})
    _wix(host, "event.created", {"wix_event_id": WIX_EVENT})
    assert tools.calls == []


def test_plan_command_validation_and_permissions(host: _Host, tools: _Tool) -> None:
    assert _say(host, f"!wix plan map {PLAN} vip") == _DENIED
    assert _say(host, "!wix modules") == _DENIED
    for bad in (
        "!wix plan",
        "!wix plan map",
        "!wix plan map nope vip",
        f"!wix plan map {PLAN}",
        f"!wix plan map {PLAN} Bad!",
        f"!wix plan map {PLAN} vip extra",
        f"!wix plan unmap {PLAN} extra",
        f"!wix plan other {PLAN}",
    ):
        assert "Plan mapped" not in _say(host, bad, **MOD), bad
    assert "removed" in _say(host, f"!wix plan unmap {PLAN}", **MOD)
    assert _USAGE in _say(host, "!wix plan", **MOD)
    assert WIX_A  # fixture constants referenced


def test_roles_late_or_replayed_active_after_ended_never_regrants(
    host: _Host, tools: _Tool
) -> None:
    _say(host, f"!wix plan map {PLAN} vip", **MOD)
    _enable(host, "roles")
    _wix(host, "subscription.started", _sub(), at="2026-11-07T10:00:00Z")
    _wix(host, "subscription.ended", _sub("ENDED"), at="2026-11-07T11:00:00Z")
    _wix(
        host, "subscription.started", _sub(), at="2026-11-07T10:30:00Z"
    )  # delayed, older
    _wix(
        host, "subscription.started", _sub(), at="2026-11-07T12:00:00Z"
    )  # newer but terminal
    _wix(host, "subscription.ended", _sub("ENDED"), at="2026-11-07T11:00:00Z")  # replay
    assert [c[1] for c in tools.calls] == [
        "discord.member.roles.add",
        "discord.member.roles.remove",
    ]
    assert "wix.roles_stale_event_ignored" in host.logs_text()


def test_roles_replay_ignored_and_new_order_grants(host: _Host, tools: _Tool) -> None:
    _say(host, f"!wix plan map {PLAN} vip", **MOD)
    _enable(host, "roles")
    _wix(host, "subscription.started", _sub(), at="2026-11-07T10:00:00Z")
    _wix(
        host, "subscription.started", _sub(), at="2026-11-07T10:00:00Z"
    )  # same ts: replay
    _wix(host, "subscription.ended", _sub("CANCELED"), at="2026-11-07T11:00:00Z")
    _wix(
        host,
        "subscription.started",
        _sub(order="order-0002"),
        at="2026-11-07T12:00:00Z",
    )
    assert [c[1] for c in tools.calls] == [
        "discord.member.roles.add",
        "discord.member.roles.remove",
        "discord.member.roles.add",
    ]


def test_roles_failed_call_does_not_advance_order_state(
    host: _Host, tools: _Tool
) -> None:
    _say(host, f"!wix plan map {PLAN} vip", **MOD)
    _enable(host, "roles")
    tools.fail = "discord.member.roles.add"
    _wix(host, "subscription.started", _sub(), at="2026-11-07T10:00:00Z")
    tools.fail = None
    _wix(
        host, "subscription.started", _sub(), at="2026-11-07T10:00:00Z"
    )  # retry still applies
    assert [c[1] for c in tools.calls].count("discord.member.roles.add") == 2


def test_roles_bad_order_or_timestamp_fail_loud(host: _Host, tools: _Tool) -> None:
    _say(host, f"!wix plan map {PLAN} vip", **MOD)
    _enable(host, "roles")
    _wix(host, "subscription.started", _sub(order="x:y"))
    _wix(host, "subscription.started", _sub(), at="yesterday")
    assert tools.calls == []
    assert "bad_order_event" in _say(host, "!wix modules", **MOD)


def test_events_late_events_after_cancel_or_replay_are_ignored(
    host: _Host, tools: _Tool
) -> None:
    _enable(host, "events")
    p = {
        "wix_event_id": WIX_EVENT,
        "title": "t",
        "starts_at": "2026-12-01T19:00:00Z",
        "ends_at": "2026-12-01T20:00:00Z",
    }
    tools.results["discord.scheduled_event.create"] = {"id": "77"}
    _wix(host, "event.created", p, at="2026-11-07T10:00:00Z")
    _wix(
        host, "event.updated", {**p, "title": "old"}, at="2026-11-07T09:00:00Z"
    )  # older
    _wix(host, "event.canceled", p, at="2026-11-07T11:00:00Z")
    _wix(
        host, "event.created", p, at="2026-11-07T12:00:00Z"
    )  # late, after terminal cancel
    _wix(host, "event.updated", p, at="2026-11-07T13:00:00Z")
    assert [c[1] for c in tools.calls] == [
        "discord.scheduled_event.create",
        "discord.scheduled_event.cancel",
    ]


def test_events_cancel_before_create_is_terminal(host: _Host, tools: _Tool) -> None:
    _enable(host, "events")
    p = {
        "wix_event_id": WIX_EVENT,
        "title": "t",
        "starts_at": "2026-12-01T19:00:00Z",
        "ends_at": "2026-12-01T20:00:00Z",
    }
    _wix(host, "event.canceled", p, at="2026-11-07T11:00:00Z")
    _wix(host, "event.created", p, at="2026-11-07T10:00:00Z")
    assert tools.calls == []
