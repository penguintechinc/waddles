# hub-api v2 migration (spec only)

Status: DRAFT for user review. No code changes. Decision (user): finish the current `/api/v1` fixes
this release; migrate hub-api to `/api/v2/...` **after** this release (v3.0.X). v1 stays frozen
and serving throughout.

Evidence base (release/v3.0.X @ 36d7a051f): `openapi/v1.yaml` (492 paths / 618 operations),
`hub_api/blueprints/v1/*` (75 modules, 545 `@route` decorators), `hub_api/blueprints/v2/platform.py`
(the only v2 surface: `/api/v2/core/platform/default/{status,echo}`), webui `api-gap-check.mjs`.

## 1. Why v2

What accumulated in v1 (all measured or cited from the webui audit, not guessed):

| Problem | Evidence |
|---|---|
| Community scope baked into the path, 3 spellings | `/api/v1/admin/{community_id}/...` (214 ops), `/api/v1/community/{community_id}/...` (23), `/api/v1/communities/{community_id}/...` (27), bare `/api/v1/{community_id}/github-sync\|join-requests` (7) |
| Audience-prefixed trees instead of resources | same resource lives under `admin/`, `community/`, `public/`, `internal/`, `superadmin/` (e.g. music: `admin/.../music`, `public/.../music-station`, `internal/music`) |
| Path stutter | `/api/v1/marketplace/admin/marketplace/*` (14 ops), `marketplace/public/vendor/modules`; vendor paths split across `marketplace/vendor`, `admin/vendor`, `/vendor/*` |
| Unprefixed / wrong legacy paths in the webui | gap-check: 34 calls omit `/api/v1` (e.g. `/ai-knowledge/*`, `/admin/vendor/requests`, `/vendor/analytics/export`) and hit the SPA fallback; 114 of 550 UI calls have no backend route |
| Numeric PKs outward | path params: `community_id` integer on 254 ops, `user_id` integer on 23, `server_id`/`module_id`/`announcement_id`/`ticket_id`/`channel_id` integer; only ~8 ops use `public_id`. Violates "UUID across boundaries" (IDOR enumeration) |
| Same param, two types | `community_id` is `string` on 52 ops and `integer` on 254; `ticket_id` is both |
| Response-shape drift | 171 handlers return `{"success": ...}`, 23 `{"error": ...}`, 14 `{"message": ...}`, 10 `"code"`; 62 `limit`, 16 `offset`, 6 `page`, 7 `total` -- three pagination dialects |
| Unvalidated responses | 545 routes vs 316 `@validate_response` + 63 `jsonify_dto()` (Gotcha #3 workaround); ~165 routes serialize unchecked shapes |
| Strings where int enum codes belong | `"status": "success"/"initiated"`, role/status/type strings across handlers |
| Scope names uneven | 349 `require_scope` uses; mix of `tenant:admin`, `platform:admin`, `community.interaction:write`, `communities:admin` -- audience-flavoured, not `resource:action` |
| Spec only describes v1 | single `openapi/v1.yaml` (also leaks the 2 v2 ops, infra routes, `/static`) |

Why not fix in place: path/shape/ID changes are breaking for every consumer; v1 is pinned by the
Node-port contract (`hub_api/PORTING.md`: "path + method IDENTICAL"). v2 was reserved as additive
(`routers/v2.py`, migration plan 2026-08-31 §8 D2) -- this spec re-scopes it as the **clean REST
surface**, with the bundle-oriented `{module}/{surface}/{app_bundle}` shape kept only under
`/api/v2/apps/...`.

## 2. v2 conventions

| Area | Rule |
|---|---|
| Base | `/api/v2/`; one OpenAPI doc `openapi/v2.yaml` (quart-schema generated, spectral clean) + `/openapi/v2.json` behind auth, `/openapi/v2-public.json` login-only (mirrors v1 split) |
| Naming | plural nouns, kebab-case, max 2 nesting levels, no audience prefix, no verbs except `:action` sub-resources (`POST /bundles/{id}/activate`) |
| Scope axis | tenant is **only** from JWT (never path/body). Community is a sub-resource: `/api/v2/communities/{community_id}/...`. `admin/`, `superadmin/` prefixes are dropped -- authority is the scope, not the URL |
| Audience splits | `/api/v2/public/...` (unauthenticated, rate-limited, read-only), `/api/v2/internal/...` (machine JWT/SPIFFE only, never routed through public ingress), everything else authenticated |
| IDs | outward = `public_id` UUID everywhere (`{community_id}` is a UUID); numeric PK never in path/body/log. Slugs are display aliases, never keys. Lookup is `public_id -> pk` inside the service layer only |
| Enums | int codes with a published enum table in the spec (`status: 1`), plus `x-enum-names`; no free strings, no booleans for 3-state fields |
| Pagination | cursor-first: `?limit=<1..100>&cursor=<opaque>`; envelope `{"data":[...],"page":{"next_cursor":null,"limit":50}}`; `total` only on explicit `?include=total`. Filters = flat query params, documented per route; unknown filter = 422 |
| Success shape | single: `{"data":{...}}`; list: envelope above; 204 for deletes. No `{"success":true}` |
| Error shape | RFC 9457 `application/problem+json`: `type,title,status,detail,instance` + `code` (stable machine string) + `request_id`; validation errors add `errors:[{field,code}]`. Never echo raw exceptions or PII |
| Request/response | every route `@validate_request` + `@validate_response` with `@dataclass(slots=True, frozen=True)` DTOs; **no `jsonify_dto` escape hatch** (fix the Gotcha #3 root cause in the shared helper first); snake_case JSON |
| Scopes | `resource:action`, resource = plural noun (`communities:read`, `communities.members:write`, `bundles:admin`); declared per route in the migration table; tenant middleware first, then scope. Role names never appear in checks. Self-service on own resource keeps the v1 "no scope, ownership check" exception, documented in the spec |
| Idempotency | every non-idempotent POST that creates money/credential/side-effect state accepts `Idempotency-Key` (stored per tenant+route, 24h); PUT/DELETE idempotent by definition |
| Concurrency | `ETag` / `If-Match` on mutable aggregates (settings, bundles, forms) -> 412 |
| Rate limit | `penguin-limiter` on every public route; `429` + `Retry-After` |
| Telemetry | every v2 route emits OTel log+metric+trace with route template (never raw path with IDs) as label |
| Flags | PostHog flag per domain `hubapi.v2-{domain}` default OFF (see s5) |

## 3. Coexistence: v1 and v2 side by side

```
client -> ingress (/api/v1/*, /api/v2/*) -> hub-api (one process)
                                            |- blueprints/v1/*  (frozen, thin adapters)
                                            |- blueprints/v2/*  (new)
                                            `- services/*       (shared domain layer, one source of truth)
```

- **Mounting:** reuse `routers/_discovery.py`; v2 modules drop into `blueprints/v2/<domain>.py` (`BLUEPRINTS`, full `/api/v2/...` prefix). No shared-file edits. Rename the current `core/platform/default` exemplar into the first domain or delete it once `/api/v2/platform` lands.
- **Shared service layer:** v2 handlers call the same `services/*_service.py` functions v1 calls; business logic and the tenant filter at the ORM layer are never forked. New logic lands in the service layer first; v1 handlers become adapters (map numeric id/old shape <-> service DTO). Divergent behaviour = bug.
- **ID bridge:** service layer gains `resolve_public_id(kind, uuid) -> pk` and the inverse; v1 keeps numeric ids, v2 never sees them. Needs `public_id` columns where missing (alembic, additive, backfilled) -- first-phase prerequisite per entity.
- **Deprecation:** once a domain's v2 is flagged ON and has a consumer cutover, v1 responses for that domain add `Deprecation: true`, `Link: <.../v2/...>; rel="successor-version"`, and `Sunset: <date>` (>=2 releases out, never before all consumers are cut over). Metric `hubapi_v1_requests_total{route}` (template label) drives the decision; sunset only at zero traffic for 30 days.
- **Ingress/helm:** `k8s/helm/.../values*.yaml` and `k8s/httproute-hub.yaml` route by explicit prefix (`/api/v1/public|auth|user|community|communities|admin|platform|superadmin|cookie|signup-settings|internal`). v2 needs `/api/v2` prefix rules added to the same lists (and to the Cloudflare/dal2 front-door rules); `/api/v2/internal` must **not** be exposed on the public ingress (matches how `/api/v1/internal` is treated today -- verify in the chart before copying).
- **No big-bang:** per-domain, per-consumer cutover; v1 never loses a route during migration; rollback = flip the flag / point the client back.

Consumer cutover order:

| # | Consumer | Today | Notes |
|---|---|---|---|
| 1 | hub-webui (`services/api.js`, `lib/apiClient.ts`) | ~550 `/api/v1` calls; baseURL `''` | First: owns the audit debt; generate a typed client from `openapi/v2.yaml`; per-domain switch behind the flag; `REFRESH_URL` and auth first |
| 2 | Rust internal REST (`connection_resolver`, `svc_ingest` outbound creds, `svc_presentation`, `overlay_schema`) | `/api/v1/internal/*` (27 ops) | Small, contract-tested already (`connection_resolver/tests/contract.rs`); machine JWT only. `hub_client` is **gRPC** (`waddles.hub.internal.v1`) -> unaffected; gRPC remains the preferred internal transport, REST internal kept only for what has no RPC |
| 3 | Python modules/bots (`svc_action` builtin handlers, `svc_process`, core `*_module` apps, `bundles/*` host calls) | mixed `/api/v1/internal/*`; some `/api/v1` are those modules' *own* APIs, not hub-api | Inventory per module before touching; many greps hit non-hub services -- do not rename those |
| 4 | Mobile (`penguinm`) | no hub-api references in this repo | Greenfield on v2; no v1 debt to carry. Confirm it is not planned against v1 |
| 5 | Third-party / bundle SDK | `/api/v2/apps/...` bundle surface | Already v2-shaped by design |

## 4. Migration inventory

Counts = operations in `openapi/v1.yaml` (618 total; 2 are the v2 exemplar, ~10 are infra
`/health /metrics /mcp /static /openapi`, leaving ~600 business ops). Scope column = proposed
v2 scope family. "Dead" = flagged by the webui audit as unserved/UI-less.

| Domain | v1 prefix (ops) | Proposed v2 resource path | Scope family | Flags/notes |
|---|---|---|---|---|
| Auth/session | `/auth` (27), `/cookie` (9), `/one-time-secrets` (2) | `/auth/...`, `/consents`, `/one-time-secrets` | none (pre-auth) / self | login/oauth/passkey/refresh; keep paths stable-ish, change error+shape; cookies stay HTTP-only |
| Self/profile | `/user` (21), `/signup-settings` (1) | `/me`, `/me/tokens`, `/me/communities` | self | UUID `user_id` |
| Public | `/public` (15), `/flags` (1), `/distribution` (1) | `/public/...` | none (rate-limited) | `public/live`, `public/communities/*`; `distribution/sources` consumed by svc_ingest |
| Tenant | `/tenant` (26), `/tenants` (2) | `/tenant`, `/tenant/{kms,discord-install,twitch-install,data-privacy,...}` | `tenants:*` | `tenant_kms`, `tenant_*_install`, `admin_data_privacy` have **no UI** |
| Communities | `/communities/{id}` (27), `/community/{id}` (23), `/{community_id}/join-requests,github-sync` (7) | `/communities`, `/communities/{cid}`, `/communities/{cid}/join-requests`, `/communities/{cid}/github-sync` | `communities:*` | collapses 3 spellings into one |
| Community admin | `/admin/{community_id}/*` (214): calendar 32, rcon 19, support 15, workflows 15, inventory 13, interaction 12, announcements 11, calls 11, music(-station) 17, streams 8, loyalty 6, shoutout 6, ai 5+2, forms 5, members 4, overlay 4, polls 4, raffle 4, tokens 4, join-requests 3, other ~28 | `/communities/{cid}/{calendar,rcon,support-tickets,workflows,inventory,interactions,announcements,voice-rooms,music,streams,loyalty,shoutouts,ai,forms,members,overlays,polls,raffles,tokens,...}` | `communities.<res>:read/write/admin` | biggest bucket; split by sub-resource into ~25 independent PRs |
| Admin global | `/admin/ai-knowledge` (8), `/admin/bundle-versions` (1), `/admin/calendar` (1) | `/ai-knowledge/sources`, `/bundles/{id}/versions` | `ai.knowledge:*`, `bundles:admin` | webui calls `/ai-knowledge/*` **unprefixed -> dead today** |
| Superadmin | `/superadmin` (24): users 14, communities 6, marketplace 5, tenants 4, platform-config 3, settings 2, dashboard 1 | `/users`, `/communities`, `/tenants`, `/platform/config` with `platform:admin` scope (+ `system` tenant) | `platform:admin` | prefix dropped; authority via scope + reserved `system` tenant, audit-logged |
| Platform | `/platform` (12), `/modules` (6), `/analytics` (11), `/compliance` (4) | `/platform/...`, `/modules`, `/analytics/...`, `/compliance/audit` | `platform:*`, `modules:admin`, `analytics:read` | `compliance_audit` no UI |
| Apps/bundles | `/apps` (22; activation, approvals, permissions, versions, tenant availability), `/admin/.../modules` | `/apps/bundles/...` (keeps bundle-oriented shape), `/apps/communities/{cid}/bundles/{bid}/activate` | `bundles:*` | `bundle_permissions` no UI; v2 exemplar `/api/v2/core/platform/default` folds in here |
| Marketplace | `/marketplace` (61): vendor 23, admin 14 (stutter), communities 8, catalog 4, payments 4, premium 4, community 4, bundles 3, public 3, tenant 3, webhooks 2, token-products 1 | `/marketplace/{catalog,listings,vendors,submissions,orders,payments,webhooks}`; vendor-admin review under `/marketplace/submissions` with `marketplace:admin` | `marketplace:*` | kills `/marketplace/admin/marketplace/*` stutter and `/vendor/*` unprefixed UI calls; `marketplace_lifecycle/modules/webhooks`, `token_billing` no UI |
| Connections | `/connections` (1), `/connection-providers` (1), `/provider-connections` (1), community connections, `youtube_connections`, `guild_pairing`, discord install | `/communities/{cid}/connections`, `/connection-providers`; per `docs/PROVIDER_CONNECTIONS.md` | `communities.connections:*` | framework just shipped on v1 (mig 0058); v2 port after it settles; resolve endpoints stay internal |
| Internal (machine) | `/internal` (27): activity 4, loyalty 6, music 6, calendar 2, communities 2, dataplane 2, live 1, marketplace 2, provider-connections 1, relay 1 | `/internal/...` | machine scopes (`internal.<res>:read/write`) | consumed by Rust/Python services (s3 #2-3); no public ingress |
| Other v1 singletons | `/discord` (1), `/github-sync` (1), `/calendar` (25), `/discord_bot_install`, `/event_*` | folded into communities/tenants rows | | audit each |

Dead / unserved (from `api-gap-check`, drop or implement in v2 deliberately, **never port blindly**):

| Class | Count | Source |
|---|---|---|
| UI calls with no hub-api route | 114 (of 550 UI calls) | `api-gap-check --list` |
| UI calls missing `/api/v1` prefix (never reach hub-api) | 34 | `api-gap-check --list` |
| Registered features explicitly unserved | 18 features / 49 endpoints (mirror-groups, linked-servers, custom-domains, bot-detection, community-analytics, ai-researcher, ai-insights, translation, reputation-config, software/service-discovery, member-profile/roster, open-join, self-create, ...) | `src/lib/unservedFeatures.json` |
| Backend blueprints with no UI call | 19 files: admin_data_privacy, ai_routing, bundle_permissions, compliance_audit, dataplane_scope, distribution, github_sync, ingest_sources, live_status, marketplace_lifecycle/modules/webhooks, provider_connections, tenant_discord_install, tenant_kms, tenant_twitch_install, token_billing, workstream_usage, youtube_connections | `api-gap-check --list` -- most are machine/API-only (legit); each needs an owner decision: keep-v2 / internal-only / retire |

Full per-route mapping table is a **Phase 0 deliverable** (`openapi/v1-to-v2-map.yaml`, generated
skeleton from `openapi/v1.yaml`, hand-reviewed): `{v1_method, v1_path, v2_method, v2_path, scope,
status: mapped|dead|internal|retired}`. It is the single source the contract tests (s5) read.

## 5. Test strategy, coverage, rollout flags

| Check | How | Gate |
|---|---|---|
| v1<->v2 mapping completeness | test loads `openapi/v1.yaml` + `v1-to-v2-map.yaml`; fails if any v1 op lacks a map entry **or** any map entry points at a v2 op absent from `openapi/v2.yaml` (and vice versa: v2 op with no map entry and no `new:true`) | pre-commit + CI; asserts denominator printed (ops examined > 0) |
| v2 spec drift | `make openapi-hubapi-v2` + `check-openapi-drift` extended to `openapi/v2.yaml`; spectral lint | CI |
| Behavioural parity | parametrised test per mapped pair: same fixtures through v1 and v2, assert equal service-layer outcome + status class; real pydal DB, no mocking (writing-python-tests skill) | CI per domain |
| Security regression per route | missing token -> 401, wrong tenant -> 403, missing scope -> 403, cross-tenant UUID -> 404 (never 403 leak), exact response field set (no extra fields) | CI; `security.md` Output Validation |
| ID rules | test: no `{*_id}` path param typed integer in v2 spec; every path id is `format: uuid` | CI |
| Shape rules | spec lint: all 4xx/5xx reference `ProblemDetails`; all lists use the page envelope; no response without a schema | spectral custom ruleset |
| Webui | `api-gap-check` gains `--spec openapi/v2.yaml`; fails on any UI call to an unmapped path, and on any unprefixed call; unservedFeatures registry shrinks monotonically | CI |
| Rust/Python internal | contract tests (`connection_resolver/tests/contract.rs` pattern) rebuilt against the v2 internal spec | CI |
| Coverage | >=90% lines/branches on every new v2 module and the ID bridge | pre-commit |
| Telemetry | smoke asserts log>=1, metric>=1, histogram>=1 on a v2 request | smoke |
| Flags | PostHog `hubapi.v2-{domain}` default OFF; flag gates **route availability** (404 when OFF) and the webui client selection; flag-on E2E per domain (enforcing-no-stubs) | release gate |

## 6. Phases, sizing, risks

| Phase | Scope | Size |
|---|---|---|
| 0 Foundations | conventions doc ratified, `v1-to-v2-map.yaml` skeleton, shared helpers (problem+json handler, page envelope, `Idempotency-Key`, ETag), `public_id` columns + resolver for communities/users/tenants, `openapi/v2.yaml` generation + CI, ingress prefix rules, contract tests (red until mapped) | ~2 wk |
| 1 Pilot | read-mostly small domain end-to-end incl. webui client switch + flag (suggest `public` + `flags`) | ~1 wk |
| 2 Identity | auth, `/me`, tenant, communities core (UUID community id everywhere) | ~3 wk |
| 3 Community admin | ~25 sub-resource PRs (the 214-op bucket), parallel agents, disjoint files | ~5-6 wk |
| 4 Marketplace + bundles + superadmin | incl. stutter removal, vendor consolidation | ~3 wk |
| 5 Internal + Rust/Python consumers | `/internal` v2, consumer rebinding, gRPC where available | ~2 wk |
| 6 Mobile + docs | `penguinm` onboarding on v2, human docs, screenshots refresh | ~1-2 wk |
| 7 Deprecate v1 | headers on, traffic metrics, Sunset after >=2 releases and 30d zero traffic | release+ |

Risks:

| Risk | Mitigation |
|---|---|
| Tenant filter lost in the port (IDOR/cross-tenant) | v2 never queries directly; only through shared service layer that applies tenant at the ORM layer; per-route cross-tenant test is a merge gate |
| Raw models / extra fields leak (security.md Output Validation) | `@validate_response` mandatory, lint forbids `jsonify_dto`/`**__dict__` in v2, exact-field-set tests |
| Numeric->UUID migration breaks existing rows/caches/keys | additive `public_id` backfill migration first; keys in the data plane stay numeric (existing rule: data plane keys on numeric ids) and are mapped at the API boundary only |
| Two code paths drift | one service layer, v1 as adapter; parity tests |
| Ingress/front-door misses `/api/v2` | chart + CF rules in Phase 0; smoke hits one v2 path per environment |
| Scope-name rename invalidates issued tokens / role bundles | scopes are additive: v2 scopes added to bundles alongside v1; v1 scope aliases kept until v1 sunset |
| Webui churn / half-migrated state | per-domain flag; gap-check enforces; codegen client |
| Porting dead routes | map status `dead` forces an explicit retire/implement decision |
| Scope creep into "redesign everything" | conventions frozen at Phase 0; domain behaviour changes forbidden in the move |

## 7. Open questions for the user

1. **Community id**: is `communities/{uuid}` acceptable everywhere (needs `public_id` backfill + data-plane mapping), or keep numeric community ids for internal/data-plane routes only?
2. **Audience prefixes**: OK to drop `/admin` and `/superadmin` from URLs (authority by scope only), or does the front door/WAF rely on those prefixes for access rules?
3. **Pagination**: cursor-only (proposed) or also support `limit/offset` for admin tables the webui sorts/jumps by page?
4. **Error format**: adopt RFC 9457 problem+json globally (changes every client parser) -- confirm, vs. a simpler `{error:{code,message}}`.
5. **Sunset horizon**: v1 sunset only after >=2 releases and 30 days of zero traffic -- agree, or pin a calendar date?
6. **Dead routes**: default policy "retire unless a named owner claims it" for the 19 UI-less blueprints and 49 unserved endpoints?
7. **Bundle surface**: keep `/api/v2/{module}/{surface}/{app_bundle}` for App Bundles only, with everything else resource-oriented (proposed) -- or one shape for all?
8. **Mobile**: any `penguinm` work already assuming v1 paths? (none found in this repo)
9. **Internal transport**: push internal calls to gRPC (`hub_client`) and shrink `/internal` REST, or keep REST internal parity in v2?
10. **Timing/staffing**: start Phase 0 right after the v3.0.X tag, or in parallel on a long-lived `release/v3.1.X` line?
