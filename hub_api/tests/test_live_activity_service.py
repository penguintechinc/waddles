"""Unit tests for `services/live_activity.py` -- list query + SSE poll generator.

Own `dal` fixture (plain `sqlite:memory` pydal DAL, not `community_db`)
-- this module's queries are all synchronous, no `AsyncDAL`, so no
file-backed-sqlite/cross-thread-visibility concern applies (see
`tests/conftest.py::auth_db`'s docstring for why THAT fixture needs a
file; this one doesn't).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydal import DAL

from services import live_activity as svc


@pytest.fixture
def dal() -> Any:
    db = DAL("sqlite:memory")
    svc._ensure_table(db, migrate=True)
    yield db
    db.close()


def _insert(dal: Any, *, community_id: int, **overrides: Any) -> int:
    fields: dict[str, Any] = {
        "community_id": community_id,
        "platform": "twitch",
        "actor": "alice",
        "message_in": "!hello",
        "reply_out": "hi!",
        "channel_id": "chan-1",
        "occurred_at": datetime.now(UTC),
    }
    fields.update(overrides)
    event_id: int = dal.live_activity_events.insert(**fields)
    dal.commit()
    return event_id


class TestListRecentEvents:
    def test_empty_community_returns_no_events(self, dal: Any) -> None:
        assert svc.list_recent_events(dal, community_id=1, limit=50) == []

    def test_newest_first_and_limit_respected(self, dal: Any) -> None:
        _insert(dal, community_id=1, actor="alice")
        second_id = _insert(dal, community_id=1, actor="bob")
        events = svc.list_recent_events(dal, community_id=1, limit=1)
        assert len(events) == 1
        assert events[0].id == second_id
        assert events[0].actor == "bob"

    def test_community_scoping_excludes_other_communities(self, dal: Any) -> None:
        _insert(dal, community_id=1, actor="alice")
        _insert(dal, community_id=2, actor="eve")
        events = svc.list_recent_events(dal, community_id=1, limit=50)
        assert [e.actor for e in events] == ["alice"]

    def test_shape_matches_frozen_contract(self, dal: Any) -> None:
        _insert(dal, community_id=1)
        event = svc.list_recent_events(dal, community_id=1, limit=1)[0]
        assert set(dataclasses.asdict(event)) == {
            "id",
            "community_id",
            "platform",
            "actor",
            "message_in",
            "reply_out",
            "occurred_at",
        }
        assert "channel_id" not in dataclasses.asdict(event)


def _decode_frame(frame: bytes) -> dict[str, Any]:
    text = frame.decode()
    assert text.startswith("data: ")
    assert text.endswith("\n\n")
    return dict(json.loads(text[len("data: ") : -2]))  # noqa: E203 - ruff-format's slice spacing


class TestEventStream:
    async def test_first_yield_is_keepalive(self, dal: Any) -> None:
        gen = svc.event_stream(dal, community_id=1, poll_interval=0.01)
        first = await gen.__anext__()
        assert first == b": keepalive\n\n"
        await gen.aclose()

    async def test_preexisting_rows_are_not_replayed(self, dal: Any) -> None:
        """Baseline is the max id AT CONNECT TIME -- history isn't dumped down the stream."""
        _insert(dal, community_id=1, actor="preexisting")
        gen = svc.event_stream(dal, community_id=1, poll_interval=0.01)
        await gen.__anext__()  # establishes baseline + keepalive

        frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert frame == b": heartbeat\n\n"
        await gen.aclose()

    async def test_new_row_after_connect_is_emitted(self, dal: Any) -> None:
        gen = svc.event_stream(dal, community_id=1, poll_interval=0.01)
        await gen.__anext__()  # establishes baseline + keepalive

        _insert(dal, community_id=1, actor="new-arrival")
        frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        payload = _decode_frame(frame)
        assert payload["actor"] == "new-arrival"
        assert payload["community_id"] == 1
        await gen.aclose()

    async def test_multiple_new_rows_emitted_oldest_first_in_one_poll(self, dal: Any) -> None:
        gen = svc.event_stream(dal, community_id=1, poll_interval=0.05)
        await gen.__anext__()  # establishes baseline + keepalive

        _insert(dal, community_id=1, actor="first")
        _insert(dal, community_id=1, actor="second")
        # Give the poll loop's asyncio.sleep a moment to elapse so both
        # inserts land inside the SAME poll iteration.
        await asyncio.sleep(0.1)

        first_frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        second_frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert _decode_frame(first_frame)["actor"] == "first"
        assert _decode_frame(second_frame)["actor"] == "second"
        await gen.aclose()

    async def test_community_scoping_filters_other_communities(self, dal: Any) -> None:
        gen = svc.event_stream(dal, community_id=1, poll_interval=0.01)
        await gen.__anext__()  # establishes baseline (community 1) + keepalive

        _insert(dal, community_id=2, actor="other-community")
        frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert frame == b": heartbeat\n\n"
        await gen.aclose()
