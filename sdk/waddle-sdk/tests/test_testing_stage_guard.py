"""`waddle_sdk.testing.StageGuard`: the stage-faithful fake that keeps economy out of dispatch.

The real host serves `economy`/`identity`/`reputation`/`ai` to the process stage only and `relay`
to the action stage only (`core/svc_process`, `core/svc_action`). A stage-blind fake hid a bundle
that settled money in `dispatch`; these tests pin the guard that now fails such a bundle.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from waddle_sdk.testing import (
    ACTION_ONLY_CAPABILITIES,
    PROCESS_ONLY_CAPABILITIES,
    StageGuard,
    StageViolationError,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_capability_sets_match_the_host() -> None:
    assert PROCESS_ONLY_CAPABILITIES == {"economy", "identity", "reputation", "ai"}
    assert ACTION_ONLY_CAPABILITIES == {"relay"}


def test_process_only_call_is_allowed_in_the_process_stage() -> None:
    guard = StageGuard()
    balance = guard.process_only("economy.balance", lambda user: 7)

    async def go() -> int:
        with guard.in_stage("process"):
            return int(balance("u"))

    assert _run(go()) == 7
    guard.assert_clean()


def test_process_only_call_from_the_action_stage_is_recorded_and_raises() -> None:
    guard = StageGuard()
    balance = guard.process_only("economy.balance", lambda user: 7)

    async def go() -> None:
        with guard.in_stage("action"):
            balance("u")

    with pytest.raises(StageViolationError, match="economy.balance"):
        _run(go())
    assert guard.violations == ["economy.balance called from the action stage"]
    with pytest.raises(AssertionError, match="economy.balance called from the action stage"):
        guard.assert_clean()
    guard.reset()
    guard.assert_clean()


def test_action_only_relay_from_the_process_stage_is_a_violation() -> None:
    guard = StageGuard()
    push = guard.action_only("relay.push", lambda provider, msg: None)

    async def go() -> None:
        with guard.in_stage("process"):
            push("twitch", "{}")

    with pytest.raises(StageViolationError, match="relay.push"):
        _run(go())
    assert guard.violations == ["relay.push called from the process stage"]


def test_a_call_with_no_stage_set_is_not_a_violation() -> None:
    guard = StageGuard()
    assert guard.process_only("economy.balance", lambda user: 1)("u") == 1
    assert guard.action_only("relay.push", lambda p, m: 2)("p", "m") == 2
    assert guard.stage is None
    guard.assert_clean()


def test_stage_wrappers_set_and_restore_the_stage() -> None:
    guard = StageGuard()
    seen: list[str | None] = []

    async def entry() -> None:
        seen.append(guard.stage)

    async def go() -> None:
        await guard.process_stage(entry)()
        await guard.action_stage(entry)()
        seen.append(guard.stage)

    _run(go())
    assert seen == ["process", "action", None]


def test_unknown_stage_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown stage"), StageGuard().in_stage("both"):
        pass


def test_the_stage_is_per_task_so_concurrent_stages_do_not_mix() -> None:
    guard = StageGuard()
    balance = guard.process_only("economy.balance", lambda user: 1)

    async def in_process() -> None:
        with guard.in_stage("process"):
            await asyncio.sleep(0)
            balance("u")
            await asyncio.sleep(0)

    async def in_action() -> None:
        with guard.in_stage("action"):
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    async def go() -> None:
        await asyncio.gather(in_process(), in_action())

    _run(go())
    guard.assert_clean()


def test_two_stage_runs_settle_as_process_then_dispatch_as_action() -> None:
    guard = StageGuard()
    order: list[tuple[str, str | None]] = []

    class Event:
        payload = {"x": 1}
        platform = "twitch"

    import dataclasses

    @dataclasses.dataclass
    class Env:
        event: Any
        community: str
        stage: str

    async def settle(intent: Any, event: Any, *, community: str) -> Any:
        order.append(("settle", guard.stage))
        return "reply-event" if intent else None

    async def dispatch(envelope: Any, config: Any, *, http_client: Any) -> str:
        order.append(("dispatch", guard.stage))
        assert envelope.stage == "action" and envelope.event == "reply-event"
        return "sent"

    run = guard.two_stage(settle, dispatch)
    result = _run(run(Env(event=Event(), community="c", stage="process"), {}, http_client=None))
    assert result == "sent"
    assert order == [("settle", "process"), ("dispatch", "action")]


def test_two_stage_drops_a_redelivery_without_dispatching() -> None:
    guard = StageGuard()
    called: list[str] = []

    import dataclasses

    @dataclasses.dataclass
    class Ev:
        payload: dict[str, Any]
        platform: str = "twitch"

    @dataclasses.dataclass
    class Env:
        event: Ev
        community: str = "c"
        stage: str = "process"

    async def settle(intent: Any, event: Any, *, community: str) -> None:
        return None

    async def dispatch(envelope: Any, config: Any, *, http_client: Any) -> None:
        called.append("dispatch")

    result = _run(guard.two_stage(settle, dispatch)(Env(Ev({})), {}))
    assert result.detail == "duplicate" and called == []
