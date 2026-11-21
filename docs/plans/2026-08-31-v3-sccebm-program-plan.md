# v3 SCCEBM — Master Program Plan & Phased Roadmap

> **Historical document.** Written against the repository layout at the time; some paths named below (shown as plain text, not links) have since moved, been renamed or been removed. Not maintained as current architecture.

**Date:** 2026-08-31
**Status:** Authoritative program roadmap — the team executes from this. **Consolidates**, does
not re-derive. Design rationale lives in the specs cited below; this doc sequences the work.

## Sources this consolidates

| Doc | Role here |
|---|---|
| `.PLAN` (476 lines, gitignored) | Captured design session — module taxonomy (SCCEBM), containers, media, token billing, premium AI, API shape, Node→Python directive, hub-api reality check |
| `docs/plans/2026-08-26-v3-scbm-apps-design.md` | Base v3 architecture — Core/Module/Feature/App, 7→8 containers, streams, tenancy, SPIFFE, gating, tier mapping, P0–P5 |
| `docs/plans/2026-08-31-app-bundle-sdk-design.md` | App Bundle SDK — manifest, per-stage contract, 3-tier lifecycle, coexistence/conflict, isolation, distribution (C1–C12) |
| `docs/plans/2026-08-31-music-station-design.md` | Per-community Music Station — normalized Track, queue, policy/moderation, presentation container |
| `docs/plans/2026-08-31-hubapi-node-to-quart-migration.md` | (parallel) hub-api 44-controller Node→Quart migration — the critical path (§8) |

**Naming:** the module set is now **SCCEBM** (7 product modules), superseding SCBM (4). The
platform is a Core + App-Bundle marketplace on an ingest→process→action→presentation pipeline,
packaged as 8 containers.

---

## 1. Architecture overview

### 1.1 SCCEBM — 7 product modules + Core

Product modules are **groupings of default App Bundles**, each globally toggleable (Helm) and
tier-gated. Core is the non-toggleable platform every module needs.

| # | Module | Scope | Notes |
|---|---|---|---|
| S | **Socials** | External social-media mgmt — posts/responses/analytics for LinkedIn, X, Facebook, Instagram, TikTok | *External*; split out of old "Social" |
| C | **Customers** | CRM — accounts, contacts, opportunities, pipelines, cases | ~100% greenfield |
| C | **Community** | *Management/engagement* — forum, chat, virtual stages, polls, announcements | The community **entity** is NOT here — it is Core (see below) |
| E | **Event** | Event + conference management | New |
| M | **Marketing** | Campaigns, shortlinking, emailing | ~70% seeded |
| B | **Bot** | Multi-platform bot — shoutout, commands, connectors, interactions | ~0% greenfield (wraps v2.2.x) |
| S | **Streaming** | `svc-streaming` container + hub-api control-plane (stream/calls/overlay/music) | Gazer-derived; Go→Rust |

**Core (non-module, always-on, still tier-gated where applicable):**

- **Community entity** = teams / OUs — the OU-level unit in the `global → tenant → community`
  scope ladder. Entity CRUD/membership/hierarchy is Core infra, **not** the Community module.
  (`.PLAN:451-456` — this correction is load-bearing: "Community" the module ≠ "communities" the
  entity.)
- auth/identity, tenancy, marketplace, token/billing, analytics, privacy/compliance,
  entitlement client, event bus (`flask_core.stream_pipeline`).

`KNOWN_MODULES` migrates from today's `{bot, social, marketing, customer, analytics, video_proxy,
auth, compliance, integrations, tenancy, core}` (`app_manifest.py`) to the SCCEBM set
`{socials, customers, community, event, marketing, bot, streaming}` + core/platform namespaces
(§9 P4). This is a breaking taxonomy change — feature-key namespaces, license catalog, and docs
all follow.

### 1.2 The 8 containers

`svc-*` = pipeline/service containers; `hub-*` = hub (`.PLAN:401-405`). Marketplace folds INTO
hub-api (not a container).

```
 inbound                                                            outbound
   │                                                                   ▲
   ▼        ┌──────────────┐   ┌──────────────┐   ┌──────────────┐    │
 svc-ingest │  Valkey str  │ svc-process │ Valkey │  svc-action  │────┘
   │  ───▶  └──────────────┘ ───▶         ───▶    └──────┬───────┘
   │                                                     │ overlay target
   │                                              ┌──────▼────────┐
   │                                              │svc-presentation│ overlays + music station
   │                                              └───────────────┘   (browser sources / OBS)
   │        ┌──────────────┐
   └───────▶│ svc-streaming │ RTC + HLS/RTMP/AV1 record/forward/transcode
            └──────────────┘

  svc-core   identity · security · credentials · entitlement (every stage depends on it)
  hub-api    admin + tenancy + marketplace module + MCP  (Python/Quart, control plane)
  hub-webui  Python/Quart backend serving the ReactJS webui
```

| Container | Carries | Language |
|---|---|---|
| `svc-ingest` | platform receivers + inbound webhooks; loads activated bundles' `ingest` component | RustLang |
| `svc-process` | event bus, routing, command dispatch, workflow; bundles' `process` component | RustLang |
| `svc-action` | outbound actions, interactions, 3rd-party calls, target adapters; bundles' `action` component | RustLang |
| `svc-core` | identity/security/credentials/entitlement (gRPC :50051) | RustLang |
| `svc-presentation` | core overlays (full_screen/media/crawler) + music station + bundles' `presentation` component | RustLang |
| `svc-streaming` | RTC + HLS/RTMP/AV1 record/forward/transcode (absorbs svc-rtc + video_proxy) | RustLang |
| `hub-api` | admin/tenancy control plane + **marketplace module** + gRPC + REST + **MCP** | Python/Quart |
| `hub-webui` | SPA assets, static-serve + `/api` proxy for ReactJS webui | ExpressScript + ReactJS |

Container topology is a **packaging** decision, decoupled from the logical Core→Module→Feature→App
model (`2026-08-26-v3-scbm-apps-design.md:486-490`). Modules cut across stages vertically; the
pipeline is horizontal. "Module off" = its bundles are not loaded in any stage — no pod
disappears.

**Build status today (verified on `release/v3.0.X`):** 6 of 8 have Helm charts in
`k8s/helm/waddlebot/templates/` — `svc-ingest/process/action/core` + `hub-api` are **skeleton
placeholders** pinned to the shared `pipeline.pythonBaseImage` (no per-service Dockerfile/CI image
yet); only **`hub-webui`** has a real image (`admin/hub_module/Dockerfile.webui`).
**`svc-presentation` and `svc-streaming` do not exist anywhere yet** (planned, §9 P5); a legacy
`svc-rtc.yaml` (Go, `goRuntimeImage`) chart is what exists today and is absorbed into
`svc-streaming`.

---

## 2. App bundle model

Cross-ref: **`docs/plans/2026-08-31-app-bundle-sdk-design.md`** (authoritative). Summary of the
load-bearing model:

- **A bundle IS the App** — `manifest (bundle.yaml)` + per-stage `{config, spec, script}`. A
  bundle may implement 1–4 stages, omitting directories it doesn't touch (SDK §1-2).
- **4 component types**: `ingest → process → action → presentation`. The first three are async
  event scripts over Valkey streams; **presentation** is HTML/JS overlay assets rendered
  client-side in an OBS browser source (music spec §8.1 — added as the 4th type/surface).
- **Coexistence, not single-winner**: `resolve_app` (one winner) → `resolve_apps` (activated
  SET) — a community runs N bundles per Feature side-by-side, fan-out per event (SDK §5.2, §7.1;
  landed C2). Conflict via `incompatible_with` (symmetric, blocks co-activation; SDK §7.3).
- **Isolation**: per-(community × app_id) Valkey namespace
  `waddles:t:{tenant}:c:{community}:app:{app_id}:{stage}` for stream/config/state (SDK §7.2;
  landed C4).
- **3-tier lifecycle** `activated ⊆ available ⊆ installed` (SDK §5):
  `installed` (GLOBAL, `app_catalog`) → `available` (TENANT, `app_tenant_availability`) →
  `activated` (COMMUNITY set, `app_activations`).
- **Execution models** (FORK1 resolved, `.PLAN:268-271`): `native` = **first-party builtin
  ONLY**, in-process; external/community/thirdparty ⇒ network boundary (`webhook_push` /
  `rest_pull`). Community-authored native scripts DEFERRED until a real sandbox exists.
- **`platform_compatibility`** (FORK6 resolved): real `PLATFORM_VERSION` replaces stale
  `flask_core.__version__`; BLOCK activation outside `[min,max]`, WARN in-range but ≠
  `tested_with` (SDK §3.4).
- **Action `target_type`** enum (`.PLAN:273-285`): `webhook | rest_api | grpc_api | graphql_api
  | email | overlay | message_queue`; platform provides one adapter per type. `overlay` has
  sub-surfaces `full_screen | media | crawler` rendered by svc-presentation. (Distinct from
  thirdparty *invocation* models webhook_push/rest_pull.)
- **Distribution**: control-plane writes (hub-api → primary `app_catalog`); code dist = stage-
  runners **poll** hub-api for installed set + reconcile per-stage; routing reads from **read
  replica** (hot path), never primary (SDK §6). Per-service DB: runners read-only on replica,
  hub-api write on primary.

---

## 3. Feature-flag + license model

### 3.1 Two-gate + module toggle + per-capability granularity

```
MODULE global toggle (Helm: modules.<name>.enabled → MODULE_LOAD_<NAME>)   [built, §9 P0]
        │  applies to the 7 SCCEBM modules
        ▼
PER-CAPABILITY PostHog flag   waddles.{module}.{feature}   (each default app bundle / capability
        │                      has its OWN flag — new flags default OFF)
        ▼
TIER gate (license entitlement)   free / professional / enterprise
```

A Feature is available iff **(tier entitles) AND (PostHog flag true)** — AND-gate, degrades to
last-known cache on server unreachability, never fails open on a never-seen flag
(`2026-08-26-v3-scbm-apps-design.md:363-369`; landed in `entitlement.py`/`feature_flags.py`).
Middleware order (contract): `tenant → scope → feature/licensing`; authz on `scope` only, never
role names. Entitlement resolves narrowest-first: `community → tenant`.

**Per-capability granularity** (`.PLAN:468-474`) is the growth axis: today's 35-feature catalog is
one flag per feature; the design grows it to one flag **per capability / default app bundle within
a module** — e.g. `waddles.community.forums / .chat / .virtual_stages`,
`waddles.socials.linkedin / .x / .facebook / .instagram / .tiktok`,
`waddles.streaming.record / .forward / .transcode / .rtc`,
`waddles.marketing.campaigns / .shortlinking / .emailing`. Flag key convention stays
`waddles.{module}.{feature}`.

### 3.2 Tiers + the 35-feature catalog (grows to per-capability)

| Tier | Sizing | Count today |
|---|---|---|
| **Free** | ≤50 emp, 1 admin, 1 tenant | 19 |
| **Professional** | 50–200 emp, many admins, 1 tenant | +9 |
| **Enterprise** | 200+ emp / 10+ yr, many tenants | +7 |

Structural caps: >1 admin = Professional; >1 tenant = Enterprise (`.PLAN:38-39`). Catalog is 35
tier-gated Feature contracts merged (`.PLAN:184-186`); it **grows** to per-capability granularity
under §9 P4. **Tier-name collision (OPEN, §11):** license-server enum is
`community/professional/enterprise`; v3 wants `free`. `api/routes/api.py:166` hardcodes `tier !=
"community"` — renaming mis-tiers that heuristic. Seed keeps `community` for now.

---

## 4. Media

Two media containers + the music station Feature. Cross-refs:
`docs/plans/2026-08-31-music-station-design.md`, and the owed svc-streaming spec (§10).

### 4.1 `svc-presentation` (overlays + music station)

The 4th stage-runner. Follows the bundle distribution model (poll hub-api installed set + read
replica routing). Renders per-community, serves browser sources (OBS) over HTTP/WS. Owns:

1. **Core overlays** — `full_screen | media | crawler` (the `overlay` action target's
   sub-surfaces). `crawler` = lower-third ticker; distinct from `message_queue` action target.
2. **Music station** — now-playing / up-next / request-list + client-side browser-source player.
3. **Each activated bundle's `presentation` component** — e.g. a giveaway-wheel overlay, isolated
   under `...:app:{app_id}:presentation`.

### 4.2 Music station (major advertised selling point)

One intermingled **per-community queue** mixing Spotify + YouTube + SoundCloud, fed by both song
requests and imported playlists. Load-bearing = normalized **`Track`** model (source, source_id,
title, artist, duration, artwork, playback_url, category, requested_by, added_via). Pipeline:
3 source-integration **ingest** bundles → 1 shared **process** queue-manager → **presentation**
player (audio plays CLIENT-side in OBS). Per-community **policy** (`music.policy:admin`:
`song_requests_allowed`, `requests_category_restricted`) + **moderation** (`music.queue:moderate`:
kick track/playlist, category override, audited). **Highest-priority open decision** (music §9.2):
the single-shared-queue requirement conflicts with per-`app_id` isolation-by-default — reconcile
via shared consumer group or Feature-scoped process stage. SoundCloud provider already exists;
per-community OAuth is the new work.

### 4.3 `svc-streaming` (RTC + broadcast)

Absorbs `svc-rtc` + `video_proxy_module` into one container (`.PLAN:385-389`). Capabilities (own
per-capability flags, module-toggleable): **RECORD**, **DISPLAY** (community "live streams"
section), **FORWARD** to 1..N targets, **ENCODE/TRANSCODE** (HLS/RTMP/AV1), **RTC** (interactive
voice/video). Language: existing Go (Gazer merge) → **migrate to Rust** (high-perf networking =
Rust target; no new Go). Monetization = transcoding/encoding **tokens** (§5).

### 4.4 Live-streams aggregation

Community "live streams" section = (a) the community's own streaming-proxy streams + (b) any
CONNECTED platform channels currently LIVE (Twitch EventSub / YouTube Live; extensible). Live-
status detection via existing platform integrations. Rendered by svc-presentation. Part of the
Streaming module's DISPLAY capability.

---

## 5. Metered-consumable token billing (3rd axis)

A **third** metering axis beyond per-node and per-seat (`.PLAN:352-356`). One mechanism, two
consumables so far:

- **Transcoding/encoding tokens** (svc-streaming §4.3)
- **Premium-AI tokens** (§6)

Mechanics: global admin sets per-token prices → community buys a balance → usage decrements →
via **marketplace payments + license-server metering**. Design once, reuse for both. Enforcement
posture (OPEN, §11): balance block-vs-overage, refill/purchase flow, ledger location. This lives
in the **marketplace module inside hub-api** (§8) + license-server.

---

## 6. Premium AI

Model-selection / provider-routing layer (`.PLAN:345-350`). Ties to WaddleAI / ai-interaction +
`model_access_policy` (migration 018).

| Tier | Model | Billing |
|---|---|---|
| **Free** | small local (e.g. `llama3.1:1b`) | free default |
| **Premium** | larger local MoE (e.g. `gemma4:26B-A4B`) | per-token metered (§5); needs beefy host |
| **BYOK** | community's own OpenAI/Anthropic/etc. key | their provider + cost; keys in secure per-community storage, **proxied server-side** (never client/hardcoded) |

Routing order: free-local → premium-local-metered → BYOK-external.

---

## 7. API

Standard bundle-oriented path (`.PLAN:391-399`):

```
/api/v2/{module}/{surface}/{app_bundle}/{target}
   {module}   = socials|customers|community|event|marketing|bot|streaming (or platform ns)
   {surface}  = ingest|process|action|presentation
   {app_bundle} = app_id / bundle
   {target}   = action target_type (action-surface only; optional elsewhere)
```

- API major **v2** (bundle API) alongside existing `/api/v1`. Tenant/community from **JWT
  claim, never in the path**.
- Three surfaces on **hub-api**: **gRPC** (services, :50051, `api_version` field), **REST**
  (browsers/mobile/CLI, OpenAPI 3.x generated), **MCP** (AI agents; per-tenant tool list derived
  from entitled Feature contracts, gated below the surface). MCP blueprint landed (`.PLAN:200`),
  ready to mount once hub-api is real.
- OpenAPI **generated** (`quart-schema`), two-document split (public login-only + full behind
  JWT) per `backend.md`.

---

## 8. hub-api Node→Quart migration (CRITICAL PATH)

**Reality (`.PLAN:416-422`; verified on `release/v3.0.X`):** `hub-api` is even less than a Python
skeleton — it exists **only as k8s manifests** (`k8s/helm/waddlebot/templates/hub-api.yaml`,
labeled "SKELETON PLACEHOLDER") pinned to the shared `pythonBaseImage` with `MODULE_NAME=hub-api`.
There is **no `app.py`, no Dockerfile, no controller logic** — it was scaffolded to lock container
topology only. The functional hub is STILL Node: admin/hub_module/backend/src/controllers
(**exactly 44 `*Controller.js`**) + admin/marketplace_module/backend (**11 controllers + 11
routes**). Node code is intact and working — nothing lost.

**Directive (`.PLAN:366-375`):** convert ALL Node/Express backend to Python3/Quart. Only
`hub-webui` (React + Express static-serve/proxy) stays Node. This makes hub-api real and is the
**prerequisite** for bundle-marketplace + control-plane endpoints (which live in hub-api).

**Archaeology result (`.PLAN:458-465`):** a Python hub (`community_hub_module_flask @ ece1f1ee`)
existed ~2.5h on 2025-12-01 before the Node cutover — 14 files, SSR Jinja, <15% coverage, ~5-6 of
44 controllers. **DO NOT resurrect; PORT from Node** (a year of features). Use `ece1f1ee` as a
**reference/pattern donor only** (Quart+hypercorn+pydal+flask_core layout, require_login/auth
decorators, 3-platform OAuth). `release/v3.0.X` already has flask_core (Quart) — the port
foundation exists. Marketplace had NO real Python (76-LOC stub).

Controller inventory (all → Python3/Quart): identity/auth (auth, identity, passkey,
userManagement, profile) · tenancy (tenant, community, communityProfile, joinRequest) ·
admin/platform (admin, superadmin, platform, platformConfig) · marketplace/billing (marketplace,
vendorRequest, vendorSubmission, token + catalog, installation, subscription, payment, premium,
discountCode, vendor, adminReview, routerIntegration) · media control-plane (music, stream,
streaming, overlay, calls) · interactions (polls, forms, announcement, calendar, shoutout,
interaction, loyalty, inventory, raffleCustomization, chat, workflow) · ai (aiChatter,
aiKnowledge) · privacy (dataPrivacy, cookieConsent) · support (support, ticket) · misc (githubSync,
rcon, analytics, activity, public).

**Full sequencing is a separate doc:** `docs/plans/2026-08-31-hubapi-node-to-quart-migration.md`
(being written in parallel). This program plan places it as the P1 critical path (auth/identity/
tenancy/platform first), with the marketplace port folded into P2.

---

## 9. PHASED ROADMAP (the core deliverable)

Dependency-ordered. **hub-api migration (P1) is the critical path** — bundle-marketplace,
control-plane endpoints, and every media/billing surface downstream need hub-api to be real.
Bundle SDK increments C1–C12 (SDK spec §5-6, C1-C12 in `.PLAN:242-266`) map into P0/P2/P3.

| Phase | What | Depends on | Deliverable / exit gate | Containers / modules |
|---|---|---|---|---|
| **P0** ✅ DONE | Foundations (flag-plane, app-framework, posthog, license catalog), Feature-contract spine + Bot pattern, 35-feature MVP (all modules registered + default Apps + gated), MCP blueprint, `require_scope`, module-toggle, hardening; **bundle plumbing C1** (per-stage manifest) **/ C2** (resolve_apps + conflict) **/ C4** (isolation keys); **specs** (app-bundle-SDK, music-station) | — | 12+ PRs merged (`.PLAN:184-215`); 35/35 contracts, 100% spine cov; C1/C2/C4 + 2 specs merged (#192-#196) | flask_core; all modules registered |
| **P1** ⭐ CRITICAL | **hub-api core migration** — port auth/identity/tenancy/platform controllers Node→Quart; establish real `PLATFORM_VERSION`; wire full-hostname bypass match | P0 | hub-api serves real auth/identity/tenancy/platform REST+gRPC; OpenAPI generated; per-service DB accounts; behavior + tests preserved | hub-api |
| **P2** | **marketplace-in-hub-api** (port `marketplace_module` Node→Python) + **bundle 3-tier tables** (C3: `app_catalog`/`app_tenant_availability`/`app_activations`, supersede `hub_module_installations`) + install/available/activate/**conflict** endpoints (C8) | P1 | Global admin installs → tenant curates available → community activates a set; conflict blocks co-activation; `activated ⊆ available ⊆ installed` enforced | hub-api (marketplace) |
| **P3** | **stage-runners + distribution** — C5 (svc-ingest/process/action load activated native bundles + fan-out) + C10 (hub-api install/uninstall + installed-set poll endpoint + artifact store) + C11 (runner poll+reconcile) + C12 (read-replica routing + read-only DB account + short-TTL cache) + C6 (platform-compat check, FORK6) + C7 (bundle.yaml authoring) + **action target_type adapters** + C9 (reference giveaway bundle) | P2 | An activated native bundle runs its stage code for a live event; poll/reconcile self-heals; routing reads hit replica not primary; target adapters emit per type | svc-ingest/process/action, hub-api |
| **P4** | **per-module default bundles + per-capability flags** — migrate `KNOWN_MODULES` to SCCEBM; map 44 controllers → default App Bundles per module; grow license catalog + PostHog flags to per-capability granularity; **close bypass-depth gap** | P2 (bundle model), P3 (runners) | SCCEBM taxonomy live; each capability a flagged+tier-gated default bundle; bypass depth per host-class, resolver takes domain arg | all 7 modules |
| **P5** | **media** — `svc-presentation` (core overlays + presentation component type/`KNOWN_SURFACES += presentation`) + `svc-streaming` (RTC + HLS/RTMP/AV1, Go→Rust) + **music station** (Track + shared queue + policy/moderation + browser-source player + SoundCloud per-community OAuth) + **live-streams aggregation** | P3 (presentation = 4th stage-runner), P4 (streaming/community modules) | Overlays render per-community; music queue plays multi-source back-to-back in OBS; streams record/forward/transcode; live section aggregates connected channels | svc-presentation, svc-streaming |
| **P6** | **token billing + premium AI** — metered-consumable ledger (transcoding + premium-AI tokens, one mechanism) via marketplace payments + license-server; premium-AI routing (free-local / premium-local-metered / BYOK) | P2 (marketplace payments), P5 (streaming transcode consumer) | Global-admin pricing; community balance/consumption; BYOK keys server-side; usage decrements a ledger | hub-api (billing), svc-streaming, AI modules |
| **P7** | **docs consolidation** (also a per-phase gate) — README + docs to 8 containers, App Bundle SDK, module/feature/bundle model, music station, streaming, presentation, premium-AI/token billing, marketplace-in-hub-api (Python), Node→Python; `scripts/check-doc-refs.sh` validator (ratchet 26 dead refs → 0) | runs alongside every phase; final pass last | Zero dead doc refs (denominator reported); README/index rewritten last once structure is true | — |

**Critical path:** P0 (done) → **P1 hub-api real** → P2 marketplace+3-tier → P3 stage-runners →
{P4 SCCEBM bundles, P5 media} → P6 billing/AI → P7 docs. P4 and P5 partly parallelize once P3
lands. Docs (P7) is a per-phase exit gate throughout, not only a tail.

**Migration state (verified):** two systems on `release/v3.0.X` — legacy SQL
`config/postgres/migrations/` (head `068_add_welcomed_users.sql`) and alembic `alembic/versions/`
(head `0004`). `app_catalog` / `app_tenant_availability` / `app_activations` **do not exist as
tables yet** (referenced only in docstrings/tests) — P2 creates them. `flask_core.__version__` is
still `"2.0.0"` — P1 establishes the real `PLATFORM_VERSION`.

---

## 10. Remaining specs needed

| Spec | Scope (1-line) | Blocks |
|---|---|---|
| **svc-streaming design** | Streaming-proxy module + container: RECORD/DISPLAY/FORWARD/TRANSCODE/RTC capabilities, Go→Rust migration timing, video_proxy/svc-rtc absorption, AV1 compute | P5 streaming |
| **metered-token-billing design** | The 3rd consumable axis: token ledger/store location, global-admin pricing surface, balance enforcement (block vs overage), refill/purchase flow, license-server metering integration | P6 billing |
| **premium-AI-routing design** | Model-selection/provider-routing layer: free-local → premium-local-metered → BYOK; model list + per-token gating; BYOK key storage + rotation; WaddleAI/model_access_policy tie-in | P6 AI |
| **presentation-component (bundle SDK follow-up)** | Formalize the 4th component type on the bundle SDK: `bundle.yaml` presentation StageSpec shape (html_entrypoint, assets, browser_source_path), `KNOWN_SURFACES += presentation`, per-community browser-source URL/token minting | P5 presentation |
| **bundle 3-tier table migration detail** | Exact `app_catalog`/`app_tenant_availability`/`app_activations` DDL + rename-vs-new-tables + backfill from `hub_module_installations`/`marketplace_subscriptions` + uninstall cascade policy | P2 |

---

## 11. Consolidated open decisions

Pulled from `.PLAN`, the app-bundle SDK spec (§10), and the music-station spec (§11).

### Program / platform (`.PLAN`)

1. **Tier-name collision** — license-server `community` vs v3 `free`; `api/routes/api.py:166`
   hardcodes `tier != "community"`. Cross-team decision; not blocking (§3.2).
2. **Bypass-depth gap** — code is flat; design wants per-host global vs global+community depth +
   full-hostname match (§3.3).
3. **Streaming module placement** — own top-level module (chosen: yes, "S" in SCCEBM) vs
   Social-owned Feature; supersede/absorb video_proxy + svc-rtc (chosen: absorb).
4. **Go→Rust migration timing** for svc-streaming.
5. **Token metering model** — ledger location, pricing surface, balance block-vs-overage,
   refill flow (spec owed, §10).
6. **Premium-AI** — model list + gating, BYOK key storage + rotation.
7. **Live-streams** — status via webhook/EventSub (push) vs poll; embed vs link; platforms at
   launch.
8. **hub-api marketplace consolidation** — confirmed: rewrite marketplace in Python inside
   hub-api (resolved by Node→Python directive).

### App Bundle SDK (spec §10)

9. **Native-script sandboxing** — subprocess/WASM/pod vs in-process trust (FORK1 partly resolved:
   native = first-party only; community-native deferred until real sandbox).
10. **Config precedence** — bundle → tenant → community; deep-merge vs full-replace per layer.
11. **`bundle.yaml` ↔ `AppManifest` reconciliation** — rename `permissions`→`requires_scopes`
    vs alias at parse; `surfaces` vs new `stage_specs`.
12. **`compatible_with` semantics** — declared app_id lists vs a `provides: [capability]`
    exclusive-provider model.
13. **3-tier migration** — new tables vs rename-in-place; backfill plan (spec owed, §10).
14. **`platform_compatibility` enforcement** — block-out-of-range/warn-untested default (FORK6
    resolved to this) + canonical running-version source.
15. **Uninstall cascade** — cascade-delete dangling available/activated rows vs block uninstall.
16. **Poll interval + artifact store** — DB BLOB vs object storage vs OCI registry.

### Music Station (spec §11)

17. **Shared-queue vs per-app_id isolation** (highest priority) — Music Station's single shared
    queue conflicts with per-`app_id` isolation-by-default (§4.2).
18. **Module ownership** — reuse `social`/`community` vs a new `music` module.
19. **License tier** for the music station Feature — free/pro/enterprise.
20. **Queue persistence** — Redis+in-memory-fallback (loses state on blip) vs durable Postgres.
21. **Category source** — per-platform metadata (inconsistent) vs internal classification layer.
22. **Playback-sync across viewers** — server-authoritative vs best-effort per-instance.
23. **SoundCloud OAuth** — new per-community `soundcloud_interaction_module` vs single-instance
    env-var OAuth.
24. **Spotify playback** — Web Playback SDK (real in-browser, Premium-required) vs Connect-device
    control (audio outside the browser source).
25. **Presentation URL/token minting** — extend `community_overlay_tokens` vs per-`app_id` token
    table.
26. **Overlay taxonomy rename** — today's `ticker/media/general/captions` → proposed
    `full_screen/media/crawler` (no confirmed 1:1).
27. **Policy storage** — dedicated `music_station_policy` table vs `app_activations.config` JSONB.
28. **Kick-whole-playlist targeting** — import-batch id on `Track` vs `QueueItem`.

**Count: 28 consolidated open decisions** (8 program/platform, 8 bundle SDK, 12 music station;
items 3 and 8 carry a recommended resolution already).
