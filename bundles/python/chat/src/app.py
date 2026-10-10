"""`!chat-history [list]` / `!channels [list]` -- community chat lookup commands.

Ported from `core/svc_process/builtin_handlers/community_chat_process.py`'s two
read-only commands into a WASM-component App Bundle. Command recognition
and grammar (`!chat-history`/`!chat-history list`, `!channels`/`!channels
list`, via the standard `waddle_sdk.command` grammar) carry over unchanged
from the original port (PR #621). The actual data read does not.

**Architecture gap (2026-10-08, release/v3.0.X): this bundle cannot serve
its reads under the current `waddle_sdk.db` facade.** PR #621 was written
against the facade's retired query-builder (`AsyncQuerySet`/`get_bundle_dal`
-> `dal(dal.hub_chat_messages.community_id == community_id).select()`),
reading the named shared table `hub_chat_messages` via the old
`data.tables: [<name>]` manifest shape. That facade is gone. The current
`waddle_sdk.db` module (see its own docstring, "no bundle-supplied SQL,
ever") exposes exactly `insert`/`get`/`query`/`update`/`delete`, each
implicitly targeting the ONE table a bundle owns (provisioned at install
time from *this bundle's own* `data.table.columns`, in `app_core`/
`app_community` -- `core/bundle_host_db/src/scope.rs::DbScope` only ever
resolves `(tenant, community, app_id)` against the calling bundle's own
schema). `hub_chat_messages` is a hub-api-owned table (`hub_api/services/
community_chat.py`), outside both schemas and outside this bundle's own
`app_id` -- there is no `table` parameter left to name it with, and no
host capability that grants cross-bundle/cross-service reads.

`waddle_sdk.kv` doesn't help either: it is a small per-app blob store,
never populated with the hub's chat message history, so routing through
it would mean inventing a brand-new duplicate-storage feature (and
PII-duplication concern, see `client.md`/`critical-rules.md` PII
Tokenization: message content and sender identity belong inside the API
boundary, not re-homed into a bundle's own storage) rather than porting
the existing read. `waddle_sdk.http`'s egress-allowlisted client is built
for external third-party calls, not an authenticated internal call back
into hub-api's own `community.chat:read`-scoped, tenant-middleware-gated
REST endpoints (`hub_api/blueprints/v1/community_chat.py`) -- no bundle
capability issues a service-to-service JWT today.

Until a host capability exists for this (e.g. a dedicated WIT import for
hub-owned read-only lookups, or a service-authenticated call path into
hub-api), both commands recognize correctly, stay flag-gated, and always
produce an explicit, honest reply -- never a crash, never a silent drop.
Tracked: https://github.com/penguintechinc/waddles/issues/678

Read-only -- open to anyone, matching the source (no `_is_privileged()`
gate, unlike `count`/`lurk`'s mutating commands).

PII note: every `log.*` call here is scoped to command metadata only,
never message content or usernames (AUTHORING.md's log-sanitization rule;
see also `critical-rules.md` PII Tokenization).

Gated behind the PostHog flag ``waddles.command-chat``, default OFF (see
`bundles/python/count/src/app.py`'s own docstring for the flag-gate
rationale and ordering: cheapest disqualifying checks first, flag check
only once a `!`-prefixed, command-matching message is in hand).

**BUILD-ONLY / INERT**: `core/svc_process`'s `bot_process._FEATURE_MODULES`
still runs the original `community_chat_process` monolith module -- this
bundle is not yet the live implementation. Cut-over is a separate, later
change (P4 activation gate), and -- per the architecture gap above --
cannot happen until the read path is built.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core import PlatformEvent, StageEnvelope
from waddle_sdk.flask_core.feature_flags import feature_enabled

FLAG_KEY = "waddles.command-chat"

_CHAT_HISTORY_SPEC = CommandSpec(name="chat-history")
_CHANNELS_SPEC = CommandSpec(name="channels")

#: Tracks the architecture gap described in the module docstring -- a
#: cross-service/cross-table read capability does not exist yet for bundles.
_TRACKING_ISSUE = "https://github.com/penguintechinc/waddles/issues/678"

_UNAVAILABLE_REPLY = (
    "Chat lookups aren't available from this bundle yet -- the read needs a "
    f"capability that doesn't exist today. Tracked: {_TRACKING_ISSUE}"
)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: reply to `!chat-history`/`!channels`, else `None`.

    Returns `None` for any non-chat payload, text with no leading `!`
    (cheap-skip before the flag check), an unrecognized leading command
    token, and while `waddles.command-chat` is disabled. Once a command
    token matches, parsing failures and the always-current architecture-gap
    reply (module docstring) both still produce a reply -- never a silent
    drop (AUTHORING.md's fail-loud rule).
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None

    head = stripped.split(maxsplit=1)[0].lower()
    if head == f"!{_CHAT_HISTORY_SPEC.name}":
        spec = _CHAT_HISTORY_SPEC
    elif head == f"!{_CHANNELS_SPEC.name}":
        spec = _CHANNELS_SPEC
    else:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        parsed = parse_command(stripped, spec)
    except CommandUsageError as exc:
        reply_text = str(exc)
    else:
        if parsed.option not in (None, "list"):
            reply_text = f"Usage: !{spec.name} [list]"
        else:
            log.warn("chat.read_unavailable", command=spec.name)
            reply_text = _UNAVAILABLE_REPLY

    log.info("chat.transform matched", command=spec.name)
    return dataclasses.replace(
        event,
        payload={**event.payload, "text": reply_text},
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `count`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the reply text `transform` already built.

    Pure relay, same minimal shape as `pyping`/`count`'s own `dispatch` --
    all query/formatting work happens in `transform`.

    Raises:
        ValueError: The envelope's payload is missing `channel_id` or
            `text` (defensive -- `transform` always sets both when it
            returns a non-`None` event).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("chat reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("chat reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("chat.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
