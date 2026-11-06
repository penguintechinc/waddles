"""`!bcm` -> Bar Citizen FULL (read/write) Discord module (`waddles.core.barcitizen.full`, #485).

Commands (Discord only; mod/broadcaster = a real boolean `True` badge, fail-closed):

- `!bcm` / `!bcm status`          -- the caller's Wix membership status (needs a link).
- `!bcm status @user`             -- (mod) another member's Wix membership status.
- `!bcm link @user <wix-member>`  -- (mod, WRITE) link a community member to their Wix member id.
- `!bcm unlink [@user]`           -- remove your own link; (mod, WRITE) remove someone else's.
- `!bcm news`                     -- the latest community announcements.
- `!bcm announce <text>`          -- (mod, WRITE) post + store an announcement (cap 10 kept).

**Wix, never Odoo / never a raw DB.** Bar Citizen replaced Odoo with Wix (design spec 2026-09-29
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

**Out of scope here (documented follow-ups, not silently skipped):** Wix webhooks (member/event/
pricing-plan inbound) need the host-side connector transport + signature/replay verification
(`waddle:connector@1.0.0`, spec Sec 2.2) and the Wix-events -> canonical event-sync feed; Wix
pricing-plan -> Discord role mapping rides the role-sync work. Neither can live in a stage bundle.

State (`waddle_sdk.community_kv`, community-scoped; `community=None` is the host's tenant-wide
sentinel). Keys use `.` only (gh-631):

- `bcf.link.<user_uuid>`    -- the linked Wix member UUID
- `bcf.status.<user_uuid>`  -- cached Wix status (TTL `STATUS_TTL_SECONDS`, rate-limit kindness)
- `bcf.news`                -- JSON `[{"ms": int, "text": str}, ...]`, newest last

Activation-gated only (OFF until a community admin activates it; repo command-bundle convention).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

from waddle_sdk import clock, community_kv, identity, log, relay
from waddle_sdk.flask_core import get_bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.http import HttpClient, resolve_secret
from waddle_sdk.safe_text import guard_leading_command, sanitize_user_text

COMMAND = "!bcm"
WIX_HOST = "www.wixapis.com"
WIX_MEMBER_URL = f"https://{WIX_HOST}/members/v1/members/"
WIX_KEY_HEADER = "Authorization"
WIX_KEY_SECRET_REF = "WIX_API_KEY"  # noqa: S105 - a secret reference name, not a value
WIX_SITE_HEADER = "wix-site-id"
WIX_SITE_SECRET_REF = "WIX_SITE_ID"  # noqa: S105 - a secret reference name, not a value

STATUS_TTL_SECONDS = 300
NEWS_CAP = 10
NEWS_SHOW = 3
MAX_ANNOUNCE_LEN = 300
MAX_TEXT_LEN = 1000
_WIX_STATUSES = frozenset({"APPROVED", "PENDING", "BLOCKED", "OFFLINE"})

_USAGE = (
    "Usage: !bcm [status [@user]] | !bcm news | !bcm unlink | "
    "!bcm link @user <wix-member-id> (mod) | !bcm announce <text> (mod) | !bcm unlink @user (mod)"
)
_DENIED = "only moderators/broadcasters can do that"
_UNAVAILABLE = "Bar Citizen member services are unavailable right now - please try again later."
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


def _has_control_chars(value: str) -> bool:
    """True if `value` contains any ASCII control character (newline, tab, NUL, ...)."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


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
    raw = await _kv_get(community, f"bcf.link.{user}")
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _KvFailureError("corrupt link") from exc
    if _valid_wix_id(text) != text:
        raise _KvFailureError("malformed link")
    return text


async def _load_news(community: str | None) -> list[dict[str, Any]]:
    """The stored announcements (empty if none); malformed stored state is a failure."""
    raw = await _kv_get(community, "bcf.news")
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _KvFailureError("corrupt news") from exc
    if not isinstance(data, list) or not all(
        isinstance(i, dict) and isinstance(i.get("ms"), int) and isinstance(i.get("text"), str)
        for i in data
    ):
        raise _KvFailureError("malformed news")
    return cast("list[dict[str, Any]]", data)


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
        log.error("bcf.wix_unavailable", error_type=type(exc).__name__)
        raise _WixError(_UNAVAILABLE) from exc
    status = response["status"]
    if status in (401, 403):
        log.error("bcf.wix_auth_rejected", status=status)
        raise _WixError(_NOT_CONFIGURED)
    if status == 404:
        log.info("bcf.wix_member_not_found")
        raise _WixError("Wix doesn't know that member id - ask a moderator to re-link it.")
    if status != 200 or response["truncated"]:
        log.error("bcf.wix_bad_response", status=status, truncated=bool(response["truncated"]))
        raise _WixError(_UNAVAILABLE)
    try:
        value = json.loads(cast("bytes", response["body"]).decode("utf-8"))["member"]["status"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        log.error("bcf.wix_bad_response", status=status, error_type=type(exc).__name__)
        raise _WixError(_UNAVAILABLE) from exc
    if value not in _WIX_STATUSES:
        log.error("bcf.wix_unknown_status")
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
    cached = await _kv_get(community, f"bcf.status.{user}")
    status: str
    if cached is not None and cached.decode("utf-8", "replace").upper() in _WIX_STATUSES:
        status = cached.decode("utf-8")
    else:
        try:
            status = await _fetch_wix_status(wix_id)
        except _WixError as exc:
            return exc.reply
        await _kv_set(community, f"bcf.status.{user}", status.encode(), STATUS_TTL_SECONDS)
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
            log.error("bcf.identity_error", op="mention", error=type(exc).__name__)
            return None, leftover, _UNAVAILABLE
        log.info("bcf.target_unresolved", error=type(exc).__name__)
        return None, leftover, message


async def _resolve_self() -> tuple[str | None, str | None]:
    """Resolve the caller -> `(user_uuid, error_reply)`."""
    try:
        return await identity.resolve_actor(), None
    except identity.IdentityError as exc:
        message = _ERRORS.get(type(exc))
        if message is None:
            log.error("bcf.identity_error", op="actor", error=type(exc).__name__)
            return None, _UNAVAILABLE
        log.info("bcf.actor_unresolved", error=type(exc).__name__)
        return None, message


async def _cmd_status(rest: str, community: str | None, privileged: bool) -> str:
    """`!bcm [status [@user]]`."""
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
    """`!bcm link @user <wix-member-id>` (mod; WRITE)."""
    user, leftover, err = await _resolve_target(rest)
    if user is None:
        return cast("str", err)
    wix_id = _valid_wix_id(leftover)
    if wix_id is None:
        return "the Wix member id must be a UUID - " + _USAGE
    await _kv_set(community, f"bcf.link.{user}", wix_id.encode())
    await _kv_delete(community, f"bcf.status.{user}")
    log.info("bcf.linked", community_id=community)
    return "Linked. They can now check !bcm status."


async def _cmd_unlink(rest: str, community: str | None, privileged: bool) -> str:
    """`!bcm unlink [@user]` (self, or mod for others; WRITE)."""
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
    await _kv_delete(community, f"bcf.link.{user}")
    await _kv_delete(community, f"bcf.status.{user}")
    log.info("bcf.unlinked", community_id=community)
    return "Wix link removed."


async def _cmd_news(community: str | None) -> str:
    """`!bcm news`: the latest announcements, newest first."""
    news = await _load_news(community)
    if not news:
        return "No announcements yet."
    shown = [sanitize_user_text(i["text"]) for i in reversed(news[-NEWS_SHOW:])]
    return "Bar Citizen news: " + " | ".join(shown)


async def _cmd_announce(rest: str, community: str | None) -> str:
    """`!bcm announce <text>` (mod; WRITE): sanitise, store (newest last, capped) and echo."""
    text = " ".join(sanitize_user_text(rest).split())
    if not text or len(text) > MAX_ANNOUNCE_LEN or _has_control_chars(rest):
        return f"announcements must be 1..{MAX_ANNOUNCE_LEN} characters - " + _USAGE
    news = await _load_news(community)
    news.append({"ms": clock.now_millis(), "text": text})
    await _kv_set(
        community, "bcf.news", json.dumps(news[-NEWS_CAP:], separators=(",", ":")).encode()
    )
    log.info("bcf.announced", count=min(len(news), NEWS_CAP))
    return f"Announcement: {text}"


async def _run(rest: str, payload: dict[str, Any], community: str | None) -> str:
    """Route one `!bcm` invocation (everything after the head) to its handler."""
    verb, _, tail = rest.strip().partition(" ")
    verb, tail = verb.lower(), tail.strip()
    privileged = _is_privileged(payload)
    if not verb or verb == "status":
        return await _cmd_status(tail, community, privileged)
    if verb == "news" and not tail:
        return await _cmd_news(community)
    if verb == "unlink":
        return await _cmd_unlink(tail, community, privileged)
    if verb in ("link", "announce"):
        if not privileged:
            log.info("bcf.permission_denied", command=verb)
            return _DENIED
        if verb == "link":
            return await _cmd_link(tail, community)
        return await _cmd_announce(tail, community)
    return _USAGE


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!bcm ...`, do the work, build the reply.

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
        reply = await _run(rest, event.payload, community)
    except _KvFailureError as exc:
        log.error("bcf.kv_error", reason=str(exc))
        reply = _UNAVAILABLE
    log.info("bcf.transform matched", platform=event.platform)
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
        raise ValueError("bar citizen reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("bar citizen reply requires text produced by transform")
    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": guard_leading_command(text)})
    log.info("bcf.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
