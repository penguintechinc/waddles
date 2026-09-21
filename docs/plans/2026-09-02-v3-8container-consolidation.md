# v3 Container Consolidation Program Roadmap

**Status:** Committed roadmap. **Scope:** the legacy connector/module → App Bundle port —
the largest remaining body of work in the v3 re-architecture.

**Container count note:** this doc is filed under the initiative's original working name,
"8-container consolidation." Mid-review the persistent-socket design gap (§5) was decided:
a 9th container, `svc-gateway`, is now part of the target. Treat "8-container" in the title/
filename as the historical name of the program, not the current target count — the real
target is **9 containers**.

**Relationship to existing docs — this roadmap does not re-derive the architecture, it
executes a slice of it:**

| Doc | What it owns | This roadmap's relationship |
|---|---|---|
| [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) | Canonical 8-container architecture, pipeline flow, bundle model, SCCEMBS | Authority for container responsibilities; this doc adds the 9th (`svc-gateway`) and does not restate the pipeline/bundle mechanics |
| [`docs/plans/2026-08-31-v3-sccembs-program-plan.md`](2026-08-31-v3-sccembs-program-plan.md) | Master phase table P0–P7 | This roadmap is the detailed execution plan **for P4** ("per-module default bundles... map controllers → default App Bundles per module", `:355`) plus the `svc-gateway` slice of P3/P5 that P4's one-line summary doesn't break out |
| [`k8s/helm/waddlebot/PIPELINE_MAPPING.md`](../../k8s/helm/waddlebot/PIPELINE_MAPPING.md) | Legacy Helm Deployment → new container mapping | Stale (written 2026-08-29, titled "31→7", predates `svc-presentation`/`svc-streaming`/`svc-gateway`). §3 below is the corrected, current mapping; updating `PIPELINE_MAPPING.md` itself is a Wave 6 task |
| [`docs/architecture/core-boundary.md`](../architecture/core-boundary.md) | Module ownership (Core vs. Bot/Social/Marketing) for `core/*` | Source of truth for §3's Core-module target assignments |
| [`docs/plans/2026-08-31-hubapi-node-to-quart-migration.md`](2026-08-31-hubapi-node-to-quart-migration.md) | hub-api Node→Quart port (≈39K LOC, 55 controllers, 9 phases) | External hard dependency (P1, critical path) — referenced in §6/§7, not re-planned here |
| [`docs/plans/2026-08-31-app-bundle-sdk-design.md`](2026-08-31-app-bundle-sdk-design.md) | Bundle authoring spec (`bundle.yaml`, stage contracts) | The format every ported module in §6 must target |

---

## 1. Goal + current vs. target

**Goal:** collapse the legacy v2.2.x "one container per platform/feature" architecture into
the fixed 9-container v3 pipeline, with all connector/module behavior running as **App
Bundles** (or, for the 5 persistent-socket receivers, as supervised tasks inside
`svc-gateway`) — not as standalone Deployments.

**Current state (verified 2026-09-02, `release/v3.0.X`):**

- **52 legacy connector/module source directories** across `trigger/receiver/`,
  `action/pushing/`, `action/interactive/`, `core/*_module`, `processing/router_module`,
  `admin/{hub,marketplace}_module` (full inventory: §2).
- **26 of those are live Helm Deployments today** carrying real traffic
  (`grep -rl "kind: Deployment" k8s/helm/waddlebot/templates/{collectors,core,interactive,pushing}`
  → 26), plus 12 infrastructure template files (managed dependencies — Postgres, Valkey,
  MinIO, Ollama, Qdrant, PostHog — out of scope for this roadmap, per
  `PIPELINE_MAPPING.md:45-51`).
- **9 new pipeline containers already have Helm skeletons** rendering alongside the legacy
  26 (`svc-ingest.yaml`, `svc-process.yaml`, `svc-action.yaml`, `svc-core.yaml`,
  `hub-api.yaml`, `hub-webui.yaml`, `svc-rtc.yaml`, `svc-presentation.yaml`,
  `svc-streaming.yaml` — 42 total `kind: Deployment` templates in the chart today). Nothing
  has been deleted; both topologies coexist (`docs/ARCHITECTURE.md:229-230`,
  `README.md:180-184`).
- **The bundle rails are real** (stage-runners, adapters, distribution service — §4) but
  **only one bundle exists end-to-end**, and it's a demo. Zero of the 52 legacy modules have
  been ported.

**Target state:**

| # | Container | Carries | Status today | Source |
|---|---|---|---|---|
| 1 | `svc-ingest` | Webhook/poll-based platform receivers + generic inbound webhooks; bundles' `ingest` stage | Real runner, poll-only, no receivers feed it yet | `core/svc_ingest/runner.py` |
| 2 | `svc-process` | Event bus, command routing, workflow; bundles' `process` stage | Real runner | `core/svc_process/runner.py` |
| 3 | `svc-action` | Outbound actions/interactions/3rd-party calls; bundles' `action` stage + 5 generic target adapters | Real runner + adapters, **zero bundles** | `core/svc_action/runner.py`, `libs/waddle_transports/waddle_transports/transports/` |
| 4 | `svc-core` | Identity, security, credentials, entitlement — synchronous gRPC | **Does not exist as app code** — Helm skeleton only (`svc-core.yaml:37-41`), no core/svc_core/ directory | `docs/ARCHITECTURE.md:225-228` |
| 5 | `hub-api` | Admin, tenancy, marketplace, billing, AI routing, distribution — control plane | Real `app.py`, many services incl. `distribution_service.py`; bulk of the 55-controller admin/marketplace surface still Node (separate P1 port, ≈39K LOC) | `hub_api/app.py`, `docs/plans/2026-08-31-hubapi-node-to-quart-migration.md` |
| 6 | `hub-webui` | SPA static-serve + `/api` proxy | Only Node container; real | `admin/hub_module/Dockerfile.webui` (legacy source) |
| 7 | `svc-presentation` | Core overlays + Music Station + bundles' `presentation` component | Real Dockerfile, newest container (2026-08-31) | `core/svc_presentation/` |
| 8 | `svc-streaming` | RTC + HLS/RTMP/AV1 control plane, fronts external LiveKit/MarchProxy | Real Dockerfile, newest (2026-09-01); `svc-rtc` absorption not yet done | `core/svc_streaming/`, `docs/plans/2026-08-31-svc-streaming-design.md` |
| 9 | **`svc-gateway`** (new, decided this doc) | Persistent inbound sockets — Discord Gateway, Slack Socket Mode, Twitch IRC, Kick Pusher WS, Mattermost WS — supervised long-lived tasks, fans events onto per-bundle `:ingest` Valkey keys via `resolve_apps` | **Does not exist yet** — net-new work, this roadmap's Wave 0–1 | §5 |

`svc-rtc` (Go/LiveKit, `core/module_rtc`) remains a 10th, standalone container — its planned
absorption into `svc-streaming` is tracked separately (`docs/ARCHITECTURE.md:41-43`) and is
not part of this roadmap's 9-container target.

---

## 2. Full inventory (verified against `release/v3.0.X`, 2026-09-02)

| Category | Count | Path pattern | Modules (verified `ls`) |
|---|---|---|---|
| Receivers | **8** | `trigger/receiver/*` | `discord_module`, `googlechat_module`, `kick_module_flask`, `mattermost_module`, `slack_module`, `teams_module`, `twitch_module`, `youtube_live_module` (+ `platform_base.py`, a shared shim, not a container) |
| Pushing (outbound) | **10** | `action/pushing/*_action_module` | `discord`, `gcp_functions`, `googlechat`, `lambda`, `mattermost`, `openwhisk`, `slack`, `teams`, `twitch`, `youtube` |
| Interactive | **17** | `action/interactive/*` | `ai_interaction_module`, `alias_interaction_module`, `calendar_interaction_module`, `clip_interaction_module`, `inventory_interaction_module`, `lfg_interaction_module`, `loyalty_interaction_module`, `memories_interaction_module`, `presence_module`, `quote_interaction_module`, `server_manager_interaction_module`, `server_status_interaction_module` (deprecated), `shoutout_interaction_module`, `spotify_interaction_module`, `translate_interaction_module`, `welcome_interaction_module`, `youtube_music_interaction_module` |
| Core modules | **13** (`*_module` glob) **+ 1** (`module_rtc`, different naming, doesn't match the glob) = **14** | `core/*_module`, `core/module_rtc` | `ai_researcher_module`, `analytics_core_module`, `browser_source_core_module`, `community_module`, `credential_manager_module`, `engagement_module`, `identity_core_module`, `labels_core_module`, `reputation_module`, `security_core_module`, `unified_music_module`, `video_proxy_module`, `workflow_core_module`, `module_rtc` |
| Router | **1** | `processing/router_module` | event bus + command dispatch |
| Admin | **2** | `admin/*_module` | `hub_module` (incl. `Dockerfile.webui`), `marketplace_module` |
| **Total legacy source dirs** | **52** (53 incl. `module_rtc`) | — | 8+10+17+14+1+2 |
| Infra (out of scope) | 12 template files (5 top-level + 7 PostHog subcomponents) | `k8s/helm/waddlebot/templates/infrastructure/` | `minio`, `ollama`, `postgres`, `qdrant`, `redis` + PostHog `{clickhouse,kafka,postgres,redis,web,worker,secret}` — managed dependencies, not connector/module code |

**Reconciling against the "~45" estimate that framed this work:** no document or code
comment in the repo states "45 containers" (grepped `docs/`, `README.md`, `CLAUDE.md`) — it
was this initiative's own working estimate. The two real denominators are **26** (legacy
Deployments live in Helm today) and **52** (legacy source directories, most of which fold
1:many into a single Deployment already — e.g. `pushing/action-platforms.yaml` alone serves 5
of the 10 pushing modules). Neither is "45." This roadmap uses the verified 52-directory /
26-Deployment split throughout and does not force a reconciliation to the original estimate.

`action/interactive` has 17 confirmed directories, but `docs/architecture/core-boundary.md:34-45`
only enumerates 16 (4 Bot + 12 Social) — it's missing `welcome_interaction_module`. Flagged
for a Wave 4 ownership call (§6), not resolved here.

---

## 3. Current → 9-container mapping

"Becomes" column: **Bundle** = ported as a `bundle.yaml` + stage scripts, installed/activated
through the marketplace; **Fold** = absorbed as generic platform code in the target
container, never a marketplace-visible bundle; **Gateway task** = a supervised persistent-
connection task inside `svc-gateway`, not a pipeline-stage bundle at all.

### → `svc-gateway` (new, 5 modules — persistent sockets, confirmed by code)

| Legacy module | Evidence of persistent connection | Becomes |
|---|---|---|
| `trigger/receiver/discord_module` | `services/discord_bot.py:49` — `discord.Bot(intents=intents)`, a live Gateway client | Gateway task |
| `trigger/receiver/slack_module` (socket-mode path only) | `app.py:73-74` — `if Config.USE_SOCKET_MODE: asyncio.create_task(slack_bolt.start_socket_mode())` | Gateway task |
| `trigger/receiver/twitch_module` (IRC path only) | `app.py` docstring: "Quart Application with TwitchIO IRC Bot"; `"Twitch IRC bot started"` | Gateway task |
| `trigger/receiver/kick_module_flask` | `services/chat_client.py:3,23` — "Real-time chat integration using Pusher WebSocket... KICK uses Pusher for its WebSocket-based chat system" | Gateway task |
| `trigger/receiver/mattermost_module` | `services/mattermost_bot.py:56-94` — `_websocket_listener()`, `asyncio.create_task(self._websocket_listener())` | Gateway task |

### → `svc-ingest` (3 receivers + generic webhook handling — confirmed webhook/poll-based)

| Legacy module | Evidence | Becomes |
|---|---|---|
| `trigger/receiver/googlechat_module` | `app.py` — Quart `Blueprint`/`request`-based webhook handler, no socket/gateway pattern found | Bundle (`ingest`) |
| `trigger/receiver/teams_module` | `app.py:107-130` — "Teams Bot Framework webhook" | Bundle (`ingest`) |
| `trigger/receiver/youtube_live_module` | `app.py:6,58` — `ChatPoller` (interval poll) + `WebhookHandler` (PubSubHubbub) | Bundle (`ingest`) |
| `pushing/trigger-webhooks.yaml` (generic inbound webhooks, `PIPELINE_MAPPING.md:23`) | Already mapped by existing skeleton work | Fold (generic webhook receipt) |
| `pushing/trigger-streaming.yaml` (`PIPELINE_MAPPING.md:24`) | Already mapped | Fold |
| `trigger/receiver/twitch_module` (EventSub webhook path, if used instead of/alongside IRC) | Open sub-decision — see §6 W0 | Bundle or fold, TBD |

### → `svc-action` (10 pushing + 17 interactive = 27 modules)

| Legacy module | Becomes |
|---|---|
| `action/pushing/{discord,gcp_functions,googlechat,lambda,mattermost,openwhisk,slack,teams,twitch,youtube}_action_module` (10) | Bundle (`action`, `target_type` per platform) |
| `action/interactive/ai_interaction_module` | Bundle |
| `action/interactive/alias_interaction_module` | Bundle |
| `action/interactive/calendar_interaction_module` | Bundle |
| `action/interactive/clip_interaction_module` | Bundle |
| `action/interactive/inventory_interaction_module` | Bundle |
| `action/interactive/lfg_interaction_module` | Bundle |
| `action/interactive/loyalty_interaction_module` | Bundle |
| `action/interactive/memories_interaction_module` | Bundle |
| `action/interactive/presence_module` | Bundle |
| `action/interactive/quote_interaction_module` | Bundle |
| `action/interactive/server_manager_interaction_module` | Bundle |
| `action/interactive/server_status_interaction_module` | **Deprecated** — candidate for drop, not port (confirm in Wave 4) |
| `action/interactive/shoutout_interaction_module` | Bundle |
| `action/interactive/spotify_interaction_module` | Bundle |
| `action/interactive/translate_interaction_module` | Bundle |
| `action/interactive/welcome_interaction_module` | Bundle |
| `action/interactive/youtube_music_interaction_module` | Bundle |
| `core/ai_researcher_module` (standalone AI assistant, Bot-owned per `core-boundary.md:37`) | Bundle |
| `core/labels_core_module` (Bot-owned, single consumer per `core-boundary.md:37,63`) | Bundle |
| `processing/router_module` — command-dispatch half (`command_processor.py`, `command_registry.py`, `emote_service.py`, `controllers/router.py`, `controllers/admin.py`; `core-boundary.md:38`) | Fold |

### → `svc-core` (7 modules — none exist as `svc-core` app code yet, §4/§6 W0)

| Legacy module | Becomes |
|---|---|
| `core/identity_core_module` | Fold |
| `core/security_core_module` | Fold |
| `core/credential_manager_module` | Fold |
| `core/community_module` (tenancy — the community entity) | Fold |
| `core/reputation_module` | Fold |
| `core/analytics_core_module` | Fold |
| `core/workflow_core_module` | Fold |
| `processing/router_module` — event-bus half (`cache_manager`, `session_manager`, `rate_limiter`, `StreamPipeline` wiring; `core-boundary.md:69`) | Fold (already partly extracted as `flask_core.stream_pipeline` library) |

### → `hub-api` (2 admin modules + 1 core module)

| Legacy module | Becomes |
|---|---|
| `admin/hub_module` | Fold — part of the separately-tracked 39K-LOC Node→Quart P1 port |
| `admin/marketplace_module` | Fold — same P1 port |
| `core/engagement_module` (forms/polls, Marketing-seeded, hub-only consumer; `core-boundary.md:49`) | Fold |

### → `svc-presentation` (1 module)

| Legacy module | Becomes |
|---|---|
| `core/browser_source_core_module` (OBS overlay surface) | Fold |

### → `svc-streaming` / `svc-rtc` (2 modules, absorption target)

| Legacy module | Becomes |
|---|---|
| `core/video_proxy_module` (RTMP simulcast, fronts external MarchProxy) | Fold |
| `core/module_rtc` (Go WebRTC/SFU, fronts external LiveKit) | Stays `svc-rtc` short-term; folds into `svc-streaming` on its own absorption timeline (`docs/plans/2026-08-31-svc-streaming-design.md`) — not this roadmap's dependency |

### Undetermined (1 module)

| Legacy module | Note |
|---|---|
| `core/unified_music_module` (song-request provider library, described as "dormant" in `core-boundary.md:37`) | Likely folds into Music Station / `svc-presentation` per program-plan P5, but P5's own scope for it isn't finalized (`docs/plans/2026-08-31-v3-sccembs-program-plan.md:356,420-438` — Music Station has 12 open decisions). Out of this roadmap's critical path; revisit at Wave 6. |

**Tally check:** 5 (svc-gateway) + 6 (svc-ingest: 3 receivers + 2 folds + 1 open) + 27
(svc-action: 10 pushing + 17 interactive) + 2 (svc-action: ai_researcher, labels) + 1
(svc-action: router command-half) + 7 (svc-core) + 1 (svc-core: router event-half) + 3
(hub-api) + 1 (svc-presentation) + 2 (svc-streaming/svc-rtc) + 1 (undetermined) = 53 rows
(counts `router_module` twice — once per split half — and `module_rtc` once), covering all 52
directories from §2 plus `module_rtc`.

---

## 4. The scaffold-only gap

The bundle **rails** are real and complete. The bundle **inventory** is almost entirely empty.

**What's real (verified, path:line):**

- `libs/flask_core/flask_core/stage_runner.py:1-212` — `BundlePoller` (polls hub-api's
  distribution endpoint, exponential backoff, degrades to last-known bundle set) and
  `load_entrypoint` (resolves a bundle's `module:function` via `importlib`, never `exec()`).
- `core/svc_ingest/runner.py:39-138` (`IngestRunner`), `core/svc_process/runner.py:30-125`
  (`ProcessRunner`), `core/svc_action/runner.py:49-289` (`ActionRunner`) — all three
  are working poll/RPOP/LPUSH loops with retry and audit logging, not stubs.
- `libs/waddle_transports/waddle_transports/transports/` — 5 generic target adapters
  (`http.py`, `message_queue.py`, `irc.py`, `overlay.py`, `email.py`), resolved by
  `transport_type` via `waddle_transports/registry.py:25-65`'s `get_transport()`. None
  reference Discord/Slack/Twitch — they're HTTP/SMTP/Redis-shaped, not platform-specific.
- `libs/flask_core/flask_core/app_binding.py:1-60` — resolves which App is bound to a
  `(feature, tenant, community)` slot; `resolve_apps()` returns the full coexistence set per
  the App Bundle SDK spec §5.2/§7.
- `hub_api/services/distribution_service.py:1-50+` — backs
  `GET /api/v1/distribution/bundles`, joins `app_catalog`/`app_activations`/
  `app_tenant_availability`, filtered by stage.

**What's not real:**

- **Exactly one end-to-end bundle exists**, and it's a demo:
  `config/postgres/migrations/071_app_catalog_stages.sql` seeds `app_id =
  waddles.core.demo.echo` with `ingest`/`process` entrypoints
  (`core/svc_ingest/bundles/echo_ingest.py`, `core/svc_process/bundles/echo_process.py`) —
  no `action` stage. Grepped every migration for `entrypoint`: 071 is the only one.
- **`svc-action` ships zero bundles** — no `core/svc_action/bundles/` directory exists at all
  (unlike `svc_ingest`/`svc_process`, which each have one).
- **The Bot module's "default Apps" are metadata-only manifests.**
  `libs/bot_module/features.py:107-152` defines 4 dicts (shoutout, commands, connectors,
  interactions) — each just `{app_id, name, version, feature, module, provider, surfaces,
  permissions, is_default}`. No `entrypoint`, no `stages` block, nothing executable.
- **The bundle 3-tier tables don't exist yet.** `app_catalog` / `app_tenant_availability` /
  `app_activations` are referenced only in docstrings/tests — the legacy SQL migration head is
  `068_add_welcomed_users.sql`, and program-plan P2 (which creates these tables) is a hard
  dependency this roadmap doesn't control (`docs/plans/2026-08-31-v3-sccembs-program-plan.md:364-368`).
- One in-progress, uncommitted worktree (`feature/v3-bundle-discord-action`,
  `.worktrees/proof-discord-bundle`) adds a 6th generic adapter (`bundle.py`) that dispatches
  to a bundle-declared script entrypoint — still a generic rail, not a Discord connector port,
  and not merged. It doesn't change the "one demo bundle" count.

**What "build the runtime for real" means:** every one of the 53 rows in §3 marked "Bundle"
needs a `bundle.yaml` + one script per stage it implements, written against the App Bundle
SDK spec, replacing its legacy Flask/Quart service entirely. Every row marked "Fold" needs its
business logic moved (not rewritten from scratch — `docs/plans/2026-08-26-v3-scbm-apps-design.md:57-83`'s
"move code, do not retype it" directive) into the target container as generic platform code.
Rows marked "Gateway task" need a `ReceiverSupervisor`-pattern wrapper (§5) instead of either.

**Scale:** `find trigger action processing core/*_module -name '*.py'` (13 core dirs, all
`action`/`trigger`/`processing` dirs) totals **139,031 LOC**, of which **126,471 is non-test
application code** (12,560 test LOC). This is the order of magnitude to port — see §7 for how
it compares to the hub-api Node→Quart effort.

---

## 5. Socket-ingest design gap — DECIDED

**The gap (as found before this decision):** `core/svc_ingest/runner.py` is strictly
poll-based (`run_forever()`, lines 48-53: `await self.run_once()` then
`await asyncio.sleep(self._poller.next_delay_s)`), and its own docstring
(`runner.py:9-11`) admits the punt: *"ingest RPOPs raw inbound events off its OWN `:ingest`
key (populated by whatever external receiver/webhook — out of scope for this PR)."* Five
legacy receivers hold genuinely persistent connections (Discord Gateway, Slack Socket Mode,
Twitch IRC, Kick Pusher WebSocket, Mattermost WebSocket — evidence in §3) that have no home in
a poll loop. No design doc or code comment anywhere in the repo proposed a resolution before
this one.

**Decision:** a 9th container, **`svc-gateway`**, owns every persistent inbound socket. It
runs each of the 5 confirmed sockets as a supervised, long-lived `ReceiverSupervisor` task
(reconnect-with-backoff on drop, health/readiness reflecting live connection state) and fans
normalized raw events onto the same per-bundle `:ingest` Valkey keys `svc-ingest` already
reads from, resolved through `resolve_apps` exactly like a poll-sourced event would be. From
`svc-process` downstream, an event's origin (gateway push vs. ingest poll) is invisible — the
isolation-key scheme (`docs/ARCHITECTURE.md:66-76`) and stage contract are unchanged.

**Receivers moving to `svc-gateway`:** `discord_module`, `slack_module` (socket-mode path),
`twitch_module` (IRC path), `kick_module_flask`, `mattermost_module`.

**Receivers staying in `svc-ingest`:** `googlechat_module`, `teams_module`,
`youtube_live_module` — all confirmed webhook/poll-based, no persistent-connection code found.

**Open sub-decisions for Wave 0** (not blocking the top-level design, but need answers before
Wave 1 starts):

1. **Slack dual-mode split.** `slack_module/app.py:73-74` gates Socket Mode behind
   `Config.USE_SOCKET_MODE` — implying a webhook (Events API) fallback path exists too. Does
   `svc-gateway` own Slack unconditionally, or only when a tenant is configured for socket
   mode, with `svc-ingest` handling the webhook-mode tenants?
2. **Twitch IRC vs. EventSub split.** `twitch_module` runs both an IRC bot (persistent) and
   EventSub (webhook-capable). Confirm whether EventSub already runs webhook-only in the
   legacy module, or whether it's currently also routed over the persistent connection —
   determines whether Twitch needs is a clean owner or a two-path porting job (like Slack).
3. **`svc-gateway` container skeleton** doesn't exist yet — no Helm template, no Dockerfile,
   no `runner.py`. It needs to be built to the same shape as `core/svc_ingest`/`svc_process`/
   `svc_action` before Wave 1 can start.

---

## 6. Phased waves

Waves are dependency-ordered. Each wave's module list is sized for parallel fan-out — most
waves list ≤17 modules so work can be dispatched roughly 10-at-a-time to parallel agents/PRs.

### Wave 0 — Foundations (sequential, not fan-out-able)

Blocking infrastructure that every later wave depends on. This is architecture and shared
library work, not per-module porting — one team, in order.

- Build the `svc-gateway` container skeleton (Helm template, Dockerfile, `runner.py`
  entrypoint, health/readiness reflecting live socket state) — mirror the shape of
  `core/svc_ingest`/`svc_process`/`svc_action`.
- Write the `ReceiverSupervisor` pattern as a shared `flask_core` primitive (reconnect/backoff,
  structured disconnect logging, credential rotation without a restart).
- Resolve the 3 open sub-decisions in §5 (Slack split, Twitch split, confirm no 6th
  persistent-socket receiver was missed).
- Land the bundle 3-tier tables (`app_catalog`/`app_tenant_availability`/
  `app_activations`) — this is program-plan P2, an external dependency this roadmap does not
  own but cannot start Wave 3+ without. **Flag to the P2 owner as a hard blocker.**
- Start `svc-core` real app code (core/svc_core/ doesn't exist yet) — at minimum a working
  gRPC skeleton on port 50203 that Wave 5's identity/security/credential folds can land into.

**Dependencies:** none (this is the root of the tree). **Risks:** if P2 (3-tier tables) slips,
every bundle-becomes row in every later wave is blocked on install/activate — this is the
single biggest external risk to the whole roadmap.

### Wave 1 — `svc-gateway`: persistent-socket receivers (5 modules)

`discord_module`, `slack_module` (socket-mode path), `twitch_module` (IRC path),
`kick_module_flask`, `mattermost_module`.

**Dependencies:** Wave 0 (`svc-gateway` skeleton + `ReceiverSupervisor` primitive + Slack/
Twitch split decisions). **Risks:** these are the modules holding live user-facing chat
connections — a bad port causes visible chat outages, not just an internal pipeline stall;
needs a real rollback plan per module, not just a revert PR.

### Wave 2 — `svc-ingest`: webhook/poll receivers + generic webhooks (5 items)

`googlechat_module`, `teams_module`, `youtube_live_module`, generic inbound webhook handling
(`pushing/trigger-webhooks.yaml`, `pushing/trigger-streaming.yaml`), Twitch EventSub webhook
path (pending the Wave 0 IRC/EventSub split decision).

**Dependencies:** Wave 0 (3-tier tables for bundle activation). Independent of Wave 1.
**Risks:** lowest risk in the program — these are already request/response, closest in shape
to what `svc-ingest`'s runner already does.

### Wave 3 — `svc-action`: pushing connectors (10 modules)

`discord`, `gcp_functions`, `googlechat`, `lambda`, `mattermost`, `openwhisk`, `slack`,
`teams`, `twitch`, `youtube` (all `action/pushing/*_action_module`).

**Dependencies:** Wave 0 (3-tier tables); independent of Waves 1-2 — can run in parallel.
**Risks:** outbound sends are user-visible (a dropped Discord/Slack message is noticed
immediately); needs the same per-adapter rollback discipline as Wave 1. `gcp_functions`/
`lambda`/`openwhisk` outbound targets may need new `target_type` adapters beyond the existing
5 generic ones — confirm during porting, don't assume `webhook`/`rest_api` cover them.

### Wave 4 — `svc-action`: interactive/interaction connectors (17 modules)

`ai_interaction_module`, `alias_interaction_module`, `calendar_interaction_module`,
`clip_interaction_module`, `inventory_interaction_module`, `lfg_interaction_module`,
`loyalty_interaction_module`, `memories_interaction_module`, `presence_module`,
`quote_interaction_module`, `server_manager_interaction_module`,
`server_status_interaction_module` (confirm deprecation → drop vs. port),
`shoutout_interaction_module`, `spotify_interaction_module`, `translate_interaction_module`,
`welcome_interaction_module`, `youtube_music_interaction_module`.

Plus 2 core modules folding into `svc-action` alongside this wave (same target container,
same PR-review context): `ai_researcher_module`, `labels_core_module`.

**Dependencies:** Wave 0; can overlap with Wave 3 (same target container, different bundle
IDs, no shared state). **Risks:** largest module count of any wave (19) — the one most worth
actually fanning out 10-at-a-time; `welcome_interaction_module`'s ownership isn't recorded in
`core-boundary.md` (§2) — resolve Bot vs. Social before assigning, since Feature-flag naming
depends on it.

### Wave 5 — `svc-core`: identity/security/tenancy/platform (7 modules + router event-half)

`identity_core_module`, `security_core_module`, `credential_manager_module`,
`community_module`, `reputation_module`, `analytics_core_module`, `workflow_core_module`,
plus `processing/router_module`'s event-bus half (`cache_manager`, `session_manager`,
`rate_limiter`, `StreamPipeline` wiring).

**Dependencies:** Wave 0's `svc-core` gRPC skeleton. This is the **highest-risk wave in the
program**: every other stage calls `svc-core` synchronously for auth/entitlement
(`docs/ARCHITECTURE.md:37-39`) — a bug here doesn't fail one bundle, it fails every request in
every other container. Recommend this wave lands behind a strict canary (dual-write/shadow-
read against the legacy 3 modules before cutover), not a direct swap.
**Risks:** identity/security/credentials are explicitly the security-sensitive tier
(`general.md` Language Selection) — Rust or Python3 only, extra scrutiny on auth-decision
code, no shortcuts on scope/tenant checks during the port.

### Wave 6 — Presentation/streaming stragglers, hub-api folds, and cutover

- `browser_source_core_module` → `svc-presentation`.
- `video_proxy_module` → `svc-streaming`; `module_rtc` stays `svc-rtc` (absorption is a
  separate, later-timed decision per the streaming design doc, not blocking this roadmap).
- `engagement_module` → `hub-api` (folds alongside the already-tracked P1 Node→Quart port).
- `unified_music_module` → resolve its Music Station placement (currently undetermined, §3) or
  explicitly defer past this roadmap.
- **Cutover:** delete the 26 legacy Helm Deployments (`collectors/`, `core/`, `interactive/`,
  `pushing/` templates) and the 52 legacy source directories; repoint Services/Ingress/
  HTTPRoute/NetworkPolicy at the 9 new containers; update `PIPELINE_MAPPING.md` from its
  current stale "31→7" to a real "31→9" (or delete it in favor of this doc, since coverage
  overlaps once cutover is done).
- **Docs gate (program-plan P7):** README/`docs/ARCHITECTURE.md`/`docs/index.md` already
  describe the 9-container end state accurately as of this doc's writing — verify they still
  match after cutover, don't re-litigate them.

**Dependencies:** Waves 1-5 fully landed (cutover can't delete a legacy Deployment whose
replacement bundle isn't live). **Risks:** this is where "v3 = major version, clean cutover,
no dual-topology compat" (per this roadmap's mandate) actually executes — there is no partial-
rollback story once the 26 legacy Deployments are deleted; each of Waves 1-5 needs its own
sign-off before Wave 6 touches that wave's Deployments, rather than one big-bang deletion at
the end.

---

## 7. Effort estimate

**Scale of this roadmap's port:** 126,471 non-test LOC (139,031 incl. tests) across 52 legacy
source directories, plus net-new work for `svc-gateway` and `svc-core` (neither has existing
Python to port from wholesale — `svc-core`'s 3 legacy modules get folded, but `svc-gateway`'s
`ReceiverSupervisor` pattern is new).

**Comparison point already in the repo:** the hub-api Node→Quart migration
(`docs/plans/2026-08-31-hubapi-node-to-quart-migration.md`) ports ≈39,000 LOC across 55
controllers, and that program is scoped as **9 phases** with per-group parity gates
(`:116,195,208`) specifically because "a single cutover has no incremental verification and an
all-or-nothing rollback."

This roadmap's port is **≈3.2–3.6× the LOC** of the hub-api migration (126K–139K vs. 39K), and
touches **52 independent modules** vs. hub-api's 55 controllers inside one codebase — more
directories, more independent Dockerfiles/CI configs/test suites to retire, but also more
naturally parallelizable (each module is already an independent service today, so waves can
fan out across genuinely disjoint codebases rather than one shared file tree). Applying the
same "move code, do not retype it" discipline the SCBM apps design doc mandates
(`2026-08-26-v3-scbm-apps-design.md:57-83`) — port business logic via `git mv`/copy + edit,
not a rewrite — is what makes the 3-6x LOC ratio tractable rather than a 3-6x rewrite of
hub-api's own effort.

**Do not read the wave count (7, W0-W6) as a time estimate equal to hub-api's 9 phases** — the
two are different shapes of work (one wide codebase vs. many independent modules) and this
roadmap does not have enough information yet (no per-module complexity audit has been done) to
convert LOC or module count into a calendar estimate. That audit is itself a Wave 0 deliverable
if the program needs a calendar date; this roadmap deliberately stops at wave-level scope +
dependency + risk, per its mandate.

---

## 8. Definition of done

- **9 containers** deployed and serving all live traffic: `svc-gateway`, `svc-ingest`,
  `svc-process`, `svc-action`, `svc-core`, `hub-api`, `hub-webui`, `svc-presentation`,
  `svc-streaming` (`svc-rtc` may remain a 10th standalone container per its own absorption
  timeline — not blocking).
- **All 52 legacy source directories deleted** — `trigger/receiver/*`, `action/pushing/*`,
  `action/interactive/*`, `core/*_module` (excl. the 9 new `svc_*` dirs), `processing/router_module`,
  `admin/{hub,marketplace}_module` — no dual-topology compatibility layer retained (this is a
  major-version, clean-cutover program).
- **All 26 legacy Helm Deployments removed** from `k8s/helm/waddlebot/templates/`
  (`collectors/`, `core/`, `interactive/`, `pushing/`); `PIPELINE_MAPPING.md` updated to
  reflect the real, final mapping (or retired in favor of this doc).
- **Every "Bundle" row in §3 is a real, installable `app_catalog` entry** with working
  `bundle.yaml` + per-stage scripts against the App Bundle SDK spec — not a metadata-only
  manifest like today's `libs/bot_module/features.py:107-152` default Apps.
- **Every "Fold" row in §3 has its business logic running inside its target container** —
  `svc-core` in particular must be a real, load-bearing gRPC service, not the current Helm
  skeleton with no backing code.
- **`svc-action` ships more than zero bundles** (today: zero).
- **`svc-gateway` exists and owns all 5 persistent-socket connections**, with no receiver
  still running as a standalone legacy Deployment.
- Docs (`README.md`, `docs/ARCHITECTURE.md`, `docs/index.md`) verified accurate against the
  post-cutover state — they already describe the target correctly as of this doc's writing,
  so this is a verification pass, not a rewrite.
