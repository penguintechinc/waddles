"""`!vso {user:<uuid>}` -- VIDEO shoutout process/action-stage logic (this task's own scope).

Wired into `app.py`'s `transform()`/`dispatch()` (this bundle's single WIT `process-stage`/
`action-stage` export pair -- one bundle, one `!so` text command and one `!vso` video command,
sharing a permission/cooldown vocabulary via `_shared.py`). Picks a RANDOM clip/short of **30
seconds or less** from the target's Twitch/Kick/YouTube channel and pushes it to the
community's stream overlay (`core/svc_presentation`), alongside a short chat message.

**PII tokenization (hard rule, security.md/critical-rules.md PII Tokenization).** The `<user>`
argument is NEVER a raw platform username/handle here -- it is a `{user:<uuid>}` mention token,
already tokenized by the platform/ingest layer before this bundle ever sees the chat text (the
same boundary `!so`'s own raw-login handling predates and is out of scope to retrofit in this
PR). This module never parses, logs, or stores a raw username for the video-shoutout path; every
chat reply it builds re-embeds the same `{user:<uuid>}` token, left for the platform-adapter
layer (svc-ingest, symmetric with how it tokenized the mention on the way in) to render back to
a real handle at send time.

KNOWN GAPS (documented per this task's own instruction -- implement what works, flag what
doesn't; each is isolated behind its own function so any ONE gap closing (a resolver ships, a
credential broker lands, `render_media` grows a `<video>` element) requires no change to the
functions around it):

1. **No host-side UUID -> platform-channel resolver exists yet.** Investigated: hub-api's
   `community_connections` (`hub_api/services/community_connections.py`, gh-320) is a
   per-COMMUNITY OAuth registry (`integration_type='community_oauth'`, `user_id` always NULL)
   -- it does not map an individual chat user's UUID to their own external channels. There is
   no other such table in this repo today. Per this task's explicit instruction, the clip
   lookups below take the UUID and expect the HOST to resolve platform broadcaster/channel
   refs server-side -- `TargetChannelResolver` is that interface (a plain async callable,
   mocked directly in tests, never the whole `db` shape). `default_target_channel_resolver`
   is this module's own best-effort implementation of it: a `db` query against a
   `user_platform_identities` table this bundle's manifest declares
   (`data.tables: [user_platform_identities]`, see `bundle.yaml`) but which does not exist in
   any migration yet -- every call degrades to `None` (SQL error: relation does not exist)
   until that table and a real population path ship. `dispatch_vso()` treats `None` as "can't
   find channels for that user yet", a plain chat reply, never a hard failure.
2. **No Twitch/Kick OAuth credential broker yet** -- same constraint `app.py`'s own module
   docstring already documents for `!so`'s Twitch enrichment call, applying identically to both
   `_fetch_twitch_clips` (assumes `TWITCH_HELIX_BEARER` alongside `TWITCH_HELIX_CLIENT_ID`) and
   `_fetch_kick_clips` (assumes `KICK_API_BEARER` alongside `KICK_API_CLIENT_ID` -- Kick's
   public API, like Twitch's, is an OAuth2 client-credentials app access token, not a static
   key). Both degrade to an empty clip list on any failure, never blocking `!vso`.
3. **No overlay video-playback surface exists yet.** `core/svc_presentation`'s existing
   `POST /overlay/<community>/<surface>/push` IS a real, live mechanism (`blueprints/
   overlay.py`) this module calls directly -- but the `media` surface's renderer
   (`services/render.py::render_media`) only understands `title`/`body`/`image_url` (a still
   image), no `<video>` element. This module still builds and sends the richer payload
   (`_build_overlay_payload`, `type: "video_clip"`, `video_url`, `duration_seconds`) so
   `render_media` (or a new `video` surface) can start consuming it the moment it's extended --
   sending it today is inert (unknown fields ignored by the current renderer), never harmful.
   Also undeclared: no `overlay.media` permission exists yet in the capability-gate design
   (`docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`, branch
   `docs/bundle-permissions-capability-gate` -- covers storage/`net.http`/`chat.send`/
   `reputation.*` only). `bundle.yaml` forward-declares `permissions: ["overlay.media"]`
   (informational only today, exactly like this bundle's own `feature_flag:` field).
4. **The overlay push host is not a fixed, known DNS name in this manifest scheme.** Helm
   templates this DNS name from the release name (`{{ include "waddlebot.fullname" . }}-svc-
   presentation`, `k8s/helm/waddlebot/templates/svc-presentation.yaml`) -- not a static string
   this manifest's `egress` allowlist can hardcode correctly for every deployment. `config`'s
   `overlay_push_host` key (3-tier resolved, see `bundle.yaml`'s `config_schema`) carries it
   instead; unset (the shipped default) degrades to "skip the overlay push, chat message only"
   -- a real, working, documented partial degrade, not a crash.
5. **Self-shoutout can only be checked when the host populates `actor_user_uuid`.** Neither
   `PlatformEvent.actor` nor `payload["author_id"]` is a tokenized UUID today (pre-existing,
   platform-scoped identifiers `!so`'s own self-check already relies on) -- this module checks
   `payload["actor_user_uuid"]` when present (the same mention-tokenization work item 1's
   `{user:<uuid>}` target token presumably comes from) and otherwise skips the check rather than
   guess, logging at DEBUG that it was skipped.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.bundle_runtime import get_bundle_context, get_bundle_dal
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.http import resolve_secret

from _shared import (
    DispatchResult,
    caller_role,
    community_id,
    permission_satisfied,
    shoutout_permission_and_cooldown,
)

#: PostHog flag key gating `!vso` specifically -- separate from `!so`'s `waddles.shoutout-bundle`
#: (a materially larger, newer capability; default OFF like every other bundle flag here).
VIDEO_FEATURE_FLAG = "waddles.shoutout-video-bundle"

#: Matches `!vso`, video-shoutout only.
VSO_PREFIX_RE = re.compile(r"^!vso\b", re.IGNORECASE)

#: A tokenized mention -- `{user:<uuid>}`, case-insensitive UUID body (see module docstring's
#: PII tokenization section). Never a raw login/handle.
_USER_TOKEN_RE = re.compile(
    r"^\{user:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\}$", re.IGNORECASE
)

VSO_USAGE = "usage: !vso {user:<uuid>}"
INVALID_MENTION_REPLY = "that doesn't look like a valid user mention"
SELF_VSO_REPLY = "you can't shout yourself out"
PERMISSION_DENIED_REPLY = "you don't have permission to shout out"
CHANNEL_ON_COOLDOWN_REPLY = "the overlay is busy right now, try the video shoutout again in a bit"

#: Marks a `transform()`-produced envelope payload as the video path for `dispatch()` to branch
#: on (see `app.py::dispatch`).
KIND_VIDEO_SHOUTOUT = "video_shoutout"

#: Legacy-service parity constant (`VideoShoutoutService`'s own 30s clip-length cap).
MAX_CLIP_SECONDS = 30

#: Longer than `!so`'s 60-minute default (migration 046) -- a video shoutout is a heavier,
#: more attention-grabbing action than a chat line, so it gets its own, longer default.
VSO_TARGET_COOLDOWN_MINUTES = 30
#: Rate-limits the overlay ITSELF (any target), independent of the per-target cooldown above --
#: this task's own "per channel" cooldown requirement, so no single community can flood its own
#: overlay with back-to-back `!vso`s from different targets.
VSO_CHANNEL_COOLDOWN_SECONDS = 60
#: "Last N clips per target" anti-repeat window (this task's own requirement).
_RECENT_CLIP_HISTORY_SIZE = 5
#: How long a "recently played" clip id is remembered -- generous (a week) since the point is
#: avoiding an immediate repeat, not permanent de-duplication.
_RECENT_CLIP_HISTORY_TTL_SECONDS = 7 * 24 * 60 * 60

_TARGET_COOLDOWN_KEY_PREFIX = "shoutout:vcd"
_CHANNEL_COOLDOWN_SENTINEL = "_channel_"
_RECENT_CLIPS_KEY_PREFIX = "shoutout:vso:recent"

#: The three clip-capable platforms this command supports, and this bundle's shipped default
#: per-community source order -- Twitch first (task's own stated preference).
CLIP_PLATFORMS: tuple[str, ...] = ("twitch", "kick", "youtube")
_DEFAULT_SOURCE_ORDER: tuple[str, ...] = ("twitch", "kick", "youtube")

#: These are `penguin-sal`/`resolve_secret()` REFERENCE NAMES (env-var keys the host resolves),
#: never secret values -- same false-positive shape as `app.py`'s inline
#: `resolve_secret("TWITCH_HELIX_CLIENT_ID")` literals, just hoisted to module constants here.
_TWITCH_CLIENT_ID_SECRET = "TWITCH_HELIX_CLIENT_ID"  # noqa: S105
_TWITCH_BEARER_SECRET = "TWITCH_HELIX_BEARER"  # noqa: S105
_KICK_CLIENT_ID_SECRET = "KICK_API_CLIENT_ID"  # noqa: S105
_KICK_BEARER_SECRET = "KICK_API_BEARER"  # noqa: S105
_YOUTUBE_API_KEY_SECRET = "YOUTUBE_DATA_API_KEY"  # noqa: S105
_YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
#: See module docstring gap 4 -- overlay auth secret, injected as a header, never a literal.
_OVERLAY_PUSH_TOKEN_SECRET = "PRESENTATION_PUSH_TOKEN"  # noqa: S105
_OVERLAY_SURFACE = "media"


def resolve_source_order(
    origin_platform: str, configured_order: Sequence[str] | None = None
) -> tuple[str, ...]:
    """Pure function: the clip-source lookup order for one `!vso` invocation.

    Invariant (never violated, regardless of `configured_order`): the platform the command
    ORIGINATED from is tried first when it is clip-capable; YouTube is always LAST unless it is
    itself the originating platform. `configured_order` (a community's per-tenant override, see
    `bundle.yaml`'s `config_schema.clip_source_order`) may only reorder Twitch and Kick relative
    to each other -- it can never move YouTube ahead of an originating Twitch/Kick, and can
    never demote the originating platform. An invalid `configured_order` (not a permutation of
    `CLIP_PLATFORMS`) is ignored, falling back to the shipped default order.

    Table (this task's own explicit spec, `origin_platform` -> order, default config):
        twitch  -> (twitch, kick, youtube)
        kick    -> (kick, twitch, youtube)
        youtube -> (youtube, twitch, kick)
        other (discord, etc., no native clips) -> (twitch, kick, youtube)  # configured default
    """
    configured = tuple(configured_order) if configured_order else _DEFAULT_SOURCE_ORDER
    if sorted(configured) != sorted(_DEFAULT_SOURCE_ORDER):
        configured = _DEFAULT_SOURCE_ORDER  # not a permutation of the 3 platforms -- ignore

    non_youtube = [
        p for p in configured if p != "youtube"
    ]  # preserves configured twitch/kick order

    if origin_platform == "youtube":
        return ("youtube", *non_youtube)
    if origin_platform in non_youtube:
        rest = [p for p in non_youtube if p != origin_platform]
        return (origin_platform, *rest, "youtube")
    return (*non_youtube, "youtube")  # origin has no native clips -- pure configured default


@dataclass(slots=True, frozen=True)
class Clip:
    """One eligible (<=30s) clip/short, already normalized across the three source platforms."""

    source: str
    clip_id: str
    title: str
    video_url: str
    duration_seconds: int


@dataclass(slots=True, frozen=True)
class TargetChannelRefs:
    """Per-platform channel/broadcaster refs for a tokenized target UUID.

    See module docstring gap 1. Any field may be `None` (that platform not linked/known for
    this target).
    """

    twitch_broadcaster_id: str | None
    kick_channel_slug: str | None
    youtube_channel_id: str | None

    def for_platform(self, platform: str) -> str | None:
        """The channel ref for `platform`, or `None` if unresolved/unsupported."""
        return {
            "twitch": self.twitch_broadcaster_id,
            "kick": self.kick_channel_slug,
            "youtube": self.youtube_channel_id,
        }.get(platform)


class TargetChannelResolver(Protocol):
    """The host-side dependency this task's own instruction calls for.

    Given a tokenized target UUID, resolve that user's per-platform broadcaster/channel refs.
    A plain async callable (not a class hierarchy) so tests can mock it directly with a bare
    `async def`/lambda, never the full `db` fake shape `default_target_channel_resolver`
    happens to use.
    """

    async def __call__(self, target_uuid: str) -> TargetChannelRefs | None:
        """Resolve `target_uuid` to its per-platform channel/broadcaster refs, or `None`."""
        ...


_TARGET_CHANNELS_SQL = (
    "SELECT platform, platform_channel_ref FROM user_platform_identities WHERE user_uuid = $1"
)


async def default_target_channel_resolver(target_uuid: str) -> TargetChannelRefs | None:
    """Best-effort `db`-backed `TargetChannelResolver` -- see module docstring gap 1.

    Degrades to `None` on ANY error (most likely: the `user_platform_identities` table does not
    exist yet) or an empty result -- never raises, `dispatch_vso()` treats `None` as "can't find
    channels for this user yet", a plain chat reply, not a failure.
    """
    dal = get_bundle_dal()
    try:
        rows = await dal.execute(_TARGET_CHANNELS_SQL, [target_uuid])
    except Exception:  # noqa: BLE001 -- see docstring: the dependency table may not exist yet
        log.warn("vso_target_resolution_failed", target_uuid=target_uuid)
        return None
    if not rows:
        return None
    refs: dict[str, str] = {}
    for row in rows:
        platform = row.get("platform")
        ref = row.get("platform_channel_ref")
        if isinstance(platform, str) and isinstance(ref, str):
            refs[platform] = ref
    if not refs:
        return None
    return TargetChannelRefs(
        twitch_broadcaster_id=refs.get("twitch"),
        kick_channel_slug=refs.get("kick"),
        youtube_channel_id=refs.get("youtube"),
    )


def _parse_iso8601_duration_seconds(duration: str) -> int | None:
    """Parse the `PT#H#M#S` subset of ISO-8601 durations YouTube's `contentDetails` returns."""
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


async def _fetch_twitch_clips(http_client: Any, broadcaster_id: str) -> list[Clip]:
    """Twitch Helix `GET /clips?broadcaster_id=...`, filtered to `duration <= 30`s.

    Degrades to `[]` on ANY failure (no client, missing/unresolvable secret, transport error,
    non-2xx, malformed body) -- see module docstring gap 2. Never raises.
    """
    if http_client is None or not broadcaster_id:
        return []
    try:
        response = await http_client.get(
            f"https://api.twitch.tv/helix/clips?broadcaster_id={broadcaster_id}&first=20",
            secret_refs={
                "Client-Id": resolve_secret(_TWITCH_CLIENT_ID_SECRET),
                "Authorization": resolve_secret(_TWITCH_BEARER_SECRET),
            },
        )
    except Exception:  # noqa: BLE001 -- see module docstring gap 2, must never block `!vso`
        log.warn("vso_twitch_clip_fetch_failed", broadcaster_id=broadcaster_id)
        return []
    if response.get("status") != 200:
        return []
    try:
        body = json.loads(response["body"])
    except (ValueError, KeyError, TypeError):
        return []

    clips: list[Clip] = []
    for item in body.get("data") or []:
        if not isinstance(item, dict):
            continue
        duration = item.get("duration")
        clip_id = item.get("id")
        url = item.get("url") or item.get("embed_url")
        if not isinstance(duration, int | float) or duration > MAX_CLIP_SECONDS:
            continue
        if not isinstance(clip_id, str) or not isinstance(url, str):
            continue
        clips.append(
            Clip(
                source="twitch",
                clip_id=clip_id,
                title=str(item.get("title") or "clip"),
                video_url=url,
                duration_seconds=int(duration),
            )
        )
    return clips


async def _fetch_kick_clips(http_client: Any, channel_slug: str) -> list[Clip]:
    """Kick public API `GET /public/v1/channels/{slug}/clips`, filtered to `duration <= 30`s.

    Degrades to `[]` on ANY failure -- see module docstring gap 2 (Kick OAuth credential broker
    does not exist yet, same constraint as Twitch). Never raises.
    """
    if http_client is None or not channel_slug:
        return []
    try:
        response = await http_client.get(
            f"https://api.kick.com/public/v1/channels/{channel_slug}/clips?limit=20",
            secret_refs={
                "Authorization": resolve_secret(_KICK_BEARER_SECRET),
                "Client-Id": resolve_secret(_KICK_CLIENT_ID_SECRET),
            },
        )
    except Exception:  # noqa: BLE001 -- see module docstring gap 2, must never block `!vso`
        log.warn("vso_kick_clip_fetch_failed", channel_slug=channel_slug)
        return []
    if response.get("status") != 200:
        return []
    try:
        body = json.loads(response["body"])
    except (ValueError, KeyError, TypeError):
        return []

    clips: list[Clip] = []
    for item in body.get("data") or body.get("clips") or []:
        if not isinstance(item, dict):
            continue
        duration = item.get("duration") or item.get("duration_seconds")
        clip_id = item.get("id") or item.get("clip_id")
        url = item.get("clip_url") or item.get("video_url") or item.get("url")
        if not isinstance(duration, int | float) or duration > MAX_CLIP_SECONDS:
            continue
        if not isinstance(clip_id, str) or not isinstance(url, str):
            continue
        clips.append(
            Clip(
                source="kick",
                clip_id=clip_id,
                title=str(item.get("title") or "clip"),
                video_url=url,
                duration_seconds=int(duration),
            )
        )
    return clips


async def _fetch_youtube_clips(http_client: Any, channel_id: str) -> list[Clip]:
    """YouTube Data API two-call sequence.

    `search.list` (short-duration videos on the channel) then `videos.list`
    (`contentDetails.duration`, authoritative filter to `<= 30`s).

    Degrades to `[]` on ANY failure (no client, missing/unresolvable API key, transport error,
    non-2xx, malformed body, no results). Never raises.
    """
    if http_client is None or not channel_id:
        return []
    try:
        search_response = await http_client.get(
            f"{_YOUTUBE_SEARCH_URL}?part=id&channelId={channel_id}&type=video"
            "&videoDuration=short&order=date&maxResults=20",
            secret_refs={"X-Goog-Api-Key": resolve_secret(_YOUTUBE_API_KEY_SECRET)},
        )
    except Exception:  # noqa: BLE001 -- must never block `!vso`
        log.warn("vso_youtube_search_failed", channel_id=channel_id)
        return []
    if search_response.get("status") != 200:
        return []
    try:
        search_body = json.loads(search_response["body"])
    except (ValueError, KeyError, TypeError):
        return []

    video_ids = [
        item["id"]["videoId"]
        for item in search_body.get("items") or []
        if isinstance(item, dict)
        and isinstance(item.get("id"), dict)
        and isinstance(item["id"].get("videoId"), str)
    ]
    if not video_ids:
        return []

    try:
        videos_response = await http_client.get(
            f"{_YOUTUBE_VIDEOS_URL}?part=contentDetails,snippet&id={','.join(video_ids)}",
            secret_refs={"X-Goog-Api-Key": resolve_secret(_YOUTUBE_API_KEY_SECRET)},
        )
    except Exception:  # noqa: BLE001 -- must never block `!vso`
        log.warn("vso_youtube_videos_failed", channel_id=channel_id)
        return []
    if videos_response.get("status") != 200:
        return []
    try:
        videos_body = json.loads(videos_response["body"])
    except (ValueError, KeyError, TypeError):
        return []

    clips: list[Clip] = []
    for item in videos_body.get("items") or []:
        if not isinstance(item, dict):
            continue
        content_details = item.get("contentDetails") or {}
        duration_seconds = _parse_iso8601_duration_seconds(content_details.get("duration", ""))
        video_id = item.get("id")
        if duration_seconds is None or duration_seconds > MAX_CLIP_SECONDS:
            continue
        if not isinstance(video_id, str):
            continue
        title = (item.get("snippet") or {}).get("title") or "short"
        clips.append(
            Clip(
                source="youtube",
                clip_id=video_id,
                title=str(title),
                video_url=f"https://www.youtube.com/shorts/{video_id}",
                duration_seconds=duration_seconds,
            )
        )
    return clips


_ClipFetcher = Callable[[Any, str], Awaitable[list[Clip]]]
_CLIP_FETCHERS: dict[str, _ClipFetcher] = {
    "twitch": _fetch_twitch_clips,
    "kick": _fetch_kick_clips,
    "youtube": _fetch_youtube_clips,
}


def _pick_random_clip(clips: list[Clip], exclude_ids: set[str]) -> Clip | None:
    """Uniform random pick over WASI random.

    Uses `import random` (`wasi:random` is allowlisted, see `hub_api/services/
    bundle_component_validator.py`'s own `ALLOWED_WASI_NAMESPACES`), excluding the target's
    last-N-played clip ids. Falls back to the full pool if excluding empties it entirely (a
    target with <= N total clips must still get a shoutout).
    """
    if not clips:
        return None
    eligible = [c for c in clips if c.clip_id not in exclude_ids]
    pool = eligible or clips
    return random.choice(pool)  # noqa: S311 -- non-cryptographic clip pick, not security-sensitive


async def find_random_clip(
    http_client: Any,
    refs: TargetChannelRefs,
    source_order: Sequence[str],
    exclude_ids: set[str],
) -> Clip | None:
    """Try each platform in `source_order`; the first with any eligible clip wins."""
    for platform in source_order:
        channel_ref = refs.for_platform(platform)
        fetcher = _CLIP_FETCHERS.get(platform)
        if not channel_ref or fetcher is None:
            continue
        clips = await fetcher(http_client, channel_ref)
        clip = _pick_random_clip(clips, exclude_ids)
        if clip is not None:
            return clip
    return None


async def _recent_clip_ids(target_uuid: str) -> list[str]:
    """The target's last-N-played clip ids.

    Degrades to `[]` (no exclusions, never blocks the fetch) on ANY `kv` error, matching every
    other `kv` call in this bundle.
    """
    try:
        raw = await kv.get(f"{_RECENT_CLIPS_KEY_PREFIX}:{target_uuid}")
    except Exception:  # noqa: BLE001 -- kv denial/outage must never block a video shoutout
        return []
    if raw is None:
        return []
    try:
        ids = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


async def _remember_clip_id(target_uuid: str, clip_id: str) -> None:
    """Best-effort: push `clip_id` onto the target's recent-clips ring buffer."""
    try:
        recent = await _recent_clip_ids(target_uuid)
        recent = ([clip_id] + [c for c in recent if c != clip_id])[:_RECENT_CLIP_HISTORY_SIZE]
        await kv.set(
            f"{_RECENT_CLIPS_KEY_PREFIX}:{target_uuid}",
            json.dumps(recent).encode("utf-8"),
            _RECENT_CLIP_HISTORY_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001 -- kv denial/outage must never fail a successful shoutout
        return


def _target_cooldown_key(community: str | None, target_uuid: str) -> str:
    return f"{_TARGET_COOLDOWN_KEY_PREFIX}:{community or '-'}:{target_uuid}"


def _channel_cooldown_key(community: str | None) -> str:
    return f"{_TARGET_COOLDOWN_KEY_PREFIX}:{community or '-'}:{_CHANNEL_COOLDOWN_SENTINEL}"


async def _is_on_cooldown(key: str) -> bool:
    """Degrades to "not on cooldown" on ANY `kv` error.

    See `app.py`'s own `_is_on_cooldown` docstring for the same, already-documented
    `kv`-capability gap this bundle inherits.
    """
    try:
        return await kv.get(key) is not None
    except Exception:  # noqa: BLE001 -- kv denial/outage must never block a video shoutout
        return False


async def _set_cooldown(key: str, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    try:
        await kv.set(key, b"1", ttl_seconds)
    except Exception:  # noqa: BLE001 -- kv denial/outage must never fail a successful shoutout
        return


def _text_reply(event: PlatformEvent, text: str) -> PlatformEvent:
    """Build a direct chat reply, preserving every other payload field (mirrors `app._text_reply`)."""  # noqa: E501
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={**event.payload, "text": text},
        occurred_at=event.occurred_at,
    )


def _mention(target_uuid: str) -> str:
    """Re-embed the tokenized mention -- never a raw handle, see module docstring."""
    return f"{{user:{target_uuid}}}"


async def transform_vso(event: PlatformEvent, text: str) -> PlatformEvent | None:
    """`!vso` gate: feature flag, target parsing, self-check, permission, then cooldown.

    Order: feature flag -> missing/malformed target -> self-shoutout (best-effort) ->
    permission (same `so_permission` tier as `!so`, per this task's own requirement) -> cooldown
    (target + channel, `kv` reads only -- writes happen in `dispatch_vso()` on success, same
    convention as `!so`'s own cooldown split).
    """
    enabled = await feature_enabled(VIDEO_FEATURE_FLAG, default=False)
    if not enabled:
        return None  # feature disabled -- behaves like an unrecognized command

    parts = text.split(maxsplit=1)
    raw_target = parts[1].strip() if len(parts) > 1 else ""
    if not raw_target:
        return _text_reply(event, VSO_USAGE)

    match = _USER_TOKEN_RE.match(raw_target)
    if not match:
        return _text_reply(event, INVALID_MENTION_REPLY)
    target_uuid = match.group(1).lower()

    actor_uuid = event.payload.get("actor_user_uuid")
    if isinstance(actor_uuid, str) and actor_uuid.lower() == target_uuid:
        return _text_reply(event, SELF_VSO_REPLY)
    if not isinstance(actor_uuid, str):
        log.debug("vso_self_check_skipped_no_actor_uuid", target_uuid=target_uuid)

    ctx = get_bundle_context()
    community_id_ = community_id(ctx.community)
    dal = get_bundle_dal()

    permission, _cooldown_minutes = await shoutout_permission_and_cooldown(dal, community_id_)
    role = await caller_role(dal, event, community_id_)
    if not permission_satisfied(permission, role):
        return _text_reply(event, PERMISSION_DENIED_REPLY)

    if await _is_on_cooldown(_target_cooldown_key(ctx.community, target_uuid)):
        return _text_reply(
            event,
            f"{_mention(target_uuid)} was already video-shouted-out recently, try again later",
        )
    if await _is_on_cooldown(_channel_cooldown_key(ctx.community)):
        return _text_reply(event, CHANNEL_ON_COOLDOWN_REPLY)

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={
            "text": "",
            "channel_id": event.payload.get("channel_id"),
            "kind": KIND_VIDEO_SHOUTOUT,
            "vso_target_user": target_uuid,
            "vso_target_cooldown_minutes": VSO_TARGET_COOLDOWN_MINUTES,
            "vso_channel_cooldown_seconds": VSO_CHANNEL_COOLDOWN_SECONDS,
        },
        occurred_at=event.occurred_at,
    )


def _build_overlay_payload(clip: Clip, target_uuid: str) -> dict[str, Any]:
    """The overlay `push` body.

    See module docstring gap 3 for why `video_url` is inert against today's `render_media`,
    and why that's fine.
    """
    return {
        "type": "video_clip",
        "title": clip.title,
        "body": f"clip via {clip.source}",
        "video_url": clip.video_url,
        "duration_seconds": clip.duration_seconds,
        "source": clip.source,
        "target_user": _mention(target_uuid),
    }


async def _push_clip_to_overlay(
    http_client: Any, overlay_host: str | None, community: str | None, clip: Clip, target_uuid: str
) -> bool:
    """`POST https://{overlay_host}/overlay/{community}/media/push`.

    See module docstring gaps 3-4. Degrades to `False` (chat message still goes out) on any
    missing prerequisite or failure; never raises.
    """
    if http_client is None or not overlay_host or not community:
        return False
    url = f"https://{overlay_host}/overlay/{community}/{_OVERLAY_SURFACE}/push"
    body = json.dumps(_build_overlay_payload(clip, target_uuid)).encode("utf-8")
    try:
        response = await http_client.post(
            url,
            headers={"Content-Type": "application/json"},
            body=body,
            secret_refs={"Authorization": resolve_secret(_OVERLAY_PUSH_TOKEN_SECRET)},
        )
    except Exception:  # noqa: BLE001 -- overlay push failure must never fail the chat message
        log.warn("vso_overlay_push_failed", community=community)
        return False
    return bool(response.get("status") == 200)


def _clip_shoutout_message(clip: Clip, target_uuid: str) -> str:
    return f"Check out this clip from {_mention(target_uuid)} (via {clip.source}): {clip.video_url}"


async def dispatch_vso(
    envelope: StageEnvelope,
    config: dict[str, Any],
    *,
    http_client: Any,
    resolver: TargetChannelResolver | None = None,
) -> DispatchResult:
    """Implement the `!vso` half of `action-stage.dispatch`.

    Cooldown re-check, resolve target channels (host-side dependency, gap 1 -- `resolver`
    defaults to `default_target_channel_resolver` but is an explicit parameter precisely so
    tests, and any future real host integration, can substitute their own
    `TargetChannelResolver` without touching this function), pick a random <=30s clip in
    origin-first order (`resolve_source_order`), push it to the overlay (best-effort, gap 3-4),
    relay a chat message, remember the clip (anti-repeat), and set both cooldowns on success.

    Raises:
        ValueError: The envelope's payload has no `channel_id` or `vso_target_user` (a
            malformed hand-off from `transform_vso()` -- should never happen in practice).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    target_uuid = payload.get("vso_target_user")
    if not channel_id:
        raise ValueError(
            "video shoutout dispatch requires a channel_id from the inbound chat.message"
        )
    if not isinstance(target_uuid, str) or not target_uuid:
        raise ValueError(
            "video shoutout dispatch requires a 'vso_target_user' uuid from transform()"
        )

    origin_platform = envelope.event.platform
    target_cooldown_key = _target_cooldown_key(envelope.community, target_uuid)
    channel_cooldown_key = _channel_cooldown_key(envelope.community)

    # Re-checked here (config-driven durations only resolve at dispatch time, see module
    # docstring's own architecture note in `resolve_source_order`'s caller) -- `transform_vso()`
    # already checked once with the default durations; this is the authoritative gate.
    if await _is_on_cooldown(target_cooldown_key):
        await relay.push(
            origin_platform,
            {
                "channel": channel_id,
                "text": (
                    f"{_mention(target_uuid)} was already video-shouted-out recently, "
                    "try again later"
                ),
            },
        )
        return DispatchResult(transport=origin_platform, detail="on_cooldown", http_status=200)
    if await _is_on_cooldown(channel_cooldown_key):
        await relay.push(
            origin_platform, {"channel": channel_id, "text": CHANNEL_ON_COOLDOWN_REPLY}
        )
        return DispatchResult(
            transport=origin_platform, detail="channel_on_cooldown", http_status=200
        )

    resolve = resolver or default_target_channel_resolver
    refs = await resolve(target_uuid)
    if refs is None:
        await relay.push(
            origin_platform,
            {
                "channel": channel_id,
                "text": f"can't find any channels for {_mention(target_uuid)} yet",
            },
        )
        return DispatchResult(
            transport=origin_platform, detail="target_unresolved", http_status=200
        )

    configured_order = config.get("clip_source_order") if isinstance(config, dict) else None
    source_order = resolve_source_order(origin_platform, configured_order)

    exclude_ids = set(await _recent_clip_ids(target_uuid))
    clip = await find_random_clip(http_client, refs, source_order, exclude_ids)
    if clip is None:
        await relay.push(
            origin_platform,
            {
                "channel": channel_id,
                "text": (
                    f"{_mention(target_uuid)} doesn't have any clips under 30s "
                    "available right now"
                ),
            },
        )
        return DispatchResult(transport=origin_platform, detail="no_clip_found", http_status=200)

    overlay_host = config.get("overlay_push_host") if isinstance(config, dict) else None
    pushed = await _push_clip_to_overlay(
        http_client, overlay_host, envelope.community, clip, target_uuid
    )

    await relay.push(
        origin_platform, {"channel": channel_id, "text": _clip_shoutout_message(clip, target_uuid)}
    )
    await _remember_clip_id(target_uuid, clip.clip_id)

    target_cooldown_minutes = payload.get(
        "vso_target_cooldown_minutes", VSO_TARGET_COOLDOWN_MINUTES
    )
    channel_cooldown_seconds = payload.get(
        "vso_channel_cooldown_seconds", VSO_CHANNEL_COOLDOWN_SECONDS
    )
    await _set_cooldown(target_cooldown_key, int(target_cooldown_minutes) * 60)
    await _set_cooldown(channel_cooldown_key, int(channel_cooldown_seconds))

    return DispatchResult(
        transport=origin_platform,
        detail="overlay_pushed" if pushed else "relayed_chat_only",
        http_status=200,
    )
