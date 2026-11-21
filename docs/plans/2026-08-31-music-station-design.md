# Per-Community Music Station — Design Specification

> **Historical document.** Written against the repository layout at the time; some paths named below (shown as plain text, not links) have since moved, been renamed or been removed. Not maintained as current architecture.

Status: **DRAFT — for review, not implementation.** Extends
`docs/plans/2026-08-26-v3-scbm-apps-design.md` (Modules/Features/Apps, container topology)
and `docs/plans/2026-08-31-app-bundle-sdk-design.md` (App Bundle SDK: the
ingest→process→action pipeline, per-community isolation, distribution model) with the
Music Station Feature. Every "today" claim cites the real file/line it is grounded in;
every new field, table, or container is a **proposal**, called out as such. Twelve open
decisions are listed in §11 — none are settled here.

**Load-bearing correction to the brief this spec was commissioned against**: a
SoundCloud provider **already exists** in the codebase
(`core/unified_music_module/providers/soundcloud_provider.py`, 26KB, OAuth2 + streaming
already implemented). SoundCloud is not a from-scratch integration — see §3 and the gap
list (§12) for exactly what *is* new about it.

---

## 1. Overview

**Model** (decided with the user, documented authoritatively here):

1. One persistent queue **per community** that intermingles tracks from Spotify,
   YouTube, and SoundCloud back-to-back in a single playlist.
2. Fed by both viewer song requests (chat command) and imported playlists — both resolve
   into the same normalized `Track` model and land in the same queue.
3. The normalized `Track` (§2) is the load-bearing abstraction that lets three
   heterogeneous sources sit in one queue.
4. Reuses the App Bundle pipeline (§9): the three source integrations are **ingest**
   bundles; a shared **process** component owns the per-community queue; a **presentation
   container** (§8, new 8th container / 4th stage-runner) renders now-playing/up-next/
   request-list and serves the browser-source player. Audio plays **client-side** in the
   streamer's OBS browser source — the server never decodes or proxies audio itself.
5. The presentation container is first-class: it owns the core overlays
   (full_screen/media/crawler) **and** the Music Station **and** any activated bundle's
   own presentation component (§8.4).
6. Per-community **policy**, admin-set, scope `music.policy:admin` (§7).
7. **Moderation** — kick a track or a whole imported playlist, override category
   restriction — scope `music.queue:moderate`, every action audited (§7).
8. Tracks carry `category`; `requests_category_restricted` gates on it (§7.4, source
   open — §11.5).
9. Gated Feature (license tier × PostHog flag); module ownership and tier are open (§11.2,
   §11.3).

### 1.1 What already exists (read this before §2 onward)

Music functionality **predates the v3 App Bundle model** and is scattered across four
places, none of them bundle-shaped yet:

| Location | What it is | Network surface |
|---|---|---|
| action/interactive/spotify_interaction_module | Dedicated per-community Spotify OAuth token lifecycle service (`docs/spotify_interaction_module/OVERVIEW.md:9-17`) | Quart, own REST API |
| action/interactive/youtube_music_interaction_module | Same, for YouTube Music (`docs/youtube_music_interaction_module/OVERVIEW.md`) | Quart, port 8025 |
| core/unified_music_module | Providers (Spotify/YouTube/SoundCloud), `UnifiedQueue`, `ModeController`, `MusicPlayer`, `RadioPlayer` | **none — no `app.py`, library-only** |
| core/browser_source_core_module | Serves overlay HTML incl. `templates/music-player-overlay.html` and `templates/music-overlay.html`; caption WebSocket | Quart + gRPC + WebSocket |

core/unified_music_module has no entrypoint of its own — it is imported as a library and
*pushes* HTTP notifications outward (`mode_controller.py:450`, `music_player.py:497`) to
endpoints (`/api/v1/internal/mode-change`, `/api/v1/internal/now-playing`) that **do not
exist** in `browser_source_core_module/app.py` today (its only `/internal/*` route is
`/internal/captions`, `app.py:62`). The overlay template itself
(`templates/music-player-overlay.html:187`) opens `ws://localhost:8052/ws/music`, which
`app.py` also never implements (`app.py:203` only wires `/ws/captions/<int:community_id>`).
**This wiring was never finished** — treat it as dead scaffolding to replace, not a working
path to preserve.

---

## 2. Normalized Track model

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TrackSource(str, Enum):
    """Which platform a Track was resolved from."""
    SPOTIFY = "spotify"
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"


class AddedVia(str, Enum):
    """How a Track entered a community's queue."""
    REQUEST = "request"
    PLAYLIST = "playlist"


@dataclass(slots=True, frozen=True)
class Track:
    """
    A single playable unit, normalized across Spotify/YouTube/SoundCloud so the
    per-community queue (§4) can play three heterogeneous sources back-to-back
    without provider-specific branching downstream of resolution.
    """

    source: TrackSource
    source_id: str          # provider-native id (Spotify track id, YouTube video id, SC track id)
    title: str
    artist: str
    duration_seconds: int
    artwork_url: Optional[str]
    playback_url: str       # stream URL (SoundCloud) or watch/embed URL (YouTube) or provider URI (Spotify)
    category: Optional[str] # §11.5 — source unresolved
    requested_by: str       # tokenized user UUID, never raw platform username (backend-database.md PII rule)
    added_via: AddedVia
```

**Reconciliation with today's `MusicTrack`** (`core/unified_music_module/providers/base_provider.py:14-37`):

| Today (`MusicTrack`) | Proposed (`Track`) | Note |
|---|---|---|
| not `slots=True` | `slots=True, frozen=True` | plain `@dataclass`, violates `backend-python.md` |
| `track_id` | `source_id` | renamed for clarity against `source` |
| `name` | `title` | — |
| `album`, `album_art_url` | dropped `album`; `album_art_url` → `artwork_url` | album not needed for a back-to-back queue |
| `duration_ms` | `duration_seconds` | unit change — confirm at implementation time |
| `provider: str` (free string) | `source: TrackSource` (enum) | closed set, matches `TrackSource` |
| `uri: str` | `playback_url: str` | see §8.5 — three sources resolve this very differently |
| `metadata: Dict[str, Any]` | dropped | catch-all dict is exactly what the loosely-typed original had; the closed `Track` shape replaces it |
| — | `category`, `requested_by`, `added_via` | **new fields**, don't exist on `MusicTrack` at all |

Today, `requested_by_user_id` and `community_id` live one layer up, on `QueueItem`
(`unified_music_module/services/unified_queue.py:46-67`), not on the track itself. This
spec moves `requested_by`/`added_via` onto `Track` per the model decision in §1 — `Track`
becomes self-describing outside the context of a specific queue entry (e.g. for playlist
import, where N tracks share one `added_via=PLAYLIST` but need individually-batched
identity for moderation, see §11.12).

---

## 3. Sources

| | Spotify | YouTube | SoundCloud |
|---|---|---|---|
| Per-community OAuth today | `spotify_interaction_module` — dedicated service, own token lifecycle (`docs/spotify_interaction_module/OVERVIEW.md:9-17`) | `youtube_music_interaction_module` — same pattern, port 8025 | **none** — `soundcloud_provider.py:69-76` reads `SOUNDCLOUD_CLIENT_ID/SECRET/REDIRECT_URI` from process env, single-instance, not per-community |
| Provider today | `unified_music_module/providers/spotify_provider.py` — **does its own independent OAuth** (`spotify_provider.py:74-90`), never calls `spotify_interaction_module` despite that module's own docs claiming it's the primary caller (`docs/spotify_interaction_module/OVERVIEW.md:17`) — architecture drift, not yet reconciled | `youtube_provider.py` | `soundcloud_provider.py` |
| Playback mechanism today | Spotify Connect device control — `PUT /me/player/play` + `device_id` (`spotify_provider.py:367-386`); audio plays on a **separately registered** Connect device (e.g. streamer's desktop app), never in-browser | "Playback is handled via browser source (iframe)" (`youtube_provider.py:5`) — already client-side | Direct stream URL with `oauth_token` query param (`soundcloud_provider.py:338-354`), genuinely embeddable via an HTML5 `<audio>` element |
| Playlist resolution today | `get_playlists`/`get_playlist_tracks` (`spotify_provider.py:559,574`) | **none found** | `get_user_playlists`/`get_playlist_tracks` (`soundcloud_provider.py:690,720`) |
| What's actually new | Reconcile the two divergent OAuth paths (§11.7 adjacent); an **ingest bundle** wrapping the existing per-community `spotify_interaction_module` token flow, resolving request/playlist input into `Track`s | An ingest bundle wrapping `youtube_provider.py`'s existing search/resolve; playlist import needs new work (no existing method) | A new **per-community** OAuth path (`soundcloud_interaction_module`, mirroring the other two — §11.7) — the *provider* and *streaming* logic already exist and are close to production-ready |

Playback mechanism is the deepest open question, not the OAuth wiring — see §11.8.
Spotify cannot stream raw audio into a browser source at all under its API terms; the
only route to genuinely client-side Spotify audio is the Spotify **Web Playback SDK**
(creates a virtual Connect device inside the overlay page, Premium-required, needs a
user-gesture unlock per browser autoplay policy) — a materially different integration
than what `spotify_provider.py` does today.

---

## 4. Queue model & per-community state/store

Today: `UnifiedQueue` (`unified_music_module/services/unified_queue.py:101`), Redis-backed
with an in-process dict fallback when Redis is unavailable
(`_fallback_queues: Dict[int, List[QueueItem]]`, `unified_queue.py:131`, read/write at
`:520,542`) — meaning **queue state is lost on restart** whenever Redis is down, not a
durable store. Keyed `{namespace}:{community_id}:queue` (`_make_key`, `unified_queue.py:180-182`)
— no tenant segment, no app_id segment; predates both v3 tenancy and the bundle isolation
scheme (§9.2).

`QueueItem` (`unified_queue.py:46-67`): `id`, `track: MusicTrack`, `requested_by_user_id`,
`requested_at`, `votes`, `position`, `status: QueueStatus`, `community_id`, `voters: List[str]`.
`QueueStatus` (`unified_queue.py:37-42`): `queued | playing | played | skipped`.

Already implemented today, reusable as-is or near-as-is:

| Method | Location | Reuse for Music Station |
|---|---|---|
| `add_track(track, user_id, community_id)` | `unified_queue.py:184` | enqueue, both request and playlist-import paths |
| `remove_track(queue_id, community_id)` | `unified_queue.py:231` | single-track moderation kick (§7) |
| `vote_track(...)` | `unified_queue.py:288` | not in the brief's model — carry over or drop, TBD |
| `get_queue(community_id)` | `unified_queue.py:332` | presentation container's queue-state read |
| `get_next_track(community_id)` | `unified_queue.py:355` | process-stage now-playing advance |
| `mark_playing`/`mark_played` | `unified_queue.py:376,389` | same |
| `skip_current(community_id)` | `unified_queue.py:402` | same |
| `clear_queue(community_id)` | `unified_queue.py:426` | admin "clear queue" action |
| `reorder_by_votes(community_id)` | `unified_queue.py:453` | vote-based reprioritization, already implemented |

**Not implemented today, all new**: dedupe, per-requester limits, category filtering,
kick-whole-playlist, moderation audit, policy gate integration.

**Persistence store — open, §11.4**: keep Redis+fallback under the bundle isolation key
convention (`waddles:t:{tenant}:c:{community}:app:{app_id}:state`,
`2026-08-31-app-bundle-sdk-design.md:405-410`), or move to a durable Postgres table
alongside `app_activations` (`2026-08-31-app-bundle-sdk-design.md:254-266`)? The
in-memory fallback losing state on a Redis blip is a real operational risk for a
persistent, streamer-facing feature.

---

## 5. Request flow

**Today: no `!songrequest`/`!sr`/`!queue`-style chat command exists anywhere in the
repository** (repo-wide grep, zero hits) — this entire flow is new work, not a migration.

Maps directly onto the ingest-stage script contract
(`2026-08-31-app-bundle-sdk-design.md:192-207`, §4 table), the same shape as the
worked `!giveaway enter` example (`2026-08-31-app-bundle-sdk-design.md:502`):

```
chat "!songrequest <query|url>"
        │
        ▼
  ingest handler (source-specific bundle, §9)
    URL → sniff host → route to matching source's resolver
    bare text → search against a configured default source (per-community setting, TBD)
        │  emits Track (unresolved) event
        ▼
  process stage — shared queue manager (§9)
    1. policy check: song_requests_allowed (§7.2) — reject if off
    2. category check: requests_category_restricted (§7.4) — reject unless music-category
       or caller carries music.queue:moderate (override)
    3. per-requester limit / dedupe — NEW, not implemented today
    4. UnifiedQueue.add_track() (existing, unified_queue.py:184)
        │  emits queue-updated event
        ▼
  presentation container — re-renders queue state (§8)
```

Rejections (policy off, category blocked, limit hit) return a typed error back through
the ingest stage's response path (chat reply "requests are off" / "music only right
now"), never a silent drop — consistent with the platform's fail-closed authz posture
(`security.md`).

---

## 6. Playlist import flow

Community admin/mod supplies a playlist URL (Spotify/YouTube/SoundCloud). Differs from
the request flow only in fan-out and `added_via`:

```
admin "!musicimport <playlist_url>"  (or hub-webui action, TBD)
        │
        ▼
  ingest handler → source's get_playlist_tracks() (exists today for Spotify/SoundCloud,
                    spotify_provider.py:574, soundcloud_provider.py:720 — NOT for YouTube,
                    net-new work there)
        │  emits N Track events, added_via=PLAYLIST, sharing one import batch (§11.12)
        ▼
  process stage — same policy/category/dedupe gate as §5, per track
        │
        ▼
  UnifiedQueue.add_track() × N
```

No bulk-add method exists on `UnifiedQueue` today (`add_track` takes one `MusicTrack`,
`unified_queue.py:184-190`) — playlist import is N sequential calls unless a bulk method
is added; not treated as a blocker, just an implementation note.

---

## 7. Policy & moderation

### 7.1 Policy toggles (community admin, scope `music.policy:admin`)

| Toggle | Default | Effect |
|---|---|---|
| `song_requests_allowed` | proposed `true` | on/off switch for the entire request flow (§5) |
| `requests_category_restricted` | proposed `false` | when `true`, only `category`-matched (music) tracks accepted via request, unless a moderator override is used |

Enforced via `@require_scope("music.policy:admin")` (`libs/flask_core/flask_core/authz.py:81-161`)
on the settings-write endpoint — same fail-closed pattern as every other scoped route in
the platform (missing/invalid token or insufficient scope → 403, never a silent pass).

**Storage — open, folds into §11.11**: a dedicated `music_station_policy` table
(`community_id` PK/FK) vs `app_activations.config` JSONB
(`2026-08-31-app-bundle-sdk-design.md:254-266`) — the bundle spec's own config layer
already exists for exactly this "per-community settings for an activated bundle" shape.

### 7.2 Moderation (admin/moderator, scope `music.queue:moderate`)

| Action | Backing today | New work |
|---|---|---|
| Kick one track off the queue | `UnifiedQueue.remove_track()` (`unified_queue.py:231`) — reusable as-is | scope check + audit wrapper |
| Kick a whole imported playlist off the queue | — | needs a way to identify "every track from *this* import" — §11.12 |
| Category-restriction override (accept a non-music track anyway) | — | net new — bypasses §7.1's `requests_category_restricted` gate at enqueue time, gated on `music.queue:moderate` |

### 7.3 Audit

Every moderation action logged — matches the existing platform convention cited in
`docs/youtube_music_interaction_module/OVERVIEW.md`'s "AAA-compliant structured logging
for audit trails." Proposed `music_moderation_audit_log` table: `id`, `community_id`,
`moderator_user_id` (tokenized UUID, `backend-database.md` PII rule), `action`
(`kick_track | kick_playlist | category_override`), `target` (queue_id or import batch
id), `reason` (optional), `created_at`. No existing table to reuse — genuinely new.

### 7.4 Category — where it comes from

`category` on `Track` (§2) has no defined source yet — see §11.5. Spotify tracks don't
carry a track-level genre (only artists do); YouTube has a `categoryId`; SoundCloud has a
free-text genre tag. None of the three agree on shape, and none map cleanly onto a single
"is this music-category" boolean without an internal normalization layer.

---

## 8. Presentation container & browser-source player

**The presentation container is its own dedicated container — the 8th** (the design
doc's "Seven, from ~40" table, `2026-08-26-v3-scbm-apps-design.md:679-683`: `svc-ingest`,
`svc-process`, `svc-action`, `hub-webui`, `hub-api`, `svc-core`, `svc-rtc`). It is not a
consolidation of `svc-rtc`/`browser_source_core` — it is new.

### 8.1 The 4th stage-runner

It follows the **same distribution model** the bundle spec §6 defines for the other three
stage-runners: it polls hub-api for the installed bundle set and reconciles locally
(§6.2, `2026-08-31-app-bundle-sdk-design.md:322-341`), and reads per-community
activation/routing from the **read replica**, never the primary (§6.3,
`2026-08-31-app-bundle-sdk-design.md:343-360`). Same two-read-path split as §6.4 of that
doc — installed set is rare and is code, routing is frequent and is data.

**This implies the App Bundle model gains a 4th component type, `presentation`**,
alongside `ingest`/`process`/`action` (`2026-08-31-app-bundle-sdk-design.md` §2-§3). Unlike
the other three, a presentation component is **not** an async event script awaited by a
dispatch loop (`2026-08-31-app-bundle-sdk-design.md:200-203`) — it's HTML/JS overlay
assets + config, rendered client-side in an OBS browser source. `KNOWN_SURFACES`
(`libs/flask_core/flask_core/app_manifest.py:63`, currently `{ingest, process, action}`)
and `bundle.yaml`'s `stages` map (§3.2 of the bundle spec) both need a `presentation`
entry — **cross-referenced here, not specified here**; formalizing the schema
(`entrypoint`-equivalent, asset manifest shape, how `consumes` maps to a live-update
channel instead of a stream) is follow-up work on the bundle SDK spec itself.

### 8.2 Responsibilities

1. **Core overlays** — `full_screen` / `media` / `crawler`. Today's
   `browser_source_core_module` overlay taxonomy is `ticker`/`media`/`general`/`captions`
   (`docs/browser_source_core_module/CONFIGURATION.md:60`,
   `overlay_service.py:59,82`) — the exact rename/consolidation mapping onto
   `full_screen`/`media`/`crawler` is **not** confirmed 1:1 here (§11.10).
2. **The Music Station** — now-playing / up-next / request-list rendering, and the
   client-side player itself. Successor to today's
   `templates/music-player-overlay.html` (276 lines) and its never-wired `/ws/music`
   route (see §1.1).
3. **Each activated bundle's own presentation component** — e.g. a giveaway-wheel
   overlay — loaded and isolated the **same way** ingest/process/action bundle code is
   today: poll+reconcile (§6.2) per stage, per-community isolation key extended with a
   `:presentation` segment (`waddles:t:{tenant}:c:{community}:app:{app_id}:presentation`,
   extending `2026-08-31-app-bundle-sdk-design.md:397-410`'s scheme).

### 8.3 Per-community browser-source URLs

Today's `community_overlay_tokens` (`docs/browser_source_core_module/API.md:250-263`) —
per-community `overlay_key` (64-hex), grace-period key rotation, `enabled_sources`
array — already implements exactly this "one secret URL per community, OBS points a
browser source at it" pattern for the three existing overlay types. Extending it to cover
the Music Station and per-bundle presentation components (new `enabled_sources` values,
or a per-app_id token/segment) vs. minting a dedicated per-app_id token table is
**open — §11.9**, per explicit ask.

### 8.4 Client-side audio, not server-side

Per the model (§1 item 4): the presentation container serves the player HTML/JS and a
live queue-state channel (WS/SSE — mirrors the existing caption WebSocket pattern,
`browser_source_core_module/app.py:203`) — it does **not** decode, transcode, or proxy
audio server-side. What "client-side" means differs materially per source (§3, §11.8):
YouTube's iframe embed already satisfies this; SoundCloud's stream URL is directly
embeddable via `<audio>`; Spotify requires the Web Playback SDK to get real in-page audio
at all — today's Connect-device control (`spotify_provider.py:367-386`) plays audio
**outside** the browser source entirely, which does not satisfy the model.

---

## 9. Pipeline mapping to bundles

### 9.1 Stage mapping

| Stage | Component(s) | What it does |
|---|---|---|
| `ingest` | 3 source-specific bundles: `waddles.social.music-station.spotify-ingest`, `...youtube-ingest`, `...soundcloud-ingest` | OAuth-backed resolve of a request/playlist URL into `Track` events (§5, §6) |
| `process` | 1 shared component: `waddles.social.music-station.queue` | dedupe, ordering, requester limits, now-playing advance, category filtering (§4, §7) |
| `action` | optional, thin | e.g. an optional "now playing" chat announcement — not required by the model |
| `presentation` | Music Station player + queue/request-list rendering | client-side playback, live queue state (§8) |

### 9.2 The isolation tension (highest-priority open decision, §11.1)

The bundle spec's coexistence model isolates **every** bundle's state/streams by its own
`app_id` (`2026-08-31-app-bundle-sdk-design.md:397-410` — e.g. 4 giveaway bundles each get
fully independent `...:giveaway-classic:{ingest,process,action,dlq}` streams, §9.1 of that
doc, no shared state). Music Station's entire point is the **opposite**: 3 ingest bundles
(Spotify/YouTube/SoundCloud) must all feed **one shared** process-stage queue, not three
isolated ones — the whole value is intermingling across sources into a single
back-to-back playlist.

This is a real conflict with the bundle spec's per-`app_id` isolation-by-default model,
not a detail to paper over. Two ways to reconcile, neither decided here:

- **(a)** All 3 ingest bundles declare the same `produces` topic and the same downstream
  consumer group, so `process` naturally merges them — isolation stays nominally
  per-`app_id` at the stream-key level but the *consumer* is shared.
- **(b)** The process-stage queue manager is itself scoped to the **Feature**
  (`waddles.social.music-station`), not to an individual `app_id` — a deliberate,
  documented exception to §7.2's isolation-key convention for this one Feature.

---

## 10. Feature, gating, tier

`FeatureContract` (`libs/flask_core/flask_core/feature_contract.py:73-89`) requires:
`id`, `version`, `module`, `requires_scopes: FrozenSet[str]`, `min_tier`, `flag` (must
equal `f"waddles.{id}"`, enforced at `feature_contract.py:151-155`).

| Field | Proposal | Status |
|---|---|---|
| `id` | `social.music-station` (reuse) or `music.music-station` (new module) | **open, §11.2** |
| `module` | `social` — already owns `browser_source_core`, chat, presence (`2026-08-26-v3-scbm-apps-design.md:338`) — or `music`, requiring a `KNOWN_MODULES` addition (currently 11 entries, no `music`, `app_manifest.py:47-59`) | **open, §11.2** |
| `requires_scopes` | `{music.queue:read, music.request:write, music.policy:admin, music.queue:moderate}` | proposed, matches `require_scope()`'s `resource:action` convention (`authz.py:81-108`) |
| `min_tier` | one of `free`/`professional`/`enterprise` (`feature_contract.py:46`) | **open, §11.3** — no precedent in `critical-rules.md`'s gated-feature table |
| `flag` | `waddles.social.music-station` or `waddles.music.music-station` | derived from `id` decision |

---

## 11. Open decisions (for the user — not decided here)

| # | Decision | Why it's open |
|---|---|---|
| 1 | **Shared-queue vs per-app_id isolation** (§9.2) — highest priority, blocks the pipeline mapping | Music Station's single-shared-queue requirement directly conflicts with the just-merged bundle spec's per-`app_id` isolation-by-default model |
| 2 | **Module ownership** — reuse `social` vs add a new `music` module to `KNOWN_MODULES` (`app_manifest.py:47-59`) | Affects `Feature.id`, `flag`, Helm toggle grouping, and whether Music Station ships with Social or independently |
| 3 | **License tier** — free/professional/enterprise | No existing precedent; not in `critical-rules.md`'s gated-feature examples |
| 4 | **Queue persistence store** — keep Redis+in-memory-fallback (today's pattern, loses state on Redis outage) vs a durable Postgres table alongside `app_activations` | Persistent, streamer-facing feature; today's fallback already loses state on a Redis blip |
| 5 | **Category source** — source-platform metadata (inconsistent shape across the 3 platforms, §7.4) vs an internal classification layer | No platform agrees on a "music category" signal; affects `requests_category_restricted` accuracy |
| 6 | **Playback-sync across viewers** — server-authoritative position pushed to every browser source vs best-effort per-instance start-on-change | A streamer running 2 OBS scenes both showing the overlay could desync; low-stakes but unaddressed |
| 7 | **SoundCloud OAuth model** — new per-community `soundcloud_interaction_module` (mirrors Spotify/YouTube) vs continuing today's single-instance env-var OAuth (`soundcloud_provider.py:69-76`) | Needed for per-community multi-tenancy; the provider/streaming logic is otherwise close to ready |
| 8 | **Spotify playback mechanism** — Web Playback SDK (real in-browser audio, Premium-required, needs user-gesture) vs today's Connect-device control (audio plays outside the browser source) | Directly determines whether the model's "audio plays client-side" holds for Spotify at all |
| 9 | **Presentation-component URL/token minting** — extend `community_overlay_tokens` (`API.md:250-263`) vs a dedicated per-`app_id` token table | Affects every bundle shipping a presentation component, not just Music Station |
| 10 | **Overlay taxonomy rename** — today's `ticker`/`media`/`general`/`captions` vs proposed `full_screen`/`media`/`crawler` | No confirmed 1:1 mapping; `captions` in particular doesn't obviously fold into either new name |
| 11 | **Policy storage shape** — dedicated `music_station_policy` table vs `app_activations.config` JSONB (bundle spec §5.1) | The bundle spec's config layer already exists for "per-community settings on an activated bundle" |
| 12 | **Kick-whole-playlist targeting** — how does a moderator action address "every track from this import"? `Track.added_via=PLAYLIST` alone doesn't distinguish two different playlist imports | Needs an import-batch identifier; unclear whether it lives on `Track` or `QueueItem` |

---

## 12. Gap list (what's new)

| Today | Becomes |
|---|---|
| `MusicTrack` (`base_provider.py:14-37`) — not `slots=True`, no `category`/`requested_by`/`added_via` | `Track` (§2) — `slots=True, frozen=True`, closed `TrackSource`/`AddedVia` enums, all three new fields |
| SoundCloud: single-instance env-var OAuth embedded in `soundcloud_provider.py:69-76`, no dedicated interaction module | `soundcloud_interaction_module`, mirroring `spotify_interaction_module`/`youtube_music_interaction_module`'s per-community pattern (§11.7) |
| `UnifiedQueue` keyed ad hoc (`_make_key`, `unified_queue.py:180-182`), no tenant/app_id segment | Re-keyed under the bundle spec's isolation convention (§7.2 of that doc), persistence store open (§11.4) |
| No `category` field or filtering anywhere in track/queue model | `category` on `Track` + `requests_category_restricted` policy gate (§7) |
| No per-community policy toggles table | `music_station_policy` (or `app_activations.config`, §11.11) — `song_requests_allowed`, `requests_category_restricted` |
| Kick-one-track exists (`remove_track`, `unified_queue.py:231`); kick-whole-playlist and category-override don't | `music.queue:moderate`-scoped kick-track / kick-playlist / category-override, all audited (§7) |
| No moderation audit table | `music_moderation_audit_log` (§7.3) |
| `unified_music_module` has no `app.py` — library-only, pushes to endpoints that don't exist (`mode_controller.py:450`, `music_player.py:497` → `browser_source_core_module/app.py` has neither route) | Real `ingest`/`process` stage-script entrypoints per the bundle spec §4 contract |
| `templates/music-player-overlay.html:187` calls `ws://localhost:8052/ws/music`, never implemented server-side; template has no SoundCloud branch | Presentation container's Music Station player, real WS route, all 3 sources handled |
| No presentation container / 4th stage-runner / 4th bundle component type | New 8th container (§8.1); bundle model gains a `presentation` component type — cross-referenced here, formal `bundle.yaml` schema is follow-up work on the bundle SDK spec |
| `KNOWN_SURFACES = {ingest, process, action}` (`app_manifest.py:63`) | Gains `presentation` (follow-up bundle-spec work) |
| No `!songrequest`-style chat command anywhere in the repo (confirmed, repo-wide grep) | New ingest-stage command handler, same shape as the `!giveaway enter` worked example |
| No `music` entry in `KNOWN_MODULES` (`app_manifest.py:47-59`, 11 entries today) | Either reuse `social` or add `music` — §11.2 |
| No playlist-import method on the YouTube provider (`youtube_provider.py`) | Net-new work — Spotify and SoundCloud already have `get_playlist_tracks` |
