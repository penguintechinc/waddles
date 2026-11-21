# hub-api: Node/Express → Python3/Quart Migration Plan

> **Historical document.** Written against the repository layout at the time; some paths named below (shown as plain text, not links) have since moved, been renamed or been removed. Not maintained as current architecture.

**Date:** 2026-08-31 · **Status:** PLANNING (no implementation) · **Branch:** `feature/v3-hubapi-migration-plan`
**Cross-ref:** master program plan `docs/plans/2026-08-31-v3-sccebm-program-plan.md` (parallel), SCBM design `docs/plans/2026-08-26-v3-scbm-apps-design.md`, App Bundle SDK `docs/plans/2026-08-31-app-bundle-sdk-design.md`

---

## 1. Goal + Constraints

Port the **entire Node/Express hub backend** to **Python3/Quart inside `hub-api`**. Only `hub-webui` (React + Express static-serve + `/api` proxy) stays Node.

| Scope | Detail |
|---|---|
| **Source (port FROM)** | admin/hub_module/backend/src — 44 controllers (23,643 LOC), 32 routes (2,729), 10 services (3,956), 5 middleware (1,329), 2 Socket.io handlers · admin/marketplace_module/backend/src — 11 controllers (2,344), 14 services (4,821). **≈39K LOC Node backend.** |
| **Target** | `hub-api` Python skeleton — today a placeholder (`k8s/helm/waddlebot/templates/hub-api.yaml`: `pythonBaseImage`, `MODULE_NAME=hub-api`, no Dockerfile/CI image, no app code). Port the real hub logic in. |
| **Stack** | Quart + hypercorn + pydal + `flask_core` (already on `release/v3.0.X`) — the port foundation exists. |
| **Contract (MUST preserve)** | `admin/hub_module/frontend/src/services/api.js` (1,054 LOC, 26 exported API groups, axios `baseURL` env, all paths `/api/v1/...`, refresh at `/api/v1/auth/refresh`). hub-webui React consumes this JSON API unchanged. **This file is the contract source of truth.** |
| **Auth/middleware** | `tenant → scope → feature/licensing` chain — `flask_core` already has `tenant_middleware`, `require_scope`, `feature_enabled` (per `.PLAN`; PRs #191/#189 landed). |
| **DB** | Per-service DB accounts (read-only / read-write / scoped-RLS / admin); pydal maps the **existing Postgres schema** — no data migration, same tables. |
| **OpenAPI** | Generated 3.x via `quart-schema` (`@validate_request`/`@validate_response`); two-doc split (public login-only + full behind JWT). Spec at `openapi/v1.yaml` (+ `v2.yaml`). |
| **Reference donor only** | `community_hub_module_flask @ ece1f1ee` (Quart blueprint layout, `require_login`/`require_auth`, pydal, 3-platform OAuth). Recover: `git archive ece1f1ee community_hub_module_flask | tar -x -C <dir>`. **Port from Node — do NOT resurrect** (that stub covered <15%). |

**Non-goals:** no behavior changes, no schema changes, no frontend rewrite, no `/api/v1` path reshaping in the port itself (v2 is additive — see §8).

---

## 2. Controller Inventory + SCCEBM Mapping

**Legend:** *Core* = always-on hub-api platform endpoint (tier-gated, not a product module). *Module* = becomes a **DEFAULT APP BUNDLE** within a SCCEBM product module. `†` = overlap/ambiguous (see §8 Open Decisions).

### Core / hub-api (33 — 22 hub + 11 marketplace)

| Controller | Core area | Notes |
|---|---|---|
| `authController` | Identity | OAuth (3-platform), login, `/auth/refresh` — contract-critical |
| `identityController` | Identity | identity linking |
| `passkeyController` | Identity | WebAuthn/passkeys |
| `userManagementController` | Identity | user CRUD/admin |
| `profileController` | Identity | user profile |
| `tokenController` | Identity | PAT/CAT access tokens |
| `tenantController` | Tenancy | tenant CRUD/scope |
| `communityController` | Tenancy | community **ENTITY** (teams/OUs) = Core, not the Community module |
| `communityProfileController` | Tenancy | community entity profile |
| `joinRequestController` | Tenancy | membership onboarding |
| `adminController` | Platform-admin | |
| `superadminController` | Platform-admin | super-admin (cross-tenant) |
| `platformController` | Platform-admin | platform mgmt |
| `platformConfigController` | Platform-admin | platform config |
| `publicController` | Public | unauth surface (stats, spotlight, live) |
| `analyticsController` | Analytics | platform analytics (Pro-gated) |
| `dataPrivacyController` | Privacy/Compliance | GDPR/CCPA |
| `cookieConsentController` | Privacy/Compliance | consent |
| `workflowController` | Automation | proxy to `workflow-core` — cross-cutting |
| `marketplaceController` | Marketplace | hub marketplace surface |
| `vendorRequestController` | Marketplace | vendor onboarding |
| `vendorSubmissionController` | Marketplace | vendor submission |
| **marketplace_module (11):** `catalog` `installation` `subscription` `payment` `premium` `discountCode` `vendor` `vendorAnalytics` `adminReview` `routerIntegration` `module` | Marketplace | folds INTO hub-api marketplace module (`.PLAN`: "marketplace = module within hub-api, not a container"). Stripe/PayPal webhooks (`webhooks.js`). |

### SCCEBM Product Modules → default app bundles (22)

| Controller | Module | Default bundle / feature key | Notes |
|---|---|---|---|
| `chatController` (+ `websocket/chatHandler`) | **Community** | `waddles.community.chat` | real-time chat + Socket.io |
| `pollsController` | **Community** | `waddles.community.polls` | |
| `announcementController` | **Community** | `waddles.community.announcements` | |
| `formsController` | **Community** | `waddles.community.forms` | "Community Forms" |
| `interactionController` | **Community** | `waddles.community.interactions` | member-voice/community interactions `†Bot` |
| `activityController` | **Community** | `waddles.community.activity` | activity tracking + leaderboards |
| `loyaltyController` | **Community** | `waddles.community.loyalty` | loyalty points `†Marketing` |
| `inventoryController` | **Community** | `waddles.community.inventory` | Quartermaster items/points-shop `†Bot` |
| `raffleCustomizationController` | **Community** | `waddles.community.raffles` | raffles/giveaways |
| `streamController` | **Streaming** | `waddles.streaming.stream` | svc-streaming control-plane |
| `streamingController` | **Streaming** | `waddles.streaming.*` | broadcast forward/record/transcode mgmt |
| `callsController` | **Streaming** | `waddles.streaming.rtc` | RTC/calls control-plane |
| `musicController` | **Streaming** | `waddles.streaming.music_station` | svc-presentation music station |
| `overlayController` | **Streaming** | `waddles.streaming.overlays` | svc-presentation overlays |
| `shoutoutController` | **Bot** | `waddles.bot.shoutout` | |
| `aiChatterController` | **Bot** | `waddles.bot.ai_chatter` | |
| `aiKnowledgeController` | **Bot** | `waddles.bot.ai_knowledge` | AI KB `†Community/Support` |
| `rconController` | **Bot** | `waddles.bot.server_manager` | Server Manager (RCON/voice) `†Streaming(voice)/game` |
| `calendarController` | **Event** | `waddles.event.calendar` | |
| `ticketController` | **Event** | `waddles.event.ticketing` | proxy to calendar for event ticketing |
| `githubSyncController` | **Socials** | `waddles.socials.github` | external content sync `†Core-integration` |
| `supportController` | **Customers** | `waddles.customers.cases` | support ticket system = CRM cases `†Core-support` |

**Greenfield (no existing Node controller — build fresh per module):** Socials (LinkedIn/X/Facebook/Instagram/TikTok posts/responses/analytics), Marketing (campaigns/shortlinking/emailing), Customers (accounts/contacts/opportunities/pipelines), Community (forum/virtual-stages), Event (conference mgmt).

### Count per bucket

| Bucket | Count | Source |
|---|---|---|
| **Core / hub-api** | **33** | 22 hub + 11 marketplace |
| **Community** | 9 | hub |
| **Streaming** | 5 | hub |
| **Bot** | 4 | hub |
| **Event** | 2 | hub |
| **Socials** | 1 (+greenfield) | hub |
| **Customers** | 1 (+greenfield) | hub |
| **Marketing** | 0 (greenfield) | — |
| **TOTAL** | **55** | 44 hub + 11 marketplace |

---

## 3. Migration Strategy — RECOMMENDED: Strangler-Fig, per-group hard cutover at the gateway

Node hub and Python hub-api run **side by side**; the ingress/gateway (Kong subchart, per `2026-02-24-kong-subchart.md`) routes `/api/v1/{group}` path-prefixes to **Python** for already-ported groups and **Node** for the rest. Flip one group at a time.

```
  React hub-webui ──/api/v1/*──▶  Kong (path-prefix router)
                                   ├─ ported groups   ──▶ hub-api (Python/Quart)   [same Postgres]
                                   └─ unported groups ──▶ hub_module (Node/Express) [same Postgres]
```

- **Cutover unit = a controller group** (e.g. all `/auth`, then all `/tenant`). Both stacks share the same DB, so a half-ported system is consistent.
- **Rollback = flip the route back to Node** (config-only, seconds). No data rollback needed (shared schema, no destructive migration).
- **Why not big-bang:** ≈39K LOC across 55 controllers; a single cutover has no incremental verification and an all-or-nothing rollback. Strangler gives per-group parity gates.
- **Why not dual-run same route (shadow):** rejected — doubles write paths on a shared DB (double-writes/races). One route → one stack at a time.

### Sequence (dependency-ordered)

| Phase | Group | Why here | Controllers |
|---|---|---|---|
| **M0** | Foundation | Quart app skeleton on hypercorn, pydal bound to existing schema, `flask_core` middleware chain, Socket.io ASGI mount, OpenAPI two-doc split, per-service DB creds, Kong route table | — |
| **M1** | **Core: Identity/Auth** | everything depends on auth + JWT/session | auth, identity, passkey, userManagement, profile, token, public |
| **M2** | **Core: Tenancy** | tenant/community entity gates all scoped queries | tenant, community, communityProfile, joinRequest |
| **M3** | **Core: Platform/Admin + Privacy + Automation** | admin surfaces + compliance plumbing | admin, superadmin, platform, platformConfig, analytics, dataPrivacy, cookieConsent, workflow |
| **M4** | **Marketplace** | unblocks bundle lifecycle (install→available→activate) + payments | 3 hub vendor* + 11 marketplace_module + Stripe/PayPal webhooks |
| **M5** | **Bot** | pattern-prover module (matches `.PLAN` Bot-first wave) | shoutout, aiChatter, aiKnowledge, rcon |
| **M6** | **Community** | largest module, real-time chat + WS | chat(+WS), polls, announcement, forms, interaction, activity, loyalty, inventory, raffle |
| **M7** | **Streaming** | svc-presentation/svc-streaming control-plane | stream, streaming, calls, music, overlay |
| **M8** | **Event** | | calendar, ticket |
| **M9** | **Socials + Customers + Marketing** | 3 mostly-greenfield tails; port the 2 existing (githubSync, support) then build fresh | githubSync, support (+ greenfield) |

Each phase, per group: port controllers → Quart blueprints · port services → async service layer · **preserve JSON contract** (diff against `api.js`) · port/characterize tests · wire per-service DB creds · add OpenAPI entries · flip Kong route · smoke + parity gate → next.

---

## 4. Per-Controller Porting Checklist (repeatable recipe)

```
[ ] 1. Route    → Quart blueprint; path + method IDENTICAL to Node route (grep api.js for the exact /api/v1 path)
[ ] 2. Validate → quart-schema @validate_request (Pydantic/dataclass) — replaces middleware/validation.js per-route
[ ] 3. Service  → async service fn (I/O off the event loop); port from src/services/*; no sync DB in handler
[ ] 4. DAL      → pydal against EXISTING table (no schema change); parameterized; tenant filter at query layer
[ ] 5. Auth     → @require_login + @require_scope(<resource:action>); tenant from validated JWT, never body/param
[ ] 6. Response → @validate_response against explicit DTO (security.md output-validation) — NEVER raw model/**dict
[ ] 7. Feature  → feature_enabled(flag, tenant=, community=) gate where the endpoint maps to a tier-gated capability
[ ] 8. Tests    → port Node test (or write characterization test from live Node response) BEFORE handler; assert exact field set
[ ] 9. OpenAPI  → auto-generated entry appears in full spec; login endpoint in public spec only
[ ] 10. Contract diff → response shape byte-compatible with api.js expectation; flip Kong route; smoke
```

---

## 5. Websocket + Middleware Porting

### Websocket (2 handlers — admin/hub_module/backend/src/websocket)

| Node | Detail | Quart port |
|---|---|---|
| `index.js` | **Socket.io** server setup; JWT-auth handshake middleware (`socket.handshake.auth.token`), attaches `userId/platform/username` | **python-socketio (ASGI)** mounted alongside Quart on hypercorn — preserves the socket.io **wire protocol** the React `socket.io-client` speaks |
| `chatHandler.js` | real-time chat events; writes `activity_message_events` (leaderboards, fire-and-forget); relays via `mirrorRelayService` | async socketio event handlers; pydal insert; port `mirrorRelayService` to async service |

**Decision:** the frontend uses `socket.io-client`, so the wire protocol is Socket.io, **not** raw WebSocket. Quart's native `@app.websocket` would break the client contract. **Use `python-socketio` in ASGI mode** (mount on the same hypercorn app) to keep `socket.io-client` working unchanged. Quart-native WS only if the frontend is migrated to raw WS (contract break — out of scope). `connectSrc ws:/wss:` CSP already present (`index.js:824`).

### Middleware (5 — admin/hub_module/backend/src/middleware)

| Node | Quart equivalent |
|---|---|
| `auth.js` | `flask_core` JWT + `tenant_middleware`; `@require_login` / `@require_scope` decorators (already exist per `.PLAN` #191) |
| `validation.js` | `quart-schema` `@validate_request` (per-route, generates OpenAPI) |
| `errorHandler.js` | `@app.errorhandler` + structured JSON error envelope (`{status,data,meta}`) |
| `csrf.js` | Quart CSRF (double-submit cookie / `quart-wtf`); SameSite+Secure+HttpOnly cookies |
| `cookieConsent.js` | `@app.before_request` blueprint + `cookieConsentController` port |

---

## 6. Risks + Verification

| Risk | Mitigation |
|---|---|
| **Contract drift** (frontend breakage) | `api.js` is the pinned contract; per-endpoint response-shape diff; `@validate_response` DTO asserts exact field set; regression test per over-exposure-prone endpoint |
| **Behavior parity** | **Port tests FIRST** / write characterization tests capturing live Node responses before writing the Quart handler; parity gate per group before Kong flip |
| **Socket.io compat** | python-socketio ASGI preserves wire protocol; assert `socket.io-client` connects + chat round-trips before flipping WS route |
| **`/api/v1` vs `/api/v2` coexistence** | v1 ported 1:1 (frozen shape); v2 (`/api/v2/{module}/{surface}/{app_bundle}/{target}`, `.PLAN` line 391) is **additive**, bundle-oriented, built alongside — never a rename of v1 |
| **Data-model continuity** | same Postgres, pydal maps existing tables, no destructive migration; Node + Python read/write same rows during split — cutover unit = whole group to avoid split-write on one route |
| **Auth/session during split-brain** | shared JWT secret + same cookie attrs so a token minted by Node validates in Python and vice-versa across the transition (see §8) |
| **Per-service DB creds** | each group gets scoped DB account before its route flips; no shared superuser cred |
| **Kong route regressions** | route table is the single cutover control; smoke every group after flip; rollback = revert route |

---

## 7. Sizing + Phasing (plugs into master program plan)

| Phase | Group | Controllers | Approx LOC (Node src) | Effort |
|---|---|---|---|---|
| M0 | Foundation | — | scaffold | S |
| M1 | Core Identity/Auth | 7 | ~4–5K | L (OAuth 3-platform, passkeys) |
| M2 | Core Tenancy | 4 | ~2.5K | M |
| M3 | Core Platform/Privacy/Automation | 8 | ~5K | L |
| M4 | Marketplace | 14 | ~9.5K (2.3K ctrl + 4.8K svc + hub vendor*) | L (Stripe/PayPal, orders) |
| M5 | Bot | 4 | ~2.5K | M (pattern-prover) |
| M6 | Community | 9 | ~7K + WS | XL (largest; Socket.io) |
| M7 | Streaming | 5 | ~3K | M (control-plane only) |
| M8 | Event | 2 | ~1.5K | S |
| M9 | Socials+Customers+Marketing | 2 existing (+greenfield) | ~2K + new | M–L (greenfield scope in program plan) |

**Totals:** ≈39K LOC Node backend · 55 controllers · avg ~537 LOC/hub-controller, ~213/marketplace. **Suggested 9-phase breakdown** maps onto the master program plan's Core→module wave (Bot pattern-prover first, then modules) — see `docs/plans/2026-08-31-v3-sccebm-program-plan.md`. Media-control modules (Streaming) are **management API only**; actual media runs in `svc-presentation`/`svc-streaming`.

---

## 8. Open Decisions (surface to user)

| # | Decision | Recommendation |
|---|---|---|
| D1 | **Strangler vs big-bang per group** | Strangler (Kong per-group cutover + config rollback). Big-bang only if the team wants one flag-day and accepts all-or-nothing rollback. |
| D2 | **Keep `/api/v1` identical vs reshape to `/api/v2/{module}/{surface}/{bundle}/{target}` during the port** | Port v1 **1:1 unchanged** (protect the React app); build v2 **additively** as the bundle-oriented API. Do NOT reshape mid-port. |
| D3 | **Auth/session compat during transition** | Share JWT signing secret + identical cookie attrs (HttpOnly/Secure/SameSite) so tokens cross Node↔Python freely while both serve. Confirm Node `jsonwebtoken` HS256 params == `flask_core` verifier. |
| D4 | **Socket.io transport** | python-socketio ASGI (keep `socket.io-client`). Reconsider raw Quart WS only with a coordinated frontend change. |
| D5 | **Ambiguous controller homes (`†`)** | `rconController` → Bot vs Streaming(voice)/game-module; `githubSync` → Socials vs Core-integration; `support` → Customers(cases) vs Core-support; `loyalty`/`inventory`/`interaction`/`aiKnowledge` module vs Community/Bot overlap. Confirm before assigning feature-flag namespaces + KNOWN_MODULES. |
| D6 | **Marketplace fold-in granularity** | Confirm marketplace becomes a hub-api **blueprint package** (not separate container) per `.PLAN` line 358; whether its 14 services (stripe/paypal/order/premium) port as-is or consolidate. |
| D7 | **Per-service DB account cardinality** | one account per SCCEBM module vs one per hub-api (control-plane) with RLS — recommend per-module scoped accounts, admin(DDL) separate. |
