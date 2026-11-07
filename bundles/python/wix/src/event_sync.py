"""Feature module: Wix events -> Discord scheduled events (`!wix enable events`), default OFF.

The trigger is the Wix webhook connector's normalized `event.{created,updated,canceled}` events
(payload `{wix_event_id, title, starts_at, ends_at, timezone, location, status}`). They are mirrored
into Discord through the Discord service tools `discord.scheduled_event.{create,update,cancel}` under
approved `tool.call:` grants -- never direct Discord HTTP. The Wix event id -> Discord event id link
lives in community kv (`wix.evmap.<wix_event_id>`), which also makes redelivery idempotent: a second
`event.created` for a mapped event is a no-op. (Outbound Wix writes are the separate `wix.event.*`
service tools, consumed by the calendar app, not by this module.) Failures are loud
(`toolcall.record_fault`, shown by `!wix modules`).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from waddle_sdk import community_kv, log

import toolcall

MODULE = "events"
TARGET_TOOLS = (
    "discord.scheduled_event.create",
    "discord.scheduled_event.update",
    "discord.scheduled_event.cancel",
)
REQUIRED_IMPORT = toolcall.REQUIRED_IMPORT
MISSING_CAPABILITY_MSG = toolcall.MISSING_CAPABILITY_MSG.format(module=MODULE)
DEFAULT_LOCATION = "TBD"
_SNOWFLAKE = re.compile(r"[0-9]{1,20}")
_WIX_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


def capability_available() -> bool:
    """True iff the runtime provides the tool-invocation import."""
    return toolcall.capability_available()


def state_key(wix_event_id: str) -> str:
    """kv key (`.`-only) holding the last applied `occurred_at|state` for a Wix event."""
    return f"wix.evst.{wix_event_id}"


def map_key(wix_event_id: str) -> str:
    """kv key (`.`-only) holding the Discord event id mirrored from a Wix event."""
    return f"wix.evmap.{wix_event_id}"


def _discord_args(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Discord scheduled-event args from a Wix event payload, or `None` when unrepresentable."""
    title, start, end = (
        payload.get("title"),
        payload.get("starts_at"),
        payload.get("ends_at"),
    )
    if not (
        isinstance(title, str)
        and title.strip()
        and isinstance(start, str)
        and isinstance(end, str)
    ):
        return None
    location = payload.get("location")
    loc = (
        location if isinstance(location, str) and location.strip() else DEFAULT_LOCATION
    )
    return {
        "name": title[:100],
        "scheduled_start_time": start,
        "scheduled_end_time": end,
        "location": loc[:100],
    }


def _key(wix_event_id: str, op: str, occurred_at: str) -> str:
    return hashlib.sha256(
        f"wix.events:{wix_event_id}:{op}:{occurred_at}".encode()
    ).hexdigest()


async def handle(
    event_type: str, payload: dict[str, Any], occurred_at: str, community: str | None
) -> None:
    """Mirror one Wix `event.*` record to Discord; every refusal is logged/recorded loudly."""
    wix_id = payload.get("wix_event_id")
    if not isinstance(wix_id, str) or _WIX_ID.fullmatch(wix_id) is None:
        log.error("wix.events_bad_payload", event_type=event_type)
        return
    ts = toolcall.parse_ts(occurred_at)
    if ts is None:
        await toolcall.record_fault(community, MODULE, "bad_event_timestamp")
        return
    prior = await toolcall.read_order(community, state_key(wix_id))
    if prior is not None and (prior[1] == "CANCELED" or ts <= prior[0]):
        log.info("wix.events_stale_event_ignored", event_type=event_type)
        return
    raw = await community_kv.get(community, map_key(wix_id))
    discord_id = raw.decode("utf-8", "replace") if raw is not None else None
    if discord_id is not None and _SNOWFLAKE.fullmatch(discord_id) is None:
        await toolcall.record_fault(community, MODULE, "corrupt_mapping")
        return
    if event_type == "event.canceled":
        if discord_id is None:
            log.debug("wix.events_cancel_unmapped")
            await toolcall.write_order(community, state_key(wix_id), ts, "CANCELED")
            return
        result = await toolcall.call(
            community,
            MODULE,
            "discord.scheduled_event.cancel",
            {"event_id": discord_id},
            idempotency_key=_key(wix_id, "cancel", occurred_at),
        )
        if result is not None:
            await community_kv.delete(community, map_key(wix_id))
            await toolcall.write_order(community, state_key(wix_id), ts, "CANCELED")
            log.info("wix.events_canceled", community_id=community)
        return
    args = _discord_args(payload)
    if args is None:
        await toolcall.record_fault(community, MODULE, "unrepresentable_event")
        return
    if discord_id is None:
        result = await toolcall.call(
            community,
            MODULE,
            "discord.scheduled_event.create",
            args,
            idempotency_key=_key(wix_id, "create", occurred_at),
        )
        if result is None:
            return
        new_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(new_id, str) or _SNOWFLAKE.fullmatch(new_id) is None:
            await toolcall.record_fault(community, MODULE, "no_remote_id")
            return
        await community_kv.set(
            community, map_key(wix_id), new_id.encode(), ttl_seconds=0
        )
        await toolcall.write_order(community, state_key(wix_id), ts, "LIVE")
        log.info("wix.events_created", community_id=community)
        return
    if event_type == "event.created":
        log.debug("wix.events_duplicate_create")
        return
    result = await toolcall.call(
        community,
        MODULE,
        "discord.scheduled_event.update",
        {"event_id": discord_id, **args},
        idempotency_key=_key(wix_id, "update", occurred_at),
    )
    if result is not None:
        await toolcall.write_order(community, state_key(wix_id), ts, "LIVE")
        log.info("wix.events_updated", community_id=community)
