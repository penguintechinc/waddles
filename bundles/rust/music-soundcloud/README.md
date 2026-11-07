# waddles.core.service.music-soundcloud

Always-on `kind: service` bundle (Rust, `world stage-tools`) exposing SoundCloud metadata to
app bundles (the `dj` app) through gated tool calls. Parity: no legacy SoundCloud code exists
anywhere in the repo (checked 2026-11-07), so this is new surface, not a port.

Implements the shared music contract `bundles/contracts/music-tool-contract.v1.json`
(authoritative; manifests are generated/checked with `scripts/gen-music-manifest-tools.py soundcloud --check ...`).

| Tool | Input | Output |
|---|---|---|
| `music.soundcloud.search` | `{query, limit<=25}` -> `GET /tracks` | `{tracks, cached, skipped, capability}` |
| `music.soundcloud.track` | `{id}` (numeric or `soundcloud:tracks:N`) -> `GET /tracks/{urn}` | `{track, cached, capability}` |
| `music.soundcloud.playlist.get` | `{playlist_id, limit<=50, page_token?}` -> `GET /playlists/{urn}/tracks` | `{tracks, next_page_token, cached, skipped, capability}` |
| `music.soundcloud.resolve` | `{url}` (https://soundcloud.com permalink) -> `GET /resolve` (302) | `{track, cached, capability}`; playlist/user permalinks fail with `unsupported_resource_kind` |

Unknown fields and unknown tools fail loud (`invalid-args`). The caller never names a community.
`page_token` is an opaque `o<offset>` cursor; `skipped` counts upstream items dropped for missing
contract-required fields (e.g. no permalink).

## Track shape

Contract fields `{provider,id,title,artist,duration_ms,thumbnail,explicit,source_url}` plus
provider extras (the contract allows extras) that the DJ needs for DMCA-safe rules:

- `license`: `type` (e.g. `cc-by`, `all-rights-reserved`), `creative_commons`, `all_rights_reserved`,
  `streamable`, `downloadable`, `access` (`playable|preview|blocked`), `sharing`. `null` = SoundCloud
  did not say; treat unknown as not-allowed.
- `embeddable`, `full_playback`, `artist_id`, `genre`.
- `capability` (contract): `playback` = `embed` when the track may be widget-embedded else `none`
  (list tools report provider-level `embed`; check per-track `embeddable`), `server_audio:false`,
  `queue_add:false`. Waddles never re-streams or downloads audio. `explicit` is always `null`.

## Credentials, cache, limits

- OAuth 2.1 client-credentials token is host-side only. Requests carry header secret-ref
  `Authorization` -> `soundcloud.access_token`; the host injects `OAuth <token>`.
- Cache (bundle kv, per community): search 5m, track 1h, playlist 10m, resolve 1h; hits return `cached:true` and cost no budget.
- Per-community fixed window: 30 upstream calls/min. Over budget, upstream 429 or host rate
  limit -> `pool-exhausted`; a 429 also sets a `backoff` kv marker (Retry-After, clamped 1..300s,
  default 30s). A limiter that cannot count (kv down) fails closed.
- Logs: tool label + outcome only (never queries, ids, tokens).

## Docs
- Auth/guide: https://developers.soundcloud.com/docs/api/guide
- OpenAPI: https://developers.soundcloud.com/docs/api/explorer/api.json

Build: `cargo component build --release --target wasm32-wasip2 --locked`.
Tests: `cargo test` (unit + contract tests validating outputs/inputs against the shared contract file).
