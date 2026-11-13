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
    WIX_B,
    _Host,
    _run,
    host,
)
from waddle_sdk.flask_core import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

from app import transform as _app_transform

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
    src: str = "wix",
) -> None:
    """Deliver one connector record; each call is strictly later than the previous unless `at`."""
    when = at or f"2026-11-07T00:{next(_CLOCK):02d}:00Z"
    payload = {"time_source": src, **payload}
    ev = PlatformEvent("wix", kind, actor, payload, when)  # type: ignore[arg-type]
    with bundle_context(
        tenant="t", community="c1", app_id="waddles.core.integration.wix"
    ):
        assert _run(_app_transform(ev)) is None


def _fault(host: _Host, module: str) -> str:
    """The last recorded tool-failure code for `module` (empty when none)."""
    hits = [v for k, v in host.kv.store.items() if f"wix.fault.{module}" in k]
    return hits[0].decode() if hits else ""


def test_events_create_update_cancel_with_idempotent_redelivery(
    host: _Host, tools: _Tool
) -> None:
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
    _wix(
        host,
        "event.created",
        {"wix_event_id": WIX_EVENT, "title": "x", "starts_at": None},
    )
    assert "unrepresentable_event" in _fault(host, "events")
    ok = {
        "wix_event_id": WIX_EVENT,
        "title": "x",
        "starts_at": "2026-12-01T19:00:00Z",
        "ends_at": "2026-12-01T20:00:00Z",
    }
    tools.results["discord.scheduled_event.create"] = {"id": "not-a-snowflake"}
    _wix(host, "event.created", ok)
    assert "no_remote_id" in _fault(host, "events")
    _wix(host, "event.created", {"title": "no id"})
    assert "wix.events_bad_payload" in host.logs_text()
    assert not any("wix.evmap" in k for k in host.kv.store)


def test_unknown_wix_event_and_bad_event_payload_make_no_call(
    host: _Host, tools: _Tool
) -> None:
    _wix(host, "member.linked", {})
    _wix(host, "event.created", {"wix_event_id": WIX_EVENT})
    assert tools.calls == []


def test_events_late_events_after_cancel_or_replay_are_ignored(
    host: _Host, tools: _Tool
) -> None:
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
    p = {
        "wix_event_id": WIX_EVENT,
        "title": "t",
        "starts_at": "2026-12-01T19:00:00Z",
        "ends_at": "2026-12-01T20:00:00Z",
    }
    _wix(host, "event.canceled", p, at="2026-11-07T11:00:00Z")
    _wix(host, "event.created", p, at="2026-11-07T10:00:00Z")
    assert tools.calls == []


def test_events_cancel_with_skewed_clock_still_cancels(
    host: _Host, tools: _Tool
) -> None:
    p = {
        "wix_event_id": WIX_EVENT,
        "title": "t",
        "starts_at": "2026-12-01T19:00:00Z",
        "ends_at": "2026-12-01T20:00:00Z",
    }
    tools.results["discord.scheduled_event.create"] = {"id": "77"}
    _wix(host, "event.created", p, at="2026-11-07T10:00:00Z")
    _wix(host, "event.canceled", p, at="2026-11-07T08:00:00Z")  # older clock
    assert [c[1] for c in tools.calls][-1] == "discord.scheduled_event.cancel"
    _wix(host, "event.updated", p, at="2026-11-07T12:00:00Z")
    assert len(tools.calls) == 2


def test_events_received_time_cancel_runs_and_received_time_create_is_refused(
    host: _Host, tools: _Tool
) -> None:
    p = {
        "wix_event_id": WIX_EVENT,
        "title": "t",
        "starts_at": "2026-12-01T19:00:00Z",
        "ends_at": "2026-12-01T20:00:00Z",
    }
    _wix(host, "event.created", p, src="received")
    assert tools.calls == []
    assert "unorderable_event" in _fault(host, "events")
    tools.results["discord.scheduled_event.create"] = {"id": "77"}
    _wix(host, "event.created", p, at="2026-11-07T10:00:00Z")
    _wix(host, "event.canceled", p, src="received")
    assert [c[1] for c in tools.calls] == [
        "discord.scheduled_event.create",
        "discord.scheduled_event.cancel",
    ]


def test_missing_tool_import_is_loud(host: _Host, tools: _Tool) -> None:
    del sys.modules["wit_world"].imports.__dict__["tool"]
    _wix(host, "event.created", {"wix_event_id": WIX_EVENT})
    assert tools.calls == []
    assert _fault(host, "events") == "capability_missing"


@pytest.mark.parametrize(
    "kind", ["subscription.started", "subscription.changed", "subscription.ended"]
)
def test_subscription_records_are_ignored(host: _Host, tools: _Tool, kind: str) -> None:
    """Subscriptions are the license server's job: a stray Wix subscription record does nothing."""
    payload = {"event_id": "e1", "order_id": "order-0001", "plan_id": WIX_B, "status": "ACTIVE"}
    _wix(host, kind, payload)
    assert tools.calls == []
    assert host.kv.store == {}
    assert "wix.event_ignored" in host.logs_text()
