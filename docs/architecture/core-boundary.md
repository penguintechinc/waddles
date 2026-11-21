# Core Boundary Inventory (v3.0.x Task 0.2)

The rule applied to every service: **does more than one Module need it? → Core. Exactly
one Module → owned by that Module.** Modules are Bot, Social, Customer, Marketing. Core is
the mandatory platform every Module depends on.

Each row cites a *real consumer* as evidence — a gRPC stub import, an `*_API_URL` client
call, a `docker-compose` env wiring, or a design-doc seed. A service is Core only where two
or more Modules can be named; a single consumer makes it Module-owned, regardless of its
name (`*_core_module` naming is not evidence).

## Core (needed by ≥2 Modules)

| Service | Source | Evidence |
|---------|--------|----------|
| identity | core/identity_core_module | OIDC/JWT/tenant claim — every service authenticates against it |
| security | core/security_core_module | authz enforcement + audit — every Module's permission checks depend on it |
| credentials | core/credential_manager_module | per-tenant secret custody — all platform integrations need it |
| tenancy | core/community_module | communities/membership — every Module scopes queries to `community_id` |
| reputation | core/reputation_module | Bot (router, loyalty) **and** Core security (bad-actor) + analytics (user-stats) |
| analytics | core/analytics_core_module | shared telemetry sink — all Modules emit; Marketing reads it |
| workflow | core/workflow_core_module | cross-Module automation executor — orchestrates every Module over gRPC |
| event bus | **library** split from processing/router_module | consolidated `flask_core.StreamPipeline`; all stages publish/consume (see router split) |
| marketplace | admin/marketplace_module | catalog / subscriptions / App installs — load-bearing for every Module |
| hub | `admin/hub_module` | admin shell that mounts enabled Modules |
| entitlement | new entitlement client — Task 1.5, not yet built | tier × flag gate consulted by every Feature in every Module |

## Module-owned (exactly one Module)

### Bot

All platform plumbing — receivers, actions, interaction commands, and the router's command half.

- **Triggers** `trigger/receiver/*` (8): discord, googlechat, kick_flask, mattermost, slack, teams, twitch, youtube_live (+ `platform_base` shim)
- **Actions** `action/pushing/*` (10): discord, gcp_functions, googlechat, lambda, mattermost, openwhisk, slack, teams, twitch, youtube
- **Interactions** `action/interactive/*` (Bot subset): ai, clip, server_manager, server_status *(deprecated)*
- **From `core/`**: `ai_researcher_module` (standalone AI assistant), `labels_core_module` (only `calendar_interaction` consumes it — fails the 2-Module test despite the `_core_` name), `unified_music_module` (song-request provider library, dormant)
- **Router command half** (processing/router_module): `command_processor.py`, `command_registry.py`, `emote_service.py`, `emote_providers/*`, `context_service.py`, `grpc_clients.py`, `controllers/router.py`, `controllers/admin.py`

### Social

Community-facing social features, chat, presence, conferencing, and stream surfaces.

- **From `core/`**: `browser_source_core_module` (OBS overlay surface), WebRTC/SFU conferencing (the retired Go RTC module, now `svc-streaming` rooms), `video_proxy_module` (RTMP simulcast)
- **Interactions** `action/interactive/*` (Social subset): presence, shoutout, alias, quote, loyalty, inventory, calendar, lfg, memories, spotify, youtube_music, translate

### Marketing

- **From `core/`**: `engagement_module` (forms/polls — only hub consumes; design seeds Marketing)

### Customer

Green-field — nothing exists yet. Built new in P4.

## Flagged judgment calls (record, don't bury)

These were decided on the balance of evidence but are worth revisiting if a second consumer appears:

| Item | Decided | If wrong |
|------|---------|----------|
| The 8 community-facing interactions (loyalty, inventory, calendar, lfg, memories, spotify, youtube_music, translate) | **Social** — community engagement, not platform plumbing | apply the literal "interaction = Bot" default → they flip to Bot |
| `translate` | Social | strongest **Core** candidate — it has a gRPC handler and cross-cuts; promote to Core (Bot + Social) if platform receivers call it to translate inbound chat |
| `labels_core_module` | **Bot** (single consumer) | promote to Core if a Social/Marketing runtime consumer is added |
| `reputation_module` | **Core** | its Social consumer is indirect (via security/analytics), not a direct Social call — still ≥2 Modules, so Core holds |
| `ai_researcher` / `ai` | Bot | either could be Social (community chatbot) or Core (WaddleAI reused) — Bot as the bot's own engine |

## Consequences for later phases

- **Router split (0.3b):** the file-level Core/Bot division above is the input — event-bus/generic-infra files (`cache_manager`, `session_manager`, `rate_limiter`, the `StreamPipeline` wiring) to Core; the command files to Bot.
- **Seven containers (0.5):** Core services land in `svc-core`; Bot/Social/Marketing/Customer code loads into `svc-ingest`/`svc-process`/`svc-action` by pipeline stage, `svc-rtc` for `module_rtc`.
- **Caveat:** `community_module` and `analytics_core_module` are deliberately dual-natured (Core tenancy/telemetry *and* a Social/Marketing consumer surface). They are Core, but their Module-facing APIs are Feature contracts, not internal calls.

## Note on `services/`

The `services/*` aggregators were extraction sources for the seven-container consolidation, not
surviving containers — their 21 dead module subdirs were already deleted in Task 0.1, and the
aggregators themselves are removed at P5. This inventory classifies the *canonical* trees, which
is what survives.
