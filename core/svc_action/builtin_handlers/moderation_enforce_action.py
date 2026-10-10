"""moderation_enforce_action.py -- ACTION-stage moderation ENFORCEMENT (timeout + warn).

Per `docs/plans/2026-09-08-content-moderation-design.md` SS2/SS12 P4 and
gh-304: `core/svc_process/services/moderation_gate.py` (P1) already
applies a reputation hit on every classifier match, unconditionally. THIS
handler is the enforcement half -- a Discord member timeout or a Twitch
timed ban, PLUS a warn message -- applied ONLY when the community has the
matched category's filter turned on (the gate's own job to decide, not
this handler's).

**Contract this handler codes against** (`moderation_gate.py` does not yet
emit it onto the envelope it produces -- that wiring is a follow-up
chunk; this module defines the shape it will carry so both sides can be
built independently):

    envelope.event.payload["moderation_enforcement"] = {
        "category": str,        # matched category, e.g. "hate_speech"
        "score": float,         # classifier confidence/severity, 0.0-1.0 (optional, informational)
        "timeout_s": int,       # <=0 means "warn only, no timeout" for this match
        "warn_text": str,       # message posted to the user/channel
        "action": str,          # optional; "none" explicitly suppresses ALL enforcement
    }

Absent (key missing, `None`, not a dict, or missing `category`/
`warn_text`) means the gate did not request enforcement for this message
(no match, or the community's filter for the matched category is OFF) --
this entrypoint is a pure no-op in that case. `action == "none"` is an
explicit, always-respected suppression on top of that, independent of
`timeout_s` (covers a gate that wants to log/flag a match without acting
on it at all, distinct from "warn but don't timeout").

Standard action-stage entrypoint contract (`runner.py` module docstring):
`async def enforce(envelope, config, *, http_client) -> TransportResult`,
raising `waddle_transports.{Retryable,NonRetryable}TransportError` with a
SPECIFIC message on every failure path (design doc SS2: "error states
must be specific, e.g. 'oauth token didn't work'") -- `runner.py::
_handle_envelope`'s own dispatch loop requires exactly this contract
(`isinstance(result, TransportResult)` on success, its two typed
exceptions on failure) to compose with its `retry_with_backoff` wrapper,
same as every sibling handler (`discord_send_action.py`/
`twitch_send_action.py`/`social_alias_action.py`). On success (including
every no-op/skip path below), `TransportResult.detail` carries the
structured `applied=<bool> action=<...> reason=<...>` outcome inline --
`TransportResult` itself has no room for extra fields, matching how
`discord_send_action.py` embeds its own `channel=`/`message_id=` detail.

Discord/Twitch HTTP and relay call logic lives in `services/
platform_moderation.py`; this module owns config parsing, target-user/
channel/guild resolution, the self-message / non-positive-timeout /
explicit-`action=none` skip logic, and combining the warn + timeout
outcomes into one `TransportResult`.

`_enforce_twitch`'s Twitch timeout path (gh-320) tries a per-community
Twitch user token (`services.platform_moderation
.resolve_community_moderator_token`) BEFORE the static
`moderator_token_ref` config below -- a community with no connected
Twitch account falls through to that existing config path unchanged.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

import httpx
import redis.asyncio as redis
from flask_core import StageEnvelope
from waddle_transports import NonRetryableTransportError, TransportResult
from waddle_transports.signing import SecretResolutionError, resolve_secret

from services.platform_moderation import (
    DEFAULT_DISCORD_API_BASE,
    DEFAULT_TWITCH_API_BASE,
    EnforcementOutcome,
    discord_timeout,
    discord_warn,
    resolve_community_moderator_token,
    twitch_timeout,
    twitch_warn,
)

logger = logging.getLogger(__name__)

#: `PlatformEvent.platform` values this handler knows how to enforce
#: against -- any other value is a config/routing error, not a silent
#: no-op (an app_catalog seed pointing this entrypoint at an unsupported
#: platform is a mistake worth surfacing, not swallowing).
_SUPPORTED_PLATFORMS = frozenset({"discord", "twitch"})

#: Lazily-built, process-wide Valkey client for the Twitch warn's outbound
#: IRC relay -- same pattern and rationale as `twitch_send_action.py`'s
#: own `_get_redis_client` (not passed via the fixed `(envelope, config,
#: *, http_client)` entrypoint signature, so built/cached here instead).
_redis_client: redis.Redis | None = None


def _get_redis_client(config: Mapping[str, Any]) -> redis.Redis:  # noqa: ARG001 - signature parity
    """Build (once) or return the cached Valkey client for the outbound IRC warn relay.

    Reads `VALKEY_URL`/`REDIS_URL` env vars, mirroring `twitch_send_
    action.py::_get_redis_client`'s own fallback chain. Tests monkeypatch
    this function directly (module-level, easy `monkeypatch.setattr`
    target) to return a `fakeredis.FakeAsyncRedis` instead.
    """
    global _redis_client
    if _redis_client is None:
        url = (
            os.environ.get("VALKEY_URL")
            or os.environ.get("REDIS_URL")
            or "redis://localhost:6379/0"
        )
        _redis_client = redis.from_url(url, decode_responses=True)
    return _redis_client


def _mask_actor(actor: str | None) -> str:
    """Mask a platform display name/id for logging -- `moderation_gate.py`'s own convention."""
    if not actor:
        return "<unknown>"
    if len(actor) <= 2:
        return actor[0] + "*"
    return actor[:2] + "*" * (len(actor) - 2)


def _resolve_str(value: object) -> str | None:
    """Non-empty `str` or `None` -- shared guard for every payload/config field read here."""
    return value if isinstance(value, str) and value else None


def _extract_enforcement(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Parse+validate `payload["moderation_enforcement"]` per this module's own contract.

    Returns `None` (no-op) for anything malformed rather than raising --
    a gate that hasn't been wired up yet (or a future gate bug) must
    never turn into a dispatch failure for this handler; absence/
    malformity is indistinguishable from "no enforcement requested."
    """
    raw = payload.get("moderation_enforcement")
    if not isinstance(raw, dict):
        return None
    category = _resolve_str(raw.get("category"))
    warn_text = _resolve_str(raw.get("warn_text"))
    if category is None or warn_text is None:
        return None
    score = raw.get("score")
    timeout_s = raw.get("timeout_s")
    action = raw.get("action")
    return {
        "category": category,
        "score": score if isinstance(score, (int, float)) else 0.0,
        "timeout_s": timeout_s if isinstance(timeout_s, int) else 0,
        "warn_text": warn_text,
        "action": action if isinstance(action, str) else None,
    }


def _resolve_target_user_id(payload: Mapping[str, Any]) -> str | None:
    """The platform-native id to enforce against, or `None` if unresolvable.

    Prefers `payload["author_id"]` (Discord's own field,
    `discord_ingest.py`'s normalized shape; also `moderation_gate.py`'s
    own `_resolve_platform_user_id` preference), falling back to
    `payload["user_id"]` (the numeric id shape `twitch_eventsub_ingest.py`
    normalizes to) -- Twitch's own chat-IRC ingest (`twitch_ingest.py`)
    does not currently carry a numeric user id at all (only a display
    username), a real upstream gap this handler cannot fix; a Twitch
    envelope without one refuses with a specific error below rather than
    guessing from a display name.
    """
    author_id = _resolve_str(payload.get("author_id"))
    if author_id is not None:
        return author_id
    return _resolve_str(payload.get("user_id"))


def _combine_outcomes(outcomes: list[EnforcementOutcome]) -> TransportResult:
    """Fold every applied `EnforcementOutcome` into one `TransportResult`.

    `outcomes` is never empty when this is called (every call site either
    returns its own skip `TransportResult` directly or appends at least
    the warn outcome first) -- see `enforce()`.
    """
    actions = "+".join(outcome.action for outcome in outcomes)
    reason = "; ".join(outcome.detail for outcome in outcomes)
    http_status = outcomes[-1].http_status
    return TransportResult(
        transport="bundle",
        detail=f"applied=True action={actions} reason={reason}",
        http_status=http_status,
    )


def _skip_result(reason: str) -> TransportResult:
    """A no-op outcome -- still a SUCCESSFUL dispatch (`runner.py` requires `TransportResult`)."""
    return TransportResult(transport="bundle", detail=f"applied=False action=none reason={reason}")


async def _enforce_discord(
    event_payload: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    category: str,
    warn_text: str,
    timeout_s: int,
    target_user_id: str,
    http_client: httpx.AsyncClient,
) -> list[EnforcementOutcome]:
    """Warn (always), then a member timeout if `timeout_s > 0` -- Discord side of `enforce`."""
    bot_token_ref = config.get("bot_token_ref")
    if not isinstance(bot_token_ref, str) or not bot_token_ref:
        raise NonRetryableTransportError(
            "discord moderation enforcement config missing required 'bot_token_ref'"
        )
    try:
        bot_token = resolve_secret(bot_token_ref)
    except SecretResolutionError as exc:
        raise NonRetryableTransportError(f"discord bot token resolution failed: {exc}") from exc

    channel_id = _resolve_str(event_payload.get("channel_id")) or _resolve_str(
        config.get("channel_id")
    )
    if channel_id is None:
        raise NonRetryableTransportError(
            "discord moderation enforcement could not resolve a channel_id from either "
            "envelope.event.payload['channel_id'] or config['channel_id']"
        )

    api_base = config.get("api_base", DEFAULT_DISCORD_API_BASE)
    api_base_str = api_base if isinstance(api_base, str) and api_base else DEFAULT_DISCORD_API_BASE

    outcomes = [
        await discord_warn(
            http_client,
            channel_id=channel_id,
            text=warn_text,
            bot_token=bot_token,
            api_base=api_base_str,
        )
    ]

    if timeout_s > 0:
        guild_id = _resolve_str(event_payload.get("guild_id")) or _resolve_str(
            config.get("guild_id")
        )
        if guild_id is None:
            raise NonRetryableTransportError(
                "discord moderation enforcement could not resolve a guild_id from either "
                "envelope.event.payload['guild_id'] or config['guild_id']"
            )
        outcomes.append(
            await discord_timeout(
                http_client,
                guild_id=guild_id,
                user_id=target_user_id,
                timeout_s=timeout_s,
                reason=category,
                bot_token=bot_token,
                api_base=api_base_str,
            )
        )

    return outcomes


async def _enforce_twitch(
    event_payload: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    category: str,
    warn_text: str,
    timeout_s: int,
    target_user_id: str,
    community_id: int | None,
    http_client: httpx.AsyncClient,
) -> list[EnforcementOutcome]:
    """Warn (always, via the existing IRC relay) then, if `timeout_s > 0`, a Helix timed ban."""
    channel = _resolve_str(event_payload.get("channel_name")) or _resolve_str(
        config.get("channel_name")
    )
    if channel is None:
        raise NonRetryableTransportError(
            "twitch moderation enforcement could not resolve a channel from either "
            "envelope.event.payload['channel_name'] or config['channel_name']"
        )

    outcomes = [
        await twitch_warn(channel=channel, text=warn_text, redis_client=_get_redis_client(config))
    ]

    if timeout_s > 0:
        # gh-320: a per-community-connected Twitch account's user token
        # takes priority over the static `moderator_token_ref` config
        # below -- tried first, never required to be configured when a
        # community connection exists. `None` (no connection, resolver
        # unavailable, or a resolver failure) falls through unchanged.
        moderator_token = await resolve_community_moderator_token(community_id)
        if moderator_token is None:
            moderator_token_ref = config.get("moderator_token_ref")
            if not isinstance(moderator_token_ref, str) or not moderator_token_ref:
                # Deliberately checked BEFORE any Helix call -- an app/
                # client-credentials token (the only kind `bot_token_ref`
                # handlers like `twitch_send_action.py` ever resolve) can
                # never authorize `/moderation/bans`; refusing here means
                # this handler never even attempts a call it knows will be
                # rejected, i.e. it never "fakes success" by half-trying.
                raise NonRetryableTransportError(
                    "twitch moderation requires a user token with moderator:manage:banned_users"
                )
            try:
                moderator_token = resolve_secret(moderator_token_ref)
            except SecretResolutionError as exc:
                raise NonRetryableTransportError(
                    f"twitch moderator token resolution failed: {exc}"
                ) from exc

        client_id = _resolve_str(config.get("client_id"))
        if client_id is None:
            raise NonRetryableTransportError(
                "twitch moderation enforcement config missing required 'client_id'"
            )
        broadcaster_id = _resolve_str(event_payload.get("broadcaster_id")) or _resolve_str(
            config.get("broadcaster_id")
        )
        if broadcaster_id is None:
            raise NonRetryableTransportError(
                "twitch moderation enforcement could not resolve a broadcaster_id from either "
                "envelope.event.payload['broadcaster_id'] or config['broadcaster_id']"
            )
        moderator_id = _resolve_str(config.get("moderator_id"))
        if moderator_id is None:
            raise NonRetryableTransportError(
                "twitch moderation enforcement config missing required 'moderator_id'"
            )

        api_base = config.get("api_base", DEFAULT_TWITCH_API_BASE)
        api_base_str = (
            api_base if isinstance(api_base, str) and api_base else DEFAULT_TWITCH_API_BASE
        )
        outcomes.append(
            await twitch_timeout(
                http_client,
                broadcaster_id=broadcaster_id,
                moderator_id=moderator_id,
                user_id=target_user_id,
                timeout_s=timeout_s,
                reason=category,
                moderator_token=moderator_token,
                client_id=client_id,
                api_base=api_base_str,
            )
        )

    return outcomes


async def enforce(
    envelope: StageEnvelope,
    config: Mapping[str, Any],
    *,
    http_client: httpx.AsyncClient,
) -> TransportResult:
    """Apply the gate's moderation decision: warn (+ timeout) on Discord/Twitch, or no-op.

    See module docstring for the full `moderation_enforcement` contract
    and skip conditions. Never enforces against the bot's own message
    (`config["bot_user_id"]` compared against the resolved target id).
    Raises a SPECIFIC `NonRetryableTransportError`/`RetryableTransportError`
    on every real failure path; every no-op/skip path returns a
    successful `TransportResult` instead (`runner.py`'s dispatch loop
    requires exactly one `TransportResult` per call, success or no-op
    alike -- only a genuine failure may raise).
    """
    event_payload = envelope.event.payload
    enforcement = _extract_enforcement(event_payload)
    if enforcement is None:
        return _skip_result("no moderation_enforcement in payload")
    if enforcement["action"] == "none":
        return _skip_result("enforcement action explicitly none")

    platform = envelope.event.platform
    if platform not in _SUPPORTED_PLATFORMS:
        raise NonRetryableTransportError(
            f"moderation enforcement bundle does not support platform: {platform}"
        )

    target_user_id = _resolve_target_user_id(event_payload)
    if target_user_id is None:
        raise NonRetryableTransportError(
            "moderation enforcement could not resolve a target user id from "
            "event.payload['author_id'] (or ['user_id'] for twitch)"
        )

    bot_user_id = _resolve_str(config.get("bot_user_id"))
    if bot_user_id is not None and target_user_id == bot_user_id:
        logger.debug(
            "moderation.enforce_skip_self platform=%s category=%s",
            platform,
            enforcement["category"],
        )
        return _skip_result("target is the bot's own message")

    category = enforcement["category"]
    warn_text = enforcement["warn_text"]
    timeout_s = enforcement["timeout_s"]

    community_id: int | None
    try:
        community_id = int(envelope.community) if envelope.community is not None else None
    except (TypeError, ValueError):
        community_id = None

    logger.debug(
        "moderation.enforce_decision platform=%s category=%s timeout_s=%s actor=%s",
        platform,
        category,
        timeout_s,
        _mask_actor(envelope.event.actor),
    )

    if platform == "discord":
        outcomes = await _enforce_discord(
            event_payload,
            config,
            category=category,
            warn_text=warn_text,
            timeout_s=timeout_s,
            target_user_id=target_user_id,
            http_client=http_client,
        )
    else:
        outcomes = await _enforce_twitch(
            event_payload,
            config,
            category=category,
            warn_text=warn_text,
            timeout_s=timeout_s,
            target_user_id=target_user_id,
            community_id=community_id,
            http_client=http_client,
        )

    action_summary = "+".join(outcome.action for outcome in outcomes)
    logger.info(
        "moderation.enforced platform=%s community=%s category=%s action=%s duration_s=%s",
        platform,
        envelope.community,
        category,
        action_summary,
        max(timeout_s, 0),
    )
    return _combine_outcomes(outcomes)
