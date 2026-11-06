"""`!wix` -> Wix FULL (read/write) Discord module (`waddles.core.integration.wix`, #485).

Commands (Discord only; mod/broadcaster = a real boolean `True` badge, fail-closed). Every command
requires the Professional tier (hard block + upgrade reply; unknown tier fails closed):

- `!wix` / `!wix status`          -- the caller's Wix membership status (needs a link).
- `!wix status @user`             -- (mod) another member's Wix membership status.
- `!wix set @user <wix-member>`   -- (mod, WRITE) link a community member to their Wix member id.
- `!wix remove [@user]`           -- remove your own link; (mod, WRITE) remove someone else's.
- `!wix enable|disable roles|events` -- (mod) feature modules, default OFF; see below.

Announcements are NOT here: they are the generic `waddles.core.example.announce` bundle.

**Wix, never Odoo / never a raw DB.** Wix replaced Odoo with Wix (design spec 2026-09-29
Sec 2.2); the only external call is Wix's REST API -- `GET https://www.wixapis.com/members/v1/
members/<id>` -- over the single `net.http.fqdn:www.wixapis.com` GET grant. The API key and site id
are injected by the stage through WIT secret-refs (`WIX_API_KEY` -> `Authorization`, `WIX_SITE_ID`
-> `wix-site-id`); their values never enter this component, its config, logs or the repo. Only the
`member.status` enum is read from the response -- no name, email or contact field is parsed, kept
or logged.

**PII tokenization.** Wix member ids are opaque UUIDs (validated, never free text) and are stored
only under the community `user_uuid` the host resolves (`identity.resolve_actor` /
`resolve_mention` -- the caller/mention are never raw handles here). No Discord username or Wix
name/email is ever stored, logged or echoed; the reply never names the member.

**Feature modules (separate files, default OFF).** `plan_roles.py` (Wix plan -> Discord role)
and `event_sync.py` (Wix events -> community events) are driven by the Wix webhook connector and the
generic table-access grant, which are separate pieces of platform work. Each module is switched on
per community with `!wix enable <module>`; enabling is REFUSED loudly (and logged) unless the
module's platform capability is present in this runtime, so a flag can never silently do nothing.

State (`waddle_sdk.community_kv`, community-scoped; `community=None` is the host's tenant-wide
sentinel). Keys use `.` only (gh-631):

- `wix.link.<user_uuid>`    -- the linked Wix member UUID
- `wix.status.<user_uuid>`  -- cached Wix status (TTL `STATUS_TTL_SECONDS`, rate-limit kindness)
- `wix.module.<name>`       -- `1` when a feature module is enabled (default absent = OFF)

Activation-gated AND license-gated: every command needs the Professional tier
(`tier_at_least("professional")`, hard block + upgrade reply; an unknown/unavailable tier degrades
to `free`, i.e. fails closed). Feed host / Wix endpoint are single constants the customer confirms.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

from waddle_sdk import community_kv, identity, log, relay
from waddle_sdk.flask_core import get_bundle_context
from waddle_sdk.flask_core.feature_flags import tier_at_least
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.http import HttpClient, resolve_secret
from waddle_sdk.safe_text import guard_leading_command

import event_sync
import plan_roles

COMMAND = "!wix"
WIX_HOST = "www.wixapis.com"
WIX_MEMBER_URL = f"https://{WIX_HOST}/members/v1/members/"
WIX_KEY_HEADER = "Authorization"
WIX_KEY_SECRET_REF = "WIX_API_KEY"  # noqa: S105 - a secret reference name, not a value
WIX_SITE_HEADER = "wix-site-id"
WIX_SITE_SECRET_REF = "WIX_SITE_ID"  # noqa: S105 - a secret reference name, not a value

STATUS_TTL_SECONDS = 300
MAX_TEXT_LEN = 1000
_WIX_STATUSES = frozenset({"APPROVED", "PENDING", "BLOCKED", "OFFLINE"})

_USAGE = (
    "Usage: !wix [status [@user]] | !wix remove | !wix set @user <wix-member-id> (mod) | "
    "!wix remove @user (mod) | !wix enable|disable roles|events (mod)"
)
_MODULES: dict[str, Any] = {"roles": plan_roles, "events": event_sync}
_UPGRADE = "Wix membership tools need the Professional tier - ask an admin to upgrade the license."
_DENIED = "only moderators/broadcasters can do that"
_UNAVAILABLE = "Wix membership services are unavailable right now - please try again later."
_NOT_CONFIGURED = "The Wix link isn't configured correctly - please tell a moderator."
_ERRORS: dict[type[identity.IdentityError], str] = {
    identity.NotLinkedError: "that account isn't linked to Waddles yet",
    identity.NotAMemberError: "that user isn't a member of this community",
    identity.NotFoundError: "I couldn't find that user - @mention them",
    identity.AmbiguousError: "that name matches more than one person - mention them directly",
}


class _KvFailureError(Exception):
    """Internal-only: a kv host call failed or stored state was corrupt. Always caught."""


class _WixError(Exception):
    """Internal-only: carries the explicit user-facing reply for an already-logged Wix failure."""

    def __init__(self, reply: str) -> None:
        """Record the reply to send back to chat."""
        super().__init__(reply)
        self.reply = reply


def _is_privileged(payload: dict[str, Any]) -> bool:
    """True only for a real boolean `True` mod/broadcaster badge (absent badges = denied)."""
    return payload.get("is_mod") is True or payload.get("is_broadcaster") is True


def _valid_wix_id(raw: str) -> str | None:
    """Canonical lower-case UUID text for a Wix member id, or `None` if it isn't a UUID."""
    try:
        parsed = uuid.UUID(raw)
    except ValueError:
        return None
    return str(parsed) if len(raw) == 36 else None


async def _kv_get(community: str | None, key: str) -> bytes | None:
    """`community_kv.get`, reclassifying any backend error."""
    try:
        return await community_kv.get(community, key)
    except Exception as exc:
        raise _KvFailureError(f"kv.get: {type(getattr(exc, 'value', exc)).__name__}") from exc


async def _kv_set(community: str | None, key: str, value: bytes, ttl: int = 0) -> None:
    """`community_kv.set`, reclassifying any backend error."""
    try:
        await community_kv.set(community, key, value, ttl_seconds=ttl)
    except Exception as exc:
        raise _KvFailureError(f"kv.set: {type(getattr(exc, 'value', exc)).__name__}") from exc


async def _kv_delete(community: str | None, key: str) -> None:
    """`community_kv.delete`, reclassifying any backend error."""
    try:
        await community_kv.delete(community, key)
    except Exception as exc:
        raise _KvFailureError(f"kv.delete: {type(getattr(exc, 'value', exc)).__name__}") from exc


async def _get_link(community: str | None, user: str) -> str | None:
    """The Wix member UUID linked to `user`, or `None`; a malformed stored value is a failure."""
    raw = await _kv_get(community, f"wix.link.{user}")
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _KvFailureError("corrupt link") from exc
    if _valid_wix_id(text) != text:
        raise _KvFailureError("malformed link")
    return text


async def _fetch_wix_status(wix_id: str) -> str:
    """GET the member from Wix and return its `status` enum, lower-cased.

    Raises:
        _WixError: Any failure -- already logged, carries the explicit reply.
    """
    try:
        response = await HttpClient().get(
            WIX_MEMBER_URL + wix_id,
            secret_refs={
                WIX_KEY_HEADER: resolve_secret(WIX_KEY_SECRET_REF),
                WIX_SITE_HEADER: resolve_secret(WIX_SITE_SECRET_REF),
            },
        )
    except Exception as exc:  # noqa: BLE001 - SDK raises Retryable/NonRetryable transport errors
        log.error("wix.wix_unavailable", error_type=type(exc).__name__)
        raise _WixError(_UNAVAILABLE) from exc
    status = response["status"]
    if status in (401, 403):
        log.error("wix.wix_auth_rejected", status=status)
        raise _WixError(_NOT_CONFIGURED)
    if status == 404:
        log.info("wix.wix_member_not_found")
        raise _WixError("Wix doesn't know that member id - ask a moderator to re-link it.")
    if status != 200 or response["truncated"]:
        log.error("wix.wix_bad_response", status=status, truncated=bool(response["truncated"]))
        raise _WixError(_UNAVAILABLE)
    try:
        value = json.loads(cast("bytes", response["body"]).decode("utf-8"))["member"]["status"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        log.error("wix.wix_bad_response", status=status, error_type=type(exc).__name__)
        raise _WixError(_UNAVAILABLE) from exc
    if value not in _WIX_STATUSES:
        log.error("wix.wix_unknown_status")
        raise _WixError(_UNAVAILABLE)
    return cast("str", value).lower()


async def _status_for(community: str | None, user: str, *, mine: bool) -> str:
    """Reply text with `user`'s Wix membership status (cached briefly)."""
    wix_id = await _get_link(community, user)
    if wix_id is None:
        return (
            "You aren't linked to a Wix member yet - ask a moderator to link you."
            if mine
            else "That member isn't linked to a Wix member yet."
        )
    cached = await _kv_get(community, f"wix.status.{user}")
    status: str
    if cached is not None and cached.decode("utf-8", "replace").upper() in _WIX_STATUSES:
        status = cached.decode("utf-8")
    else:
        try:
            status = await _fetch_wix_status(wix_id)
        except _WixError as exc:
            return exc.reply
        await _kv_set(community, f"wix.status.{user}", status.encode(), STATUS_TTL_SECONDS)
    who = "Your" if mine else "That member's"
    return f"{who} Wix membership status: {status}"


async def _resolve_target(rest: str) -> tuple[str | None, str, str | None]:
    """Resolve the single @mention in `rest` -> `(user_uuid, leftover_text, error_reply)`."""
    tokens = identity.mention_tokens(rest)
    if len(tokens) != 1:
        return None, rest, _USAGE
    token = tokens[0]
    leftover = rest.replace("{user:" + token + "}", " ", 1).strip()
    try:
        return await identity.resolve_mention(token), leftover, None
    except identity.IdentityError as exc:
        message = _ERRORS.get(type(exc))
        if message is None:
            log.error("wix.identity_error", op="mention", error=type(exc).__name__)
            return None, leftover, _UNAVAILABLE
        log.info("wix.target_unresolved", error=type(exc).__name__)
        return None, leftover, message


async def _resolve_self() -> tuple[str | None, str | None]:
    """Resolve the caller -> `(user_uuid, error_reply)`."""
    try:
        return await identity.resolve_actor(), None
    except identity.IdentityError as exc:
        message = _ERRORS.get(type(exc))
        if message is None:
            log.error("wix.identity_error", op="actor", error=type(exc).__name__)
            return None, _UNAVAILABLE
        log.info("wix.actor_unresolved", error=type(exc).__name__)
        return None, message


async def _cmd_status(rest: str, community: str | None, privileged: bool) -> str:
    """`!wix [status [@user]]`."""
    if not rest:
        user, err = await _resolve_self()
        if user is None:
            return cast("str", err)
        return await _status_for(community, user, mine=True)
    if not privileged:
        return _DENIED
    user, _left, err = await _resolve_target(rest)
    if user is None:
        return cast("str", err)
    return await _status_for(community, user, mine=False)


async def _cmd_link(rest: str, community: str | None) -> str:
    """`!wix link @user <wix-member-id>` (mod; WRITE)."""
    user, leftover, err = await _resolve_target(rest)
    if user is None:
        return cast("str", err)
    wix_id = _valid_wix_id(leftover)
    if wix_id is None:
        return "the Wix member id must be a UUID - " + _USAGE
    await _kv_set(community, f"wix.link.{user}", wix_id.encode())
    await _kv_delete(community, f"wix.status.{user}")
    log.info("wix.linked", community_id=community)
    return "Linked. They can now check !wix status."


async def _cmd_unlink(rest: str, community: str | None, privileged: bool) -> str:
    """`!wix unlink [@user]` (self, or mod for others; WRITE)."""
    if rest:
        if not privileged:
            return _DENIED
        user, _left, err = await _resolve_target(rest)
    else:
        user, err = await _resolve_self()
    if user is None:
        return cast("str", err)
    if await _get_link(community, user) is None:
        return "No Wix link to remove."
    await _kv_delete(community, f"wix.link.{user}")
    await _kv_delete(community, f"wix.status.{user}")
    log.info("wix.unlinked", community_id=community)
    return "Wix link removed."


async def _cmd_module(verb: str, rest: str, community: str | None) -> str:
    """`!wix enable|disable <module>` (mod): flip a module flag; enabling needs its capability."""
    name = rest.strip().lower()
    module = _MODULES.get(name)
    if module is None:
        return _USAGE
    key = f"wix.module.{name}"
    if verb == "disable":
        await _kv_delete(community, key)
        log.info("wix.module_disabled", module=name)
        return f"Wix {name} module disabled."
    if not module.capability_available():
        log.error("wix.module_capability_missing", module=name)
        return cast("str", module.MISSING_CAPABILITY_MSG)
    await _kv_set(community, key, b"1")
    log.info("wix.module_enabled", module=name)
    return f"Wix {name} module enabled."


async def _run(rest: str, payload: dict[str, Any], community: str | None) -> str:
    """Route one `!wix` invocation (everything after the head) to its handler."""
    verb, _, tail = rest.strip().partition(" ")
    verb, tail = verb.lower(), tail.strip()
    privileged = _is_privileged(payload)
    if not verb or verb == "status":
        return await _cmd_status(tail, community, privileged)
    if verb == "remove":
        return await _cmd_unlink(tail, community, privileged)
    if verb in ("set", "enable", "disable"):
        if not privileged:
            log.info("wix.permission_denied", command=verb)
            return _DENIED
        if verb == "set":
            return await _cmd_link(tail, community)
        return await _cmd_module(verb, tail, community)
    return _USAGE


async def _licensed() -> bool:
    """True iff the tenant tier is Professional+; any tier-lookup failure fails closed (False)."""
    try:
        return await tier_at_least("professional")
    except Exception as exc:  # noqa: BLE001 - a host-call failure must deny, never crash or allow
        log.error("wix.tier_error", error_type=type(getattr(exc, "value", exc)).__name__)
        return False


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!wix ...`, do the work, build the reply.

    Returns `None` for non-matching text. A recognized command always yields an explicit reply
    (usage, denial or error) -- never silently dropped; kv/Wix failures are logged AND replied.
    """
    text = event.payload.get("text")
    if not isinstance(text, str) or len(text) > MAX_TEXT_LEN:
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != COMMAND:
        return None
    community = get_bundle_context().community
    try:
        if not await _licensed():
            log.info("wix.tier_denied")
            reply = _UPGRADE
        else:
            reply = await _run(rest, event.payload, community)
    except _KvFailureError as exc:
        log.error("wix.kv_error", reason=str(exc))
        reply = _UNAVAILABLE
    log.info("wix.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id"), "text": reply},
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("detail", "http_status", "sub_type", "transport")

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

    Raises:
        ValueError: The payload is missing `channel_id` or `text` (defensive).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("wix reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("wix reply requires text produced by transform")
    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": guard_leading_command(text)})
    log.info("wix.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
