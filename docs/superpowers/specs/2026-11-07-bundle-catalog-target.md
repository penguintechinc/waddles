# Target bundle catalog: hub-api = site foreman, consumer features = bundles

Status: DRAFT spec (2026-11-07), docs only. Read against `origin/release/v3.0.X` @ `10286de0e`. Calendar internals live in PR #1099 (`docs/superpowers/specs/2026-11-07-calendar-app-bundle.md`) -- referenced, NOT duplicated; this spec only adds the per-platform event-tool contract (s7).

## 1. Principles (locked)

- **hub-api = site foreman**: gateway for external visitors (integrations, OAuth, webhooks); directs workers (config, lifecycle, scaling); builds reports (analytics, audit). **No consumer apps/modules in hub-api.**
- Service bundles own tool definitions; app bundles invoke (app->service, service->service per MCP-tools framework; never app->app).
- **New bundles = Rust** (WASM component). Existing Python bundles may stay Python until replaced.
- No customer names anywhere. App bundles not tier-licensed; AI/media host features gate host-side (WaddleAI/BYOK/media seams). State community-scoped (numeric `community_id`, never slug).
- Redundancy rule: parity matrix (s9) proves coverage, THEN delete (git history keeps it).

## 2. Verified current state (code at release/v3.0.X)

| Area | Where today | Size / note |
|---|---|---|
| Events | `hub_api/blueprints/v1/{event.py 446, event_discord_sync.py 273}`; `action/interactive/calendar_interaction_module` (15 services); `bundles/python/event` (kv) | detail in #1099 |
| Music | `blueprints/v1/{music.py 352, community_music_queue.py 967, public_music_queue.py 181}`; `services/music_service.py 399`; `services/music_providers/{youtube.py 792, spotify.py 202}`; `core/unified_music_module/services/*` (~2.9k LOC incl. radio_player 929, unified_queue 646); `action/interactive/{youtube_music,spotify}_interaction_module` (thin: 59/57 LOC, spotify oauth_service only); `bundles/python/music` (!sr request queue, no playback); `core/svc_presentation/src/overlay/render/music.rs` (Music Station overlay) | no SoundCloud anywhere |
| Calls/voice/video | `blueprints/v1/calls.py 493` + `services/calls_proxy.py 162` -> `core/module_rtc` (**Go**, 5 .go files; rooms, lock, kick, mute-all, raised hands, `/interact/voice/rooms`); `core/svc_streaming/src/{rtc,ingest/whip.rs,egress/whep.rs}` already hold the Rust WebRTC stack | Go phase-out: REPLACE module_rtc, never extend |
| Game servers | `bp bot.py` rcon routes (servers CRUD/test/command/kick/ban/channels/move/message/log/policy/enforce/access-log/info/status/players); `services/bot_rcon.py 507`; `action/interactive/{server_manager 485,server_status 276}_interaction_module` (+encryption/enforcement/provider/rcon/status services) | credentials encrypted in module |
| Live status | `blueprints/v1/{live_activity.py 88,live_status.py 185}`; `services/{live_activity 196,live_status_service 105}` | reads platform live state |
| Overlay admin | `blueprints/v1/overlay.py 305` (browser-sources, overlay cfg/rotate/stats) | platform config for svc-presentation: stays hub-api (admin of a host service), NOT a consumer feature -- see Q4 |
| AI | `core/ai_researcher_module` (~11.5k LOC; blueprints build/clip_researcher/event_lookup/game/patch/price/tech; helm `ai-researcher` values.yaml:2466 + kustomize + CI build); `action/interactive/ai_interaction_module` (chat, ollama/waddleai providers, `proactive_chat_service`, `chatter_rate_limiter`, `prompt_safety`); hub-api `bot.py` ai-chatter + ai-knowledge routes, `services/{bot_ai_chatter 116,bot_ai_knowledge 976}`; `bundles/python/memories`; `action/interactive/memories_interaction_module` | knowledge docs live in hub-api tables |
| Welcome | `action/interactive/welcome_interaction_module` (`/welcome/check`, `ai_client_service`); `bundles/python/greeter` (keyword responder -- NOT welcome; stays) | |
| Clip | `action/interactive/clip_interaction_module` (create/bookmark/list/reels/overlay; `twitch_clip_service`) | |
| Shoutout | `bot.py` shoutout routes + `bundles/python/shoutout`, `action/interactive/shoutout_interaction_module` | out of scope, listed for completeness (Q5) |
| Platform bundles (Rust) | `discord` (tools: `discord.scheduled_event.{create,update,cancel,delete}`, `discord.member.roles.{add,remove,set}`, `discord.roles.list`); `twitch` (tools: `get_chatters`, `get_followers`, `get_subscriptions` -- `tools.rs:120`; **no** schedule/clip/live tool); `youtube` (templates only: poll, chat send, search; **no** tool surface); `wix` (inbound-only webhook connector, "no egress, no tools" -- `bundles/rust/wix/README.md`; #1054 generic Wix integration + #1073 webhook ingest merged) | tool surfaces to add in s7/s8 |
| Streaming | `core/svc_streaming`: WHIP ingest `ingest/whip.rs` (573 LOC), WHEP fan-out `egress/whep.rs` (514), `rtc/{pc_factory,fanout,ingest_auth,rtp_leg,sdp_writer,config,metrics}.rs` (~1.3k), `egress/{relay 729,hls,record,discord_voice}`; pipeline `supervisor.rs` 1940 LOC (real, `StubSupervisor` remains at :684) | built on **`webrtc`-rs =0.21.1** (`Cargo.toml:84`), NOT str0m; no ring buffer yet; `rtc/mod.rs` documents unproven real-browser RTP delivery |

## 3. Target catalog

Kind: S = service bundle (owns tools), A = app bundle (invokes), P = platform service bundle. All new = Rust.

| Bundle | Kind | Lang | Tools exposed | Tools consumed | Replaces (old paths) | Cut |
|---|---|---|---|---|---|---|
| `calendar` | A | Rust | (admin actions only) | `discord.scheduled_event.*`, `twitch.schedule.*`, `wix.event.*`, `chat.send` | `event.py`, `event_discord_sync.py`, `calendar_interaction_module`, `bundles/python/event` (per #1099) | v3.0 (discord) / v3.1 (twitch, wix) |
| `discord` (extend) | P | Rust | existing + `discord.live.channels` + `discord.voice.*` (v3.1) | -- | -- | live tool v3.0 |
| `twitch` (extend) | P | Rust | + `twitch.schedule.{list,create,update,delete}`, `twitch.clip.create`, `twitch.live.channels` | -- | `clip_interaction_module/twitch_clip_service` | v3.0 live+clip; schedule v3.1 |
| `youtube` (extend) | P | Rust | + `youtube.live.channels` | -- | `live_status_service` (yt part) | v3.0 |
| `kick` (new, if API allows) | P | Rust | `kick.live.channels` | -- | -- | v3.1 (Q6) |
| `wix` (extend) | P | Rust | + `wix.event.{list,create,update,cancel}` (outbound Wix Events API) | -- | -- | v3.1 |
| `live-now` | A | Rust | (admin actions) | `*.live.channels`, `chat.send` | `live_activity.py`, `live_status.py`, `live_activity.py`/`live_status_service.py` | v3.0 |
| `music-youtube` | P/S | Rust | `music.youtube.{search,track,playlist.items,queue.add,playback}` | -- | `music_providers/youtube.py`, `youtube_music_interaction_module` | v3.0 |
| `music-spotify` | P/S | Rust | `music.spotify.{search,track,queue.add,playback}` | -- | `music_providers/spotify.py`, `spotify_interaction_module` | v3.0 |
| `music-soundcloud` | P/S | Rust | `music.soundcloud.{search,track,stream_url}` | -- | (new) | v3.1 |
| `dj` | A | Rust | (admin actions; queue read for Music Station) | `music.*.*`, `ai.complete`, `ai.classify`, `chat.send` | `music.py`, `community_music_queue.py`, `public_music_queue.py`, `music_service.py`, `core/unified_music_module`, `bundles/python/music` | v3.0 (static+requests) / v3.1 (AI DJ, auto-remove) |
| `ai` | S | Rust | `ai.complete`, `ai.classify`, `ai.embed`, `ai.rag.{ingest,search,delete}`, `ai.memory.{put,search,forget}` | host WaddleAI/BYOK | `ai_interaction_module` providers, `bot_ai_knowledge.py`, `bot_ai_chatter.py`, `memories_interaction_module`, `bundles/python/memories`, `ai_researcher_module` model plumbing | v3.0 (complete/classify/rag) / v3.1 (memory) |
| `ai-teammates` | A | Rust | (admin actions) | `ai.*`, `chat.send`, `*.live.channels` (opt) | `ai_researcher_module`, `welcome_interaction_module`, `ai_interaction_module/proactive_chat_service`, `bot.py` ai-* routes | v3.0 Researcher+Receptionist / v3.1 Producer+DJ teammate |
| `clip-capture` | S | Rust | `clip.capture` (last N s of a stream), `clip.get`, `clip.list`, `clip.delete` | host `svc_streaming` ring-buffer seam, bundle object storage | `clip_interaction_module/clip_service` (reels part -> Q7) | v3.1 |
| `clips` | A | Rust | (admin actions) | `twitch.clip.create`, `clip.*`, `chat.send` | `clip_interaction_module` commands | v3.0 (twitch native) / v3.1 (capture) |
| `webrtc` | S | Rust | Signaling/policy: `whip.{endpoint_create,endpoint_revoke,list}` (ingest tokens), `whep.{endpoint_create,endpoint_revoke,list}` (playback), `room.{create,close,list,get,lock,unlock}`, `participant.{token_issue,token_revoke,kick,mute}`, `track.{publish_route,subscribe_route}`, `relay.{create,close}` (pass-through proxy), `recording.{start,stop}`, `stats.get` | host `svc_streaming` seam (existing `ingest/whip.rs`, `egress/whep.rs`, `rtc/*`); TURN/ICE host config | `calls_proxy.py`, `core/module_rtc` (Go); WHIP/WHEP control surface of svc-streaming | v3.0 minimal (whip/whep endpoint + room token) / v3.1 full |
| `rooms` | A | Rust | (admin actions) | `webrtc.*`, `discord.voice.*` (opt), `chat.send`; event link is data (UUID), no app->app call | `calls.py`, `/interact/voice` | v3.1 |
| `game-servers` | A | Rust | (admin actions) | `gameserver.*`, `chat.send` | `bot.py` rcon routes, `bot_rcon.py`, `server_manager`/`server_status` modules | v3.1 |
| `gameserver` | S | Rust | `gameserver.{status,players,command,kick,ban,message,move}` (RCON/Source/Minecraft/etc. per provider) | host secret store (rcon creds) | provider/rcon/enforcement services | v3.1 |

## 4. Music architecture

```
 viewers/mods (chat !sr !music)      webui (thin)       Music Station overlay
        |                                |                      ^
        v                                v                      | queue + now-playing (read)
   dj APP bundle  <-- admin actions -- hub-api (gateway only)   |
   | queue/policy (bundle tables,community-scoped)              |
   | tool.call (host-mediated, grant-checked)                   |
   +--> music-youtube   (search/track/queue.add/playback)       |
   +--> music-spotify   (search/track/queue.add/playback)  svc-presentation (reads dj
   +--> music-soundcloud(search/track/stream_url) [v3.1]    state via host media seam)
   +--> ai SERVICE  ai.classify / ai.complete (policy parse, gap-fill)
   +--> chat.send   (replies, removal reasons)
 OAuth: provider consent -> hub-api gateway callback -> host connection store
        (tokens never reach any guest; bundle holds opaque connection handle)
```

- Playback: provider API permitting only. YouTube/YT Music has no sanctioned server-side audio playback -> `playback` = "hand to overlay player (embed iframe in Music Station)"; Spotify = Connect `play`/`queue` against the streamer's own Premium device; SoundCloud = metadata + widget embed. ToS/DMCA: Waddles never re-streams or downloads audio; overlay uses provider embeds only. Each tool returns `capability` flags so `dj` degrades loudly, not silently.
- Music Station overlay reads `dj` state through the existing host media/presentation seam (Q3: confirm seam; today `render/music.rs` is fed by hub-api queue routes, which die with the parity delete).

## 5. DJ policy enforcement

| Layer | Behavior |
|---|---|
| Static config | `dj.sources_allow` (provider+channel/artist allowlist e.g. NCS/RFM), `deny_tracks`, `deny_artists`, `max_per_user`, `max_duration_s`, `explicit_allowed`, `cooldown_replay_s` -- all enforced **deterministically** in bundle code, no AI |
| Instruction text | Free-text channel instructions ("don't play X, overplayed"; "only NCS and RFM") parsed ONCE at save-time by `ai.classify` into structured rules (allow/deny lists, genre, constraints) -> shown to admin for confirmation before activation; re-parse only on edit. Runtime never trusts raw instruction text per-request |
| Runtime decision order | 1 deterministic deny/allow (source allowlist, deny lists, replay cooldown, duration) -> 2 provider metadata (title/channel/explicit flag) -> 3 optional AI judge (`ai.classify` w/ track metadata only, never chat content) only for rules not expressible deterministically -> 4 allow |
| DMCA-safe mode | `dj.dmca_safe=true` forces allowlist-only sources (channel ids of license-friendly labels); non-allowlisted requests rejected with reason. Default ON when a stream destination is live |
| Auto-remove (v3.1) | request violating rules -> removed, reason sent via `chat.send` to requester (UUID-routed), audit row `{community_id, request_id, rule_id, decided_by: deterministic|ai, ts}`; mod override `!music restore <id>` re-queues, skips AI re-judge, audited |
| AI failure | AI unreachable/low-confidence => request held "pending mod review" (fail-closed to mods), never auto-played |
| Audit | every enqueue/remove/override logged PII-free (numeric community_id, request id, rule id, outcome) |

AI DJ teammate (v3.1): genre + limiting instructions -> builds on-demand playlist via `music.*.search` filtered through the same pipeline; fills gaps only when queue empty; hard cap per hour; same policy gate -- the AI never bypasses s5.

## 6. AI Teammates + AI service

```
 chat/ingest -> process -> ai-teammates APP bundle (per-teammate config, community-scoped)
                |  Researcher   : ai.rag.search + ai.complete  (game/tech/price/patch lookups)
                |  Receptionist : member-join/first-message trigger -> ai.complete -> chat.send (absorbs Welcome)
                |  Producer     : timer hook -> quiet-chat check -> ai.complete -> chat.send   [v3.1]
                |  DJ teammate  : drives dj APP? NO (app->app forbidden) -> see below           [v3.1]
                v
         ai SERVICE bundle --host--> WaddleAI / BYOK (tier + key gating host-side), RAG store, memories
```

- **DJ teammate boundary**: app->app is forbidden, so the AI-DJ lives INSIDE the `dj` bundle (calls `ai` service tools directly); `ai-teammates` just links to its config in the admin view. Q2.
- **Producer pacing/safety**: opt-in per community, default OFF; min interval (default 20 min, floor 5), max N/hour, quiet-hours window, only fires when no human message for T minutes AND stream is live (`*.live.channels`); never replies to a specific user (no PII), output passes (a) deterministic blocklist, (b) `ai.classify` moderation, (c) length cap; on any failure -> post nothing. Per-community kill-switch + mod `!producer pause`. Rate counters in bundle kv (community-scoped).
- **Prompt-injection guard** (all teammates reading chat): chat text is DATA, wrapped/delimited and length-capped; tools exposed to the model = none (teammates call tools from code, never model-selected); system prompt never contains secrets; output scanned for `@everyone`/links/pings per existing `prompt_safety.py` rules (port its tests as acceptance corpus); no model output executed as a command; refusal of instructions embedded in chat.
- `ai` service: model selection default non-PRC (gemma per standing decision); BYOK/tier decided host-side; `ai.rag.*` replaces hub-api ai-knowledge (sources, reindex, search, suggest, feedback).

## 7. Calendar x platform event tools (user addition)

Calendar internals: #1099. Contract this spec adds: each platform bundle exposes event tools; calendar invokes.

| Platform | Existing | Must add | Notes |
|---|---|---|---|
| Discord | `discord.scheduled_event.{create,update,cancel,delete}` (`bundles/rust/discord/bundle.yaml:77-170`) | `discord.scheduled_event.list` (needed for import + drift) | already covers push |
| Twitch | none | `twitch.schedule.{list,create,update,delete}` over Helix `/schedule` (+`/schedule/segment`); scopes `channel:manage:schedule`; segment id = mapping ref; recurring segments are Twitch-side only: Waddles pushes single instances | read tool enables import |
| Wix | inbound webhooks only (`wix` README: "no egress, no tools"; events arrive as normalized records via `svc_ingest` `Scheme::WixJwt`) | `wix.event.{list,create,update,cancel}` over Wix Events API; needs egress `www.wixapis.com` + connection handle (app instance token host-side) | inbound webhook `event` records feed import; outbound is new |

| Rule | Design |
|---|---|
| Direction | **Waddles calendar = source of truth** (default push-only). Per-platform, per-community opt-in `import` (Twitch schedule, Wix events) creates calendar events with `source=<platform>`; imported events are read-only in Waddles unless `adopt=true` (then become Waddles-owned and pushed back) |
| Mapping store | calendar bundle table `event_sync(event_id, community_id, target SMALLINT enum {1 discord,2 twitch,3 wix}, target_ref, remote_id, remote_version/etag, last_synced_hash, status)`, UNIQUE(event_id,target,target_ref); never Valkey (see #1099 s5/s7) |
| Idempotency | key `sha256(community_id:event.public_id:op:version:target)`; create guarded by `remote_id IS NULL` + row lock; imports keyed by `(target, remote_id)` UNIQUE so re-import is a no-op |
| Conflict | push-only: remote edit detected on reconcile (hash/etag drift) -> Waddles wins, overwrite + audit. Import mode: remote wins for `source!=waddles`; for adopted events Waddles wins. Never two-way merge. Remote delete of a pushed event -> recreate once, then flag `error` for admin |
| Platform limits | Discord name<=100/desc<=1000; Twitch segment title<=140, duration 30-1380 min, 1 yr horizon; Wix per-site category/timezone required -> per-target payload adapter inside calendar, limits surfaced as admin-visible `error` rows |
| Tool grants | all write tools `risk: dangerous` (admin approval + activation consent); read/list `normal` |
| Cut | v3.0: Discord only (as #1099). v3.1: Twitch push + import, then Wix push + import |

## 7b. WebRTC service + Rooms app

`webrtc` is the single control/tool/policy surface for ALL WebRTC concerns in streaming: WHIP publish into the pipeline, WHEP playback out, SFU / pass-through relay, rooms. The media path stays in svc-streaming Rust; the bundle never touches RTP.

```
 browsers / OBS / apps ==== WebRTC media + WHIP/WHEP HTTP signaling (direct, host-side) ====+
                                                                                            v
 Rooms APP --tool.call--> webrtc SERVICE bundle --host seam--> svc_streaming (Rust, stays)
 (rooms, events,          (policy + tool surface:               ingest/whip.rs   POST /whip/{token}  -> TrackFanout (copy path, no ffmpeg)
  virtual rooms,           token/endpoint mint+revoke,                          \-> sdp_writer -> ffmpeg (transcode path) -> pipeline
  access, schedule)        room/track routing, relay,           egress/whep.rs  POST /whep/..       <- TrackFanout (SFU: 1 encode, N PCs)
                           recording hooks, stats)              egress/relay.rs  pass-through proxy; rtc/{pc_factory,rtp_leg,...}
   event link = UUID data                                       TURN/ICE + UDP range = host config; recording -> bundle object storage
 other apps (DJ, clips, live-now) never call webrtc; they use streaming-adjacent tools only
```

How existing code is exposed (no rewrite, no media in guests):

| Existing (svc-streaming) | Today | Becomes |
|---|---|---|
| WHIP token auth `rtc/ingest_auth.rs` (`WhipTokenAuthorizer`) | token authorizer, per stream key | `webrtc.whip.endpoint_create/revoke` mints/revokes tokens through the same authorizer (host-side state); bundle holds only the opaque endpoint ref |
| `ingest/whip.rs` `POST/DELETE /whip/{token}[/{session}]` | live router, non-trickle | unchanged host endpoint; bundle controls WHICH tokens exist + per-token policy (codec allowlist, max bitrate, community) |
| `egress/whep.rs` `WhepState::register_fanout` / `router` | mechanism exists; **nothing wires a real pipeline yet** (doc `whep.rs:17-21`) | `webrtc.whep.endpoint_create` + `track.subscribe_route` call `register_fanout`; wiring `PipelineId`+profile -> fanout is host work (P4b-H, below), then bundle exposes it |
| `rtc/fanout.rs` `TrackFanout` | SFU fan-out | `track.publish_route` / `subscribe_route` select copy vs transcoded fanout per room/stream |
| `egress/relay.rs` | RTMP/SRT relay out | pass-through proxy tools (`relay.create/close`) with destination allowlist (host-side; guests never supply raw URLs) |
| `rtc/config.rs`, `pc_factory.rs` ICE/UDP range | env/host config | stays host-only; never in tool I/O |
| `egress/record/*` | file recording | `recording.start/stop` -> object storage via bundle storage model |
| `api/live_channels.rs` (community channels list) | hub-style route, Twitch/YT only | superseded by `*.live.channels` tools (s8); route deleted with D3 |

- Spec-vs-code notes (verify before building): (a) the stack is `webrtc`-rs 0.21.1, not str0m -- keep it unless the user wants a migration (Q14); (b) `rtc/mod.rs` records an unresolved gap: in-process peer-to-peer RTP delivery untested, real browser<->server WHIP/WHEP delivery unproven -- a live smoke test is a P4b-H exit criterion; (c) AV1-over-WebRTC deferred (H264/VP8/Opus only); (d) `SettingEngine::set_udp_network` is a TODO in the crate, port range enforced by `PortAllocator`.
- `webrtc` owns: endpoint/token lifecycle (JWT/opaque tokens <=1h, signed host-side), room lifecycle, participant token issue/revoke (room+role claims), track routing, relay, recording hooks, stats. TURN/ICE secrets never returned to guests (tokens embed server-minted ICE creds).
- `rooms` owns the nuances: room/stream <-> event mapping (UUID only), virtual rooms (breakout/stage/green room = child rooms, parent id + type enum), membership/role access (scopes `rooms:join|speak|moderate`, never role names), schedule windows (auto-open/close from event time), capacity.
- Calendar tie: `rooms` and `calendar` never call each other (app->app forbidden); link is data (event `public_id` set via admin action or `!rooms link <event>`); Q12 asks about a host event-changed seam.
- Go phase-out: `core/module_rtc` (Go) is replaced, not extended; `svc_streaming/src/rtc` (Rust) is the base.

## 8. Live channels tool + Clip

**`*.live.channels` contract** (each platform bundle; same shape):

```
in : { scope: "community"|"linked", cursor?: string, limit?: 1-100 }   // channels = host-resolved connections of this community, never caller-supplied ids
out: { channels: [ { channel_ref: opaque, display_name, is_live: bool,
        title?, category?, viewers?: int, started_at?: RFC3339, thumbnail_ref? } ],
       cursor?, as_of: RFC3339 }
err: platform_unavailable | rate_limited | not_connected   // loud, no empty-success fallback
```

- No user PII in output (channel/stream metadata only); cached 30-60s host-side; `live-now` app announces/aggregates and feeds overlays. Replaces `live_activity` (SSE stream -> `live-now` pushes via overlay seam, Q3) and `live_status`.
- Twitch Helix `/streams`; YouTube `search.list eventType=live` or `videos.list liveStreamingDetails` (quota-aware, cache); Discord = voice/stage/activity presence per linked guild (what `live_activity.py` reads today).

**Clip**
- `twitch.clip.create` (service tool): Helix `POST /clips`, returns clip id/url; broadcaster pinned host-side (same destination-arg rejection as `tools.rs:35`). Replaces `twitch_clip_service`.
- `clip-capture` (service bundle on svc-streaming): svc-streaming keeps a per-stream rolling in-memory/spool **ring buffer** (default 60s, configurable cap) for streams passing through; `clip.capture{stream_ref, seconds<=30}` asks the host seam to cut the last N s (keyframe-aligned remux, no re-encode) -> writes MP4 to bundle object storage under the bundle prefix (`2026-11-06-bundle-object-storage.md`: 1 bucket, prefix per bundle, tag for media) -> returns `{clip_id, object_ref, duration_s}`. Retention TTL + per-community quota; capture only for streams the community owns; download via signed URL minted host-side.
- Pre-req: ring buffer in `core/svc_streaming` (new; `egress/record` spool is the closest base) -- scope risk, v3.1.

## 9. Parity matrix (old feature -> new home)

Status legend: C = covered by target bundle (design), M = missing/needs explicit work, X = dropped by decision.

| Old feature | Old path | New home | Status |
|---|---|---|---|
| Event CRUD/approve/rsvp/search | `event.py`, calendar module | `calendar` | C (#1099) |
| Discord scheduled-event sync | `event_discord_sync.py` | `calendar`+`discord` tools | C |
| Calendar OAuth/booking/availability/tournaments/tickets | calendar module | none | M -> Q (#1099 Q5/Q6) |
| Music provider settings + OAuth connect/disconnect | `music.py` /settings,/providers | `music-*` (OAuth via hub-api gateway) + `dj` config | C |
| Radio stations CRUD | `music.py` /radio-stations, `radio_player.py` | `dj` static "stream/station" source type | M (v3.1; Q8) |
| Request queue + policy + reorder + advance + playlists | `community_music_queue.py`, `unified_queue.py` | `dj` | C |
| Public queue read | `public_music_queue.py` | `dj` read view -> Music Station | C |
| Playback control `/music/playback`, status | `community_music_queue.py` | `dj` + `music-*.playback` | C (capability-dependent) |
| Mode controller (queue vs radio vs...) | `mode_controller.py` | `dj` modes: playlist/requests/ai | C (v3.1 ai) |
| YouTube search/track meta | `music_providers/youtube.py` | `music-youtube` | C |
| Spotify search/track/oauth | `music_providers/spotify.py`, `spotify_interaction_module` | `music-spotify` | C |
| SoundCloud | none | `music-soundcloud` | new |
| `!sr` `!music` chat commands | `bundles/python/music` | `dj` (aliases kept) | C |
| WHIP ingest endpoint + token mgmt | `svc_streaming` `ingest/whip.rs`, `rtc/ingest_auth.rs` | `webrtc.whip.*` (host code kept) | C (v3.0 min) |
| WHEP playback endpoint + SFU fan-out | `egress/whep.rs`, `rtc/fanout.rs` | `webrtc.whep.*`, `track.*_route` (host code kept; pipeline wiring pending) | M until P4b-H wires `register_fanout` |
| Pass-through relay / proxy | `egress/relay.rs` | `webrtc.relay.*` | C (v3.1) |
| Room create/get/close/list | `calls.py`, `calls_proxy.py`, `module_rtc` | `webrtc.room.*` | C (v3.0 min) |
| Lock/unlock, kick, mute-all, raised hands, participants | `calls.py`, `module_rtc` | `webrtc.room.lock/unlock`, `participant.kick/mute`, `raised_hands.list` | C (v3.1) |
| Join token issue (module_rtc auth) | `module_rtc` | `webrtc.participant.token_issue/revoke` | C (v3.0 min) |
| `/interact/voice/rooms` (chat/Discord-initiated rooms) | `calls.py` | `rooms` app commands | C (v3.1) |
| Room<->event link, virtual rooms (breakout/stage/green room), role-based access, scheduling | none today | `rooms` | new (v3.1) |
| Recording | none in module_rtc | `webrtc.recording.*` -> bundle object storage | new (v3.1) |
| RCON servers CRUD/test/cmd/kick/ban/move/message | `bot.py`, `bot_rcon.py` | `game-servers`+`gameserver` | C (v3.1) |
| RCON policy/enforce/access-log | `bot.py`, `enforcement_service.py` | `game-servers` | C (v3.1) |
| Server status/players | `server_status` module, `bot.py` info | `gameserver.status/players` | C (v3.1) |
| Live activity (poll+SSE) | `live_activity.py` | `*.live.channels`+`live-now` | C |
| Live status (`/live`) | `live_status.py` | same | C |
| AI chatter config + proactive chat | `bot_ai_chatter.py`, `proactive_chat_service.py` | `ai-teammates` Producer | C (v3.1) |
| AI knowledge sources/reindex/search/suggest/feedback | `bot_ai_knowledge.py` | `ai.rag.*` + Researcher | C (feedback loop: M) |
| AI researcher (build/game/patch/price/tech/event lookup/clip researcher) | `ai_researcher_module` | `ai-teammates` Researcher | M: per-lookup parity audit (Q1) -- 7 blueprints, some need external HTTP |
| AI chat completion/providers | `ai_interaction_module` | `ai` | C |
| Prompt safety | `prompt_safety.py` | `ai-teammates`/`ai` guard (ported tests) | C |
| Welcome | `welcome_interaction_module` | Receptionist | C |
| Memories | `memories_interaction_module`, `bundles/python/memories` | `ai.memory.*` | C (v3.1) |
| Clip create/bookmark/list | `clip_interaction_module` | `clips`+`twitch.clip.create` | C |
| Reels/clip overlay | clip module | `clips`+`clip-capture` | M (Q7) |
| Overlay config/browser-sources | `overlay.py` | stays hub-api (host admin) | N/A (Q4) |
| Shoutout, inventory, translate etc. | various | not in this spec | N/A (Q5) |

**Rule: no row may be M at delete time.** M rows either gain a bundle feature or get an explicit X decision recorded here by the user.

## 10. Deletion list (per PR, only after parity C + soak)

| Delete PR | Removes | Gate |
|---|---|---|
| D1 (calendar) | per #1099 s12 | calendar ACTIVATED on alpha + migration verified |
| D2 (music) | `music.py`, `community_music_queue.py`, `public_music_queue.py`, `services/music_service.py`, `services/music_providers/`, `core/unified_music_module`, `youtube_music`/`spotify` modules, `bundles/python/music`, music routes' helm/httproute entries; webui music pages repointed to generic bundle-admin | dj+music-* parity C incl. data migration verified |
| D3 (live) | `live_activity.py`, `live_status.py`, their services | live-now + 3 tools live |
| D4 (calls) | `calls.py`, `calls_proxy.py`, `core/module_rtc/` (whole Go module) + its helm/CI/httproute entries | webrtc (full) + rooms (v3.1); no Go left behind |
| D5 (gameserver) | rcon block in `bot.py`, `bot_rcon.py`, `server_manager`, `server_status` modules | game-servers (v3.1) |
| D6 (ai) | `bot_ai_*.py`, ai routes in `bot.py`, `ai_interaction_module`, `welcome_interaction_module`, `memories_*`, `bundles/python/memories`, `core/ai_researcher_module` + helm `ai-researcher` (values.yaml:2466, templates/core/ai-researcher.yaml, kustomize, `build-ai-researcher.yml`, httproute, `service-roles.yaml` entry) | s9 all C incl. researcher audit |
| D7 (clip) | `clip_interaction_module` | clips + capture |

Each D-PR: delete code + tests + helm/CI/DB-role refs; drop tables only a release after soak (dual SQL + Alembic chain caveat per #1099 s11).

## 11. Data migration (one-shot, reversible; zero rows read = FAIL)

| Data | Source | Target | Notes |
|---|---|---|---|
| Music queue + policy + providers | hub-api music tables (policy, queue, playlists, radio stations, provider connections) | `dj` bundle tables; OAuth tokens: re-link via host connection store (re-consent acceptable, Q9) | dry-run counts per community; legacy tables retained one release |
| Song requests kv | `bundles/python/music` kv | `dj` queue | ids remapped |
| AI knowledge sources/docs/embeddings | hub-api `bot_ai_knowledge` tables | `ai.rag` store (re-embed with current model; do not copy vectors across models) | record doc counts in/out |
| AI chatter config | hub-api | `ai-teammates` Producer config | default OFF preserved |
| Welcome config | welcome module | Receptionist config | |
| RCON servers | encrypted creds in module | host secret store via connection framework; never plaintext in bundle tables | re-encrypt, not decrypt-copy to bundle |
| Calendar | #1099 s11 | | |

## 12. Security

- OAuth tokens/refresh tokens/RCON passwords/API keys: host-side connection store only; guests see opaque handles; token refresh in host; never in tool output, logs, or traces.
- Per-community scoping: every table/kv row carries numeric `community_id`; tool calls resolve community from the envelope, never from args; platform channel ids never accepted from caller (reject-destination-args pattern, `tools.rs:35`).
- PII-free logs: log community_id, op, bundle app_id, counts only; never chat text, track-requester names, handles, or prompts.
- Prompt injection: s6 guard; teammates have no model-selected tools; AI outputs never executed.
- Dangerous tools (`*.create/update/delete`, `gameserver.command/kick/ban`, `clip.capture`) -> `risk: dangerous`, admin approval + consent; gameserver commands additionally allowlisted per server policy (carry over `enforcement_service` rules).
- DMCA/ToS: s4; no audio download/re-stream; takedown contact + audit trail for removals.
- Fail loud: unimplemented provider capability returns `unsupported`, never silent no-op.

## 13. PR breakdown (file-ownership disjoint within a wave)

| Wave | PR | Owns (paths) | Depends | Cut |
|---|---|---|---|---|
| 0 | P0 generic bundle-admin/config API in hub-api (if absent, #1099 Q2) | `hub_api/blueprints/v1/bundle_admin*.py`, webui generic view | -- | v3.0 |
| 0 | P0b `ai` service bundle (complete/classify/rag) | `bundles/rust/ai/` | -- | v3.0 |
| 1 (parallel) | P1 `twitch` tools: live.channels + clip.create (schedule in v3.1) | `bundles/rust/twitch/` | -- | v3.0 |
| 1 | P2 `youtube` live.channels | `bundles/rust/youtube/` | -- | v3.0 |
| 1 | P3 `discord` live.channels + scheduled_event.list | `bundles/rust/discord/` | -- | v3.0 |
| 1 | P4 `music-youtube`, `music-spotify` | `bundles/rust/music-*/` | P0 (OAuth gateway reuse) | v3.0 |
| 1 | P4b-H svc-streaming host seam: tool-callable endpoint/token/room API over existing `whip.rs`/`whep.rs`/`rtc/*`, wire `register_fanout` for real pipelines, real-browser WHIP->WHEP smoke | `core/svc_streaming/src/{rtc,ingest/whip.rs,egress/whep.rs,api}` (sole owner) | -- | v3.0 stretch |
| 1 | P4b `webrtc` service bundle minimal (whip/whep endpoint_create/revoke + room.create/close + token issue/revoke) | `bundles/rust/webrtc/` | P4b-H | v3.0 stretch |
| 1 | P5 `calendar` (per #1099) | `bundles/rust/calendar/` | P0, P3 | v3.0 |
| 2 (parallel) | P6 `dj` (static + requests, policy s5 deterministic part) | `bundles/rust/dj/` | P4, P0b | v3.0 |
| 2 | P7 `live-now` | `bundles/rust/live-now/` | P1-P3 | v3.0 |
| 2 | P8 `ai-teammates` Researcher+Receptionist | `bundles/rust/ai-teammates/` | P0b | v3.0 |
| 2 | P9 `clips` (twitch native) | `bundles/rust/clips/` | P1 | v3.0 |
| 3 | M1 data migrations (music, ai knowledge, welcome) | `scripts/migrate/*`, one-off admin job | P6, P8 | v3.0 |
| 3 | D1, D3, D7-partial then D2, D6 (after parity C + soak) | deletions per s10 | wave 2 + M1 | v3.0 end / v3.1 |
| 4 (v3.1) | `music-soundcloud`; `dj` AI + auto-remove; Producer; `twitch.schedule` + `wix.event` + calendar import; `clip-capture` + svc-streaming ring buffer; `webrtc` full (lock/kick/mute/recording/track routing) + `rooms` app; `gameserver`+`game-servers`; `ai.memory`; D2-remainder, D4, D5 | disjoint per bundle dir; svc-streaming PR owns `core/svc_streaming/` alone | wave 3 | v3.1 |

## 14. v3.0 (beta Wed 2026-11-11) vs v3.1 -- honest cut

- Beta is 4 days out and nothing here is built: **realistic v3.0 = spec + wave 0/1 tools only** (generic admin API if missing, `ai` service minimal, live.channels x3, `twitch.clip.create`, calendar+Discord per #1099). dj/music-*/live-now/teammates (wave 2) are a stretch and likely **v3.0.x post-beta**.
- **Do NOT delete anything for beta**: legacy keeps serving until parity C + alpha soak. Deletion PRs land after beta, one domain at a time.
- `webrtc` minimal (whip/whep endpoint mgmt + room token) = v3.0 stretch only, gated on real-browser WHIP/WHEP smoke (P4b-H); `rooms` app, full `webrtc`, module_rtc deletion = v3.1.
- v3.1: everything marked v3.1 above (SoundCloud, AI DJ, Producer, schedule/Wix sync, clip capture, webrtc full + rooms, gameserver, memories).

## 15. Open questions

1. `ai_researcher_module` is ~11.5k LOC with 7 blueprints (build/game/patch/price/tech/event lookup/clip researcher): confirm which lookups are actually used before parity sign-off; any needing arbitrary egress get explicit egress allowlists or are dropped (X).
2. AI DJ teammate inside `dj` (app->app forbidden) vs a shared `ai-teammates` -- OK to keep it in `dj`?
3. Does a host seam exist for svc-presentation (Music Station overlay, live-now SSE) to read bundle state, or must hub-api keep a thin read route?
4. `overlay.py` browser-source/overlay admin: keep in hub-api as host-service admin (this spec's assumption)?
5. Shoutout, inventory, translate, quote, lfg, loyalty modules: out of scope here -- do they need a follow-up catalog pass?
6. Kick platform bundle: API availability/ToS -- include in v3.1 or drop `kick.live.channels`?
7. Clip reels/overlay (`clip_interaction_module` `/reels`, `/overlay`): keep in `clips`, or drop?
8. Radio stations (`radio_player.py` 929 LOC): needed? Re-streaming internet radio has DMCA/ToS exposure.
9. Music OAuth tokens: acceptable to force re-link at cutover instead of migrating tokens?
10. Wix event write scope: are Wix sites calendar targets at all, or import-only (webhook-fed)?
11. Does bundle-owned `data.tables` exist in the host (#1099 Q1)? Determines dj/calendar storage (tables vs kv).
12. Room<->event linkage: data-only UUID link vs a host event-changed notification seam so rooms auto-follow calendar edits?
13. `webrtc` minimal (whip/whep/room+token) in the beta window, or defer wholly to v3.1 given svc_streaming churn (#239/#240)?
14. Keep `webrtc`-rs 0.21.1 (current) vs the str0m named in the streaming-stack note; and who runs the live browser WHIP/WHEP smoke the code comments call unproven?
