# Provider connections (standard connection framework)

One adapter interface, one connection API and one encrypted per-community token store for every
external platform Waddles talks to. **Twitch is the first provider**; Discord, Slack, Kick and
YouTube plug into the same framework (see [Adding a provider](#adding-a-provider)).

Implemented in **hub-api**: `hub_api/services/provider_connections/`,
`hub_api/blueprints/v1/provider_connections.py`, migration `0058_provider_connections`.
Design background and the storage model it builds on:
[`CONNECTION_MODEL.local.md`](CONNECTION_MODEL.local.md).

> **Transport (2026-11):** the data-plane reads (list + resolve) are internal **gRPC** on hub-api's
> internal listener (TLS pinned to the internal CA, machine JWT in `authorization` metadata, same
> scopes, `system` tenant, community-bound `Resolve`). The former `/api/v1/internal/...` REST routes
> are removed; a refusal to hand out a credential is `FAILED_PRECONDITION` with detail
> `not_usable:<reason>`, a missing/foreign binding is `NOT_FOUND`/`PERMISSION_DENIED`.

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
| Discord adapter (bot token + guild + managed-roles binding metadata) | **shipped** -- see [Discord provider](#discord-provider-guild--managed-roles) |
| YouTube adapter (OAuth connect + `live_chat_id` binding + discovery) | **shipped** -- see [YouTube provider](#youtube-provider-channel--live-chat-oauth-connect) |
| Slack / Kick adapters | follow-ups (one adapter + one registry line each) |
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
| 403 | `OWNERSHIP_NOT_PROVEN` | Twitch/Kick/YouTube: neither the token nor the installer's OAuth-linked account owns the channel (see [Channel ownership](#channel-ownership-proof-at-bind-time)) |
| 403 | `LIVE_CHAT_NOT_OWNED` | YouTube: `live_chat_id` is not one of the connected channel's own broadcasts |
| 404 | `NOT_FOUND` | no such connection in *your* community (same answer for "belongs to someone else") |
| 409 | `CONFLICT` | resource already connected in this tenant/community; shared link cannot be changed |
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
| Data plane auth | Short-lived ES256 (ECDSA P-256) **machine JWT**, scope `connections:resolve`, operator-plane `system` tenant (the routes span tenants by design). No shared key, no fallback. |
| Audit | Admin mutations pass `require_scope`, so the app-wide audit hook records them as `admin.action` for entitled tenants; `/api/v1/internal/` is intentionally unaudited plumbing. |

> **Operational caution.** Never enable SQLAlchemy SQL echo (`sqlalchemy.engine` at INFO/DEBUG) in a
> deployed environment: bound parameters -- ciphertext and adapter metadata -- would be written to
> the log. (A test-only driver, `aiosqlite`, logs parameters at DEBUG; production's `asyncpg` does
> not.)

## Channel ownership: proof at bind time

Security finding (High, release @1e4d2ee34): binding a channel never proved ownership, so a free
signup in another tenant could bind a victim's channel and svc-ingest's dedup then silenced both.

| Provider | Proof required at create / re-target / credential swap |
|---|---|
| Twitch | token owner id == channel (broadcaster) id, **or** the installing hub user has OAuth-linked (`hub_user_identities`, platform `twitch`) the broadcaster's account |
| Kick | token owner (`self_user_id`) == `broadcaster_user_id`, **or** a linked `kick` identity. No Kick account-linking flow exists yet, so in practice only the broadcaster's own token proves it |
| YouTube | `channels.list?mine=true` is scoped to the token, so the token proves the channel; an admin-typed `live_chat_id` must be one of that channel's own `liveBroadcasts` (else `LIVE_CHAT_NOT_OWNED`) |
| Discord | unchanged: bot-in-guild only (MANAGE_GUILD on the installer's linked account is NOT checked -- the login flow keeps no user token; follow-up) |
| Static / Wix / Spectrum / gameserver | no ownership notion |

- **Owner only.** A moderator is not an owner; the account-linking flow grants no moderated-channels scope to check it.
- `platform_connections.ownership_verified` (migration `0103_conn_ownership_verified`) records the proof; the admin DTO shows `ownershipVerified`; the data plane gets `metadata.ownership_verified = "true"|"false"` for ownership providers.
- **N:M is allowed.** One channel may be bound to many communities, across tenants (users pick context with `!community set context`). There is no global unique constraint; uniqueness stays `(tenant, platform, resource)` + one grant per community, so a duplicate in the same tenant/community is a 409.
- **svc-ingest dedup** (`connector_runtime/feed.rs`): when any binding of a channel is verified, every verified community keeps it (lowest connection id per community) and unverified duplicates are dropped with a WARN and `feed_skipped_total{reason="unverified"}`. With no verified binding (legacy rows) the old rule holds: one community keeps it, several are all dropped (`reason="conflict"`).
- **Legacy rows** stay `false` and keep routing (unless a verified binding of the same channel exists) until an owner edits the link, which re-proves and flags it. Alpha is disposable: no backfill.
- **Kill-switch `waddles.disable-unverified-binding-routing`** (svc-ingest, `connector_runtime/unverified_gate.rs`; user decision 2026-11-12). Unverified bindings keep routing through the demo; afterwards flip this flag, no code change needed.

  | Flag state | Unverified binding |
  |---|---|
  | unseen / OFF (default) | legacy rule above (a sole unverified claimant routes) |
  | ON | never routes: one WARN per skipped binding (ids + claim hash, never the handle) and `feed_skipped_total{reason="unverified"}` +1 per binding |

  Resolution: PostHog (when a snapshot exists) -> env baseline `FLAG_WADDLES_DISABLE_UNVERIFIED_BINDING_ROUTING=true|false` -> OFF. The `*.waddles.app` license bypass is ignored for this flag (bypass reads every flag ON, which would flip every environment at once). Re-read on every binding discovery, so a flip applies on the next reconcile.
- Telemetry: `waddles_provider_connection_ownership_total{provider,outcome}` (`token_owner`, `linked_account`, `rejected`, `live_chat_rejected`) and a WARN `provider_connections.ownership_rejected` (no handles, no tokens).

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
   | gRPC ServiceAuthService.MintServiceToken  (projected SA token, TLS 1.3)  -> short-lived ES256 JWT
   |                                                        scope connections:resolve, tenant=system
   | gRPC ProviderConnectionService.ListBindings{provider=twitch, capability=chat_ingest, after=0, limit=500}
   |  <- {items:[{grantId, connectionId, communityId, tenantId, resourceId, capabilities,
   |              expiresAt, metadata:{channel_login, bot_login, ...}}], nextAfter}   (NO tokens)
   | for each new item: start an IRC session; for each removed item: stop it
   | gRPC ProviderConnectionService.ResolveConnection{community_id, connection_id}
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

### Binding-feed fields (consumer contract for the connector runtime)

Rust-side consumer note (hub-api change only; `core/connection_resolver` `BindingWire` /
`BindingSummary` and `connector_runtime/feed.rs` adopt these in their own PR -- unknown fields are
ignored today, so this is additive). Every list item is exactly these keys, nothing else
(`DataPlaneBindingDTO`, `tests/provider_connections/test_binding_feed_fields.py` pins the set):

| Key | Type | Meaning / how a profile must use it |
|---|---|---|
| `grantId`, `connectionId`, `communityId` | int | numeric ids (cursor = last scanned `grantId`) |
| `tenantId` | int | **server-derived**: the tenant of the *community* row the grant points at (`communities.tenant_id`), never `resource_metadata`. A grant whose community tenant differs from the link's tenant, or whose community no longer exists, is **not served** (error-logged by ids). Profiles must read the tenant from this field only and refuse any tenant found in `metadata`. |
| `secretVersion` | int >= 1 | credential-rotation counter (migration `0074`, `platform_connections.secret_version`). Bumped atomically when an admin **replaces** the credential; **not** bumped by an OAuth token refresh, an `enabled` toggle or a metadata edit. Include it in the connection fingerprint: a change means *restart this connection* (re-`resolve` for the new token). It is a counter, never a hash or any secret material. |
| `capabilities` | string[] | effective set, sorted: stored capabilities unioned with what the adapter grants today, so rows connected before a capability existed are still served |
| `kind` | int | `ConnectionKind` code (1 `platform_oauth` ...) |
| `provider`, `resourceType`, `resourceId`, `expiresAt`, `updatedAt`, `metadata` | as before | `metadata` is the adapter's flat string->string binding contract |

`capability=chat_ingest` now returns rows for **Twitch, YouTube and Discord** (Discord's gateway
inbound feed runs on the same bot token as its `action_relay`; it was previously unlisted, so a
`chat_ingest` scan returned no Discord rows). An unknown capability is still a 400.

### svc-ingest's service identity (the scope grant)

hub-api's gRPC mint (`hub_api/grpc_internal/service_auth_servicer.py`) only mints a scope the caller's
allow-listed identity carries. The allow-list is chart config, rendered into hub-api's
`SERVICE_JWT_IDENTITIES` env var and parsed at startup by
`flask_core.service_jwt.load_identities_from_env`:

| Piece | Where |
|---|---|
| Identity + scope policy | `k8s/helm/waddlebot/values.yaml` `serviceJwt.identities.svc-ingest` -- `spiffe://penguintech.io/<env>/svc-ingest`, `allowedScopes: ["connections:resolve", "connections:list", "dataplane:resolve-scope", "identity:platform-user:resolve"]` (never `connections:resolve:static`; the last scope is the gRPC detokenizer, see [identity-resolve-platform-user](reference/identity-resolve-platform-user.md)), `tenant: system` | **Rendered only when `pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled=true`** (default false in `values.yaml`, true in `values-alpha.yaml`): the grant, the `SERVICE_JWT_*` env and the bootstrap-token mount are absent otherwise, so an internet-facing svc-ingest pod cannot mint a cross-tenant `system` token while the feature is off.
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
2. The next `ProviderConnectionService.ListBindings(provider=twitch, capability=chat_ingest)` lists
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
> | Tenant Twitch bot-install (`tenant_twitch_install.py`, role-sync) | `/api/v1/tenant/twitch-install/callback` | `tenant_platform_apps` + `platform_connections` with **no grant** (`twitch_install_credentials.py`) |
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
| YouTube connect (Google OAuth) | `<base>/api/v1/provider-connections/youtube/callback` |

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

`ProviderConnectionService.ResolveConnection` (gRPC, `proto/waddles/hub/internal/v1/provider_connection.proto`) returns
`{connectionId, communityId, provider, kind, accessToken, expiresAt, endpoint:{url,region,function,project},
secret:{scheme, fields}, metadata}`. `kind` is the int code (1 `platform_oauth`, 2 `external_http`,
3 `aws_lambda`, 4 `gcp_function`); `secret.scheme` is 1 bearer, 2 basic, 3 api-key-header, 4 aws-access-key,
5 gcp-service-account. OAuth rows carry `accessToken` and no `secret`; static rows the reverse. Golden
fixtures: `hub_api/tests/fixtures/resolve_v2_*.json` (parsed byte-for-byte by the Rust contract test).
Responses are `Cache-Control: no-store`. The Rust client is `core/connection_resolver`: one authenticated
call per send, no credential cache, zeroizing secrets, `Debug` redacts them.

## Wix outbound API credential, SoundCloud and YouTube API-key connections

- **Wix API credential** (`api_credential: {api_key, site_id}` on `POST`/`PATCH` of a `wix` connection):
  sealed (AES-256-GCM, row-bound AAD) in `platform_connections.sealed_api_credential` (migration `0082`),
  **write-only** -- admin DTOs carry only `hasApiCredential`. Wix API keys authenticate with the
  `Authorization` header + `wix-site-id` ([auth docs](https://dev.wix.com/docs/rest/articles/getting-started/authentication));
  Wix states keys are unavailable to third-party apps (those use an OAuth app-instance token, a separate,
  deferred flow). Resolve returns it ONLY to a caller holding `connections:resolve:static` as
  `secret = {scheme: 6 (wix_api_key), fields: {apiKey, siteId}}` next to the public key in `accessToken`;
  `connections:resolve` (svc-ingest) never sees it. Replacing it bumps `secretVersion`.
- **SoundCloud** (`soundcloud`, resource `soundcloud_app`): `credentials.access_token` = app client secret,
  `metadata.client_id` = public app id. **YouTube Data API key** (`youtube_api`, resource `youtube_api_key`):
  `credentials.access_token` = Google API key, `metadata.key_label` = admin name. Both resolve under the base
  `connections:resolve` scope (secret as `accessToken`, ids in `metadata`); structural validation only, no
  outbound call. Spotify app credentials are platform env/secret, not per community.
- **OAuth PKCE audit** (`services/oauth_providers.py`): S256 PKCE is on for YouTube/Google, Spotify and Kick
  (a missing challenge fails closed); Twitch, Discord and Slack do not document PKCE for the code grant.
  `state` is single-use (`GETDEL`) and bound to the initiating browser (cookie nonce hash) and user.

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
gRPC `ServiceAuthService.MintServiceToken` from their projected ServiceAccount token. A request carrying only the old header
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

## Discord provider (guild + managed roles)

`discord` (`services/provider_connections/discord.py`) is a `PLATFORM_OAUTH`-kind, **non-refreshable**
provider: the credential is the community's **bot token** (verified with `GET /users/@me` using the
`Bot` scheme; a user token is refused), the resource is the **guild** (verified with
`GET /guilds/{id}`; a bot that is not a member is a 422). Resolve returns the bot token as the kind-1
`accessToken` (there is no refresh token). The binding contract the outbound executor
(`core/svc_ingest` `outbound_creds_discord.rs`) reads is **non-secret resource metadata**:

| Metadata field | Required | Validation | Stored / served as |
|---|---|---|---|
| `guild_id` | yes | numeric snowflake, 17-20 digits; the bot must be a member | `guild_id` (also the connection's `resource_id`) |
| `managed_roles` | no | object `key -> role snowflake` (or the canonical string); **<= 32 entries**; key `[a-z][a-z0-9_]{0,31}`; role id numeric string 17-20 digits (JSON numbers refused -- precision loss); duplicates refused | canonical string `key:snowflake,key:snowflake`, sorted by key; omitted when empty |

Notes:

- **Keys must start with a letter.** The documented `^[a-z0-9_]{1,32}$` would admit `1abc`/`_x`, but the
  executor's `is_role_key` (`[a-z][a-z0-9_-]{0,31}`) rejects those and then refuses the WHOLE binding
  (fail closed). hub-api validates the intersection so an accepted binding is always usable.
- **Admin-only**: set through the ordinary connection routes (`POST` / `PATCH
  /api/v1/communities/<id>/provider-connections`), i.e. `connections:admin` **and** the DB-backed
  `authorize_community(admin=True)` check -- the same gate as every other connection write. The tenant
  comes from the token. `PATCH` with `metadata` **replaces** the whole metadata set (omit `managed_roles`
  to clear it) and re-verifies the guild.
- **Request shape**: `metadata` values may be a string or (for `managed_roles`) an object; unknown keys,
  a bad key, a non-numeric id, > 32 entries -> `400` naming only the field, never the value.
- **Where it surfaces**: the admin view (`metadata`), the internal list (`items[].metadata`) and resolve
  (`metadata`) -- exactly `{"guild_id": "...", "managed_roles": "key:id,..."}`. A binding with no
  `guild_id` is skipped by list and is `409 metadata_incomplete` on resolve.
- No migration: `resource_type = 'discord_guild'` was already in migration 0035's allow-list and the
  metadata rides the existing `resource_metadata` JSON column.

## YouTube provider (channel + live chat, OAuth connect)

`youtube` (`services/provider_connections/youtube.py`, install flow `services/youtube_install.py`,
routes `blueprints/v1/youtube_connections.py`) is a `PLATFORM_OAUTH`-kind, **refreshable** provider and
the first `installKind: oauth_connect` one. The community connects its **own YouTube channel**; the
outbound executor (`core/svc_ingest` `outbound_creds_youtube.rs` / `youtube_slots.rs`) then posts to
the **live chat** pinned by the binding.

```
admin ──POST …/provider-connections/youtube/authorize──▶ hub-api: state + PKCE ──▶ Google consent URL
browser ◀── redirect ── Google ── code ──▶ GET /api/v1/provider-connections/youtube/callback (PUBLIC)
                          state (GETDEL) ─▶ code+verifier exchange ─▶ service.create ─▶ sealed tokens
admin ──POST …/<conn>/discover (or PATCH metadata.live_chat_id)──▶ binding complete ──▶ data plane
```

**What the data plane receives** (`ProviderConnectionService.ResolveConnection`, gRPC; the JSON below is the camelCase view of the proto message, kept as the golden fixtures):
`kind: 1` + `accessToken` (the executor's `ConnectionSecret::OAuthAccessToken`), `expiresAt`
(RFC 3339; the executor refuses a token expiring within 30 s, hub-api refreshes inside a 120 s skew),
`metadata: {"live_chat_id": "..."}`, `secret: null`. Never the refresh token, client id/secret or tenant.

| Metadata field | Required | Validation | Stored / served as |
|---|---|---|---|
| `live_chat_id` | for the data plane (binding), not at create | `^[A-Za-z0-9_-]{1,128}$` (the executor's `is_live_chat_id`) | `live_chat_id` |

- **The binding is incomplete until `live_chat_id` is set**: the OAuth callback cannot know it, so a
  fresh connection is created without it, is skipped by the internal list and is `409
  metadata_incomplete` on resolve. Set it by `PATCH` (`{"metadata": {"live_chat_id": "..."}}`) or by
  **discovery**.
- **Discovery** -- `POST /api/v1/communities/<id>/provider-connections/<conn>/discover` (admin): one
  `liveBroadcasts.list?mine=true` call (1 quota unit) picks the channel's own `live` (else `testing`)
  broadcast, most recently started first, and writes its `liveChatId` through the same
  validate-and-verify path as a typed value. Guarded: SSRF guard + host allowlist, one page,
  one call per connection per 30 s (cross-replica Redis cooldown, `429 TOO_MANY_REQUESTS`), Google
  `quotaExceeded` -> `429 PROVIDER_QUOTA_EXCEEDED`, no active broadcast -> `422 RESOURCE_NOT_FOUND`
  (nothing written), a shared connection -> `409`. Provider-generic hook: an adapter implementing
  `MetadataDiscoverer` gets this endpoint for free; others answer `400`.
- **Scope**: exactly `https://www.googleapis.com/auth/youtube.force-ssl` (read + post live chat). No
  `readonly`. A token without it is `422 INSUFFICIENT_SCOPES`.
- **Per-tenant Google application** ([spec](superpowers/specs/2026-11-02-per-tenant-bot-credentials.md)):
  the **global** tenant uses the platform app from `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET`
  (Helm: the `waddlebot-platform-credentials` secret, already `envFrom`'d into hub-api); every other
  tenant brings its own, written by a **tenant admin** (`tenant:admin`) with `PUT /api/v1/tenants/youtube/app`
  `{"client_id", "client_secret"}` (sealed in `tenant_platform_apps`). The community flow can never supply
  or change it (`403 YOUTUBE_APP_TENANT_ADMIN_ONLY`), an existing app is never replaced by a different
  `client_id` (`409 YOUTUBE_APP_CONFLICT`; the secret of the same app may rotate), a tenant never falls
  back to the global app and the global tenant cannot store one. Without an app:
  `422 YOUTUBE_APP_NOT_CONFIGURED`.
  **Replacing** an app with a different `client_id` needs the explicit `"replace": true` (otherwise
  `409`); a tenant admin doing so expires every YouTube connection minted under the old app
  (`status=expired`, `status_reason=app_replaced`, sealed refresh token dropped, rows kept so each admin
  reconnects) BEFORE the new app is stored, logs `audit youtube_app_replaced tenant_id= actor_id=
  invalidated=` (no secrets), and the HTTP audit chain records the write like any scope-gated mutation;
  the response carries `invalidatedConnections`. The community flow can never replace or overwrite.
  **Audit query** (the adapter is new, so this should return zero rows; run it after any deploy to flag an
  app written by someone who is not a tenant admin -- the tenant-admin check is `tenant_admins`):
  ```sql
  SELECT a.tenant_id, a.installed_by_user_id, a.updated_at
    FROM tenant_platform_apps a
    LEFT JOIN tenant_admins t ON t.tenant_id = a.tenant_id AND t.user_id = a.installed_by_user_id
   WHERE a.platform = 'youtube' AND t.user_id IS NULL;
  ```
- **OAuth security**: authorization code + PKCE (`S256`) with `access_type=offline&prompt=consent`;
  single-use CSRF `state` (Redis `GETDEL`, 10 min TTL) carrying tenant/community/admin -- the public
  callback reads nothing tenant-shaped from the query; the verifier rides in the state AES-GCM-sealed
  with AAD bound to tenant + community; **the state is bound to the initiating browser** -- authorize sets a
  `__Host-yt_install` cookie (HttpOnly, Secure, SameSite=Lax, host-only) holding a random nonce whose SHA-256
  is stored in the state; the callback requires it (constant-time compare) BEFORE consuming, and a missing or
  foreign cookie gets the same generic `400` as an unknown state without burning it (so the authorize call
  and the callback must share a host: `connectionsCallbackBaseUrl` = the API origin the webui calls); the redirect URI is the
  server-configured callback (never client input); a response without a refresh token is refused
  (`no_refresh_token`); a replay, forged, expired or tampered state is `400 invalid_or_expired_state`.
  Hosts are an explicit allowlist (`accounts.google.com`, `oauth2.googleapis.com`,
  `www.googleapis.com`) *and* the shared SSRF guard; no redirects, bounded responses, tokens only in a
  header or form body.
- **Refresh**: Google access tokens last ~1 h. The framework refreshes under the row lock using the
  tenant's own app (the framework stamps `tenant_id` into `resource_metadata`; never served). Google
  normally omits the refresh token from a refresh answer (kept); if it rotates, the new one is stored
  sealed. `invalid_grant` (revoked / expired -- apps in "Testing" mode expire refresh tokens after 7
  days) marks the connection `status=expired, status_reason=refresh_rejected` (kept, `usable: false`,
  resolve `409 reason: refresh_rejected`) until the admin **reconnects** -- running `authorize` again
  for the same channel rotates the credentials of the existing connection in place. `invalid_client` /
  a token from another app -> `refresh_not_configured` (the token is NOT marked dead); an outage
  changes nothing.
- **Redirect URI to register in the Google Cloud console**: `<base>/api/v1/provider-connections/youtube/callback`
  (see [Redirect URIs](#redirect-uris-register-these-in-the-provider-console)).
- **Migration**: `0073_youtube_connection_resource` adds `youtube_channel` to the `resource_type` CHECK.
- **Quota**: connect = 2 units (`channels.list`), discovery = +1 per run (plus the re-verification),
  the executor's `liveChatMessages.insert` is the dominant cost (default 10,000 units/day per Google
  project, ~50 units per insert) -- a per-tenant app isolates tenants' quotas.

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
  yet; the descriptor's `installKind` is `credentials` for Twitch/Discord. YouTube is the first
  `oauth_connect` provider (its own connect flow, see [YouTube provider](#youtube-provider-channel--live-chat-oauth-connect));
  converging the rest onto this store is Step 2.
* **Sharing/approval flow.** Grants support `pending`, but there is no endpoint yet to request access
  to an existing link; creating a duplicate is a `409`.
* **Per-community admin delegation.** `connections:admin` is carried by tenant owners (and global
  admins). A community-only admin needs that scope issued to them; deriving it from community roles
  is a platform-wide change to session issuance, out of scope here.
* **Refresh uses the platform Twitch app only**, not a tenant's own app (layer 1). Tenant-app refresh
  is a follow-up.
* **AAD binding.** Done for connection secrets (see [At-rest AAD](#at-rest-aad-finding-m4)); the legacy no-AAD
  fallback stays open until the reseal runbook completes and `CONNECTION_SEAL_REFUSE_LEGACY=1` is set.
