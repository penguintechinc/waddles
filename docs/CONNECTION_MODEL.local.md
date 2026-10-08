# Waddles platform connection model (user-confirmed 2026-10-03)

Supersedes the per-tenant credential model that 0034 + PRs #565/#566 shipped (now being evolved forward).

## Model — THREE separate layers (user-confirmed 2026-10-03)
1. **App/bot credential = per tenant.** `tenant_platform_apps(tenant_id, platform) -> client_id/secret + bot_token` (encrypted). Tenant 0 = SaaS env creds, no row. "The per-tenant part is the app id / bot token."
2. **Connection = the Discord/Twitch -> Waddles link, per (tenant app, resource).** `platform_connections(tenant_id, platform, resource_type, resource_id, access_token, refresh_token, status, ...)` — the bot actually installed/authorized into a specific Discord GUILD or Twitch CHANNEL under that tenant's app. The TOKENS live HERE. Installed ONCE per resource (unique on tenant_id+platform+resource_id). resource_type enum (discord_guild, twitch_channel, …).
3. **Community access mapping = SEPARATE, no tokens.** `community_connection_access(community_id, connection_id, status, requested_by, approved_by)` — which communities may leverage a given connection. This is the user's "mapping of which communities can leverage that connection should be separate." NO credentials/tokens here — just the grant.

**Install once; reuse with approval.** A connection (layer 2) is created once per resource. When a community wants to use a connection that ALREADY exists, it does NOT re-install: it creates a PENDING `community_connection_access` row that a **server/guild admin must approve**; on approval the community REUSES the same connection (same bot presence/tokens). First-ever connect to a resource does the real OAuth bot-install and creates layer-2 + an approved layer-3 row.

## Resolver
`resolve(community_id, platform) -> (tenant app creds, community connection token)`. Tenant 0 -> SaaS. Missing/unapproved -> fail-closed (TransportUnavailable).

## Migration serialization
0034 (merged) has `tenant_platform_credentials` (tenant-scoped, conflates app+token). Evolve via ONE new migration off current head: introduce tenant_platform_apps + community_platform_connections + the reuse/approval linkage; migrate the app half of 0034's data into tenant_platform_apps. Only ONE migration PR open at a time.

**Status (2026-10-03, migration `0035_connection_model_layers`, merged):** landed exactly this shape — `tenant_platform_apps` (renamed from `tenant_platform_credentials`, same columns/trigger), `platform_connections` (new), `community_connection_access` (new), plus a `tenant_platform_credentials` compatibility VIEW (INSTEAD OF triggers) so the then-still-open PR #563 stacked chain kept working unmodified. See that migration's own docstring for the full rationale and the "Follow-up required" list this doc's Step 0 section below completes.

## Rework needed (C/D merged per-tenant -> modify forward)
- CredentialResolver (from #563/B): re-key to community connections.
- Discord install (#565) + Twitch install (#566): write community connections, with the reuse + server-admin-approval path.
- Admin UI (G): community connect UX with reuse+approval ("already connected -> request admin approval").
- Role-sync worker (F): resolve creds by community (minor call-site change).

---

## Step 0 (DONE, `feature/connection-model-port`): service-layer port off the compat shape

Migration 0035's own "Follow-up required" note named three things; this PR does (a) and the
`services/twitch_install_credentials.py` half of (b), scoped deliberately — see "What's
deferred" below.

**(a) `services/credential_resolver.py` ported onto the real tables, not the compat view:**
- `store_tenant_credentials()`/`resolve()` (layer 1) now read/write `tenant_platform_apps`
  directly. No hub-api code path reads/writes the `tenant_platform_credentials` compat view
  anymore (it remains, read-only from this PR's perspective, for any still-stacked PR that
  references the old name by table — see migration 0035's own docstring).
- New layer 2 primitives: `upsert_platform_connection()`, `get_platform_connection()`,
  `get_platform_connection_for_tenant()`, `delete_platform_connection()` — AES-256-GCM
  encrypted access/refresh tokens, same primitive layer 1 already used.
- New layer 3 primitives: `grant_community_connection_access()` (enforces a tenant-match
  guard between the community and the connection it's being granted — the actual isolation
  mechanism `resolve_community_connection()` depends on, not a bare query filter),
  `resolve_community_connection()` (only an `approved` grant to an `active` connection ever
  resolves).

**(b) `services/twitch_install_credentials.py` — the one install flow actually reworked:**
Twitch's tenant-install flow conflated a tenant's own app credentials (`client_id`/
`client_secret`) with the authorizing channel's OAuth grant (`access_token`/`refresh_token`)
into one `tenant_platform_credentials` row (migration 0034's documented shape:
`bot_token`=access_token, `extra.refresh_token`=refresh token). Split:
- Layer 1 (`tenant_platform_apps`): `client_id`/`client_secret` only.
- Layer 2 (`platform_connections`, `resource_type="twitch_channel"`): the channel token,
  keyed by `resource_id` = the authorizing broadcaster's own Twitch user id, resolved once at
  install time via a new `services/twitch_install_oauth.fetch_token_user_id()`
  (`GET /oauth2/validate` — Twitch's documented token-introspection call, no Helix scope
  needed) and reused unchanged across refreshes.
- A rejected refresh (reused/replayed refresh token) now revokes ONLY the layer-2 connection
  — layer 1's app credentials are untouched, since only the per-channel grant was compromised,
  not the tenant's app itself. (Previously the whole conflated row was deleted, forcing a
  full re-entry of `client_id`/`client_secret` too.)
- No layer-3 grant is created by this flow — it's tenant-scoped (a tenant admin authorizing
  their own channel), with no community in play at that point. Wiring a community's opt-in
  to reuse this connection is explicitly Step 1+ (Unit F/G below).

**What's deferred (explicitly, not an oversight):**
- **Discord's bot-install flow (`services/discord_install_service.py`) is UNCHANGED.** Its bot
  token is a true app-level credential (one static token for the whole Discord application,
  not scoped to any one guild — unlike Twitch's per-channel OAuth token), and today's OAuth
  callback never captures a `guild_id` to key a layer-2 row by in the first place. Capturing
  one is a separate, larger rework of that flow's own OAuth-callback contract (Discord returns
  `guild_id` as a query param on the bot-install consent redirect when requested) — Step 1/2
  work (Unit C), not guessed at here.
- **No caller of `resolve_community_connection()`/`grant_community_connection_access()` yet.**
  The primitives exist and are tested (unit + a real-Postgres round-trip), but wiring the
  role-sync worker (Unit F) to resolve creds by community, and the admin reuse+approval UX
  (Unit G), are Step 1+ — this PR is the data-access layer those units build on top of, per
  the task's own scope line ("don't build the generic AdapterSpec/API yet").
- **`tenant_platform_credentials` compat view retained.** Dropping it (migration 0035's item
  (c)) waits until no caller references the old name by any path — confirmed clear of hub-api
  service code by this PR, but the view's own removal is a follow-up migration, not bundled
  here (avoids opening a second migration PR concurrently with this one, per the "only ONE
  migration PR open at a time" rule above — this PR adds no migration at all).

**Library-extractability note (flagged for the future `penguin-aaa` move):** the resolver core
(`resolve()`/`store_tenant_credentials()`/the layer-2/3 primitives) stays Waddles-agnostic in
its own right — it takes a bound `dal` + plain scalars, no Waddles-specific types in its
signatures. The one piece of real, unavoidable coupling: `bind_bar_citizen_tables()`
(`services/schema.py`) assumes its caller has already bound `tenants`/`communities` under
those exact names (pydal's own idiom throughout this codebase, not specific to this module) —
a future library extraction needs an explicit table-name mapping seam here rather than the
hardcoded `dal.tenants`/`dal.communities` attribute access this port kept, to stay consistent
with every other service module in this file.

---

## Step 1+: the generic provider/connection framework (not built by Step 0)

This section is the written design the subsequent step-agents build against — summarized from
the orchestrating task's own brief, recorded here so it isn't re-derived per agent.

### Adapter interface
Each platform (`discord`, `twitch`, future: `youtube`, `kick`, `slack`, …) implements one
`AdapterSpec`-shaped registration (mirrors `services/oauth_providers.py`'s existing
`ProviderSpec` registry convention, extended for the three-layer model):

```python
@dataclass(slots=True, frozen=True)
class AdapterSpec:
    platform: str                      # "discord", "twitch", ...
    install_kind: str                   # oauth_connect | bot_invite_link | app_manifest
    resource_type: str                  # "discord_guild", "twitch_channel", ...
    authorize_url_builder: Callable[..., str]
    code_exchange: Callable[..., Awaitable[TokenResult]]
    resource_id_resolver: Callable[..., Awaitable[str]]   # token -> resource_id
    refresh: Callable[..., Awaitable[TokenResult]] | None  # None for non-refreshable (e.g. static bot tokens)
```

`install_kind` determines which install UX the webui renders and which callback shape hub-api
expects:
- `oauth_connect` — standard authorization-code flow (Twitch's tenant-install today).
- `bot_invite_link` — Discord-style "add bot to a guild" consent redirect, `guild_id` returned
  as a query param on success (not an OAuth code exchange in the traditional sense).
- `app_manifest` — Slack-style app-manifest install (future; no current caller).

### API surface
- `POST /api/v1/connections/<provider>/authorize` — tenant-admin-scoped, mints a `state`,
  returns the provider's authorize/invite URL (generalizes `tenant_twitch_install.py`'s
  `/authorize` route across all adapters).
- `GET /api/v1/connections/<provider>/callback` — public, provider's own redirect target;
  verifies `state`, exchanges the code (or reads the invite-flow's resource id), creates/
  updates the layer-2 `platform_connections` row via `upsert_platform_connection()`.
- `POST /api/v1/connections/<provider>/bind` — community-admin-scoped: given an existing
  connection (already installed by this tenant, possibly by a different community), create a
  layer-3 `community_connection_access` grant — `approved` immediately if the caller is the
  connection's own installer/first community, `pending` otherwise (reuse+approval path).
- `GET /internal/connections/<provider>/resolve?community_id=...` — service-to-service
  (`X-Service-Key`, same mechanism `community_connections.py`'s internal blueprint uses),
  wraps `resolve_community_connection()` + transparent refresh-before-expiry (same
  `_REFRESH_SKEW_S` pattern `blueprints/v1/community_connections.py` already established).

### Data-plane poll
The Rust data plane (core/svc_ingest et al.) never reaches into Postgres directly for
connection tokens — it polls `GET /internal/connections/<provider>/resolve` per active
community+platform pairing on a fixed cadence (mirrors `role_sync_service.py`'s own "periodic
reconcile, not event-driven" posture, `backend.md` service-to-service auth: short-lived signed
JWT/machine-token on every call, regardless of transport).

### Step 0 → Step 3 sequence (for the subsequent step-agents)
0. **(this PR)** Service-layer port off the compat shape onto `platform_connections` +
   `community_connection_access`; resolver primitives exist and are tested.
1. Generic `AdapterSpec` registry + the four API routes above, Twitch as the first real
   adapter (it already has the full layer-1/2 split from Step 0 to build against).
2. Discord adapter: rework the OAuth-callback contract to capture `guild_id`, port
   `discord_install_service.py` onto `upsert_platform_connection()` the same way Twitch was
   ported in Step 0.
3. Admin UI (Unit G) reuse+approval UX; role-sync worker (Unit F) resolves creds by community
   via `resolve_community_connection()` instead of its current tenant-wide `credential_
   resolver.resolve()` call for Discord's bot token.
