# Bundle tool contracts

Single source of truth per contract lives in `bundles/contracts/`; producers (service bundles that export tools) and consumers (bundles that call them) must match it exactly. Drift gate: `scripts/ci/tests/test_bundle_contracts.py` (`BUNDLE_CONTRACT_ROOTS=dir:dir` scans other checkouts).

| Contract | File | Producer (authoritative) | Consumers |
|---|---|---|---|
| AI tools | `ai-tools.v1.json` | PR #1109 `bundles/rust/ai` | ai-teammates #1117, dj |
| Music | `music-tool-contract.v1.json` (byte-identical to #1110) | music-youtube #1110 | music-spotify #1111, music-soundcloud #1108, dj |
| Live channels | `live-channels-contract.v1.json` (byte-identical to #1119) | live-now #1119 (per platform bundle) | ai-teammates |
| Twitch clips | `twitch-clip-tools.v1.json` (= #1113 `contract/twitch-clip-tools.json`) | `bundles/rust/twitch` (twitch.clip.create/get/list) | twitch-clips #1113 |

Rules: tool inputs are closed (`additionalProperties:false`); consumers send only declared fields within producer bounds; consumers rely only on producer `output_schema.required`. Purpose/model selection is host-side, never a tool argument. Music providers may add extra tools (e.g. `player.*`) but common tools must equal the contract exactly; use `scripts/gen-music-manifest-tools.py` (#1110).

## Required diffs per PR

### #1117 ai-teammates (`bundles/rust/ai-teammates/contracts/*.json` + `src/ai.rs`)
- `ai.complete`: drop `purpose` from request; `system` maxLength 4000 -> 1200; `prompt` maxLength 8000 -> 3500 and minLength 1; required becomes `[prompt]`; response needs only `text`.
- `ai.classify`: drop `purpose`; labels items min 1/max 40, `maxItems` 16 -> 20 ok; add optional `instructions`; response `score` -> `confidence` (+ `reason`, both required); `text` minLength 1.
- `ai.rag.query`: request `query` -> `question` (maxLength 1000); `collection` required (pattern `^[a-z0-9][a-z0-9_.-]{0,39}$`); `top_k` optional; response `matches[]` -> `answer` + `citations[{doc_id,chunk,score}]`.
- `*.live.channels` fixtures (discord/twitch/youtube): request must send `scope` (`community`|`linked`), optional `cursor`, `limit` 1-100; response fields `channel_id`->`channel_ref`, `live`->`is_live`, `game`->`category`, add `display_name`; response `as_of` required; read only `channels`, `as_of` as guaranteed.

### #1111 music-spotify (`bundle.yaml` + `hub-manifest.yaml` exported_tools, `src`)
- Common tools must equal contract: `search` (`limit` max 25, output `tracks[]` with contract `track` shape: `id` not `external_id`, plus `thumbnail`, `explicit`, `source_url`; drop `count`; add `cached`, `skipped`, `capability`).
- `track`: input `track` -> `id`; output `{track, cached?, capability}`.
- `playlist.get`: input `playlist` -> `playlist_id` (max 64), `limit` max 50, drop `offset`, add `page_token`; output `{tracks, next_page_token, ...}`, drop `playlist`.
- `resolve`: `url` minLength 1, maxLength 512; output `{track, capability}` (drop `kind`/`id`/`playlist`/`tracks`; playlist URLs -> separate or error).
- `player.*` extras may stay.

### #1108 music-soundcloud (same files)
- `search`: input `q` -> `query`, drop `access`/`offset`, `limit` max 25; output contract `track` shape + `capability`/`cached`/`skipped`; drop `provider`/`next_offset`.
- `track`: id min 1/max 64; output `{track:{contract shape}, capability}` (was flat; `url` -> `source_url`, `duration_s` -> `duration_ms`).
- `playlist.get`: input `id` -> `playlist_id`, add `page_token`; output `tracks` contract shape, `next_page_token`, `capability`; drop `truncated`/top-level `license` (move under track extras).
- `resolve`: url min 1/max 512; output `{track, capability}` (no `kind`).

### #1110 music-youtube / #1109 ai / #1113 twitch-clips
- No schema change; #1110's contract file is byte-identical here (no merge conflict). #1109 fixtures equal `ai-tools.v1.json`; #1113 `contract/` equals `twitch-clip-tools.v1.json`.
- twitch platform bundle (owner of `bundles/rust/twitch`, not in these PRs) must export `twitch.clip.*` exactly per contract.

### dj / live-now (not yet pushed)
- dj: call only contract tools (`ai.*`, `music.<provider>.{search,track,playlist.get,resolve}`); request/response fixtures under `bundles/rust/dj/contracts/*.json` in the ai-teammates fixture format.
- live-now #1119: authoritative, no change.
