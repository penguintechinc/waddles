"""Feature module: Wix pricing plan -> Discord role (`!wix enable roles`), default OFF.

The trigger is the Wix webhook connector's normalized `subscription.{started,changed,ended}` events
(actor = host-resolved community UUID, payload `{order_id, plan_id, status}`). A moderator maps a Wix
plan id to a Discord role KEY (`!wix plan map <plan-uuid> <role_key>`; the role key is resolved to a
real role host-side). An active subscription adds the role, an ended/canceled/paused one removes it,
via the Discord service tools `discord.member.roles.{add,remove}` under approved `tool.call:` grants
-- never direct Discord HTTP. Failures are loud (`toolcall.record_fault`, shown by `!wix modules`).
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


def capability_available() -> bool:
    """True iff the runtime provides the tool-invocation import."""
    return toolcall.capability_available()


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
        log.info("wix.roles_applied", op=tool_name, community_id=community)
