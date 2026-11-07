# waddles.core.service.music-spotify

Always-on `kind: service` bundle (Rust, `world stage-tools`) owning the Spotify tool surface of the
music catalog. App bundles (e.g. the DJ/request-queue bundle) call these tools via `tool.invoke`;
app-to-app calls are forbidden. Spotify never re-streams audio: playback is Spotify Connect
(`play`/`queue`) against the streamer's own Premium device.

## Tools

| Tool | Access | Credential | What |
|---|---|---|---|
| `music.spotify.search {query, limit?<=50}` | read | app | `{count, tracks[], capability}` (empty result = empty list) |
| `music.spotify.track {track}` | read | app | normalized Track (`track` = id-URI or open.spotify.com URL) |
| `music.spotify.playlist.get {playlist, limit?<=100, offset?}` | read | app | `{playlist{...,returned,skipped}, tracks[]}`; local files/episodes/removed rows counted in `skipped` |
| `music.spotify.resolve {url}` | read | app | `{kind: track|playlist, id, track|playlist+tracks}` |
| `music.spotify.player.state {}` | read | connection | `{active, is_playing, device, track, ...}`; `active:false` when no device |
| `music.spotify.player.play {uris? \| context_uri?, offset?, position_ms?, device_id?}` | write, dangerous | connection | no content args = resume |
| `music.spotify.player.pause {device_id?}` | write, dangerous | connection | |
| `music.spotify.player.next {device_id?}` | write, dangerous | connection | applied once per idempotency key |
| `music.spotify.player.queue_add {track, device_id?}` | write, dangerous | connection | applied once per idempotency key |

Metadata tools ALWAYS use the app credential - callers cannot select the connected account (no `auth` arg; unknown args are rejected). The connected account is reachable only through `player.*`.

**Authorization (host `required_scope` in both manifests + handler `has_scope` check, `forbidden` before any host call):** `player.state/play/pause/next` need `community:moderate`; `player.queue_add` needs `community:member` (a verified member actor). Viewer requests are mediated and policy-checked by the DJ app, which alone holds the tool grant. Consequence: calls with no verified actor (timers) are refused - the DJ app must advance playback in a moderator-initiated context.
Every output carries `capability` (`playback: spotify-connect`, `requires_premium: true`,
`embed_only: false`) so callers degrade loudly. Normalized Track: `provider, external_id, title,
artist, artists[], album, duration_ms, artwork_url, url, uri, explicit` (superset of the legacy
`hub_api/services/music_providers/track.py`).

## Errors (typed `invalid-args` codes, never a default answer)

`premium_required`, `no_active_device`, `not_connected` (secret-ref unresolved), `connection_expired`
(401, reconnect), `insufficient_scope` (403, reconnect with playback scopes),
`rate_limited:retry_after=N`, `not_found`, `unsupported_kind`, `invalid_spotify_reference`,
`idempotency_key_required`, `invalid_config`, `upstream_*`. 5xx/transport = `target-unavailable`.

## Credentials (host-side only)

The guest sends `Authorization` only as secret-ref NAMES: `spotify.app` (client-credentials bearer)
and `spotify.oauth` (the community's connected Spotify account). The host resolves, refreshes and
injects `Bearer <token>` and scrubs it from responses; tokens never enter guest memory or logs.
OAuth (authorization code) is run by hub-api (`hub_api/services/oauth_providers.py`, `spotify`).

## Safety

Egress pinned to `api.spotify.com` (manifest `net.http.fqdn`); URLs/URIs shape-validated (exact host
`open.spotify.com`, no short-link redirects); unknown args rejected. Per-community fixed-window rate
limit (default 60 upstream calls/min), 429 `Retry-After` stored as a backoff (clamped 1..3600 s) and
honored before any further call, metadata cache with TTL (default 300 s). kv unavailable fails closed.
Logs carry tool label + outcome code only.

Optional kv `config` (per community; invalid = loud error): `{"market":"US","rate_limit_per_min":60,"cache_ttl_s":300}`.

## Legacy parity

| Legacy | Here |
|---|---|
| `music_providers/spotify.py` `resolve`/`search` | `track`/`resolve`/`search` (empty search = `[]`, track 404 = `not_found`; divergence from raising TrackNotFound) |
| `spotify_interaction_module` OAuth | hub-api gateway (unchanged) |
| (none) playback | `player.*` |

Build: `cargo component build --release --target wasm32-wasip2 --locked`. Tests: `cargo test --locked`
(unit + `tests/contract.rs` consumer-contract fixtures, 96% line coverage).
