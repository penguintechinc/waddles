# waddles.core.service.music-soundcloud

Always-on `kind: service` bundle (Rust, `world stage-tools`) exposing SoundCloud metadata to
app bundles (the `dj` app) through gated tool calls. Parity: no legacy SoundCloud code exists
anywhere in the repo (checked 2026-11-07), so this is new surface, not a port.

| Tool | What |
|---|---|
| `music.soundcloud.search {q, limit<=50, offset<=1000, access[]?}` | `GET /tracks`; `next_offset` when a full page returned |
| `music.soundcloud.track {id}` | `GET /tracks/{urn}`; `id` = numeric or `soundcloud:tracks:N` |
| `music.soundcloud.playlist.get {id, limit<=50}` | `GET /playlists/{urn}?show_tracks=false` + `/tracks`, truncated to `limit` |
| `music.soundcloud.resolve {url}` | `GET /resolve` (302) -> track or playlist; permalink must be `https://soundcloud.com/...` |

Unknown fields and unknown tools fail loud (`invalid-args`). The caller never names a community.

## Output contract (common music track)

`{provider, id, title, artist, artist_id, url, artwork_url, genre, duration_s, explicit, license, capability}`

- `license`: `type` (e.g. `cc-by`, `all-rights-reserved`), `creative_commons`, `all_rights_reserved`,
  `streamable`, `downloadable`, `access` (`playable|preview|blocked`), `sharing` -- `null` means
  SoundCloud did not say; the DJ must treat unknown as not-allowed for DMCA-safe mode.
- `capability`: `embed` (widget embed allowed), `full_playback` (access=playable). `stream_url`
  and `download` are always `false`: Waddles never re-streams or downloads audio; playback is the
  SoundCloud widget embed only. `explicit` is always `null` (SoundCloud exposes no such flag).

## Credentials, cache, limits

- OAuth 2.1 client-credentials token is host-side only. Requests carry header secret-ref
  `Authorization` -> `soundcloud.access_token`; the host injects `OAuth <token>`.
- Cache (bundle kv, per community): search 5m, track 1h, playlist 10m, resolve 1h. Cache hits cost no budget.
- Per-community fixed window: 30 upstream calls/min. Over budget, upstream 429 or host rate
  limit -> `pool-exhausted`; a 429 also sets a `backoff` kv marker (Retry-After, clamped 1..300s,
  default 30s) so later calls fail fast. A limiter that cannot count (kv down) fails closed.
- Logs: tool label + outcome only (never queries, ids, tokens).

## Docs
- Auth/guide: https://developers.soundcloud.com/docs/api/guide
- OpenAPI: https://developers.soundcloud.com/docs/api/explorer/api.json

Build: `cargo component build --release --target wasm32-wasip2 --locked`.
Tests: `cargo test` (unit + manifest/contract fixtures).
