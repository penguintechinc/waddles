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
5. **`!aiso <user>` (AI shoutout) has no native `ai.generate` host
   capability yet.** A dedicated WIT import for AI generation is being
   designed on branch `docs/bundle-permissions-capability-gate` (not yet
   landed -- its tip is still even with `main`). Until it lands, this
   bundle implements against a small local interface (`AiGenerator`) so
   swapping in the real capability later is a one-line change at
   `_try_ai_shoutout`'s single call site; the interim implementation
   (`_HttpAiGenerator`) goes over the existing `http` WIT import to
   hub-api's own `/api/v1/community/<id>/ai/completions` proxy (the same
   endpoint `core/svc_action/bundles/integrations_waddleai_action.py`
   calls natively), authenticated with a `resolve_secret()` bearer
   SecretRef -- never an embedded key. Two carry-over gaps from that
   endpoint's own design, both non-blocking (any failure degrades to the
   generic template, see below): (a) hub-api's Service DNS name is
   Helm-release-dependent (`{{ fullname }}-hub-api-v3`), not a fixed
   literal this bundle's static `egress` allowlist can encode once for
   every deployment -- `config.waddleai_base_url` (default
   `http://hub-api:8204`, matching `HUB_API_URL`'s own compose-mode
   default) lets an operator override it, but the matching `egress` host
   entry below is a placeholder an operator must keep in sync until
   templated/env-driven egress hosts (or the native capability) exist;
   (b) that endpoint's `require_community_member()` gate expects a real
   end-user session, not yet a machine/service-account caller -- a 401/403
   from it is caught exactly like any other generation failure.

   **PII boundary (hard rule, mandatory).** The prompt sent to WaddleAI,
   and the text WaddleAI returns, NEVER contain the target's login or
   display name -- only the literal placeholder token `{user}` plus
   public, non-identifying stream metadata (Twitch category/title, best-
   effort via `_fetch_twitch_stream_info()`). The real name is substituted
   back in locally, inside this same bundle's `dispatch()`, only AFTER the
   AI response is received, placeholder-checked, and moderated -- WaddleAI
   itself never receives a name, a UUID, or any other per-user identifier.
   `!aiso` is gated on WaddleAI's Enterprise tier (`waddle_sdk.flags.
   tier()`) AND the PostHog flag `waddles.shoutout-ai` (default OFF, see
   `_ai_eligible()`); either missing degrades silently to the plain `!so`
   template, never an error reply. Per-channel and per-target cooldowns
   (`kv`, see `_is_aiso_on_cooldown()`) gate the AI call itself and, unlike
   `!so`'s own cooldown, **fail CLOSED** on a `kv` outage/denial (an
   unmetered AI call is a real cost/abuse surface the free `relay` path
   isn't) -- a cooldown hit or any generation/moderation failure both
   degrade to the same generic template, never a hard denial of the
   underlying shoutout.

   **Metrics gap.** There is no `metrics` WIT host capability (histogram/
   counter) in `wit/waddle-bundle/stage.wit` today, only `log` -- the AI
   latency histogram and fallback counter this task calls for are emitted
   as structured `log.info`/`log.warn` lines (`metric="shoutout_ai_
   latency_ms"` / `"shoutout_ai_fallback_total"`) for a log-to-metric
   pipeline to aggregate, pending a native histogram/counter import.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from waddle_sdk import clock, flags, kv, log, relay
from waddle_sdk.db import AsyncDB
from waddle_sdk.flask_core.bundle_runtime import get_bundle_context, get_bundle_dal
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.http import resolve_secret

#: Matches `!so`/`!shoutout`, either alias -- text shoutout only (see module docstring, gap 3).
_SO_PREFIX_RE = re.compile(r"^!(so|shoutout)\b", re.IGNORECASE)

#: Matches `!aiso` -- the AI-generated shoutout variant (see module docstring, gap 5).
#: Deliberately its own pattern, never folded into `_SO_PREFIX_RE`'s alternation, so the
#: two commands stay independently testable and `!vso` (out of scope, sibling PR) can't
#: collide with either.
_AISO_PREFIX_RE = re.compile(r"^!aiso\b", re.IGNORECASE)

#: Post-normalization (lowercased, leading `@` stripped) Twitch login character set -- same
#: shape `identity_service.py`/`twitch_service.py` validated in the legacy module.
_LOGIN_RE = re.compile(r"^[a-z0-9_]{3,25}$")

_SO_USAGE = "usage: !so <user>"
_AISO_USAGE = "usage: !aiso <user>"
_INVALID_LOGIN_REPLY = "that doesn't look like a Twitch username"
_SELF_SHOUTOUT_REPLY = "you can't shout yourself out"
_PERMISSION_DENIED_REPLY = "you don't have permission to shout out"

#: PostHog flag key gating this entire bundle -- this task's own manifest requirement.
#: Default OFF (contrast `social_shoutout_process.py`'s `waddles.bot.shoutout`, default ON).
_FEATURE_FLAG = "waddles.shoutout-bundle"

#: PostHog flag key gating `!aiso` specifically, on top of `_FEATURE_FLAG` above -- this
#: task's own requirement. Default OFF; missing/OFF degrades to the generic `!so`
#: template, never an error (see `_ai_eligible()`).
_AI_FEATURE_FLAG = "waddles.shoutout-ai"
#: `waddle_sdk.flags.tier()` value required for `!aiso` -- anything else degrades to
#: `!so`, same as the PostHog flag being off.
_AI_REQUIRED_TIER = "enterprise"

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

# --------------------------------------------------------------------------
# `!aiso` -- AI-generated shoutout. See module docstring, gap 5, for the full design
# (PII boundary, tier/flag gate, fail-closed cooldown, metrics-via-log gap).
# --------------------------------------------------------------------------

#: Literal placeholder the AI prompt instructs WaddleAI to write around instead of a
#: name -- substituted for the real display name locally, after generation/moderation.
_AI_PLACEHOLDER = "{user}"
#: Hard caps: keeps the outbound prompt small and the WaddleAI reply short enough to
#: always fit in a single chat message on every supported platform.
_AI_PROMPT_MAX_CHARS = 500
_AI_OUTPUT_MAX_CHARS = 300
_AI_MAX_TOKENS = 80

_AI_SYSTEM_INSTRUCTION = (
    "Write ONE short, upbeat, friendly shoutout sentence for a Twitch chat bot. "
    "Refer to the streamer ONLY by the literal placeholder token {user} -- never "
    "invent or guess a name. No links, no hashtags, no profanity. Keep it under 40 "
    "words."
)

#: `waddle_sdk.kv` key prefix for `!aiso`'s own per-channel/per-target cooldowns --
#: deliberately a different namespace from `_COOLDOWN_KEY_PREFIX` (the two commands'
#: cooldowns are independent, see module docstring gap 5).
_AISO_COOLDOWN_KEY_PREFIX = "shoutout:aicd"
_AISO_CHANNEL_COOLDOWN_SECONDS = 300
_AISO_TARGET_COOLDOWN_SECONDS = 1800

#: Default hub-api base URL for the WaddleAI completions proxy -- matches
#: `core/svc_action/config.py`'s own `HUB_API_URL` compose-mode default. Override via
#: the bundle's resolved `config["waddleai_base_url"]` (see module docstring gap 5(a)
#: on why this can't be a single static value for every deployment).
_DEFAULT_WADDLEAI_BASE_URL = "http://hub-api:8204"

#: Basic denylist -- interim substitute for a real platform-moderation capability (no
#: WIT `moderation` import exists; `libs/moderation_module` is a svc-process-side
#: service, not importable inside this sandbox). Deliberately small and easy to extend;
#: any hit fails the AI text closed to the generic template, never a partial redaction.
_MODERATION_DENYLIST = frozenset(
    {
        "fuck", "shit", "bitch", "cunt", "asshole", "bastard", "slut", "whore",
        "nigger", "faggot", "retard",
    }
)
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z']+")


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


def _fails_basic_moderation(text: str) -> bool:
    """Basic denylist + URL guard on AI-generated text -- see `_MODERATION_DENYLIST`'s own
    comment for why this exists instead of a real moderation capability call. Any hit is
    treated as a hard failure of the AI text (never partially redacted)."""
    lowered = text.lower()
    if _URL_RE.search(lowered):
        return True
    tokens = _WORD_RE.findall(lowered)
    return any(token in _MODERATION_DENYLIST for token in tokens)


async def _ai_eligible() -> bool:
    """`!aiso` gate: WaddleAI Enterprise tier AND the `waddles.shoutout-ai` PostHog flag.

    Both default-closed; either missing means "not eligible" -- `transform()` then
    silently downgrades the command to the plain `!so` path, never an error reply. A
    `flags` host error degrades to "not eligible" too, same fail-safe posture as every
    other gate in this module.
    """
    try:
        current_tier = await flags.tier()
    except Exception:  # noqa: BLE001 -- flags outage must never crash, only deny
        return False
    if current_tier != _AI_REQUIRED_TIER:
        return False
    try:
        return await feature_enabled(_AI_FEATURE_FLAG, default=False)
    except Exception:  # noqa: BLE001 -- flags outage must never crash, only deny
        return False


def _aiso_channel_cooldown_key(channel_id: str) -> str:
    """Build the per-channel `!aiso` cooldown key."""
    return f"{_AISO_COOLDOWN_KEY_PREFIX}:chan:{channel_id}"


def _aiso_target_cooldown_key(community: str | None, target: str) -> str:
    """Build the per-(community, target) `!aiso` cooldown key."""
    return f"{_AISO_COOLDOWN_KEY_PREFIX}:target:{community or '-'}:{target}"


async def _is_aiso_on_cooldown(channel_id: str | None, community: str | None, target: str) -> bool:
    """Per-channel AND per-target `!aiso` cooldown gate -- FAILS CLOSED.

    Unlike `_is_on_cooldown()`'s fail-OPEN posture (a free `relay` push, safe to allow
    through a `kv` outage), an unmetered AI call is a real cost/abuse surface -- any
    `kv` error or denial here is treated as "on cooldown", so `transform()` downgrades
    to the plain `!so` template rather than ever calling WaddleAI unmetered.
    """
    try:
        if channel_id and await kv.get(_aiso_channel_cooldown_key(channel_id)) is not None:
            return True
        return await kv.get(_aiso_target_cooldown_key(community, target)) is not None
    except Exception:  # noqa: BLE001 -- fail CLOSED: a kv outage denies the AI path
        return True


async def _set_aiso_cooldown(channel_id: str | None, community: str | None, target: str) -> None:
    """Best-effort cooldown set for both `!aiso` buckets.

    Called from `dispatch()` right before the WaddleAI call itself (not after a
    successful reply, contrast `_set_cooldown()`) so a slow or failing generation still
    counts against the window -- prevents a retry-storm from bypassing the cooldown.
    """
    try:
        if channel_id:
            await kv.set(
                _aiso_channel_cooldown_key(channel_id), b"1", _AISO_CHANNEL_COOLDOWN_SECONDS
            )
        await kv.set(
            _aiso_target_cooldown_key(community, target), b"1", _AISO_TARGET_COOLDOWN_SECONDS
        )
    except Exception:  # noqa: BLE001 -- best-effort; never fail the command over a kv write
        return


def _elapsed_ms(start_ns: int) -> float:
    """Milliseconds elapsed since `start_ns` (a `waddle_sdk.clock.monotonic_nanos()` reading)."""
    return (clock.monotonic_nanos() - start_ns) / 1_000_000


def _metric_ai_latency(duration_ms: float, *, outcome: str) -> None:
    """Structured log line standing in for an AI-latency HISTOGRAM metric.

    No `metrics` WIT host capability exists yet (only `log`, see module docstring gap
    5) -- `metric`/`duration_ms` are the field names a log-to-metric pipeline
    aggregates on, pending a native histogram import.
    """
    log.info(
        "shoutout_ai_latency",
        metric="shoutout_ai_latency_ms",
        duration_ms=round(duration_ms, 2),
        outcome=outcome,
    )


def _metric_ai_fallback(reason: str) -> None:
    """Structured log line standing in for a fallback COUNTER metric -- same interim
    shape/gap as :func:`_metric_ai_latency`."""
    log.warn("shoutout_ai_fallback", metric="shoutout_ai_fallback_total", reason=reason)


class AiGenerator(Protocol):
    """Interface a future native `ai.generate` WIT host capability would satisfy
    directly -- see module docstring gap 5. `_HttpAiGenerator` is the interim
    implementation over the existing `http` import; swapping in a real capability
    later is a one-line change at `_try_ai_shoutout`'s single call site.
    """

    async def generate(self, prompt: str, *, max_tokens: int) -> str:
        """Return raw WaddleAI-generated text for `prompt`, or raise on any failure."""
        ...


class _AiGenerationError(Exception):
    """Any WaddleAI call failure -- always caught by `_try_ai_shoutout`, never surfaces."""


class _HttpAiGenerator:
    """`AiGenerator` over the WIT `http` import against hub-api's internal AI-completions
    proxy -- see module docstring gap 5 for the auth/DNS caveats this still carries.
    """

    __slots__ = ("_base_url", "_community_id", "_http_client")

    def __init__(self, http_client: Any, community_id: int, base_url: str) -> None:
        """Bind the http transport, target community, and hub-api base URL for one call."""
        self._http_client = http_client
        self._community_id = community_id
        self._base_url = base_url.rstrip("/")

    async def generate(self, prompt: str, *, max_tokens: int) -> str:
        """POST `prompt` to hub-api's `/ai/completions` proxy; raise `_AiGenerationError`
        on ANY failure (no client, transport error, non-200, malformed/empty body)."""
        if self._http_client is None:
            raise _AiGenerationError("no http client available")
        url = f"{self._base_url}/api/v1/community/{self._community_id}/ai/completions"
        body = json.dumps(
            {
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "invocation": "interactive",
            }
        ).encode()
        try:
            response = await self._http_client.post(
                url,
                headers={"Content-Type": "application/json"},
                body=body,
                secret_refs={"Authorization": resolve_secret("WADDLEAI_SERVICE_TOKEN")},
            )
        except Exception as exc:
            raise _AiGenerationError(f"waddleai http error: {exc}") from exc
        if response.get("status") != 200:
            raise _AiGenerationError(f"waddleai non-200 status: {response.get('status')}")
        try:
            data = json.loads(response["body"])
        except (ValueError, KeyError) as exc:
            raise _AiGenerationError("waddleai malformed response body") from exc
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _AiGenerationError("waddleai response missing 'text'")
        return text


async def _fetch_twitch_stream_info(http_client: Any, login: str) -> dict[str, Any] | None:
    """Best-effort public stream metadata (category/title) via Twitch Helix `/streams`.

    Used ONLY to enrich the AI prompt with non-identifying public context -- the login
    itself is never sent to WaddleAI (see `_build_ai_prompt()`). `None` on ANY failure,
    same degrade contract as `_fetch_twitch_user()`.
    """
    if http_client is None:
        return None
    try:
        response = await http_client.get(
            f"https://api.twitch.tv/helix/streams?user_login={login}",
            secret_refs={
                "Client-Id": resolve_secret("TWITCH_HELIX_CLIENT_ID"),
                "Authorization": resolve_secret("TWITCH_HELIX_BEARER"),
            },
        )
    except Exception:  # noqa: BLE001 -- must never block a shoutout, only degrade
        return None

    if response.get("status") != 200:
        return None

    try:
        body = json.loads(response["body"])
    except (ValueError, KeyError):
        return None

    streams = body.get("data") or []
    return streams[0] if streams else None


def _build_ai_prompt(stream_info: dict[str, Any] | None) -> str:
    """Build a PII-free WaddleAI prompt: a fixed system instruction plus, at most,
    public stream metadata (category/title, each capped to 80 chars).

    NEVER includes the target's login/display name or any other per-user identifier
    (hard PII boundary, module docstring gap 5) -- the model is instructed to write
    around the literal `{user}` placeholder, substituted back in locally after
    generation and moderation (`_try_ai_shoutout()`).
    """
    context_bits: list[str] = []
    if stream_info:
        game_name = stream_info.get("game_name")
        title = stream_info.get("title")
        if isinstance(game_name, str) and game_name.strip():
            context_bits.append(f"category: {game_name.strip()[:80]}")
        if isinstance(title, str) and title.strip():
            context_bits.append(f"stream title: {title.strip()[:80]}")
    context_text = f" Public context -- {'; '.join(context_bits)}." if context_bits else ""
    return f"{_AI_SYSTEM_INSTRUCTION}{context_text}"[:_AI_PROMPT_MAX_CHARS]


async def _try_ai_shoutout(
    *,
    http_client: Any,
    community_id: int | None,
    base_url: str,
    target: str,
    twitch_user: dict[str, Any] | None,
) -> str | None:
    """Attempt the WaddleAI-authored shoutout; `None` on ANY failure -- `dispatch()`
    then falls back to `_render_shoutout_message()`, exactly like every other degrade
    point in this module.

    PII boundary (hard rule): the prompt built here and the text WaddleAI returns never
    contain `target`'s login/display name -- only the literal `{user}` placeholder plus
    public stream metadata. The real name is substituted in locally, AFTER the response
    is placeholder-checked and moderated -- WaddleAI itself never sees it.
    """
    start_ns = clock.monotonic_nanos()
    if community_id is None:
        _metric_ai_fallback("no_community_id")
        return None

    stream_info = await _fetch_twitch_stream_info(http_client, target)
    prompt = _build_ai_prompt(stream_info)
    generator: AiGenerator = _HttpAiGenerator(http_client, community_id, base_url)

    try:
        raw_text = await generator.generate(prompt, max_tokens=_AI_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001 -- any generation failure degrades, never raises
        _metric_ai_latency(_elapsed_ms(start_ns), outcome="error")
        _metric_ai_fallback(f"generation_error:{type(exc).__name__}")
        return None

    text = raw_text.strip()
    if _AI_PLACEHOLDER not in text:
        _metric_ai_latency(_elapsed_ms(start_ns), outcome="invalid")
        _metric_ai_fallback("missing_placeholder")
        return None
    if _fails_basic_moderation(text):
        _metric_ai_latency(_elapsed_ms(start_ns), outcome="moderated")
        _metric_ai_fallback("moderation")
        return None

    display_name = (twitch_user or {}).get("display_name", target)
    message = text.replace(_AI_PLACEHOLDER, str(display_name))[:_AI_OUTPUT_MAX_CHARS]
    _metric_ai_latency(_elapsed_ms(start_ns), outcome="success")
    return message


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: parse `!so`/`!shoutout`/`!aiso`, gate, pass through.

    Order: prefix match (`!aiso` or `!so`/`!shoutout`) -> base feature flag (off -> `None`, no
    DB/kv touched) -> missing target -> invalid login format -> self-shoutout -> permission
    (config read + role lookup, shared by both commands) -> command-specific cooldown gate ->
    forward to `dispatch()`. `!aiso` additionally checks `_ai_eligible()` (tier + its own
    PostHog flag) and its own fail-closed per-channel/per-target cooldown
    (`_is_aiso_on_cooldown()`); failing either silently downgrades the command to plain `!so`
    (same cooldown/permission decisions already made are reused, not re-evaluated) rather than
    denying the shoutout outright -- see module docstring gap 5. Only a fully successful parse
    is forwarded to this bundle's own `dispatch()`; every other reply (usage hint, invalid
    login, self-shoutout, permission denied, on cooldown) is a plain chat reply.

    Raises:
        ValueError: The event payload is missing a string `text` field.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        raise ValueError("event payload missing required 'text' string field")

    text = text.strip()
    if _AISO_PREFIX_RE.match(text):
        command = "aiso"
    elif _SO_PREFIX_RE.match(text):
        command = "so"
    else:
        return None  # not a shoutout command, skip

    enabled = await feature_enabled(_FEATURE_FLAG, default=False)
    if not enabled:
        return None  # feature disabled -- behaves like an unrecognized command

    parts = text.split(maxsplit=1)
    raw_target = parts[1].strip() if len(parts) > 1 else ""
    if not raw_target:
        return _text_reply(event, _AISO_USAGE if command == "aiso" else _SO_USAGE)

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

    raw_channel_id = event.payload.get("channel_id")
    channel_id = raw_channel_id if isinstance(raw_channel_id, str) else None

    if command == "aiso":
        if not await _ai_eligible():
            command = "so"  # tier/flag missing -- degrade silently, never an error reply
        elif await _is_aiso_on_cooldown(channel_id, ctx.community, target):
            command = "so"  # AI path on cooldown (fail closed) -- degrade, don't deny

    if command == "so" and await _is_on_cooldown(ctx.community, target):
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
            "command": command,
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

    `config` carries only `waddleai_base_url` (optional, see module docstring gap 5) --
    otherwise accepted but unused (no `required_config`, see `bundle.yaml`). Always relays to
    the event's own origin platform (`envelope.event.platform`), never a hardcoded provider --
    mirrors `bundles/python/pyping`'s own regression-tested behavior for this exact convention.

    For `command == "aiso"` (set only when `transform()` already confirmed tier/flag
    eligibility and the AI cooldown gate), attempts a WaddleAI-authored message via
    `_try_ai_shoutout()`, setting `!aiso`'s own cooldown immediately beforehand (fail-closed
    metering, see `_set_aiso_cooldown()`); ANY failure there (generation error, missing
    placeholder, moderation hit) falls back to the exact same generic template `!so` uses --
    never an error, never a bare command failure.

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

    command = payload.get("command", "so")
    twitch_user = await _fetch_twitch_user(http_client, target)

    ai_message: str | None = None
    if command == "aiso":
        await _set_aiso_cooldown(channel_id, envelope.community, target)
        base_url = str(config.get("waddleai_base_url") or _DEFAULT_WADDLEAI_BASE_URL)
        ai_message = await _try_ai_shoutout(
            http_client=http_client,
            community_id=_community_id(envelope.community),
            base_url=base_url,
            target=target,
            twitch_user=twitch_user,
        )

    message = ai_message if ai_message is not None else _render_shoutout_message(target, twitch_user)

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": message})

    cooldown_minutes = payload.get("cooldown_minutes", _DEFAULT_COOLDOWN_MINUTES)
    ttl_seconds = int(cooldown_minutes) * 60
    await _set_cooldown(envelope.community, target, ttl_seconds)

    return DispatchResult(
        transport=provider,
        detail="relayed_ai" if ai_message is not None else "relayed",
        http_status=200,
    )
