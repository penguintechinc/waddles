# Provider connections (standard connection framework)

One adapter interface, one connection API and one encrypted per-community token store for every
external platform Waddles talks to. **Twitch is the first provider**; Discord, Slack, Kick and
YouTube plug into the same framework (see [Adding a provider](#adding-a-provider)).

Implemented in **hub-api**: `hub_api/services/provider_connections/`,
`hub_api/blueprints/v1/provider_connections.py`, migration `0058_provider_connections`.
Design background and the storage model it builds on:
[`CONNECTION_MODEL.local.md`](CONNECTION_MODEL.local.md).

## What problem this solves

Platform connections used to be **static environment variables**. `svc-ingest`'s Twitch IRC receiver
read `TWITCH_IRC_NICK` / `TWITCH_IRC_CHANNEL` / `TWITCH_OAUTH_TOKEN` once at startup and never looked
at any connection store, so connecting a bot in the webui changed nothing, and a Helm upgrade could
silently wipe a hand-patched value. Now a community admin (webui today, CLI next -- both call the
same API) registers a connection per community, the token is stored encrypted, and the data plane
asks hub-api which connections to run and for a fresh token to run them with.

| Part | Status |
|---|---|
| Connection API (CRUD per community), scope-gated, tenant + community isolated | **shipped (this change)** |
| Encrypted per-community token store with refresh | **shipped (this change)** |
| Provider adapter interface + registry | **shipped (this change)** |
| Twitch adapter (bot account + channel resolution + refresh) | **shipped (this change)** |
| `GET/POST /api/v1/internal/...` data-plane endpoints (list + resolve) | **shipped (this change)** |
| `svc-ingest` (Rust) resolves connections dynamically instead of static env | per-community Twitch sessions behind `INGEST_DYNAMIC_CONNECTIONS` (alpha) -- runbook: `core/svc_ingest/README.md` |
| webui connection UI, CLI connection commands | follow-ups (call the API below unchanged) |
| Discord / Slack / Kick / YouTube adapters | follow-ups (one adapter + one registry line each) |
| Redirect-style installs (OAuth connect, Discord invite link) | follow-up -- see [Limitations](#limitations-and-follow-ups) |

## Data model

The framework **extends** the three-layer model migration 0035 already landed; it adds no table.

```
tenants 1 ---- N communities                         (a community = the Team/OU layer, in ONE tenant)

tenant_platform_apps        layer 1  the tenant's own OAuth app (untouched here)
platform_connections        layer 2  THE LINK: provider, resource, status, expiry, adapter
                                     metadata, and the only copy of the token (ciphertext)
community_connection_access layer 3  which community may use a link + its approval state + `enabled`
                                     -- no credentials, ever
```

Migration `0058_provider_connections` adds, additively and online-safe:

| Table | Column | Meaning |
|---|---|---|
| `platform_connections` | `resource_metadata JSONB NOT NULL DEFAULT '{}'` | adapter-validated facts (bot account, resolved channel, scopes). Never a secret. |
| `platform_connections` | `expires_at TIMESTAMPTZ` | when `access_token` stops working (NULL = never); drives refresh |
| `platform_connections` | `status_reason VARCHAR(40)` | fixed machine code for why `status` left `active` (`refresh_rejected`, `token_expired`) |
| `community_connection_access` | `enabled BOOLEAN NOT NULL DEFAULT TRUE` | each community's own on/off switch |

A connection is created **once per resource** (`UNIQUE (tenant, platform, resource_id)`) and reached
**through a grant** -- so one bot token can serve many channels, and one channel's link can be shared
by several communities of the same tenant. Creating a connection inserts the link *and* this
community's approved grant in one transaction.

Rules that follow from the sharing model:

* `enabled` is per community and always allowed -- turning yours off never touches anyone else.
* Editing the **shared link** (rotating the token, re-targeting the channel) is refused with `409`
  while another community uses it; it would silently change what they are connected to.
* `DELETE` removes *your* grant. The link -- and with it the ciphertext -- is purged the moment the
  last grant goes.
* Creating a connection for a resource that is already linked in your tenant is a `409`, **never an
  overwrite**: replacing someone else's stored token would be a takeover, not a create. The
  request-access / admin-approval flow for sharing is a follow-up (the grant table already supports
  `pending`; a pending grant is visible to its community and is never usable).

## The API

Admin routes (webui and CLI use the same ones). Every route runs `tenant_middleware` ->
`require_scope("connections:admin")` -> a DB-backed check that the caller administers **this**
community of **their** tenant -> the feature/tier gate -> the service.

| Method & path | Purpose |
|---|---|
| `GET /api/v1/connection-providers` | install descriptors -- a generic UI/CLI renders the connect form from these, with no per-provider code |
| `GET /api/v1/communities/{id}/provider-connections` | list this community's connections |
| `POST /api/v1/communities/{id}/provider-connections` | create (verifies the credential with the provider first) |
| `GET /api/v1/communities/{id}/provider-connections/{conn}` | read one |
| `PATCH /api/v1/communities/{id}/provider-connections/{conn}` | `enabled`, re-target `metadata`, rotate `credentials` |
| `DELETE /api/v1/communities/{id}/provider-connections/{conn}` | disconnect (`{"success":true,"purged":bool}`) |

The OpenAPI document is [`openapi/v1.yaml`](../openapi/v1.yaml) (regenerated by
`make openapi-hubapi`).

### Example: `waddlebots` joins `penguinzplays`

```bash
BASE=https://hub.example.com/api/v1
AUTH="Authorization: Bearer $HUB_TOKEN"   # a tenant-owner token (carries connections:admin)

# 1. What does Twitch need?  (fields to render, scopes the token must hold)
curl -s -H "$AUTH" "$BASE/connection-providers"

# 2. Connect.  The tokens come from the environment, never the command line / shell history:
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  "$BASE/communities/42/provider-connections" \
  --data @- <<EOF
{"provider": "twitch",
 "credentials": {"access_token": "$BOT_ACCESS_TOKEN", "refresh_token": "$BOT_REFRESH_TOKEN"},
 "metadata": {"channel_login": "penguinzplays"},
 "enabled": true}
EOF
```

```json
{"success": true,
 "connection": {
   "id": 17, "provider": "twitch", "resourceType": "twitch_channel",
   "status": "active", "statusReason": null,
   "enabled": true, "access": "approved", "usable": true, "shared": false,
   "hasRefreshToken": true, "expiresAt": "2026-10-10T16:00:00+00:00",
   "capabilities": ["action_relay", "chat_ingest"],
   "metadata": {"channel_login": "penguinzplays", "channel_id": "7001",
                "bot_login": "waddlebots", "bot_user_id": "9001",
                "scopes": ["chat:read", "chat:edit"],
                "capabilities": ["action_relay", "chat_ingest"]},
   "createdAt": "...", "updatedAt": "..."}}
```

There is no field in any response that can carry a token: `hasRefreshToken` is a boolean, and
adapter metadata is filtered through the adapter's own public-key allow-list.

```bash
curl -s -X PATCH -H "$AUTH" -H 'Content-Type: application/json' "$BASE/communities/42/provider-connections/17" -d '{"enabled": false}'
curl -s -X DELETE -H "$AUTH" "$BASE/communities/42/provider-connections/17"
```

### Errors

All errors use the shared envelope `{"success": false, "error": {"code", "message", ...}}`. Messages
are application-authored; provider text, tokens and user input are never echoed. `5xx` bodies are
generic.

| Status | `error.code` | When |
|---|---|---|
| 400 | `BAD_REQUEST` / `UNKNOWN_PROVIDER` | schema/metadata/token-shape violation, unknown provider, unknown field |
| 401 / 403 | -- | no/invalid token; missing `connections:admin`; not an admin of that community; wrong tenant |
| 402 | `FEATURE_NOT_ENABLED` | flag off, or tenant below Professional |
| 404 | `NOT_FOUND` | no such connection in *your* community (same answer for "belongs to someone else") |
| 409 | `CONFLICT` | resource already connected; shared link cannot be changed |
| 409 | `CONNECTION_NOT_USABLE` | `details.reason` says why (`disabled`, `pending_approval`, `refresh_rejected`, ...) |
| 422 | `CREDENTIALS_REJECTED` / `INSUFFICIENT_SCOPES` / `RESOURCE_NOT_FOUND` | the provider refused the token, the token lacks a required scope, the channel does not exist |
| 502 | `PROVIDER_UNAVAILABLE` | provider unreachable / 5xx -- nothing was changed, safe to retry |
| 503 | `CONNECTIONS_UNAVAILABLE` | encryption key missing/malformed or migration not applied -- fail loud, never plaintext |

## Security model

| Concern | How |
|---|---|
| Tenant isolation | Tenant comes only from the validated JWT. Every store query joins through the **grant** for `community_id` *and* filters `platform_connections.tenant_id`; both must match. Creating re-verifies community -> tenant inside its transaction. |
| Community isolation | `authorize_community` is a DB-backed "admin of this exact community" check (the JWT scope alone cannot say *which* community). A sibling community in the same tenant sees `404`. |
| Scope | `connections:admin` -- carried by the tenant-owner bundle (`flask_core.auth.SCOPE_BUNDLES`) and by global admins via `*:admin`. Deliberately **not** `connections:read`: every session carries `*:read`, which would satisfy it for any logged-in user. |
| Feature/tier | Flag `waddles.integrations.provider_connections` (PostHog, default **OFF**) **and** Professional tier (`flask_core.tier_catalog`, contract in `libs/core_platform_module/features.py`). Evaluated *after* authz so an outsider learns nothing about a tenant's plan. |
| Encryption at rest | Tokens are AES-256-GCM ciphertext in `platform_connections.access_token` / `refresh_token` (random 96-bit IV per seal), under `CREDENTIAL_ENCRYPTION_KEY` -- the platform baseline, the same key/format the other layer-2 readers use. A missing or malformed key is a loud `503`; there is no plaintext mode and no fallback key. The per-tenant envelope / customer-managed KMS (PR #789) is the Enterprise upgrade *on top*: implement `TokenVault` over it and pass it to the service -- no store/adapter/API change. |
| Decrypt only at the point of use | Admin reads never select a token column (a test captures the SQL). A token is decrypted only for a provider call, or for the data-plane `resolve` response -- which carries the **access token only**; the refresh token and OAuth app secret never leave hub-api. |
| Never logged | Logs go through `flask_core.safe_logging` (exception type, code, category and frames -- never the message). Tokens, ciphertext, bot/channel names and provider response bodies are asserted absent from logs, spans and metric labels by tests. httpx's INFO line (which prints full URLs) is filtered for Twitch hosts. |
| Output validation | Every response is an explicit DTO; request bodies reject unknown fields (`extra=forbid`). A regression test pins the exact field set. |
| Outbound | Provider URLs are fixed constants but still pass the SSRF guard; redirects are never followed; responses are size-bounded; timeouts 5 s connect / 15 s total. |
| Data plane auth | Short-lived Ed25519 **machine JWT**, scope `connections:resolve`, operator-plane `system` tenant (the routes span tenants by design). No shared key, no fallback. |
| Audit | Admin mutations pass `require_scope`, so the app-wide audit hook records them as `admin.action` for entitled tenants; `/api/v1/internal/` is intentionally unaudited plumbing. |

> **Operational caution.** Never enable SQLAlchemy SQL echo (`sqlalchemy.engine` at INFO/DEBUG) in a
> deployed environment: bound parameters -- ciphertext and adapter metadata -- would be written to
> the log. (A test-only driver, `aiosqlite`, logs parameters at DEBUG; production's `asyncpg` does
> not.)

## Token lifecycle and failure semantics

Twitch user tokens live about four hours and **rotate the refresh token on every use** (a spent
refresh token is dead). The framework therefore refreshes **under a row lock**
(`SELECT ... FOR UPDATE` on the connection row): concurrent resolvers queue, the first refreshes,
the rest re-read the rotated token. A real-Postgres test fires eight concurrent resolves and asserts
Twitch sees exactly one refresh.

| Situation | Result |
|---|---|
| Token valid for > 2 minutes | decrypted and returned; no provider call |
| Token near/after expiry, refresh succeeds | rotated pair persisted atomically, fresh token returned |
| Provider **rejects** the refresh token | link marked `expired`, `status_reason=refresh_rejected`; **row kept** so the admin can reconnect (rotate credentials via `PATCH` -> `active` again) |
| Provider **transient** failure (5xx, network) | transaction rolled back, nothing changes, `502`, retry later -- an outage never kills a connection |
| No platform Twitch OAuth app configured, or token minted by a different Twitch app | `409 CONNECTION_NOT_USABLE` / `refresh_not_configured`; nothing changes and no request is sent to Twitch (it would be rejected and wrongly mark the link dead) |
| Expired and no refresh token | link marked `expired` / `token_expired` |
| Grant disabled / pending / revoked, link expired / revoked | `409 CONNECTION_NOT_USABLE` with that precise `details.reason` |

Refresh uses the platform's own Twitch OAuth app (`TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET`, the
same variables `oauth_providers` already reads). The bot token must be minted by that app for
refresh to work; otherwise the connection works until the access token expires.

## Data-plane contract (for the `svc-ingest` follow-up)

`svc-ingest` has **no** database grant on the connection tables (`rbac-matrix.yaml`:
`privileges: []`) -- it reaches connections only through hub-api:

```
svc-ingest                              hub-api
   | POST /internal/service-token  (projected SA token)  -> short-lived Ed25519 JWT
   |                                                        scope connections:resolve, tenant=system
   | GET  /api/v1/internal/provider-connections?provider=twitch&capability=chat_ingest&after=0&limit=500
   |  <- {items:[{grantId, connectionId, communityId, tenantId, resourceId, capabilities,
   |              expiresAt, metadata:{channel_login, bot_login, ...}}], nextAfter}   (NO tokens)
   | for each new item: start an IRC session; for each removed item: stop it
   | POST /api/v1/internal/communities/{communityId}/provider-connections/{connectionId}/resolve
   |  <- {accessToken, expiresAt, metadata}     (refreshed under lock if stale)
```

* The list returns only **usable** bindings: grant `approved` **and** `enabled`, link `active`.
  Poll it on a fixed cadence (the same "periodic reconcile, not event-driven" posture as
  `role_sync_service`); diff against running sessions to start/stop. `nextAfter` is the cursor for
  the next page (`null` on the last); a capability-filtered page can be short without being the last.
* Call `resolve` before connecting and again before `expiresAt`. A `409 CONNECTION_NOT_USABLE` with
  `details.reason` of `disabled` / `pending_approval` / `revoked_grant` / `refresh_rejected` /
  `token_expired` means *stop that session*; `502` means retry with backoff. Never retry a `409`.
* The IRC session needs only `metadata.bot_login` (nick), `metadata.channel_login` (JOIN) and
  `accessToken` (PASS `oauth:<token>`).
* Two tenants may legitimately connect the **same** channel, so the list can hold several bindings
  for one `resourceId`. Open one IRC session per `(provider, resourceId)` and fan events out to every
  binding, or one session per binding -- the framework deliberately does not decide that for you.
* Responses are `Cache-Control: no-store`.

### svc-ingest's service identity (the scope grant)

hub-api's token endpoint (`hub_api/blueprints/service_jwt_bp.py`) only mints a scope the caller's
allow-listed identity carries. The allow-list is chart config, rendered into hub-api's
`SERVICE_JWT_IDENTITIES` env var and parsed at startup by
`flask_core.service_jwt.load_identities_from_env`:

| Piece | Where |
|---|---|
| Identity + scope policy | `k8s/helm/waddlebot/values.yaml` `serviceJwt.identities.svc-ingest` -- `spiffe://penguintech.io/<env>/svc-ingest`, `allowedScopes: ["connections:resolve", "connections:list", "dataplane:resolve-scope"]` (never `connections:resolve:static`), `tenant: system` | **Rendered only when `pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled=true`** (default false in `values.yaml`, true in `values-alpha.yaml`): the grant, the `SERVICE_JWT_*` env and the bootstrap-token mount are absent otherwise, so an internet-facing svc-ingest pod cannot mint a cross-tenant `system` token while the feature is off.
| Dedicated ServiceAccount | `k8s/helm/waddlebot/templates/service-jwt-rbac.yaml` (`svc-ingest`); the identity matches on namespace + this name, so no other pod can bootstrap it |
| Caller wiring | `templates/svc-ingest-rust.yaml`: `serviceAccountName: svc-ingest`, projected token (audience `hub-api`), `SERVICE_JWT_TOKEN_ENDPOINT`, `SERVICE_JWT_SA_TOKEN_PATH` |
| Verification | `hub_api/tests/provider_connections/test_chart_service_identity_grant.py` (renders the chart, mints through the real issuer, hits the real routes) and `k8s/helm/waddlebot/tests/test_svc_ingest_connections_scope_render.py` |

`egress:connect` (svc-ingest is also in `egressProxy.allowedCallerServices`) is deliberately **not**
granted here; add it in its own change when svc-ingest dials the egress proxy.

### Binding lifecycle: how a connection becomes visible to svc-ingest

The data-plane list returns only **usable** bindings (grant `approved` and `enabled`, link
`active`). Through the connection API there is **no manual approval step**:

1. `POST /api/v1/communities/<c>/provider-connections` (`create`, `service.py` ->
   `store.insert_connection`) inserts the link with `status=active` and this community's grant with
   `status=approved` (approver = the installing community admin, whose authority was already checked
   by `authorize_community`) and `enabled` = the request's `enabled` (default `true`).
2. The next `GET /api/v1/internal/provider-connections?provider=twitch&capability=chat_ingest` lists
   it (`service.list_for_dataplane` -> `store.list_dataplane`).
3. The only optional step is an admin's own toggle: `create` with `"enabled": false` (or a later
   `PATCH {"enabled": false}`) hides it until `PATCH {"enabled": true}`.

Prerequisites outside the framework: the tenant needs the flag
`waddles.integrations.provider_connections` ON **and** the Professional tier (admin routes return
`402` otherwise), and the platform Twitch app (`TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET`) must be the
app that minted the bot token, or refresh is refused (`409 CONNECTION_NOT_USABLE` / `refresh_not_configured`).

> **Gap -- the webui "Connect Twitch" OAuth buttons do NOT feed this store.** There are two OAuth
> flows today and neither creates a `platform_connections` row *with* a
> `community_connection_access` grant:
>
> | Flow | Callback | Writes |
> |---|---|---|
> | Community Connections page (`community_connections.py`, `services/community_connections.py`) | `/api/v1/connections/callback/<provider>` | `platform_integrations` (legacy store) |
> | Tenant Twitch bot-install (`tenant_twitch_install.py`, Bar Citizen role-sync) | `/api/v1/tenant/twitch-install/callback` | `tenant_platform_apps` + `platform_connections` with **no grant** (`twitch_install_credentials.py`) |
>
> A channel connected only through those flows therefore never appears in the data-plane list. Until
> the webui gains a provider-connections UI (listed as a follow-up above), register the binding with
> the API: `POST /api/v1/communities/<c>/provider-connections` with
> `{"provider":"twitch","credentials":{"access_token":"...","refresh_token":"..."},"metadata":{"channel_login":"penguinzplays"}}`.
> An OAuth-driven path needs a product decision first -- the channel to join is an admin input
> (bot account and channel differ, e.g. bot `waddlebots` joining `penguinzplays`), which the OAuth
> flow cannot infer.

### Redirect URIs (register these in the provider console)

`<base>` = `CONNECTIONS_CALLBACK_BASE_URL`, falling back to `PUBLIC_WEBUI_URL`
(`hub_api/config.py`; Helm: `webui.connectionsCallbackBaseUrl` / `webui.publicUrl`). The URI must
match the provider app's registered redirect **exactly** (scheme, host, port, path, no trailing
slash). Twitch accepts `http://` only for `localhost`, which is why alpha sets the callback base to
`localhost` while `publicUrl` stays the LAN address.

| Flow | URI |
|---|---|
| Community Connections OAuth (Twitch) | `<base>/api/v1/connections/callback/twitch` |
| Tenant Twitch bot-install | `<base>/api/v1/tenant/twitch-install/callback` |

Alpha (`values-alpha.yaml`: `connectionsCallbackBaseUrl: "http://localhost:30879"`, webui NodePort
30879) -- the literal URIs to register:

```
http://localhost:30879/api/v1/connections/callback/twitch
http://localhost:30879/api/v1/tenant/twitch-install/callback
```

The browser completing the consent must be able to reach `localhost:30879` (run it on the node, or
SSH-tunnel `-L 30879:localhost:30879`).

## Static-secret kinds (`external_http`, `aws_lambda`, `gcp_function`)

Not every target is an OAuth install. These three providers take a pasted, structured secret and a
target instead of a token pair (`connection_kind` 2/3/4; OAuth rows are 1).

```
POST /api/v1/communities/{id}/provider-connections
{"provider": "external_http",
 "secret":   {"scheme": 1, "document": {"token": "..."}},        # write-only, sealed on arrival
 "metadata": {"url": "https://hooks.example.com/waddles"}}
```

| Provider | `secret.scheme` (code) | document keys | `metadata` |
|---|---|---|---|
| `external_http` | 1 bearer, 2 basic, 3 api-key-header | `token` / `username`+`password` / `header`+`value` | `url` |
| `aws_lambda` | 4 aws-access-key | `access_key_id`, `secret_access_key` | `region`, `function` |
| `gcp_function` | 5 gcp-service-account | `client_email`, `private_key` | `project`, `region`, `function` |

- **Sealed at rest**: the JSON document is AES-256-GCM sealed (`platform_connections.sealed_secret`)
  under the row-bound AAD (`conn:<public_id>:<platform>`), `resource_id = public_id`, `access_token` NULL.
  No admin route, DTO, list, log or span carries it; the descriptor advertises `kind` + `secretSchemes` only.
- **Structural validation only** -- no outbound call at write time. Endpoint URLs must be https, with no
  userinfo/fragment and no private / loopback / link-local / CGNAT / NAT64 / 6to4 IP literal. The host must be a bracketed IPv6, a canonical dotted-quad, or a DNS name whose last label starts with a letter (obfuscated spellings such as `2130706433`, `0x7f.0.0.1`, `127.1` are refused), and the canonical re-serialised URL is what is stored (DNS-name hosts are
  re-checked, DNS-pinned, at send time by the dispatcher). Secret documents have exact keys per scheme.
- Gated by flag `waddles.integrations.static_connections` (Professional, default OFF) on create, on top
  of the base feature; with it off the three providers are not even listed. A static link cannot be
  edited (delete + recreate). The data-plane `resolve` returns its `endpoint` + structured `secret`
  only to a caller holding `connections:resolve:static` (see [Scope matrix](#scope-matrix)); the base
  `connections:resolve` scope is refused with a generic `403`.

### Resolve v2 wire shape (all four kinds)

`POST /api/v1/internal/communities/{communityId}/provider-connections/{connectionId}/resolve` returns
`{connectionId, communityId, provider, kind, accessToken, expiresAt, endpoint:{url,region,function,project},
secret:{scheme, fields}, metadata}`. `kind` is the int code (1 `platform_oauth`, 2 `external_http`,
3 `aws_lambda`, 4 `gcp_function`); `secret.scheme` is 1 bearer, 2 basic, 3 api-key-header, 4 aws-access-key,
5 gcp-service-account. OAuth rows carry `accessToken` and no `secret`; static rows the reverse. Golden
fixtures: `hub_api/tests/fixtures/resolve_v2_*.json` (parsed byte-for-byte by the Rust contract test).
Responses are `Cache-Control: no-store`. The Rust client is `core/connection_resolver`: one authenticated
call per send, no credential cache, zeroizing secrets, `Debug` redacts them.

## Scope matrix

Each service identity mints only what it needs (chart `serviceJwt.identities`, rendered into hub-api's
`SERVICE_JWT_IDENTITIES`). Resolve tokens are **community-bound** (`community` claim, optional `conn`);
`connections:list` is unbound.

| Identity | `connections:resolve` | `connections:resolve:static` | `connections:list` | Gate (default false) |
|---|---|---|---|---|
| `svc-action` | yes (twitch OAuth, youtube, moderation sends) | yes (Lambda / GCP / HTTP kinds) | **no** | `pipeline.rustDataPlane.svcAction.dynamicConnections.enabled` -- strips only its `connections:*` scopes; its other scopes always render |
| `svc-ingest` | yes (twitch OAuth) | **no** | yes (binding discovery) | `pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled` -- the whole identity is absent while off |
| every other identity | no | no | no | -- |

Both flags are on only in `values-alpha.yaml`. The legacy Python `svc_action` pod
(`platform_moderation` / `youtube_oauth` via `resolve_community_tokens`) mounts the same
audience-scoped projected ServiceAccount token (`serviceJwtBootstrapVolume`) and, when the svcAction flag is
on, gets `SERVICE_JWT_TOKEN_ENDPOINT` + `SERVICE_JWT_SA_TOKEN_PATH`; without them it would silently stay on the
static env fallback after the cutover. Data-plane DB roles hold **no** privilege on `platform_connections`
(`rbac-matrix.yaml`; pinned by `hub_api/tests/provider_connections/test_rbac_no_data_plane_connection_privileges.py`).

## At-rest AAD (finding M4)

Every sealed value is AES-256-GCM bound to its row via associated data, so a ciphertext copied to another
row (or another platform) fails to open: `platform_integrations` uses `community_id:provider`; the
tenant-shared `platform_connections` uses `conn:{public_id}:{platform}`. `seal_version` is only a **hint**
(never the AAD switch -- a downgrade attempt cannot select the no-AAD path). Legacy rows (`seal_version` 0, no
AAD) still open through a fallback until you close it -- see the runbook below.

## `X-Service-Key` removed

The shared `X-Service-Key` header no longer authenticates either data-plane route nor the connection token
endpoint (HIGH-2): there is **no dual-accept window**. Callers use a machine JWT minted via
`/internal/service-token` from their projected ServiceAccount token. A request carrying only the old header
gets `401` (asserted live by `live_legacy_service_key_is_rejected`).

## SPIFFE readiness and its footguns

hub-api can take the caller identity from a verified peer header instead of the SA-token bootstrap:
`SPIFFE_PEER_TRUST` (chart `pipeline.hubApi.spiffePeerTrust`, default **false**) and `SPIFFE_PEER_ID_HEADER`
(`pipeline.hubApi.spiffePeerIdHeader`, default `X-Spiffe-Peer-Id`). Before enabling:

* the edge/mesh **must strip** `X-Spiffe-Peer-Id` on every non-mTLS path -- with trust on, whoever can set that
  header is that service;
* `X-Connection-Community` is **not** an entitlement check -- it only names the community; require a
  per-identity community allow-list first, otherwise any trusted identity could resolve any community.

## Reseal runbook (after deploy)

Rows written before the AAD change are `seal_version` 0 (no AAD). After deploying:

1. Run `make reseal-connections` from hub-api's environment (`DATABASE_URL`/`DB_*`, `CREDENTIAL_ENCRYPTION_KEY`).
   It prints `examined=N resealed=M failed=K`; re-run until it reports **`failed=0`** (exit 1 = a row failed,
   2 = nothing examined although rows exist, 3 = could not run; add `EXPECT_ROWS=1` to also fail on empty tables).
2. **Only then** set `CONNECTION_SEAL_REFUSE_LEGACY=1` on hub-api. This closes the legacy no-AAD fallback: any
   remaining no-AAD secret fails closed instead of opening. Setting it earlier turns un-resealed rows into outages.

## Live end-to-end (Rust client <-> real hub-api <-> Postgres)

`make test-connection-resolver-live` (`scripts/ci/connection-resolver-live.sh`, needs docker + helm + cargo and a
python with hub-api's requirements via `HUB_PYTHON`) boots Postgres 17 at Alembic head and the real blueprints
(`hub_api/tests/live/live_hub.py`, identities rendered from the real chart), then runs
`core/connection_resolver/tests/e2e_live_hub.rs`: per-kind round trip (decrypted fields equal what was stored),
cross-community `NotFound`, tampered ciphertext -> fixed error with no secret text, legacy `X-Service-Key` -> `401`,
and the chart's scope split. It prints the pass count and the resolver's log/span/metric counts and fails on zero.
Only the Kubernetes TokenReview call and the PostHog/licence backends are stubbed. Amd64-only for now.

## Adding a provider

A provider is **one adapter module + one registry line**. Nothing else changes -- the service,
store, API, DTOs, telemetry, encryption and tests are provider-agnostic, and a test
(`test_framework_boundary.py`) fails if framework code ever names Twitch.

1. Write `services/provider_connections/<name>.py` with an `AdapterSpec` (provider id, resource
   type, capabilities, required scopes, credential and metadata fields, the `public_` and
   `runtime_metadata_keys` allow-lists, whether it refreshes) and a class implementing
   `ProviderAdapter` (`normalize_credentials`, `validate_credentials`, `resolve_resource`, `refresh`,
   `granted_capabilities`). Stored `facts` must include every metadata field the admin supplied
   (normalised) so a later credential rotation can replay them.
2. Register it in `services/provider_connections/registry.py`.
3. If it introduces a new **resource kind**, extend the `platform_connections.resource_type` CHECK
   allow-list in a one-line migration (a deliberate, reviewable DB-level allow-list).
4. Add its tests next to `test_twitch_adapter.py`; `fake_provider.py` shows the minimum.

The `descriptors` endpoint then advertises it, and any UI/CLI that renders from descriptors needs no
change.

## Observability

OTel (OTLP endpoint configured by the standard `OTEL_*` env vars, never hardcoded):

| Signal | Name | Labels (bounded enums only) |
|---|---|---|
| counter | `waddles_provider_connection_ops_total` | `provider`, `operation`, `outcome` (`ok` or the error code) |
| histogram | `waddles_provider_connection_op_duration_seconds` | `provider`, `operation` |
| histogram | `waddles_provider_connection_provider_call_duration_seconds` | `provider`, `call` (`validate`/`resolve`/`refresh`) |
| counter | `waddles_provider_connection_token_refresh_total` | `provider`, `outcome` (`refreshed`, `rejected`, `unavailable`, `not_configured`, `already_rotated`, `no_refresh_token`) |
| histogram | `waddles_provider_connection_dataplane_list_size` | `provider` |
| spans | `provider_connections.{create,update,delete,resolve,dataplane_list}` | `provider`, `operation` |

Alert on a rising `outcome="rejected"` refresh rate (tokens being revoked at the provider) and on
`provider_unavailable` outcomes. Spans record only the exception **type**, never its message. Log
events are structured `provider_connections.*` lines carrying ids and counts only; most detail is at
DEBUG.

## Deploying

1. **Migrate.** `0058_provider_connections` (additive; no new table, no RBAC-matrix change).
2. **Key.** `CREDENTIAL_ENCRYPTION_KEY` is already provisioned for hub-api by the chart
   (`secrets.yaml`); without it connections fail loud with `503`.
3. **Refresh app.** hub-api must see `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` (the platform
   Twitch app) so refresh works -- the chart already injects the `platform-credentials` Secret
   into hub-api (`waddlebot.platformCredentialsSecretName`), so put them there. Mint the bot's
   token with that same app.
4. **Flag + tier.** Turn on `waddles.integrations.provider_connections` for the tenant and ensure it
   is Professional or above. Default OFF.
5. **Scope.** Tenant owners already hold `connections:admin` (new sessions); existing sessions pick
   it up on next login.

## Testing

```bash
cd hub_api
python3 -m pytest tests/provider_connections -q \
  --cov=services.provider_connections --cov=blueprints.v1.provider_connections --cov-branch
python3 -m pytest ../alembic/tests/test_0058_provider_connections.py -q   # real Postgres (docker)
```

Real-path principle: only the Twitch **socket** (`httpx.MockTransport`), DNS, and the entitlement
backends are replaced; the HTTP stack, auth chain, DB-backed community authz, entitlement client,
service, adapter, vault (real AES-GCM), store and machine-JWT verifier are all real.
`test_pg_integration.py` repeats the lifecycle on a migrated Postgres 17 -- schema parity with the
sqlite test mirror, FK/unique enforcement, JSONB/timestamptz, and the concurrent-refresh lock.

## Limitations and follow-ups

* **Credential intake is token-based.** The admin supplies the bot's access/refresh token. The
  redirect-style installs (`oauth_connect`, Discord `bot_invite_link`, Slack `app_manifest`) described
  in `CONNECTION_MODEL.local.md` and the existing per-community OAuth page
  (`/communities/{id}/connections/...`, stored in `platform_integrations`) are **not** folded in
  yet; the descriptor's `installKind` is `credentials` for now. Converging them onto this store is
  Step 2.
* **Sharing/approval flow.** Grants support `pending`, but there is no endpoint yet to request access
  to an existing link; creating a duplicate is a `409`.
* **Per-community admin delegation.** `connections:admin` is carried by tenant owners (and global
  admins). A community-only admin needs that scope issued to them; deriving it from community roles
  is a platform-wide change to session issuance, out of scope here.
* **Refresh uses the platform Twitch app only**, not a tenant's own app (layer 1). Tenant-app refresh
  is a follow-up.
* **AAD binding.** Done for connection secrets (see [At-rest AAD](#at-rest-aad-finding-m4)); the legacy no-AAD
  fallback stays open until the reseal runbook completes and `CONNECTION_SEAL_REFUSE_LEGACY=1` is set.
