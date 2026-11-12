"""Feature module: Wix pricing plan -> Discord role (event-only, runs on bundle activation).

The trigger is the Wix webhook connector's normalized `subscription.{started,changed,ended}` events
(actor = host-resolved community UUID, payload `{order_id, plan_id, status}`). The admin config path maps a
Wix plan id to a Discord role KEY via `set_mapping` (kv `wix.plan.<plan_uuid>`; the role key is
resolved to a real role host-side). An active subscription adds the role, an ended/canceled/paused one removes it,
via the Discord service tools `discord.member.roles.{add,remove}` under approved `tool.call:` grants
-- never direct Discord HTTP. Failures are loud (`toolcall.record_fault`, kv `wix.fault.roles`).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from waddle_sdk import community_kv, log

import toolcall

MODULE = "roles"
TARGET_TOOLS = ("discord.member.roles.add", "discord.member.roles.remove")
REQUIRED_IMPORT = toolcall.REQUIRED_IMPORT
MISSING_CAPABILITY_MSG = toolcall.MISSING_CAPABILITY_MSG.format(module=MODULE)
#: Same pattern as the Discord tool schema's `role_key`; `fullmatch` only (ReDoS/`$` newline).
_ROLE_KEY = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_ACTIVE = frozenset({"ACTIVE"})
_INACTIVE = frozenset({"ENDED", "CANCELED", "PAUSED"})
_ORDER_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


def capability_available() -> bool:
    """True iff the runtime provides the tool-invocation import."""
    return toolcall.capability_available()


def order_key(order_id: str) -> str:
    """kv key (`.`-only) holding the last applied `occurred_at|status` for a Wix order."""
    return f"wix.sub.{order_id}"


def plan_key(plan_id: str) -> str:
    """kv key (`.`-only) holding the role key mapped to a Wix plan id."""
    return f"wix.plan.{plan_id}"


def valid_role_key(raw: str) -> str | None:
    """`raw` if it is a valid role key, else `None`."""
    return raw if _ROLE_KEY.fullmatch(raw) else None


async def set_mapping(community: str | None, plan_id: str, role_key: str) -> None:
    """Map `plan_id` -> `role_key` (both already validated by the caller)."""
    await community_kv.set(
        community, plan_key(plan_id), role_key.encode(), ttl_seconds=0
    )


async def remove_mapping(community: str | None, plan_id: str) -> None:
    """Drop the mapping for `plan_id`."""
    await community_kv.delete(community, plan_key(plan_id))


async def handle(
    event_type: str,
    actor: str | None,
    payload: dict[str, Any],
    occurred_at: str,
    community: str | None,
) -> None:
    """Apply one `subscription.*` event; unmapped plans and unlinked actors are logged, not errors."""
    if actor is None:
        log.warn("wix.roles_no_actor", event_type=event_type)
        return
    plan_id = payload.get("plan_id")
    status = payload.get("status")
    if not isinstance(plan_id, str) or not isinstance(status, str):
        log.error("wix.roles_bad_payload", event_type=event_type)
        return
    raw = await community_kv.get(community, plan_key(plan_id))
    if raw is None:
        log.debug("wix.roles_plan_unmapped", event_type=event_type)
        return
    role_key = raw.decode("utf-8", "replace")
    if valid_role_key(role_key) is None:
        await toolcall.record_fault(community, MODULE, "corrupt_mapping")
        return
    if event_type == "subscription.ended" or status in _INACTIVE:
        tool_name = "discord.member.roles.remove"
    elif status in _ACTIVE:
        tool_name = "discord.member.roles.add"
    else:
        log.debug("wix.roles_status_ignored", event_type=event_type)
        return
    order_id = payload.get("order_id")
    ts = toolcall.parse_ts(occurred_at)
    if (
        not isinstance(order_id, str)
        or _ORDER_ID.fullmatch(order_id) is None
        or ts is None
    ):
        await toolcall.record_fault(community, MODULE, "bad_order_event")
        return
    prior = await toolcall.read_order(community, order_key(order_id))
    wix_time = payload.get("time_source") == "wix"
    revoke = tool_name == "discord.member.roles.remove"
    if prior is not None and prior[1] in toolcall.TERMINAL:
        log.info("wix.roles_terminal_order_ignored", event_type=event_type)
        return
    if not revoke and not toolcall.grant_is_newer(prior, ts, wix_time=wix_time):
        # Revocations are never timestamp-gated; only grants need a newer Wix-sourced time.
        if wix_time:
            log.info("wix.roles_stale_event_ignored", event_type=event_type)
        else:
            await toolcall.record_fault(community, MODULE, "unorderable_grant")
        return
    key = hashlib.sha256(
        f"wix.roles:{payload.get('event_id')}:{tool_name}:{occurred_at}".encode()
    ).hexdigest()
    result = await toolcall.call(
        community,
        MODULE,
        tool_name,
        {"user": actor, "role_key": role_key},
        idempotency_key=key,
    )
    if result is not None:
        if not revoke:
            applied = "ACTIVE"
        elif event_type == "subscription.ended" or status in toolcall.TERMINAL:
            applied = "CANCELED" if status == "CANCELED" else "ENDED"
        else:
            applied = "PAUSED"
        await toolcall.write_order(
            community, order_key(order_id), ts, applied, wix_time=wix_time
        )
        log.info("wix.roles_applied", op=tool_name, community_id=community)
