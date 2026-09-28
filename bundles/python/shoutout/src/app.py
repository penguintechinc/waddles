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
  a cooldown reply. **The cooldown check FAILS CLOSED**: unlike every other
  degrade point in this module, a `kv` error/denial while checking the
  cooldown refuses the shoutout outright (logged at WARN with a `metric`
  field, no chat reply) rather than treating the outage as "not on
  cooldown" -- see `_check_cooldown()`'s own docstring for the full
  rationale and the current `kv`-capability gap this interacts with.
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
3. **`!vso` (video/clip shoutout) is out of scope.** Legacy's
   `VideoShoutoutService` (YouTube Data API + Twitch clips, its own
   per-platform video lookup and a `channel.follow`/raid-triggered
   auto-shoutout mode) is a materially larger surface than a single chat
   command and is not ported here -- `!vso` is simply not matched by
   this bundle's command regex, same as any other unrecognized command.
4. **Per-community custom templates are not read.** Legacy's
   `ShoutoutService._get_template()` read a `shoutout_templates` table
   for a community-custom message; this bundle only implements the two
   built-in Twitch templates (live/minimal) -- custom templates are a
   follow-up once this bundle proves out the `db`+`http` combination in
   production.
5. **`kv` (and `db`) are currently hardcoded `denied` host-side**
   (`core/{svc_process,svc_action}/src/capabilities.rs`), pending a
   separate in-flight PR (`feature/bundle-kv-capability`). This bundle is
   feature-flagged OFF by default so the gap is inert today; once the
   flag is turned on ahead of that capability PR landing, `_check_cooldown()`
   fails CLOSED on the resulting `kv` denial -- every `!so`/`!shoutout` is
   silently refused (logged at WARN with a `metric` field) rather than
   sent without a working anti-spam cooldown. See `_check_cooldown()`'s
   own docstring for why this is the one host-import failure in this
   module that blocks the action instead of degrading past it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from waddle_sdk import kv, log, relay
from waddle_sdk.db import AsyncDB
from waddle_sdk.flask_core.bundle_runtime import get_bundle_context, get_bundle_dal
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.http import resolve_secret

#: Matches `!so`/`!shoutout`, either alias -- text shoutout only (see module docstring, gap 3).
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

#: `shoutout_config.so_permission`'s own column default (migration 046) -- used whenever the
#: table/row/connection isn't reachable, never raised or treated as a denial.
_DEFAULT_PERMISSION = "mod"
#: `shoutout_config.cooldown_minutes`'s own column default (migration 046).
_DEFAULT_COOLDOWN_MINUTES = 60

#: Permission levels this bundle can never evaluate for lack of badge data (`vip`/
#: `subscriber`) plus the always-open `everyone` -- all three are satisfied unconditionally.
#: Same documented gap as `social_shoutout_process.py` (no badge data on `PlatformEvent`).
_ALWAYS_ALLOWED_PERMISSIONS = frozenset({"everyone", "vip", "subscriber"})
#: `community_members.role` values satisfying `admin_only` -- owner/admin tiers only.
_ADMIN_ONLY_ROLES = frozenset({"owner", "admin", "community-owner", "community-admin"})
#: `community_members.role` values satisfying `mod` -- moderator or above.
_MOD_OR_ABOVE_ROLES = frozenset(
    {"owner", "admin", "moderator", "community-owner", "community-admin"}
)

_SHOUTOUT_CONFIG_SQL = (
    "SELECT so_permission, cooldown_minutes FROM shoutout_config WHERE community_id = $1 LIMIT 1"
)
_ROLE_BY_PLATFORM_SQL = (
    "SELECT role FROM community_members "
    "WHERE community_id = $1 AND platform = $2 AND platform_user_id = $3 LIMIT 1"
)
_ROLE_BY_DISPLAY_NAME_SQL = (
    "SELECT role FROM community_members WHERE community_id = $1 AND display_name = $2 LIMIT 1"
)

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


def _community_id(community: str | None) -> int | None:
    """Best-effort `int(community)` for the config/role lookups; unparseable/`None` -> `None`."""
    if community is None:
        return None
    try:
        return int(community)
    except ValueError:
        return None


async def _shoutout_permission_and_cooldown(
    dal: AsyncDB, community_id: int | None
) -> tuple[str, int]:
    """Read `(so_permission, cooldown_minutes)` from `shoutout_config`; defaults on any miss.

    Degrades to `(_DEFAULT_PERMISSION, _DEFAULT_COOLDOWN_MINUTES)` on a missing community, a
    missing row, or any DB error -- never raises, never denies outright on an infrastructure
    problem (same degrade-not-deny contract as `social_shoutout_process._shoutout_permission`).
    """
    if community_id is None:
        return _DEFAULT_PERMISSION, _DEFAULT_COOLDOWN_MINUTES
    try:
        rows = await dal.execute(_SHOUTOUT_CONFIG_SQL, [community_id])
    except Exception:  # noqa: BLE001 -- must never block a shoutout, only degrade
        return _DEFAULT_PERMISSION, _DEFAULT_COOLDOWN_MINUTES
    if not rows:
        return _DEFAULT_PERMISSION, _DEFAULT_COOLDOWN_MINUTES
    row = rows[0]
    permission = str(row.get("so_permission") or _DEFAULT_PERMISSION)
    cooldown = row.get("cooldown_minutes")
    cooldown_minutes = int(cooldown) if cooldown is not None else _DEFAULT_COOLDOWN_MINUTES
    return permission, cooldown_minutes


async def _caller_role(dal: AsyncDB, event: PlatformEvent, community_id: int | None) -> str | None:
    """`community_members.role` for the caller, or `None` on any miss/error.

    Same lookup convention as `social_shoutout_process._caller_role` (match by `(platform,
    platform_user_id)` first, else `display_name == event.actor`). Fails closed (`None`) on a
    missing community, any lookup error, or no matching row; never raises.
    """
    if community_id is None:
        return None

    raw_author_id = event.payload.get("author_id")
    platform_user_id = raw_author_id if isinstance(raw_author_id, str) else None

    try:
        if platform_user_id:
            rows = await dal.execute(
                _ROLE_BY_PLATFORM_SQL, [community_id, event.platform, platform_user_id]
            )
            if rows:
                return str(rows[0]["role"]).lower()
        if event.actor:
            rows = await dal.execute(_ROLE_BY_DISPLAY_NAME_SQL, [community_id, event.actor])
            if rows:
                return str(rows[0]["role"]).lower()
    except Exception:  # noqa: BLE001 -- permission check must fail closed, never crash
        return None

    return None


def _permission_satisfied(permission: str, role: str | None) -> bool:
    """Evaluate `so_permission` against the caller's community role.

    `everyone`/`vip`/`subscriber` are always-satisfied (see module docstring on the
    `vip`/`subscriber` badge-data gap); `admin_only` requires owner/admin; `mod` requires
    moderator or above. An unrecognized value falls back to the `mod` threshold.
    """
    normalized = permission.lower()
    if normalized in _ALWAYS_ALLOWED_PERMISSIONS:
        return True
    if normalized == "admin_only":
        return role in _ADMIN_ONLY_ROLES
    return role in _MOD_OR_ABOVE_ROLES


def _cooldown_key(community: str | None, target: str) -> str:
    """Build the `kv` cooldown key for `target` in `community` (or the tenant-wide bucket)."""
    return f"{_COOLDOWN_KEY_PREFIX}:{community or '-'}:{target}"


#: `_check_cooldown()`'s three outcomes -- `UNAVAILABLE` is distinct from `ALLOWED` precisely
#: so a `kv` error/denial can never be silently treated as "not on cooldown" (see that
#: function's own docstring: the cooldown is an anti-spam control and must fail CLOSED).
_COOLDOWN_ALLOWED = "allowed"
_COOLDOWN_ACTIVE = "active"
_COOLDOWN_UNAVAILABLE = "unavailable"

#: Structured-log field identifying a WARN line as this metric for any log-based alerting/
#: counting pipeline scraping `waddle_sdk.log` output -- there is no dedicated WIT metrics
#: import in `wit/waddle-bundle/stage.wit` (only `context/http/kv/db/relay/%flags/log/clock`),
#: so a tagged WARN log is this sandbox's only available substitute for an emitted counter.
_KV_UNAVAILABLE_METRIC = "shoutout.cooldown_kv_unavailable"


async def _check_cooldown(community: str | None, target: str) -> str:
    """Read the `kv` cooldown key for `target` -- FAILS CLOSED on any `kv` error/denial.

    Returns one of `_COOLDOWN_ALLOWED`/`_COOLDOWN_ACTIVE`/`_COOLDOWN_UNAVAILABLE`. The cooldown
    is an anti-spam control, not a best-effort enrichment like the DB permission lookup or the
    Twitch Helix fetch elsewhere in this module -- a `kv` outage must never be silently read as
    "not on cooldown" (that would let a `kv` denial disable anti-spam entirely), so this is the
    one host-import failure in this bundle that blocks the action instead of degrading past it.
    Logs at WARN (with a `metric` field, see `_KV_UNAVAILABLE_METRIC`) on the failure path so
    the outage is observable; `transform()` sends no reply for this outcome (a plain
    "shoutouts unavailable" notice would itself need `kv`-backed per-channel rate-limiting to
    avoid becoming its own spam vector while `kv` is down, so this bundle deliberately says
    nothing rather than adding a second control that depends on the very capability that just
    failed).

    **Known, temporary gap**: the host `kv` capability is currently hardcoded to `denied` in
    `svc_process`/`svc_action` (`core/{svc_process,svc_action}/src/capabilities.rs`) pending a
    separate in-flight PR (`feature/bundle-kv-capability`) that wires it up for real -- until
    that lands, every `!so`/`!shoutout` in a community with this bundle's flag ON will hit
    `_COOLDOWN_UNAVAILABLE` and be silently refused. Flag defaults OFF, so this is inert today.
    """
    try:
        on_cooldown = await kv.get(_cooldown_key(community, target)) is not None
    except Exception as exc:  # noqa: BLE001 -- classified below, never re-raised past this point
        log.warn(
            "shoutout cooldown check unavailable -- refusing to send (fail closed)",
            metric=_KV_UNAVAILABLE_METRIC,
            community=community,
            target=target,
            error=str(exc),
        )
        return _COOLDOWN_UNAVAILABLE
    return _COOLDOWN_ACTIVE if on_cooldown else _COOLDOWN_ALLOWED


async def _set_cooldown(community: str | None, target: str, ttl_seconds: int) -> None:
    """Best-effort `kv` cooldown set, run only AFTER a shoutout has already been relayed.

    A `kv.set()` failure here cannot un-send the chat message already pushed by `dispatch()`,
    so unlike `_check_cooldown()` (which fails closed BEFORE anything is sent) this stays
    best-effort -- logged at WARN with the same `metric` tag for the same observability
    pipeline, but never raised. See `_check_cooldown()`'s docstring for the full `kv`-outage
    gap and why the fail-closed contract already covers the anti-spam guarantee going forward.
    """
    if ttl_seconds <= 0:
        return
    try:
        await kv.set(_cooldown_key(community, target), b"1", ttl_seconds)
    except Exception as exc:  # noqa: BLE001 -- observability only, must never fail a sent shoutout
        log.warn(
            "shoutout cooldown set failed after a successful relay",
            metric=_KV_UNAVAILABLE_METRIC,
            community=community,
            target=target,
            error=str(exc),
        )


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: parse `!so`/`!shoutout`, gate, and pass through.

    Order: prefix match -> feature flag (off -> `None`, no DB/kv touched) -> missing target ->
    invalid login format -> self-shoutout -> permission (config read + role lookup) ->
    cooldown (`kv` read only -- never set here; set on a successful `dispatch()` instead, so a
    permission-denied or failed lookup never burns a cooldown window). Only a fully successful
    parse is forwarded to this bundle's own `dispatch()`; every other reply (usage hint,
    invalid login, self-shoutout, permission denied, on cooldown) is a plain chat reply.

    Raises:
        ValueError: The event payload is missing a string `text` field.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    text = text.strip()
    if not text or not _SO_PREFIX_RE.match(text):
        return None  # not a shoutout command, skip

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
    community_id = _community_id(ctx.community)
    dal = get_bundle_dal()

    permission, cooldown_minutes = await _shoutout_permission_and_cooldown(dal, community_id)
    role = await _caller_role(dal, event, community_id)
    if not _permission_satisfied(permission, role):
        return _text_reply(event, _PERMISSION_DENIED_REPLY)

    cooldown_state = await _check_cooldown(ctx.community, target)
    if cooldown_state == _COOLDOWN_ACTIVE:
        return _text_reply(event, f"{target} was already shouted out recently, try again later")
    if cooldown_state == _COOLDOWN_UNAVAILABLE:
        # Fail closed: the anti-spam control couldn't be verified, so no shoutout is sent --
        # see _check_cooldown()'s docstring for why this stays silent (no chat reply) rather
        # than sending a "temporarily unavailable" notice that would need its own kv-backed
        # rate limiting to avoid becoming a spam vector itself.
        return None

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


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `bundles/python/pyping`'s
    identical class for why this exact duck-typed shape (`transport`, `detail`, `sub_type`,
    `http_status`) is what `waddle_sdk._component_entry.WitWorld.dispatch` reads off.
    """

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str, http_status: int | None = None) -> None:
        """Record which provider the shoutout was relayed to, a short detail, and any HTTP status."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = http_status


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
    """Implement `action-stage.dispatch`: enrich (best-effort), relay, and set the cooldown.

    `config` is accepted but unused (no `required_config`, see `bundle.yaml`). Always relays
    to the event's own origin platform (`envelope.event.platform`), never a hardcoded provider
    -- mirrors `bundles/python/pyping`'s own regression-tested behavior for this exact
    convention.

    Raises:
        ValueError: The envelope's payload has no `channel_id` or `target` (a malformed
            hand-off from `transform()` -- should never happen in practice).
    """
    payload = envelope.event.payload
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

    cooldown_minutes = payload.get("cooldown_minutes", _DEFAULT_COOLDOWN_MINUTES)
    ttl_seconds = int(cooldown_minutes) * 60
    await _set_cooldown(envelope.community, target, ttl_seconds)

    return DispatchResult(transport=provider, detail="relayed", http_status=200)
