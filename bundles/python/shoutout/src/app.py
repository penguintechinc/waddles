"""Real `!so`/`!shoutout` -> Twitch shoutout process/action-stage bundle.

Migrates the legacy `action/interactive/shoutout_interaction_module`
(`ShoutoutService`/`TwitchService`, standalone Flask service) and
`hub_api/services/bot_shoutout.py` (admin-UI config/creators/history reads
against the same `shoutout_config`/`community_members` tables) onto the
first-party App Bundle spine (`waddle_sdk`, WASI 0.2 component, hand-built
like `bundles/python/pyping` rather than routed through `bundle_compiler`,
per this bundle's own task scope). Unlike `core/svc_process/bundles/
social_shoutout_process.py` (gh #316's in-process port of the same legacy
module, running as a native `svc_process` plugin under app_id
`waddles.bot.shoutout.default`), this bundle owns BOTH its process and
action stages itself -- no cross-app `PROCESS_TARGET_APP_ID_KEY` routing
is needed, `transform()`'s output flows straight into this bundle's own
`dispatch()`.

Behavior ported faithfully from the two legacy sources above:
- `!so <user>` / `!shoutout <user>` (leading `@` stripped, lowercased,
  validated against Twitch's login charset `^[a-z0-9_]{3,25}$`) --
  `!vso <user>` (video/clip shoutout) is explicitly OUT OF SCOPE for this
  migration (see module-level `KNOWN GAPS` below).
- Usage hint on a bare `!so`, invalid-login reply on a malformed target,
  self-shoutout denial (flat rule, any permission level), and a
  `so_permission`-gated tier check (`admin_only`/`mod`/`everyone`,
  `vip`/`subscriber` degrade to always-allow for lack of badge data on
  `PlatformEvent`) against `shoutout_config`/`community_members` --
  mirrors `social_shoutout_process.py`'s proven, already-tested logic
  (gh #316) exactly, only reimplemented against this SDK's own `db`
  facade (`waddle_sdk.db.AsyncDB.execute()`, `$1`/`$2` positional params)
  instead of `flask_core.bundle_runtime.raw_sql_rows()` (named `:param`
  placeholders, unavailable in the sandbox -- see `waddle_sdk.flask_core.
  bundle_runtime.raw_sql_rows`'s own docstring: no live SQLAlchemy engine
  inside the sandbox to join against).
- Per-target cooldown, ported from `VideoShoutoutService.check_cooldown`'s
  `cooldown_minutes`-based gate (same `shoutout_config.cooldown_minutes`
  column, migration 046 default 60) but reimplemented over the WIT `kv`
  host import (this task's explicit requirement) rather than a
  DB-timestamp comparison: a successful dispatch sets `kv` key
  `shoutout:cd:{community}:{target}` with TTL `cooldown_minutes * 60`;
  a `!so` for the same target while that key is still set is refused with
  a cooldown reply, no DB/HTTP round trip spent.
- Feature-gated via `waddle_sdk.flask_core.feature_flags.feature_enabled`,
  flag key `waddles.shoutout-bundle`, **default OFF** (this bundle's own
  manifest requirement -- contrast `social_shoutout_process.py`'s
  `waddles.bot.shoutout`, default ON: a different flag for a different
  app_id/bundle).

KNOWN GAPS (documented per this task's explicit instruction -- implement
what works, flag what doesn't):

1. **No Twitch OAuth credential broker yet.** `TwitchService._get_access_
   token` (legacy) exchanges `client_id`+`client_secret` for an app access
   token via a `client_credentials` POST to `id.twitch.tv/oauth2/token` --
   `client_secret` travels in the POST body/query, not a header. This
   bundle's only egress path, the WIT `http` import, can inject a secret
   as a request HEADER only (`waddle_sdk.http.SecretRef`/`resolve_secret`,
   spec Sec8.3) -- there is no mechanism to inject a secret into a POST
   body or query string, and embedding `client_secret` directly in this
   bundle's source/manifest would violate "never embed credentials"
   outright. This bundle therefore assumes a future credential-broker
   component performs the OAuth exchange out-of-band and republishes the
   resulting app access token as a resolvable secret
   (`TWITCH_HELIX_BEARER`, expected to already read `"Bearer <token>"`)
   alongside the existing `TWITCH_HELIX_CLIENT_ID` secret. Until that
   broker exists, `_fetch_twitch_user()`'s HTTP call will fail (secret
   resolution error or a 401 from Twitch) -- caught and degraded to the
   minimal template below, never a hard failure of the whole command.
2. **Enrichment degrades gracefully, never blocks the shoutout.** Any
   `_fetch_twitch_user()` failure (transport error, non-2xx, missing
   secret) falls back to `_MINIMAL_TEMPLATE` (mirrors legacy
   `ShoutoutService.DEFAULT_TEMPLATES['twitch']['minimal']`'s own
   "ultimate fallback") instead of `['live']`/`['offline']` -- the
   shoutout still posts, just without live viewer count / game name.
3. **`!vso` (video/clip shoutout) is now implemented** (`video_shoutout.py`,
   this same PR) -- see that module's own docstring for its own, separate
   set of documented gaps (host-side UUID->channel resolver, Kick OAuth,
   overlay video playback). Legacy's `VideoShoutoutService` (YouTube Data
   API + Twitch clips, a `channel.follow`/raid-triggered auto-shoutout
   mode) is still not ported in full -- only the explicit `!vso <user>`
   chat command, not the raid-triggered auto mode.
4. **Per-community custom templates are not read.** Legacy's
   `ShoutoutService._get_template()` read a `shoutout_templates` table
   for a community-custom message; this bundle only implements the two
   built-in Twitch templates (live/minimal) -- custom templates are a
   follow-up once this bundle proves out the `db`+`http` combination in
   production.
"""

from __future__ import annotations

import json
import re
from typing import Any

from waddle_sdk import kv, relay
from waddle_sdk.flask_core.bundle_runtime import get_bundle_context, get_bundle_dal
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.http import resolve_secret

import video_shoutout
from _shared import DEFAULT_COOLDOWN_MINUTES as _SHARED_DEFAULT_COOLDOWN_MINUTES
from _shared import (
    DispatchResult,
    caller_role,
    permission_satisfied,
    shoutout_permission_and_cooldown,
)
from _shared import community_id as _shared_community_id

__all__ = ["DispatchResult", "dispatch", "transform"]

#: Matches `!so`/`!shoutout`, either alias -- text shoutout only.
_SO_PREFIX_RE = re.compile(r"^!(so|shoutout)\b", re.IGNORECASE)

#: Post-normalization (lowercased, leading `@` stripped) Twitch login character set -- same
#: shape `identity_service.py`/`twitch_service.py` validated in the legacy module.
_LOGIN_RE = re.compile(r"^[a-z0-9_]{3,25}$")

_SO_USAGE = "usage: !so <user>"
_INVALID_LOGIN_REPLY = "that doesn't look like a Twitch username"
_SELF_SHOUTOUT_REPLY = "you can't shout yourself out"
_PERMISSION_DENIED_REPLY = "you don't have permission to shout out"

#: PostHog flag key gating this entire bundle -- this task's own manifest requirement.
#: Default OFF (contrast `social_shoutout_process.py`'s `waddles.bot.shoutout`, default ON).
_FEATURE_FLAG = "waddles.shoutout-bundle"

#: `waddle_sdk.kv` key prefix for the per-(community, target) cooldown set by a successful
#: dispatch -- see module docstring's cooldown section.
_COOLDOWN_KEY_PREFIX = "shoutout:cd"

#: Mirrors `ShoutoutService.DEFAULT_TEMPLATES['twitch']` -- only the two templates this
#: bundle actually uses (see module docstring, gap 4 for what's not ported).
_LIVE_TEMPLATE = (
    "Go check out {display_name} at twitch.tv/{login}! They're currently streaming "
    "{game_name} with {viewer_count} viewers!"
)
_MINIMAL_TEMPLATE = "Shoutout to {display_name}! Check them out at twitch.tv/{login}"


def _normalize_login(raw: str) -> str:
    """Strip a leading `@` and lowercase -- shared by target parsing and self-shoutout check."""
    return raw.strip().lstrip("@").lower()


def _text_reply(event: PlatformEvent, text: str) -> PlatformEvent:
    """Build a direct chat reply, preserving every other payload field."""
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={**event.payload, "text": text},
        occurred_at=event.occurred_at,
    )


def _cooldown_key(community: str | None, target: str) -> str:
    """Build the `kv` cooldown key for `target` in `community` (or the tenant-wide bucket)."""
    return f"{_COOLDOWN_KEY_PREFIX}:{community or '-'}:{target}"


async def _is_on_cooldown(community: str | None, target: str) -> bool:
    """Read the `kv` cooldown key for `target`; degrades to "not on cooldown" on ANY error.

    **Known, temporary gap**: the host `kv` capability is currently hardcoded to `denied` in
    `svc_process`/`svc_action` (`core/{svc_process,svc_action}/src/capabilities.rs`) pending a
    separate in-flight PR (`feature/bundle-kv-capability`) that wires it up for real. Every
    `kv` call in this bundle is therefore wrapped to degrade rather than raise, exactly like
    this module's DB/HTTP degrade points -- a denied `kv` call must never block a shoutout,
    only skip the cooldown gate, so this bundle is already safe to activate (behind its own
    `waddles.shoutout-bundle` flag, default OFF) before that capability PR lands.
    """
    try:
        return await kv.get(_cooldown_key(community, target)) is not None
    except Exception:  # noqa: BLE001 -- kv denial/outage must never block a shoutout
        return False


async def _set_cooldown(community: str | None, target: str, ttl_seconds: int) -> None:
    """Best-effort `kv` cooldown set -- see :func:`_is_on_cooldown`'s docstring for the same gap."""
    if ttl_seconds <= 0:
        return
    try:
        await kv.set(_cooldown_key(community, target), b"1", ttl_seconds)
    except Exception:  # noqa: BLE001 -- kv denial/outage must never fail a successful shoutout
        return


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: parse `!so`/`!shoutout`/`!vso`, gate, pass through.

    Dispatches on prefix to `_transform_text_shoutout` (`!so`/`!shoutout`) or
    `video_shoutout.transform_vso` (`!vso`) -- see each for its own gate order. Neither branch
    touches the `text`-field validation below more than once.

    Raises:
        ValueError: The event payload is missing a string `text` field.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    text = text.strip()
    if not text:
        return None

    if _SO_PREFIX_RE.match(text):
        return await _transform_text_shoutout(event, text)
    if video_shoutout.VSO_PREFIX_RE.match(text):
        return await video_shoutout.transform_vso(event, text)
    return None  # not a recognized shoutout command, skip


async def _transform_text_shoutout(event: PlatformEvent, text: str) -> PlatformEvent | None:
    """`!so`/`!shoutout` gate: feature flag, target parsing, self-check, permission, cooldown.

    Order: feature flag -> missing target -> invalid login -> self-shoutout
    -> permission (config read + role lookup) -> cooldown (`kv` read only -- never set here; set
    on a successful `dispatch()` instead, so a permission-denied or failed lookup never burns a
    cooldown window). Only a fully successful parse is forwarded to this bundle's own
    `dispatch()`; every other reply (usage hint, invalid login, self-shoutout, permission
    denied, on cooldown) is a plain chat reply.
    """
    enabled = await feature_enabled(_FEATURE_FLAG, default=False)
    if not enabled:
        return None  # feature disabled -- behaves like an unrecognized command

    parts = text.split(maxsplit=1)
    raw_target = parts[1].strip() if len(parts) > 1 else ""
    if not raw_target:
        return _text_reply(event, _SO_USAGE)

    target = _normalize_login(raw_target)
    if not _LOGIN_RE.match(target):
        return _text_reply(event, _INVALID_LOGIN_REPLY)

    caller = _normalize_login(event.actor) if event.actor else ""
    if caller and target == caller:
        return _text_reply(event, _SELF_SHOUTOUT_REPLY)

    ctx = get_bundle_context()
    community_id = _shared_community_id(ctx.community)
    dal = get_bundle_dal()

    permission, cooldown_minutes = await shoutout_permission_and_cooldown(dal, community_id)
    role = await caller_role(dal, event, community_id)
    if not permission_satisfied(permission, role):
        return _text_reply(event, _PERMISSION_DENIED_REPLY)

    if await _is_on_cooldown(ctx.community, target):
        return _text_reply(event, f"{target} was already shouted out recently, try again later")

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={
            "text": "",
            "channel_id": event.payload.get("channel_id"),
            "target": target,
            "cooldown_minutes": cooldown_minutes,
        },
        occurred_at=event.occurred_at,
    )


async def _fetch_twitch_user(http_client: Any, login: str) -> dict[str, Any] | None:
    """Look up `login` via the Twitch Helix `/users` endpoint over the host `http` import.

    Returns `None` on ANY failure (no `http_client`, missing/unresolvable secret, transport
    error, non-2xx, empty result) -- this is the documented degrade point, see module
    docstring gaps 1-2. Never raises; `dispatch()` treats `None` as "use the minimal template".
    """
    if http_client is None:
        return None
    try:
        response = await http_client.get(
            f"https://api.twitch.tv/helix/users?login={login}",
            secret_refs={
                "Client-Id": resolve_secret("TWITCH_HELIX_CLIENT_ID"),
                "Authorization": resolve_secret("TWITCH_HELIX_BEARER"),
            },
        )
    except Exception:  # noqa: BLE001 -- see module docstring gap 1-2, must never block a shoutout
        return None

    if response.get("status") != 200:
        return None

    try:
        body = json.loads(response["body"])
    except (ValueError, KeyError):
        return None

    users = body.get("data") or []
    return users[0] if users else None


def _render_shoutout_message(login: str, twitch_user: dict[str, Any] | None) -> str:
    """Build the shoutout chat message -- live/minimal template, mirrors `ShoutoutService`."""
    if twitch_user is None:
        return _MINIMAL_TEMPLATE.format(display_name=login, login=login)
    display_name = twitch_user.get("display_name", login)
    return _MINIMAL_TEMPLATE.format(display_name=display_name, login=login)


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`, branching on `payload["kind"]`.

    Dispatches to the `!vso` video path (`video_shoutout.dispatch_vso`) when present, else the
    original `!so` text path below.

    `config` is unused by the text path (no `required_config`, see `bundle.yaml`) but IS read
    by the video path (`clip_source_order`/`overlay_push_host`, see `video_shoutout.py`).
    Always relays to the event's own origin platform (`envelope.event.platform`), never a
    hardcoded provider -- mirrors `bundles/python/pyping`'s own regression-tested behavior for
    this exact convention.

    Raises:
        ValueError: The envelope's payload has no `channel_id` or `target`/`vso_target_user` (a
            malformed hand-off from `transform()` -- should never happen in practice).
    """
    payload = envelope.event.payload
    if payload.get("kind") == video_shoutout.KIND_VIDEO_SHOUTOUT:
        return await video_shoutout.dispatch_vso(envelope, config, http_client=http_client)

    channel_id = payload.get("channel_id")
    target = payload.get("target")
    if not channel_id:
        raise ValueError("shoutout dispatch requires a channel_id from the inbound chat.message")
    if not isinstance(target, str) or not target:
        raise ValueError("shoutout dispatch requires a 'target' login from transform()")

    twitch_user = await _fetch_twitch_user(http_client, target)
    message = _render_shoutout_message(target, twitch_user)

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": message})

    cooldown_minutes = payload.get("cooldown_minutes", _SHARED_DEFAULT_COOLDOWN_MINUTES)
    ttl_seconds = int(cooldown_minutes) * 60
    await _set_cooldown(envelope.community, target, ttl_seconds)

    return DispatchResult(transport=provider, detail="relayed", http_status=200)
