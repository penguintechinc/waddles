# waddles.core.service.music-youtube

Always-on `kind: service` bundle (Rust, `world stage-tools`) exposing the **common music tool
contract** for YouTube. Search/metadata only: Waddles never re-streams or downloads audio;
playback is the DJ app + svc-presentation Music Station overlay using the YouTube embed.

## Tools (contract: `bundles/contracts/music-tool-contract.v1.json`)

Spotify/SoundCloud bundles implement the same tool names as `music.<provider>.<tool>`;
`scripts/gen-music-manifest-tools.py <provider> [tool...]` emits the manifest `exported_tools`
block and `--check <manifest>...` fails on drift.

| Tool | In | Out |
|---|---|---|
| `music.youtube.search` | `query` (1..200), `limit` (1..25, default 10) | `tracks[]`, `cached`, `skipped`, `capability` |
| `music.youtube.track` | `id` (11-char video id) | `track`, `cached`, `capability` |
| `music.youtube.playlist.get` | `playlist_id`, `limit` (1..50), `page_token` | `tracks[]`, `next_page_token`, `cached`, `skipped`, `capability` |
| `music.youtube.resolve` | `url` (youtube.com / music.youtube.com / youtu.be only) | `track`, `cached`, `capability` |

Track: `provider,id,title,artist,duration_ms,thumbnail,explicit,source_url` plus
`channel_id,embeddable,live,labels`. `explicit` is `true` only for age-restricted videos,
otherwise `null` (YouTube has no explicit-lyrics flag; never a guessed `false`).
`capability = {playback:"embed", server_audio:false, queue_add:false}` so the DJ degrades loudly.
`search` uses `search.list` with `videoCategoryId=10` (Music), then one batched `videos.list`.

Errors are stable `invalid-args` codes: `rate_limited`, `quota_exceeded`, `not_found`,
`auth_unavailable`, `config_invalid`, `upstream_*`, `not_a_youtube_url`,
`playlist_url_use_playlist.get`, `no_video_id`; upstream outage = `target-unavailable`.

## Credentials (host-side only)

Requests carry secret-ref NAMES, never values: `YOUTUBE_API_KEY` (`?key=` query slot, default) or
`YOUTUBE_OAUTH_AUTHORIZATION` (`Authorization` header, a community's connected account via the
hub-api gateway connection). The host resolves and injects them and scrubs them from errors.

## Quota, cache, limits (bundle kv, per community; keys use `.`)

| Control | Default | Config key (kv `config`) |
|---|---|---|
| Upstream calls per minute | 20 | `rate_per_min` 1..600 |
| Quota units per UTC day (search=100, list=1) | 3000 | `daily_quota_units` 100..1e6 |
| Track metadata TTL | 6h | `cache_ttl_s` 60..604800 |
| Search/playlist id-list TTL | 1h | `search_ttl_s` 60..604800 |
| Region / safe search | none / moderate | `region_code` (`US`), `safe_search` |
| Auth mode | `api_key` | `auth_mode` `api_key`\|`oauth` |

Cache hits cost no quota and no rate budget. Invalid config fails loud. Logs carry tool name and
outcome code only (never queries, urls or ids).

## Legacy parity (`hub_api/services/music_providers/youtube.py`, `youtube_music_interaction_module`)

| Legacy feature | Here |
|---|---|
| `resolve(url)` all URL shapes (watch, youtu.be, embed, shorts, live) | `resolve` (+ music.youtube.com, strict host allowlist) |
| `search(query)` -> hydrated tracks | `search` (now category 10, safeSearch, region, limit) |
| Track: title/artist/duration/artwork/url | same (`thumbnail`, `source_url`) |
| `labels` (category + tags + topics, lowercased) | `labels` on every track |
| 24h in-process label cache | kv TTL cache (community-scoped) |
| API key / env OAuth / per-community connected account | host secret refs `api_key` / `oauth` |
| Quota/403 reason mapping | `quota_exceeded` / `upstream_forbidden` |
| Playlist read | new: `playlist.get` |
| Embeddable / live / age-restricted flags | new: `embeddable`, `live`, `explicit` |
| `youtube_music_interaction_module` | only `/status`; nothing to port |
| Queue add, playback, radio | DJ app + Music Station (not this bundle) |

Build: `cargo component build --release --target wasm32-wasip2 --locked`
(the core-bundle image does this in `bundles/Dockerfile.core-bundles`).
