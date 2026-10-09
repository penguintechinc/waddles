"""`!secret <username> <message>` -> one-time secret messaging (feature #684).

Flow (ORDER MATTERS -- the original message is never deleted until the
secret is safely stored):

1. validate + resolve the target (identity index, kv) and check the platform
   supports BOTH `chat.delete` and `dm.send` -- all BEFORE any side effect, so
   an unsupported platform (Twitch today: both ops are `Unsupported` stubs in
   `core/svc_ingest/src/outbound_ops.rs`) fails loud with nothing stored;
2. store the message via hub-api `POST /api/v1/one-time-secrets` (scope
   `secret_messaging:create`, service token injected by the stage as the
   `Authorization` header via secret-ref -- it never enters the guest);
   failure -> error reply, original message untouched;
3. emit a `chat.delete` op for the event's message; failure -> error reply and
   NO DM (the text is still public, tell the sender);
4. emit a `dm.send` op with `<webui>/secret#<token>` -- the token rides in the
   URL fragment (never sent to a server/access log) of the webui page (#721).

If the DM fails after the delete, the sender is told delivery failed; the
stored secret simply expires (its link was never shared).

Identity limitation (hub-UUID resolver #429 not landed): `<username>` is
resolved through a community-kv index `secret.target.<sha256(lower(name))>`
-> JSON `{"uuid": <hub_users.uuid>, "platform_user_id": <id>}`. The index is
keyed by a non-reversible hash (same pseudonym approach as `coinflip`/`fish`)
and must be populated by the identity-link flow; an unlinked target FAILS
LOUD ("not linked") rather than guessing. Replace `_resolve_target` once
#429 lands.

PII-FREE logs: never the secret text, the username, the target, or the
token -- only op / platform / community_id / exception type. Reply text to
the public channel is generic for the same reason.

Gated behind PostHog flags `waddles.command-secret` AND
`waddles.secret-messaging` (the latter also gates the hub-api endpoint).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from waddle_sdk import community_kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.http import HttpClient, resolve_secret

#: PostHog flag keys, `{product}.{feature-name}` convention.
FLAG_KEY = "waddles.command-secret"
FEATURE_FLAG_KEY = "waddles.secret-messaging"

#: Platforms whose senders implement BOTH `chat.delete` and `dm.send`.
#: Twitch's are `Unsupported` stubs (Helix client follow-up) -> refused up front.
SUPPORTED_PLATFORMS: frozenset[str] = frozenset({"discord"})

#: Stage-injected secret-ref holding `Bearer <service jwt>` (scope secret_messaging:create).
SERVICE_TOKEN_SECRET = "SECRET_MESSAGING_SERVICE_TOKEN"  # noqa: S105 -- ref name, not a credential

_TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")
_USAGE = "Usage: !secret <username> <message>"
_MAX_MESSAGE_CHARS = 4000

_ERR_UNSUPPORTED = "Secret messaging isn't supported on this platform; nothing was sent."
_ERR_TARGET = "That user isn't linked for secret messaging; nothing was sent."
_ERR_STORE = "Couldn't store the secret; your message was left as-is."
_ERR_DELETE = "Couldn't remove your message, so the secret was NOT delivered."
_ERR_DM = "Your message was removed but the DM failed; please resend."
_OK = "Secret delivered by DM."


class SecretFlowError(Exception):
    """A step of the secret flow failed; `public_reply` is the safe, generic chat notice."""

    def __init__(self, step: str, public_reply: str) -> None:
        """Record the failed step (log-safe) and the generic reply for the sender."""
        super().__init__(step)
        self.step = step
        self.public_reply = public_reply


def _normalize_target(raw: str) -> str | None:
    """Strip an optional leading `@`; `None` if not a plausible username shape."""
    candidate = raw[1:] if raw.startswith("@") else raw
    if not candidate or not _TARGET_RE.match(candidate):
        return None
    return candidate


def _target_key(username: str) -> str:
    """Identity-index key: non-reversible hash of the lower-cased username (no raw PII in kv)."""
    return "secret.target." + hashlib.sha256(username.lower().encode()).hexdigest()


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!secret`, parse, forward to dispatch.

    Returns `None` for non-matching payloads or while either flag is off. A
    malformed invocation is forwarded with `command="usage"` (never silently
    dropped, and the malformed text is NOT echoed).
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != "!secret":
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None
    if not await feature_enabled(FEATURE_FLAG_KEY, default=False):
        return None

    target_token, _, message = rest.strip().partition(" ")
    target = _normalize_target(target_token) if target_token else None
    message = message.strip()
    payload: dict[str, Any] = {
        "channel_id": event.payload.get("channel_id"),
        "message_id": event.payload.get("message_id"),
    }
    if target is None or not message or len(message) > _MAX_MESSAGE_CHARS:
        payload["command"] = "usage"
        log.info("secret.transform usage", platform=event.platform)
    else:
        payload.update(command="secret", target=target, message=message)
        log.info("secret.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload=payload,
        occurred_at=event.occurred_at,
    )


async def _resolve_target(community: str, username: str) -> tuple[str, str]:
    """Return `(hub_user_uuid, platform_user_id)` for `username`, or raise `SecretFlowError`."""
    try:
        raw = await community_kv.get(community, _target_key(username))
    except Exception as exc:  # noqa: BLE001 -- host error union, classified by type name only
        log.error("secret.resolve_failed", community_id=community, exc=type(exc).__name__)
        raise SecretFlowError("resolve", _ERR_TARGET) from exc
    if raw is None:
        raise SecretFlowError("target_unlinked", _ERR_TARGET)
    try:
        record = json.loads(raw.decode())
        return str(record["uuid"]), str(record["platform_user_id"])
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        log.error("secret.index_corrupt", community_id=community, exc=type(exc).__name__)
        raise SecretFlowError("index_corrupt", _ERR_TARGET) from exc


async def _store_secret(
    http_client: Any, config: dict[str, Any], *, community: str, target_uuid: str, message: str
) -> str:
    """POST the OTS create endpoint; return the one-time link token. Never logs body/token."""
    try:
        community_int = int(community)
    except ValueError as exc:
        raise SecretFlowError("bad_community", _ERR_STORE) from exc
    client = http_client if http_client is not None else HttpClient()
    body = json.dumps(
        {"communityId": community_int, "targetUserUuid": target_uuid, "message": message}
    ).encode()
    try:
        resp = await client.post(
            str(config["hub_api_url"]).rstrip("/") + "/api/v1/one-time-secrets",
            headers={"Content-Type": "application/json"},
            body=body,
            secret_refs={"Authorization": resolve_secret(SERVICE_TOKEN_SECRET)},
        )
    except Exception as exc:  # noqa: BLE001 -- transport union, classified by type name only
        log.error("secret.store_failed", community_id=community, exc=type(exc).__name__)
        raise SecretFlowError("store", _ERR_STORE) from exc
    if resp.get("status") != 201:
        log.error("secret.store_rejected", community_id=community, status=resp.get("status"))
        raise SecretFlowError("store_status", _ERR_STORE)
    try:
        return str(json.loads(bytes(resp["body"]).decode())["token"])
    except (ValueError, KeyError, TypeError) as exc:
        log.error("secret.store_bad_body", community_id=community, exc=type(exc).__name__)
        raise SecretFlowError("store_body", _ERR_STORE) from exc


async def _push(platform: str, op: dict[str, Any], *, community: str, fail: SecretFlowError) -> None:
    """Emit one outbound op over `relay`; any failure becomes `fail` (PII-free log)."""
    try:
        await relay.push(platform, {"v": 1, "platform": platform, **op})
    except Exception as exc:  # noqa: BLE001 -- host error union, classified by type name only
        log.error(
            "secret.relay_failed", op=op["op"], community_id=community, exc=type(exc).__name__
        )
        raise fail from exc


async def _run_flow(
    envelope: StageEnvelope, config: dict[str, Any], http_client: Any
) -> None:
    """Execute the ordered flow; raises `SecretFlowError` with the safe reply on any failure."""
    payload = envelope.event.payload
    platform = envelope.event.platform
    community = envelope.community
    channel_id, message_id = payload.get("channel_id"), payload.get("message_id")
    if platform not in SUPPORTED_PLATFORMS:
        raise SecretFlowError("unsupported_platform", _ERR_UNSUPPORTED)
    if not community or not channel_id or not message_id:
        raise SecretFlowError("missing_context", _ERR_STORE)

    target_uuid, platform_user_id = await _resolve_target(community, payload["target"])
    token = await _store_secret(
        http_client, config, community=community, target_uuid=target_uuid,
        message=payload["message"],
    )
    await _push(
        platform,
        {"op": "chat.delete", "channel": channel_id, "message_id": message_id},
        community=community,
        fail=SecretFlowError("delete", _ERR_DELETE),
    )
    link = f"{str(config['webui_url']).rstrip('/')}/secret#{token}"
    await _push(
        platform,
        {
            "op": "dm.send",
            "user_id": platform_user_id,
            "text": f"You have a one-time secret message. It can be opened once: {link}",
        },
        community=community,
        fail=SecretFlowError("dm", _ERR_DM),
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record the provider and a short, PII-free detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: run the store -> delete -> DM flow, then reply.

    Always replies to the sender with a generic notice. A failed step is
    logged (step name only) and replied, then re-raised so the host sees the
    failure -- never swallowed.

    Raises:
        ValueError: No `channel_id` to reply to.
        SecretFlowError: A flow step failed (after the failure reply was sent).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("secret reply requires a channel_id from the inbound chat.message")
    platform = envelope.event.platform
    community = envelope.community

    failure: SecretFlowError | None = None
    if payload.get("command") == "usage":
        reply = _USAGE
    elif payload.get("command") == "secret":
        try:
            await _run_flow(envelope, config, http_client)
            reply = _OK
        except SecretFlowError as exc:
            failure, reply = exc, exc.public_reply
            log.error("secret.flow_failed", step=exc.step, platform=platform, community_id=community)
    else:
        raise ValueError(f"unrecognized secret command: {payload.get('command')!r}")

    await relay.push(
        platform, {"v": 1, "op": "chat.send", "platform": platform, "channel": channel_id, "text": reply}
    )
    log.info("secret.dispatch done", platform=platform, community_id=community, ok=failure is None)
    if failure is not None:
        raise failure
    return DispatchResult(transport=platform, detail="relayed")
